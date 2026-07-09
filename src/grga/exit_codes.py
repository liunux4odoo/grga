"""Stable exit codes and structured error emission for grga CLI.

The contract is frozen as of 0.2.0 — codes may only be **added** in future
versions, never removed or renumbered. Downstream Agents must be able to
switch on these codes to build reliable pipelines.

Human-readable errors go to *stderr*; ``--json`` errors go to *stdout* as a
single JSON object so pipelines can `json.load(sys.stdin)`.
"""

import json
import sys
from typing import Any, Dict, Optional

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_USER_ABORT = 3
EXIT_MISSING_TOOL = 4
EXIT_OFFLINE = 5
EXIT_PARTIAL_INSTALL = 6
EXIT_DOWNLOAD_FAIL = 7
EXIT_VERIFY_FAIL = 8
EXIT_IO = 9

CODE_TO_NAME = {
    EXIT_OK: "ok",
    EXIT_GENERIC: "generic",
    EXIT_USAGE: "usage",
    EXIT_USER_ABORT: "user_abort",
    EXIT_MISSING_TOOL: "missing_tool",
    EXIT_OFFLINE: "offline",
    EXIT_PARTIAL_INSTALL: "partial_install",
    EXIT_DOWNLOAD_FAIL: "download_fail",
    EXIT_VERIFY_FAIL: "verify_fail",
    EXIT_IO: "io",
}


def emit_error(
    code: int,
    kind: str,
    message: str,
    *,
    json_mode: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """Emit a structured error and return the exit code.

    ``details`` is merged shallowly into the JSON payload so callers can
    attach ``missing``, ``manual_urls``, ``expected``, ``actual`` etc.
    """
    payload: Dict[str, Any] = {
        "ok": False,
        "error": kind,
        "error_name": CODE_TO_NAME.get(code, "generic"),
        "message": message,
        "exit_code": code,
    }
    if details:
        for key, value in details.items():
            payload[key] = value
    if json_mode:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(f"error [{kind}]: {message}\n")
        sys.stderr.flush()
    return code


def emit_warning(kind: str, message: str, *, json_mode: bool = False, details: Optional[Dict[str, Any]] = None) -> None:
    """Emit a warning line without exiting.

    In ``--json`` mode a single JSON line goes to *stderr* so it does not
    pollute the stdout result payload.
    """
    if json_mode:
        payload: Dict[str, Any] = {"warning": kind, "message": message}
        if details:
            for key, value in details.items():
                payload[key] = value
        sys.stderr.write(json.dumps(payload, ensure_ascii=False))
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"warning [{kind}]: {message}\n")
    sys.stderr.flush()
