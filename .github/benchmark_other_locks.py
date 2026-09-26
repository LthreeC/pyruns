"""Compare ordinary settings saves and exact-name reservations in ABBA order."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def measure(source):
    sys.path.insert(0, str(source))
    from pyruns.core.task_generator import TaskGenerator
    from pyruns.utils import settings

    assert Path(settings.__file__).resolve().is_relative_to(source)
    count = 200
    with tempfile.TemporaryDirectory(prefix="pyruns-json-lock-cost-") as temporary:
        root = Path(temporary)
        settings.save_setting_for_root(str(root), "header_refresh_interval", 3.0)
        started = time.perf_counter()
        for index in range(count):
            settings.save_setting_for_root(str(root), "header_refresh_interval", float(index % 5 + 1))
        saves = time.perf_counter() - started
        assert settings.load_settings(str(root))["header_refresh_interval"] == float((count - 1) % 5 + 1)
        generator = TaskGenerator(str(root / "tasks"))
        warmup = generator.reserve_exact_task_name("warmup")
        assert warmup is not None
        generator.release_task_name_reservation(warmup)
        started = time.perf_counter()
        for _ in range(count):
            reservation = generator.reserve_exact_task_name("alpha")
            assert reservation is not None
            generator.release_task_name_reservation(reservation)
        reservations = time.perf_counter() - started
        assert list((root / "tasks").iterdir()) == []
        assert not Path(settings._settings_path(str(root)) + ".lock").exists()
    files = ("pyruns/utils/settings.py", "pyruns/core/task_generator.py", "pyruns/utils/lock_owner.py")
    return {"settings_save_seconds": saves, "name_reservations_seconds": reservations, "operations": count,
            "platform": sys.platform,
            "source_sha256": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                              for name in files if (source / name).exists()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.source is not None:
        print(json.dumps(measure(args.source.resolve())))
        return
    rows = []
    for variant in ("baseline", "candidate", "candidate", "baseline") * 2:
        command = [sys.executable, str(Path(__file__).resolve()), "--source", str(getattr(args, variant).resolve())]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=90, **kwargs)
        rows.append({"variant": variant, **json.loads(result.stdout)})
    medians = {variant: {key: statistics.median(row[key] for row in rows if row["variant"] == variant)
                         for key in ("settings_save_seconds", "name_reservations_seconds")}
               for variant in ("baseline", "candidate")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"samples": rows, "medians": medians}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(medians))


if __name__ == "__main__":
    main()
