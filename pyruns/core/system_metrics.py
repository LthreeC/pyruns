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
    _GPU_PROCESS_QUERY_TIMEOUT_SEC = 5.0
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
    _GPU_SUMMARY_KEYS = (
        "id",
        "index",
        "name",
        "uuid",
        "util",
        "mem_used",
        "mem_total",
    )

    def __init__(self, *, gpu_ttl_sec: float = 1.5) -> None:
        self._gpu_cache: List[Dict[str, Any]] = []
        self._gpu_cache_at: float = 0.0
        self._gpu_cache_valid: bool = False
        self._gpu_cache_has_details: bool = False
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

    def sample(
        self,
        *,
        include_processes: bool = True,
        detail: bool | None = None,
    ) -> Dict[str, Any]:
        """Collect system metrics."""

        include_detail = include_processes if detail is None else bool(detail)
        return {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics(
                include_processes=include_processes,
                detail=include_detail,
            ),
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
            "available": False,
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
            details["available"] = True
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

    @classmethod
    def get_process_details(cls, pid: int) -> Dict[str, Any]:
        """Return detailed metadata for one process on explicit request."""

        if isinstance(pid, bool) or pid <= 0:
            raise ValueError("Process ID must be a positive integer.")
        return {"pid": pid, **cls._process_details(pid)}

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
            timeout=(self._GPU_QUERY_TIMEOUT_SEC if scope == "gpu"
                     else self._GPU_PROCESS_QUERY_TIMEOUT_SEC),
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        ).decode("utf-8", errors="replace").strip()

    def _query_gpu_snapshot(self, *, detail: bool) -> tuple[str, bool]:
        """Query GPU rows and report whether detailed fields were returned."""

        if not detail or not self._gpu_detail_query_supported:
            return (
                self._query_nvidia_smi(
                    self._GPU_SUMMARY_FIELDS,
                    scope="gpu",
                ),
                False,
            )
        try:
            return (
                self._query_nvidia_smi(
                    self._GPU_DETAIL_FIELDS,
                    scope="gpu",
                ),
                True,
            )
        except subprocess.CalledProcessError:
            result = self._query_nvidia_smi(
                self._GPU_SUMMARY_FIELDS,
                scope="gpu",
            )
            self._gpu_detail_query_supported = False
            return result, False

    def _get_gpu_processes(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return GPU processes keyed by GPU UUID."""

        out = self._query_nvidia_smi(
            "gpu_uuid,pid,process_name,used_memory",
            scope="compute",
        )

        processes_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
        for parts in self._parse_csv_rows(out):
            if len(parts) < 4:
                raise ValueError("NVIDIA returned an unrecognized GPU process response.")

            gpu_uuid, pid_raw, process_name, memory_raw = parts[:4]
            if not gpu_uuid:
                continue

            pid = self._coerce_int(pid_raw, default=-1)
            process_info = {
                "pid": pid,
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
        refresh_processes: bool = True,
    ) -> List[Dict[str, Any]]:
        """Attach optional, separately cached process data to GPU rows."""

        processes_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
        process_error = ""
        if include_processes:
            cache_expired = (
                now - self._gpu_process_cache_at >= self._gpu_ttl_sec
            )
            if refresh_processes and (
                not self._gpu_process_cache_valid or cache_expired
            ):
                try:
                    processes = self._get_gpu_processes()
                except subprocess.TimeoutExpired:
                    process_error = (
                        "NVIDIA process query timed out after "
                        f"{self._GPU_PROCESS_QUERY_TIMEOUT_SEC:g}s. Retry to refresh."
                    )
                except Exception as exc:
                    detail = getattr(exc, "stderr", None) or getattr(exc, "output", None) or str(exc)
                    if isinstance(detail, bytes):
                        detail = detail.decode("utf-8", errors="replace")
                    process_error = f"Could not query NVIDIA processes: {str(detail).strip()[:512]}"
                else:
                    self._gpu_process_cache = processes
                    self._gpu_process_cache_at = now
                    self._gpu_process_cache_valid = True
            if self._gpu_process_cache_valid:
                processes_by_uuid = self._gpu_process_cache
            elif not refresh_processes:
                process_error = "GPU process data is unavailable because NVIDIA device discovery failed."

        result = [
            {
                **gpu,
                "processes": list(
                    processes_by_uuid.get(str(gpu.get("uuid", "")), [])
                ),
                **({"processes_error": process_error} if process_error else {}),
            }
            for gpu in gpus
        ]
        return result

    @staticmethod
    def _copy_cached_gpu_rows(
        gpus: List[Dict[str, Any]],
        *,
        detail: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return well-formed cached rows without another GPU query."""

        return [
            {
                **(
                    gpu
                    if detail
                    else {
                        key: gpu.get(key)
                        for key in SystemMonitor._GPU_SUMMARY_KEYS
                    }
                ),
                "processes": list(gpu.get("processes") or []),
            }
            for gpu in gpus
        ]

    def _get_gpu_metrics(
        self,
        *,
        include_processes: bool = True,
        detail: bool | None = None,
    ) -> List[Dict[str, Any]]:
        """Return cached GPU metrics and load process details on demand."""

        include_detail = include_processes if detail is None else bool(detail)
        now = time.monotonic()
        cache_fresh = now - self._gpu_cache_at < self._gpu_ttl_sec
        cache_satisfies_request = (
            not include_detail
            or self._gpu_cache_has_details
            or not self._gpu_detail_query_supported
        )
        if (
            self._gpu_cache_valid
            and cache_fresh
            and cache_satisfies_request
        ):
            cached_gpus = self._copy_cached_gpu_rows(
                self._gpu_cache,
                detail=include_detail,
            )
            return self._attach_gpu_processes(
                cached_gpus,
                now=now,
                include_processes=include_processes,
            )

        if not self._gpu_available:
            if now - self._gpu_disabled_at < self._gpu_retry_sec:
                return self._attach_gpu_processes(
                    self._copy_cached_gpu_rows(
                        self._gpu_cache,
                        detail=include_detail,
                    ),
                    now=now,
                    include_processes=include_processes,
                    refresh_processes=bool(self._gpu_cache),
                )
            self._gpu_available = True
            self._gpu_fail_count = 0

        try:
            out, has_details = self._query_gpu_snapshot(
                detail=include_detail,
            )
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
                }
                if has_details:
                    gpu_info.update(
                        {
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
                    )
                gpus.append(gpu_info)

            self._gpu_cache = gpus
            self._gpu_cache_at = now
            self._gpu_cache_valid = True
            self._gpu_cache_has_details = has_details
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
            return self._attach_gpu_processes(
                self._copy_cached_gpu_rows(
                    self._gpu_cache,
                    detail=include_detail,
                ),
                now=now,
                include_processes=include_processes,
                refresh_processes=bool(self._gpu_cache),
            )
