"""System metrics collector for CPU, RAM, and NVIDIA GPUs."""

from __future__ import annotations

import csv
import subprocess
import time
from typing import Any, Dict, List

import psutil

from pyruns.utils.process_utils import hidden_subprocess_kwargs


class SystemMonitor:
    """Collect CPU, RAM, and optional GPU utilization metrics."""

    _GPU_QUERY_TIMEOUT_SEC = 1.0

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
    def _process_username(pid: int) -> str:
        """Best-effort owner lookup for an OS process."""

        if pid < 0:
            return "unknown"
        try:
            return psutil.Process(pid).username() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _parse_csv_rows(output: str) -> List[List[str]]:
        """Parse ``nvidia-smi`` CSV output without assuming names contain no commas."""

        return [
            [item.strip() for item in row]
            for row in csv.reader(output.splitlines(), skipinitialspace=True)
            if any(str(item).strip() for item in row)
        ]

    def _query_nvidia_smi(self, fields: str, *, scope: str) -> str:
        """Run one ``nvidia-smi`` CSV query and return stripped text."""

        query_flag = "--query-gpu" if scope == "gpu" else "--query-compute-apps"
        return subprocess.check_output(
            [
                "nvidia-smi",
                f"{query_flag}={fields}",
                "--format=csv,noheader,nounits",
            ],
            timeout=self._GPU_QUERY_TIMEOUT_SEC,
            **hidden_subprocess_kwargs(),
        ).decode("utf-8", errors="replace").strip()

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
        for parts in self._parse_csv_rows(out):
            if len(parts) < 4:
                continue

            gpu_uuid, pid_raw, process_name, memory_raw = parts[:4]
            if not gpu_uuid:
                continue

            pid = self._coerce_int(pid_raw, default=-1)
            process_info = {
                "pid": pid,
                "user": self._process_username(pid),
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
        """Return copies of GPU rows with optional, separately cached process data."""

        processes_by_uuid: Dict[str, List[Dict[str, Any]]] = {}
        if include_processes:
            if not self._gpu_process_cache_valid or now - self._gpu_process_cache_at >= self._gpu_ttl_sec:
                self._gpu_process_cache = self._get_gpu_processes()
                self._gpu_process_cache_at = now
                self._gpu_process_cache_valid = True
            processes_by_uuid = self._gpu_process_cache

        result = [
            {
                **gpu,
                "processes": list(processes_by_uuid.get(str(gpu.get("uuid", "")), [])),
            }
            for gpu in gpus
        ]
        if include_processes:
            # Preserve the legacy observable cache shape for callers that ask
            # for details, while include_processes=False still strips them.
            self._gpu_cache = result
        return result

    def _get_gpu_metrics(self, *, include_processes: bool = True) -> List[Dict[str, Any]]:
        """Return cached GPU metrics, loading expensive process details only on demand."""

        now = time.monotonic()
        if self._gpu_cache_valid and now - self._gpu_cache_at < self._gpu_ttl_sec:
            return self._attach_gpu_processes(
                self._gpu_cache,
                now=now,
                include_processes=include_processes,
            )

        if not self._gpu_available:
            if now - self._gpu_disabled_at < self._gpu_retry_sec:
                if include_processes:
                    return list(self._gpu_cache)
                return self._attach_gpu_processes(
                    self._gpu_cache,
                    now=now,
                    include_processes=include_processes,
                )
            self._gpu_available = True
            self._gpu_fail_count = 0

        try:
            out = self._query_nvidia_smi(
                "index,name,uuid,utilization.gpu,memory.used,memory.total",
                scope="gpu",
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
                return list(self._gpu_cache)
            return self._attach_gpu_processes(
                self._gpu_cache,
                now=now,
                include_processes=include_processes,
            )
