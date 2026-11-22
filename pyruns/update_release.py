"""Install and select app releases without replacing a live runner's files."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from importlib.metadata import distributions
from pathlib import Path

from packaging.version import InvalidVersion, Version


_RELEASES_DIR = ".pyruns-releases"
_RELEASE_NAME = re.compile(r"release-[a-z0-9_]{8,32}")
_RUNTIME_TAG = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
_MODULE_BOOTSTRAP = (
    "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
    "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
)


def current_release_root() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    if root.parent.name == _RELEASES_DIR and _RELEASE_NAME.fullmatch(root.name):
        return root
    return None


def installation_root() -> Path:
    release = current_release_root()
    return release.parent.parent if release is not None else Path(__file__).resolve().parent.parent


def _releases_root() -> Path:
    return installation_root() / _RELEASES_DIR


def _release_marker() -> str:
    return str(_releases_root() / f"active-{_RUNTIME_TAG}.json")


def _distribution_version(root: Path) -> str:
    try:
        package = next(iter(distributions(name="pyruns", path=[str(root)])), None)
        return str(Version(package.version)) if package is not None else ""
    except (OSError, TypeError, ValueError):
        return ""


def _verified_release(name: str, expected_version: str) -> Path | None:
    if not _RELEASE_NAME.fullmatch(name):
        return None
    candidate = _releases_root() / name
    try:
        if (
            candidate.resolve(strict=True) == candidate
            and (candidate / "pyruns" / "__init__.py").is_file()
            and _distribution_version(candidate) == expected_version
        ):
            return candidate
    except (OSError, RuntimeError):
        pass
    return None


def selected_release(fallback: str) -> tuple[str, Path | None]:
    """Read the version for new app processes, including later manual pip installs."""
    from pyruns.update_coordination import _bounded_json

    installed = _distribution_version(installation_root())
    running_release = current_release_root()
    running_version = _distribution_version(running_release) if running_release is not None else ""
    selected_root = None
    if running_version and (not installed or Version(running_version) > Version(installed)):
        installed, selected_root = running_version, running_release
    record = _bounded_json(_release_marker())
    if record and isinstance(record.get("directory"), str) and isinstance(record.get("version"), str):
        try:
            version = Version(record["version"])
            if not installed or version > Version(installed):
                root = _verified_release(record["directory"], str(version))
                if root is not None:
                    return str(version), root
        except InvalidVersion:
            pass
    return installed or fallback, selected_root


def create_release_directory() -> Path:
    root = _releases_root()
    root.mkdir(exist_ok=True)
    if root.resolve() != root:
        raise OSError("The Pyruns release directory cannot redirect outside this installation.")
    return Path(tempfile.mkdtemp(prefix="release-", dir=root))


def discard_unpublished_release(root: Path) -> None:
    """Remove an install that never became active."""
    if root.parent == _releases_root() and _RELEASE_NAME.fullmatch(root.name) and root.resolve() == root:
        shutil.rmtree(root, ignore_errors=True)


def publish_release(root: Path, version: str) -> None:
    from pyruns.update_coordination import _atomic_write_json

    if _verified_release(root.name, version) != root:
        raise ValueError("The staged Pyruns release is unavailable.")
    if os.name != "nt":
        root.chmod(root.parent.stat().st_mode & 0o755)
    _atomic_write_json(_release_marker(), {"directory": root.name, "version": version})


def module_command(module: str, *args: str, release: Path | None = None) -> list[str]:
    if release is None:
        return [sys.executable, "-m", module, *args]
    return [sys.executable, "-I", "-c", _MODULE_BOOTSTRAP, str(release), module, *args]


def launch_cli_release(release: Path, program: str, args: list[str]) -> int:
    command = [
        sys.executable, "-I", "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from pyruns.cli.app import main; "
        "raise SystemExit(main(sys.argv[2:], prog=sys.argv[1]))",
        str(release), program, *args,
    ]
    if os.name == "nt":
        from pyruns.utils.process_utils import hidden_subprocess_kwargs
        from pyruns.utils.terminal_capture import _windows_console_available

        process = subprocess.Popen(
            command,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            **({} if _windows_console_available() else hidden_subprocess_kwargs()),
        )
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The selected CLI also receives Ctrl+C on the shared console.
            # Give it time to cancel the exact foreground run it submitted.
            try:
                return process.wait(timeout=30)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                return 130
    os.execv(sys.executable, command)
    raise RuntimeError("Could not launch the selected Pyruns release.")
