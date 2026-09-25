"""Task config views must preserve parsing, snapshots, and execution semantics."""

import json
import time

from omegaconf import DictConfig
import pytest
import yaml

from pyruns._config import CONFIG_FILENAME, TASK_INFO_FILENAME
from pyruns.core.task_manager import TaskManager
from pyruns.utils import config_utils
from pyruns.utils.info_io import load_task_info
from pyruns.utils.task_files import read_task_payload


@pytest.fixture(params=["python", "libyaml"])
def yaml_backend(request, monkeypatch):
    if request.param == "python":
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    elif not hasattr(yaml, "CSafeLoader"):
        pytest.skip("PyYAML was installed without libyaml")
    config_utils._get_pyruns_yaml_loader.cache_clear()
    yield
    config_utils._get_pyruns_yaml_loader.cache_clear()


@pytest.mark.parametrize("document", [
    "", "null", "{}",
    "epochs: 10\nrate: 1e-3\nlabel: 中文\nnested: {enabled: true, layers: [64, 32, null]}\n",
    "batch: 1:3:1\nleading: 012\nhex: 0xff\noctal: 0o17\nbinary: 0b101\ndate: 2026-09-25\n",
    "base: &base {x: 7, values: [1, 2]}\ncopy: *base\nmerged: {<<: *base, x: 9}\n",
    'name: "\\uD800"\n',
])
def test_plain_views_preserve_values_and_types(yaml_backend, document):
    expected = config_utils.to_container(config_utils.load_config_text(document), resolve=False)
    actual = config_utils.load_config_view_text(document)
    assert type(actual) is dict
    assert actual == expected
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_view_aliases_and_documents_are_independent(yaml_backend):
    document = "base: &base {nested: {x: 7}, values: [1, 2]}\ncopy: *base\n"
    first = config_utils.load_config_view_text(document)
    first["base"]["nested"]["x"] = 99
    first["base"]["values"].append(3)
    assert first["copy"] == {"nested": {"x": 7}, "values": [1, 2]}
    second = config_utils.load_config_view_text(document)
    assert second["base"] == second["copy"] == first["copy"]


@pytest.mark.parametrize("document", [
    "base: 7\nvalue: ${base}\n",
    "value: ${oc.env:PYRUNS_VIEW_ENV}\n",
    "value: ???\n",
    "1: one\nfalse: no\n",
    "value: !!binary YQ==\n",
    "value: !!omap [{a: 1}, {b: 2}]\n",
    "[1, 2]", "'scalar'",
    "{node: " * 20 + "1" + "}" * 20,
])
def test_special_views_keep_omegaconf_behavior(yaml_backend, monkeypatch, document):
    monkeypatch.setenv("PYRUNS_VIEW_ENV", "first")
    expected = config_utils.load_config_text(document)
    actual = config_utils.load_config_view_text(document)
    assert type(actual) is type(expected)
    assert config_utils.to_container(actual, resolve=False) == config_utils.to_container(expected, resolve=False)
    if "PYRUNS_VIEW_ENV" in document:
        monkeypatch.setenv("PYRUNS_VIEW_ENV", "second")
        assert actual["value"] == expected["value"] == "second"


@pytest.mark.parametrize("document", [
    "value: 1\nvalue: 2\n", "items: [1, 2\n", "copy: *undefined\n", "name: \ud800\n",
    "value: ${broken\n", "value: !!set {a: null}\n", "null: invalid-key\n",
    "value: !!timestamp 2026-09-25\n", "value: !!python/object/apply:os.system [forbidden]\n",
    "value: &loop [*loop]\n",
])
def test_view_errors_preserve_existing_diagnostics(yaml_backend, document):
    with pytest.raises(Exception) as expected:
        config_utils.load_config_text(document)
    with pytest.raises(type(expected.value)) as actual:
        config_utils.load_config_view_text(document)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("document", ["value: 7\n", "items: [broken", "[1, 2]", "value: ${broken\n"])
