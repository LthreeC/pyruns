"""Compare explicit SDK call sizes on one runner using one installed wheel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--pip-zipapp", type=Path)
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    def execute(argv, name):
        print("Running:", *map(str, argv), flush=True)
        with (output / (name + ".log")).open("w", encoding="utf-8") as stream:
            result = subprocess.run(list(map(str, argv)), cwd=output, stdout=stream, stderr=subprocess.STDOUT,
                                    env={key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}})
        print("Exit:", result.returncode, "Log:", name, flush=True)
        return result.returncode

    build_args = ["--no-isolation"] if args.no_build_isolation else []
    assert execute([sys.executable, "-m", "build", "--wheel", *build_args, "--outdir", output / "wheel", root], "build") == 0
    wheel, = (output / "wheel").glob("*.whl")
    python = args.python
    if python is None:
        environment = output / "environment"
        venv.create(environment, with_pip=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = python.resolve()
    pip = [python, args.pip_zipapp] if args.pip_zipapp else [python, "-m", "pip"]
    assert execute([*pip, "install", str(wheel) + "[test]"], "dependencies") == 0
    assert execute([*pip, "install", "--force-reinstall", "--no-deps", wheel], "install") == 0
    assert execute([*pip, "check"], "pip-check") == 0
    manifest = {"commit": os.environ.get("GITHUB_SHA"), "host": platform.node(), "system": platform.platform(),
                "python": sys.version, "wheel": str(wheel.relative_to(output)),
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "runs": []}

    def save():
        (output / "comparison.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    manifest["tests_exit"] = execute([python, "-I", "-m", "pytest", "--import-mode=importlib", "-q",
                                      root / "tests/test_track_store.py", root / "tests/test_integration.py",
                                      root / "tests/test_package_imports.py", "-o", "faulthandler_timeout=30",
                                      f"--junitxml={output / 'tests.xml'}"], "tests")
    save()
    modes = ("single", "batch") if args.smoke else ("single", "batch", "batch", "single")
    for index, mode in enumerate(modes):
        script = root / ("scripts/check_installed_lifecycle.py" if mode == "single" else ".github/check_track_batch_lifecycle.py")
        label = f"{index}-{mode}"
        report = output / label / "report.json"
        code = execute([python, "-I", script, "--output", report], label)
        manifest["runs"].append({"mode": mode, "batch_size": 100 if mode == "batch" else 1,
                                 "exit_code": code, "report": str(report.relative_to(output))})
        save()
    assert manifest["tests_exit"] == 0 and all(run["exit_code"] == 0 for run in manifest["runs"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
