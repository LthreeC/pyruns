"""Compare public results and error text before measuring the prototype."""

import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from pyruns.utils import config_utils, info_io, task_files
from validation_stat_prototype import install

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
checks = []


def outcome(function):
    try:
        value = function()
        if isinstance(value, tuple):
            kind, config, text, error = value
            value = [kind, config_utils.to_container(config, resolve=False), text, error]
        return {"value": value}
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def compare(label, call):
    install(False)
    before = outcome(call)
    install(True)
    after = outcome(call)
    checks.append({"label": label, "before": before, "after": after})
    assert before == after, checks[-1]


try:
    with tempfile.TemporaryDirectory(prefix="pyruns-validation-stat-") as directory:
        base = Path(directory)
        workspace = base / "_pyruns_" / "中文 workspace"
        task = workspace / "tasks" / "sample"
        task.mkdir(parents=True)
        payload = task / "config.yaml"
        payload.write_text("value: 1\n", encoding="utf-8")
        outside = base / "outside"
        outside.mkdir()
        foreign = outside / "data.yaml"
        foreign.write_text("value: outside\n", encoding="utf-8")
        for path in (task, payload, task / "missing", payload / "child", str(task / "nul") + "\0"):
            compare("file " + str(path), lambda: info_io.validate_workspace_file(str(path), str(task), label="Task payload"))
            compare("root " + str(path), lambda: info_io.validate_tasks_root(str(path)))
        for target, is_dir in ((outside, True), (base / "missing-directory", True),
                               (foreign, False), (outside / "missing-file", False)):
            alias = task / "alias"
            alias.symlink_to(target, target_is_directory=is_dir)
            try:
                compare("linked file " + str(target), lambda: info_io.validate_workspace_file(str(alias), str(task), label="Task payload"))
                compare("linked root " + str(target), lambda: info_io.validate_tasks_root(str(alias)))
                compare("linked payload " + str(target), lambda: task_files.read_task_payload(str(task), {"config_file": "alias"}))
            finally:
                alias.unlink()
        for raw in (b"value: 1\n", b"", b"[1, 2]", b"value: [broken", b"value: ${base}", b"bad: \xff"):
            payload.write_bytes(raw)
            compare("payload " + repr(raw), lambda: task_files.read_task_payload(str(task), {"config_file": "config.yaml"}))
        if os.name == "nt":
            import _winapi

            alias = task / "junction"
            _winapi.CreateJunction(str(outside), str(alias))
            try:
                compare("junction root", lambda: info_io.validate_tasks_root(str(alias)))
                compare("junction file", lambda: info_io.validate_workspace_file(str(alias / "data.yaml"), str(task), label="Task payload"))
            finally:
                os.rmdir(alias)
            extended = "\\\\?\\" + str(task)
            for suffix in ("literal.", "literal "):
                special = extended + "\\" + suffix
                os.mkdir(special)
                try:
                    with open(special + "\\value.yaml", "w") as handle:
                        handle.write("value: special\n")
                    compare("literal file " + suffix, lambda: info_io.validate_workspace_file(special + "\\value.yaml", special, label="Task payload"))
                    compare("missing boundary " + suffix, lambda: info_io.validate_workspace_file(special + "\\value.yaml", str(task / "literal"), label="Task payload"))
                finally:
                    os.unlink(special + "\\value.yaml")
                    os.rmdir(special)
    install(True)
    import pytest

    result = pytest.main([
        "tests/test_workspace_path_safety.py", "tests/test_utils.py", "tests/test_core.py",
        "tests/test_cli.py", "tests/test_transactional_creation.py", "tests/test_lock_queue.py",
        "-q", "-k", "reparse or symlink or junction or workspace_file_boundary or rechecks or task_payload_keeps or task_payload_resolves",
        "--junitxml=" + str(output / "paths.xml"),
    ])
    assert result == 0, result
finally:
    install(False)
    (output / "contracts.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"checks": len(checks), "equivalent": all(x["before"] == x["after"] for x in checks)}))
