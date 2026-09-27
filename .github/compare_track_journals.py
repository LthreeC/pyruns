"""Compare rollback journals with the unchanged installed SDK/CLI lifecycle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pyruns/utils/track_store.py"
ORIGINAL = '''        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("Track storage requires DELETE journal mode")
'''


def candidate(source, mode):
    assert mode in ("delete", "truncate", "persist")
    assert source.count(ORIGINAL) == 1
    if mode == "delete":
        return source
    replacement = '''        if connection.execute("PRAGMA journal_mode").fetchone()[0] not in {"delete", "truncate", "persist"}:
            raise ValueError("Track storage requires rollback journal mode")
        if not readonly:
            if connection.execute("PRAGMA journal_mode=MODE").fetchone()[0] != "mode":
                raise ValueError("Track storage could not select MODE journal mode")
'''.replace("MODE", mode.upper()).replace('"mode"', repr(mode))
    if mode == "persist":
        replacement += '            connection.execute("PRAGMA journal_size_limit=1048576")\n'
    return source.replace(ORIGINAL, replacement)


def run_command(argv, log, *, cwd, env=None):
    print("Running:", *map(str, argv), flush=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    print("Exit:", result.returncode, "Log:", log, flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", type=Path, help="Use an existing isolated environment for local smoke")
    parser.add_argument("--pip-zipapp", type=Path)
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="Run each mode once, without the mirrored sequence")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original_bytes = SOURCE.read_bytes()
    original = original_bytes.decode("utf-8").replace("\r\n", "\n")
    wheels = {}
    manifest = {"commit": os.environ.get("GITHUB_SHA"), "host": platform.node(), "system": platform.platform(),
                "python": sys.version, "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "wheels": {}, "runs": [], "tests": {}}

    def save():
        (output / "comparison.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    try:
        for mode in ("delete", "truncate", "persist"):
            changed = candidate(original, mode)
            SOURCE.write_text(changed, encoding="utf-8", newline="\n")
            destination = output / "wheels" / mode
            build_args = ["--no-isolation"] if args.no_build_isolation else []
            assert run_command([sys.executable, "-m", "build", "--wheel", *build_args, "--outdir", destination],
                               output / f"build-{mode}.log", cwd=ROOT) == 0
            wheel, = destination.glob("*.whl")
            with zipfile.ZipFile(wheel) as archive:
                assert archive.read("pyruns/utils/track_store.py").decode("utf-8") == changed
                hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()
                          if name.startswith("pyruns/") and not name.endswith("/")}
            wheels[mode] = wheel
            manifest["wheels"][mode] = {"path": str(wheel.relative_to(output)),
                                         "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "package_files": hashes}
            save()
        baseline = manifest["wheels"]["delete"]["package_files"]
        for mode in ("truncate", "persist"):
            hashes = manifest["wheels"][mode]["package_files"]
            assert hashes.keys() == baseline.keys()
            assert [name for name in baseline if baseline[name] != hashes[name]] == ["pyruns/utils/track_store.py"]
    finally:
        SOURCE.write_bytes(original_bytes)
    python = args.python
    if python is None:
        environment = output / "environment"
        venv.create(environment, with_pip=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = python.resolve()
    pip = [python, args.pip_zipapp] if args.pip_zipapp else [python, "-m", "pip"]
    assert run_command([*pip, "install", str(wheels["delete"]) + "[test]"], output / "install.log", cwd=output) == 0
    assert run_command([*pip, "check"], output / "pip-check.log", cwd=output) == 0
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    sequence = ("delete", "truncate", "persist") if args.smoke else ("delete", "truncate", "persist", "persist", "truncate", "delete")
    previous = None
    for index, mode in enumerate(sequence):
        if mode != previous:
            assert run_command([*pip, "install", "--force-reinstall", "--no-deps", wheels[mode]],
                               output / f"install-{index}-{mode}.log", cwd=output) == 0
        previous = mode
        if mode != "delete" and mode not in manifest["tests"]:
            # Run the existing tests against the installed wheel. Their subprocesses
            # inherit this output directory, which contains no source package.
            test_exit = run_command([python, "-I", "-m", "pytest", "--import-mode=importlib", "-q",
                                     ROOT / "tests/test_track_store.py", "-o", "faulthandler_timeout=30",
                                     f"--junitxml={output / ('tests-' + mode + '.xml')}"],
                                    output / f"tests-{mode}.log", cwd=output, env=environment)
            manifest["tests"][mode] = {"exit_code": test_exit}
            save()
        label = f"{index}-{mode}"
        report = output / label / "report.json"
        code = run_command([python, "-I", ROOT / "scripts/check_installed_lifecycle.py", "--output", report],
                           output / f"{label}.log", cwd=output, env=environment)
        manifest["runs"].append({"index": index, "mode": mode, "exit_code": code,
                                 "report": str(report.relative_to(output))})
        save()
    assert SOURCE.read_bytes() == original_bytes
    assert all(item["exit_code"] == 0 for item in manifest["runs"])
    assert all(item["exit_code"] == 0 for item in manifest["tests"].values())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
