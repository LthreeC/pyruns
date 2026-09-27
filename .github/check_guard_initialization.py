"""Compare actual OS guard initialization in controlled concurrent schedules."""
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading

import pyruns.update_coordination as coordination

root = Path(__file__).resolve().parents[1]
output = root / "test-results/guard-init"
output.mkdir(parents=True, exist_ok=True)
baseline_source = (root / ".github/guard_init_baseline.py").read_text()
namespace = vars(coordination).copy()
exec(compile(baseline_source, "guard_init_baseline.py", "exec"), namespace)
baseline = namespace["_acquire_native_guard"]
candidate = coordination.CoordinationStore._acquire_native_guard
report = {"sha": os.environ.get("GITHUB_SHA"), "platform": platform.platform(),
          "baseline_sha": "4be89fa57f3c47108be5576faf8bbfb7192325d5",
          "candidate_source_sha256": hashlib.sha256(Path(coordination.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
          "baseline_function_sha256": hashlib.sha256(baseline_source.encode()).hexdigest(),
          "observations": [], "passed": False}


def error_info(error):
    return {"type": type(error).__name__, "message": str(error),
            "errno": getattr(error, "errno", None), "winerror": getattr(error, "winerror", None)}


def held_empty_guard(directory):
    store = coordination.CoordinationStore(str(directory))
    store.ensure()
    fd = os.open(store.lock_guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = store._try_native_lock(fd)
    try:
        assert acquired is True, "native lock on an empty file is unavailable"
        before = os.fstat(fd).st_size
        failure = None
        try:
            with coordination.CoordinationStore(str(directory)).locked(timeout=0.05):
                raise AssertionError("contender entered another owner's guard")
        except (OSError, coordination.UpdateCoordinationError) as error:
            failure = error_info(error)
        return {"guard_size": [before, os.fstat(fd).st_size], "failure": failure}
    finally:
        store._close_native_lock(fd, acquired)


def concurrent_first_use(directory, mode):
    observed_empty = threading.Event()
    release_initializer = threading.Event()
    contender_entered = threading.Event()
    release_contender = threading.Event()
    initializer_done = threading.Event()
    entries, failures = [], {}
    original_fstat = os.fstat

    def paused_fstat(fd):
        value = original_fstat(fd)
        if threading.current_thread().name == "initializer" and not observed_empty.is_set():
            assert value.st_size == 0
            observed_empty.set()
            assert release_initializer.wait(5), "initializer was not released"
        return value

    def worker(name):
        try:
            with coordination.CoordinationStore(str(directory)).locked(timeout=3):
                entries.append(name)
                if name == "contender":
                    contender_entered.set()
                    assert release_contender.wait(5), "contender was not released"
        except BaseException as error:
            failures[name] = error_info(error)
        finally:
            if name == "initializer":
                initializer_done.set()

    threads = [threading.Thread(target=worker, args=(name,), name=name, daemon=True)
               for name in ("initializer", "contender")]
    os.fstat = paused_fstat
    try:
        threads[0].start()
        assert observed_empty.wait(3)
        threads[1].start()
        entered_before_release = contender_entered.wait(2 if mode == "baseline" else 0.1)
        assert entered_before_release is (mode == "baseline")
        release_initializer.set()
        initializer_done.wait(1)
    finally:
        release_initializer.set()
        release_contender.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(4)
        os.fstat = original_fstat
    assert not any(thread.is_alive() for thread in threads), "diagnostic left a running thread"
    with coordination.CoordinationStore(str(directory)).locked(timeout=0.3):
        retry_after_release = True
    return {"entries": entries, "failures": failures,
            "contender_entered_before_initializer_resumed": entered_before_release,
            "retry_after_release": retry_after_release, "threads_remaining": 0}


try:
    with tempfile.TemporaryDirectory(prefix="pyruns-guard-init-") as temporary:
        for mode, method in (("baseline", baseline), ("candidate", candidate)):
            coordination.CoordinationStore._acquire_native_guard = method
            observation = {"mode": mode,
                           "held_empty_guard": held_empty_guard(Path(temporary) / mode / "held"),
                           "concurrent_first_use": concurrent_first_use(Path(temporary) / mode / "race", mode)}
            report["observations"].append(observation)
    old, new = report["observations"]
    assert new["held_empty_guard"]["guard_size"] == [0, 0]
    assert new["held_empty_guard"]["failure"]["type"] == "UpdateCoordinationError"
    assert new["concurrent_first_use"]["entries"] == ["initializer", "contender"]
    assert new["concurrent_first_use"]["failures"] == {}
    if sys.platform == "win32":
        assert old["held_empty_guard"]["failure"]["type"] == "PermissionError"
        assert old["concurrent_first_use"]["failures"]["initializer"]["type"] == "PermissionError"
        assert old["concurrent_first_use"]["entries"] == ["contender"]
    else:
        assert old["held_empty_guard"]["guard_size"] == [0, 1]
        assert old["concurrent_first_use"]["entries"] == ["contender", "initializer"]
        assert old["concurrent_first_use"]["failures"] == {}
    report["passed"] = True
finally:
    coordination.CoordinationStore._acquire_native_guard = candidate
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
