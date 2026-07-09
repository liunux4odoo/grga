"""Central resolver for the grga managed-tools cache directory.

Priority (highest first):

1. ``$GRGA_TOOLS_DIR``                          -> used as the tools *root*
2. ``$XDG_DATA_HOME/grga/tools``               -> root
3. platform default                             -> root
   * Linux / macOS: ``~/.local/share/grga/tools``
   * Windows:       ``%LOCALAPPDATA%\\grga\\tools``

The *root* never contains the platform segment. The per-platform directory is
``<root>/<platform>``; use :func:`resolve_tools_dir` for that. So
``GRGA_TOOLS_DIR=/tmp/x`` resolves the current-platform dir to
``/tmp/x/<platform>``.

This module is intentionally free of any :mod:`grga.toolchain` import so it
stays a leaf dependency. The platform key is passed in by callers.
"""

import os
import sys
from pathlib import Path

APP_NAME = "grga"
TOOLS_SUBDIR = "tools"


def _is_windows():
    return sys.platform.startswith("win") or os.name == "nt"


def _env(name):
    value = os.environ.get(name)
    if value is None:
        return ""
    return value.strip()


def resolve_tools_root():
    """Return the tools cache root (without the platform segment)."""
    override = _env("GRGA_TOOLS_DIR")
    if override:
        return Path(override)

    xdg = _env("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME / TOOLS_SUBDIR

    if _is_windows():
        local = _env("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / TOOLS_SUBDIR

    return Path.home() / ".local" / "share" / APP_NAME / TOOLS_SUBDIR


def resolve_tools_dir(platform_key):
    """Return the per-platform tools directory ``<root>/<platform_key>``.

    Path is *not* created; creation is deferred to install time so read-only
    filesystems do not fail on resolution.
    """
    return resolve_tools_root() / platform_key


def resolve_state_dir():
    """Return the grga state directory (sentinels, migration markers).

    Priority:
      1. ``$GRGA_STATE_DIR`` (explicit override)
      2. ``$XDG_STATE_HOME/grga``
      3. ``$XDG_DATA_HOME/grga`` (paired with the tools cache)
      4. platform default state dir
    """
    override = _env("GRGA_STATE_DIR")
    if override:
        return Path(override)

    xdg_state = _env("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / APP_NAME

    xdg = _env("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME

    if _is_windows():
        local = _env("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME

    return Path.home() / ".local" / "share" / APP_NAME
