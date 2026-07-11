"""Unified file → list[str] reader with suffix-based dispatch.

This module isolates the "how do I get all lines of a file" question that
both :mod:`grga.expand` and :mod:`grga.graph.build` used to answer with a
plain ``open(..., encoding='utf-8')``. That answer was fine for text but
silently produced garbage on PDFs / Office docs / archives.

The design (see the 2026-07-11 thread on ripgrep-all adapter reuse):

* **Text whitelist** → :func:`pathlib.Path.read_text`. Fast, no subprocess,
  no external dependency. Works for every file whose suffix we recognise
  as text (source code, config, markdown, plain txt). Also serves as the
  fallback when a file has an unknown suffix — we prefer "try to read it
  as text and let the caller see what it gets" over silently skipping.

* **rga-supported suffix** → ``rga --passthru``. We reuse the same
  ripgrep-all binary the search path uses, so:

  1. Line numbers reported by :func:`grga.search.search_directory` line up
     one-to-one with what :func:`read_file_lines` returns for the same
     file — no adapter drift possible.
  2. rga's SQLite cache means a file already searched is essentially free
     to re-read at expand time.
  3. We never depend on ``rga-preproc`` directly. That subcommand is an
     internal adapter runner of ripgrep-all; its arguments and output
     format are not part of the public contract.

The rga path is best-effort: if rga is missing, times out, or reports a
hard failure, we return an empty list and let the caller decide how to
degrade (expand falls back through its degrade chain, graph/build bumps
a skip reason). We never raise from :func:`read_file_lines`.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_logger = logging.getLogger("grga.file_read")


# ---------------------------------------------------------------------------
# Suffix whitelists
# ---------------------------------------------------------------------------
#
# ``TEXT_SUFFIXES`` mirrors what :mod:`grga.graph.build` used to enforce
# with ``_TEXT_SUFFIXES``. It is the union of "obviously text" extensions
# we would open() directly. Files with unknown suffixes still get the
# open() path (see :func:`read_file_lines`) — the whitelist only decides
# *routing*, not *acceptance*.
TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".mdown", ".mkd", ".rst", ".txt", ".text",
    ".py", ".pyi", ".rb", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".java", ".kt", ".swift", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".css", ".scss", ".less", ".html", ".htm", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".json", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".sql", ".r", ".m",
})

# Extensions rga's default adapters know how to preprocess. Kept in sync
# with ``rga --rga-list-adapters`` output for the default set (pandoc,
# poppler, ffmpeg, zip, decompress, tar, sqlite). Disabled-by-default
# adapters (mail) are intentionally omitted; users who need them can
# still search those files via a system rga, but expand/graph won't try.
RGA_SUPPORTED_SUFFIXES = frozenset({
    # poppler
    ".pdf",
    # pandoc
    ".epub", ".odt", ".docx", ".fb2", ".ipynb", ".html", ".htm",
    # ffmpeg (metadata only; text extraction is limited)
    ".mkv", ".mp4", ".avi", ".mp3", ".ogg", ".flac", ".webm",
    # zip
    ".zip", ".jar", ".xpi", ".kra", ".snagx",
    # decompress
    ".als", ".bz2", ".gz", ".tbz", ".tbz2", ".tgz", ".xz", ".zst",
    # tar
    ".tar",
    # sqlite
    ".db", ".db3", ".sqlite", ".sqlite3",
})

# Subset of :data:`RGA_SUPPORTED_SUFFIXES` that is *only* meaningful after
# rga's adapter runs. Opening these with a plain UTF-8 ``open()`` yields
# garbled bytes, never useful text. When rga cannot run on one of these
# (missing binary, timeout, corrupt/encrypted file, adapter rejection) we
# must report ``FAILED`` rather than fall back to a native read — otherwise
# the garbage would be tokenised and silently poison the graph index.
#
# ``.html`` / ``.htm`` / ``.ipynb`` are deliberately excluded: they are
# text-adjacent (HTML source and notebook JSON are perfectly readable as
# UTF-8), so a native fallback there is "worse formatting" not "binary
# garbage" and is preferable to skipping the file entirely.
RGA_BINARY_ONLY_SUFFIXES = frozenset({
    ".pdf",
    ".epub", ".odt", ".docx", ".fb2",
    ".mkv", ".mp4", ".avi", ".mp3", ".ogg", ".flac", ".webm",
    ".zip", ".jar", ".xpi", ".kra", ".snagx",
    ".als", ".bz2", ".gz", ".tbz", ".tbz2", ".tgz", ".xz", ".zst",
    ".tar",
    ".db", ".db3", ".sqlite", ".sqlite3",
})


def is_text_suffix(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def is_rga_supported_suffix(path: Path) -> bool:
    return path.suffix.lower() in RGA_SUPPORTED_SUFFIXES


def is_rga_binary_only_suffix(path: Path) -> bool:
    return path.suffix.lower() in RGA_BINARY_ONLY_SUFFIXES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ReadSource:
    """String constants for :attr:`ReadResult.source`. Free-standing so
    callers can compare without importing an enum."""

    NATIVE = "native"   # plain open() / read_text
    RGA = "rga"         # dispatched to ripgrep-all
    EMPTY = "empty"     # file existed but produced zero lines
    FAILED = "failed"   # read attempt failed (missing rga, timeout, ...)


@dataclass
class ReadResult:
    """Structured result of :func:`read_file_lines_ex`.

    ``lines`` is always a list (possibly empty) of decoded lines with
    trailing ``\\n`` / ``\\r`` stripped. ``source`` tells the caller
    which branch produced them, useful for stats and for degrade
    decisions in :mod:`grga.expand`.
    """

    lines: List[str]
    source: str


def read_file_lines(path: Path) -> List[str]:
    """Return every line of ``path`` as a list of strings.

    Thin wrapper over :func:`read_file_lines_ex` that discards the
    source label. Errors are swallowed and returned as an empty list;
    callers wanting to react to failure should use
    :func:`read_file_lines_ex`.
    """
    return read_file_lines_ex(path).lines


def read_file_lines_ex(path: Path, *, rga_timeout: float = 120.0) -> ReadResult:
    """Suffix-dispatching read.

    * Suffix in :data:`RGA_SUPPORTED_SUFFIXES` → route through
      ``rga --passthru``.

      - When rga succeeds, return its lines (``RGA`` / ``EMPTY``).
      - When rga cannot run (missing binary, timeout, hard error) the
        fallback depends on the suffix class:

        * :data:`RGA_BINARY_ONLY_SUFFIXES` (PDF / Office / archives /
          sqlite / media) → ``FAILED``. A native UTF-8 read of these
          would be pure garbage; feeding it into the graph index or an
          expand context is worse than reporting failure and letting
          the caller degrade / skip.
        * text-adjacent rga suffixes (``.html`` / ``.htm`` / ``.ipynb``)
          → native fallback. Their raw bytes are legible UTF-8, so a
          native read is "worse formatting" not "binary garbage".

    * Otherwise → :meth:`pathlib.Path.read_text` with UTF-8 replace.
      Unknown suffixes get this branch: we bet on text and let the
      caller judge the output.
    """
    if is_rga_supported_suffix(path):
        rga_result = _read_via_rga(path, timeout=rga_timeout)
        if rga_result is not None:
            source = ReadSource.RGA if rga_result else ReadSource.EMPTY
            return ReadResult(lines=rga_result, source=source)
        # rga refused (missing binary, timeout, hard error).
        if is_rga_binary_only_suffix(path):
            # A native read here would tokenise binary garbage into the
            # graph index or an expand context. Fail cleanly instead.
            return ReadResult(lines=[], source=ReadSource.FAILED)
        # text-adjacent (.html/.htm/.ipynb): native read is legible and
        # better than skipping the file entirely.
    native_lines = _read_native(path)
    if native_lines is None:
        return ReadResult(lines=[], source=ReadSource.FAILED)
    source = ReadSource.NATIVE if native_lines else ReadSource.EMPTY
    return ReadResult(lines=native_lines, source=source)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _read_native(path: Path) -> Optional[List[str]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _find_rga() -> Optional[str]:
    # Local import: keeps this module importable in tests that stub out
    # the toolchain and avoids ``search`` <-> ``file_read`` circular
    # imports if ``search`` ever grows to import from us.
    from grga.toolchain import add_tools_to_path, find_tool

    add_tools_to_path()
    return find_tool("rga")


# Impossible pattern: end-of-line anchor followed by start-of-line anchor
# never matches, so with ``--passthru`` rga prints every line as a
# ``context`` event without any ``match`` overhead.
_NEVER_MATCH = r"$^"


def _read_via_rga(path: Path, *, timeout: float) -> Optional[List[str]]:
    """Invoke ``rga --json --passthru`` and reconstruct the line list.

    Returns ``None`` (distinct from ``[]``) when rga could not run at
    all — missing binary, OSError, timeout, or a hard non-zero return
    code that isn't the standard "no match" (rc=1). ``[]`` means the
    file was successfully processed but yielded no text (empty file,
    or every line came back as binary ``bytes`` and was filtered).
    """
    rga = _find_rga()
    if rga is None:
        return None
    cmd = [
        rga,
        "--json",
        "--passthru",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--no-messages",
        "-e", _NEVER_MATCH,
        "--",
        str(path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.debug("rga passthru failed for %s: %r", path, exc)
        return None
    # rc 0 == matches found (won't happen with $^), 1 == no matches
    # (expected), anything else means rga bailed for real.
    if completed.returncode not in (0, 1):
        _logger.debug(
            "rga passthru returncode=%s for %s (stderr=%r)",
            completed.returncode, path, completed.stderr[:200],
        )
        return None
    return _parse_passthru_json(completed.stdout)


def _parse_passthru_json(output: str) -> List[str]:
    """Turn rga's ``--json --passthru`` stream into a dense line list.

    rga emits ``begin`` / ``context`` / ``match`` / ``end`` / ``summary``
    events, one per line. We only care about ``context`` and ``match``
    (context is what passthru non-matches produce; match is the theoretical
    hit — kept for safety even though ``$^`` should never fire). Each such
    event carries ``line_number`` and ``lines.text``; when rga could not
    decode a line as UTF-8 it emits ``lines.bytes`` (base64) instead —
    those are dropped because our consumers want strings.

    Reconstruction is dense (indexed 1..max) so that ``lines[hit.line-1]``
    from :func:`grga.search.search_directory` and this reader always
    point at the same physical line. Gaps left by dropped binary lines
    are filled with empty strings to preserve alignment.
    """
    lines_by_number: Dict[int, str] = {}
    max_line = 0
    for raw in output.splitlines():
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in ("context", "match"):
            continue
        data = event.get("data") or {}
        line_no = data.get("line_number")
        if not isinstance(line_no, int) or line_no < 1:
            continue
        line_payload = data.get("lines") or {}
        text = line_payload.get("text")
        if text is None:
            # binary line: leave a placeholder so line numbers still
            # align with what rga sees during search.
            lines_by_number.setdefault(line_no, "")
        else:
            if text.endswith("\n"):
                text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
            lines_by_number[line_no] = text
        if line_no > max_line:
            max_line = line_no
    if max_line == 0:
        return []
    return [lines_by_number.get(i, "") for i in range(1, max_line + 1)]


# ---------------------------------------------------------------------------
# Helpers for callers that want the union suffix set
# ---------------------------------------------------------------------------

def indexable_suffixes() -> frozenset:
    """Return every suffix :func:`read_file_lines` knows how to route.

    Used by :func:`grga.graph.build.iter_text_files` to decide which
    files to enumerate. Unknown suffixes are *not* returned here; the
    graph builder wants a bounded set to avoid pulling in random
    binaries. Callers that want to attempt a read on any suffix should
    call :func:`read_file_lines` directly.
    """
    return TEXT_SUFFIXES | RGA_SUPPORTED_SUFFIXES
