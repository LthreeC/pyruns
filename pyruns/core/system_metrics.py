"""System metrics collector for CPU, RAM, and NVIDIA GPUs."""

from __future__ import annotations

import csv
import subprocess
import time
from typing import Any, Dict, List

import psutil


class SystemMonitor:
    """Collect CPU, RAM, and optional GPU utilization metrics."""

    _GPU_QUERY_TIMEOUT_SEC = 1.0

    def __init__(self) -> None:
        self._gpu_cache: List[Dict[str, Any]] = []
        self._gpu_cache_at: float = 0.0
        self._gpu_cache_valid: bool = False
        self._gpu_ttl_sec: float = 1.5
        self._gpu_available: bool = True
        self._gpu_fail_count: int = 0
        self._gpu_max_fails: int = 3
        self._gpu_disabled_at: float = 0.0
        self._gpu_retry_sec: float = 30.0

    def sample(self) -> Dict[str, Any]:
        """Collect system metrics."""

        return {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics(),
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
                "memory_mb": self._coerce_float(memory_raw, default=0.0),
            }
            processes_by_uuid.setdefault(gpu_uuid, []).append(process_info)

        for process_list in processes_by_uuid.values():
            process_list.sort(
                key=lambda item: (
                    float(item.get("memory_mb", 0.0)),
                    int(item.get("pid", -1)),
                ),
                reverse=True,
            )

        return processes_by_uuid

    def _get_gpu_metrics(self) -> List[Dict[str, Any]]:
        """Return cached GPU metrics, refreshing them with ``nvidia-smi`` when needed."""

        now = time.monotonic()
        if self._gpu_cache_valid and now - self._gpu_cache_at < self._gpu_ttl_sec:
            return self._gpu_cache

        if not self._gpu_available:
            if now - self._gpu_disabled_at < self._gpu_retry_sec:
                return self._gpu_cache
            self._gpu_available = True
            self._gpu_fail_count = 0

        try:
            out = self._query_nvidia_smi(
                "index,name,uuid,utilization.gpu,memory.used,memory.total",
                scope="gpu",
            )
            processes_by_uuid = self._get_gpu_processes()

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
                    "processes": processes_by_uuid.get(uuid, []),
                }
                gpus.append(gpu_info)

            self._gpu_cache = gpus
            self._gpu_cache_at = now
            self._gpu_cache_valid = True
            self._gpu_fail_count = 0
            self._gpu_disabled_at = 0.0
            return gpus
        except Exception:
            self._gpu_fail_count += 1
            if self._gpu_fail_count >= self._gpu_max_fails:
                self._gpu_available = False
                self._gpu_disabled_at = now
            return self._gpu_cache
