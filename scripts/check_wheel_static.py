"""Check bundled Pyruns web static files inside a built wheel."""

from __future__ import annotations

import argparse
import glob
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_PREFIX = "pyruns/web/static/"
ASSETS_PREFIX = f"{STATIC_PREFIX}assets/"


class _StaticRefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.refs.append(value)


def _source_static_files(root: Path) -> set[str]:
    static_dir = root / "pyruns" / "web" / "static"
    return {
        path.relative_to(static_dir).as_posix()
        for path in static_dir.rglob("*")
        if path.is_file()
    }


def _wheel_static_files(wheel_path: Path) -> set[str]:
    with zipfile.ZipFile(wheel_path) as wheel:
        return {
            name.removeprefix(STATIC_PREFIX)
            for name in wheel.namelist()
            if name.startswith(STATIC_PREFIX) and not name.endswith("/")
        }


def _wheel_text(wheel_path: Path, name: str) -> str:
    return _wheel_bytes(wheel_path, name).decode("utf-8")


def _wheel_bytes(wheel_path: Path, name: str) -> bytes:
    with zipfile.ZipFile(wheel_path) as wheel:
        return wheel.read(f"{STATIC_PREFIX}{name}")


def _asset_refs(index_html: str) -> list[str]:
    parser = _StaticRefParser()
    parser.feed(index_html)
    refs: list[str] = []
    for ref in parser.refs:
        if ref.startswith("/assets/"):
            refs.append(ref.removeprefix("/"))
        elif ref.startswith("assets/"):
            refs.append(ref)
    return refs


def check_wheel_static(wheel_path: Path, *, root: Path = ROOT) -> list[str]:
    source_files = _source_static_files(root)
    wheel_files = _wheel_static_files(wheel_path)
    source_assets = {name for name in source_files if name.startswith("assets/")}
    wheel_assets = {name for name in wheel_files if name.startswith("assets/")}

    errors: list[str] = []
    if not source_assets:
        errors.append("source static assets directory is empty")

    missing_assets = sorted(source_assets - wheel_assets)
    extra_assets = sorted(wheel_assets - source_assets)
    if missing_assets:
        errors.append("wheel is missing static assets: " + ", ".join(missing_assets))
    if extra_assets:
        errors.append("wheel contains stale static assets: " + ", ".join(extra_assets))

    if "index.html" not in wheel_files:
        errors.append("wheel is missing pyruns/web/static/index.html")
        return errors

    source_index = (root / "pyruns" / "web" / "static" / "index.html").read_bytes()
    wheel_index = _wheel_bytes(wheel_path, "index.html")
    if wheel_index != source_index:
        errors.append("wheel index.html does not match source static index.html")

    for ref in _asset_refs(wheel_index.decode("utf-8")):
        if ref not in wheel_files:
            errors.append(f"wheel index.html references missing asset: {ref}")

    return errors


def _expand_wheel_args(values: list[str]) -> list[Path]:
    wheel_paths: list[Path] = []
    for value in values:
        matches = glob.glob(value)
        if matches:
            wheel_paths.extend(Path(match) for match in matches)
        else:
            wheel_paths.append(Path(value))
    return wheel_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", nargs="+", help="Wheel path or glob to inspect")
    args = parser.parse_args(argv)

    wheel_paths = _expand_wheel_args(args.wheel)
    if not wheel_paths:
        print("No wheel files matched.", file=sys.stderr)
        return 2

    failed = False
    for wheel_path in wheel_paths:
        errors = check_wheel_static(wheel_path)
        if errors:
            failed = True
            print(f"{wheel_path}:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
