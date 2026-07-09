"""Graph schema management (Phase 4.1).

``ensure_schema(conn)`` creates the v2 tables (``keyword_docs``,
``heading_docs``, ``llm_extension_votes``) without ever touching the v1
tables that ``grga.deep_search.KeywordLibrary`` already manages
(``keyword_counts``, ``keyword_pairs``, ``keyword_sources``). Upgrading an
old database is safe: this module only ever runs ``CREATE TABLE IF NOT
EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` statements plus a lightweight
``PRAGMA user_version`` bump, so no v1 rows are moved or destroyed.

The schema version is stored on the SQLite ``user_version`` pragma so we
can layer future migrations without disturbing the v1 counterparts. See
``docs/design/v2-design.md`` §6.2 for the table shapes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION = 3

# Preserved v1 tables. Never dropped, altered, or renamed.
V1_TABLES = ("keyword_counts", "keyword_pairs", "keyword_sources")

# v2 tables owned by this module.
V2_TABLES = ("keyword_docs", "heading_docs", "llm_extension_votes")


def resolve_scope_key(directory: Optional[Union[str, Path]]) -> Optional[str]:
    """Canonicalise a directory scope for the graph tables.

    Single source of truth shared by :mod:`grga.graph.votes` and
    :mod:`grga.graph.promote` so the "unscoped" concept never drifts:

    * ``None`` -> ``None`` meaning *no scope filter* (match every scope).
    * ``""`` (or whitespace) -> ``""`` meaning the *unscoped* rows, i.e.
      votes recorded without a directory. This is a real, filterable key.
    * a real path -> its resolved absolute string.

    Callers store rows using the ``""``-vs-path distinction and filter with
    ``scope_key is None`` (no ``WHERE``) vs ``scope_key == ""`` / a path
    (``WHERE directory = ?``). Because ``""`` round-trips instead of
    collapsing to ``None``, "promote only unscoped votes" is expressible.
    """
    if directory is None:
        return None
    text = str(directory).strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def scope_write_key(directory: Optional[Union[str, Path]]) -> str:
    """Return the value to *store* in a ``directory`` column.

    Writers always need a concrete string, so ``None`` and ``""`` both map
    to the unscoped sentinel ``""`` (whereas :func:`resolve_scope_key`
    keeps ``None`` distinct for read-time filtering).
    """
    key = resolve_scope_key(directory)
    return "" if key is None else key


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open ``path`` and enable ``sqlite3.Row`` factory.

    The parent directory is created on demand (matching v1
    ``KeywordLibrary`` semantics). The caller owns the connection and is
    responsible for closing it.
    """
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``user_version`` pragma value."""
    cur = conn.execute("PRAGMA user_version")
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row[0])


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Idempotently create v2 tables and stamp the schema version.

    Safe to call on:

    * a brand-new empty database (creates every v2 table);
    * a v1-only database (only the v2 tables are added, v1 tables are
      untouched);
    * a database that already reflects ``SCHEMA_VERSION`` (no-op fast path).

    Returns the final ``user_version`` after the call.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keyword_docs (
            scope TEXT NOT NULL DEFAULT '',
            directory TEXT NOT NULL,
            keyword TEXT NOT NULL,
            doc_path TEXT NOT NULL,
            occurrence_count REAL NOT NULL DEFAULT 0,
            first_line INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, keyword, doc_path)
        );
        CREATE TABLE IF NOT EXISTS heading_docs (
            directory TEXT NOT NULL,
            heading_path TEXT NOT NULL,
            doc_path TEXT NOT NULL,
            chapter_id TEXT NOT NULL DEFAULT '',
            start_line INTEGER NOT NULL DEFAULT 0,
            end_line INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, heading_path, doc_path, chapter_id)
        );
        CREATE TABLE IF NOT EXISTS llm_extension_votes (
            directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL,
            keyword_b TEXT NOT NULL,
            vote_count REAL NOT NULL DEFAULT 0,
            last_voted TEXT,
            PRIMARY KEY (directory, keyword_a, keyword_b)
        );
        CREATE INDEX IF NOT EXISTS idx_keyword_docs_kw
            ON keyword_docs (directory, keyword, occurrence_count DESC);
        CREATE INDEX IF NOT EXISTS idx_keyword_docs_doc
            ON keyword_docs (directory, doc_path);
        CREATE INDEX IF NOT EXISTS idx_heading_docs_head
            ON heading_docs (directory, heading_path);
        CREATE INDEX IF NOT EXISTS idx_heading_docs_doc
            ON heading_docs (directory, doc_path);
        CREATE INDEX IF NOT EXISTS idx_votes_dir
            ON llm_extension_votes (directory, vote_count DESC);
        """
    )
    current = read_schema_version(conn)
    if current < 3:
        _migrate_v2_to_v3(conn)
    if current < SCHEMA_VERSION:
        # ``?`` placeholders are not accepted for PRAGMA statements, but the
        # value is a hard-coded module constant so string formatting is safe.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    conn.commit()
    return read_schema_version(conn)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: casefold every ``keyword_docs.keyword`` value.

    Prior to v3 keywords were stored in their original casing and every
    read path had to wrap them in ``LOWER(keyword)`` — a non-sargable
    expression that forced a full-table SCAN when the query lacked a
    ``directory`` predicate (PERF plan §P1-2). v3 stores the folded form
    directly so the existing ``idx_keyword_docs_kw`` index covers those
    lookups.

    SQLite's ``LOWER()`` is ASCII-only, so we can't do the transform in
    SQL for unicode-heavy corpora (CJK, ß, dotted-i, etc.). We read every
    row, casefold in Python, drop and re-insert. The operation is idempotent
    and safe to run against an already-folded database.

    The primary key is ``(directory, keyword, doc_path)``. Two rows with
    different original casings (``Install`` and ``install``) in the same
    ``(directory, doc_path)`` collide after folding — we merge their
    ``occurrence_count`` / ``first_line`` / ``updated_at`` into a single row
    rather than hit a constraint error.
    """
    all_rows = conn.execute(
        """
        SELECT directory, keyword, doc_path, occurrence_count,
               first_line, updated_at, scope
        FROM keyword_docs
        """
    ).fetchall()
    if not all_rows:
        return

    needs_migration = any(
        (r[1] or "") != (r[1] or "").casefold().strip() for r in all_rows
    )
    if not needs_migration:
        return

    merged: dict = {}
    for r in all_rows:
        directory = r[0]
        keyword = (r[1] or "").casefold().strip()
        doc_path = r[2]
        if not keyword:
            continue
        key = (directory, keyword, doc_path)
        occ = float(r[3] or 0)
        first_line = int(r[4] or 0)
        updated_at = r[5]
        scope = r[6] or ""
        if key in merged:
            existing = merged[key]
            existing["occurrence_count"] += occ
            if first_line and (existing["first_line"] == 0 or first_line < existing["first_line"]):
                existing["first_line"] = first_line
            if updated_at and (existing["updated_at"] is None or updated_at > existing["updated_at"]):
                existing["updated_at"] = updated_at
            # ``scope`` normally follows ``directory`` so colliding rows
            # agree, but don't rely on it: pick deterministically (prefer a
            # non-empty value, then the lexicographically greatest) so the
            # merge result never depends on row iteration order.
            if scope and (not existing["scope"] or scope > existing["scope"]):
                existing["scope"] = scope
        else:
            merged[key] = {
                "occurrence_count": occ,
                "first_line": first_line,
                "updated_at": updated_at,
                "scope": scope,
            }
    conn.execute("DELETE FROM keyword_docs")
    conn.executemany(
        """
        INSERT INTO keyword_docs
            (directory, keyword, doc_path, occurrence_count, first_line,
             updated_at, scope)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                d, k, dp, v["occurrence_count"], v["first_line"],
                v["updated_at"], v["scope"],
            )
            for (d, k, dp), v in merged.items()
        ],
    )


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user table name in the database (debug helper)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
