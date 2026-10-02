"""Real ripgrep integration: matching, byte locations, caching and processes."""

import os
import subprocess
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from pyruns.utils import log_search
from pyruns.utils.log_io import log_file_identity
from pyruns.utils.search_query import SearchQuery, SearchQueryError


def write_log(root, payload, task="task", filename="run1.log"):
    directory = root / "tasks" / task
    logs = directory / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / filename
    path.write_bytes(payload)
    return str(directory), path


@pytest.mark.parametrize("query,options,count", [
    ("lr:0.001", {}, 0), ("lr: 0.001", {}, 1),
    (r"lr:\s*0\.001", {"use_regex": True}, 1),
    ("TOKEN", {"match_case": True}, 0), ("token", {"whole_word": True}, 1),
    ("token", {}, 2), ("to.ken", {}, 0),
    (r"(?<=prefix )Token(?= suffix)", {"use_regex": True}, 1),
    (" token ", {}, 1), ("^$", {"use_regex": True}, 1),
], ids=["missing-space", "literal", "regex-space", "case", "word", "substring", "literal-dot", "lookaround", "outer-spaces", "empty-line"])
def test_literal_spaces_and_search_options(tmp_path, query, options, count):
    task, _ = write_log(tmp_path, b"lr: 0.001\r\nprefix Token suffix\r\ntokenize\r\n\r\n")
    result = log_search.LogSearch().search(task, query, threading.Event(), SearchQuery(query, **options))
    assert result["match_count"] == count
    assert not result["errors"]


@pytest.mark.parametrize("prefix,token", [
    (b"x" * 65536 + b"\r\n", "Token42"),
    (("前缀😀" * 10000).encode(), "😀测试"),
    (b"\xff" * 2000, "token"),
    (b"prefix ", "İstanbul"),
], ids=["long-ascii", "unicode", "invalid-utf8", "unicode-case"])
def test_utf8_and_invalid_bytes_keep_original_log_locations(tmp_path, prefix, token):
    payload = prefix + b"\x1b[31m" + token.encode() + b"\x1b[0m suffix\r\n"
    task, path = write_log(tmp_path, payload)
    result = log_search.LogSearch().search(task, token, threading.Event())
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["snippet"][match["match_start"]:match["match_end"]] == token
    assert "\x1b" not in match["snippet"] and "\r" not in match["snippet"]
    assert len(match["snippet"]) <= 180
    assert match["line"] == prefix.count(b"\n") + 1
    assert 0 <= payload.index(token.encode()) - match["offset"] <= 256
    assert match["log_identity"] == log_file_identity(str(path))


def test_ansi_is_stripped_only_for_display(tmp_path):
    task, _ = write_log(tmp_path, b"to\x1b[31mken\x1b[0m\n\x1b[31mtoken\x1b[0m\n")
    search = log_search.LogSearch()
    result = search.search(task, "token", threading.Event())
    assert result["match_count"] == 1 and result["matches"][0]["line"] == 2
    assert result["matches"][0]["snippet"] == "token"
    assert search.search(task, "^token$", threading.Event(), SearchQuery("^token$", use_regex=True))["match_count"] == 0


