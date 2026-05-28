"""Web server package for the React-based Pyruns UI."""

__all__ = ["PyrunsRuntime", "create_app", "main"]


def __getattr__(name: str):
    if name == "PyrunsRuntime":
        from .runtime import PyrunsRuntime

        return PyrunsRuntime
    if name in {"create_app", "main"}:
        from .app import create_app, main

        return {"create_app": create_app, "main": main}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
