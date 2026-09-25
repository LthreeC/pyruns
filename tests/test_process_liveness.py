"""Fast Windows liveness checks must preserve suspended and exited states."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pyruns.utils import process_utils


@pytest.mark.parametrize("exists", [True, False])
def test_windows_liveness_does_not_inspect_thread_suspension(monkeypatch, exists):
    calls = []

    def pid_exists(pid):
        calls.append(pid)
        return exists

    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_utils, "_psutil", SimpleNamespace(
        pid_exists=pid_exists,
        Process=lambda pid: pytest.fail("liveness must not enumerate process threads"),
    ))
    assert process_utils.is_pid_running("4242") is exists
    assert calls == [4242]


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle lifecycle")
def test_windows_liveness_preserves_suspended_process_and_detects_exit():
    import psutil

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process_utils.is_pid_running(process.pid)
        psutil.Process(process.pid).suspend()
        assert process_utils.is_pid_running(process.pid)
        psutil.Process(process.pid).resume()
        process.terminate()
        process.wait(timeout=10)
        # Popen still owns a handle to the terminated process here.
        assert not process_utils.is_pid_running(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
