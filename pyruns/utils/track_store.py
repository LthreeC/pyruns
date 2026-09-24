"""Durable append storage for large task curves.

Callers serialize writes and generation changes with ``task_info_lock``.
JSON metadata selects a committed generation; preparing a new generation never
changes the old one. Only unreferenced generations may be pruned after the
metadata file has been atomically replaced.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


TRACK_STORE_KEY = "track_store"
TRACK_STORE_FILENAME = "tracks.sqlite3"
INLINE_TRACK_POINTS = 1024
INLINE_TRACK_BYTES = 256 * 1024
MAX_TRACK_EVENT_BYTES = 16 * 1024 * 1024
_SCHEMA_VERSION = 1
_GENERATION_RE = re.compile(r"[0-9a-f]{32}\Z")
_READ_BATCH_POINTS = 1024
_READ_BATCH_CHARS = 256 * 1024


class MissingTrackGeneration(ValueError):
    """A reader's metadata pointer may have been replaced and pruned."""


def _generation(descriptor: Any) -> str:
    if not isinstance(descriptor, dict) or descriptor.get("version") != _SCHEMA_VERSION:
        raise ValueError("Unsupported track storage version")
    generation = descriptor.get("generation")
    if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
        raise ValueError("Invalid track storage generation")
    return generation


def encode_values(data: Any) -> str:
    """Snapshot one append before touching either storage file."""
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_TRACK_EVENT_BYTES:
        raise ValueError(f"Track update exceeds {MAX_TRACK_EVENT_BYTES} bytes")
    return text


def should_externalize(tracks: list) -> bool:
    points = 0
    for run in tracks:
        if not isinstance(run, dict):
            raise ValueError("Track history entries must be objects")
        for values in run.values():
            if not isinstance(values, list):
                raise ValueError("Track series must be arrays")
            points += len(values)
            if points >= INLINE_TRACK_POINTS:
                return True
    # Wide individual values must not consume the lifecycle metadata budget.
    return len(json.dumps(tracks, ensure_ascii=False, allow_nan=False).encode("utf-8")) >= INLINE_TRACK_BYTES


def _database_path(task_dir: str) -> str:
    from pyruns.utils.info_io import validate_task_directory, validate_workspace_file

    validate_task_directory(task_dir)
    path = os.path.join(task_dir, TRACK_STORE_FILENAME)
    for suffix in ("", "-journal", "-wal", "-shm"):
        validate_workspace_file(path + suffix, task_dir, label="Track storage")
    return path


@contextmanager
def _connect(task_dir: str, *, create: bool = False, readonly: bool = False):
    path = _database_path(task_dir)
    if not create and not os.path.isfile(path):
        raise FileNotFoundError(f"Track storage is missing: {path}")
    mode = "ro" if readonly else ("rwc" if create else "rw")
    uri = Path(path).resolve().as_uri() + f"?mode={mode}"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        try:
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.OperationalError as exc:
            if not (readonly and "readonly" in str(exc).lower() and os.path.isfile(path + "-journal")):
                raise
            # A hot rollback journal from an interrupted writer needs SQLite's
            # recovery writes. Clean stores remain readable without write access.
            connection.close()
            with _connect(task_dir) as recovered:
                yield recovered
            return
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("Track storage requires DELETE journal mode")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and create:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                    raise ValueError("Track storage path contains an unrelated database")
                connection.execute("""CREATE TABLE generations (
                    generation TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                )""")
                connection.execute("""CREATE TABLE points (
                    sequence INTEGER PRIMARY KEY,
                    generation TEXT NOT NULL,
                    operation TEXT NOT NULL UNIQUE,
                    run_index INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )""")
                connection.execute("CREATE INDEX points_generation ON points(generation, sequence)")
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        elif version != _SCHEMA_VERSION:
            raise ValueError(f"Unsupported track database version: {version}")
        yield connection
    finally:
        connection.close()


def new_descriptor(tracks: list) -> dict[str, Any]:
    """Allocate a pointer so metadata can be validated before changing storage."""
    return {
        "version": _SCHEMA_VERSION,
        "generation": uuid.uuid4().hex,
        "max_run_index": max((index + 1 for index, run in enumerate(tracks) if any(run.values())), default=0),
    }


