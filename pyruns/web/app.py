"""FastAPI app and unified server entry point for the React-based UI."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pyruns import __version__
from pyruns._config import (
    DEFAULT_UI_PORT,
    MAX_MONITOR_CHUNK_SIZE,
    MAX_MONITOR_SCROLLBACK,
    QUEUE_LOG_FILENAME,
    RUN_LOGS_DIR,
)
from pyruns.utils.events import log_emitter
from pyruns.utils.log_io import log_file_identity
from pyruns.utils.shell_runtime import get_follow_shell_runtime
from pyruns.web.runtime import (
    PyrunsRuntime,
    TaskEnvConflictError,
    TaskNotesConflictError,
    WorkspaceChangedError,
)
from pyruns.web.self_update import (
    ActiveTasksError,
    UI_PRODUCTION_RESTART_ENV,
    UI_TOKEN_ENV,
    LatestVersionCheckError,
    UiUpdateCoordinator,
    UpdateCheckError,
    UpdateInProgressError,
    check_latest_version,
    read_update_result,
    replace_process_with_updater,
)
from pyruns.utils import get_logger

logger = get_logger(__name__)

LOG_STREAM_QUEUE_LIMIT = 256
LOG_STREAM_TAIL_INTERVAL_SEC = 0.5
LOG_STREAM_TAIL_CHUNK_SIZE = 64 * 1024
LOG_STREAM_EMITTER_QUIET_SEC = 2.0
TASK_EVENT_HEARTBEAT_SEC = 15.0
MAX_API_REQUEST_BYTES = 8 * 1024 * 1024
MAX_TASK_BATCH_ITEMS = 10_000
MAX_TASK_PAGE_SIZE = 10_000
MAX_TASK_NOTES_CHARS = 1_000_000
MAX_ENVIRONMENT_ITEMS = 1_024
MAX_UI_WORKERS = 32
MAX_QUERY_CHARS = 2_048
MAX_PATH_CHARS = 32_768
MAX_LOG_TAIL_LINES = MAX_MONITOR_SCROLLBACK
MAX_LOG_RESPONSE_BYTES = MAX_MONITOR_CHUNK_SIZE
_UI_SESSION_COOKIE_PREFIX = "pyruns_session_"
_UI_TOKEN_ENV = UI_TOKEN_ENV
_UI_COOKIE_NONCE_ENV = "PYRUNS_UI_COOKIE_NONCE"
LOG_STREAM_DROPPED_NOTICE = (
    "[pyruns] Live log stream skipped older buffered output; "
    "open the log file for full history.\n"
)
_LOCAL_WEB_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ASGI_TEST_HOST = "testserver"


def _compact_monitor_task(task: dict[str, Any]) -> dict[str, Any]:
    """Strip detail/search fields after server-side filtering for Monitor polling."""

    item = dict(task)
    item.update(
        {
            "config": {},
            "config_text": "",
            "log": "",
            "env": {},
            "cmd": None,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "durations": [],
            "exit_codes": [],
            "source_states": [],
            "records": [],
            "tracks": [],
            "notes": "",
            "preview_text": "",
            "search_text": "",
        }
    )
    return item


def _authority_parts(
    authority: str,
    *,
    scheme: str,
    allow_test_host: bool = False,
) -> tuple[str, int] | None:
    """Return a normalized local authority or ``None`` for malformed input."""

    try:
        parsed = urlsplit(f"{scheme}://{authority}")
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return None
    if hostname not in _LOCAL_WEB_HOSTS and not (allow_test_host and hostname == _ASGI_TEST_HOST):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return hostname, port


def _is_allowed_local_host(host_header: str, *, scheme: str, allow_test_host: bool = False) -> bool:
    return _authority_parts(
        str(host_header or ""),
        scheme=scheme,
        allow_test_host=allow_test_host,
    ) is not None


def _origin_matches_request(
    origin: str,
    host_header: str,
    *,
    scheme: str,
    allow_test_host: bool = False,
) -> bool:
    """Reject cross-site browser access while allowing same-origin local UI calls."""

    try:
        parsed_origin = urlsplit(str(origin or ""))
    except ValueError:
        return False
    if parsed_origin.scheme not in {"http", "https"} or parsed_origin.scheme != scheme:
        return False
    if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
        return False
    origin_parts = _authority_parts(
        parsed_origin.netloc,
        scheme=parsed_origin.scheme,
        allow_test_host=allow_test_host,
    )
    request_parts = _authority_parts(host_header, scheme=scheme, allow_test_host=allow_test_host)
    return origin_parts is not None and origin_parts == request_parts


class RequestModel(BaseModel):
    """Reject misspelled fields instead of silently accepting a bad request."""

    if hasattr(BaseModel, "model_validate"):
        model_config = {"extra": "forbid"}
    else:
        class Config:
            extra = "forbid"


class RunRootRequest(RequestModel):
    """Workspace switch request payload."""

    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)


class TaskRunRequest(RequestModel):
    """Strictly validate that task runs do not accept options."""


class TaskBatchActionRequest(RequestModel):
    """Batch task action request payload."""

    task_names: list[str] = Field(default_factory=list)
    max_workers: int | None = Field(default=None, ge=1, le=MAX_UI_WORKERS)


class TaskBatchDeleteRequest(RequestModel):
    """Batch delete payload."""

    task_names: list[str] = Field(default_factory=list)


class TaskPinRequest(RequestModel):
    """Pin or unpin one task."""

    pinned: bool | None = None


class TaskReorderItem(RequestModel):
    """One task position in a manual card order request."""

    name: str = Field(min_length=1, max_length=200)
    pinned: bool | None = None


class TaskReorderRequest(RequestModel):
    """Manual task card order payload."""

    items: list[TaskReorderItem] = Field(default_factory=list)


class TaskNotesRequest(RequestModel):
    """Notes update payload."""

    notes: str = Field(default="", max_length=MAX_TASK_NOTES_CHARS)
    expected_notes: str = Field(max_length=MAX_TASK_NOTES_CHARS)


class TaskEnvRequest(RequestModel):
    """Env update payload."""

    env: dict[str, Any] = Field(default_factory=dict)
    expected_env: dict[str, Any]


class RuntimeUpdateRequest(RequestModel):
    """Workspace runtime settings update payload."""

    python_executable: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    conda_env: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    conda_executable: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    global_env: dict[str, Any] | None = None
    global_env_text: str | None = Field(default=None, max_length=MAX_API_REQUEST_BYTES)
    gpu_scheduler: dict[str, Any] | None = None


class TaskRenameRequest(RequestModel):
    """Rename payload."""

    new_name: str = Field(min_length=1, max_length=200)


class LauncherOpenRequest(RequestModel):
    """Launcher selection payload."""

    script_path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    config_path: str | None = Field(default=None, max_length=MAX_PATH_CHARS)


class LauncherConfigPickRequest(RequestModel):
    """Native config picker payload."""

    script_path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)


class ShellRootOpenRequest(RequestModel):
    """Manual shell workspace folder selection payload."""

    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)


class GeneratorCreateRequest(RequestModel):
    """Task generation payload for the React generator workspace."""

    name_prefix: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="form", min_length=1, max_length=16)
    yaml_text: str = Field(default="", max_length=MAX_API_REQUEST_BYTES)
    shell_text: str = Field(default="", max_length=MAX_API_REQUEST_BYTES)
    template_value: str = Field(default="", max_length=MAX_PATH_CHARS)
    append_timestamp: bool = True


class GeneratorPreviewRequest(RequestModel):
    """Task preview payload for the React generator workspace."""

    mode: str = Field(default="form", min_length=1, max_length=16)
    yaml_text: str = Field(default="", max_length=MAX_API_REQUEST_BYTES)
    shell_text: str = Field(default="", max_length=MAX_API_REQUEST_BYTES)
    template_value: str = Field(default="", max_length=MAX_PATH_CHARS)


def _frontend_candidates() -> list[Path]:
    return [
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
      <p>Once the frontend is built into <code>pyruns/web/static</code>, this page will serve it automatically.</p>
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


def _url_with_access_token(url: str, token: str) -> str:
    """Add the private UI bootstrap token without corrupting an existing query."""

    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "token"]
    query.append(("token", token))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _token_matches(candidate: Any, expected: Any) -> bool:
    """Compare untrusted token text without rejecting non-ASCII input."""

    candidate_bytes = str(candidate or "").encode("utf-8")
    expected_bytes = str(expected or "").encode("utf-8")
    return bool(candidate_bytes and expected_bytes) and secrets.compare_digest(
        candidate_bytes,
        expected_bytes,
    )


def _session_cookie_name(nonce: str | None = None) -> str:
    """Return one opaque cookie name for this server instance."""

    value = str(nonce or "")
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        value = secrets.token_hex(16)
    return f"{_UI_SESSION_COOKIE_PREFIX}{value}"


def _session_cookie_nonce_for_port(port: int) -> str:
    """Reuse one cookie slot when a later UI instance reuses the same port."""

    return f"{int(port):032x}"


def _clean_bootstrap_target(request: Request) -> str:
    query = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "token"
    ]
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{request.url.path}{suffix}"


def create_app(
    runtime: PyrunsRuntime | None = None,
    *,
    access_token: str | None = None,
    session_cookie_nonce: str | None = None,
    allow_test_client_bypass: bool = False,
    update_coordinator: UiUpdateCoordinator | None = None,
) -> FastAPI:
    """Create the Pyruns FastAPI app."""
    get_follow_shell_runtime()
    app = FastAPI(title="Pyruns API", version=__version__)
    app.state.runtime = runtime or PyrunsRuntime()
    reload_token = os.getenv(_UI_TOKEN_ENV) if access_token is None else None
    app.state.access_token = str(access_token or reload_token or secrets.token_urlsafe(32))
    cookie_nonce = session_cookie_nonce
    if cookie_nonce is None and reload_token:
        cookie_nonce = os.getenv(_UI_COOKIE_NONCE_ENV)
    app.state.session_cookie_name = _session_cookie_name(cookie_nonce)
    app.state.allow_test_client_bypass = bool(allow_test_client_bypass)
    app.state.instance_id = secrets.token_urlsafe(16)
    app.state.update_coordinator = update_coordinator
    app.state.update_result = read_update_result()

    @app.middleware("http")
    async def protect_local_server(request: Request, call_next):
        def protect_response(response: Response) -> Response:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
                "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self' data:; connect-src 'self' ws: wss:"
            )
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            if request.url.path == "/api" or request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        def json_error(status_code: int, detail: str) -> Response:
            return protect_response(JSONResponse({"detail": detail}, status_code=status_code))

        scheme = str(request.url.scheme or "http").lower()
        host = request.headers.get("host", "")
        allow_test_host = request.client is not None and request.client.host == "testclient"
        if not _is_allowed_local_host(host, scheme=scheme, allow_test_host=allow_test_host):
            return json_error(403, "Forbidden host")
        origin = request.headers.get("origin")
        if origin and not _origin_matches_request(
            origin,
            host,
            scheme=scheme,
            allow_test_host=allow_test_host,
        ):
            return json_error(403, "Forbidden origin")

        bootstrap_token = request.query_params.get("token")
        if bootstrap_token is not None and not request.url.path.startswith("/api"):
            if request.method != "GET" or not _token_matches(
                bootstrap_token,
                app.state.access_token,
            ):
                return json_error(401, "Invalid UI access token")
            response = RedirectResponse(_clean_bootstrap_target(request), status_code=303)
            response.set_cookie(
                app.state.session_cookie_name,
                app.state.access_token,
                httponly=True,
                samesite="strict",
                secure=scheme == "https",
                path="/",
            )
            return protect_response(response)

        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        test_client_bypass = (
            app.state.allow_test_client_bypass
            and request.client is not None
            and request.client.host == "testclient"
        )
        session_token = request.cookies.get(app.state.session_cookie_name, "")
        if is_api and not test_client_bypass and not _token_matches(
            session_token,
            app.state.access_token,
        ):
            response = json_error(401, "UI authentication required")
            response.headers["WWW-Authenticate"] = "PyrunsToken"
            return response

        if request.method in {"POST", "PUT", "PATCH"}:
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    return json_error(400, "Invalid Content-Length")
                if content_length < 0:
                    return json_error(400, "Invalid Content-Length")
                if content_length > MAX_API_REQUEST_BYTES:
                    return json_error(413, f"Request body exceeds {MAX_API_REQUEST_BYTES} bytes")

            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_API_REQUEST_BYTES:
                    return json_error(413, f"Request body exceeds {MAX_API_REQUEST_BYTES} bytes")
            request._body = bytes(body)
        return protect_response(await call_next(request))

    dist_dir = _frontend_dist_dir()

    logger.info(f"Frontend dist directory: {dist_dir}")

    if dist_dir is not None:
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    def get_runtime() -> PyrunsRuntime:
        return app.state.runtime

    def task_start_guard():
        coordinator = app.state.update_coordinator
        return coordinator.task_start_guard() if coordinator is not None else nullcontext()

    def require_item_limit(items: list[Any], *, label: str) -> None:
        if len(items) > MAX_TASK_BATCH_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"{label} accepts at most {MAX_TASK_BATCH_ITEMS} items",
            )

    def require_environment_limit(values: dict[str, Any] | None) -> None:
        if values is not None and len(values) > MAX_ENVIRONMENT_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"Environment accepts at most {MAX_ENVIRONMENT_ITEMS} variables",
            )

    def websocket_rejection(websocket: WebSocket) -> tuple[int, str] | None:
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        allow_test_host = websocket.client is not None and websocket.client.host == "testclient"
        if not _is_allowed_local_host(host, scheme="http", allow_test_host=allow_test_host) or (
            origin
            and not _origin_matches_request(
                origin,
                host,
                scheme="http",
                allow_test_host=allow_test_host,
            )
        ):
            return 4403, "Forbidden origin"
        test_client_bypass = app.state.allow_test_client_bypass and allow_test_host
        session_token = websocket.cookies.get(app.state.session_cookie_name, "")
        if not test_client_bypass and not _token_matches(
            session_token,
            app.state.access_token,
        ):
            return 4401, "UI authentication required"
        return None

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
            require_environment_limit(data.get("global_env"))
            return get_runtime().update_runtime_settings(data, refresh_providers=refresh_providers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/templates")
    def get_templates() -> dict[str, Any]:
        return {"items": get_runtime().list_templates()}

    @app.get("/api/templates/content")
    def get_template_content(value: str = Query(min_length=1, max_length=MAX_PATH_CHARS)) -> dict[str, Any]:
        try:
            return get_runtime().get_template_content(value)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    def get_dashboard(
        refresh: bool = True,
        recent_limit: int = Query(default=6, ge=1, le=50),
    ) -> dict[str, Any]:
        return get_runtime().get_dashboard(refresh=refresh, recent_limit=recent_limit)

    @app.get("/api/launcher/scripts")
    def get_launcher_scripts() -> dict[str, Any]:
        return {"items": get_runtime().list_launcher_scripts()}

    @app.get("/api/launcher/configs")
    def get_launcher_configs(script: str = Query(min_length=1, max_length=MAX_PATH_CHARS)) -> dict[str, Any]:
        try:
            return get_runtime().get_launcher_config_info(script)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/launcher/workspaces")
    def get_launcher_workspaces(
        script: str = Query(min_length=1, max_length=MAX_PATH_CHARS),
        config: str | None = Query(default=None, max_length=MAX_PATH_CHARS),
    ) -> dict[str, Any]:
        try:
            return {"items": get_runtime().list_launcher_workspaces(script, config)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/launcher/validate-path")
    def validate_launcher_path(
        kind: str = Query(min_length=1, max_length=32),
        path: str = Query(min_length=1, max_length=MAX_PATH_CHARS),
        script: str | None = Query(default=None, max_length=MAX_PATH_CHARS),
    ) -> dict[str, Any]:
        return get_runtime().validate_launcher_path(kind, path, script)

    @app.post("/api/launcher/open")
    def open_launcher_workspace(payload: LauncherOpenRequest) -> dict[str, Any]:
        try:
            return get_runtime().open_launcher_workspace(
                payload.script_path,
                payload.config_path,
            )
        except (FileNotFoundError, ValueError) as exc:
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
        query: str = Query(default="", max_length=MAX_QUERY_CHARS),
        status: str = Query(default="All", max_length=32),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=MAX_TASK_PAGE_SIZE),
        refresh: bool = True,
        summary: bool = False,
        compact: bool = False,
        sort: Literal[
            "priority",
            "manual",
            "activity_desc",
            "activity_asc",
            "name_asc",
            "name_desc",
        ] = "priority",
    ) -> dict[str, Any]:
        page = get_runtime().list_tasks(
            query=query,
            status=status,
            offset=offset,
            limit=limit,
            refresh=refresh,
            summary=summary,
            sort_mode=sort,
        )
        items = [_compact_monitor_task(item) for item in page.items] if compact else page.items
        return {
            "items": items,
            "total": page.total,
            "offset": page.offset,
            "limit": page.limit,
            "has_more": page.has_more,
            "status_counts": page.status_counts,
        }

    @app.post("/api/tasks/reorder")
    def reorder_tasks(payload: TaskReorderRequest) -> dict[str, Any]:
        try:
            require_item_limit(payload.items, label="Task reorder")
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
            require_item_limit(payload.task_names, label="Batch run")
            with task_start_guard():
                return get_runtime().start_tasks_batch(
                    payload.task_names,
                    max_workers=payload.max_workers,
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except UpdateInProgressError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/batch/delete")
    def delete_tasks_batch(payload: TaskBatchDeleteRequest) -> dict[str, Any]:
        try:
            require_item_limit(payload.task_names, label="Batch delete")
            return get_runtime().delete_tasks_batch(payload.task_names)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/export/csv")
    def export_tasks_csv(payload: TaskBatchDeleteRequest) -> Response:
        try:
            require_item_limit(payload.task_names, label="Task export")
            csv_text = get_runtime().export_tasks_csv(payload.task_names)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=csv_text, media_type="text/csv; charset=utf-8")

    @app.post("/api/tasks/{task_name}/run")
    def run_task(task_name: str, _payload: TaskRunRequest | None = None) -> dict[str, Any]:
        try:
            with task_start_guard():
                task = get_runtime().start_task(task_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except UpdateInProgressError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
            task = get_runtime().update_task_notes(
                task_name,
                payload.notes,
                payload.expected_notes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except TaskNotesConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task": task}

    @app.patch("/api/tasks/{task_name}/env")
    def update_task_env(task_name: str, payload: TaskEnvRequest) -> dict[str, Any]:
        try:
            require_environment_limit(payload.env)
            require_environment_limit(payload.expected_env)
            task = get_runtime().update_task_env(
                task_name,
                payload.env,
                payload.expected_env,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc
        except TaskEnvConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        log_file_name: str | None = Query(default=None, max_length=255),
        offset: int | None = Query(default=None, ge=0),
        log_identity: str | None = Query(default=None, max_length=512),
        tail_bytes: int | None = Query(default=None, ge=1, le=MAX_LOG_RESPONSE_BYTES),
        tail_lines: int | None = Query(default=None, ge=1, le=MAX_LOG_TAIL_LINES),
        chunk_size: int | None = Query(default=None, ge=1, le=MAX_LOG_RESPONSE_BYTES),
    ) -> dict[str, Any]:
        try:
            return get_runtime().get_task_logs(
                task_name,
                log_file_name=log_file_name,
                offset=offset,
                log_identity=log_identity,
                tail_bytes=tail_bytes,
                tail_lines=tail_lines,
                chunk_size=chunk_size,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found") from exc

    @app.websocket("/api/tasks/events")
    async def stream_task_events(websocket: WebSocket) -> None:
        """Push task-list invalidations and keep low-frequency polling as a fallback."""
        rejection = websocket_rejection(websocket)
        if rejection is not None:
            await websocket.close(code=rejection[0], reason=rejection[1])
            return

        runtime = get_runtime()
        await websocket.accept()
        stream_root, stream_manager = runtime.get_task_event_stream_context()

        loop = asyncio.get_running_loop()
        changes: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        disconnected = asyncio.Event()

        def on_change() -> None:
            def enqueue() -> None:
                if not disconnected.is_set() and changes.empty():
                    changes.put_nowait(None)

            try:
                loop.call_soon_threadsafe(enqueue)
            except RuntimeError:
                pass

        async def send_events() -> None:
            revision = 0
            await websocket.send_json({"type": "ready", "revision": revision})
            while not disconnected.is_set():
                try:
                    await asyncio.wait_for(changes.get(), timeout=TASK_EVENT_HEARTBEAT_SEC)
                    revision += 1
                    payload = {"type": "changed", "revision": revision}
                except asyncio.TimeoutError:
                    payload = {"type": "heartbeat", "revision": revision}

                if not runtime.workspace_stream_is_current(stream_root, stream_manager):
                    await websocket.close(code=4409, reason="Workspace changed")
                    disconnected.set()
                    return
                await websocket.send_json(payload)

        async def receive_client_messages() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                disconnected.set()

        stream_manager.on_change(on_change)
        sender = asyncio.create_task(send_events())
        receiver = asyncio.create_task(receive_client_messages())
        try:
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done | pending:
                try:
                    await task
                except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    pass
        finally:
            disconnected.set()
            stream_manager.off_change(on_change)
            runtime.release_task_event_stream_context(stream_root, stream_manager)
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                pass

    @app.websocket("/api/tasks/{task_name}/logs/stream")
    async def stream_task_logs(
        websocket: WebSocket,
        task_name: str,
        log_file_name: str | None = Query(default=None, max_length=255),
        offset: int | None = Query(default=None, ge=0),
        log_identity: str | None = Query(default=None, max_length=512),
    ) -> None:
        rejection = websocket_rejection(websocket)
        if rejection is not None:
            await websocket.close(code=rejection[0], reason=rejection[1])
            return

        runtime = get_runtime()
        try:
            stream_root, stream_task = runtime.get_task_log_stream_context(task_name)
        except KeyError:
            await websocket.close(code=4404, reason="Task not found")
            return
        stream_task_dir = os.path.normcase(os.path.abspath(str(stream_task["dir"])))

        requested_log_name = str(log_file_name or "").strip()
        requested_offset = None if offset is None else max(0, int(offset))
        requested_identity = str(log_identity or "").strip()
        await websocket.accept()
        loop = asyncio.get_running_loop()
        log_emitter.bind_loop(loop)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=LOG_STREAM_QUEUE_LIMIT)
        disconnected = asyncio.Event()
        dropped_notice_sent = False
        stream_log_name = ""
        stream_offset = 0
        stream_identity = ""
        stream_offsets: dict[str, int] = {}
        stream_initialized = False
        last_emitter_chunk_at = 0.0

        def enqueue_message(message: dict[str, Any]) -> None:
            nonlocal dropped_notice_sent
            try:
                queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            if not dropped_notice_sent:
                message = {**message, "content": LOG_STREAM_DROPPED_NOTICE + str(message.get("content") or "")}
                dropped_notice_sent = True
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.debug("Dropping live log chunk for %s because websocket queue is full", task_name)

        def on_chunk(chunk_text: str, metadata: dict[str, Any] | None = None) -> None:
            nonlocal last_emitter_chunk_at, stream_log_name, stream_offset, stream_identity
            if disconnected.is_set():
                return
            last_emitter_chunk_at = time.monotonic()
            chunk_log_name = str((metadata or {}).get("log_file_name") or stream_log_name or "")
            if chunk_log_name and stream_log_name == QUEUE_LOG_FILENAME and chunk_log_name != stream_log_name:
                stream_log_name = chunk_log_name
                stream_offset = stream_offsets.get(chunk_log_name, 0)
                stream_identity = ""
            message = {
                "type": "chunk",
                "task_name": task_name,
                "content": chunk_text,
            }
            if chunk_log_name:
                message["log_file_name"] = chunk_log_name
            if stream_identity:
                message["log_identity"] = stream_identity
            offset = (metadata or {}).get("offset")
            if offset is not None:
                try:
                    chunk_offset = max(0, int(offset))
                except (TypeError, ValueError):
                    chunk_offset = None
                if chunk_offset is not None:
                    previous_offset = stream_offsets.get(chunk_log_name, stream_offset)
                    if chunk_offset <= previous_offset:
                        return
                    stream_offsets[chunk_log_name] = chunk_offset
                    if not chunk_log_name or chunk_log_name == stream_log_name:
                        stream_offset = chunk_offset
                    message["offset"] = chunk_offset
            enqueue_message(message)

        async def tail_log_file() -> None:
            nonlocal stream_initialized, stream_log_name, stream_offset, stream_identity
            while not disconnected.is_set():
                try:
                    if not runtime.workspace_stream_is_current(stream_root):
                        disconnected.set()
                        break
                    if not stream_initialized:
                        if requested_offset is None:
                            payload = await asyncio.to_thread(
                                runtime.get_task_logs,
                                task_name,
                                log_file_name=requested_log_name or None,
                                tail_lines=0,
                                expected_workspace_root=stream_root,
                                expected_task_dir=stream_task_dir,
                            )
                        else:
                            payload = await asyncio.to_thread(
                                runtime.get_task_logs,
                                task_name,
                                log_file_name=requested_log_name or None,
                                offset=requested_offset,
                                log_identity=requested_identity or None,
                                chunk_size=LOG_STREAM_TAIL_CHUNK_SIZE,
                                expected_workspace_root=stream_root,
                                expected_task_dir=stream_task_dir,
                            )
                        stream_log_name = str(payload.get("selected_log") or "")
                        stream_offset = max(0, int(payload.get("offset") or 0))
                        stream_identity = str(payload.get("log_identity") or "")
                        if stream_log_name:
                            stream_offsets[stream_log_name] = stream_offset
                        content = str(payload.get("content") or "")
                        if requested_offset is not None and (content or payload.get("reset")):
                            enqueue_message({
                                "type": "reset" if payload.get("reset") else "chunk",
                                "task_name": task_name,
                                "content": content,
                                "offset": stream_offset,
                                "log_file_name": stream_log_name,
                                "log_identity": stream_identity,
                            })
                        stream_initialized = True
                    elif stream_log_name:
                        stream_path = os.path.join(stream_task_dir, RUN_LOGS_DIR, stream_log_name)
                        current_identity = log_file_identity(stream_path)
                        try:
                            current_size = os.path.getsize(stream_path)
                        except OSError:
                            current_size = 0
                        identity_changed = bool(
                            stream_identity
                            and current_identity
                            and current_identity != stream_identity
                        )
                        if identity_changed or current_size < stream_offset:
                            payload = await asyncio.to_thread(
                                runtime.get_task_logs,
                                task_name,
                                log_file_name=stream_log_name,
                                offset=stream_offset,
                                log_identity=stream_identity or None,
                                chunk_size=LOG_STREAM_TAIL_CHUNK_SIZE,
                                expected_workspace_root=stream_root,
                                expected_task_dir=stream_task_dir,
                            )
                            stream_offset = max(0, int(payload.get("offset") or 0))
                            stream_identity = str(payload.get("log_identity") or current_identity or "")
                            stream_offsets[stream_log_name] = stream_offset
                            enqueue_message({
                                "type": "reset",
                                "task_name": task_name,
                                "content": str(payload.get("content") or ""),
                                "offset": stream_offset,
                                "log_file_name": stream_log_name,
                                "log_identity": stream_identity,
                            })
                            continue
                        if not stream_identity and current_identity:
                            stream_identity = current_identity
                        emitter_quiet = time.monotonic() - last_emitter_chunk_at >= LOG_STREAM_EMITTER_QUIET_SEC
                        if emitter_quiet:
                            switched_log = False
                            if stream_log_name == QUEUE_LOG_FILENAME:
                                queue_payload = await asyncio.to_thread(
                                    runtime.get_task_logs,
                                    task_name,
                                    tail_lines=0,
                                    expected_workspace_root=stream_root,
                                    expected_task_dir=stream_task_dir,
                                )
                                selected_log = str(queue_payload.get("selected_log") or stream_log_name)
                                if selected_log != stream_log_name:
                                    stream_log_name = selected_log
                                    stream_offset = stream_offsets.get(selected_log, 0)
                                    stream_identity = ""
                                    switched_log = True
                            if not switched_log:
                                read_offset = stream_offset
                                payload = await asyncio.to_thread(
                                    runtime.get_task_logs,
                                    task_name,
                                    log_file_name=stream_log_name,
                                    offset=read_offset,
                                    log_identity=stream_identity or None,
                                    chunk_size=LOG_STREAM_TAIL_CHUNK_SIZE,
                                    expected_workspace_root=stream_root,
                                    expected_task_dir=stream_task_dir,
                                )
                                selected_log = str(payload.get("selected_log") or stream_log_name)
                                new_offset = max(0, int(payload.get("offset") or read_offset))
                                new_identity = str(payload.get("log_identity") or stream_identity or "")
                                if selected_log != stream_log_name:
                                    stream_log_name = selected_log
                                    stream_offset = new_offset
                                    stream_identity = new_identity
                                    stream_offsets[selected_log] = new_offset
                                elif payload.get("reset"):
                                    stream_offset = new_offset
                                    stream_identity = new_identity
                                    stream_offsets[stream_log_name] = new_offset
                                    enqueue_message({
                                        "type": "reset",
                                        "task_name": task_name,
                                        "content": str(payload.get("content") or ""),
                                        "offset": new_offset,
                                        "log_file_name": stream_log_name,
                                        "log_identity": stream_identity,
                                    })
                                elif new_offset > stream_offset:
                                    content = str(payload.get("content") or "")
                                    stream_offset = new_offset
                                    stream_identity = new_identity
                                    stream_offsets[stream_log_name] = new_offset
                                    if content:
                                        enqueue_message({
                                            "type": "chunk",
                                            "task_name": task_name,
                                            "content": content,
                                            "offset": new_offset,
                                            "log_file_name": stream_log_name,
                                            "log_identity": stream_identity,
                                        })
                except WorkspaceChangedError:
                    disconnected.set()
                    break
                except Exception as exc:
                    logger.debug("Log file tail fallback failed for %s: %s", task_name, exc)

                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=LOG_STREAM_TAIL_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass

        async def watch_client_messages() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                disconnected.set()

        watcher = asyncio.create_task(watch_client_messages())
        tailer = asyncio.create_task(tail_log_file())
        log_emitter.subscribe(
            task_name,
            on_chunk,
            loop=loop,
            include_metadata=True,
            task_dir=stream_task_dir,
        )
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
            tailer.cancel()
            log_emitter.unsubscribe(task_name, on_chunk)
            for task in (watcher, tailer):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                pass

    @app.get("/api/system/info")
    def get_system_info() -> dict[str, Any]:
        coordinator = app.state.update_coordinator
        return {
            "version": __version__,
            "instance_id": app.state.instance_id,
            "update_supported": coordinator is not None,
            "update_state": coordinator.state if coordinator is not None else "unavailable",
            "last_update": app.state.update_result,
        }

    @app.get("/api/system/update/check")
    def check_pyruns_update() -> dict[str, Any]:
        if app.state.update_coordinator is None:
            raise HTTPException(
                status_code=503,
                detail="Update checks are available only in the normal 'pyr ui' server.",
            )
        try:
            return check_latest_version(__version__)
        except LatestVersionCheckError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/system/update", status_code=202)
    def update_pyruns(background_tasks: BackgroundTasks) -> dict[str, Any]:
        coordinator = app.state.update_coordinator
        if coordinator is None:
            raise HTTPException(
                status_code=503,
                detail="Full-process updates are available only in the normal 'pyr ui' server.",
            )
        try:
            coordinator.prepare(get_runtime())
        except ActiveTasksError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UpdateCheckError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        background_tasks.add_task(coordinator.trigger_shutdown)
        return {
            "ok": True,
            "instance_id": app.state.instance_id,
            "version": __version__,
            "state": coordinator.state,
        }

    @app.get("/api/system/metrics")
    def get_metrics(include_processes: bool = False) -> dict[str, Any]:
        return get_runtime().get_metrics(include_processes=include_processes)

    if dist_dir is not None:

        @app.get("/{full_path:path}")
        def serve_frontend(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")

            requested: Path | None = None
            inside_static = False
            try:
                requested = (dist_dir / full_path).resolve()
                inside_static = os.path.commonpath([str(requested), str(dist_dir)]) == str(dist_dir)
            except (OSError, ValueError):
                inside_static = False

            if (
                full_path
                and inside_static
                and requested is not None
                and requested.exists()
                and requested.is_file()
            ):
                return FileResponse(requested)
            return FileResponse(
                dist_dir / "index.html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

    else:

        @app.get("/{full_path:path}")
        def serve_frontend_fallback(full_path: str) -> HTMLResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return HTMLResponse(_fallback_frontend_html())

    return app


def _request_server_shutdown() -> None:
    """Deliver the same graceful stop signal used by Ctrl+C."""

    signal.raise_signal(signal.SIGINT)


def main(
    *,
    reload: bool = False,
    open_browser: bool | None = None,
    start_path: str = "/",
    port: int | None = None,
    access_token: str | None = None,
) -> None:
    """Launch the unified Pyruns API and frontend server."""
    runtime = PyrunsRuntime()
    token = str(access_token or secrets.token_urlsafe(32))
    update_coordinator = None if reload else UiUpdateCoordinator(_request_server_shutdown)
    previous_token = os.environ.get(_UI_TOKEN_ENV) if reload else None
    previous_cookie_nonce = os.environ.get(_UI_COOKIE_NONCE_ENV) if reload else None
    if reload:
        # Uvicorn's reload child creates the app from an import string and must
        # receive the token out of process. Normal single-process startup passes
        # it directly and must not expose it through the server environment.
        os.environ[_UI_TOKEN_ENV] = token
    try:
        host = "127.0.0.1"
        explicit_port = port is not None
        configured_port = int(port if explicit_port else runtime.settings.get("ui_port", DEFAULT_UI_PORT))
        try:
            port = (
                find_available_port(configured_port, host=host, max_attempts=0)
                if explicit_port
                else find_available_port(configured_port, host=host)
            )
        except RuntimeError as exc:
            if explicit_port:
                raise RuntimeError(
                    f"UI port {configured_port} is already in use; choose another with --port"
                ) from exc
            raise
        if port != configured_port:
            print(
                f"[pyruns] Port {configured_port} is busy; using {port} instead.",
                flush=True,
            )
        cookie_nonce = _session_cookie_nonce_for_port(port)
        if reload:
            os.environ[_UI_COOKIE_NONCE_ENV] = cookie_nonce
        url = f"http://{host}:{port}{start_path}"
        authenticated_url = _url_with_access_token(url, token)
        print(f"[pyruns] UI: {authenticated_url}", flush=True)
        should_open_browser = (
            not reload and _can_open_browser_from_environment()
            if open_browser is None
            else open_browser
        )
        if should_open_browser:
            _schedule_browser_open(authenticated_url)
        else:
            print(
                "[pyruns] Browser auto-open disabled; open the URL manually.",
                flush=True,
            )
        uvicorn.run(
            "pyruns.web.app:create_app"
            if reload
            else create_app(
                runtime,
                access_token=token,
                session_cookie_nonce=cookie_nonce,
                update_coordinator=update_coordinator,
            ),
            host=host,
            port=port,
            reload=reload,
            factory=reload,
            proxy_headers=False,
            access_log=False,
            log_level="warning",
        )
    finally:
        if reload:
            if previous_token is None:
                os.environ.pop(_UI_TOKEN_ENV, None)
            else:
                os.environ[_UI_TOKEN_ENV] = previous_token
            if previous_cookie_nonce is None:
                os.environ.pop(_UI_COOKIE_NONCE_ENV, None)
            else:
                os.environ[_UI_COOKIE_NONCE_ENV] = previous_cookie_nonce
        shutdown = getattr(runtime, "shutdown", None)
        if callable(shutdown):
            shutdown()
    if update_coordinator is not None and update_coordinator.requested:
        replace_process_with_updater(
            port=int(port),
            token=token,
            previous_version=__version__,
        )


def _parse_main_options(args: list[str]) -> tuple[int | None, bool | None]:
    """Parse UI launch options for ``python -m pyruns.web.app``."""

    parser = argparse.ArgumentParser(prog="python -m pyruns.web.app")
    parser.add_argument("-p", "--port", type=_parse_port_value)
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--browser", dest="open_browser", action="store_true")
    browser.add_argument("--no-browser", dest="open_browser", action="store_false")
    parser.set_defaults(open_browser=None)
    options = parser.parse_args(args)
    return options.port, options.open_browser


def _parse_port_value(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid port: {raw}") from exc
    if value < 1 or value > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


if __name__ == "__main__":
    main_port, main_open_browser = _parse_main_options(sys.argv[1:])
    production_restart = _env_truthy(UI_PRODUCTION_RESTART_ENV)
    restart_token = os.environ.pop(_UI_TOKEN_ENV, None) if production_restart else None
    if production_restart:
        os.environ.pop(UI_PRODUCTION_RESTART_ENV, None)
    try:
        main(
            reload=not production_restart,
            port=main_port,
            open_browser=main_open_browser,
            access_token=restart_token,
        )
    except RuntimeError as exc:
        print(f"pyruns: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