@pytest.mark.parametrize("payload,count", [(b"a" * 100_000, 100_000 // 3), (b"aaa\n" * 100_000, 100_000)], ids=["long-line", "many-lines"])
def test_counts_all_occurrences_with_bounded_previews(tmp_path, payload, count):
    task, _ = write_log(tmp_path, payload)
    search = log_search.LogSearch()
    result = search.search(task, "aaa", threading.Event())
    assert result["match_count"] == count
    assert len(result["matches"]) == 24
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        search.search(task, "aaa", cancelled)


def test_batching_overlapping_keywords_and_cache_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(log_search, "_CACHE_FILES_PER_QUERY", 1)
    monkeypatch.setattr(log_search, "_CACHE_MISSES_PER_QUERY", 2)
    tasks = [write_log(tmp_path, b"needle\n" if i == 4 else b"other\n", task=f"task{i}")[0] for i in range(5)]
    original = log_search._ripgrep_events
    batches = []

    def record(paths, *args):
        batches.append(tuple(paths))
        yield from original(paths, *args)

    monkeypatch.setattr(log_search, "_ripgrep_events", record)
    search = log_search.LogSearch()
    for iteration in range(3):
        results = search.search_many(tasks, "needle", threading.Event())
        assert sum(result["match_count"] for result in results.values()) == 1
        assert len(batches) == iteration + 1
        assert len(batches[-1]) == (5 if iteration == 0 else 2)
    results = search.search_many(tasks, "need\nneedle", threading.Event())
    assert results[tasks[-1]]["found"] == {"need", "needle"}
    assert results[tasks[-1]]["match_count"] == 2
    # Force command-line splitting; every file must still be searched once.
    monkeypatch.setattr(log_search, "_ARG_BYTES", 4150)
    results = search.search_many(tasks, "other", threading.Event())
    assert sum(result["match_count"] for result in results.values()) == 4
    assert all(len(batch) == 1 for batch in batches[-5:])


@pytest.mark.parametrize("change", ["append", "rewrite", "replace", "remove"])
def test_cached_misses_recheck_file_changes(tmp_path, change):
    task, path = write_log(tmp_path, b"absent\n")
    search = log_search.LogSearch()
    event = threading.Event()
    assert search.search(task, "needle", event)["match_count"] == 0
    previous = path.stat()
    if change == "append":
        with path.open("ab") as handle:
            handle.write(b"needle\n")
    elif change == "replace":
        replacement = path.with_suffix(".new")
        replacement.write_bytes(b"needle\n")
        replacement.replace(path)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "remove":
        path.unlink()
    else:
        path.write_bytes(b"needle\n")
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    result = search.search(task, "needle", event)
    assert result["match_count"] == (0 if change == "remove" else 1)
    assert not result["errors"]

    if change != "remove":
        path.write_bytes(b"gone\n")
        assert search.search(task, "needle", event)["match_count"] == 0


@pytest.mark.parametrize("before,after", [
    (("absent", {}), ("needle", {})),
    (("NEEDLE", {"match_case": True}), ("NEEDLE", {})),
    (("need", {"whole_word": True}), ("need", {})),
    (("n.*le", {}), ("n.*le", {"use_regex": True})),
])
def test_cache_keeps_query_options_separate(tmp_path, before, after):
    task, _ = write_log(tmp_path, b"needle\n")
    search = log_search.LogSearch()
    assert search.search(task, before[0], threading.Event(), SearchQuery(before[0], **before[1]))["match_count"] == 0
    assert search.search(task, after[0], threading.Event(), SearchQuery(after[0], **after[1]))["match_count"] == 1


def test_read_errors_are_reported_and_not_cached(tmp_path, monkeypatch):
    task, path = write_log(tmp_path, b"needle\n")
    original = log_search._ripgrep_events

    def remove_before_read(*args):
        path.unlink()
        yield from original(*args)

    search = log_search.LogSearch()
    with monkeypatch.context() as patch:
        patch.setattr(log_search, "_ripgrep_events", remove_before_read)
        result = search.search(task, "needle", threading.Event())
    assert result["errors"] and "run1.log" in result["errors"][0]
    path.write_bytes(b"needle\n")
    assert search.search(task, "needle", threading.Event())["match_count"] == 1


def test_cancel_kills_and_reaps_a_process_with_no_output(tmp_path, monkeypatch):
    task, _ = write_log(tmp_path, b"needle\n")
    cancelled, started = threading.Event(), threading.Event()
    original = subprocess.Popen
    processes = []

    def waiting_process(command, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(log_search.subprocess, "Popen", waiting_process)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(log_search.LogSearch().search, task, "needle", cancelled)
        try:
            assert started.wait(5)
        finally:
            cancelled.set()
        with pytest.raises(CancelledError):
            future.result(timeout=5)
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed


def test_no_path_or_user_config_dependency_and_engine_errors(tmp_path, monkeypatch):
    task, _ = write_log(tmp_path, b"needle\n")
    config = tmp_path / "rg-config"
    config.write_text("--glob=!*.log\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    search = log_search.LogSearch()
    assert search.search(task, "needle", threading.Event())["match_count"] == 1
    # Accepted by Python regex, rejected by PCRE2's bounded lookbehind rule.
    pattern = r"(?<=a*)needle"
    with pytest.raises(SearchQueryError, match="compiling pattern"):
        search.search(task, pattern, threading.Event(), SearchQuery(pattern, use_regex=True))
