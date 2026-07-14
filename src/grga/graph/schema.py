"""Graph schema (v10 — scope / directory split).

The v10 schema splits **knowledge** (portable across machines) from
**evidence** (machine-local). The pre-v10 model conflated the two by
using a resolved absolute directory path as the shard key for every
knowledge asset (``keyword_pairs`` / ``keyword_stats`` / ...); v10
replaces that key with a logical ``scope_id`` and demotes the physical
directory to a bypass table (``scope_directories``) that can be wiped
during a knowledge-pack transfer without touching the assets.

Design: ``docs/design/plan-2026-07-14-scope-directory-split.md``.

Knowledge tables (portable — enter a knowledge pack):

* ``scopes``               — logical domain name (e.g. ``physics-cn``).
                             *Shard key* of every knowledge asset.
* ``keywords``             — global casefold vocabulary.
* ``keyword_pairs``        — per-scope co-occurrence (``chunk_cooccur``
                             or ``llm_promoted``) with ``cooccur_count``
                             and ``npmi``.
* ``keyword_stats``        — per-scope ``chunk_df`` / ``doc_df`` /
                             ``total_occurrences``.
* ``scope_stats``          — per-scope ``n_chunks`` (npmi denominator)
                             / ``n_docs`` / ``vocab_size``.
* ``auto_ngram_dict``      — per-scope tokenizer n-gram dictionary.
* ``llm_extension_votes``  — LLM vote buffer (promoted rows below).
* ``meta``                 — global k/v (``toolchain_fingerprint`` etc).

Evidence tables (machine-local — NOT in a knowledge pack):

* ``scope_directories``    — scope → local filesystem path binding.
                             A scope may bind multiple directories on
                             this machine; other machines have their own
                             bindings. Wiped on transfer, rebuilt via
                             ``grga scope bind``.
* ``docs``                 — file-level metadata (size / mtime / hash)
                             backing the incremental build.
* ``keyword_docs``         — doc-level inverted index (seed → docs).
* ``heading_docs``         — Markdown chapter structure per doc.
* ``keyword_counts``       — v1 deep-search heat counter (per-scope).
* ``keyword_sources``      — v1 deep-search hit locations (path/line).

The v10 migration is **not backward compatible**. An older database is
detected via ``PRAGMA user_version`` or the presence of the pre-v10
``directories`` intern table and rejected with a clear error pointing at
the recovery path (delete DB + `grga index build --scope <name>
--directory <path>`). This is a deliberate call from
plan-2026-07-14 §7: the tool has no historical users, and mapping
absolute paths to logical scope names has no clean auto-inference.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Union

SCHEMA_VERSION = 10

# Allowed ``keyword_pairs.source`` values (exhaustive; anything else is
# a bug). Enforced at write time by build / promote.
CHUNK_COOCCUR_SOURCE = "chunk_cooccur"
LLM_PROMOTED_SOURCE = "llm_promoted"

# Write-time admission gates for ``chunk_cooccur`` pairs. A pair enters
# the knowledge asset only when it clears BOTH thresholds in its scope.
# A query may filter with a *stricter* ``min_npmi`` but can never recover
# a pair that fell below ``BUILD_MIN_NPMI`` here (never memorised).
# ``llm_promoted`` pairs bypass these; ``npmi`` is NULL for them.
BUILD_MIN_PAIR_COUNT = 5
BUILD_MIN_NPMI = 0.2


def resolve_scope_name(name: Optional[str]) -> Optional[str]:
    """Normalise a logical scope name for query-side scope filters.

    * ``None`` -> ``None`` meaning *no scope filter* (cross-scope union).
    * a non-empty string -> its stripped form (case-sensitive; the caller
      is responsible for consistent casing across build and query).

    Raises :class:`ValueError` for the empty / whitespace string: v10
    does not have an "unscoped" sentinel — every knowledge row must
    belong to a real, named scope.

    Unlike the pre-v10 ``resolve_scope_key``, this function **never
    touches the filesystem**: no ``expanduser`` / ``resolve``. Scope
    names are opaque handles that must survive being copied to another
    machine (plan-2026-07-14 §1.1, §3).
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        raise ValueError(
            "scope name must be a non-empty string; pass None to disable "
            "the scope filter (cross-scope union)"
        )
    return text


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open ``path`` with ``sqlite3.Row`` factory and generous busy timeout.

    Parent directory is created on demand. The caller owns the connection
    and is responsible for closing it. The 180 s busy timeout preserves
    the legacy migration-safety margin even though v10 no longer runs
    a long-lived migration (fresh installs are instantaneous).
    """
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=180.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``user_version`` pragma value."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# v10 DDL. Split into logical groups so a fresh install issues them all,
# and so tests can reference specific groups (KNOWLEDGE_* vs EVIDENCE_*).
# ---------------------------------------------------------------------------

_CREATE_SCOPE_TABLES = """
-- Logical domain name. Shard key of every knowledge asset. Portable
-- across machines: the same scope name identifies the same domain
-- regardless of where the corpus lives.
CREATE TABLE IF NOT EXISTS scopes (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT,
    created_at TEXT
);

