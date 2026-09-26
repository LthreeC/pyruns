"""Observe real Windows delete-sharing denial across task lock release."""
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback


def observe(directory, mode, info_io, kernel):
    directory.mkdir()
    info_io.save_task_info(str(directory), {"notes": "before"})
    lock_path = directory / info_io._LOCK_FILENAME
    original_remove = info_io.os.remove
    events = []
    reader = [None]
    reader_lock = threading.Lock()
    timer_errors = []
    timer = None
    started = time.monotonic()
    result = {"mode": mode, "events": events}

    def close_reader():
        with reader_lock:
            if reader[0] is not None:
                if not kernel.CloseHandle(reader[0]):
                    raise ctypes.WinError(ctypes.get_last_error())
                reader[0] = None
                events.append({"event": "reader_closed", "seconds": time.monotonic() - started})

    def timed_close():
        try:
            close_reader()
        except Exception as error:
            timer_errors.append(repr(error))

    def remove(path, *args, **kwargs):
        if Path(path) != lock_path:
            return original_remove(path, *args, **kwargs)
        try:
            original_remove(path, *args, **kwargs)
        except OSError as error:
            events.append({"event": "remove_error", "seconds": time.monotonic() - started,
                           "type": type(error).__name__, "winerror": getattr(error, "winerror", None)})
            raise
        events.append({"event": "removed", "seconds": time.monotonic() - started})

    info_io.os.remove = remove
    try:
        with info_io.task_info_lock(str(directory), create_dir=False):
            result["owner_before_release"] = lock_path.read_text(encoding="utf-8")
            if mode != "unblocked":
                # Actual kernel sharing mode: permit reads/writes, deny deletion.
                handle = kernel.CreateFileW(str(lock_path), 0x80000000, 0x3, None, 3, 0x80, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                reader[0] = handle
                if mode == "short-reader":
                    timer = threading.Timer(0.15, timed_close)
                    timer.start()
            events.append({"event": "leaving_lock_body", "seconds": time.monotonic() - started})
        result["release_finished_seconds"] = time.monotonic() - started
        if timer is not None:
            timer.join(timeout=5)
            assert not timer.is_alive() and not timer_errors, timer_errors
        close_reader()
        result["file_remains_after_reader_closed"] = lock_path.exists()
        if lock_path.exists():
            result["remaining_owner"] = lock_path.read_text(encoding="utf-8")
            result["considered_stale"] = info_io._lock_file_is_stale(str(lock_path))
        attempt = time.monotonic()
        try:
            info_io.update_task_metadata(str(directory), lambda value: value.update(notes="after"))
        except TimeoutError as error:
            result["next_write"] = {"status": "timeout", "error": str(error)}
        else:
            result["next_write"] = {"status": "completed"}
        result["next_write"]["seconds"] = time.monotonic() - attempt
        result["saved_notes"] = info_io.load_task_metadata(str(directory), raise_error=True)["notes"]
        return result
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=5)
        close_reader()
        info_io.os.remove = original_remove
        # The only fixture data belongs to the enclosing TemporaryDirectory.


def main(output):
    assert os.name == "nt", "Run this observation only on Windows CI"
    root = Path(os.environ["PYRUNS_AUDIT_ROOT"]).resolve()
    sys.path.insert(0, str(root))
    info_io = importlib.import_module("pyruns.utils.info_io")
    assert Path(info_io.__file__).resolve().is_relative_to(root)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    report = {"observations": [], "completed": False,
              "info_io_sha256": hashlib.sha256(Path(info_io.__file__).read_bytes()).hexdigest(),
              "timeout_seconds": info_io._LOCK_TIMEOUT_SEC,
              "release_attempts": info_io._REPLACE_RETRY_COUNT,
              "release_delay_seconds": info_io._REPLACE_RETRY_DELAY_SEC}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="pyruns-release-boundary-") as directory:
            for mode in ("unblocked", "short-reader", "reader-through-release"):
                report["observations"].append(observe(Path(directory) / mode, mode, info_io, kernel))
                output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        assert all(row["next_write"]["status"] == "completed" for row in report["observations"][:2])
        report["completed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
