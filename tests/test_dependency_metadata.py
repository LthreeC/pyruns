from html.parser import HTMLParser
import json
from pathlib import Path

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
