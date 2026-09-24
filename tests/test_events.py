import os
from collections import deque

import pytest

from pyruns.utils.events import LogEmitter, SimpleEventBus


class RecordingLoop:
    def __init__(self):
        self.calls = []

    def is_running(self):
        return True

    def call_soon_threadsafe(self, callback, chunk):
        self.calls.append((callback, chunk))


def test_simple_event_bus_handles_sync_async_and_failing_callbacks():
    bus = SimpleEventBus()
    results = []

    async def async_listener(value):
        results.append(("async", value))

    def sync_listener(value):
        results.append(("sync", value))

    def failing_listener(value):
        raise RuntimeError("listener failed")

    bus.on("go", sync_listener)
    bus.on("go", sync_listener)
    bus.on("go", async_listener)
    bus.on("go", failing_listener)
    bus.emit("other", "ignored")
    bus.emit("go", "value")
    bus.off("go", sync_listener)
    bus.emit("go", "again")

    assert results == [("sync", "value")]


def test_log_emitter_routes_multiple_callbacks_and_unsubscribes():
    emitter = LogEmitter()
    primary = []
    secondary = []

    def record(chunk):
        primary.append(chunk)

    emitter.subscribe("task1", record)
    emitter.subscribe("task1", secondary.append)
    emitter.emit("task2", "ignored")
    emitter.emit("task1", "before")
    emitter.unsubscribe("task1", record)
    emitter.emit("missing", "ignored")
    emitter.emit("task1", "after")

    assert primary == ["before"]
    assert secondary == ["before", "after"]


def test_log_emitter_dispatches_running_loop_and_swallows_callback_errors():
    emitter = LogEmitter()
    received = []

    class RunningLoop:
        def __init__(self):
            self.calls = 0

        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback, *args):
            self.calls += 1
            callback(*args)

    loop = RunningLoop()

    def record(chunk):
        received.append(chunk)

    emitter.subscribe("task", record, loop=loop)
    emitter.subscribe("task", lambda chunk: (_ for _ in ()).throw(RuntimeError("callback failed")))
    emitter.bind_loop()
    emitter.emit("task", "chunk")
    emitter.unsubscribe("task", record)
    emitter.emit("task", "after")

    assert loop.calls == 1
    assert received == ["chunk"]


def test_log_emitter_dispatches_each_subscriber_on_its_own_loop():
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


def test_log_emitter_can_include_optional_metadata_without_changing_default_callbacks():
    emitter = LogEmitter()
    plain = []
    with_metadata = []

    emitter.subscribe("task1", plain.append)
    emitter.subscribe("task1", lambda chunk, metadata: with_metadata.append((chunk, metadata)), include_metadata=True)
    emitter.emit("task1", "live\n", offset=42, log_file_name="run1.log")

    assert plain == ["live\n"]
    assert with_metadata == [("live\n", {"offset": 42, "log_file_name": "run1.log"})]


def test_log_emitter_scopes_same_named_tasks_by_directory(tmp_path):
    emitter = LogEmitter()
    received = []
    task_a = tmp_path / "workspace-a" / "tasks" / "same-name"
    task_b = tmp_path / "workspace-b" / "tasks" / "same-name"

    emitter.subscribe(
        "same-name",
        lambda chunk, metadata: received.append((chunk, metadata)),
        include_metadata=True,
        task_dir=str(task_a),
    )
    emitter.emit("same-name", "wrong workspace\n", task_dir=str(task_b))
    emitter.emit("same-name", "right workspace\n", task_dir=str(task_a))

    assert len(received) == 1
    assert received[0][0] == "right workspace\n"
    assert received[0][1]["task_dir"] == os.path.normcase(str(task_a.resolve()))


class PausedLoop:
    def __init__(self):
        self.calls = deque()
        self.running = True

    def is_running(self):
        return self.running

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))

    def drain(self):
        while self.calls:
            callback, args = self.calls.popleft()
            callback(*args)


@pytest.mark.parametrize("limit_kind", ["chunks", "characters"])
def test_log_emitter_bounds_dispatch_and_reports_first_skipped_offset(limit_kind):
    emitter = LogEmitter()
    loop = PausedLoop()
    received, overflow = [], []
    emitter.subscribe(
        "task", lambda chunk, metadata: received.append((chunk, metadata)),
        loop=loop, include_metadata=True, on_overflow=overflow.append,
        max_pending_chunks=2 if limit_kind == "chunks" else 100,
        max_pending_chars=8 if limit_kind == "characters" else 1000,
    )

    for index in range(10_000):
        emitter.emit("task", "data", offset=(index + 1) * 4, byte_length=4, log_file_name="run1.log")

    assert len(loop.calls) == 3
    loop.drain()
    assert [meta["offset"] for _, meta in received] == [4, 8]
    assert overflow == [{"offset": 12, "byte_length": 4, "log_file_name": "run1.log"}]

    emitter.emit("task", "next", offset=40_004, byte_length=4, log_file_name="run1.log")
    loop.drain()
    assert received[-1] == ("next", {"offset": 40_004, "byte_length": 4, "log_file_name": "run1.log"})
    assert len(overflow) == 1


def test_log_emitter_oversized_chunk_retains_only_replay_metadata():
    emitter = LogEmitter()
    loop = PausedLoop()
    received, overflow = [], []
    emitter.subscribe(
        "task", received.append, loop=loop, max_pending_chars=4,
        on_overflow=overflow.append,
    )
    emitter.emit("task", "中" * 1000)
    loop.drain()
    assert received == []
    assert overflow == [{"byte_length": 3000}]
    emitter.emit("task", "ok")
    loop.drain()
    assert received == ["ok"]


def test_log_emitter_unsubscribe_cancels_queued_dispatch_and_overflow():
    emitter = LogEmitter()
    loop = PausedLoop()
    received, overflow = [], []

    def on_chunk(chunk):
        received.append(chunk)

    emitter.subscribe("task", on_chunk, loop=loop, max_pending_chunks=1, on_overflow=overflow.append)
    emitter.emit("task", "pending")
    emitter.emit("task", "overflow")
    emitter.unsubscribe("task", on_chunk)
    loop.drain()
    assert received == overflow == []


def test_log_emitter_dispatch_failure_releases_capacity():
    emitter = LogEmitter()
    loop = PausedLoop()
    received = []
    emitter.subscribe("task", received.append, loop=loop, max_pending_chunks=1, on_overflow=lambda _: None)
    schedule = loop.call_soon_threadsafe

    def fail(*args):
        raise RuntimeError("event loop closed")

    loop.call_soon_threadsafe = fail
    emitter.emit("task", "failed")
    loop.call_soon_threadsafe = schedule
    emitter.emit("task", "next")
    loop.drain()
    assert received == ["next"]
    loop.running = False
    emitter.emit("task", "stopped")
    assert received == ["next"]


def test_log_emitter_callback_failure_releases_capacity():
    emitter = LogEmitter()
    loop = PausedLoop()
    received = []

    def on_chunk(chunk):
        received.append(chunk)
        if chunk == "failing":
            raise RuntimeError("callback failed")

    emitter.subscribe("task", on_chunk, loop=loop, max_pending_chunks=1, on_overflow=lambda _: None)
    emitter.emit("task", "failing")
    loop.drain()
    emitter.emit("task", "next")
    loop.drain()
    assert received == ["failing", "next"]
