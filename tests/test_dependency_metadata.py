from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_python_runtime_dependencies_do_not_include_legacy_nicegui():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    assert "nicegui" not in pyproject
    assert 'exclude = ["pyruns.ui*"]' in pyproject
    assert "pydantic" in pyproject


def test_ci_installs_declared_web_test_dependencies():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")

    assert "[project.optional-dependencies]" in pyproject
    assert "test = [" in pyproject
    assert "lint = [" in pyproject
    assert '"httpx>=' in pyproject
    assert '"flake8>=' in pyproject
    assert 'pip install -e ".[test,lint]"' in workflow


def test_frontend_dependencies_do_not_include_unused_editor_or_terminal_addons():
    package_json = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    package_lock = (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")

    assert "@codemirror/lang-json" not in package_json
    assert "@codemirror/lang-json" not in package_lock
    assert "@xterm/addon-web-links" not in package_json
    assert "@xterm/addon-web-links" not in package_lock
