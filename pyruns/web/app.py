"""FastAPI app and unified server entry point for the React-based UI."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pyruns import __version__
from pyruns._config import DEFAULT_UI_PORT
from pyruns.utils.events import log_emitter
from pyruns.utils.shell_runtime import get_follow_shell_runtime
from pyruns.web.runtime import PyrunsRuntime
from pyruns.utils import get_logger

logger = get_logger(__name__)

LOG_STREAM_QUEUE_LIMIT = 256
LOG_STREAM_DROPPED_NOTICE = (
    "[pyruns] Live log stream skipped older buffered output; "
    "open the log file for full history.\n"
)


class RunRootRequest(BaseModel):
    """Workspace switch request payload."""

    path: str = Field(min_length=1)


class TaskActionRequest(BaseModel):
    """Task action request payload."""

    execution_mode: str | None = None


class TaskBatchActionRequest(BaseModel):
    """Batch task action request payload."""

    task_names: list[str] = Field(default_factory=list)
    execution_mode: str | None = None
    max_workers: int | None = None


class TaskBatchDeleteRequest(BaseModel):
    """Batch delete payload."""

    task_names: list[str] = Field(default_factory=list)


class TaskPinRequest(BaseModel):
    """Pin or unpin one task."""

    pinned: bool | None = None


class TaskReorderItem(BaseModel):
    """One task position in a manual card order request."""

    name: str = Field(min_length=1)
    pinned: bool | None = None


class TaskReorderRequest(BaseModel):
    """Manual task card order payload."""

    items: list[TaskReorderItem] = Field(default_factory=list)


class TaskNotesRequest(BaseModel):
    """Notes update payload."""

    notes: str = ""


class TaskEnvRequest(BaseModel):
    """Env update payload."""

    env: dict[str, Any] = Field(default_factory=dict)


class RuntimeUpdateRequest(BaseModel):
    """Workspace runtime settings update payload."""

    python_executable: str | None = None
    conda_env: str | None = None
    conda_executable: str | None = None
    global_env: dict[str, Any] | None = None
    global_env_text: str | None = None
    gpu_scheduler: dict[str, Any] | None = None


class TaskRenameRequest(BaseModel):
    """Rename payload."""

    new_name: str = Field(min_length=1)


class LauncherOpenRequest(BaseModel):
    """Launcher selection payload."""

    script_path: str = Field(min_length=1)
    config_path: str | None = None


class LauncherConfigPickRequest(BaseModel):
    """Native config picker payload."""

    script_path: str = Field(min_length=1)


class ShellRootOpenRequest(BaseModel):
    """Manual shell workspace folder selection payload."""

    path: str = Field(min_length=1)


class GeneratorCreateRequest(BaseModel):
    """Task generation payload for the React generator workspace."""

    name_prefix: str = Field(min_length=1)
    mode: str = Field(default="form", min_length=1)
    yaml_text: str = ""
    shell_text: str = ""
    template_value: str = ""
    append_timestamp: bool = True


class GeneratorPreviewRequest(BaseModel):
    """Task preview payload for the React generator workspace."""

    mode: str = Field(default="form", min_length=1)
    yaml_text: str = ""
    shell_text: str = ""
    template_value: str = ""


def _frontend_candidates() -> list[Path]:
    project_root = Path(__file__).resolve().parents[2]
    return [
        # project_root / "frontend" / "dist", # we don't want to serve from the source dir to avoid accidentally running unbuilt code
        Path(__file__).resolve().parent / "static",
    ]


def _frontend_dist_dir() -> Path | None:
    for candidate in _frontend_candidates():
        if candidate.exists() and candidate.is_dir() and (candidate / "index.html").exists():
            return candidate
    return None


def _fallback_frontend_html() -> str:
    return """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Pyruns React UI</title>
    <style>
      body {
        margin: 0;
        font-family: "Segoe UI", sans-serif;
        background: linear-gradient(135deg, #0f172a, #1e293b 55%, #134e4a);
        color: #e2e8f0;
      }
      main {
        max-width: 760px;
        margin: 10vh auto;
        padding: 32px;
        background: rgba(15, 23, 42, 0.76);
        border: 1px solid rgba(148, 163, 184, 0.2);
        box-shadow: 0 24px 80px rgba(15, 23, 42, 0.35);
      }
      h1 { margin-top: 0; font-size: 28px; }
      p, li { line-height: 1.6; color: #cbd5e1; }
      code {
        background: rgba(15, 23, 42, 0.95);
        padding: 2px 6px;
      }
      a { color: #5eead4; }
    </style>
  </head>
  <body>
    <main>
      <h1>Pyruns API server is running</h1>
      <p>The React source tree is present, but no built frontend bundle was found yet.</p>
      <p>Once the frontend is built into <code>frontend/dist</code> or <code>pyruns/web/static</code>, this page will serve it automatically.</p>
      <ul>
        <li>Workspace API: <a href="/api/workspace">/api/workspace</a></li>
        <li>Task list API: <a href="/api/tasks">/api/tasks</a></li>
        <li>Metrics API: <a href="/api/system/metrics">/api/system/metrics</a></li>
      </ul>
    </main>
  </body>
</html>
""".strip()


def _schedule_browser_open(url: str, *, delay_seconds: float = 0.8) -> None:
    """Open the local UI shortly after the server starts listening."""

    def _open() -> None:
        time.sleep(delay_seconds)
        try:
            webbrowser.open(url)
        except Exception:
            return

    threading.Thread(target=_open, daemon=True).start()


def _env_truthy(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and value.strip().lower() not in {"", "0", "false", "no", "off"}


def _env_falsey(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and value.strip().lower() in {"0", "false", "no", "off"}


def _can_open_browser_from_environment() -> bool:
    """Return whether auto-opening a browser is suitable for this process."""

    if _env_truthy("PYRUNS_NO_BROWSER"):
        return False
    if _env_truthy("PYRUNS_OPEN_BROWSER"):
        return True
    if _env_falsey("PYRUNS_OPEN_BROWSER"):
        return False
    if os.getenv("TMUX"):
        return False
    if sys.platform.startswith(("linux", "freebsd", "openbsd")):
        if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
            return False
    return True


def find_available_port(start_port: int, *, host: str = "127.0.0.1", max_attempts: int = 100) -> int:
    """Return the first local TCP port available at or after ``start_port``."""

    try:
        start = int(start_port)
    except (TypeError, ValueError):
        start = DEFAULT_UI_PORT
    if start < 1 or start > 65535:
        start = DEFAULT_UI_PORT

    stop = min(65535, start + max(0, int(max_attempts)))
    for port in range(start, stop + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port

    raise RuntimeError(f"No available UI port found from {start} to {stop}")


def create_app(runtime: PyrunsRuntime | None = None) -> FastAPI:
    """Create the Pyruns FastAPI app."""
    get_follow_shell_runtime()
    app = FastAPI(title="Pyruns API", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.runtime = runtime or PyrunsRuntime()

    dist_dir = _frontend_dist_dir()
    
    logger.info(f"Frontend dist directory: {dist_dir}")
    
    if dist_dir is not None:
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    def get_runtime() -> PyrunsRuntime:
        return app.state.runtime

    @app.get("/api/workspace")
    def get_workspace() -> dict[str, Any]:
        return get_runtime().get_workspace_info()

    @app.post("/api/workspace/run-root")
    def set_run_root(payload: RunRootRequest) -> dict[str, Any]:
        try:
            return get_runtime().change_run_root(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/workspace/shell")
    def open_shell_workspace() -> dict[str, Any]:
        try:
            return get_runtime().open_shell_workspace()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runtime")
    def get_runtime_info() -> dict[str, Any]:
        return get_runtime().get_runtime_info()

    @app.patch("/api/runtime")
    def update_runtime_info(
        payload: RuntimeUpdateRequest,
        refresh_providers: bool = Query(False),
    ) -> dict[str, Any]:
        try:
            data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
            return get_runtime().update_runtime_settings(data, refresh_providers=refresh_providers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/templates")
    def get_templates() -> dict[str, Any]:
        return {"items": get_runtime().list_templates()}

    @app.get("/api/templates/content")
    def get_template_content(value: str) -> dict[str, Any]:
        try:
            return get_runtime().get_template_content(value)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/generator/create")
    def create_tasks_from_generator(payload: GeneratorCreateRequest) -> dict[str, Any]:
        try:
            return get_runtime().create_tasks_from_template(
                name_prefix=payload.name_prefix,
                mode=payload.mode,
                yaml_text=payload.yaml_text,
                shell_text=payload.shell_text,
                template_value=payload.template_value,
                append_timestamp=payload.append_timestamp,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/generator/preview")
    def preview_tasks_from_generator(payload: GeneratorPreviewRequest) -> dict[str, Any]:
        try:
            return get_runtime().preview_tasks_from_template(
                mode=payload.mode,
                yaml_text=payload.yaml_text,
                shell_text=payload.shell_text,
                template_value=payload.template_value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/generator/pick-shell-file")
    def pick_generator_shell_file() -> dict[str, Any]:
        try:
            return get_runtime().pick_generator_shell_file()
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/dashboard")
    def get_dashboard(refresh: bool = True, recent_limit: int = 6) -> dict[str, Any]:
        return get_runtime().get_dashboard(refresh=refresh, recent_limit=recent_limit)

    @app.get("/api/launcher/scripts")
    def get_launcher_scripts() -> dict[str, Any]:
        return {"items": get_runtime().list_launcher_scripts()}

    @app.get("/api/launcher/configs")
    def get_launcher_configs(script: str) -> dict[str, Any]:
        try:
            return get_runtime().get_launcher_config_info(script)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/launcher/workspaces")
    def get_launcher_workspaces(script: str, config: str | None = None) -> dict[str, Any]:
        try:
            return {"items": get_runtime().list_launcher_workspaces(script, config)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/launcher/validate-path")
    def validate_launcher_path(
        kind: str = Query(min_length=1),
        path: str = Query(min_length=1),
        script: str | None = None,
    ) -> dict[str, Any]:
        return get_runtime().validate_launcher_path(kind, path, script)

    @app.post("/api/launcher/open")
    def open_launcher_workspace(payload: LauncherOpenRequest) -> dict[str, Any]:
        try:
            return get_runtime().open_launcher_workspace(
                payload.script_path,
                payload.config_path,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/launcher/pick-script")
    def pick_launcher_script() -> dict[str, Any]:
        try:
            return get_runtime().pick_and_open_launcher_workspace()
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/launcher/pick-script-path")
    def pick_launcher_script_path() -> dict[str, Any]:
        try:
            return get_runtime().pick_launcher_script_path()
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/launcher/pick-config-path")
    def pick_launcher_config_path(payload: LauncherConfigPickRequest) -> dict[str, Any]:
        try:
            return get_runtime().pick_launcher_config_path(payload.script_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/launcher/pick-shell-root")
    def pick_launcher_shell_root() -> dict[str, Any]:
        try:
            return get_runtime().pick_and_open_shell_workspace()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/launcher/open-shell-root")
    def open_launcher_shell_root(payload: ShellRootOpenRequest) -> dict[str, Any]:
        try:
            return get_runtime().open_shell_workspace_at(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    def get_tasks(
        query: str = "",
        status: str = "All",
        offset: int = 0,
        limit: int = 50,
        refresh: bool = True,
        summary: bool = False,
    ) -> dict[str, Any]:
        page = get_runtime().list_tasks(
            query=query,
            status=status,
            offset=offset,
            limit=limit,
            refresh=refresh,
            summary=summary,
        )
        return {
            "items": page.items,
            "total": page.total,
            "offset": page.offset,
            "limit": page.limit,
            "has_more": page.has_more,
        }

    @app.post("/api/tasks/reorder")
    def reorder_tasks(payload: TaskReorderRequest) -> dict[str, Any]:
        try:
            items = [
                item.model_dump() if hasattr(item, "model_dump") else item.dict()
                for item in payload.items
            ]
            return get_runtime().reorder_tasks(items)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_name}")
    def get_task(task_name: str, refresh: bool = True) -> dict[str, Any]:
        task = get_runtime().get_task(task_name, refresh=refresh)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found")
        return task

    @app.post("/api/tasks/batch/run")
    def run_tasks_batch(payload: TaskBatchActionRequest) -> dict[str, Any]:
        try:
            return get_runtime().start_tasks_batch(
                payload.task_names,
                execution_mode=payload.execution_mode,
                max_workers=payload.max_workers,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/batch/delete")
    def delete_tasks_batch(payload: TaskBatchDeleteRequest) -> dict[str, Any]:
        try:
            return get_runtime().delete_tasks_batch(payload.task_names)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/export/csv")
    def export_tasks_csv(payload: TaskBatchDeleteRequest) -> Response:
        try:
            csv_text = get_runtime().export_tasks_csv(payload.task_names)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=csv_text, media_type="text/csv; charset=utf-8")

    @app.post("/api/tasks/{task_name}/run")
    def run_task(task_name: str, payload: TaskActionRequest | None = None) -> dict[str, Any]:
        try:
            execution_mode = payload.execution_mode if payload is not None else None
            task = get_runtime().start_task(task_name, execution_mode)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.post("/api/tasks/{task_name}/cancel")
    def cancel_task(task_name: str) -> dict[str, Any]:
        try:
            task = get_runtime().cancel_task(task_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.post("/api/tasks/{task_name}/pin")
    def pin_task(task_name: str, payload: TaskPinRequest) -> dict[str, Any]:
        try:
            task = get_runtime().set_task_pin(task_name, payload.pinned)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.patch("/api/tasks/{task_name}/notes")
    def update_task_notes(task_name: str, payload: TaskNotesRequest) -> dict[str, Any]:
        try:
            task = get_runtime().update_task_notes(task_name, payload.notes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.patch("/api/tasks/{task_name}/env")
    def update_task_env(task_name: str, payload: TaskEnvRequest) -> dict[str, Any]:
        try:
            task = get_runtime().update_task_env(task_name, payload.env)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.post("/api/tasks/{task_name}/rename")
    def rename_task(task_name: str, payload: TaskRenameRequest) -> dict[str, Any]:
        try:
            task = get_runtime().rename_task(task_name, payload.new_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.get("/api/tasks/{task_name}/logs")
    def get_task_logs(
        task_name: str,
        log_file_name: str | None = None,
        offset: int | None = Query(default=None),
        tail_bytes: int | None = Query(default=None),
        tail_lines: int | None = Query(default=None),
        chunk_size: int | None = Query(default=None),
    ) -> dict[str, Any]:
        try:
            return get_runtime().get_task_logs(
                task_name,
                log_file_name=log_file_name,
                offset=offset,
                tail_bytes=tail_bytes,
                tail_lines=tail_lines,
                chunk_size=chunk_size,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc

    @app.websocket("/api/tasks/{task_name}/logs/stream")
    async def stream_task_logs(websocket: WebSocket, task_name: str) -> None:
        runtime = get_runtime()
        if runtime.get_task(task_name, refresh=False) is None:
            await websocket.close(code=4404, reason="Task not found")
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        log_emitter.bind_loop(loop)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=LOG_STREAM_QUEUE_LIMIT)
        disconnected = asyncio.Event()
        dropped_notice_sent = False

        def on_chunk(chunk_text: str, metadata: dict[str, Any] | None = None) -> None:
            nonlocal dropped_notice_sent
            if disconnected.is_set():
                return
            message = {
                "type": "chunk",
                "task_name": task_name,
                "content": chunk_text,
            }
            offset = (metadata or {}).get("offset")
            if offset is not None:
                message["offset"] = offset
            try:
                queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            if not dropped_notice_sent:
                message = {**message, "content": LOG_STREAM_DROPPED_NOTICE + chunk_text}
                dropped_notice_sent = True
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.debug("Dropping live log chunk for %s because websocket queue is full", task_name)

        async def watch_client_messages() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                disconnected.set()

        watcher = asyncio.create_task(watch_client_messages())
        log_emitter.subscribe(task_name, on_chunk, loop=loop, include_metadata=True)
        try:
            while not disconnected.is_set():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    await websocket.send_json(message)
                except (WebSocketDisconnect, RuntimeError):
                    disconnected.set()
                    break
        except WebSocketDisconnect:
            pass
        finally:
            disconnected.set()
            watcher.cancel()
            log_emitter.unsubscribe(task_name, on_chunk)

    @app.get("/api/system/metrics")
    def get_metrics() -> dict[str, Any]:
        return get_runtime().get_metrics()

    if dist_dir is not None:

        @app.get("/{full_path:path}")
        def serve_frontend(full_path: str) -> FileResponse:
            requested = (dist_dir / full_path).resolve()
            if (
                full_path
                and requested.exists()
                and requested.is_file()
                and os.path.commonpath([str(requested), str(dist_dir)]) == str(dist_dir)
            ):
                return FileResponse(requested)
            return FileResponse(
                dist_dir / "index.html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

    else:

        @app.get("/{full_path:path}")
        def serve_frontend_fallback(full_path: str) -> HTMLResponse:
            return HTMLResponse(_fallback_frontend_html())

    return app


def main(
    *,
    reload: bool = False,
    open_browser: bool | None = None,
    start_path: str = "/",
    port: int | None = None,
) -> None:
    """Launch the unified Pyruns API and frontend server."""
    runtime = PyrunsRuntime()
    host = "127.0.0.1"
    configured_port = int(port if port is not None else runtime.settings.get("ui_port", DEFAULT_UI_PORT))
    port = find_available_port(configured_port, host=host)
    if port != configured_port:
        print(f"[pyruns] Port {configured_port} is busy; using {port} instead.")
    url = f"http://{host}:{port}{start_path}"
    print(f"[pyruns] UI: {url}")
    should_open_browser = (not reload and _can_open_browser_from_environment()) if open_browser is None else open_browser
    if should_open_browser:
        _schedule_browser_open(url)
    else:
        print("[pyruns] Browser auto-open disabled; open the URL manually.")
    try:
        uvicorn.run(
            "pyruns.web.app:create_app" if reload else create_app(runtime),
            host=host,
            port=port,
            reload=reload,
            factory=reload,
            access_log=False,
            log_level="warning",
        )
    finally:
        shutdown = getattr(runtime, "shutdown", None)
        if callable(shutdown):
            shutdown()


def _parse_main_options(args: list[str]) -> tuple[int | None, bool | None]:
    """Parse minimal UI launch options for ``python -m pyruns.web.app``."""

    index = 0
    selected_port: int | None = None
    open_browser: bool | None = None
    while index < len(args):
        arg = args[index]
        if arg == "-p" or arg == "--port":
            if index + 1 >= len(args):
                print(f"Missing value for {arg}.")
                sys.exit(1)
            selected_port = _parse_port_value(args[index + 1])
            index += 2
            continue
        if arg.startswith("--port="):
            selected_port = _parse_port_value(arg.split("=", 1)[1])
            index += 1
            continue
        if arg == "--no-browser":
            open_browser = False
            index += 1
            continue
        if arg in {"--browser", "--open-browser"}:
            open_browser = True
            index += 1
            continue
        index += 1
    return selected_port, open_browser


def _parse_port_value(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"Invalid port: {raw}")
        sys.exit(1)
    if value < 1 or value > 65535:
        print("Port must be between 1 and 65535.")
        sys.exit(1)
    return value


if __name__ == "__main__":
    main_port, main_open_browser = _parse_main_options(sys.argv[1:])
    main(reload=True, port=main_port, open_browser=main_open_browser)
