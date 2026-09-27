"""Run the existing installed lifecycle with explicit 100-point SDK batches."""
import importlib.util
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lifecycle", root / "scripts/check_installed_lifecycle.py")
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
loop = "for step in range(cfg.steps):"
call = "    pyruns.track(step=step, score=run * 10000 + step)"
assert lifecycle.WORKLOAD.count(loop) == lifecycle.WORKLOAD.count(call) == 1
lifecycle.WORKLOAD = lifecycle.WORKLOAD.replace(loop, "pending_points = []\n" + loop).replace(call, '''    pending_points.append({"step": step, "score": run * 10000 + step})
    if len(pending_points) == 100 or step == cfg.steps - 1:
        pyruns.track_many(pending_points)
        pending_points.clear()''')
raise SystemExit(lifecycle.main())