-- scope -> local filesystem path binding. NOT in the knowledge pack —
-- the target machine rebuilds these via ``grga scope bind``. A scope
-- may have multiple directory rows (e.g. papers + notes both bound to
-- ``physics-cn``) — ``grga index build`` handles one directory per call.
CREATE TABLE IF NOT EXISTS scope_directories (
    scope_id   INTEGER NOT NULL REFERENCES scopes(id),
    directory  TEXT NOT NULL,
    role       TEXT,
    indexed_at TEXT,
    PRIMARY KEY (scope_id, directory)
);
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_CREATE_KEYWORDS = """
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY,
    kw TEXT NOT NULL UNIQUE
);
"""

_CREATE_DOCS = """
-- Evidence layer: per-file metadata for incremental build detection.
-- ``scope_id`` is denormalised for a fast per-scope filter without a
-- ``scope_directories`` JOIN. ``doc_path`` is relative to the directory
-- that ``docs`` was indexed under (recorded in ``scope_directories``).
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    doc_path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    content_hash TEXT,
    n_chunks_with_kw INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scope_id, doc_path)
);
"""

_CREATE_KEYWORD_PAIRS = """
-- The core knowledge asset. Once written, only refined (never deleted
-- by evidence removal). See plan-2026-07-12-keyword-primary.md §1.1
-- for the "grow-and-refine" contract.
CREATE TABLE IF NOT EXISTS keyword_pairs (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_a_id  INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id  INTEGER NOT NULL REFERENCES keywords(id),
    source   TEXT NOT NULL,
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi     REAL,
    updated_at TEXT,
    PRIMARY KEY (scope_id, kw_a_id, kw_b_id, source)
) WITHOUT ROWID;
"""

_CREATE_ASSET_TABLES = """
CREATE TABLE IF NOT EXISTS keyword_stats (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id    INTEGER NOT NULL REFERENCES keywords(id),
    chunk_df INTEGER NOT NULL DEFAULT 0,
    doc_df   INTEGER NOT NULL DEFAULT 0,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope_id, kw_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS scope_stats (
    scope_id INTEGER PRIMARY KEY REFERENCES scopes(id),
    n_chunks INTEGER NOT NULL DEFAULT 0,
    n_docs   INTEGER NOT NULL DEFAULT 0,
    vocab_size INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS auto_ngram_dict (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    phrase TEXT NOT NULL,
    n INTEGER NOT NULL,
    count INTEGER NOT NULL,
    npmi REAL NOT NULL,
    branch_ent REAL NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (scope_id, phrase)
);

CREATE TABLE IF NOT EXISTS llm_extension_votes (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
    vote_count REAL NOT NULL DEFAULT 0,
    last_voted TEXT,
    promoted_at TEXT,
    PRIMARY KEY (scope_id, kw_a_id, kw_b_id)
);
"""

