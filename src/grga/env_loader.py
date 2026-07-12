"""Load ``.env`` from the grga user config directory at process startup.

Behavior:

* Config dir resolution matches :mod:`grga.deep_search`:
  ``$XDG_CONFIG_HOME/grga`` if set, otherwise ``~/.config/grga``. This keeps a
  single canonical location on every platform (POSIX / macOS / Windows) so the
  ``.env`` file lives next to ``settings.json``.
* If ``<config_dir>/.env`` exists, parse it and inject variables into
  :data:`os.environ` **without overriding** already-set variables. Real shell
  env always wins so operators can still override on the command line.
* If the file is missing, write a fully-commented example ``.env`` with every
  environment variable documented in the codebase. Callers can uncomment the
  ones they need. The example is written exactly once; subsequent startups
  find the file and load it (which is a no-op until the user edits it).

The parser is intentionally minimal (no shell expansion, no ``export``
keyword) so we do not need to depend on ``python-dotenv``. It supports:

* ``KEY=VALUE`` lines
* ``#`` comments and blank lines
* Surrounding double / single quotes on the value
* Trailing inline comments after unquoted values are **not** stripped — keep
  values on their own line for predictability.

Cross-platform notes:

* Uses :func:`pathlib.Path` throughout — no shell-specific separators.
* File I/O is UTF-8 with newline normalization.
* Writing is atomic (write to ``.env.tmp`` then rename) to survive interrupts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Tuple


def config_dir() -> Path:
    """Return the grga user config directory (may not exist yet)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "grga"


def env_path() -> Path:
    """Return the path to ``<config_dir>/.env`` (may not exist)."""
    return config_dir() / ".env"


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines out of a ``.env`` text blob.

    Comments (``#``) and blank lines are ignored. Values may be wrapped in
    single or double quotes; the quotes are stripped. Malformed lines are
    silently skipped so a partially-broken ``.env`` never crashes startup.
    """
    result: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not _is_valid_key(key):
            continue
        result[key] = _strip_quotes(value.strip())
    return result


def _is_valid_key(key: str) -> bool:
    if not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def load_env_file(path: Path, *, override: bool = False) -> Tuple[int, int]:
    """Load ``path`` into :data:`os.environ`.

    Returns ``(loaded, skipped)`` — the number of variables injected and the
    number left untouched because they were already present. When
    ``override`` is true, existing vars are overwritten (used only in tests).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0, 0

    loaded = 0
    skipped = 0
    for key, value in parse_env_text(text).items():
        if not override and key in os.environ:
            skipped += 1
            continue
        os.environ[key] = value
        loaded += 1
    return loaded, skipped


# --------------------------------------------------------------------------- #
# Example .env content
# --------------------------------------------------------------------------- #

EXAMPLE_ENV_TEMPLATE = """\
# grga environment variables (.env)
#
# This file lives at $XDG_CONFIG_HOME/grga/.env (or ~/.config/grga/.env when
# $XDG_CONFIG_HOME is unset) and is loaded automatically at grga startup.
#
# Rules:
#   * KEY=VALUE, one per line. Quote values with spaces.
#   * Lines starting with # are comments. All entries below are commented by
#     default; uncomment the ones you actually need.
#   * Values already set in the real shell environment take precedence — the
#     .env file only fills in the gaps.
#   * Boolean flags accept: 1 / 0, true / false, yes / no, on / off.
#
# See docs/exit-codes.md and docs/tool-resolution.md for related context.

# ---------------------------------------------------------------------------
# LLM credentials (used by `grga deep-search` and `grga suggest`)
# ---------------------------------------------------------------------------
# grga-prefixed names win over the OPENAI_* fallbacks.

# GRGA_OPENAI_API_KEY=sk-...
# GRGA_OPENAI_MODEL=gpt-4o-mini
# GRGA_OPENAI_BASE_URL=https://api.openai.com/v1

# Fallbacks recognised when the GRGA_OPENAI_* variants are missing.
# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=https://api.openai.com/v1

# ---------------------------------------------------------------------------
# Managed toolchain (rga / rg / pandoc / pdftotext / fzf ...)
# ---------------------------------------------------------------------------

# Override the tools cache root. Default resolution order:
#   $XDG_DATA_HOME/grga/tools  ->  ~/.local/share/grga/tools (POSIX)
#                              ->  %LOCALAPPDATA%\\grga\\tools (Windows)
# GRGA_TOOLS_DIR=/path/to/tools

# Override the grga state directory (migration markers, sentinels). Default
# resolution order: $XDG_STATE_HOME/grga -> $XDG_DATA_HOME/grga -> platform.
# GRGA_STATE_DIR=/path/to/state

# Prefer system-installed tools on $PATH over the managed cache.
# GRGA_PREFER_SYSTEM=0

# Refuse network access. Missing tools fail fast with exit code 5 instead of
# being downloaded. Same effect as passing --offline.
# GRGA_OFFLINE=0

# Skip sha256 verification when downloading tools. Only useful for debugging
# a broken mirror; never enable in production.
# GRGA_INSECURE=0

# ---------------------------------------------------------------------------
# Structure sidecar cache (Markdown chapter tree, used by expand)
# ---------------------------------------------------------------------------

# Root directory for the on-disk structure sidecar cache. Default is the
# search directory root at `.grga/structure/`.
# GRGA_SIDECAR_ROOT=/path/to/sidecar

# ---------------------------------------------------------------------------
# Graph / related tuning
# ---------------------------------------------------------------------------

# Read-side floor on chunk_cooccur pair NPMI for `related --mode graph`.
# Can only tighten the v8 write-time floor (0.2). Default 0.2.
# GRGA_PPR_MIN_NPMI=0.2

# Silence schema migration log noise (test / CI convenience).
# GRGA_QUIET_MIGRATION=0

# ---------------------------------------------------------------------------
# Desktop GUI (`grga-gui`)
# ---------------------------------------------------------------------------

# Manual HiDPI / display scale override (1.0 - 2.5).
# GRGA_DISPLAY_SCALE=1.0
"""


def write_example_env(path: Path) -> None:
    """Atomically write the fully-commented example ``.env`` to ``path``.

    Parent directories are created as needed. Existing files are **not**
    overwritten — callers should test :meth:`Path.exists` first. On POSIX the
    file is created with mode ``0600`` because users are told to paste LLM API
    keys into it; a default-umask ``0644`` would leak secrets to other local
    users.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(EXAMPLE_ENV_TEMPLATE, encoding="utf-8")
    os.replace(tmp, path)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def ensure_env() -> Optional[Path]:
    """Load ``.env`` if present, otherwise seed a commented example.

    Called from :func:`grga.cli.main` and :func:`grga.gui.main` at process
    startup. Returns the resolved path on success, ``None`` if the config
    directory is not writable and no example could be produced (a benign
    situation — grga still runs with plain shell env).
    """
    path = env_path()
    try:
        if path.exists():
            load_env_file(path)
            return path
        write_example_env(path)
        return path
    except OSError:
        return None
