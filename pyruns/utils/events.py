"""
Global log event emitter — thread-safe publish-subscribe for real-time logs.

Executor threads call ``emit()`` from their reader threads;
Monitor UI calls ``subscribe()`` / ``unsubscribe()`` per task.
``emit()`` uses ``call_soon_threadsafe`` to push data into the
bound asyncio event loop so websocket/UI callbacks run on the
correct thread.
"""
import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from pyruns.utils import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class _LogSubscriber:
    callback: Callable
    loop: Any = None


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

    def subscribe(self, task_name: str, callback: Callable, loop=None) -> None:
        """Register *callback* to receive log chunks for *task_name*."""
        subscriber = _LogSubscriber(callback=callback, loop=loop)
        with self._lock:
            if not any(item.callback is callback for item in self._subscribers[task_name]):
                self._subscribers[task_name].append(subscriber)

    def unsubscribe(self, task_name: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            subs = self._subscribers.get(task_name)
            if subs:
                subs[:] = [item for item in subs if item.callback is not callback]
                if not subs:
                    del self._subscribers[task_name]

    def emit(self, task_name: str, chunk_text: str) -> None:
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

        for subscriber in subs:
            cb = subscriber.callback
            loop = subscriber.loop or default_loop
            try:
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(cb, chunk_text)
                else:
                    # CLI / non-async context: call directly (thread-safe
                    # by convention — CLI consumers use queue.Queue).
                    cb(chunk_text)
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
