"""Bounded reads preserve growth, short reads, encoding, and size limits."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pyruns.utils import file_io
from pyruns.utils import file_boundary
from pyruns.utils.file_io import read_bounded_bytes


@pytest.mark.parametrize("path_query", [True, pytest.param(False, marks=pytest.mark.skipif(os.name == "nt", reason="POSIX fallback"))])
def test_open_file_validation_checks_the_descriptor_without_reading(tmp_path, monkeypatch, path_query):
    root = tmp_path / "task"
    inside = root / "nested" / "中文.yaml"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    boundary = os.path.realpath(root)
    if not path_query:
        monkeypatch.setattr(file_boundary, "_opened_file_path", lambda _fd: None)
    # The current path is contained, but the handle belongs to another file.
    with outside.open("rb") as handle:
        with pytest.raises(ValueError if path_query else OSError, match="resolves outside|changed while validating"):
            file_boundary.validate_open_file(handle, str(inside), boundary)
        assert handle.tell() == 0
    with inside.open("rb") as handle:
        file_boundary.validate_open_file(handle, str(inside), boundary)
        assert handle.tell() == 0
        assert handle.read() == b"inside"


def test_small_document_readers_do_not_allocate_their_size_limits():
    root = Path(__file__).resolve().parents[1]
    # Keep tracing isolated from pytest/plugins; the shared Windows fixture
    # ensures this child cannot create a console window.
    result = subprocess.run(
        [sys.executable, str(root / "scripts/benchmark_file_reads.py"), "--iterations", "0"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["passed"]
    assert {item["reader"] for item in report["readers"]} == {
        "task", "yaml", "settings", "config", "template", "metadata", "submission",
    }


def test_empty_file_with_large_limit_returns_empty_bytes(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    with path.open("rb") as handle:
        assert read_bounded_bytes(handle, 32 * 1024 * 1024) == b""
        assert handle.tell() == 0


@pytest.mark.parametrize("limit", [0, 1, 7, 65536, 65537, 200000])
def test_bounded_file_read_returns_exact_prefix_from_current_position(tmp_path, limit):
    data = bytes(range(256)) * 600
    path = tmp_path / "content"
    path.write_bytes(data)
    with path.open("rb") as handle:
        handle.seek(17)
        assert read_bounded_bytes(handle, limit) == data[17:17 + limit]
        assert handle.tell() == min(len(data), 17 + limit)


@pytest.mark.parametrize("size_hint", [0, 10**9, None, "non_regular"])
def test_file_size_is_only_an_allocation_hint(tmp_path, monkeypatch, size_hint):
    data = b"unchanged content\n" * 5000
    path = tmp_path / "content"
    path.write_bytes(data)

    def fstat(_fd):
        if size_hint is None:
            raise OSError("size unavailable")
        return SimpleNamespace(
            st_mode=stat.S_IFIFO if size_hint == "non_regular" else stat.S_IFREG,
            st_size=0 if size_hint == "non_regular" else size_hint,
        )

    monkeypatch.setattr(file_io, "os", SimpleNamespace(fstat=fstat))
    with path.open("rb") as handle:
        assert read_bounded_bytes(handle, 50000) == data[:50000]


def test_bounded_file_read_observes_growth_after_size_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "growing"
    path.write_bytes(b"start")
    appended = b"x" * 100000
    real_fstat = os.fstat

    def grow_after_stat(fd):
        info = real_fstat(fd)
        with path.open("ab") as writer:
            writer.write(appended)
        return info

    monkeypatch.setattr(file_io, "os", SimpleNamespace(fstat=grow_after_stat))
    with path.open("rb") as handle:
        assert read_bounded_bytes(handle, 70001) == (b"start" + appended)[:70001]
        assert handle.tell() == 70001


def test_bounded_file_read_handles_short_reads_without_file_descriptor():
    class ShortReads(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 3))

    handle = ShortReads(b"abcdefghijklmnopqrstuvwxyz")
    assert read_bounded_bytes(handle, 20) == b"abcdefghijklmnopqrst"
    assert handle.read() == b"uvwxyz"


def test_bounded_file_read_falls_back_when_position_is_unavailable(tmp_path):
    path = tmp_path / "content"
    path.write_bytes(b"hello world")
    with path.open("rb") as handle:
        class NoPosition:
            read = handle.read
            fileno = handle.fileno

            def tell(self):
                raise OSError("position unavailable")

        assert read_bounded_bytes(NoPosition(), 7) == b"hello w"


def test_bounded_file_read_propagates_failure_after_partial_data():
    class BrokenRead(io.BytesIO):
        def read(self, count=-1):
            if self.tell():
                raise OSError("read failed")
            return super().read(2)

    with pytest.raises(OSError, match="read failed"):
        read_bounded_bytes(BrokenRead(b"abcdef"), 6)
    with pytest.raises(ValueError, match="non-negative"):
        read_bounded_bytes(io.BytesIO(b"abcdef"), -1)


@pytest.mark.parametrize("reader", ["task", "yaml", "settings", "config", "template", "metadata", "submission"])
def test_document_readers_enforce_regular_files_and_size_limits(tmp_path, monkeypatch, reader):
    from pyruns.cli import submission_protocol
    from pyruns.core import config_manager
    from pyruns.utils import config_utils, info_io, settings, task_files
    from pyruns.web import runtime

    token = "a" * 32
    document = {"value": "中文"}
    if reader == "submission":
        document = {"schema_version": submission_protocol.SCHEMA_VERSION,
                    "submission_token": token, "submissions": [{"name": "中文", "run_index": 1}]}
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path = tmp_path / "document.yaml"
    path.write_bytes(raw)
    limit = len(raw)
    for module in (config_utils, config_manager, settings):
        monkeypatch.setattr(module, "MAX_CONFIG_FILE_BYTES", limit)
    monkeypatch.setattr(runtime, "MAX_TASK_PAYLOAD_BYTES", limit)
    monkeypatch.setattr(submission_protocol, "MAX_SUBMISSION_PAYLOAD_BYTES", limit)
    readers = {
        "task": lambda: task_files._read_text_limited(str(path), max_bytes=limit),
        "yaml": lambda: config_utils._read_yaml_text_limited(str(path)),
        "settings": lambda: settings._read_settings_text(str(path)),
        "config": lambda: config_manager._read_config_bytes(str(path)),
        "template": lambda: runtime.PyrunsRuntime._read_template_text(str(path), "document"),
        "metadata": lambda: info_io._load_json_object(str(path), max_bytes=limit, label="Metadata"),
        "submission": lambda: submission_protocol.read_submission_payload(str(path), token=token),
    }
    value = readers[reader]()
    if reader == "metadata":
        assert value == document
    elif reader == "submission":
        assert value.names == ("中文",) and value.run_indices == (1,)
    else:
        assert value == (raw if reader == "config" else raw.decode("utf-8"))

    document_path = path
    path = Path(os.devnull)
    with pytest.raises(ValueError, match="regular file"):
        readers[reader]()
    path = document_path

    real_fstat = os.fstat

    def grow_after_stat(fd):
        info = real_fstat(fd)
        with path.open("ab") as writer:
            writer.write(b" ")
        return info

    monkeypatch.setattr(file_io, "os", SimpleNamespace(**{**vars(os), "fstat": grow_after_stat}))
    with pytest.raises(ValueError, match="too large"):
        readers[reader]()


def test_document_opener_checks_actual_descriptor_and_closes_rejected_file(tmp_path, monkeypatch):
    path = tmp_path / "document"
    path.write_bytes(b"regular document")
    descriptors = []
    real_open = os.open

    def open_device(_path, flags):
        fd = real_open(os.devnull, flags)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(file_io, "os", SimpleNamespace(**{**vars(os), "open": open_device}))
    with pytest.raises(ValueError, match="regular file"):
        with open(path, "rb", opener=file_io.regular_file_opener):
            pytest.fail("A regular path must not hide a non-regular descriptor")
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_task_scan_rejects_fifo_metadata_and_recovers(tmp_path):
    task = tmp_path / "sample"
    task.mkdir()
    (task / "config.yaml").write_text("value: 7\n", encoding="utf-8")
    os.mkfifo(task / "task_info.json")
    code = """
