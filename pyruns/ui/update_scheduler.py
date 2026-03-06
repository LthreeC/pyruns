"""
Reusable per-client debounced UI updater.

Used by pages that receive frequent background-thread events and need to
coalesce them into a single UI refresh on the NiceGUI loop.
"""
import asyncio
import inspect
import threading
from typing import Any, Callable

from nicegui.background_tasks import create

from pyruns.utils import get_logger

logger = get_logger(__name__)


class ClientDebouncedUpdater:
    """Thread-safe debounced scheduler for client-scoped UI callbacks."""

    def __init__(self, client, callback: Callable[[], Any], delay_sec: float = 0.12):
        self._client = client
        self._callback = callback
        self._delay = max(0.0, float(delay_sec))
        self._scheduled = False
        self._closed = False
        self._lock = threading.Lock()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def trigger(self) -> None:
        """Schedule one refresh callback if not already scheduled."""
        with self._lock:
            if self._closed or self._scheduled:
                return
            self._scheduled = True

        def _start_task() -> None:
            create(self._runner())

        self._dispatch(_start_task)

    def close(self) -> None:
        """Disable future scheduling (used on disconnect cleanup)."""
        with self._lock:
            self._closed = True

    def _dispatch(self, fn: Callable[[], None]) -> None:
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(fn)
            else:
                fn()
        except RuntimeError:
            # Loop already stopped; nothing to do during shutdown.
            pass

    async def _runner(self) -> None:
        try:
            if self._delay:
                await asyncio.sleep(self._delay)

            with self._lock:
                if self._closed:
                    return

            if getattr(self._client, "has_socket_connection", False) is False:
                return

            with self._client:
                result = self._callback()
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.debug("ClientDebouncedUpdater callback failed: %s", exc)
        finally:
            with self._lock:
                self._scheduled = False