_CREATE_EVIDENCE_TABLES = """
-- Doc-level inverted index (v9 semantics, scope_id-shard). Evidence
-- layer: machine-local, wiped on transfer.
CREATE TABLE IF NOT EXISTS keyword_docs (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id    INTEGER NOT NULL REFERENCES keywords(id),
    doc_id   INTEGER NOT NULL REFERENCES docs(id),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kw_id, doc_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS heading_docs (
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    heading_path TEXT NOT NULL,
    chapter_id TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (doc_id, heading_path, chapter_id)
);

-- v1 deep-search heat counter. Per-scope (kept in v10 for parity but
-- semantically closer to evidence than knowledge — it decays over time
-- as the deep-search agent revisits terms).
CREATE TABLE IF NOT EXISTS keyword_counts (
    scope TEXT NOT NULL DEFAULT '',
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope, scope_id, kw_id)
);

CREATE TABLE IF NOT EXISTS keyword_sources (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    path TEXT NOT NULL,
    line_number INTEGER NOT NULL DEFAULT 0,
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope_id, kw_id, path, line_number)
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_docs_scope
    ON docs (scope_id);
-- ``scope_directories(scope_id, directory)`` is the PRIMARY KEY, which
-- already gives us a leading-column index on ``scope_id`` for the
-- binding lookup; no dedicated ``idx_scope_dirs`` needed.
CREATE INDEX IF NOT EXISTS idx_kp_a_npmi
    ON keyword_pairs (scope_id, kw_a_id, npmi DESC);
CREATE INDEX IF NOT EXISTS idx_kp_b_npmi
    ON keyword_pairs (scope_id, kw_b_id, npmi DESC);
CREATE INDEX IF NOT EXISTS idx_heading_docs_head
    ON heading_docs (heading_path);
CREATE INDEX IF NOT EXISTS idx_keyword_counts_count
    ON keyword_counts (scope_id, count DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_sources_kw
    ON keyword_sources (scope_id, kw_id);
CREATE INDEX IF NOT EXISTS idx_votes_scope
    ON llm_extension_votes (scope_id, vote_count DESC);
CREATE INDEX IF NOT EXISTS idx_auto_ngram_scope
    ON auto_ngram_dict (scope_id);
CREATE INDEX IF NOT EXISTS idx_keyword_stats_scope
    ON keyword_stats (scope_id, chunk_df DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_docs_scope
    ON keyword_docs (scope_id, kw_id);
CREATE INDEX IF NOT EXISTS idx_keyword_docs_reverse
    ON keyword_docs (scope_id, doc_id);
"""


def _split_sql(script: str) -> List[str]:
    """Split a ``;``-separated SQL script into individual statements.

    ``executescript`` issues an implicit ``COMMIT`` so we cannot use it
    inside a caller-owned transaction. Feed statements one at a time
    with ``execute`` instead.

    Robust to ``--`` line comments containing ``;`` (which would
    otherwise trigger a spurious statement split): each line is trimmed
    of everything following the first ``--`` before we look for the
    delimiter. Block ``/* */`` comments are uncommon here and are left
    intact.
    """
    cleaned_lines: List[str] = []
    for line in script.splitlines():
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    out: List[str] = []
    for chunk in cleaned.split(";"):
        stmt = chunk.strip()
        if stmt:
            out.append(stmt)
    return out


def _create_current_tables(conn: sqlite3.Connection) -> None:
    """Create every v10 table + index (idempotent).

    Ordered so foreign-key referents exist before referrers even though
    SQLite doesn't enforce FKs by default here — makes the DDL easier
    to read.
    """
    for stmt in _split_sql(
        _CREATE_SCOPE_TABLES
        + _CREATE_META_TABLE
        + _CREATE_KEYWORDS
        + _CREATE_DOCS
        + _CREATE_KEYWORD_PAIRS
        + _CREATE_ASSET_TABLES
        + _CREATE_EVIDENCE_TABLES
        + _CREATE_INDEXES
    ):
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Legacy-DB rejection: v10 is intentionally not backward compatible.
# ---------------------------------------------------------------------------

# Tables whose presence unambiguously identifies a pre-v10 shape.
# ``directories`` covers v5-v9 (the absolute-path intern table). The
# other names cover the pre-v5 ``KeywordLibrary`` shape used by the
# original ``deep_search.py``, which shipped ``keyword_counts`` /
# ``keyword_sources`` / ``keyword_pairs`` with a TEXT ``directory``
# column, no ``directories`` intern table, and ``user_version = 0``.
# Together they detect every pre-v10 shape the tool has ever created.
_LEGACY_MARKER_TABLES = (
    "directories",
    "keyword_chunks",   # pre-v9 chunk-level inverted index
    "chunks",           # pre-v9 chunk table
)

# Table names that existed in pre-v10 shapes AND still exist in v10 —
# they cannot be used as a solitary legacy marker (a fresh v10 DB has
# them too), but if any of them exist without ``scopes``, the DB is
# pre-v10. The check is "pre-v10 tables present, v10 anchor absent".
_LEGACY_COEXISTING_TABLES = (
    "keyword_counts",
    "keyword_sources",
    "keyword_pairs",
    "keywords",
)


