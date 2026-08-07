"""Public API for the one-shot Pyruns CLI."""

__all__ = ["CliContext", "build_parser", "main"]


def __getattr__(name: str):
    if name in __all__:
        from .app import CliContext, build_parser, main

        return {
            "CliContext": CliContext,
            "build_parser": build_parser,
            "main": main,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
