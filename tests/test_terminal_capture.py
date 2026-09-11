"""Streaming log regressions; these tests never launch a native console."""

from unittest.mock import MagicMock

import pytest

from pyruns.utils.terminal_capture import (
    _ConPtyOutputFilter,
    _SgrOutputFilter,
    _WindowsPtyStdout,
)


def erase_screen(rows):
    return b"\x1b[K\r\n" * (rows - 1) + b"\x1b[K\x1b[H"


def render(output_filter, chunks):
    result = b"".join(output_filter.feed(chunk) for chunk in chunks)
    result += output_filter.finish()
    assert output_filter.finish() == b""
    return result


@pytest.mark.parametrize("rows", [10, 30, 60, 200])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 8192])
def test_conpty_conda_screen_handoff(rows, chunk_size):
    # Captured ConPTY/Conda sequence: erase rows, return home, show cursor.
    sweep = b"\x1b[?25l" + erase_screen(rows) + b"\x1b[?25h"
    body = "\r\n\x1b[32m运行完成\x1b[0m\r\n\r\nend-marker".encode()
    raw = (
        b"\x1b[2J\x1b[H\x1b]0;PowerShell\x07" + sweep + body
        + b"\x1b[8;1H\x1b[H" + sweep + b"footer\r\n"
    )
    chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
    assert render(_ConPtyOutputFilter(rows), chunks) == (
        body + b"\r\nfooter\r\n"
    )


@pytest.mark.parametrize("ending", [b"", b"\r", b"\r\n", b"\n", b"\n\n"])
def test_conpty_handoff_separates_text_without_adding_blank_lines(ending):
    body = b"result" + ending
    raw = body + erase_screen(30) + b"footer"
    separator = b"" if b"\n" in ending else b"\r\n"
    assert render(_ConPtyOutputFilter(30), [raw]) == (
        body + separator + b"footer"
    )


def test_conpty_handoff_at_every_read_boundary():
    raw = erase_screen(10) + b"body" + erase_screen(10) + b"footer"
    for split in range(len(raw) + 1):
        assert render(_ConPtyOutputFilter(10), [raw[:split], raw[split:]]) == (
            b"body\r\nfooter"
        )


@pytest.mark.parametrize("chunk_size", [1, 7, 8192])
@pytest.mark.parametrize("raw", [
    b"\r\n" * 100 + b"body\r\n" + b"\r\n" * 100,
    b"\x1b[K\r\n" * 29 + b"body\r\n",  # No final cursor return.
    erase_screen(29),  # A different height is not a full-screen handoff.
    b"\x1b[K\r\n" * 15 + b"body" + b"\x1b[K\r\n" * 15,
    b"\x1b[K\r\n" * 29 + b"\x1b[K\x1b[",  # Incomplete at EOF.
    b"\x1b[K\r\n" * 29 + b"\x1b[K\x1b[2H",  # Different cursor target.
    "\x1b[31m第一行\r进度 50%\r进度 100%\n\n".encode(),
    b"\x1b]0;title\x1b\\text\x1b[38;2;1;2;3mcolor",
])
def test_conpty_preserves_blank_lines_and_nonmatching_output(raw, chunk_size):
    chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
    expected = render(_SgrOutputFilter(), [raw])
    assert render(_SgrOutputFilter(), chunks) == expected
    assert render(_ConPtyOutputFilter(30), chunks) == expected


def test_conpty_streams_plain_output_and_bounds_handoff_buffer():
    output_filter = _ConPtyOutputFilter(200)
    line = b"epoch=1 loss=0.125\r\n" * 1000
    for _ in range(100):
        assert output_filter.feed(line) == line
        assert not output_filter._candidate
    sweep = erase_screen(200)
    for byte in sweep[:-1]:
        assert output_filter.feed(bytes([byte])) == b""
        assert len(output_filter._candidate) < len(sweep)
    assert output_filter.feed(sweep[-1:]) == b""
    assert not output_filter._candidate
    assert output_filter.feed(line) == line


@pytest.mark.parametrize("eof", [EOFError, OSError, ValueError, None])
def test_windows_stdout_skips_redraws_without_reporting_premature_eof(eof):
    process = MagicMock()
    process.read.side_effect = [
        "", erase_screen(30).decode(), "\x1b[31mready\r\n",
        erase_screen(30).decode(), eof() if eof else "",
    ]
    process.isalive.side_effect = [True, False]
    stdout = _WindowsPtyStdout(process, 30)
    assert stdout.read1(8192) == b"\x1b[31mready\r\n"
    assert stdout.read1(8192) == b"\x1b[0m"
    process.read.side_effect = EOFError
    assert stdout.read1(8192) == b""
