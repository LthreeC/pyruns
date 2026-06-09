"""GPU-aware admission control for local task scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol

from pyruns.core.system_metrics import SystemMonitor


CUDA_VISIBLE_DEVICES = "CUDA_VISIBLE_DEVICES"
PYRUNS_ASSIGNED_GPUS = "PYRUNS_ASSIGNED_GPUS"


@dataclass(frozen=True)
class GpuDevice:
    """One GPU snapshot normalized for scheduling decisions."""

    index: int
    name: str
    uuid: str
    memory_used_mb: float
    memory_total_mb: float
    compute_util_pct: float

    @classmethod
    def from_metric(cls, metric: Dict[str, Any]) -> "GpuDevice":
        return cls(
            index=int(metric.get("index", metric.get("id", 0)) or 0),
            name=str(metric.get("name", "") or "GPU"),
            uuid=str(metric.get("uuid", "") or ""),
            memory_used_mb=float(metric.get("mem_used", 0.0) or 0.0),
            memory_total_mb=float(metric.get("mem_total", 0.0) or 0.0),
            compute_util_pct=float(metric.get("util", 0.0) or 0.0),
        )

    @property
    def memory_used_pct(self) -> float:
        if self.memory_total_mb <= 0:
            return 100.0
        return (self.memory_used_mb / self.memory_total_mb) * 100.0

    @property
    def free_memory_mb(self) -> float:
        return max(0.0, self.memory_total_mb - self.memory_used_mb)

    @property
    def free_memory_gb(self) -> float:
        return self.free_memory_mb / 1024.0


class GpuProvider(Protocol):
    def sample(self) -> List[GpuDevice]:
        """Return a normalized GPU snapshot."""


class SystemGpuProvider:
    """GPU provider backed by the existing ``SystemMonitor``."""

    def __init__(self, monitor: Optional[SystemMonitor] = None) -> None:
        self.monitor = monitor or SystemMonitor()

    def sample(self) -> List[GpuDevice]:
        metrics = self.monitor.sample().get("gpus", [])
        if not isinstance(metrics, list):
            return []
        return [GpuDevice.from_metric(metric) for metric in metrics if isinstance(metric, dict)]


@dataclass
class GpuSchedulerConfig:
    enabled: bool = False
    task_mode: str = "single"
    gpus_per_task: int = 1
    device_ids: List[int] = field(default_factory=list)
    memory_used_pct: float = 40.0
    min_free_memory_gb: float = 40.0
    compute_used_pct: float = 30.0
    stable_seconds: float = 15.0
    max_wait_seconds: float = 172800.0
    max_tasks_per_gpu: int = 1
    respect_cuda_visible_devices: bool = True

    def __post_init__(self) -> None:
        self.stable_seconds = max(1.0, _coerce_float(self.stable_seconds, 15.0))

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "GpuSchedulerConfig":
        task_mode = str(settings.get("gpu_scheduler_task_mode", "single") or "single").strip().lower()
        task_mode = "multi" if task_mode == "multi" else "single"
        return cls(
            enabled=_coerce_bool(settings.get("gpu_scheduler_enabled", False), False),
            task_mode=task_mode,
            gpus_per_task=max(1, _coerce_int(settings.get("gpu_scheduler_gpus_per_task"), 1)),
            device_ids=_coerce_device_ids(settings.get("gpu_scheduler_device_ids", [])),
            memory_used_pct=_coerce_pct(settings.get("gpu_scheduler_memory_used_pct"), 40.0),
            min_free_memory_gb=max(0.0, _coerce_float(settings.get("gpu_scheduler_min_free_memory_gb"), 40.0)),
            compute_used_pct=_coerce_pct(settings.get("gpu_scheduler_compute_used_pct"), 30.0),
            stable_seconds=max(1.0, _coerce_float(settings.get("gpu_scheduler_stable_seconds"), 15.0)),
            max_wait_seconds=max(1.0, _coerce_float(settings.get("gpu_scheduler_max_wait_seconds"), 172800.0)),
            max_tasks_per_gpu=max(1, _coerce_int(settings.get("gpu_scheduler_max_tasks_per_gpu"), 1)),
            respect_cuda_visible_devices=_coerce_bool(settings.get("gpu_scheduler_respect_cuda_visible_devices", True), True),
        )

    @property
    def required_gpu_count(self) -> int:
        if self.task_mode == "multi":
            return max(1, int(self.gpus_per_task or 1))
        return 1


@dataclass(frozen=True)
class GpuAssignment:
    task_name: str
    run_index: int
    gpu_ids: List[int]
    cuda_visible_devices: str
    env: Dict[str, str]
    waited_seconds: float


@dataclass(frozen=True)
class GpuDecision:
    assignment: Optional[GpuAssignment]
    reason: str
    snapshot: List[GpuDevice]


class GpuResourceScheduler:
    """Small in-memory GPU reservation manager.

    Admission checks resample on each scheduling pass. ``stable_seconds``
    controls how long a GPU must remain eligible before it can be reserved.
    """

    def __init__(
        self,
        provider: Optional[GpuProvider] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        import time

        self.provider = provider or SystemGpuProvider()
        self.clock = clock or time.monotonic
        self._snapshot: List[GpuDevice] = []
        self._snapshot_at: float = -10**12
        self._eligible_since: Dict[int, float] = {}
        self._reservations: Dict[str, List[int]] = {}
        self._lock = threading.RLock()

    def snapshot(self, config: GpuSchedulerConfig, *, now: float | None = None) -> List[GpuDevice]:
        with self._lock:
            sample_at = self.clock() if now is None else now
            self._snapshot = self.provider.sample()
            self._snapshot_at = sample_at
            self._refresh_eligible_since(config, sample_at)
            return self._snapshot

    def release(self, task_name: str) -> None:
        with self._lock:
            self._reservations.pop(str(task_name), None)

    def try_reserve(
        self,
        task_name: str,
        run_index: int,
        config: GpuSchedulerConfig,
        *,
        task_env: Dict[str, str] | None,
        queued_since: float | None = None,
    ) -> GpuDecision:
        with self._lock:
            now = self.clock()
            env = {str(k): str(v) for k, v in (task_env or {}).items() if str(k)}
            snapshot = self.snapshot(config, now=now)
            waited = max(0.0, now - queued_since) if queued_since is not None else 0.0

            existing_cuda = str(env.get(CUDA_VISIBLE_DEVICES, "") or "").strip()
            if existing_cuda and not any(part.strip() for part in existing_cuda.split(",")):
                existing_cuda = ""
            requested_ids = _parse_cuda_visible_devices(existing_cuda) if existing_cuda else None
            if existing_cuda and config.respect_cuda_visible_devices and requested_ids is None:
                assignment = GpuAssignment(
                    task_name=task_name,
                    run_index=run_index,
                    gpu_ids=[],
                    cuda_visible_devices=existing_cuda,
                    env={PYRUNS_ASSIGNED_GPUS: existing_cuda},
                    waited_seconds=waited,
                )
                return GpuDecision(assignment=assignment, reason="using existing CUDA_VISIBLE_DEVICES", snapshot=snapshot)

            if requested_ids and config.respect_cuda_visible_devices:
                required_ids = requested_ids
                visible = existing_cuda
                inject_cuda = False
            else:
                required_ids = []
                visible = ""
                inject_cuda = True

            if required_ids:
                selected, reason = self._validate_fixed_ids(required_ids, config, now)
            else:
                selected, reason = self._select_available_group(config, now)

            if not selected:
                return GpuDecision(assignment=None, reason=reason, snapshot=snapshot)

            self._reservations[str(task_name)] = selected
            if not visible:
                visible = ",".join(str(gpu_id) for gpu_id in selected)
            injected_env = {PYRUNS_ASSIGNED_GPUS: ",".join(str(gpu_id) for gpu_id in selected)}
            if inject_cuda:
                injected_env[CUDA_VISIBLE_DEVICES] = visible
            assignment = GpuAssignment(
                task_name=task_name,
                run_index=run_index,
                gpu_ids=selected,
                cuda_visible_devices=visible,
                env=injected_env,
                waited_seconds=waited,
            )
            return GpuDecision(assignment=assignment, reason="assigned", snapshot=snapshot)

    def _refresh_eligible_since(self, config: GpuSchedulerConfig, now: float) -> None:
        visible_ids = {gpu.index for gpu in self._candidate_devices(config)}
        for gpu in self._candidate_devices(config):
            if self._meets_static_limits(gpu, config):
                self._eligible_since.setdefault(gpu.index, now)
            else:
                self._eligible_since.pop(gpu.index, None)
        for gpu_id in list(self._eligible_since):
            if gpu_id not in visible_ids:
                self._eligible_since.pop(gpu_id, None)

    def _candidate_devices(self, config: GpuSchedulerConfig) -> List[GpuDevice]:
        allowed = set(config.device_ids or [])
        devices = [gpu for gpu in self._snapshot if not allowed or gpu.index in allowed]
        devices.sort(key=lambda gpu: (-gpu.free_memory_mb, gpu.compute_util_pct, gpu.index))
        return devices

    def _select_available_group(self, config: GpuSchedulerConfig, now: float) -> tuple[List[int], str]:
        required_count = config.required_gpu_count
        selected: List[int] = []
        blocked: List[str] = []
        for gpu in self._candidate_devices(config):
            reason = self._blocked_reason(gpu, config, now)
            if reason:
                blocked.append(reason)
                continue
            selected.append(gpu.index)
            if len(selected) >= required_count:
                return selected, "assigned"

        if not self._snapshot:
            return [], "no NVIDIA GPU metrics available"
        if selected:
            return [], f"need {required_count} eligible GPUs, only {len(selected)} available"
        return [], "; ".join(blocked[:3]) or f"waiting for GPUs to be stable for {config.stable_seconds:g}s"

    def _validate_fixed_ids(
        self,
        requested_ids: List[int],
        config: GpuSchedulerConfig,
        now: float,
    ) -> tuple[List[int], str]:
        required_count = config.required_gpu_count
        if len(requested_ids) < required_count:
            return [], f"need {required_count} requested GPUs, only {len(requested_ids)} provided"

        allowed = set(config.device_ids or [])
        devices_by_id = {gpu.index: gpu for gpu in self._snapshot}
        for gpu_id in requested_ids:
            if allowed and gpu_id not in allowed:
                return [], f"GPU {gpu_id} outside configured GPU pool"
            gpu = devices_by_id.get(gpu_id)
            if gpu is None:
                return [], f"GPU {gpu_id} unavailable"
            reason = self._blocked_reason(gpu, config, now)
            if reason:
                return [], reason
        return list(requested_ids), "assigned"

    def _blocked_reason(self, gpu: GpuDevice, config: GpuSchedulerConfig, now: float) -> str:
        reserved = self._reserved_count(gpu.index)
        if reserved >= config.max_tasks_per_gpu:
            return f"GPU {gpu.index} reserved ({reserved}/{config.max_tasks_per_gpu})"
        if gpu.memory_used_pct > config.memory_used_pct:
            return f"GPU {gpu.index} memory {gpu.memory_used_pct:.0f}% > {config.memory_used_pct:g}%"
        if gpu.free_memory_gb < config.min_free_memory_gb:
            return f"GPU {gpu.index} free {gpu.free_memory_gb:.1f} GiB < {config.min_free_memory_gb:g} GiB"
        if gpu.compute_util_pct > config.compute_used_pct:
            return f"GPU {gpu.index} compute {gpu.compute_util_pct:.0f}% > {config.compute_used_pct:g}%"
        since = self._eligible_since.get(gpu.index)
        if since is None or now - since < config.stable_seconds:
            remaining = config.stable_seconds if since is None else config.stable_seconds - (now - since)
            return f"GPU {gpu.index} waiting stable for {max(0.0, remaining):.0f}s"
        return ""

    def _meets_static_limits(self, gpu: GpuDevice, config: GpuSchedulerConfig) -> bool:
        return (
            gpu.memory_used_pct <= config.memory_used_pct
            and gpu.free_memory_gb >= config.min_free_memory_gb
            and gpu.compute_util_pct <= config.compute_used_pct
        )

    def _reserved_count(self, gpu_id: int) -> int:
        return sum(1 for gpu_ids in self._reservations.values() if gpu_id in gpu_ids)


def format_gpu_queue_block(title: str, lines: List[str]) -> str:
    """Format a Pyruns-owned GPU queue event for ``queue.log``."""

    clean_title = str(title or "GPU").strip().upper()
    body = "\n".join(f"[PYRUNS] {line}" for line in lines)
    return (
        f"[PYRUNS] {'=' * 17} {clean_title} {'=' * 17}\n"
        f"{body}\n"
        f"[PYRUNS] {'=' * (36 + len(clean_title))}\n"
    )


def format_gpu_rule(config: GpuSchedulerConfig) -> str:
    return (
        f"Rule: memory used <= {config.memory_used_pct:g}%; "
        f"compute <= {config.compute_used_pct:g}%; "
        f"free memory >= {config.min_free_memory_gb:g} GiB; "
        f"stable for {config.stable_seconds:g}s"
    )


def _parse_cuda_visible_devices(value: str) -> Optional[List[int]]:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not parts:
        return None
    parsed: List[int] = []
    seen: set[int] = set()
    for part in parts:
        if not part.isdigit():
            return None
        value = int(part)
        if value not in seen:
            parsed.append(value)
            seen.add(value)
    return parsed


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_pct(value: Any, default: float) -> float:
    return min(100.0, max(0.0, _coerce_float(value, default)))


def _coerce_device_ids(value: Any) -> List[int]:
    if value in (None, "", "auto"):
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        return []
    ids: List[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        text = str(raw).strip()
        if text.isdigit():
            value = int(text)
            if value not in seen:
                ids.append(value)
                seen.add(value)
    return ids
