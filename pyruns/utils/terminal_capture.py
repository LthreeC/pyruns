"""Cross-platform pseudoterminal capture for color-preserving shell logs."""

from __future__ import annotations

import errno
import os
import subprocess
import time
from typing import Any, Mapping, Sequence


_ESC = 0x1B


class _SgrOutputFilter:
    """Keep text and ANSI SGR colors while dropping screen-oriented controls."""

    def __init__(self) -> None:
        self._pending = b""
        self._sgr_needs_reset = False

    def feed(self, data: bytes) -> bytes:
        source = self._pending + bytes(data)
        self._pending = b""
        output = bytearray()
        index = 0
        while index < len(source):
            if source[index] != _ESC:
                output.append(source[index])
                index += 1
                continue
            if index + 1 >= len(source):
                self._pending = source[index:]
                break
            marker = source[index + 1]
            if marker == ord("["):
                end = index + 2
                while end < len(source) and not 0x40 <= source[end] <= 0x7E:
                    end += 1
                if end >= len(source):
                    self._pending = source[index:]
                    break
                if source[end] == ord("m"):
                    sequence = source[index : end + 1]
                    output.extend(sequence)
                    parameters = source[index + 2 : end]
                    self._sgr_needs_reset = parameters not in {b"", b"0"}
                index = end + 1
                continue
            if marker == ord("]"):
                end = index + 2
                while end < len(source):
                    if source[end] == 0x07:
                        end += 1
                        break
                    if (
                        source[end] == _ESC
                        and end + 1 < len(source)
                        and source[end + 1] == ord("\\")
                    ):
                        end += 2
                        break
                    end += 1
                else:
                    self._pending = source[index:]
                    break
                index = end
                continue
            index += 2
        return bytes(output)

    def finish(self) -> bytes:
        self._pending = b""
        if not self._sgr_needs_reset:
            return b""
        self._sgr_needs_reset = False
        return b"\x1b[0m"


class _WindowsPtyStdout:
    def __init__(self, process: Any) -> None:
        self._process = process
        self._filter = _SgrOutputFilter()

    def read1(self, size: int) -> bytes:
        while True:
            try:
                text = self._process.read(size)
            except (EOFError, OSError, ValueError):
                return self._filter.finish()
            if not text:
                if not self._process.isalive():
                    return self._filter.finish()
                time.sleep(0.01)
                continue
            filtered = self._filter.feed(str(text).encode("utf-8"))
            if filtered:
                return filtered


class _PosixPtyStdout:
    def __init__(self, fd: int) -> None:
        self._fd = int(fd)
        self._filter = _SgrOutputFilter()

    def read1(self, size: int) -> bytes:
        while True:
            try:
                data = os.read(self._fd, max(1, int(size)))
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return self._filter.finish()
                raise
            if not data:
                return self._filter.finish()
            filtered = self._filter.feed(data)
            if filtered:
                return filtered

    def close(self) -> None:
        if self._fd < 0:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = -1


class WindowsConPtyProcessAdapter:
    """Expose the Popen subset used by the task worker for pywinpty ConPTY."""

    def __init__(self, process: Any, args: Sequence[str]) -> None:
        self._process = process
        self.args = list(args)
        self.pid = int(process.pid)
        self.stdout = _WindowsPtyStdout(process)
        self._returncode: int | None = None
        self._output_closed = False

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def _read_exit_status(self) -> int:
        status = self._process.exitstatus
        return int(status) if status is not None else 1

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if self._process.isalive():
            return None
        self._returncode = self._read_exit_status()
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is not None:
            return self._returncode
        if timeout is None:
            self._process.wait()
        else:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while self._process.isalive():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                time.sleep(0.01)
        self._returncode = self._read_exit_status()
        return self._returncode

    def close_output(self) -> None:
        if self._output_closed:
            return
        self._output_closed = True
        try:
            self._process.close()
        except (EOFError, OSError, ValueError):
            pass


class PosixPtyProcessAdapter:
    """Expose a Popen process with its PTY master as captured stdout."""

    def __init__(self, process: subprocess.Popen[Any], master_fd: int) -> None:
        self._process = process
        self.args = process.args
        self.pid = int(process.pid)
        self.stdout = _PosixPtyStdout(master_fd)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return int(self._process.wait(timeout=timeout))

    def close_output(self) -> None:
        self.stdout.close()


def _terminal_dimensions(env: Mapping[str, str]) -> tuple[int, int]:
    def _bounded(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(str(env.get(name, default)))
        except (TypeError, ValueError):
            value = default
        return max(lower, min(value, upper))

    return (
        _bounded("LINES", 30, 10, 200),
        _bounded("COLUMNS", 160, 40, 500),
    )


def _terminal_env(env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    result.setdefault("TERM", "xterm-256color")
    result.setdefault("COLORTERM", "truecolor")
    return result


def _spawn_windows_conpty(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
) -> WindowsConPtyProcessAdapter:
    from winpty import Backend, PtyProcess

    process = PtyProcess.spawn(
        list(command),
        cwd=cwd,
        env=_terminal_env(env),
        dimensions=_terminal_dimensions(env),
        backend=Backend.ConPTY,
    )
    return WindowsConPtyProcessAdapter(process, command)


def _spawn_posix_pty(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
) -> PosixPtyProcessAdapter:
    import fcntl
    import pty
    import struct
    import termios

    rows, columns = _terminal_dimensions(env)
    master_fd, slave_fd = pty.openpty()
    try:
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )
        process = subprocess.Popen(
            list(command),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=_terminal_env(env),
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return PosixPtyProcessAdapter(process, master_fd)


def spawn_terminal_process(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
) -> WindowsConPtyProcessAdapter | PosixPtyProcessAdapter:
    """Spawn one color-preserving terminal child on Windows, Linux, or macOS."""

    if os.name == "nt":
        return _spawn_windows_conpty(command, cwd=cwd, env=env)
    return _spawn_posix_pty(command, cwd=cwd, env=env)
