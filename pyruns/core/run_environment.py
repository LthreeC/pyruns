"""Small, best-effort environment snapshots for individual task runs."""

from __future__ import annotations

import os
import platform
import shutil
import socket
from typing import Any

from pyruns.core.system_metrics import SystemMonitor


def collect_run_environment(
    command: list[str],
    env: dict[str, str],
    workdir: str | None,
    *,
    assigned_gpu_ids: list[int] | None = None,
    conda_env: str = "",
) -> dict[str, Any]:
    """Describe the launcher host and GPU visibility, never inferred GPU use.

    CUDA ordinals need not match NVIDIA device indices. Only scheduler-owned
    physical IDs or unambiguous GPU UUIDs are used to select inventory rows.
    Commands may activate another environment or enter a container internally;
    this snapshot describes the environment in which Pyruns launches them.
    """
    system = platform.system()
    release = platform.release()
    if system == "Linux":
        try:
            system = platform.freedesktop_os_release().get("PRETTY_NAME") or system
        except OSError:
            pass
        if "microsoft" in release.lower() or "WSL_INTEROP" in env:
            system += " (WSL)"
    else:
        system = f"{system} {release}".strip()

    executable = str(command[0]) if command else ""
    search_path = next((value for key, value in env.items() if key.upper() == "PATH"), "")
    if executable and (os.path.isabs(executable) or os.path.dirname(executable)):
        executable = os.path.abspath(os.path.join(workdir or os.getcwd(), executable))
    elif executable:
        executable = shutil.which(executable, path=search_path) or executable

    visible = env.get("CUDA_VISIBLE_DEVICES")
    assigned = list(assigned_gpu_ids or [])
    snapshot: dict[str, Any] = {
        "host": socket.gethostname(),
        "system": f"{system} · {platform.machine()}",
        "launcher": executable,
        "conda_env": conda_env,
        "cuda_visible_devices": visible,
        "assigned_gpu_ids": assigned,
        "gpu_scope": "assigned" if assigned else "detected",
        "gpu_status": "unavailable",
        "gpus": [],
    }
    if visible is not None and visible.strip() in {"", "-1"}:
        snapshot.update(gpu_scope="disabled", gpu_status="ok")
        return snapshot

    try:
        monitor = SystemMonitor()
        output = monitor._query_nvidia_smi("index,uuid,name,memory.total", scope="gpu")
        gpus = []
        for row in monitor._parse_csv_rows(output):
            if len(row) != 4:
                raise ValueError("Unrecognized GPU inventory response")
            index, uuid, name, memory = row
            gpus.append({
                "index": int(index),
                "uuid": uuid,
                "name": name,
                "memory_total_mb": monitor._coerce_optional_float(memory),
            })
        if assigned:
            gpus = [gpu for gpu in gpus if gpu["index"] in assigned]
            if len(gpus) != len(assigned):
                return snapshot
        elif visible:
            tokens = [token.strip() for token in visible.split(",")]
            if all(token.startswith("GPU-") for token in tokens):
                matches = [[gpu for gpu in gpus if gpu["uuid"].startswith(token)] for token in tokens]
                if all(len(match) == 1 for match in matches):
                    gpus = [match[0] for match in matches]
                    snapshot["gpu_scope"] = "visible"
        snapshot.update(gpus=gpus, gpu_status="ok")
    except Exception:
        # Optional telemetry must not prevent a command from running.
        pass
    return snapshot