import json
from pathlib import Path
import sys
from pyruns.core.task_manager import TaskManager

root = Path(sys.argv[1])
manager = TaskManager(str(root), lazy_scan=None, owns_task_lifecycle=False)
try:
    manager.scan_disk()
    assert 'regular file' in manager.get_task('sample')['_load_error']
    path = root / 'sample' / 'task_info.json'
    path.unlink()
    path.write_text(json.dumps({'status': 'pending', 'notes': 'recovered'}))
    assert manager.refresh_from_disk(check_all=True, check_payload=False)
    task = manager.get_task('sample')
    assert (task['notes'], task['config'], task['_load_error']) == ('recovered', {'value': 7}, '')
finally:
    manager.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_text_readers_preserve_bom_and_newline_rules(tmp_path):
    from pyruns.utils import config_utils, settings, task_files
    from pyruns.web.runtime import PyrunsRuntime

    path = tmp_path / "document.yaml"
    path.write_bytes("\ufeffvalue: 中文\r\nnext: 2\r".encode("utf-8"))
    assert config_utils._read_yaml_text_limited(str(path)) == "value: 中文\r\nnext: 2\r"
    assert settings._read_settings_text(str(path)) == "value: 中文\r\nnext: 2\r"
    assert task_files._read_text_limited(str(path)) == "\ufeffvalue: 中文\r\nnext: 2\r"
    assert PyrunsRuntime._read_template_text(str(path), "document") == "\ufeffvalue: 中文\nnext: 2\n"
