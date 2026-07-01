from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


class _StaticRefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.refs.append(value)


def _load_pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_static_checker():
    script_path = ROOT / "scripts" / "check_wheel_static.py"
    spec = importlib.util.spec_from_file_location("check_wheel_static", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_frontend_static_checker():
    script_path = ROOT / "scripts" / "check_frontend_static.py"
    spec = importlib.util.spec_from_file_location("check_frontend_static", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    else:
        yield value


def test_python_runtime_dependencies_do_not_include_legacy_nicegui():
    pyproject = _load_pyproject()
    dependencies = {item.lower() for item in pyproject["project"]["dependencies"]}
    package_find = pyproject["tool"]["setuptools"]["packages"]["find"]

    assert not any(item.startswith("nicegui") for item in dependencies)
    assert package_find["exclude"] == ["pyruns.ui*"]
    assert any(item.startswith("pydantic") for item in dependencies)


def test_python_runtime_dependencies_include_websocket_server_support():
    dependencies = {item.lower() for item in _load_pyproject()["project"]["dependencies"]}

    assert (
        any(item.startswith("websockets>=12,<16") for item in dependencies)
        or any(item.startswith("wsproto>=") for item in dependencies)
        or any(item.startswith("uvicorn[standard]>=") for item in dependencies)
    )


def test_ci_installs_declared_web_test_dependencies():
    optional = _load_pyproject()["project"]["optional-dependencies"]
    workflow = (ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")

    assert "test" in optional
    assert "lint" in optional
    assert any(item.startswith("httpx>=") for item in optional["test"])
    assert any(item.startswith("tomli>=") for item in optional["test"])
    assert any(item.startswith("flake8>=") for item in optional["lint"])
    assert 'pip install -e ".[test,lint]"' in workflow


def test_python_version_metadata_matches_modern_type_syntax():
    project = _load_pyproject()["project"]

    assert project["requires-python"] == ">=3.10"
    assert "Programming Language :: Python :: 3.8" not in project["classifiers"]
    assert "Programming Language :: Python :: 3.9" not in project["classifiers"]


def test_flake8_ignores_local_editor_history():
    config = (ROOT / ".flake8").read_text(encoding="utf-8")

    assert ".history" in config
    assert ".venv" in config


def test_packaging_includes_static_svg_assets():
    package_data = _load_pyproject()["tool"]["setuptools"]["package-data"]["pyruns"]

    assert "*.svg" in package_data
    assert "**/*.svg" in package_data
    assert (ROOT / "pyruns" / "web" / "static" / "pyruns.svg").exists()


def test_examples_extra_declares_hydra_dependencies():
    optional = _load_pyproject()["project"]["optional-dependencies"]
    examples_readme = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")

    assert "examples" in optional
    assert any(item.startswith("hydra-core>=") for item in optional["examples"])
    assert any(item.startswith("omegaconf>=") for item in optional["examples"])
    assert 'pip install "pyruns[examples]"' in examples_readme


def test_root_package_metadata_marks_docs_workspace_private():
    package_json = _load_json(ROOT / "package.json")

    assert package_json["name"] == "pyruns-docs-workspace"
    assert package_json["private"] is True


def test_pages_workflow_uploads_docs_vitepress_dist():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

    assert "npm run docs:build" in workflow
    assert "path: docs/.vitepress/dist" in workflow


def test_frontend_dependencies_do_not_include_unused_editor_or_terminal_addons():
    package_json = _load_json(ROOT / "frontend" / "package.json")
    package_lock = _load_json(ROOT / "frontend" / "package-lock.json")
    dependencies = {
        *package_json.get("dependencies", {}),
        *package_json.get("devDependencies", {}),
        *package_lock.get("packages", {}),
    }

    assert "@codemirror/lang-json" not in dependencies
    assert "@xterm/addon-web-links" not in dependencies


def test_frontend_dependencies_include_terminal_search_addon():
    package_json = _load_json(ROOT / "frontend" / "package.json")
    package_lock = _load_json(ROOT / "frontend" / "package-lock.json")

    assert "@xterm/addon-search" in package_json["dependencies"]
    assert "node_modules/@xterm/addon-search" in package_lock["packages"]


def test_npm_lockfiles_do_not_reference_local_workspaces():
    for lock_path in [ROOT / "package-lock.json", ROOT / "frontend" / "package-lock.json"]:
        lock = _load_json(lock_path)
        for value in _walk_json(lock):
            assert not (isinstance(value, str) and value.startswith("file:..")), lock_path
            assert not (isinstance(value, str) and value.startswith("workspace:")), lock_path
            assert not (isinstance(value, dict) and value.get("link") is True), lock_path


def test_built_static_index_references_existing_assets():
    index_path = ROOT / "pyruns" / "web" / "static" / "index.html"
    parser = _StaticRefParser()
    parser.feed(index_path.read_text(encoding="utf-8"))
    refs = [ref for ref in parser.refs if ref.startswith("/assets/") or ref == "/pyruns.svg"]

    assert "/pyruns.svg" in refs
    assert any(ref.endswith(".js") for ref in refs)
    assert any(ref.endswith(".css") for ref in refs)
    for ref in refs:
        assert (ROOT / "pyruns" / "web" / "static" / ref.removeprefix("/")).exists(), ref


def test_package_workflow_cleans_build_and_checks_wheel_static_assets():
    workflow = (ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")

    assert "rm -rf build .tmp-dist" in workflow
    assert "python scripts/check_wheel_static.py .tmp-dist/*.whl" in workflow


def test_frontend_workflow_checks_committed_static_assets():
    workflow = (ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")

    assert "python scripts/check_frontend_static.py" in workflow
    assert "python scripts/check_frontend_static.py" in workflow.split("npm --prefix frontend ci", 1)[1]


def test_wheel_static_checker_rejects_stale_assets(tmp_path):
    source_static = tmp_path / "pyruns" / "web" / "static"
    source_assets = source_static / "assets"
    source_assets.mkdir(parents=True)
    (source_assets / "index-current.js").write_text("console.log('ok')\n", encoding="utf-8")
    (source_assets / "index-current.css").write_text("body{}\n", encoding="utf-8")
    (source_static / "index.html").write_text(
        '<link rel="stylesheet" href="/assets/index-current.css">'
        '<script type="module" src="/assets/index-current.js"></script>',
        encoding="utf-8",
    )

    checker = _load_static_checker()
    good_wheel = tmp_path / "good.whl"
    with zipfile.ZipFile(good_wheel, "w") as wheel:
        wheel.write(source_static / "index.html", "pyruns/web/static/index.html")
        wheel.write(source_assets / "index-current.js", "pyruns/web/static/assets/index-current.js")
        wheel.write(source_assets / "index-current.css", "pyruns/web/static/assets/index-current.css")

    assert checker.check_wheel_static(good_wheel, root=tmp_path) == []

    stale_wheel = tmp_path / "stale.whl"
    with zipfile.ZipFile(stale_wheel, "w") as wheel:
        wheel.write(source_static / "index.html", "pyruns/web/static/index.html")
        wheel.write(source_assets / "index-current.js", "pyruns/web/static/assets/index-current.js")
        wheel.write(source_assets / "index-current.css", "pyruns/web/static/assets/index-current.css")
        wheel.writestr("pyruns/web/static/assets/index-old.js", "console.log('old')\n")

    assert any("stale static assets" in error for error in checker.check_wheel_static(stale_wheel, root=tmp_path))


def test_frontend_static_checker_rejects_stale_assets(tmp_path, monkeypatch):
    frontend_dir = tmp_path / "frontend"
    static_dir = tmp_path / "pyruns" / "web" / "static"
    frontend_dir.mkdir(parents=True)
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("old\n", encoding="utf-8")

    checker = _load_frontend_static_checker()
    monkeypatch.setattr(checker.shutil, "which", lambda _name: "npm")

    def fake_run(command, cwd, text, capture_output, **_kwargs):
        build_dir = Path(command[command.index("--outDir") + 1])
        build_dir.mkdir(parents=True)
        (build_dir / "index.html").write_text("new\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)

    assert checker.check_frontend_static(root=tmp_path) == ["changed committed static asset: index.html"]


def test_frontend_log_stream_passes_resume_options_without_offset_reconnects():
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    hook_source = (ROOT / "frontend" / "src" / "hooks" / "useWebSocket.ts").read_text(encoding="utf-8")
    monitor_source = (ROOT / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx").read_text(
        encoding="utf-8"
    )

    assert "createLogStream(taskName: string, options:" in api_source
    assert "sp.set('log_file_name', options.logFileName)" in api_source
    assert "sp.set('offset', String(options.offset))" in api_source
    assert "offsetRef.current = offset" in hook_source
    assert "createLogStream(taskName, { logFileName, offset: offsetRef.current })" in hook_source
    assert "[taskName, enabled, disconnect, logFileName]" in hook_source
    assert "enabled: !loading && isLive && canUseLogStream" in monitor_source
    assert "logFileName: selectedLog || liveLogName || undefined" in monitor_source
    assert "offset: logOffsetRef.current" in monitor_source


def test_frontend_task_store_retries_last_valid_page_after_empty_page():
    store_source = (ROOT / "frontend" / "src" / "store.ts").read_text(encoding="utf-8")

    assert "page.items.length === 0" in store_source
    assert "Math.floor((page.total - 1) / limit) * limit" in store_source
    assert "retryPage = await api.getTasks" in store_source
