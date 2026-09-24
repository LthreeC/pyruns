"""
Global log event emitter — thread-safe publish-subscribe for real-time logs.

Executor threads call ``emit()`` from their reader threads;
Monitor UI calls ``subscribe()`` / ``unsubscribe()`` per task.
``emit()`` uses ``call_soon_threadsafe`` to push data into the
bound asyncio event loop so websocket/UI callbacks run on the
correct thread.
"""
import asyncio
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from pyruns.utils import get_logger

logger = get_logger(__name__)


class _BoundedLogDispatch:
    """Bound work waiting for an asyncio loop; recover overflow from disk."""

    def __init__(self, max_chunks: int, max_chars: int, on_overflow: Callable):
        self._lock = threading.Lock()
        self._max_chunks = max_chunks
        self._max_chars = max_chars
        self._on_overflow = on_overflow
        self._pending = 0
        self._pending_chars = 0
        self._overflow_pending = False
        self._active = True

    def close(self) -> None:
        with self._lock:
            self._active = False

    def submit(self, loop, callback: Callable, args: tuple) -> None:
        chars = len(args[0])
        with self._lock:
            if not self._active:
                return
            overflow = (
                self._pending >= self._max_chunks
                or self._pending_chars + chars > self._max_chars
            )
            if overflow:
                if self._overflow_pending:
                    return
                self._overflow_pending = True
            else:
                self._pending += 1
                self._pending_chars += chars

        # An overflow notification retains no log text and is coalesced to one
        # callback. Consumers can wake their disk reader even if no more output
        # arrives after this burst.
        if overflow:
            metadata = dict(args[1]) if len(args) > 1 else {}
            if "byte_length" not in metadata:
                metadata["byte_length"] = len(args[0].replace("\r\n", "\n").encode("utf-8", errors="replace"))
            callback, args, chars = self._on_overflow, (metadata,), 0
        try:
            loop.call_soon_threadsafe(self._deliver, callback, args, chars, overflow)
        except Exception:
            self._release(chars, overflow)
            raise

    def _release(self, chars: int, overflow: bool) -> None:
        with self._lock:
            if overflow:
                self._overflow_pending = False
            else:
                self._pending -= 1
                self._pending_chars -= chars

    def _deliver(self, callback: Callable, args: tuple, chars: int, overflow: bool) -> None:
        try:
            with self._lock:
                active = self._active
            if active:
                callback(*args)
        except Exception as exc:
            logger.debug("LogEmitter callback error: %s", exc)
        finally:
            self._release(chars, overflow)


@dataclass(frozen=True)
class _LogSubscriber:
    callback: Callable
    loop: Any = None
    include_metadata: bool = False
    task_dir: str | None = None
    dispatch: _BoundedLogDispatch | None = None


def _normalize_task_dir(task_dir: str | None) -> str | None:
    if not task_dir:
        return None
    return os.path.normcase(os.path.abspath(task_dir))


