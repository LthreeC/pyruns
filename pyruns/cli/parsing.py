"""
Argparse-based normalization for direct CLI commands.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


class CLIParseError(Exception):
    """Raised when CLI arguments are invalid."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


class _NoExitParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIParseError(f"{self.prog}: {message}", code=2)


@dataclass
class GlobalCLIOptions:
    json: bool = False
    quiet: bool = False
    no_color: bool = False


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def parse_global_options(argv: list[str]) -> tuple[GlobalCLIOptions, list[str]]:
    options = GlobalCLIOptions()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--json":
            options.json = True
            index += 1
            continue
        if arg == "--quiet":
            options.quiet = True
            index += 1
            continue
        if arg == "--no-color":
            options.no_color = True
            index += 1
            continue
        break
    return options, argv[index:]


def normalize_direct_command(command: str, args: list[str]) -> list[str]:
    cmd = command.lower()

    if cmd in {"ls", "list"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("-i", "--interactive", action="store_true")
        parser.add_argument("-s", "--status", action="append", default=[])
        parser.add_argument("-n", "--limit", type=_positive_int)
        parser.add_argument("search_terms", nargs="*")
        ns = parser.parse_args(args)
        out: list[str] = []
        if ns.interactive:
            out.append("--interactive")
        for value in ns.status:
            out.extend(["--status", value])
        if ns.limit is not None:
            out.extend(["--limit", str(ns.limit)])
        out.extend(ns.search_terms)
        return out

    if cmd in {"run"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("-d", "--detach", "--no-follow", action="store_true")
        parser.add_argument("-w", "--workers", type=_positive_int)
        parser.add_argument("-m", "--mode", choices=["thread", "process"])
        parser.add_argument("targets", nargs="+")
        ns = parser.parse_args(args)
        out = list(ns.targets)
        if ns.workers is not None:
            out.extend(["--workers", str(ns.workers)])
        if ns.mode:
            out.extend(["--mode", ns.mode])
        if ns.detach:
            out.append("--detach")
        return out

    if cmd in {"delete", "del", "rm"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("-y", "--yes", action="store_true")
        parser.add_argument("targets", nargs="+")
        ns = parser.parse_args(args)
        out = list(ns.targets)
        if ns.yes:
            out.append("--yes")
        return out

    if cmd in {"show", "inspect", "log", "fg"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("target")
        ns = parser.parse_args(args)
        return [ns.target]

    if cmd in {"open"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("target")
        parser.add_argument("file_type", nargs="?")
        ns = parser.parse_args(args)
        out = [ns.target]
        if ns.file_type:
            out.append(ns.file_type)
        return out

    if cmd in {"gen", "generate", "gentask"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("template", nargs="?")
        ns = parser.parse_args(args)
        return [ns.template] if ns.template else []

    if cmd in {"export"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("-a", "--all", action="store_true")
        parser.add_argument("-s", "--status", action="append", default=[])
        parser.add_argument("-f", "--format", choices=["csv", "json"])
        parser.add_argument("-o", "--output")
        parser.add_argument("targets", nargs="*")
        ns = parser.parse_args(args)
        out = list(ns.targets)
        if ns.all:
            out.append("--all")
        for value in ns.status:
            out.extend(["--status", value])
        if ns.format:
            out.extend(["--format", ns.format])
        if ns.output:
            out.extend(["--output", ns.output])
        return out

    if cmd in {"stat", "status"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.add_argument("-i", "--interactive", action="store_true")
        ns = parser.parse_args(args)
        return ["--interactive"] if ns.interactive else []

    if cmd in {"jobs", "info"}:
        parser = _NoExitParser(prog=cmd, add_help=False)
        parser.parse_args(args)
        return []

    return args
