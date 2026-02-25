"""
Global log event emitter — thread-safe publish-subscribe for real-time logs.

Executor threads call ``emit()`` from their reader threads;
Monitor UI calls ``subscribe()`` / ``unsubscribe()`` per task.
``emit()`` uses ``call_soon_threadsafe`` to push data into the
NiceGUI (asyncio) event loop so that ``term.write()`` is always
called on the correct thread.
"""
import asyncio
import threading
from collections import defaultdict
from typing import Callable, Dict, List

from pyruns.utils import get_logger

logger = get_logger(__name__)


class LogEmitter:
    """Cross-thread log event bus.

    Subscribers receive decoded text chunks exactly as produced by the
    subprocess — no extra newline conversion is done here.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # { task_name: [callback, ...] }
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)

    def subscribe(self, task_name: str, callback: Callable) -> None:
        """Register *callback* to receive log chunks for *task_name*."""
        with self._lock:
            if callback not in self._subscribers[task_name]:
                self._subscribers[task_name].append(callback)

    def unsubscribe(self, task_name: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            subs = self._subscribers.get(task_name)
            if subs and callback in subs:
                subs.remove(callback)
                if not subs:
                    del self._subscribers[task_name]

    def emit(self, task_name: str, chunk_text: str) -> None:
        """Broadcast *chunk_text* to all subscribers of *task_name*.

        Called from executor reader threads — dispatches into the
        asyncio event loop via ``call_soon_threadsafe`` so that
        NiceGUI UI updates happen safely.
        """
        with self._lock:
            subs = list(self._subscribers.get(task_name, []))
        if not subs:
            return

        # Push into the asyncio event loop via call_soon_threadsafe so that
        # NiceGUI / Quasar element updates (e.g. term.write) happen on the
        # correct thread.  If no loop is running (e.g. in tests) call directly.
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        for cb in subs:
            try:
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(cb, chunk_text)
                else:
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
                    asyncio.create_task(cb(*args, **kwargs))
                else:
                    cb(*args, **kwargs)
            except Exception as e:
                logger.error("EventBus error emitting %s: %s", event_name, e)

event_sys = SimpleEventBus()