def prepare_generation(task_dir: str, tracks: list, descriptor: dict[str, Any]) -> None:
    """Commit an unreferenced snapshot; the caller next switches JSON metadata."""
    generation = _generation(descriptor)
    snapshot = json.dumps(tracks, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with _connect(task_dir, create=True) as connection, connection:
        connection.execute("INSERT INTO generations VALUES (?, ?)", (generation, snapshot))


def prune_generations(task_dir: str, descriptor: dict[str, Any]) -> None:
    """Remove only generations excluded by the committed metadata pointer."""
    generation = _generation(descriptor)
    with _connect(task_dir) as connection, connection:
        if connection.execute("SELECT 1 FROM generations WHERE generation=?", (generation,)).fetchone() is None:
            raise ValueError("Referenced track generation is missing")
        connection.execute("DELETE FROM points WHERE generation<>?", (generation,))
        connection.execute("DELETE FROM generations WHERE generation<>?", (generation,))


def _read_point_batch(connection: sqlite3.Connection, generation: str, after: int, maximum: int) -> list:
    """Copy a bounded batch, releasing SQLite's read lock before decoding it."""
    connection.execute("BEGIN")
    if connection.execute("SELECT 1 FROM generations WHERE generation=?", (generation,)).fetchone() is None:
        raise MissingTrackGeneration("Referenced track generation is missing")
    cursor = connection.execute(
        "SELECT sequence, run_index, payload FROM points "
        "WHERE generation=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
        (generation, after, maximum, _READ_BATCH_POINTS),
    )
    rows = []
    characters = 0
    try:
        for row in cursor:
            rows.append(row)
            characters += len(row[2])
            if characters >= _READ_BATCH_CHARS:
                break
    finally:
        # An unfinished SELECT cursor can retain a read lock after COMMIT.
        cursor.close()
    connection.commit()
    return rows


def read_tracks(task_dir: str, descriptor: dict[str, Any], *, slots: int) -> list:
    """Read a fixed history using short transactions, then decode without locks."""
    generation = _generation(descriptor)
    with _connect(task_dir, readonly=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT snapshot FROM generations WHERE generation=?", (generation,)).fetchone()
        if row is None:
            raise MissingTrackGeneration("Referenced track generation is missing")
        maximum = connection.execute(
            "SELECT max(sequence) FROM points WHERE generation=?", (generation,),
        ).fetchone()[0] or 0
        connection.commit()
        tracks = json.loads(row[0])
        if not isinstance(tracks, list) or any(not isinstance(run, dict) for run in tracks):
            raise ValueError("Invalid track snapshot")
        if len(tracks) > slots:
            tracks = tracks[:slots]
        tracks.extend({} for _ in range(slots - len(tracks)))
        previous = 0
        while previous < maximum:
            rows = _read_point_batch(connection, generation, previous, maximum)
            if not rows:
                raise ValueError("Track events disappeared during read")
            previous = rows[-1][0]
            for _, run_index, payload in rows:
                if not 1 <= run_index <= slots:
                    # Reserved/trimmed run slots are controlled by JSON metadata.
                    continue
                values = json.loads(payload)
                if not isinstance(values, dict):
                    raise ValueError("Invalid track point")
                target = tracks[run_index - 1]
                for key, value in values.items():
                    target.setdefault(key, []).append(value)
        return tracks


def append_point(task_dir: str, descriptor: dict[str, Any], run_index: int, payload: str) -> None:
    """Append once, retrying a transaction without releasing the task lock."""
    generation = _generation(descriptor)
    operation = uuid.uuid4().hex
    for attempt in range(5):
        try:
            with _connect(task_dir) as connection, connection:
                if connection.execute("SELECT 1 FROM generations WHERE generation=?", (generation,)).fetchone() is None:
                    raise ValueError("Referenced track generation is missing")
                connection.execute(
                    "INSERT OR IGNORE INTO points(generation,operation,run_index,payload) VALUES (?,?,?,?)",
                    (generation, operation, run_index, payload),
                )
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.05)
