"""Compare workspace validation and public payload results for a stat prototype."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import subprocess
from unittest.mock import patch

ROOT = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))
from pyruns.utils import config_utils, info_io, task_files
from workspace_stat_prototype import validate_workspace_directory as candidate

original = info_io.validate_workspace_directory
if "--product" in sys.argv:
    candidate = original
    before = subprocess.check_output(
        ["git", "show", "1eaff82a2def3ff6fd70fc1b8fb54bb164f2d19b:pyruns/utils/info_io.py"], cwd=ROOT,
    )
    baseline = types.ModuleType("pyruns_workspace_stat_before")
    exec(compile(before, "1eaff82/pyruns/utils/info_io.py", "exec"), baseline.__dict__)
    original = baseline.validate_workspace_directory
checks = []
skipped = []


def capture(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
        if isinstance(result, tuple):
            kind, config, text, error = result
            result = {"kind": kind, "config_type": type(config).__name__,
                      "config": config_utils.to_container(config, resolve=False), "text": text, "error": error}
        return {"result": result}
    except Exception as exc:
        return {"exception": type(exc).__name__, "message": str(exc)}


def compare(label, function, *args, **kwargs):
    info_io.validate_workspace_directory = original
    before = capture(function, *args, **kwargs)
    info_io.validate_workspace_directory = candidate
    after = capture(function, *args, **kwargs)
    assert before == after, (label, before, after)
    checks.append({"label": label, "equal": True, "error": before.get("exception")})


def directory(path):
    info_io.validate_workspace_directory(str(path))


try:
    with tempfile.TemporaryDirectory(prefix="pyruns-workspace-stat-contract-") as temporary:
        base = Path(temporary)
        root = base / "_pyruns_" / "中文 workspace"
        task = root / "tasks" / "sample"
        task.mkdir(parents=True)
        (root / "plain-file").write_text("file", encoding="utf-8")
        for path in (base, root, task, root / "plain-file", root / "missing", root / "missing" / "child",
                     root / "plain-file" / "child", str(base / "bad") + "\0", str(root / "bad") + "\0"):
            compare("directory " + repr(str(path.relative_to(base) if isinstance(path, Path) else path)), directory, path)
        outside = base / "outside"
        outside.mkdir()
        for target in (outside, base / "missing-target", root / "plain-file"):
            link = root / "alias"
            link.mkdir()
            compare("directory before link " + target.name, directory, link)
            link.rmdir()
            try:
                link.symlink_to(target, target_is_directory=target != root / "plain-file")
            except OSError as exc:
                skipped.append({"case": "symlink " + target.name, "reason": str(exc)})
                continue
            try:
                compare("managed link " + target.name, directory, link)
                compare("managed linked ancestor " + target.name, directory, link / "child")
            finally:
                link.unlink()
            link.mkdir()
            compare("directory after link " + target.name, directory, link)
            link.rmdir()
        if os.name == "nt":
            import _winapi
            junction = root / "junction"
            junction.mkdir()
            compare("directory before junction", directory, junction)
            junction.rmdir()
            _winapi.CreateJunction(str(outside), str(junction))
            try:
                compare("junction", directory, junction)
                compare("junction ancestor", directory, junction / "child")
            finally:
                os.rmdir(junction)
            junction.mkdir()
            compare("directory after junction", directory, junction)
            extended = "\\\\?\\" + str(root)
            compare("extended directory", directory, extended)
            for name in ("trailing.", "trailing "):
                special = extended + "\\" + name
                os.mkdir(special)
                try:
                    compare("literal " + repr(name), directory, special)
                    compare("missing plain versus " + repr(name), directory, root / "trailing")
                finally:
                    os.rmdir(special)

        documents = [b"value: 7\n", b"", b"null", b"[1, 2]", b"value: [broken", b"value: ${broken",
                     b"base: 7\nvalue: ${base}\n", b"value: ???\n", b"1: one\nfalse: no\n",
                     b"value: !!binary YQ==\n", b"value: !!set {a: null}\n",
                     b"value: !!timestamp 2026-09-25\n", b"base: &a {items: [1, 2]}\ncopy: *a\n",
                     b"bad: \xff", b"x" * (task_files.MAX_TASK_PAYLOAD_BYTES + 1)]
        for index, raw in enumerate(documents):
            (task / "config.yaml").write_bytes(raw)
            for view in (False, True):
                compare(f"payload-{index}-{view}", task_files.read_task_payload, str(task), {}, config_view=view)
        for index, raw in enumerate((b"echo hello\r\n", b"\xff", b"x" * (task_files.MAX_TASK_PAYLOAD_BYTES + 1))):
            (task / "run.sh").write_bytes(raw)
            compare(f"shell-{index}", task_files.read_task_payload, str(task), {"task_kind": "shell", "config_file": "run.sh"})
        (task / "config.yaml").unlink()
        compare("missing payload", task_files.read_task_payload, str(task), {})
        compare("escaped payload", task_files.read_task_payload, str(task), {"config_file": "../outside.yaml"})
        (task / "config.yaml").write_text("value: 7\n", encoding="utf-8")
        with patch.object(task_files, "open", side_effect=OSError("cannot read"), create=True):
            compare("open failure", task_files.read_task_payload, str(task), {}, config_view=True)
        with patch.object(task_files.os, "fstat", side_effect=OSError("no file identity")):
            compare("fstat failure", task_files.read_task_payload, str(task), {}, config_view=True)
finally:
    info_io.validate_workspace_directory = original

report = {"passed": True, "comparisons": len(checks), "checks": checks, "skipped": skipped}
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"passed": True, "comparisons": len(checks), "skipped": skipped}))
