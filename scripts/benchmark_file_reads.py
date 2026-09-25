"""Check small-file allocation bounds and optionally report warm read timings.

Run in a separate Python process so pytest, coverage, or another thread cannot
pollute tracemalloc. Imports and one warm-up read are outside the measurement.
"""

from __future__ import annotations

import argparse
from functools import partial
import gc
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc


ROOT = Path(__file__).resolve().parents[1]
# Check this checkout even when another Pyruns version is installed.
sys.path.insert(0, str(ROOT))

# Current small-file peaks are around 10 KiB. Allow substantial interpreter
# variation while rejecting the former 4/32 MiB limit-sized allocations.
MAX_SMALL_READ_PEAK_BYTES = 256 * 1024


def _measure(name, read, expected, iterations):
    assert read() == expected, f"{name}: warm-up returned different content"
    gc.collect()
    tracemalloc.start()
    try:
        value = read()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert value == expected, f"{name}: measured read returned different content"

    samples = []
    if iterations:
        for _ in range(5):
            started = time.perf_counter()
            for _ in range(iterations):
                assert read() == expected, f"{name}: timed read returned different content"
            samples.append((time.perf_counter() - started) * 1_000_000 / iterations)
    return {
        "reader": name,
        "peak_bytes": peak,
        "allocation_ok": peak <= MAX_SMALL_READ_PEAK_BYTES,
        "mean_us_per_batch": samples,
        "median_us": statistics.median(samples) if samples else None,
    }


def benchmark(directory, iterations):
    from pyruns.cli import submission_protocol
    from pyruns.core import config_manager
    from pyruns.utils import config_utils, info_io, settings, task_files
    from pyruns.web.runtime import PyrunsRuntime

    document = {"value": "中文", "epochs": 10, "nested": {"enabled": True, "labels": ["a", "b"]}}
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path = directory / "document.yaml"
    path.write_bytes(raw)
    text = raw.decode("utf-8")

    token = "a" * 32
    payload = {
        "schema_version": submission_protocol.SCHEMA_VERSION,
        "submission_token": token,
        "submissions": [{"name": "中文", "run_index": 1}, {"name": "second", "run_index": 2}],
    }
    submission = directory / "submission.json"
    submission_raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    submission.write_bytes(submission_raw)

    readers = [
        ("task", partial(task_files._read_text_limited, str(path)), text),
        ("yaml", partial(config_utils._read_yaml_text_limited, str(path)), text),
        ("settings", partial(settings._read_settings_text, str(path)), text),
        ("config", partial(config_manager._read_config_bytes, str(path)), raw),
        ("template", partial(PyrunsRuntime._read_template_text, str(path), "document"), text),
        ("metadata", partial(info_io._load_json_object, str(path),
                             max_bytes=info_io.MAX_TASK_INFO_BYTES, label="Task"), document),
        ("submission", partial(submission_protocol.read_submission_payload, str(submission), token=token),
         submission_protocol.SubmissionPayload(("中文", "second"), (1, 2))),
    ]
    return {
        "python": sys.version,
        "platform": sys.platform,
        "source_root": str(ROOT),
        "temporary_directory": str(directory),
        "document_bytes": len(raw),
        "submission_bytes": len(submission_raw),
        "cache_state": "warm; imports and initial reads excluded",
        "allocation_scope": "Python tracemalloc peak, not RSS",
        "max_peak_bytes": MAX_SMALL_READ_PEAK_BYTES,
        "iterations_per_batch": iterations,
        "readers": [_measure(name, read, expected, iterations) for name, read, expected in readers],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100,
                        help="Reads per timing batch; 0 checks only allocations and content")
    parser.add_argument("--output", type=Path, help="Also save the full JSON report to this file")
    args = parser.parse_args()
    if args.iterations < 0:
        parser.error("--iterations must be non-negative")

    with tempfile.TemporaryDirectory(prefix="pyruns-file-reads-") as temporary:
        report = benchmark(Path(temporary), args.iterations)
    report["passed"] = all(item["allocation_ok"] for item in report["readers"])
    output = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
