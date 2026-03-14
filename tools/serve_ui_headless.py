"""Start the NiceGUI app on a custom port without creating its process pool.

This is a small development helper for local screenshot capture in constrained
environments where NiceGUI's default process-pool setup is not permitted.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Run root passed via __PYRUNS_ROOT__")
    parser.add_argument("--port", type=int, default=8110)
    args = parser.parse_args()

    os.environ["__PYRUNS_ROOT__"] = args.root

    from nicegui import ui
    import nicegui.run as nicegui_run
    import pyruns.ui.app as app

    # NiceGUI starts a process pool on startup; skip that for local screenshots.
    nicegui_run.setup = lambda: None

    original_run = ui.run

    def patched_run(*run_args, **run_kwargs):
        run_kwargs["show"] = False
        run_kwargs["port"] = args.port
        return original_run(*run_args, **run_kwargs)

    ui.run = patched_run
    app.main()


if __name__ == "__main__":
    main()

