"""Observe settings/name recovery with real Windows delete-sharing conflicts."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch


def observe(root, kind, mode, settings, generator_module, kernel):
    directory = root / f"{kind}-{mode}"
    directory.mkdir()
    generator = generator_module.TaskGenerator(str(directory)) if kind == "task_name" else None
    if kind == "settings_unset":
        settings.save_settings_for_root(str(directory), {"header_refresh_interval": 3.0, "ui_port": 8099})
    lock_path = (Path(settings._settings_path(str(directory)) + ".lock") if generator is None
                 else Path(generator._task_name_lock_path("alpha")))
    result = {"kind": kind, "mode": mode, "events": []}
    reader = [None]
    reader_guard = threading.Lock()
    timer = None
    timer_errors = []
    started = time.monotonic()

    def close_reader():
        with reader_guard:
            if reader[0] is not None:
                if not kernel.CloseHandle(reader[0]):
                    raise ctypes.WinError(ctypes.get_last_error())
                reader[0] = None
                result["events"].append({"event": "reader_closed", "seconds": time.monotonic() - started})

    def timed_close():
        try:
            close_reader()
        except Exception as error:
            timer_errors.append(repr(error))

    def open_reader():
        nonlocal timer
        result["owner_before_release"] = lock_path.read_text(encoding="utf-8")
        if mode == "unblocked":
            return
        # GENERIC_READ; FILE_SHARE_READ | FILE_SHARE_WRITE, without FILE_SHARE_DELETE.
        handle = kernel.CreateFileW(str(lock_path), 0x80000000, 0x3, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        reader[0] = handle
        if mode == "short-reader":
            timer = threading.Timer(0.15, timed_close)
            timer.start()

    def record(operation):
        def perform(path, *args, **kwargs):
            if Path(path) != lock_path:
                return operation(path, *args, **kwargs)
            try:
                value = operation(path, *args, **kwargs)
            except OSError as error:
                result["events"].append({"event": operation.__name__, "seconds": time.monotonic() - started,
                                         "winerror": getattr(error, "winerror", None)})
                raise
            result["events"].append({"event": operation.__name__, "seconds": time.monotonic() - started,
                                     "winerror": None})
            return value
        return perform

    release = settings._release_settings_lock

    def release_settings(path, owner):
        assert Path(path) == lock_path
        open_reader()
        release(path, owner)

    try:
        with patch.object(os, "replace", record(os.replace)), patch.object(os, "remove", record(os.remove)), \
             patch.object(os, "unlink", record(os.unlink)):
            if generator is None:
                with patch.object(settings, "_release_settings_lock", release_settings):
                    if kind == "settings_unset":
                        settings.unset_setting_for_root(str(directory), "header_refresh_interval")
                    else:
                        settings.save_setting_for_root(str(directory), "header_refresh_interval", 3.0)
            else:
                reservation = generator.reserve_exact_task_name("alpha")
                assert reservation is not None
                open_reader()
                generator.release_task_name_reservation(reservation)
        result["release_seconds"] = time.monotonic() - started
        if timer is not None:
            timer.join(timeout=5)
            assert not timer.is_alive() and not timer_errors, timer_errors
        close_reader()
        result["lock_remains"] = lock_path.exists()
        if lock_path.exists():
            result["remaining_owner"] = lock_path.read_text(encoding="utf-8")
        attempt = time.monotonic()
        if generator is None:
            try:
                settings.save_setting_for_root(str(directory), "header_refresh_interval", 4.0)
            except TimeoutError as error:
                result["next_operation"] = {"status": "timeout", "error": str(error)}
            else:
                result["next_operation"] = {"status": "completed"}
            result["saved_value"] = settings.load_settings(str(directory))["header_refresh_interval"]
        else:
            assert not (directory / "alpha").exists()
            reservation = generator.reserve_exact_task_name("alpha")
            result["next_operation"] = {"status": "refused" if reservation is None else "completed"}
            if reservation is not None:
                generator.release_task_name_reservation(reservation)
        result["next_operation"]["seconds"] = time.monotonic() - attempt
        if mode != "reader-through-release":
            assert result["next_operation"]["status"] == "completed", result
        else:
            assert sum(event.get("winerror") == 32 for event in result["events"]) >= 15, result
        return result
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=5)
        close_reader()


def main(output):
    assert os.name == "nt", "This observation runs only on Windows CI"
    source_root = Path(os.environ["PYRUNS_AUDIT_ROOT"]).resolve()
    sys.path.insert(0, str(source_root))
    from pyruns.core import task_generator
    from pyruns.utils import settings

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    modules = {"settings": settings, "task_generator": task_generator}
    if "pyruns.utils.lock_owner" in sys.modules:
        modules["lock_owner"] = sys.modules["pyruns.utils.lock_owner"]
    assert all(Path(module.__file__).resolve().is_relative_to(source_root) for module in modules.values())
    report = {"completed": False, "observations": [],
              "source_sha": os.environ.get("PYRUNS_SOURCE_SHA", os.environ["GITHUB_SHA"]),
              "module_sha256": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                for name, module in modules.items()},
              "settings_timeout_seconds": settings._SETTINGS_LOCK_TIMEOUT_SEC,
              "release_attempts": {"settings": settings._SETTINGS_LOCK_RELEASE_ATTEMPTS,
                                   "task_name": task_generator._TASK_NAME_LOCK_RELEASE_ATTEMPTS}}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="pyruns-other-lock-release-") as directory:
            for kind in ("settings", "settings_unset", "task_name"):
                for mode in ("unblocked", "short-reader", "reader-through-release"):
                    report["observations"].append(observe(Path(directory), kind, mode, settings, task_generator, kernel))
                    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for row in report["observations"]:
            expected = "completed"
            if os.environ.get("PYRUNS_EXPECT_RELEASE_RECOVERY") == "0" and row["mode"] == "reader-through-release":
                expected = "refused" if row["kind"] == "task_name" else "timeout"
            assert row["next_operation"]["status"] == expected, row
        report["completed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
