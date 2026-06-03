import pytest
from pyruns.utils.events import SimpleEventBus, LogEmitter


class RecordingLoop:
    def __init__(self):
        self.calls = []

    def is_running(self):
        return True

    def call_soon_threadsafe(self, callback, chunk):
        self.calls.append((callback, chunk))


def test_event_sys_subscribe_emit():
    """Test global EventBus publish/subscribe mechanics."""
    bus = SimpleEventBus()
    results = []
    
    def callback(data):
        results.append(data)
        
    bus.on("test_event", callback)
    bus.emit("test_event", "hello")
    bus.emit("test_event", "world")
    bus.emit("other_event", "ignored")
    
    assert results == ["hello", "world"]


def test_log_emitter_routing():
    """Test LogEmitter routing by task name."""
    emitter = LogEmitter()
    results = []
    
    def on_log(chunk):
        results.append(chunk)
        
    # Subscribe to "task1"
    emitter.subscribe("task1", on_log)
    
    emitter.emit("task1", "line1\n")
    emitter.emit("task2", "line2\n") # Shouldn't be received
    
    assert results == ["line1\n"]


def test_log_emitter_dispatches_each_subscriber_on_its_own_loop():
    """Concurrent UI subscribers must not overwrite each other's event loop."""

    emitter = LogEmitter()
    loop_a = RecordingLoop()
    loop_b = RecordingLoop()

    def on_a(chunk):
        raise AssertionError("callback should be scheduled, not called directly")

    def on_b(chunk):
        raise AssertionError("callback should be scheduled, not called directly")

    emitter.subscribe("task1", on_a, loop=loop_a)
    emitter.subscribe("task1", on_b, loop=loop_b)

    emitter.emit("task1", "live\n")

    assert loop_a.calls == [(on_a, "live\n")]
    assert loop_b.calls == [(on_b, "live\n")]
