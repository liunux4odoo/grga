"""Exit-code contract tests (Step 3.3).

The exit-code table is a frozen public contract. ``test_exit_codes_frozen``
compares the live constants against a checked-in lock file so any change
fails loudly and must be an intentional (breaking) edit.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from grga import exit_codes
from grga.exit_codes import CODE_TO_NAME, emit_error, emit_warning

LOCK_FILE = Path(__file__).resolve().parent / "fixtures" / "exit_codes.locked.json"


def _live_codes():
    return {name: value for name, value in vars(exit_codes).items() if name.startswith("EXIT_")}


def test_exit_codes_frozen():
    locked = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    assert _live_codes() == locked, (
        "Exit-code contract changed. This is a BREAKING change; update "
        "tests/fixtures/exit_codes.locked.json intentionally."
    )


def test_code_to_name_covers_all_constants():
    assert set(CODE_TO_NAME) == set(_live_codes().values())


def test_emit_error_human_mode_uses_stderr():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = emit_error(4, "missing_tools", "msg", json_mode=False)
    assert code == 4
    assert out.getvalue() == ""
    assert "error [missing_tools]: msg" in err.getvalue()


def test_emit_error_json_mode_payload():
    out = io.StringIO()
    with redirect_stdout(out):
        code = emit_error(4, "missing_tools", "msg", json_mode=True, details={"missing": ["rg"]})
    assert code == 4
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert payload["error"] == "missing_tools"
    assert payload["error_name"] == "missing_tool"
    assert payload["exit_code"] == 4
    assert payload["message"] == "msg"
    assert payload["missing"] == ["rg"]


def test_emit_error_unknown_code_falls_back_to_generic():
    out = io.StringIO()
    with redirect_stdout(out):
        emit_error(99, "weird", "x", json_mode=True)
    assert json.loads(out.getvalue())["error_name"] == "generic"


def test_emit_warning_json_mode_uses_stderr():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        emit_warning("insecure", "no verify", json_mode=True)
    assert out.getvalue() == ""
    assert json.loads(err.getvalue())["warning"] == "insecure"


def test_emit_warning_human_mode():
    err = io.StringIO()
    with redirect_stderr(err):
        emit_warning("insecure", "no verify", json_mode=False)
    assert "warning [insecure]: no verify" in err.getvalue()
