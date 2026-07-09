"""Runtime environment detection helpers.

These helpers decide *how* to communicate with the caller:

* Is a human likely watching the terminal? (``is_interactive``)
* Did the caller forbid network access? (``is_offline``)
* Does the caller prefer system tools? (``prefer_system_tools``)
* Are sha256 checks disabled? (``insecure_enabled``)

The functions are deliberately environment-driven so tests can monkeypatch
``os.environ`` / ``sys.stdin`` / ``sys.stdout``.
"""

import os
import sys


def _flag(name):
    value = os.environ.get(name, "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _stream_isatty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def is_ci():
    """Heuristic CI detection."""
    if _flag("CI"):
        return True
    for name in ("GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TRAVIS", "TF_BUILD"):
        if _flag(name):
            return True
    return False


def is_interactive():
    """True when a human likely watches the terminal.

    Requires both stdin and stdout to be TTYs and no CI signal in env.
    """
    if is_ci():
        return False
    return _stream_isatty(sys.stdin) and _stream_isatty(sys.stdout)


def is_offline(offline_flag=False):
    """Return True when network access is forbidden.

    Honors ``--offline`` and ``GRGA_OFFLINE=1``.
    """
    if offline_flag:
        return True
    return _flag("GRGA_OFFLINE")


def prefer_system_tools(flag=False):
    """Return True when the caller wants system tools preferred over managed."""
    if flag:
        return True
    return _flag("GRGA_PREFER_SYSTEM")


def insecure_enabled():
    return _flag("GRGA_INSECURE")

