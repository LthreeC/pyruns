"""Run the existing isolated-wheel lifecycle with per-point SDK stage timing."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installed_lifecycle", root / "scripts/check_installed_lifecycle.py")
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
marker = "for step in range(cfg.steps):"
assert lifecycle.WORKLOAD.count(marker) == 1
probe = (root / ".github/track_stage_probe.py").read_text(encoding="utf-8")
lifecycle.WORKLOAD = lifecycle.WORKLOAD.replace(marker, probe + "\n" + marker)
original_verify = lifecycle._verify
output = Path(sys.argv[sys.argv.index("--output") + 1]).resolve().parent


def verify(project, report, identities):
    profiles = []
    try:
        original_verify(project, report, identities)
    finally:
        for source in sorted(project.glob("track-profile-*.json")):
            destination = output / source.name
            shutil.copyfile(source, destination)
            profiles.append(json.loads(source.read_text(encoding="utf-8")))
    assert len(profiles) == 2
    for profile, expected_run in zip(profiles, (1, 2), strict=True):
        assert profile["run_index"] == expected_run
        assert profile["observed_steps"] == profile["expected_steps"] == 600
        assert [row["step"] for row in profile["points"]] == list(range(600))
        for point in profile["points"]:
            stages = point["stages"]
            assert stages["sdk.track"]["calls"] == 1
            duration = stages["sdk.track"]["inclusive_seconds"]
            assert abs(sum(stage["exclusive_seconds"] for stage in stages.values()) - duration) < 1e-6
            assert all(stage["exclusive_seconds"] >= -1e-9 for stage in stages.values())
        assert profile["totals"]["store.append_point"]["calls"] > 0
        assert profile["totals"]["sqlite.commit"]["calls"] > 0
        assert profile["totals"]["sqlite.open"]["calls"] == profile["totals"]["sqlite.close"]["calls"]
    assert profiles[0]["source_sha256"] == profiles[1]["source_sha256"]
    report["profile_validation"] = {"runs": 2, "steps_each": 600, "exclusive_times_partition_track_duration": True}


lifecycle._verify = verify
raise SystemExit(lifecycle.main())
