"""Temporary bounded diagnosis of the existing queued log-switch regression."""
from collections import deque
import json
import os
from pathlib import Path
import sys
import time

import anyio
import pytest
from starlette.testclient import WebSocketTestSession

ROOT = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


class Trace:
    def __init__(self, output):
        self.output = output
        self.reports = []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        events = deque(maxlen=200)
        runtimes = []
        started = time.monotonic()
        module = item.module
        build = module._build_runtime
        original_receive = WebSocketTestSession.receive

        def record(kind, **data):
            events.append({"seconds": time.monotonic() - started, "kind": kind, **data})

        def traced_build(*args, **kwargs):
            runtime = build(*args, **kwargs)
            runtimes.append(runtime)
            get_task, get_logs = runtime.get_task, runtime.get_task_logs
            manager = runtime.task_manager
            apply_info = manager._apply_info_to_task

            def traced_task(*args, **kwargs):
                before = manager._tasks_by_name.get("alpha", {}).get("status")
                try:
                    result = get_task(*args, **kwargs)
                except Exception as error:
                    record("get_task_error", error=repr(error), before=before)
                    raise
                record("get_task", refresh=kwargs.get("refresh", True), before=before,
                       after=(result or {}).get("status"), run_index=(result or {}).get("run_index"))
                return result

            def traced_logs(*args, **kwargs):
                try:
                    result = get_logs(*args, **kwargs)
                except Exception as error:
                    record("get_logs_error", error=repr(error))
                    raise
                record("get_logs", selected=result.get("selected_log"), offset=result.get("offset"),
                       content=result.get("content"), requested=kwargs)
                return result

            def traced_apply(task, info, **kwargs):
                record("apply_info", before=task.get("status"), disk=info.get("status"),
                       payload_signature=repr(task.get("_payload_signature")),
                       probed_payload=repr(manager._payload_signature(task["dir"], task["config_file"])),
                       cached_info_signature=repr(task.get("_info_signature")),
                       info_signature=repr(kwargs.get("info_signature")))
                return apply_info(task, info, **kwargs)

            runtime.get_task = traced_task
            runtime.get_task_logs = traced_logs
            manager._apply_info_to_task = traced_apply
            return runtime

        def bounded_receive(session):
            async def receive():
                with anyio.fail_after(5):
                    return await session._send_rx.receive()
            result = session.portal.call(receive)
            record("receive", message=result)
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "_build_runtime", traced_build)
            patch.setattr(WebSocketTestSession, "receive", bounded_receive)
            outcome = yield
        snapshots = []
        for runtime in runtimes:
            manager = runtime.task_manager
            task = manager._tasks_by_name.get("alpha", {})
            snapshots.append({
                "task": {key: task.get(key) for key in ("status", "run_index", "dir")},
                "disk": json.loads((Path(task["dir"]) / module.TASK_INFO_FILENAME).read_text()),
            })
            runtime.shutdown()
        self.reports.append({"nodeid": item.nodeid, "failed": outcome.excinfo is not None,
                             "events": list(events), "snapshots": snapshots})
        self.output.write_text(json.dumps(self.reports, indent=2, default=str) + "\n", encoding="utf-8")
        assert WebSocketTestSession.receive is original_receive


if __name__ == "__main__":
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    node = "tests/test_web.py::test_logs_websocket_idle_queue_skips_log_listing_until_run_starts"
    trace = Trace(output)
    for attempt in range(attempts):
        code = pytest.main(["-q", "-x", node], plugins=[trace])
        if code:
            raise SystemExit(code)
    assert len(trace.reports) == attempts
    print(f"Completed {len(trace.reports)} distinct invocations")
