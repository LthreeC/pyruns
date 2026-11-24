"""Submit CLI task runs to a process that outlives the invoking command."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from pyruns._config import ENV_KEY_CLI_SHELL_EXECUTABLE
from pyruns.cli.submission_protocol import (
    SUBMITTER_ABORT_TIMEOUT_SEC,
    TERMINAL_RECEIPT_STATUSES,
    SubmissionReceipt,
    read_submission_receipt,
    remove_control_file,
    submission_control_paths,
    submission_payload_path,
    write_abort_request,
    write_submission_payload,
)
from pyruns.utils import get_logger
from pyruns.utils.process_utils import get_process_create_time, kill_process
from pyruns.utils.shell_runtime import get_follow_shell_runtime
from pyruns.update_release import current_release_root, module_command


SubmissionStatus = Literal[
    "accepted",
    "accepted_partial",
    "partial",
    "rejected",
    "aborted",
    "unresolved",
]
DEFAULT_STARTUP_TIMEOUT_SEC = 15.0
DEFAULT_ABORT_TIMEOUT_SEC = SUBMITTER_ABORT_TIMEOUT_SEC
_POLL_INTERVAL_SEC = 0.05
_MAX_POLL_INTERVAL_SEC = 1.0
_MAX_STARTUP_POLL_INTERVAL_SEC = 0.25
_EXITED_LAUNCHER_GRACE_SEC = 1.0
logger = get_logger(__name__)


@dataclass(frozen=True)
class SubmissionResult:
    """Verified ownership outcome for one submitted batch."""

    status: SubmissionStatus
    claimed: tuple[str, ...]
    unclaimed: tuple[str, ...]
    runner_id: str | None = None
    attempt_id: str | None = None


class SubmissionInterrupted(KeyboardInterrupt):
    """Ctrl+C raised after a runner process may have claimed submitted tasks."""

    def __init__(
        self,
        result: SubmissionResult,
        submission_token: str,
        runner_pid: int,
    ) -> None:
        super().__init__("task submission interrupted")
        self.result = result
        self.submission_token = submission_token
        self.runner_pid = runner_pid


@dataclass(frozen=True)
class _AbortOutcome:
    result: SubmissionResult
    interrupted: bool
    runner_pid: int | None = None


def _submission_result(
    names: list[str] | tuple[str, ...],
    *,
    status: SubmissionStatus,
    claimed: list[str] | tuple[str, ...] = (),
) -> SubmissionResult:
    claimed_set = {str(name) for name in claimed}
    accepted = tuple(name for name in names if name in claimed_set)
    unclaimed = tuple(name for name in names if name not in claimed_set)
    return SubmissionResult(status=status, claimed=accepted, unclaimed=unclaimed)


def _result_from_receipt(
    receipt: SubmissionReceipt,
    *,
    runner_id: str | None = None,
    attempt_id: str | None = None,
) -> SubmissionResult:
    return SubmissionResult(
        status=cast(SubmissionStatus, receipt.status),
        claimed=receipt.claimed,
        unclaimed=receipt.unclaimed,
        runner_id=runner_id,
        attempt_id=attempt_id,
    )


def _terminal_result_from_receipt(
    receipt: SubmissionReceipt | None,
    *,
    token: str,
) -> SubmissionResult | None:
    if receipt is None or receipt.status not in TERMINAL_RECEIPT_STATUSES:
        return None
    runner_id = None
    if receipt.status in {"accepted", "accepted_partial"}:
        runner_id = f"{socket.gethostname().lower()}:{receipt.runner_pid}:{token}"
    return _result_from_receipt(receipt, runner_id=runner_id, attempt_id=token if runner_id else None)


def _detached_popen(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    package_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "cwd": package_root,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _read_receipt(
    receipt_file: str,
    *,
    token: str,
    names: list[str],
    run_indices: list[int],
) -> SubmissionReceipt | None:
    return read_submission_receipt(
        receipt_file,
        token=token,
        names=names,
        run_indices=run_indices,
    )


def _abort_submission(
    process: subprocess.Popen,
    *,
    process_create_time: float | None,
    receipt_file: str,
    abort_file: str,
    token: str,
    names: list[str],
    run_indices: list[int],
    reason: str,
    expected_runner_pid: int | None = None,
) -> _AbortOutcome:
    """Request cleanup, then verify a receipt or terminate the runner tree."""

    interrupted = False
    abort_written = False
    known_claimed: list[str] = []
    known_claimed_set: set[str] = set()
    runner_pid = expected_runner_pid
    runner_create_time = (
        process_create_time
        if runner_pid == process.pid
        else get_process_create_time(runner_pid)
        if runner_pid is not None
        else None
    )
    launcher_exit_deadline: float | None = None

    def observe_receipt(receipt: SubmissionReceipt | None) -> SubmissionResult | None:
        nonlocal runner_pid, runner_create_time
        if receipt is None:
            return None

        if runner_pid is None:
            runner_pid = receipt.runner_pid
            runner_create_time = (
                process_create_time
                if runner_pid == process.pid
                else get_process_create_time(runner_pid)
            )
        elif receipt.runner_pid != runner_pid:
            return _submission_result(
                names,
                status="unresolved",
                claimed=known_claimed,
            )

        for name in receipt.claimed:
            if name not in known_claimed_set:
                known_claimed_set.add(name)
                known_claimed.append(name)

        if receipt.status not in TERMINAL_RECEIPT_STATUSES or receipt.status in {"accepted", "accepted_partial"}:
            return None

        # A later receipt may never forget ownership already reported by this runner.
        # If it does, cleanup of the missing tasks is not proven.
        if set(receipt.claimed) != known_claimed_set:
            return _submission_result(
                names,
                status="unresolved",
                claimed=known_claimed,
            )
        return _result_from_receipt(receipt)

    while not abort_written:
        try:
            write_abort_request(abort_file, token=token, reason=reason)
            abort_written = True
        except KeyboardInterrupt:
            interrupted = True
        except OSError:
            break

    deadline = time.monotonic() + max(0.05, DEFAULT_ABORT_TIMEOUT_SEC)
    poll_interval = _POLL_INTERVAL_SEC
    previous_receipt: SubmissionReceipt | None = None
    previous_process_exited = False
    while abort_written and time.monotonic() < deadline:
        try:
            receipt = _read_receipt(
                receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
            )
            terminal_result = observe_receipt(receipt)
            if terminal_result is not None:
                return _AbortOutcome(terminal_result, interrupted, runner_pid)

            process_exited = process.poll() is not None
            if process_exited:
                if runner_pid == process.pid:
                    break
                if runner_pid is None:
                    if launcher_exit_deadline is None:
                        launcher_exit_deadline = (
                            time.monotonic() + _EXITED_LAUNCHER_GRACE_SEC
                        )
                    elif time.monotonic() >= launcher_exit_deadline:
                        break
            progressed = (
                receipt != previous_receipt
                or process_exited != previous_process_exited
            )
            previous_receipt = receipt
            previous_process_exited = process_exited
            time.sleep(poll_interval)
            poll_interval = (
                _POLL_INTERVAL_SEC
                if progressed
                else min(_MAX_POLL_INTERVAL_SEC, poll_interval * 1.5)
            )
        except KeyboardInterrupt:
            interrupted = True

    try:
        receipt = _read_receipt(
            receipt_file,
            token=token,
            names=names,
            run_indices=run_indices,
        )
        terminal_result = observe_receipt(receipt)
        if terminal_result is not None:
            return _AbortOutcome(terminal_result, interrupted, runner_pid)
    except KeyboardInterrupt:
        interrupted = True

    target_pid = runner_pid if runner_pid is not None else process.pid
    target_create_time = (
        runner_create_time if runner_pid is not None else process_create_time
    )
    if (
        (target_pid != process.pid and target_create_time is None)
        or (target_pid == process.pid and process.poll() is not None)
    ):
        return _AbortOutcome(
            _submission_result(
                names,
                status="unresolved",
                claimed=known_claimed,
            ),
            interrupted,
            runner_pid,
        )

    while True:
        try:
            kill_process(
                target_pid,
                expected_create_time=target_create_time,
            )
        except KeyboardInterrupt:
            interrupted = True
            continue
        except Exception:
            pass
        break

    return _AbortOutcome(
        _submission_result(names, status="unresolved", claimed=known_claimed),
        interrupted,
        runner_pid,
    )


def submit_cli_tasks(
    tm,
    task_names: list[str],
    *,
    expected_runs: dict[str, int],
    max_workers: int | None = 1,
    startup_timeout: float | None = None,
    gpu_wait_rule: str | None = None,
    web_batch: bool = False,
    task_identities: dict[str, str] | None = None,
) -> SubmissionResult:
    """Start a hidden runner and wait for its exact ownership receipt."""

    names = [str(name) for name in task_names if str(name)]
    if not names or len(names) != len(set(names)):
        return _submission_result(names, status="rejected")
    if (
        not isinstance(expected_runs, dict)
        or set(expected_runs) != set(names)
        or any(type(expected_runs[name]) is not int or expected_runs[name] <= 0 for name in names)
    ):
        return _submission_result(names, status="rejected")
    run_indices = [expected_runs[name] for name in names]

    known_names = {
        str(task.get("name"))
        for task in getattr(tm, "tasks", [])
        if task and str(task.get("name", ""))
    }
    if any(name not in known_names for name in names):
        return _submission_result(names, status="rejected")

    workspace = os.path.dirname(os.path.abspath(str(tm.tasks_dir)))
    submission_token = uuid.uuid4().hex
    receipt_file, abort_file = submission_control_paths(str(tm.tasks_dir), submission_token)
    payload_file = submission_payload_path(str(tm.tasks_dir), submission_token)
    command = module_command(
        "pyruns.cli.detached_runner",
        "--workspace",
        workspace,
        "--jobs",
        str(max(1, int(max_workers)) if max_workers is not None else 0),
        "--submission-token",
        submission_token,
        release=current_release_root(),
    )
    if web_batch:
        command.append("--web-batch")

    env = os.environ.copy()
    # A CLI process may have been launched by a Python test runner, an IDE,
    # or another wrapper whose process ancestry does not expose the user's
    # actual shell.  Shell tasks already persist the resolved executable at
    # creation time, so prefer that value when handing work to the detached
    # runner.  Falling back to ancestry detection keeps older tasks and
    # Python workspaces compatible.
    shell_executables = {
        str(task.get("shell_executable", "") or "").strip()
        for task in getattr(tm, "tasks", [])
        if task and str(task.get("name", "")) in names
    }
    shell_executables.discard("")
    if len(shell_executables) == 1:
        shell_executable = next(iter(shell_executables))
    else:
        runtime = get_follow_shell_runtime()
        shell_executable = str(runtime.get("executable", "") or "").strip()
    if shell_executable:
        env[ENV_KEY_CLI_SHELL_EXECUTABLE] = shell_executable

    process: subprocess.Popen | None = None
    process_create_time: float | None = None
    outcome: SubmissionResult | None = None
    outcome_runner_pid: int | None = None
    interrupted = False
    try:
        try:
            write_submission_payload(
                payload_file,
                token=submission_token,
                names=names,
                run_indices=run_indices,
                gpu_wait_rule=gpu_wait_rule,
                **({"task_identities": task_identities} if task_identities is not None else {}),
            )
        except (OSError, ValueError):
            remove_control_file(payload_file)
            return _submission_result(names, status="rejected")
        try:
            process = _detached_popen(command, env)
        except OSError:
            remove_control_file(payload_file)
            return _submission_result(names, status="rejected")

        process_create_time = get_process_create_time(process.pid)
        timeout_seconds = (
            DEFAULT_STARTUP_TIMEOUT_SEC
            if startup_timeout is None
            else float(startup_timeout)
        )
        deadline = time.monotonic() + max(0.05, timeout_seconds)
        observed_runner_pid: int | None = None
        process_exit_deadline: float | None = None
        poll_interval = _POLL_INTERVAL_SEC
        previous_receipt: SubmissionReceipt | None = None
        previous_process_exited = False
        while True:
            abort_reason = None
            receipt = _read_receipt(
                receipt_file,
                token=submission_token,
                names=names,
                run_indices=run_indices,
            )
            if receipt is not None:
                if observed_runner_pid is None:
                    observed_runner_pid = receipt.runner_pid
                elif receipt.runner_pid != observed_runner_pid:
                    abort_reason = "runner_identity_changed"

            if abort_reason is None:
                terminal_result = _terminal_result_from_receipt(receipt, token=submission_token)
                if terminal_result is not None and receipt is not None:
                    outcome_runner_pid = receipt.runner_pid
                    outcome = terminal_result
                    break

            now = time.monotonic()
            process_exited = process.poll() is not None
            if abort_reason is None and process_exited:
                if process_exit_deadline is None:
                    process_exit_deadline = now + _EXITED_LAUNCHER_GRACE_SEC
                elif now >= process_exit_deadline:
                    abort_reason = (
                        "runner_exited_before_receipt"
                        if observed_runner_pid is not None else "launcher_exited_before_receipt"
                    )
            if abort_reason is None and now >= deadline:
                abort_reason = "startup_timeout"
            if abort_reason is not None:
                if web_batch:
                    # The HTTP wait and the detached experiment have independent
                    # lifetimes. An unreadable receipt does not authorize a stop.
                    logger.warning("Could not confirm web task start for runner %s (%s)",
                                   observed_runner_pid or process.pid, abort_reason)
                    known_receipt = receipt if receipt and receipt.runner_pid == observed_runner_pid else previous_receipt
                    outcome = _submission_result(
                        names, status="unresolved", claimed=known_receipt.claimed if known_receipt else (),
                    )
                    break
                aborted = _abort_submission(
                    process,
                    process_create_time=process_create_time,
                    receipt_file=receipt_file,
                    abort_file=abort_file,
                    token=submission_token,
                    names=names,
                    run_indices=run_indices,
                    reason=abort_reason,
                    expected_runner_pid=observed_runner_pid,
                )
                outcome = aborted.result
                outcome_runner_pid = aborted.runner_pid or process.pid
                interrupted = aborted.interrupted
                break
            progressed = (
                receipt != previous_receipt
                or process_exited != previous_process_exited
            )
            previous_receipt = receipt
            previous_process_exited = process_exited
            time.sleep(poll_interval)
            poll_interval = (
                _POLL_INTERVAL_SEC
                if progressed
                else min(_MAX_STARTUP_POLL_INTERVAL_SEC, poll_interval * 1.5)
            )

        if outcome is None:
            outcome = _submission_result(names, status="unresolved")
        if outcome.status != "unresolved":
            remove_control_file(payload_file)
            remove_control_file(receipt_file)
            remove_control_file(abort_file)
        if interrupted:
            raise SubmissionInterrupted(
                outcome,
                submission_token,
                outcome_runner_pid or process.pid,
            )
        return outcome
    except SubmissionInterrupted:
        raise
    except KeyboardInterrupt:
        if process is None:
            raise
        while process_create_time is None:
            try:
                process_create_time = get_process_create_time(process.pid)
                break
            except KeyboardInterrupt:
                continue
        aborted = _abort_submission(
            process,
            process_create_time=process_create_time,
            receipt_file=receipt_file,
            abort_file=abort_file,
            token=submission_token,
            names=names,
            run_indices=run_indices,
            reason="interrupted",
        )
        if aborted.result.status != "unresolved":
            remove_control_file(payload_file)
            remove_control_file(receipt_file)
            remove_control_file(abort_file)
        raise SubmissionInterrupted(
            aborted.result,
            submission_token,
            aborted.runner_pid or process.pid,
        ) from None
