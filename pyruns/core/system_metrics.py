"""
System metrics collector – CPU, RAM, GPU via psutil + nvidia-smi.
"""
import psutil
import subprocess
import threading
from typing import List, Dict, Any


class SystemMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._gpu_cache: List[Dict[str, Any]] = []

    def sample(self) -> Dict[str, Any]:
        """Collect system metrics (CPU, RAM, GPU)."""
        metrics = {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics()
        }
        return metrics

    def _get_gpu_metrics(self) -> List[Dict[str, Any]]:
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
            return gpus
        except Exception:
            return self._gpu_cache

