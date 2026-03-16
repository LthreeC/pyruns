"""
System metrics collector – CPU, RAM, GPU via psutil + nvidia-smi.
"""
import time

import psutil
import subprocess
from typing import List, Dict, Any


class SystemMonitor:
    """Collect CPU, RAM, and (optional) GPU utilisation metrics."""

    def __init__(self):
        self._gpu_cache: List[Dict[str, Any]] = []
        self._gpu_cache_at: float = 0.0
        self._gpu_ttl_sec: float = 1.5
        self._gpu_available: bool = True  # False after first nvidia-smi failure
        self._gpu_fail_count: int = 0
        self._GPU_MAX_FAILS: int = 3  # Stop trying after N consecutive failures

    def sample(self) -> Dict[str, Any]:
        """Collect system metrics (CPU, RAM, GPU)."""
        metrics = {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics()
        }
        return metrics

    def _get_gpu_metrics(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if self._gpu_cache and now - self._gpu_cache_at < self._gpu_ttl_sec:
            return self._gpu_cache

        # Skip nvidia-smi entirely after repeated failures (no GPU on this machine)
        if not self._gpu_available:
            return self._gpu_cache

        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                timeout=0.5,
            ).decode("utf-8").strip()

            gpus = []
            for line in out.splitlines():
                if not line.strip():
                    continue
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
                        "index": int(parts[0]),
                        "util": float(parts[1]),
                        "mem_used": float(parts[2]),
                        "mem_total": float(parts[3]),
                    })
            self._gpu_cache = gpus
            self._gpu_cache_at = now
            self._gpu_fail_count = 0  # Reset on success
            return gpus
        except Exception:
            self._gpu_fail_count += 1
            if self._gpu_fail_count >= self._GPU_MAX_FAILS:
                self._gpu_available = False
            return self._gpu_cache

