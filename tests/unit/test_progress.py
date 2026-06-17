"""Tests for lib/progress.py — Progress class."""

from __future__ import annotations

import time

import pytest

from f5kb.lib.progress import Progress, _fmt_duration


# ── _fmt_duration ─────────────────────────────────────────────────────────────

def test_fmt_duration_seconds():
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(5.4) == "5s"
    assert _fmt_duration(59.9) == "60s"


def test_fmt_duration_minutes():
    assert _fmt_duration(60) == "1m00s"
    assert _fmt_duration(90) == "1m30s"
    assert _fmt_duration(3599) == "59m59s"


def test_fmt_duration_hours():
    assert _fmt_duration(3600) == "1h00m"
    assert _fmt_duration(7260) == "2h01m"


# ── Progress (non-TTY) ────────────────────────────────────────────────────────

class _Capture:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, s: str) -> None:
        self.lines.append(s)


def _make_progress(is_tty: bool = False) -> tuple[Progress, _Capture]:
    cap = _Capture()
    p = Progress(is_tty=is_tty, write=cap.write)
    return p, cap


def test_done_writes_line(noop_sleep):
    p, cap = _make_progress(is_tty=False)
    p.start("MyType", total=10)
    p.done("5/10 written articles -> out/")
    assert len(cap.lines) == 1
    assert "MyType" in cap.lines[0]
    assert "5/10 written articles -> out/" in cap.lines[0]
    assert cap.lines[0].endswith("\n")


def test_done_includes_total(noop_sleep):
    p, cap = _make_progress(is_tty=False)
    p.start("Support_Solution", total=100)
    p.done("ok")
    line = cap.lines[0]
    assert "Support_Solution" in line
    assert "100" in line


def test_second_update_suppressed_on_plain(noop_sleep):
    """In non-TTY mode, second update() within 2s interval is suppressed."""
    p, cap = _make_progress(is_tty=False)
    p.start("T", 5)
    p.update(1)  # first update always fires (_last_emit=0)
    first_count = len(cap.lines)
    p.update(2)  # within interval — should be suppressed
    assert len(cap.lines) == first_count


def test_tty_done_appends_newline():
    p, cap = _make_progress(is_tty=True)
    p.start("T", 5)
    p.done("3/5 written")
    # TTY: last write is "\n"
    assert cap.lines[-1] == "\n"


def test_tty_update_uses_carriage_return():
    p, cap = _make_progress(is_tty=True)
    p.start("T", 100)
    # Force emit by setting _last_emit to 0
    p._last_emit = 0.0
    p.update(50)
    assert any(s.startswith("\r") for s in cap.lines)


def test_active_false_after_done():
    p, _ = _make_progress()
    p.start("T", 1)
    assert p._active is True
    p.done()
    assert p._active is False


def test_update_noop_when_not_active():
    p, cap = _make_progress()
    p.update(5)  # called without start()
    assert cap.lines == []
