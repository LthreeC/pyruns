from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_python_runtime_dependencies_do_not_include_legacy_nicegui():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    assert "nicegui" not in pyproject
    assert 'exclude = ["pyruns.ui*"]' in pyproject
    assert "pydantic" in pyproject


def test_python_runtime_dependencies_include_websocket_server_support():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    assert (
        '"websockets>=12,<16"' in pyproject
        or '"wsproto>=' in pyproject
        or '"uvicorn[standard]>=' in pyproject
    )


def test_ci_installs_declared_web_test_dependencies():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")

    assert "[project.optional-dependencies]" in pyproject
    assert "test = [" in pyproject
    assert "lint = [" in pyproject
    assert '"httpx>=' in pyproject
    assert '"flake8>=' in pyproject
    assert 'pip install -e ".[test,lint]"' in workflow


def test_python_version_metadata_matches_modern_type_syntax():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in pyproject
    assert "Programming Language :: Python :: 3.8" not in pyproject
    assert "Programming Language :: Python :: 3.9" not in pyproject


def test_flake8_ignores_local_editor_history():
    config = (ROOT / ".flake8").read_text(encoding="utf-8")

    assert ".history" in config


def test_pages_workflow_uploads_docs_vitepress_dist():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

    assert "npm run docs:build" in workflow
    assert "path: docs/.vitepress/dist" in workflow


def test_frontend_dependencies_do_not_include_unused_editor_or_terminal_addons():
    package_json = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    package_lock = (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")

    assert "@codemirror/lang-json" not in package_json
    assert "@codemirror/lang-json" not in package_lock
    assert "@xterm/addon-web-links" not in package_json
    assert "@xterm/addon-web-links" not in package_lock


def test_frontend_dependencies_include_terminal_search_addon():
    package_json = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    package_lock = (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")

    assert '"@xterm/addon-search"' in package_json
    assert 'node_modules/@xterm/addon-search' in package_lock
