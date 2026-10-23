"""Central resolver for grga directory paths.

This module resolves the tools cache, state directory, general cache
directory, and the dream assets root, following XDG Base Directory
conventions with grga-specific overrides. Each resolver function documents
its own priority chain.

Tools cache notes
-----------------
The tools *root* never contains the platform segment. The per-platform
directory is ``<root>/<platform>``; use :func:`resolve_tools_dir` for that.
So ``GRGA_TOOLS_DIR=/tmp/x`` resolves the current-platform dir to
``/tmp/x/<platform>``.

This module is intentionally free of any :mod:`grga.toolchain` import so it
stays a leaf dependency. The platform key is passed in by callers.
"""

import os
import sys
from pathlib import Path

APP_NAME = "grga"
TOOLS_SUBDIR = "tools"
DREAM_SUBDIR = "dream"


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


def resolve_cache_dir():
    """Return the grga cache directory (sidecar structure docs, ...).

    Priority:
      1. ``$GRGA_CACHE_DIR`` (explicit override)
      2. ``$XDG_CACHE_HOME/grga``
      3. platform default cache dir
         * Linux / macOS: ``~/.cache/grga``
         * Windows:       ``%LOCALAPPDATA%\\grga\\cache``
    """
    override = _env("GRGA_CACHE_DIR")
    if override:
        return Path(override)

    xdg_cache = _env("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / APP_NAME

    if _is_windows():
        local = _env("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "cache"

    return Path.home() / ".cache" / APP_NAME


def resolve_dream_dir():
    """Return the dream assets root directory (``<root>/dream``).

    Dream assets are user-editable knowledge (entity/relation wiki files
    written by the caller's LLM), so they live in the **data** directory —
    never the cache — and carry **no platform segment** (unlike the tools
    cache). See ``docs/design/plan-2026-10-14-dream-ontology.md`` §目录布局.

    Priority:
      1. ``$GRGA_DREAM_DIR`` (explicit override)
      2. ``$XDG_DATA_HOME/grga/dream``
      3. platform default data dir
         * Linux / macOS: ``~/.local/share/grga/dream``
         * Windows:       ``%LOCALAPPDATA%\\grga\\dream``

    The path is **not** created; creation is deferred to write time so
    read-only filesystems do not fail on resolution (same contract as the
    other ``resolve_*`` helpers).
    """
    override = _env("GRGA_DREAM_DIR")
    if override:
        return Path(override)

    xdg = _env("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME / DREAM_SUBDIR

    if _is_windows():
        local = _env("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / DREAM_SUBDIR

    return Path.home() / ".local" / "share" / APP_NAME / DREAM_SUBDIR
