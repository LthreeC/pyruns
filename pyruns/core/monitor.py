import psutil
import subprocess
import threading
from typing import List, Dict, Any, Optional

class SystemMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._gpu_cache = []

    def sample(self) -> Dict[str, Any]:
        """Collect system metrics (CPU, RAM, GPU)."""
        metrics = {
            "cpu_percent": psutil.cpu_percent(),
            "mem_percent": psutil.virtual_memory().percent,
            "gpus": self._get_gpu_metrics()
        }
        return metrics

    def _get_gpu_metrics(self) -> List[Dict[str, Any]]:
        # Naive implementation invoking nvidia-smi
        # In production, use pynvml or cache this heavily
        try:
            # Format: index, utilization.gpu, memory.used, memory.total
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                timeout=0.5
            ).decode("utf-8").strip()
            
            gpus = []
            for line in out.splitlines():
                if not line.strip(): continue
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
                        "index": int(parts[0]),
                        "util": float(parts[1]),
                        "mem_used": float(parts[2]),
                        "mem_total": float(parts[3])
                    })
            self._gpu_cache = gpus
            return gpus
        except Exception:
            # Fallback to cache or empty
            return self._gpu_cache

class GpuAllocator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = self._detect_gpus()

    def _detect_gpus(self) -> List[int]:
        try:
            out = subprocess.check_output(["nvidia-smi", "-L"], timeout=1).decode("utf-8").strip()
            if not out:
                return []
            count = len([line for line in out.splitlines() if line.strip()])
            return list(range(count))
        except Exception:
            return []

    def refresh(self) -> None:
        with self._lock:
            self._available = self._detect_gpus()

    def reserve(self) -> Optional[int]:
        with self._lock:
            if not self._available:
                return None
            return self._available.pop(0)

    def release(self, gpu_id: Optional[int]) -> None:
        if gpu_id is None:
            return
        with self._lock:
            if gpu_id not in self._available:
                self._available.append(gpu_id)
                self._available.sort()