def test_task_payload_default_type_and_view_errors(tmp_path, document):
    (tmp_path / CONFIG_FILENAME).write_text(document, encoding="utf-8")
    default = read_task_payload(str(tmp_path), {})
    view = read_task_payload(str(tmp_path), {}, config_view=True)
    assert isinstance(default[1], DictConfig)
    assert default[0] == view[0]
    assert config_utils.to_container(default[1], resolve=False) == config_utils.to_container(view[1], resolve=False)
    assert default[2:] == view[2:]


def _write_task(tasks_dir, name, document, *, script=None):
    task = tasks_dir / name
    task.mkdir(parents=True)
    (task / CONFIG_FILENAME).write_text(document, encoding="utf-8")
    (task / TASK_INFO_FILENAME).write_text(json.dumps({
        "name": name, "status": "pending", "created_at": "2026-01-01_00-00-00", "script": script,
    }), encoding="utf-8")
    return task


def test_manager_view_refresh_search_and_snapshot_isolation(tmp_path):
    tasks = tmp_path / "_pyruns_" / "train" / "tasks"
    task = _write_task(tasks, "sample", "value: 7\nnested: {items: [1, 2]}\n")
    manager = TaskManager(tasks_dir=str(tasks), lazy_scan=False, owns_task_lifecycle=False)
    try:
        assert type(manager.tasks[0]["config"]) is dict
        snapshot = manager.get_task("sample")
        snapshot["config"]["nested"]["items"].append(99)
        assert manager.get_task("sample")["config"]["nested"]["items"] == [1, 2]
        assert manager.get_task_summary_page(query="value:7", search_field="config")[1] == 1
        (task / CONFIG_FILENAME).write_text("value: [broken", encoding="utf-8")
        assert manager.refresh_from_disk(task_ids=["sample"])
        assert manager.get_task("sample")["_load_error"]
        (task / CONFIG_FILENAME).write_text("base: 8\nvalue: ${base}\n", encoding="utf-8")
        assert manager.refresh_from_disk(task_ids=["sample"])
        assert isinstance(manager.tasks[0]["config"], DictConfig)
        assert manager.get_task("sample")["config"]["value"] == "${base}"
        assert manager.get_task("sample")["_load_error"] == ""
        (task / CONFIG_FILENAME).write_text("value: 9\n", encoding="utf-8")
        assert manager.refresh_from_disk(task_ids=["sample"])
        assert type(manager.tasks[0]["config"]) is dict
        assert manager.get_task_summary_page(query="value:9", search_field="config")[1] == 1
        assert manager.get_task_summary_page(query="value:7", search_field="config")[1] == 0
    finally:
        manager.shutdown()


@pytest.mark.parametrize("interpolated", [False, True])
def test_manager_executes_config_views_with_real_argparse(tmp_path, monkeypatch, interpolated):
    script = tmp_path / "train.py"
    output = tmp_path / "received.json"
    script.write_text(
        "import argparse, json\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser()\np.add_argument('--value', type=int)\n"
        "p.add_argument('--items', nargs='+', type=int)\np.add_argument('--output')\n"
        "a=p.parse_args()\nPath(a.output).write_text(json.dumps(vars(a)), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYRUNS_VIEW_NUMBER", "17")
    value = "${oc.env:PYRUNS_VIEW_NUMBER}" if interpolated else 17
    document = yaml.safe_dump({"value": value, "items": [2, 3], "output": str(output)})
    tasks = tmp_path / "_pyruns_" / "train" / "tasks"
    task = _write_task(tasks, "sample", document, script=str(script))
    manager = TaskManager(tasks_dir=str(tasks), lazy_scan=False)
    try:
        assert isinstance(manager.tasks[0]["config"], DictConfig) is interpolated
        assert manager.start_task_now("sample")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = load_task_info(str(task))
            if info.get("status") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert info["status"] == "completed", info
        assert json.loads(output.read_text(encoding="utf-8")) == {"value": 17, "items": [2, 3], "output": str(output)}
        assert info["exit_codes"] == [0]
    finally:
        manager.shutdown()
