"""Black-box contracts for the lightweight public package import."""

from __future__ import annotations

import subprocess
import sys
import threading
import time


def test_cli_app_import_keeps_runtime_dependencies_lazy() -> None:
    probe = """
import sys

import pyruns
import pyruns.cli.app

assert {
        "psutil",
        "pyruns.core.config_manager",
        "pyruns.utils.info_io",
        "winpty",
        "yaml",
}.isdisjoint(sys.modules)

from pyruns import ConfigManager, ensure_run_slot, load_task_info, run_slot_count, update_task_info

namespace = {}
exec("from pyruns import *", namespace)
assert set(pyruns.__all__).issubset(namespace)
assert pyruns.ConfigManager is ConfigManager
assert namespace["ConfigManager"] is ConfigManager
assert [
    ConfigManager.__name__,
    ensure_run_slot.__name__,
    load_task_info.__name__,
    run_slot_count.__name__,
    update_task_info.__name__,
] == [
    "ConfigManager",
    "ensure_run_slot",
    "load_task_info",
    "run_slot_count",
    "update_task_info",
]
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


def test_config_manager_lazy_initialization_is_thread_safe(monkeypatch) -> None:
    import pyruns

    constructor_calls: list[object] = []

    class SlowConfigManager:
        def __init__(self) -> None:
            constructor_calls.append(object())
            time.sleep(0.05)

    monkeypatch.setattr(pyruns, "ConfigManager", SlowConfigManager)
    monkeypatch.setattr(pyruns, "_global_config_manager_", None)
    start = threading.Barrier(3)
    managers: list[object] = []

    def resolve_manager() -> None:
        start.wait()
        managers.append(pyruns._get_config_manager())

    workers = [threading.Thread(target=resolve_manager) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert len(constructor_calls) == 1
    assert len(managers) == 2
    assert managers[0] is managers[1]
