"""Verify committed frontend static assets match a fresh Vite build."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
STATIC_DIR = ROOT / "pyruns" / "web" / "static"


def _hash_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    data = path.read_bytes()
    if b"\0" in data:
        return _hash_bytes(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _hash_bytes(data)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _hash_bytes(normalized.encode("utf-8"))


def _snapshot_files(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    files: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files[path.relative_to(root).as_posix()] = _file_hash(path)
    return files


def check_frontend_static(*, root: Path = ROOT) -> list[str]:
    frontend_dir = root / "frontend"
    static_dir = root / "pyruns" / "web" / "static"
    errors: list[str] = []

    if not frontend_dir.exists():
        return [f"frontend directory not found: {frontend_dir}"]
    if not static_dir.exists():
        return [f"committed static directory not found: {static_dir}"]
    npm_executable = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if npm_executable is None:
        return ["npm executable not found; install Node.js/npm to check frontend static assets"]

    with tempfile.TemporaryDirectory(prefix="pyruns-frontend-static-") as tmp:
        build_dir = Path(tmp) / "static"
        command = [
            npm_executable,
            "run",
            "build",
            "--",
            "--outDir",
            str(build_dir),
            "--emptyOutDir",
        ]
        result = subprocess.run(
            command,
            cwd=frontend_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        if result.returncode != 0:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            return [f"frontend build failed with exit code {result.returncode}\n{output}".rstrip()]

        expected = _snapshot_files(build_dir)
        committed = _snapshot_files(static_dir)

    expected_files = set(expected)
    committed_files = set(committed)
    for path in sorted(expected_files - committed_files):
        errors.append(f"missing committed static asset: {path}")
    for path in sorted(committed_files - expected_files):
        errors.append(f"stale committed static asset: {path}")
    for path in sorted(expected_files & committed_files):
        if expected[path] != committed[path]:
            errors.append(f"changed committed static asset: {path}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    errors = check_frontend_static()
    if errors:
        print("Frontend static assets are not in sync with frontend/src:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Frontend static assets are in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
