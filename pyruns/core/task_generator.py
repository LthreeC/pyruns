"""Task creation helpers for turning configs or shell scripts into disk-backed tasks."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from typing import Any, Dict, List

from pyruns._config import (
    RUN_LOGS_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TASK_KIND_TO_CONFIG_FILENAME,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.info_io import (
    load_script_info,
    save_task_info,
    validate_task_name,
    validate_tasks_root,
)
from pyruns.utils.shell_runtime import get_shell_config_filename_for_workspace
from pyruns.utils.task_files import (
    build_task_preview_and_search,
    is_known_task_kind,
    normalize_task_kind,
    write_task_payload,
)

logger = get_logger(__name__)

_TASK_NAME_LOCK_PREFIX = ".pyruns-create-"
_TASK_NAME_LOCK_SUFFIX = ".lock"


def _resolve_requested_task_kind(task_kind: str) -> str:
    """Normalize task-kind input and reject unsupported values."""

    requested_kind = str(task_kind or TASK_KIND_CONFIG).strip().lower()
    if not is_known_task_kind(requested_kind):
        raise ValueError(f"Unsupported task kind: {task_kind}")
    return normalize_task_kind(requested_kind)


def create_task_object(
    task_dir: str,
    name: str,
    *,
    task_kind: str = TASK_KIND_CONFIG,
    config: Dict[str, Any] | None = None,
    config_text: str = "",
    config_file: str | None = None,
) -> Dict[str, Any]:
    """Build the in-memory representation used by TaskManager and the UI."""

    normalized_kind = _resolve_requested_task_kind(task_kind)
    resolved_config_file = config_file or TASK_KIND_TO_CONFIG_FILENAME[normalized_kind]
    preview_text, search_text = build_task_preview_and_search(
        task_kind=normalized_kind,
        config=config or {},
        config_text=config_text,
        task_name=name,
    )
    return {
        "dir": task_dir,
        "name": name,
        "status": "pending",
        "config": config or {},
        "config_text": config_text if normalized_kind == TASK_KIND_SHELL else "",
        "config_file": resolved_config_file,
        "task_kind": normalized_kind,
        "log": "",
        "progress": 0.0,
        "created_at": get_now_str(),
        "env": {},
        "pinned": False,
        "start_times": [],
        "finish_times": [],
        "pids": [],
        "pid_create_times": [],
        "run_statuses": [],
        "durations": [],
        "exit_codes": [],
        "source_states": [],
        "records": [],
        "tracks": [],
        "notes": "",
        "preview_text": preview_text,
        "search_text": search_text,
    }


class TaskGenerator:
    """Create one or many task folders under the workspace tasks directory."""

    def __init__(self, root_dir: str | None = None):
        if root_dir is None:
            from pyruns._config import ROOT_DIR, TASKS_DIR

            root_dir = os.path.join(ROOT_DIR, TASKS_DIR)

        self.root_dir = root_dir
        validate_tasks_root(self.root_dir)
        os.makedirs(self.root_dir, exist_ok=True)
        validate_tasks_root(self.root_dir)

    @staticmethod
    def _path_identity(path: str) -> tuple[int, int] | None:
        """Return a stable directory identity for guarded rollback."""

        try:
            value = os.lstat(path)
        except FileNotFoundError:
            return None
        return int(value.st_dev), int(value.st_ino)

    @classmethod
    def task_directory_identity(cls, path: str) -> tuple[int, int] | None:
        """Return a task-directory identity for guarded cross-component moves."""

        return cls._path_identity(path)

    def _remove_private_task_dir(
        self,
        task_dir: str,
        *,
        expected_identity: tuple[int, int],
    ) -> bool:
        """Remove one direct child created by this generator without following links."""

        try:
            validate_tasks_root(self.root_dir)
        except ValueError:
            logger.warning("Refusing to clean through an unsafe tasks root: %s", self.root_dir)
            return False
        root = os.path.abspath(self.root_dir)
        target = os.path.abspath(task_dir)
        if os.path.normcase(os.path.dirname(target)) != os.path.normcase(root):
            logger.warning("Refusing to clean task path outside tasks root: %s", task_dir)
            return False
        if not os.path.lexists(target):
            return True

        identity = self._path_identity(target)
        if identity != expected_identity:
            logger.warning("Refusing to clean task path whose identity changed: %s", task_dir)
            return False

        try:
            is_junction = getattr(os.path, "isjunction", lambda _path: False)
            if os.path.islink(target):
                os.unlink(target)
            elif is_junction(target):
                os.rmdir(target)
            elif identity is not None and stat.S_ISDIR(os.lstat(target).st_mode):
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except FileNotFoundError:
            return True
        return not os.path.lexists(target)

    def _cleanup_private_task_dir(
        self,
        task_dir: str,
        *,
        expected_identity: tuple[int, int],
    ) -> None:
        """Best-effort cleanup that preserves the original creation failure."""

        try:
            removed = self._remove_private_task_dir(
                task_dir,
                expected_identity=expected_identity,
            )
        except OSError as exc:
            logger.warning("Could not clean incomplete task directory %s: %s", task_dir, exc)
            return
        if not removed:
            logger.warning("Could not safely clean incomplete task directory %s", task_dir)

    def _task_name_lock_path(self, task_name: str) -> str:
        return os.path.join(
            self.root_dir,
            f"{_TASK_NAME_LOCK_PREFIX}{task_name}{_TASK_NAME_LOCK_SUFFIX}",
        )

    def _try_reserve_task_name(
        self,
        task_name: str,
    ) -> tuple[str, int, tuple[int, int]] | None:
        """Atomically reserve one candidate name across Pyruns processes."""

        validate_tasks_root(self.root_dir)
        lock_path = self._task_name_lock_path(task_name)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(lock_path, flags, 0o600)
        except FileExistsError:
            return None

        identity: tuple[int, int] | None = None
        try:
            lock_stat = os.fstat(fd)
            identity = int(lock_stat.st_dev), int(lock_stat.st_ino)
            payload = f"pid={os.getpid()}\n".encode("ascii")
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError(f"Could not write task name reservation: {lock_path}")
                offset += written
            os.fsync(fd)
            return lock_path, fd, identity
        except BaseException:
            try:
                os.close(fd)
            finally:
                if identity is not None and self._path_identity(lock_path) == identity:
                    try:
                        os.unlink(lock_path)
                    except OSError:
                        pass
            raise

    def _reservation_is_owned(
        self,
        reservation: tuple[str, int, tuple[int, int]],
    ) -> bool:
        lock_path, _fd, identity = reservation
        try:
            validate_tasks_root(self.root_dir)
            return self._path_identity(lock_path) == identity
        except (OSError, ValueError):
            return False

    def _release_task_name_reservation(
        self,
        reservation: tuple[str, int, tuple[int, int]],
    ) -> None:
        """Release only the reservation file opened by this generator."""

        lock_path, fd, identity = reservation
        try:
            os.close(fd)
        except OSError as exc:
            logger.warning("Could not close task name reservation %s: %s", lock_path, exc)

        try:
            validate_tasks_root(self.root_dir)
            if self._path_identity(lock_path) != identity:
                logger.warning("Refusing to remove replaced task reservation: %s", lock_path)
                return
            os.unlink(lock_path)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("Could not remove task name reservation %s: %s", lock_path, exc)

    def reserve_exact_task_name(
        self,
        task_name: str,
    ) -> tuple[str, int, tuple[int, int]] | None:
        """Reserve one exact final task name without choosing a suffix.

        Restore uses the same reservation namespace as normal task creation so
        two Pyruns processes cannot both publish the same direct child under
        ``tasks/``. The caller must release a successful reservation.
        """

        name_error = validate_task_name(task_name)
        if name_error:
            raise ValueError(name_error)
        validate_tasks_root(self.root_dir)
        task_dir = os.path.join(self.root_dir, task_name)
        if os.path.lexists(task_dir):
            return None
        reservation = self._try_reserve_task_name(task_name)
        if reservation is None:
            return None
        if os.path.lexists(task_dir):
            self._release_task_name_reservation(reservation)
            return None
        return reservation

    def owns_task_name_reservation(
        self,
        reservation: tuple[str, int, tuple[int, int]],
    ) -> bool:
        """Return whether a previously acquired exact-name reservation is intact."""

        return self._reservation_is_owned(reservation)

    def release_task_name_reservation(
        self,
        reservation: tuple[str, int, tuple[int, int]],
    ) -> None:
        """Release a reservation returned by :meth:`reserve_exact_task_name`."""

        self._release_task_name_reservation(reservation)

    def _resolve_script_path(self) -> str:
        """Best-effort lookup of the source script from workspace metadata."""

        workspace_dir = os.path.dirname(self.root_dir)
        script_info = load_script_info(workspace_dir)
        script_path = str(script_info.get("script_path", "") or "")
        return script_path if script_path and os.path.exists(script_path) else ""

    @staticmethod
    def _clean_task_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """Remove UI-only metadata before persisting ``config.yaml``."""

        return {
            key: value
            for key, value in (config or {}).items()
            if not str(key).startswith("_meta")
        }

    def create_task(
        self,
        name_prefix: str,
        config: Dict[str, Any] | None = None,
        *,
        exact_name: bool = False,
        config_text: str = "",
        group_index: str = "",
        task_kind: str = TASK_KIND_CONFIG,
        config_file: str | None = None,
        task_metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create one task folder with task metadata, task payload, and ``run_logs/``."""

        timestamp = get_now_str()
        base_name = name_prefix.strip() if name_prefix else ""
        if not base_name:
            base_name = f"task_{timestamp}"

        base_folder_name = f"{base_name}_{group_index}" if group_index else base_name
        name_error = validate_task_name(base_folder_name)
        if name_error:
            raise ValueError(name_error)

        normalized_kind = _resolve_requested_task_kind(task_kind)
        resolved_config_file = config_file or TASK_KIND_TO_CONFIG_FILENAME[normalized_kind]
        clean_config = self._clean_task_config(config or {})
        clean_config_text = str(config_text or "")
        metadata = dict(task_metadata or {})
        script_path = self._resolve_script_path()

        validate_tasks_root(self.root_dir)
        attempt = 0
        while True:
            if attempt == 0:
                folder_name = base_folder_name
            elif attempt == 1:
                folder_name = f"{base_folder_name}_{int(time.time() * 1000)}"
            else:
                folder_name = f"{base_folder_name}_{int(time.time() * 1000)}_{attempt - 1}"

            name_error = validate_task_name(folder_name)
            if name_error:
                raise ValueError(name_error)
            task_dir = os.path.join(self.root_dir, folder_name)
            reservation = self._try_reserve_task_name(folder_name)
            if reservation is None:
                if exact_name:
                    raise ValueError(
                        f"Task name '{base_folder_name}' already exists or is being created"
                    )
                attempt += 1
                continue

            staging_dir = ""
            staging_identity: tuple[int, int] | None = None
            try:
                if os.path.lexists(task_dir):
                    if exact_name:
                        raise ValueError(
                            f"Task name '{base_folder_name}' already exists or is being created"
                        )
                    attempt += 1
                    continue

                staging_dir = tempfile.mkdtemp(prefix=".pyruns-task-", dir=self.root_dir)
                staging_identity = self._path_identity(staging_dir)
                if staging_identity is None:
                    raise OSError(f"Could not verify private task directory: {staging_dir}")
                task_obj = create_task_object(
                    task_dir,
                    folder_name,
                    task_kind=normalized_kind,
                    config=clean_config,
                    config_text=clean_config_text,
                    config_file=resolved_config_file,
                )
                task_obj.update(metadata)

                task_info: Dict[str, Any] = {
                    "name": task_obj["name"],
                    "status": task_obj["status"],
                    "progress": task_obj["progress"],
                    "created_at": task_obj["created_at"],
                    "pinned": task_obj["pinned"],
                    "task_kind": normalized_kind,
                    "config_file": resolved_config_file,
                    "start_times": [],
                    "finish_times": [],
                    "pids": [],
                    "durations": [],
                    "exit_codes": [],
                    "source_states": [],
                    "records": [],
                    "tracks": [],
                }
                task_info.update(metadata)
                if script_path:
                    task_info["script"] = script_path

                save_task_info(staging_dir, task_info)
                write_task_payload(
                    staging_dir,
                    task_kind=normalized_kind,
                    config_file=resolved_config_file,
                    config=clean_config,
                    config_text=clean_config_text,
                )
                os.makedirs(os.path.join(staging_dir, RUN_LOGS_DIR), exist_ok=False)
                validate_tasks_root(self.root_dir)
                if self._path_identity(staging_dir) != staging_identity:
                    raise OSError(f"Private task directory identity changed: {staging_dir}")
                if not self._reservation_is_owned(reservation):
                    if exact_name:
                        raise ValueError(
                            f"Task name '{base_folder_name}' already exists or is being created"
                        )
                    attempt += 1
                    continue

                # The final task name remains absent until every required artifact exists.
                # The exclusive name reservation prevents another Pyruns creator from
                # introducing this target during the final check/rename interval.
                if os.path.lexists(task_dir):
                    if exact_name:
                        raise ValueError(
                            f"Task name '{base_folder_name}' already exists or is being created"
                        )
                    attempt += 1
                    continue
                try:
                    os.rename(staging_dir, task_dir)
                except OSError as exc:
                    if os.path.lexists(task_dir):
                        if exact_name:
                            raise ValueError(
                                f"Task name '{base_folder_name}' already exists or is being created"
                            ) from exc
                        attempt += 1
                        continue
                    raise
                staging_dir = ""

                logger.debug("Created task '%s' at %s", folder_name, task_dir)
                return task_obj
            finally:
                if staging_dir and staging_identity is not None:
                    self._cleanup_private_task_dir(
                        staging_dir,
                        expected_identity=staging_identity,
                    )
                self._release_task_name_reservation(reservation)

    def create_tasks(
        self,
        configs: List[Dict[str, Any]],
        name_prefix: str,
        task_kind: str = TASK_KIND_CONFIG,
    ) -> List[Dict[str, Any]]:
        """Create many config tasks, appending ``[i-of-n]`` suffixes when needed."""

        total = len(configs)
        normalized_kind = _resolve_requested_task_kind(task_kind)
        tasks: List[Dict[str, Any]] = []
        created: List[tuple[str, tuple[int, int]]] = []
        try:
            for index, config in enumerate(configs, start=1):
                group_index = f"[{index}-of-{total}]" if total > 1 else ""
                task = self.create_task(
                    name_prefix,
                    config,
                    group_index=group_index,
                    task_kind=normalized_kind,
                )
                tasks.append(task)
                identity = self._path_identity(task["dir"])
                if identity is None:
                    raise OSError(f"Could not verify created task directory: {task['dir']}")
                created.append((task["dir"], identity))
        except BaseException:
            for task_dir, identity in reversed(created):
                self._cleanup_private_task_dir(
                    task_dir,
                    expected_identity=identity,
                )
            raise
        return tasks

    def create_shell_task(
        self,
        name_prefix: str,
        shell_text: str,
        *,
        exact_name: bool = False,
        command_mode: str = 'shell',
        command_argv: List[str] | None = None,
        workdir: str | None = None,
        shell_executable: str | None = None,
        shell_kind: str | None = None,
        env: Dict[str, str] | None = None,
        script_path: str | None = None,
    ) -> Dict[str, Any]:
        """Create a single shell task backed by one shell-native payload file."""

        config_file = get_shell_config_filename_for_workspace(os.path.dirname(self.root_dir))
        metadata: Dict[str, Any] = {
            'command_mode': str(command_mode or 'shell'),
            'env': {str(key): str(value) for key, value in (env or {}).items()},
        }
        if command_argv is not None:
            metadata['cmd'] = [str(part) for part in command_argv]
        if workdir:
            metadata['workdir'] = os.path.abspath(workdir)
        if shell_executable:
            metadata['shell_executable'] = str(shell_executable)
        if shell_kind:
            metadata['shell_kind'] = str(shell_kind)
        if script_path:
            metadata['script'] = os.path.abspath(script_path)

        return self.create_task(
            name_prefix,
            config=None,
            exact_name=exact_name,
            config_text=shell_text,
            task_kind=TASK_KIND_SHELL,
            config_file=config_file,
            task_metadata=metadata,
        )
