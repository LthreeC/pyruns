import pytest
from pyruns.utils.events import SimpleEventBus, LogEmitter


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
