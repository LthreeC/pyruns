"""Identify the execution environment whose process IDs a runner can inspect."""

import hashlib
import os
import re
import socket
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _machine_fingerprint() -> str | None:
    """Hash the OS installation identifier before using it in task metadata or paths."""
    try:
        if sys.platform.startswith("linux"):
            machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", machine) or int(machine, 16) == 0:
                return None
        elif sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                machine = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        elif sys.platform == "darwin":
            output = subprocess.check_output(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL, timeout=2,
            )
            match = re.search(rb'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', output)
            machine = match[1].decode("ascii") if match else ""
        else:
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not machine:
        return None
    return hashlib.sha256(f"pyruns:runner:v1:{sys.platform}:{machine.lower()}".encode()).hexdigest()


@lru_cache(maxsize=1)
def local_runner_device() -> str | None:
    machine = _machine_fingerprint()
    if not machine or not sys.platform.startswith("linux"):
        return machine
    try:
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    return hashlib.sha256(f"{machine}:{namespace}".encode()).hexdigest()


@lru_cache(maxsize=1)
def local_runner_host() -> str:
    return socket.gethostname().strip().lower()


@lru_cache(maxsize=1)
def device_tasks_folder() -> str:
    host = re.sub(r"[^a-z0-9._-]+", "-", local_runner_host()).strip(".-")[:48] or "device"
    fingerprint = _machine_fingerprint() or hashlib.sha256(f"{sys.platform}:{local_runner_host()}".encode()).hexdigest()
    return f"tasks-{host}-{fingerprint[:12]}"


def record_runner_device(info: dict[str, Any]) -> None:
    device = local_runner_device()
    if device:
        info["runner_device"] = device
    else:
        info.pop("runner_device", None)


def runner_location(info: dict[str, Any], local_host: str | None = None) -> str:
    """Classify recorded ownership without looking up a foreign PID."""
    host = str(info.get("runner_host") or "").strip().lower()
    if not host:
        return "unknown"
    if host != (local_host if local_host is not None else local_runner_host()):
        return "remote"
    device = info.get("runner_device")
    if device and (local := local_runner_device()):
        return "local" if device == local else "remote"
    return "unknown"
