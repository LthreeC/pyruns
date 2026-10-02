"""Real ripgrep integration: matching, byte locations, caching and processes."""

import os
import subprocess
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from pyruns.utils import log_search
from pyruns.utils.log_io import log_file_identity
from pyruns.utils.search_query import SearchBudget, SearchQuery, SearchQueryError


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
    assert bool(result["found"]) == bool(count)
    assert not result["errors"]


def test_newline_only_regex_does_not_create_cached_hits(tmp_path):
    for payload in (b"token\n", b"token\r\n"):
        task, _ = write_log(tmp_path, payload)
        search = log_search.LogSearch()
        for _ in range(2):
            result = search.search(task, r"\R", threading.Event(), SearchQuery(r"\R", use_regex=True))
            assert result == {"matches": [], "match_count": 0, "found": set(), "errors": []}


@pytest.mark.parametrize("text,query,count", [
    ("İ", "i", 0), ("İ", "İ", 1), ("ς", "σ", 1), ("ος", "ΟΣ", 1),
    ("ẞ", "ß", 1), ("ß", "ss", 0), ("ſ", "s", 1), ("ᾈ", "ᾀ", 1),
])
def test_literal_unicode_case_matches_metadata_and_logs(tmp_path, text, query, count):
    from pyruns.utils.task_files import build_task_preview_and_search, build_task_search_result, task_search_found

    directory, _ = write_log(tmp_path, (text + "\n").encode())
    task = {"name": text, "notes": text, "config": {"payload": text}, "env": {"DATA": text}}
    for options in ({}, {"whole_word": True}, {"match_case": True}):
        expected = int(query in text) if options.get("match_case") else count
        matcher = SearchQuery(query, **options)
        log = log_search.LogSearch().search(directory, query, threading.Event(), matcher)
        assert log["match_count"] == expected
        for kind in ("config", "shell"):
            task.update(task_kind=kind, config_text=text)
            _, task["search_text"] = build_task_preview_and_search(
                task_kind=kind, task_name=text, notes=text, config=task["config"], config_text=text,
            )
            for field in ("all", "name", "notes", "env", "config" if kind == "config" else "script"):
                assert task_search_found(task, matcher, field) == log["found"]
                result = build_task_search_result(task, query, search_field=field, matcher=matcher)
                assert result["match_count"] == expected * (4 if field == "all" else 1)
                for match in result["matches"]:
                    assert match["snippet"][match["match_start"]:match["match_end"]] == text


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


def test_batching_literal_multiline_and_cache_capacity(tmp_path, monkeypatch):
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
    assert results[tasks[-1]]["found"] == set()
    assert results[tasks[-1]]["match_count"] == 0
    # Force command-line splitting; every file must still be searched once.
    monkeypatch.setattr(log_search, "_ARG_BYTES", 4150)
    results = search.search_many(tasks, "other", threading.Event())
    assert sum(result["match_count"] for result in results.values()) == 4
    assert all(len(batch) == 1 for batch in batches[-5:])


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_multiline_search_keeps_contiguous_text_locations_and_regex_escapes(tmp_path, newline):
    task, _ = write_log(tmp_path, newline.join([b"header", b"first", b"second", b"footer", b"first", b"second"]))
    search = log_search.LogSearch()
    for query, options in [("first\nsecond", {}), (r"first\nsecond", {"use_regex": True}),
                           (r"first\n+second", {"use_regex": True})]:
        result = search.search(task, query, threading.Event(), SearchQuery(query, **options))
        assert result["match_count"] == 2
        assert [match["line"] for match in result["matches"]] == [2, 5]
        assert all(match["snippet"][match["match_start"]:match["match_end"]] == "first\nsecond" for match in result["matches"])
    task, _ = write_log(tmp_path, b"first\n")
    write_log(tmp_path, b"second\nfirst\\nsecond\n", filename="run2.log")
    assert search.search(task, "first\nsecond", threading.Event())["match_count"] == 0
    escaped = SearchQuery(r"first\\nsecond", use_regex=True)
    assert not escaped.multiline
    assert search.search(task, r"first\\nsecond", threading.Event(), escaped)["match_count"] == 1


@pytest.mark.parametrize("query,use_regex,count", [
    (".foo", False, 1), ("foo.", False, 1), ("中文", False, 2),
    ("foo", False, 2), ("fo[o]", True, 2), ("ſ", False, 1),
])
def test_vscode_whole_word_pattern_edges(tmp_path, query, use_regex, count):
    from pyruns.utils.task_files import build_task_search_result

    text = "x.foo xfoox foo.x x中文x 中文 xsx"
    task, _ = write_log(tmp_path, text.encode())
    matcher = SearchQuery(query, whole_word=True, use_regex=use_regex)
    result = log_search.LogSearch().search(task, query, threading.Event(), matcher)
    assert result["match_count"] == count
    assert build_task_search_result({"notes": text}, query, search_field="notes", matcher=matcher)["match_count"] == count


def test_match_limit_reaps_process_and_never_caches_partial_results(tmp_path, monkeypatch):
    task, _ = write_log(tmp_path, b"needle\n" * 100)
    original = subprocess.Popen
    processes = []

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(log_search.subprocess, "Popen", record)
    search = log_search.LogSearch()
    for limit in (3, None, 2, None):
        budget = SearchBudget(limit)
        result = search.search_many([task], "needle", threading.Event(), budget=budget)[task]
        assert result["match_count"] == (limit or 100)
        assert len(result["matches"]) == min(limit or 100, 24)
        assert budget.limit_hit is (limit is not None)
    assert len(processes) == 2
    assert all(process.poll() is not None and process.stdout.closed for process in processes)


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


def test_cancel_stops_log_enumeration_before_remaining_metadata_reads(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from pyruns.utils import info_io

    task, first = write_log(tmp_path, b"needle\n")
    _, second = write_log(tmp_path, b"needle\n", filename="run2.log")
    cancelled = threading.Event()
    checked = []
    closed = []
    real_lstat = os.lstat

    def entry_stat(path, *args, **kwargs):
        if os.fspath(path) in {str(first), str(second)}:
            checked.append(str(path))
            cancelled.set()
        return real_lstat(path, *args, **kwargs)

    @contextmanager
    def entries(_directory):
        try:
            yield (
                SimpleNamespace(name=path.name, path=str(path),
                                stat=lambda *, follow_symlinks, path=path: entry_stat(path))
                for path in (first, second)
            )
        finally:
            closed.append(True)

    with monkeypatch.context() as patch:
        patch.setattr(info_io.os, "scandir", entries)
        patch.setattr(info_io.os, "lstat", entry_stat)
        with pytest.raises(CancelledError):
            log_search.LogSearch().search(task, "needle", cancelled)
    assert checked == [str(first)]
    assert closed == [True]


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
