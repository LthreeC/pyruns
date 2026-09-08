"""System metrics collector for CPU, RAM, and NVIDIA GPUs."""

from __future__ import annotations

import csv
import math
import os
import shlex
import subprocess
import time
from typing import Any, Dict, List

import psutil

from pyruns.utils.process_utils import hidden_subprocess_kwargs


class SystemMonitor:
    """Collect CPU, RAM, and optional GPU utilization metrics."""

    _GPU_QUERY_TIMEOUT_SEC = 1.0
    _PROCESS_COMMAND_LIMIT = 16_384
    _PROCESS_TEXT_LIMIT = 4_096
    _GPU_SUMMARY_FIELDS = (
        "index,name,uuid,utilization.gpu,memory.used,memory.total"
    )
    _GPU_DETAIL_FIELDS = (
        f"{_GPU_SUMMARY_FIELDS},utilization.memory,memory.free,"
        "temperature.gpu,"
        "fan.speed,power.draw,power.limit,pstate,compute_mode,"
        "clocks.current.graphics,clocks.current.memory,pci.bus_id,"
        "driver_version"
    )

    def __init__(self, *, gpu_ttl_sec: float = 1.5) -> None:
        self._gpu_cache: List[Dict[str, Any]] = []
        self._gpu_cache_at: float = 0.0
        self._gpu_cache_valid: bool = False
        self._gpu_process_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._gpu_process_cache_at: float = 0.0
        self._gpu_process_cache_valid: bool = False
        try:
            ttl = float(gpu_ttl_sec)
        except (TypeError, ValueError):
            ttl = 1.5
        self._gpu_ttl_sec: float = max(0.0, ttl)
        self._gpu_available: bool = True
        self._gpu_detail_query_supported: bool = True
        self._gpu_fail_count: int = 0
        self._gpu_max_fails: int = 3
        self._gpu_disabled_at: float = 0.0
        self._gpu_retry_sec: float = 30.0

    def sample(self, *, include_processes: bool = True) -> Dict[str, Any]:
        """Collect system metrics."""

        return {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics(include_processes=include_processes),
        }

    @staticmethod
    def _coerce_float(value: str, default: float = 0.0) -> float:
        """Parse one float-like CSV field safely."""

        try:
            return float(str(value or "").strip())
        except (TypeError, ValueError):
            return default

    @classmethod
    def _coerce_int(cls, value: str, default: int = 0) -> int:
        """Parse one integer-like CSV field safely."""

        try:
            return int(float(str(value or "").strip()))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_optional_float(value: str) -> float | None:
        """Parse an optional NVIDIA value without turning ``N/A`` into zero."""

        text = str(value or "").strip()
        normalized = text.strip("[]").strip().lower()
        if normalized in {"", "n/a", "na", "unknown", "not supported"}:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_optional_text(value: str) -> str:
        """Normalize an optional textual NVIDIA field."""

        text = str(value or "").strip()
        if text.strip("[]").strip().lower() in {
            "", "n/a", "na", "unknown", "not supported"
        }:
            return ""
        return text

    @staticmethod
    def _read_process_value(process: Any, method_name: str) -> Any:
        """Read one psutil value without losing other process metadata."""

        try:
            return getattr(process, method_name)()
        except Exception:
            return None

    @classmethod
    def _bounded_process_text(
        cls,
        value: Any,
        *,
        limit: int | None = None,
    ) -> str:
        if not isinstance(value, str):
            return ""
        text = value.strip()
        max_chars = (
            cls._PROCESS_TEXT_LIMIT
            if limit is None
            else max(0, int(limit))
        )
        return text[:max_chars]

    @staticmethod
    def _optional_process_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @classmethod
    def _format_process_command(cls, value: Any) -> tuple[str, bool]:
        if isinstance(value, (list, tuple)):
            argv = [str(item) for item in value]
            try:
                text = (
                    subprocess.list2cmdline(argv)
                    if os.name == "nt"
                    else shlex.join(argv)
                )
            except (TypeError, ValueError):
                text = " ".join(argv)
        elif isinstance(value, str):
            text = value
        else:
            text = ""
        text = text.strip()
        truncated = len(text) > cls._PROCESS_COMMAND_LIMIT
        return text[: cls._PROCESS_COMMAND_LIMIT], truncated

    @classmethod
    def _process_details(cls, pid: int) -> Dict[str, Any]:
        """Return bounded, best-effort OS metadata for one GPU process."""

        details: Dict[str, Any] = {
            "user": "unknown",
            "process_name": "",
            "status": "",
            "executable": "",
            "command_line": "",
            "command_line_truncated": False,
            "working_directory": "",
            "created_at": None,
            "host_memory_mb": None,
            "host_memory_percent": None,
            "thread_count": None,
            "parent_pid": None,
        }
        if pid < 0:
            return details
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                username = cls._bounded_process_text(
                    cls._read_process_value(process, "username")
                )
                details["user"] = username or "unknown"
                details["process_name"] = cls._bounded_process_text(
                    cls._read_process_value(process, "name")
                )
                details["status"] = cls._bounded_process_text(
                    cls._read_process_value(process, "status")
                )
                details["executable"] = cls._bounded_process_text(
                    cls._read_process_value(process, "exe")
                )
                details["working_directory"] = cls._bounded_process_text(
                    cls._read_process_value(process, "cwd")
                )
                command, truncated = cls._format_process_command(
                    cls._read_process_value(process, "cmdline")
                )
                details["command_line"] = command
                details["command_line_truncated"] = truncated
                created_at = cls._optional_process_number(
                    cls._read_process_value(process, "create_time")
                )
                details["created_at"] = (
                    created_at if created_at and created_at > 0 else None
                )
                memory_info = cls._read_process_value(process, "memory_info")
                rss = cls._optional_process_number(
                    getattr(memory_info, "rss", None)
                )
                details["host_memory_mb"] = (
                    max(0.0, rss / (1024 * 1024))
                    if rss is not None
                    else None
                )
                memory_percent = cls._optional_process_number(
                    cls._read_process_value(process, "memory_percent")
                )
                details["host_memory_percent"] = (
                    max(0.0, memory_percent)
                    if memory_percent is not None
                    else None
                )
                thread_count = cls._optional_process_number(
                    cls._read_process_value(process, "num_threads")
                )
                details["thread_count"] = (
                    max(0, int(thread_count))
                    if thread_count is not None
                    else None
                )
                parent_pid = cls._optional_process_number(
                    cls._read_process_value(process, "ppid")
                )
                details["parent_pid"] = (
                    max(0, int(parent_pid))
                    if parent_pid is not None
                    else None
                )
        except Exception:
            pass
        return details

    @staticmethod
    def _parse_csv_rows(output: str) -> List[List[str]]:
        """Parse NVIDIA CSV without assuming names contain no commas."""

        return [
            [item.strip() for item in row]
            for row in csv.reader(output.splitlines(), skipinitialspace=True)
            if any(str(item).strip() for item in row)
        ]

    def _query_nvidia_smi(self, fields: str, *, scope: str) -> str:
        """Run one ``nvidia-smi`` CSV query and return stripped text."""

        query_flag = (
            "--query-gpu" if scope == "gpu" else "--query-compute-apps"
        )
        return subprocess.check_output(
            [
                "nvidia-smi",
                f"{query_flag}={fields}",
                "--format=csv,noheader,nounits",
            ],
            timeout=self._GPU_QUERY_TIMEOUT_SEC,
            **hidden_subprocess_kwargs(),
        ).decode("utf-8", errors="replace").strip()

    def _query_gpu_snapshot(self) -> str:
        """Query detailed GPU rows, falling back for older NVIDIA drivers."""

        fields = (
            self._GPU_DETAIL_FIELDS
            if self._gpu_detail_query_supported
            else self._GPU_SUMMARY_FIELDS
        )
        try:
            return self._query_nvidia_smi(fields, scope="gpu")
        except subprocess.CalledProcessError:
            if not self._gpu_detail_query_supported:
                raise
            result = self._query_nvidia_smi(
                self._GPU_SUMMARY_FIELDS,
                scope="gpu",
            )
            self._gpu_detail_query_supported = False
            return result

    def _get_gpu_processes(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return GPU processes keyed by GPU UUID."""

        try:
            out = self._query_nvidia_smi(
                "gpu_uuid,pid,process_name,used_memory",
                scope="compute",
            )
        except Exception:
            return {}

        processes_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
        details_by_pid: Dict[int, Dict[str, Any]] = {}
        for parts in self._parse_csv_rows(out):
            if len(parts) < 4:
                continue

            gpu_uuid, pid_raw, process_name, memory_raw = parts[:4]
            if not gpu_uuid:
                continue

            pid = self._coerce_int(pid_raw, default=-1)
            if pid not in details_by_pid:
                details_by_pid[pid] = self._process_details(pid)
            process_info = {
                "pid": pid,
                **details_by_pid[pid],
                "name": process_name or "unknown",
                "memory_mb": self._coerce_optional_float(memory_raw),
            }
            processes_by_uuid.setdefault(gpu_uuid, []).append(process_info)

        for process_list in processes_by_uuid.values():
            process_list.sort(
                key=lambda item: (
                    float(item.get("memory_mb") or -1.0),
                    int(item.get("pid", -1)),
                ),
                reverse=True,
            )

        return processes_by_uuid

    def _attach_gpu_processes(
        self,
        gpus: List[Dict[str, Any]],
        *,
        now: float,
        include_processes: bool,
    ) -> List[Dict[str, Any]]:
        """Attach optional, separately cached process data to GPU rows."""

        processes_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
        if include_processes:
            cache_expired = (
                now - self._gpu_process_cache_at >= self._gpu_ttl_sec
            )
            if not self._gpu_process_cache_valid or cache_expired:
                self._gpu_process_cache = self._get_gpu_processes()
                self._gpu_process_cache_at = now
                self._gpu_process_cache_valid = True
            processes_by_uuid = self._gpu_process_cache

        result = [
            {
                **gpu,
                "processes": list(
                    processes_by_uuid.get(str(gpu.get("uuid", "")), [])
                ),
            }
            for gpu in gpus
        ]
        if include_processes:
            # Preserve the legacy observable cache shape for callers that ask
            # for details, while include_processes=False still strips them.
            self._gpu_cache = result
        return result

    @staticmethod
    def _copy_cached_gpu_rows(
        gpus: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return well-formed cached rows without another GPU query."""

        return [
            {
                **gpu,
                "processes": list(gpu.get("processes") or []),
            }
            for gpu in gpus
        ]

    def _get_gpu_metrics(
        self,
        *,
        include_processes: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return cached GPU metrics and load process details on demand."""

        now = time.monotonic()
        cache_fresh = now - self._gpu_cache_at < self._gpu_ttl_sec
        if self._gpu_cache_valid and cache_fresh:
            return self._attach_gpu_processes(
                self._gpu_cache,
                now=now,
                include_processes=include_processes,
            )

        if not self._gpu_available:
            if now - self._gpu_disabled_at < self._gpu_retry_sec:
                if include_processes:
                    return self._copy_cached_gpu_rows(self._gpu_cache)
                return self._attach_gpu_processes(
                    self._gpu_cache,
                    now=now,
                    include_processes=include_processes,
                )
            self._gpu_available = True
            self._gpu_fail_count = 0

        try:
            out = self._query_gpu_snapshot()
            gpus: List[Dict[str, Any]] = []
            for parts in self._parse_csv_rows(out):
                if len(parts) < 6:
                    continue

                index = self._coerce_int(parts[0], default=0)
                name = parts[1] or f"GPU {index}"
                uuid = parts[2]
                gpu_info = {
                    "id": index,
                    "index": index,
                    "name": name,
                    "uuid": uuid,
                    "util": self._coerce_float(parts[3], default=0.0),
                    "mem_used": self._coerce_float(parts[4], default=0.0),
                    "mem_total": self._coerce_float(parts[5], default=0.0),
                    "mem_util": self._coerce_optional_float(
                        parts[6] if len(parts) > 6 else ""
                    ),
                    "mem_free": self._coerce_optional_float(
                        parts[7] if len(parts) > 7 else ""
                    ),
                    "temperature_c": self._coerce_optional_float(
                        parts[8] if len(parts) > 8 else ""
                    ),
                    "fan_speed_pct": self._coerce_optional_float(
                        parts[9] if len(parts) > 9 else ""
                    ),
                    "power_draw_w": self._coerce_optional_float(
                        parts[10] if len(parts) > 10 else ""
                    ),
                    "power_limit_w": self._coerce_optional_float(
                        parts[11] if len(parts) > 11 else ""
                    ),
                    "performance_state": self._coerce_optional_text(
                        parts[12] if len(parts) > 12 else ""
                    ),
                    "compute_mode": self._coerce_optional_text(
                        parts[13] if len(parts) > 13 else ""
                    ),
                    "graphics_clock_mhz": self._coerce_optional_float(
                        parts[14] if len(parts) > 14 else ""
                    ),
                    "memory_clock_mhz": self._coerce_optional_float(
                        parts[15] if len(parts) > 15 else ""
                    ),
                    "pci_bus_id": self._coerce_optional_text(
                        parts[16] if len(parts) > 16 else ""
                    ),
                    "driver_version": self._coerce_optional_text(
                        parts[17] if len(parts) > 17 else ""
                    ),
                }
                gpus.append(gpu_info)

            self._gpu_cache = gpus
            self._gpu_cache_at = now
            self._gpu_cache_valid = True
            self._gpu_fail_count = 0
            self._gpu_disabled_at = 0.0
            return self._attach_gpu_processes(
                gpus,
                now=now,
                include_processes=include_processes,
            )
        except Exception:
            self._gpu_fail_count += 1
            if self._gpu_fail_count >= self._gpu_max_fails:
                self._gpu_available = False
                self._gpu_disabled_at = now
            if include_processes:
                return self._copy_cached_gpu_rows(self._gpu_cache)
            return self._attach_gpu_processes(
                self._gpu_cache,
                now=now,
                include_processes=include_processes,
            )