class LogEmitter:
    """Cross-thread log event bus.

    Subscribers receive decoded text chunks exactly as produced by the
    subprocess — no extra newline conversion is done here.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # { task_name: [callback, ...] }
        self._subscribers: Dict[str, List[_LogSubscriber]] = defaultdict(list)
        self._loop = None

    def bind_loop(self, loop=None) -> None:
        """Bind a UI asyncio loop for thread-safe callback dispatch."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        with self._lock:
            self._loop = loop

    def subscribe(
        self,
        task_name: str,
        callback: Callable,
        loop=None,
        include_metadata: bool = False,
        task_dir: str | None = None,
        *,
        max_pending_chunks: int = 256,
        max_pending_chars: int = 1024 * 1024,
        on_overflow: Callable | None = None,
    ) -> None:
        """Register *callback* to receive log chunks for *task_name*.

        Supplying ``on_overflow`` bounds asyncio dispatch before the event loop
        can accept work. The consumer must replay skipped chunks from disk;
        the overflow callback receives the first skipped chunk's metadata on
        its loop. Ordinary subscribers retain their complete stream.
        """
        if max_pending_chunks < 1 or max_pending_chars < 1:
            raise ValueError("Pending log limits must be positive")
        subscriber = _LogSubscriber(
            callback=callback,
            loop=loop,
            include_metadata=include_metadata,
            task_dir=_normalize_task_dir(task_dir),
            dispatch=(
                _BoundedLogDispatch(max_pending_chunks, max_pending_chars, on_overflow)
                if on_overflow is not None else None
            ),
        )
        with self._lock:
            if not any(item.callback is callback for item in self._subscribers[task_name]):
                self._subscribers[task_name].append(subscriber)

    def unsubscribe(self, task_name: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            subs = self._subscribers.get(task_name)
            if subs:
                for item in subs:
                    if item.callback is callback and item.dispatch is not None:
                        item.dispatch.close()
                subs[:] = [item for item in subs if item.callback is not callback]
                if not subs:
                    del self._subscribers[task_name]

    def emit(
        self,
        task_name: str,
        chunk_text: str,
        *,
        offset: int | None = None,
        byte_length: int | None = None,
        log_file_name: str | None = None,
        task_dir: str | None = None,
    ) -> None:
        """Broadcast *chunk_text* to all subscribers of *task_name*.

        Called from executor reader threads — dispatches into the
        asyncio event loop via ``call_soon_threadsafe`` so websocket
        and UI updates happen safely.

        For CLI subscribers (no asyncio loop), callbacks are called
        directly from the emit thread — CLI consumers should use
        thread-safe data structures (e.g. ``queue.Queue``).
        """
        with self._lock:
            subs = list(self._subscribers.get(task_name, []))
            default_loop = self._loop
        if not subs:
            return

        metadata: Dict[str, Any] = {}
        if offset is not None:
            metadata["offset"] = offset
        if byte_length is not None:
            metadata["byte_length"] = byte_length
        if log_file_name:
            metadata["log_file_name"] = log_file_name
        normalized_task_dir = _normalize_task_dir(task_dir)
        if normalized_task_dir:
            metadata["task_dir"] = normalized_task_dir

        for subscriber in subs:
            if subscriber.task_dir is not None and subscriber.task_dir != normalized_task_dir:
                continue
            cb = subscriber.callback
            loop = subscriber.loop or default_loop
            args = (chunk_text, metadata) if subscriber.include_metadata else (chunk_text,)
            try:
                if loop and loop.is_running():
                    if subscriber.dispatch is not None:
                        subscriber.dispatch.submit(loop, cb, args)
                    else:
                        loop.call_soon_threadsafe(cb, *args)
                elif loop and subscriber.dispatch is not None:
                    # A stopped UI loop must not run asyncio callbacks on an
                    # executor thread. Its persisted logs remain replayable.
                    continue
                else:
                    # CLI / non-async context: call directly (thread-safe
                    # by convention — CLI consumers use queue.Queue).
                    cb(*args)
            except Exception as exc:
                logger.debug("LogEmitter callback error: %s", exc)


# ── Module-level singleton ──
log_emitter = LogEmitter()


class SimpleEventBus:
    """A minimal event bus for coordinating cross-component UI actions (e.g. Tab switch)."""
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)

    def on(self, event_name: str, callback: Callable):
        if callback not in self._listeners[event_name]:
            self._listeners[event_name].append(callback)

    def off(self, event_name: str, callback: Callable):
        if callback in self._listeners[event_name]:
            self._listeners[event_name].remove(callback)

    def emit(self, event_name: str, *args, **kwargs):
        for cb in self._listeners.get(event_name, []):
            try:
                import asyncio
                if asyncio.iscoroutinefunction(cb):
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.ensure_future(cb(*args, **kwargs), loop=loop)
                    except RuntimeError:
                        # No running event loop — skip async callback safely
                        logger.debug("EventBus: skipping async callback %s (no event loop)", cb)
                else:
                    cb(*args, **kwargs)
            except Exception as e:
                logger.error("EventBus error emitting %s: %s", event_name, e)

event_sys = SimpleEventBus()
