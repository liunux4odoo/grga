"""Regression test for the `grga probe` / `grga search` orphan-process bug.

Before the fix, `subprocess.run(timeout=N)` killed only the rga parent
process.  rga's children — rga-preproc → (pandoc | pdftotext | …) —
survived as orphans, holding stdout pipes open and making the caller hang
indefinitely ("卡住无任何输出").

After the fix, `_run_checked` uses `start_new_session=True` +
`os.killpg(SIGKILL)` so the **entire** process group dies on timeout.

Design decision recorded in docs/design/plan-2026-09-08-rename-tools-check.md
section "probe 超时 orphan 进程修复".
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from grga.search import _run_checked, _kill_process_group


def _count_orphans(pattern: str) -> int:
    """Count running processes whose command line matches *pattern*."""
    result = subprocess.run(
        ["bash", "-c", f"pgrep -f '{pattern}'"],
        capture_output=True, text=True,
    )
    return len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0


def test_run_checked_kills_child_process_group_on_timeout(tmp_path):
    """_run_checked must kill the entire process group, not just the parent.

    We launch a bash process that:
      1. Spawns ``sleep 300`` as a child (simulating rga-preproc).
      2. Waits for it.

    After the timeout, the child ``sleep 300`` must NOT survive.
    """
    marker = tmp_path / f"grga_test_orphan_{os.getpid()}_{int(time.time())}"
    cmd_str = f"sleep 300 & echo $! > {marker}_pid; wait $!"
    cmd = ["bash", "-c", cmd_str]

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_checked(cmd, timeout=2)
    elapsed = time.monotonic() - start

    # Timeout should fire quickly (~2s), not wait for the 300s sleep
    assert elapsed < 6, f"Timeout took {elapsed:.1f}s, expected ~2s"

    # Give processes time to die after killpg
    time.sleep(0.5)

    # The child sleep process should be dead
    # (If only the parent was killed, sleep 300 would still be running)
    orphans = _count_orphans("sleep 300")
    assert orphans == 0, f"Orphaned child processes remain: {orphans} (should be 0)"


def test_run_checked_normal_completes_successfully(tmp_path):
    """Sanity: _run_checked works correctly for a normal (non-timeout) case."""
    result = _run_checked(
        ["bash", "-c", "echo hello"],
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_run_checked_timeout_propagates_correctly(tmp_path):
    """TimeoutExpired is re-raised so callers' existing handlers work."""
    with pytest.raises(subprocess.TimeoutExpired):
        _run_checked(["bash", "-c", "sleep 10"], timeout=1)


def test_kill_process_group_survives_already_dead():
    """_kill_process_group must not crash if process already exited."""
    proc = subprocess.Popen(
        ["true"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait(timeout=5)  # let it exit
    # Calling _kill_process_group on a dead process must not raise
    _kill_process_group(proc)