def _looks_legacy(conn: sqlite3.Connection) -> bool:
    """True when the DB shows any pre-v10 marker.

    Detects:

    * ``user_version`` stamps of 1..9 (v1-v9 explicit).
    * ``user_version > SCHEMA_VERSION`` (future DB — don't rewrite either).
    * Any of :data:`_LEGACY_MARKER_TABLES` present (v5-v9 or v3-era).
    * Any of :data:`_LEGACY_COEXISTING_TABLES` present without the v10
      anchor ``scopes`` — this catches pre-v5 ``KeywordLibrary`` DBs
      whose ``user_version`` was never stamped and whose tables have a
      TEXT ``directory`` column instead of ``scope_id``.
    """
    version = read_schema_version(conn)
    if version not in (0, SCHEMA_VERSION):
        return True
    for name in _LEGACY_MARKER_TABLES:
        if _table_exists(conn, name):
            return True
    # Pre-v5 case: coexisting tables exist but the v10 ``scopes`` anchor
    # does not. On a fresh install neither exists (short-circuit False).
    if not _table_exists(conn, "scopes"):
        for name in _LEGACY_COEXISTING_TABLES:
            if _table_exists(conn, name):
                return True
    return False


def _reject_legacy_db(conn: sqlite3.Connection) -> None:
    """Raise with the plan-2026-07-14 §7 recovery message.

    Names the offending DB file so the user can act on it, and tailors
    the "inspect old scopes" hint to the shape actually present
    (``SELECT path FROM directories`` only makes sense when the v5-v9
    ``directories`` table exists; pre-v5 DBs get a different hint).
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = row[2] if row is not None and len(row) >= 3 else "<memory>"
    prev_version = read_schema_version(conn)
    if _table_exists(conn, "directories"):
        inspect_hint = (
            "  1. (optional) inspect the old scopes:  sqlite3 <db> "
            "'SELECT path FROM directories;'\n"
        )
    else:
        # Pre-v5 shape — no ``directories`` table. Point at the pre-v10
        # heat / source tables that carry the raw ``directory`` column.
        inspect_hint = (
            "  1. (optional) inspect the pre-v10 shape:  sqlite3 <db> "
            "'.tables'  and  'PRAGMA user_version;'\n"
        )
    raise RuntimeError(
        f"grga index: schema v{prev_version} -> v{SCHEMA_VERSION} is a "
        "non-backward-compatible rewrite (scope / directory split). The "
        "old database at "
        f"{db_file!r} cannot be migrated automatically — mapping absolute "
        "directory paths to logical scope names has no clean auto-"
        "inference. To recover:\n"
        + inspect_hint
        + f"  2. delete the old database:  rm {db_file!r}\n"
        "  3. re-build with a logical scope name:\n"
        "       grga index build --scope <name> --directory <path>\n"
        "See docs/design/plan-2026-07-14-scope-directory-split.md §7."
    )


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Idempotently bring ``conn`` to schema v10.

    * Fresh database (no legacy marker, ``user_version == 0``): create
      every v10 table + index, stamp ``user_version = 10``.
    * Already at v10 (``user_version == 10``): fast no-op path.
    * Any pre-v10 shape (legacy tables present, or ``user_version`` in
      ``1..9``): raise with an actionable recovery message
      (:func:`_reject_legacy_db`).

    Returns the final ``user_version`` (always ``SCHEMA_VERSION`` on
    success).
    """
    current = read_schema_version(conn)
    if current == SCHEMA_VERSION and not any(
        _table_exists(conn, t) for t in _LEGACY_MARKER_TABLES
    ):
        # Fast path: DB is at v10 and free of any legacy remnant.
        return current

    if _looks_legacy(conn):
        _reject_legacy_db(conn)

    # Fresh DB: create + stamp under a caller-owned transaction if one
    # is open, otherwise open our own.
    owned_txn = not conn.in_transaction
    if owned_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _create_current_tables(conn)
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        if owned_txn:
            conn.execute("COMMIT")
    except Exception:
        if owned_txn and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise
    return SCHEMA_VERSION


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user-table name (debug helper)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def vacuum_db(db_path: Union[str, Path]) -> dict:
    """Run ``VACUUM`` on the graph DB and report the size delta.

    Opens its own autocommit connection because ``VACUUM`` cannot run
    inside a transaction. Returns
    ``{"before_bytes", "after_bytes", "freed_bytes"}`` for the caller.
    """
    resolved = Path(db_path).expanduser()
    before = os.path.getsize(str(resolved)) if resolved.exists() else 0
    conn = sqlite3.connect(str(resolved), timeout=60.0, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = os.path.getsize(str(resolved)) if resolved.exists() else 0
    return {
        "before_bytes": int(before),
        "after_bytes": int(after),
        "freed_bytes": int(before - after),
    }
