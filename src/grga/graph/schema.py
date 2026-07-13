"""Graph schema management (normalized migration, schema v8).

``ensure_schema(conn)`` creates the current graph tables and, when it finds
an older database, performs a **one-shot** migration. Three historical
migration hops are chained so any pre-v8 database converges on v8 in a
single atomic transaction:

* v1/v2/v3 → v4: the chunk-level co-occurrence model
  (``docs/design/plan-2026-07-11-chunk-migration.md``).
* v4 → v5: **normalization** — the repeated ``directory`` / ``keyword`` /
  ``doc_path`` TEXT columns are interned into three lookup tables and the
  business tables reference them by integer id
  (``docs/design/plan-2026-07-11-schema-v5-normalization.md``). This is a
  ~73% size reduction on large corpora (327 MB → ~87 MB measured).
* v7 → v8: **keyword-primary** — ``keyword_pairs`` is promoted from a
  derived index to a first-class knowledge asset (write-time gated by
  ``BUILD_MIN_PAIR_COUNT`` / ``BUILD_MIN_NPMI``), and new asset tables
  ``keyword_stats`` / ``scope_stats`` are added. ``keyword_chunks`` /
  ``chunks`` are marked deprecated but *retained* in Phase 1 for backward
  compatibility; a later phase drops them
  (``docs/design/plan-2026-07-12-keyword-primary.md``).

Current (v8) tables owned by this module:

Intern tables:

* ``directories``  — ``id`` → resolved scope path (``''`` = unscoped).
* ``keywords``     — ``id`` → casefolded keyword.
* ``docs``         — ``id`` → ``(dir_id, doc_path)``.

Knowledge-asset tables (v8):

* ``keyword_stats``  — per-scope df / occurrence stats keyed by
  ``(dir_id, kw_id)``. Feeds npmi denominators + hub-word signals.
* ``scope_stats``    — per-scope scalars (``n_chunks`` = npmi N,
  ``n_docs``, ``vocab_size``).

Business tables (all referencing the intern tables by id):

* ``keyword_pairs``   — multi-source pair table: ``source='chunk_cooccur'``
  rows carry ``cooccur_count`` + ``npmi`` (both stored, v8);
  ``source='llm_promoted'`` rows come from ``grga index promote``.
* ``heading_docs``    — Markdown chapter structure.
* ``llm_extension_votes`` — LLM pair vote buffer.
* ``keyword_counts`` / ``keyword_sources`` — v1 deep-search heat/source
  tables (also normalized to ``dir_id`` / ``kw_id`` in v5).
* ``auto_ngram_dict`` — tokenizer's unsupervised n-gram dictionary.

Deprecated tables (retained in Phase 1, dropped later):

* ``chunks``         — per-doc chunk line ranges (only remaining reader is
  ``related._total_chunks_in_scope`` → ``scope_stats.n_chunks`` after
  Phase 3).
* ``keyword_chunks`` — chunk-level inverted index (~30× larger than the
  ``keyword_stats`` doc-level aggregate it feeds; scales linearly with
  corpus size, was the primary storage bottleneck).

The migration is idempotent: an already-v8 database is a no-op fast path.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Union

SCHEMA_VERSION = 9

# Source markers for ``keyword_pairs`` rows. These are the *only* two
# allowed values (plan HC4 / §3.3).
CHUNK_COOCCUR_SOURCE = "chunk_cooccur"
LLM_PROMOTED_SOURCE = "llm_promoted"

# Write-time admission thresholds for the keyword-primary model (schema v8,
# docs/design/plan-2026-07-12-keyword-primary.md §3.3). A ``chunk_cooccur``
# pair is only recorded as a knowledge asset when it clears BOTH gates in
# its scope. These fix the *lower bound* of the stored pairs: a query may
# filter with a *stricter* ``min_npmi`` on demand, but can never recover a
# pair that fell below ``BUILD_MIN_NPMI`` here (it was never memorised).
# ``llm_promoted`` pairs bypass these (npmi is NULL; they arrive via
# vote→promote).
BUILD_MIN_PAIR_COUNT = 5
BUILD_MIN_NPMI = 0.2

# v1 deep-search counter tables. Normalized in v5 but conceptually still the
# "v1" heat / source pair. Kept as a named group for tests / introspection.
V1_TABLES = ("keyword_counts", "keyword_sources")

# Intern (lookup) tables introduced in v5.
INTERN_TABLES = ("directories", "keywords", "docs")

# Graph business tables owned by this module (v9 keyword-primary shape).
# ``chunks`` / ``keyword_chunks`` were dropped in v9 — pair co-occurrence
# is now derived from RAM at build time, and the doc-level inverted index
# ``keyword_docs`` covers the seed→docs lookup the old ``keyword_chunks``
# provided for ``mode=heading`` and ``inspect``.
GRAPH_TABLES = ("keyword_pairs", "heading_docs",
                "llm_extension_votes", "auto_ngram_dict")

# Knowledge-asset tables owned by the v8/v9 keyword-primary model.
V8_ASSET_TABLES = ("keyword_stats", "scope_stats")
V9_ASSET_TABLES = ("keyword_docs",)


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
    # A migration holds a write lock (``BEGIN IMMEDIATE``) for the duration
    # of its one-shot rewrite. The full v7→v9 chain is measured at ~40s on a
    # 450 MB corpus (pair prune + keyword_docs backfill + chunk-table drop).
    # Python's default 5s busy timeout would make a *concurrent* grga
    # invocation fail with ``database is locked`` before the migrator
    # finishes. A 180s timeout leaves comfortable headroom over the observed
    # 40s so the loser waits out the migration and then takes the idempotent
    # fast-path instead of crashing.
    conn = sqlite3.connect(str(resolved), timeout=180.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``user_version`` pragma value."""
    cur = conn.execute("PRAGMA user_version")
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row[0])


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}


def _has_any_pairs(conn: sqlite3.Connection) -> bool:
    """True when a ``keyword_pairs`` table exists and holds at least one row.

    Used to gate the lossy v7→v8 migration warning: a fresh / empty DB has
    no pairs to prune, so the "will permanently delete" banner should not
    fire (it would be wrong and would pollute ``--json`` first-run output).
    Cheap ``EXISTS`` probe — never scans the whole table.
    """
    if not _table_exists(conn, "keyword_pairs"):
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM keyword_pairs LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


_CREATE_INTERN_TABLES = """
CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY,
    kw TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    doc_path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    content_hash TEXT,
    UNIQUE(dir_id, doc_path)
);
"""

# Global key/value metadata (schema v7). Currently holds
# ``toolchain_fingerprint`` used by ``grga index build`` to emit a
# "toolchain changed, consider rebuild" warning. Never a judgement input —
# see docs/design/plan-2026-07-11-build-vs-rebuild.md §7.
_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_CREATE_KEYWORD_PAIRS = """
CREATE TABLE IF NOT EXISTS keyword_pairs (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
    source TEXT NOT NULL,
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi REAL,
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_a_id, kw_b_id, source)
) WITHOUT ROWID;
"""

_CREATE_GRAPH_TABLES = """
CREATE TABLE IF NOT EXISTS heading_docs (
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    heading_path TEXT NOT NULL,
    chapter_id TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (doc_id, heading_path, chapter_id)
);
CREATE TABLE IF NOT EXISTS llm_extension_votes (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
    vote_count REAL NOT NULL DEFAULT 0,
    last_voted TEXT,
    promoted_at TEXT,
    PRIMARY KEY (dir_id, kw_a_id, kw_b_id)
);
CREATE TABLE IF NOT EXISTS keyword_counts (
    scope TEXT NOT NULL DEFAULT '',
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope, dir_id, kw_id)
);
CREATE TABLE IF NOT EXISTS keyword_sources (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    path TEXT NOT NULL,
    line_number INTEGER NOT NULL DEFAULT 0,
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_id, path, line_number)
);
CREATE TABLE IF NOT EXISTS auto_ngram_dict (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    phrase TEXT NOT NULL,
    n INTEGER NOT NULL,
    count INTEGER NOT NULL,
    npmi REAL NOT NULL,
    branch_ent REAL NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (dir_id, phrase)
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_docs_dir
    ON docs (dir_id);
CREATE INDEX IF NOT EXISTS idx_keyword_pairs_a
    ON keyword_pairs (dir_id, kw_a_id);
CREATE INDEX IF NOT EXISTS idx_keyword_pairs_b
    ON keyword_pairs (dir_id, kw_b_id);
CREATE INDEX IF NOT EXISTS idx_heading_docs_head
    ON heading_docs (heading_path);
CREATE INDEX IF NOT EXISTS idx_keyword_counts_count
    ON keyword_counts (dir_id, count DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_sources_kw
    ON keyword_sources (dir_id, kw_id);
CREATE INDEX IF NOT EXISTS idx_votes_dir
    ON llm_extension_votes (dir_id, vote_count DESC);
CREATE INDEX IF NOT EXISTS idx_auto_ngram_dir
    ON auto_ngram_dict (dir_id);
CREATE INDEX IF NOT EXISTS idx_keyword_stats_scope
    ON keyword_stats (dir_id, chunk_df DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_docs_dir
    ON keyword_docs (dir_id, kw_id);
CREATE INDEX IF NOT EXISTS idx_keyword_docs_reverse
    ON keyword_docs (dir_id, doc_id);
"""

# v8 keyword-primary tables (docs/design/plan-2026-07-12-keyword-primary.md §2.1).
# ``keyword_stats`` — per-scope df / occurrence stats keyed by (dir_id, kw_id).
# Fed by build; consumed at query time to supply npmi denominators (df side)
# and hub-word discriminators (chunk_df signal). Separate from ``keywords``
# because df is per-scope while ``keywords`` is a global 1:1 dictionary
# — merging them would denormalise (repeat kw text per scope).
#
# ``scope_stats`` — per-scope scalars: ``n_chunks`` (npmi denominator N),
# ``n_docs`` / ``vocab_size`` (introspection). In v9 ``n_chunks`` fully
# replaces the dropped ``chunks`` table as the npmi denominator source.
_CREATE_V8_ASSET_TABLES = """
CREATE TABLE IF NOT EXISTS keyword_stats (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    chunk_df INTEGER NOT NULL DEFAULT 0,
    doc_df INTEGER NOT NULL DEFAULT 0,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS scope_stats (
    dir_id INTEGER PRIMARY KEY REFERENCES directories(id),
    n_chunks INTEGER NOT NULL DEFAULT 0,
    n_docs INTEGER NOT NULL DEFAULT 0,
    vocab_size INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""

# v9 keyword-primary table (docs/design/plan-2026-07-12-keyword-primary.md §2,
# Phase 3b-3). ``keyword_docs`` — doc-level inverted index replacing the
# dropped chunk-level ``keyword_chunks``. ~30× smaller (one row per
# (kw, doc) instead of per (kw, doc, chunk)). Provides the seed→docs lookup
# that ``related(mode='heading')`` and ``graph inspect`` need.
#
# ``chunk_count`` = number of distinct chunks in this doc containing the kw
# (so ``SUM(chunk_count)`` per kw reconstructs ``keyword_stats.chunk_df``).
# ``dir_id`` is denormalised from ``docs`` for a fast per-scope filter
# without a JOIN (dir_id never changes for a given doc).
_CREATE_V9_ASSET_TABLES = """
CREATE TABLE IF NOT EXISTS keyword_docs (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kw_id, doc_id)
) WITHOUT ROWID;
"""


def _split_sql(script: str) -> List[str]:
    """Split a ``;``-separated SQL script into individual statements.

    ``sqlite3.Connection.executescript`` issues an implicit ``COMMIT``
    before running the script, so it cannot be used inside our migration
    transaction. This helper feeds ``conn.execute`` statement by
    statement, preserving the outer BEGIN / COMMIT.
    """
    out: List[str] = []
    for chunk in script.split(";"):
        stmt = chunk.strip()
        if stmt:
            out.append(stmt)
    return out


def _create_current_tables(conn: sqlite3.Connection) -> None:
    """Create every current graph table + index (idempotent).

    Executes individual statements so the caller's transaction is
    preserved (:func:`sqlite3.Connection.executescript` would COMMIT).
    """
    for stmt in _split_sql(
        _CREATE_INTERN_TABLES
        + _CREATE_META_TABLE
        + _CREATE_KEYWORD_PAIRS
        + _CREATE_GRAPH_TABLES
        + _CREATE_V8_ASSET_TABLES
        + _CREATE_V9_ASSET_TABLES
        + _CREATE_INDEXES
    ):
        conn.execute(stmt)


def _ensure_docs_v7_columns(conn: sqlite3.Connection) -> None:
    """Add the schema v7 ``docs`` incremental-build columns if missing.

    ``size`` / ``mtime`` / ``content_hash`` back the three-level change
    detection in ``grga index build`` (see
    docs/design/plan-2026-07-11-build-vs-rebuild.md §4-5). On a v6 database
    the ``docs`` table predates them; add them idempotently. SQLite's
    ``ALTER TABLE ADD COLUMN`` has no portable ``IF NOT EXISTS``, so probe
    ``PRAGMA table_info`` first. New columns default to NULL, which the
    builder reads as "no prior record" → full processing on first pass.
    """
    if not _table_exists(conn, "docs"):
        return
    cols = _table_columns(conn, "docs")
    for name, decl in (
        ("size", "INTEGER"),
        ("mtime", "REAL"),
        ("content_hash", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE docs ADD COLUMN {name} {decl}")


def _ensure_docs_v9_columns(conn: sqlite3.Connection) -> None:
    """Add the schema v9 ``docs.n_chunks_with_kw`` column if missing.

    After Phase 3b-3 drops the raw ``chunks`` table, ``scope_stats.n_chunks``
    (the npmi denominator N = chunks-that-contain-at-least-one-keyword)
    must be maintainable incrementally without re-scanning a chunks table.
    Store the per-doc contribution on ``docs`` itself so
    ``scope_stats.n_chunks = SUM(docs.n_chunks_with_kw)`` per scope stays
    accurate through add/update/remove of individual files.

    Default 0 means "no keywords indexed yet" — matches the reality of a
    doc that hasn't been through pass-2 tokenisation.
    """
    if not _table_exists(conn, "docs"):
        return
    cols = _table_columns(conn, "docs")
    if "n_chunks_with_kw" not in cols:
        conn.execute(
            "ALTER TABLE docs ADD COLUMN n_chunks_with_kw INTEGER NOT NULL DEFAULT 0"
        )


def _migration_announce(db_path: Optional[str], stage: str) -> None:
    """Emit a one-line stderr notice around the migration.

    Silent when ``GRGA_QUIET_MIGRATION=1`` (for test noise reduction) or when
    ``sys.stderr`` is unavailable. This is UX plumbing — never let a failure
    here derail the migration itself.
    """
    if os.environ.get("GRGA_QUIET_MIGRATION"):
        return
    try:
        loc = f" at {db_path}" if db_path else ""
        print(f"[grga] {stage}{loc}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _check_disk_space(conn: sqlite3.Connection) -> None:
    """Raise a clear message if the migration is likely to run out of space.

    Historically this guarded the v4 → v5 migration (which briefly doubles
    the on-disk size while intermediate ``v4__*`` tables coexist with the
    source shape). It now also guards v7 → v8, which grows the DB during
    the pair-table DELETE before ``VACUUM`` can reclaim freelist pages
    (measured 452 MB → 620 MB mid-migration on real prod DB). We require
    ~2× the current DB size in free space; if less, abort *before* touching
    the DB so the user sees an actionable message instead of a random
    ``disk is full`` deep in a copy or DELETE loop.
    """
    # Resolve the DB path from the connection. sqlite3 exposes it via
    # ``PRAGMA database_list`` (file for the main DB, "" for :memory:).
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return
    db_file = row[2] if len(row) >= 3 else ""
    if not db_file:
        return  # in-memory or file-less DB — no disk check meaningful
    try:
        db_size = os.path.getsize(db_file)
    except OSError:
        return
    try:
        stat = shutil.disk_usage(os.path.dirname(db_file) or ".")
    except OSError:
        return
    required = db_size * 2
    if stat.free < required:
        mb = 1024 * 1024
        raise RuntimeError(
            f"grga schema v{SCHEMA_VERSION} migration needs about "
            f"{required // mb} MB free space (current DB is "
            f"{db_size // mb} MB), but only {stat.free // mb} MB is "
            f"available on the volume holding {db_file}. Free up space "
            "and retry, or run `grga index vacuum` first to shrink the "
            "existing DB."
        )


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Idempotently bring ``conn`` to the current normalized schema (v5).

    Safe to call on:

    * a brand-new empty database (creates every current table);
    * a v1-only database (v1 counters normalized in place, v3 legacy pair
      table dropped, chunk-level tables added — all in one hop);
    * a v2/v3 database (``keyword_docs`` dropped, ``keyword_pairs``
      restructured, ``chunks`` / ``keyword_chunks`` created — v4 shape —
      then normalized to v5 shape in the same transaction);
    * a v4 database (normalized to v5);
    * a database already at ``SCHEMA_VERSION`` (no-op fast path).

    Does **not** commit — the caller's transaction (implicit or explicit)
    is honoured throughout. Migration (any → v5) *does* need its own atomic
    scope: :func:`_migrate_forward` opens a nested transaction only when
    the caller has none in progress, otherwise it participates in the
    caller's transaction (Python 3.12 sqlite3 forbids nesting ``BEGIN``
    and would raise ``cannot start a transaction within a transaction`` if
    we opened one unconditionally). The ``user_version`` stamp is issued
    **inside** that transaction so the shape and the version can never
    diverge on a crash / SIGKILL.

    Returns the final ``user_version`` after the call.
    """
    current = read_schema_version(conn)
    if current >= SCHEMA_VERSION:
        # Fast path: DB is at the current schema. Assume migration created
        # every table when it stamped user_version = SCHEMA_VERSION, so we
        # avoid the sqlite_master probes ``_create_current_tables`` would
        # otherwise perform on every call.
        return current

    _migrate_forward(conn)
    return SCHEMA_VERSION


def _migrate_forward(conn: sqlite3.Connection) -> None:
    """Bring ``conn`` from any pre-v5 shape to v5 in one atomic transaction.

    Runs v3→v4 then v4→v5 back-to-back inside a single ``BEGIN IMMEDIATE``
    (or the caller's txn if one is already in flight) so partial state can
    never land on disk. The ``PRAGMA user_version`` stamp lives inside the
    same transaction so an SIGKILL between the data rewrite and the version
    bump cannot leave the DB in a mislabelled state.

    ``BEGIN IMMEDIATE`` is issued inside the ``try`` block so a lock
    contention (concurrent writer holding the DB during a big migration)
    surfaces as a normal exception path and does not skip the rollback
    contract.
    """
    owned_txn = not conn.in_transaction
    # Real (non-fast-path) migration is user-visible latency. Warn once
    # before we grab the write lock so the CLI does not appear to hang.
    # ``_migrate_v4_to_v5_inplace`` and the v3→v4 hop are both expensive
    # only when there is real data; on empty DBs the whole thing is a
    # sub-millisecond no-op and printing a banner would be spammy.
    db_path_str: Optional[str] = None
    announced = False
    row = None
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and len(row) >= 3:
        db_path_str = row[2] or None

    # v7 → v8 is a LOSSY migration — permanently DELETEs ``keyword_pairs``
    # rows below ``BUILD_MIN_PAIR_COUNT`` / ``BUILD_MIN_NPMI``. Warn users
    # unconditionally (bypassing the 20 MB "may take a while" gate below)
    # so small DBs also see the notice, and so downgrade-to-v7 users know
    # to back up first. See docs/design/plan-2026-07-12-keyword-primary.md
    # §6. Silenced only by ``GRGA_QUIET_MIGRATION=1`` (test / CI).
    #
    # Only warn when there is actually data at risk: a fresh/empty DB
    # (no ``keyword_pairs`` table, or an empty one) has nothing to prune,
    # and emitting a scary "will delete" banner there is both wrong and
    # noisy (it would also pollute ``grga index build --json`` on first
    # run). We therefore gate on the pair table existing with >= 1 row.
    pre_version = read_schema_version(conn)
    if pre_version < 8 <= SCHEMA_VERSION and db_path_str and _has_any_pairs(conn):
        _migration_announce(
            db_path_str,
            f"WARNING: v{pre_version}→v8 upgrade will permanently delete "
            "``keyword_pairs`` chunk_cooccur rows failing any of: "
            "(1) independence — df_a == df_b == cooccur_count (glued, "
            "npmi≈1.0 artefact); "
            "(2) scope-adaptive count floor — min 2, up to "
            f"{int(BUILD_MIN_PAIR_COUNT)} on >20000-chunk scopes; "
            f"(3) npmi < {BUILD_MIN_NPMI:.2f}. "
            "``llm_promoted`` rows are unaffected. This is NOT reversible; "
            "only ``grga index rebuild`` can recompute the pruned data. "
            "Back up the DB now if you may need to roll back.",
        )
    # v8 → v9 (Phase 3b-3) is the second lossy hop — it DROPs the entire
    # ``keyword_chunks`` + ``chunks`` tables. A user already on v8 (they
    # ran Phase 1) sees the pair-prune warning nowhere in this upgrade
    # unless we announce it here. Gate on the deprecated table actually
    # existing so fresh/already-v9 DBs stay silent. Reuse GRGA_QUIET_MIGRATION.
    if (
        pre_version < 9 <= SCHEMA_VERSION
        and db_path_str
        and _table_exists(conn, "keyword_chunks")
    ):
        _migration_announce(
            db_path_str,
            f"WARNING: v{pre_version}→v9 upgrade will permanently DROP the "
            "``keyword_chunks`` and ``chunks`` tables. They are replaced by "
            "the doc-level ``keyword_docs`` index (30× smaller). This is "
            "NOT reversible — pre-v9 grga cannot read the resulting DB, "
            "and chunk-level data cannot be reconstructed without a full "
            "rebuild. Back up the DB now if you may need to roll back.",
        )
    if db_path_str:
        try:
            # Announce only when the DB is big enough for the migration to
            # take user-noticeable time (>= 20 MB is our rough threshold).
            if os.path.getsize(db_path_str) >= 20 * 1024 * 1024:
                _migration_announce(
                    db_path_str,
                    f"Upgrading index to schema v{SCHEMA_VERSION} (one-time; typically a few seconds per 100 MB)",
                )
                announced = True
        except OSError:
            pass

    # Pre-flight disk-space check when the migration would rewrite real
    # data. Cheap ``PRAGMA``; only actually stat's the FS when the DB has
    # a real file backing.
    _check_disk_space(conn)

    try:
        if owned_txn:
            conn.execute("BEGIN IMMEDIATE")
        # Re-check under the write lock (TOCTOU guard): another process may
        # have already migrated between the caller's read_schema_version()
        # and this point.
        if read_schema_version(conn) >= SCHEMA_VERSION:
            _create_current_tables(conn)
            if owned_txn:
                conn.execute("COMMIT")
            return

        # v1/v2/v3 → v4 first (only if we detect legacy shape). This
        # produces the v4 shape (denormalized TEXT columns) in memory,
        # then v4 → v5 folds it into the intern tables.
        if _needs_v3_to_v4(conn):
            _migrate_v3_to_v4_inplace(conn)

        # v4 → v5 normalization. Only run when the DB is actually pre-v5 —
        # running the intern-table rewrite on an already-v5 DB would fail
        # on the pre-existing directories/keywords/docs tables.
        if read_schema_version(conn) < 5:
            _migrate_v4_to_v5_inplace(conn)

        # v5 → v6 was purely additive (auto_ngram_dict + its index).
        # v6 → v7 is also additive: docs gains size/mtime/content_hash
        # incremental-build metadata, plus a global meta key/value table.
        # The ``_create_current_tables`` script uses ``IF NOT EXISTS``
        # throughout so it is idempotent on any post-v4 shape; the docs
        # column adds need a separate helper because ``ALTER TABLE`` has
        # no portable ``IF NOT EXISTS`` clause.
        _create_current_tables(conn)
        _ensure_docs_v7_columns(conn)
        _ensure_docs_v9_columns(conn)

        # v7 → v8 keyword-primary migration
        # (docs/design/plan-2026-07-12-keyword-primary.md §6). Additive +
        # lossy-prune: creates the v8 asset tables (already done above by
        # ``_create_current_tables``), backfills them from keyword_chunks /
        # chunks, then prunes sub-threshold ``chunk_cooccur`` pairs. The
        # deprecated ``keyword_chunks`` / ``chunks`` tables are *kept* here
        # (Phase 1) so build/related keep working; a later phase drops them.
        if read_schema_version(conn) < 8:
            _migrate_v7_to_v8_inplace(conn)

        # v8 → v9 (Phase 3b-3): backfill keyword_docs from keyword_chunks,
        # then DROP the deprecated chunks/keyword_chunks tables. Once we
        # reach v9 the pair table is derived from RAM at build time and no
        # chunk-level inverted index remains on disk.
        if read_schema_version(conn) < 9:
            _migrate_v8_to_v9_inplace(conn)

        # Ensure the npmi-ordered pair indexes exist. Owned outside
        # ``_CREATE_INDEXES`` (see :func:`_ensure_npmi_indexes`) so the
        # v7→v8 hop can build them post-prune; here we cover the fresh-install
        # and already-migrated fast paths. Idempotent.
        _ensure_npmi_indexes(conn)

        # Stamp the version **inside** the transaction so shape + version
        # commit atomically.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

        if owned_txn:
            conn.execute("COMMIT")
        if announced:
            _migration_announce(
                db_path_str,
                f"Schema v{SCHEMA_VERSION} upgrade complete. Run `grga index vacuum` to reclaim disk space.",
            )
    except Exception:
        if owned_txn and conn.in_transaction:
            # Only attempt rollback when we actually opened a txn (and it
            # is still open — a lock-contention failure on BEGIN IMMEDIATE
            # leaves in_transaction False, and there is nothing to roll
            # back in that case).
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise


# ---------------------------------------------------------------------------
# v3 → v4 helpers (the chunk-level migration). Retained so v1/v3 databases
# can still reach v5 via the v3→v4→v5 chain in one atomic transaction.
# ---------------------------------------------------------------------------


def _needs_v3_to_v4(conn: sqlite3.Connection) -> bool:
    """True when the DB has legacy pre-v4 pair/doc tables that need rebuild.

    v9 note: ``keyword_docs`` is now a *v9* table (doc-level inverted
    index) that shares the name of an unrelated legacy v3 table. The
    presence check therefore also gates on ``user_version < 4`` so a v9
    database is not misidentified as legacy.
    """
    current = read_schema_version(conn)
    if current >= 4:
        return False
    if _table_exists(conn, "keyword_docs"):
        return True
    if _table_exists(conn, "keyword_pairs"):
        cols = _table_columns(conn, "keyword_pairs")
        if "source" not in cols or "cooccur_count" not in cols:
            return True
    return False


def _migrate_v3_to_v4_inplace(conn: sqlite3.Connection) -> None:
    """Legacy chunk-level migration (retained for v1/v3 → v5 chains).

    Salvages old promotion rows (legacy ``scope='llm_promoted'``) into a
    v4-shaped ``keyword_pairs`` and drops ``keyword_docs``. Must run inside
    the same transaction as the subsequent v4 → v5 normalisation.
    """
    salvaged: List[tuple] = []
    if (
        _table_exists(conn, "keyword_pairs")
        and "source" not in _table_columns(conn, "keyword_pairs")
    ):
        rows = conn.execute(
            "SELECT directory, keyword_a, keyword_b, count, updated_at "
            "FROM keyword_pairs WHERE scope = ?",
            (LLM_PROMOTED_SOURCE,),
        ).fetchall()
        salvaged = [
            (
                row["directory"],
                row["keyword_a"],
                row["keyword_b"],
                float(row["count"] or 0.0),
                row["updated_at"] if "updated_at" in row.keys() else None,
            )
            for row in rows
        ]
        conn.execute("DROP TABLE keyword_pairs")

    if _table_exists(conn, "keyword_docs"):
        conn.execute("DROP TABLE IF EXISTS keyword_docs")

    # Create the *v4-shape* keyword_pairs so we can insert salvaged rows,
    # then v4→v5 rewrites it. We define it inline (rather than via
    # _create_current_tables) so the schema for this intermediate shape
    # is explicit and never leaves the transaction.
    conn.execute(
        """
        CREATE TABLE keyword_pairs (
            scope TEXT NOT NULL DEFAULT '',
            directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL,
            keyword_b TEXT NOT NULL,
            source TEXT NOT NULL,
            cooccur_count REAL NOT NULL DEFAULT 0,
            npmi REAL,
            updated_at TEXT,
            PRIMARY KEY (directory, keyword_a, keyword_b, source)
        )
        """
    )
    if salvaged:
        conn.executemany(
            """
            INSERT OR IGNORE INTO keyword_pairs
                (scope, directory, keyword_a, keyword_b, source,
                 cooccur_count, npmi, updated_at)
            VALUES ('', ?, ?, ?, ?, ?, NULL, ?)
            """,
            [
                (directory, kw_a, kw_b, LLM_PROMOTED_SOURCE, count, updated)
                for (directory, kw_a, kw_b, count, updated) in salvaged
            ],
        )


# ---------------------------------------------------------------------------
# v4 → v5 normalization: intern directories / keywords / docs, rewrite the
# business tables to reference them by id.
# ---------------------------------------------------------------------------


def _v4_has_normalized(conn: sqlite3.Connection) -> bool:
    """True when v5 intern tables + v5-shaped business tables already exist."""
    if not _table_exists(conn, "directories"):
        return False
    if not _table_exists(conn, "keywords"):
        return False
    if not _table_exists(conn, "docs"):
        return False
    if _table_exists(conn, "keyword_chunks"):
        cols = _table_columns(conn, "keyword_chunks")
        if "kw_id" not in cols or "doc_id" not in cols:
            return False
    return True


def _migrate_v4_to_v5_inplace(conn: sqlite3.Connection) -> None:
    """Fold v4 denormalized TEXT columns into v5 intern-based layout.

    Runs inside the caller's transaction. The whole rewrite is pure SQL
    (bulk ``INSERT OR IGNORE ... SELECT ... JOIN``) so the migration cost
    stays inside SQLite's engine — no Python row loop, no per-row
    ``INSERT + SELECT`` round-trip for the ~646K distinct keywords that a
    large corpus produces.

    Strategy:

    1. Create the intern tables (empty).
    2. Rename each v4 table to ``v4__<name>``.
    3. Populate the intern tables with ``INSERT OR IGNORE ... SELECT DISTINCT``
       drawing from every v4 shadow table (so a keyword mentioned only in
       ``keyword_pairs`` still gets an id).
    4. Create each v5-shaped business table.
    5. ``INSERT ... SELECT ... JOIN`` from each ``v4__<name>`` into the new
       table, joining through the intern tables to translate TEXT → id.
    6. Drop the ``v4__<name>`` shadow tables.
    7. Call :func:`_create_current_tables` to add any table that did not
       exist in the source DB, plus every index.
    """
    if _v4_has_normalized(conn):
        # Already normalized (or brand-new empty DB about to be built). Just
        # ensure every table + index exists.
        _create_current_tables(conn)
        return

    # 1. Create intern tables (empty).
    for stmt in _split_sql(_CREATE_INTERN_TABLES):
        conn.execute(stmt)

    # 2. Rename every v4 shadow table that exists. We need the renames done
    #    before the DISTINCT sweeps so the shadow-table names are stable in
    #    every subsequent SELECT.
    v4_tables = [
        "keyword_chunks", "chunks", "keyword_pairs", "heading_docs",
        "llm_extension_votes", "keyword_counts", "keyword_sources",
    ]
    present: Dict[str, bool] = {}
    for name in v4_tables:
        exists = _table_exists(conn, name)
        # ``keyword_counts`` / ``keyword_sources`` may be pre-v5 (TEXT
        # columns) or already v5-shaped; only rename the pre-v5 shape.
        if exists and name in ("keyword_counts", "keyword_sources"):
            if "kw_id" in _table_columns(conn, name):
                # Already v5-normalised — leave it alone.
                exists = False
        present[name] = exists
        if exists:
            conn.execute(f"ALTER TABLE {name} RENAME TO v4__{name}")

    # 3. Populate intern tables. ``UNION`` collapses duplicates before the
    #    OR IGNORE round-trip, so directories/keywords each get exactly one
    #    row per distinct string regardless of source-table cardinality.
    dir_sources: List[str] = []
    if present["keyword_chunks"]:
        dir_sources.append("SELECT directory FROM v4__keyword_chunks")
    if present["chunks"]:
        dir_sources.append("SELECT directory FROM v4__chunks")
    if present["keyword_pairs"]:
        dir_sources.append("SELECT directory FROM v4__keyword_pairs")
    if present["heading_docs"]:
        dir_sources.append("SELECT directory FROM v4__heading_docs")
    if present["llm_extension_votes"]:
        dir_sources.append("SELECT directory FROM v4__llm_extension_votes")
    if present["keyword_counts"]:
        dir_sources.append("SELECT directory FROM v4__keyword_counts")
    if present["keyword_sources"]:
        dir_sources.append("SELECT directory FROM v4__keyword_sources")
    if dir_sources:
        conn.execute(
            "INSERT OR IGNORE INTO directories(path) "
            + " UNION ".join(dir_sources)
        )

    kw_sources: List[str] = []
    if present["keyword_chunks"]:
        kw_sources.append("SELECT keyword FROM v4__keyword_chunks")
    if present["keyword_pairs"]:
        kw_sources.append("SELECT keyword_a FROM v4__keyword_pairs")
        kw_sources.append("SELECT keyword_b FROM v4__keyword_pairs")
    if present["llm_extension_votes"]:
        kw_sources.append("SELECT keyword_a FROM v4__llm_extension_votes")
        kw_sources.append("SELECT keyword_b FROM v4__llm_extension_votes")
    if present["keyword_counts"]:
        kw_sources.append("SELECT term FROM v4__keyword_counts")
    if present["keyword_sources"]:
        kw_sources.append("SELECT keyword FROM v4__keyword_sources")
    if kw_sources:
        conn.execute(
            "INSERT OR IGNORE INTO keywords(kw) "
            + " UNION ".join(kw_sources)
        )

    # docs = distinct (directory, doc_path) across every table that carries
    # a doc_path column. INSERT resolves ``directory`` → dir_id via join.
    doc_sources: List[str] = []
    if present["keyword_chunks"]:
        doc_sources.append(
            "SELECT DISTINCT directory, doc_path FROM v4__keyword_chunks"
        )
    if present["chunks"]:
        doc_sources.append(
            "SELECT DISTINCT directory, doc_path FROM v4__chunks"
        )
    if present["heading_docs"]:
        doc_sources.append(
            "SELECT DISTINCT directory, doc_path FROM v4__heading_docs"
        )
    if doc_sources:
        conn.execute(
            "INSERT OR IGNORE INTO docs(dir_id, doc_path) "
            "SELECT dir.id, u.doc_path "
            "FROM (" + " UNION ".join(doc_sources) + ") AS u "
            "JOIN directories dir ON dir.path = u.directory"
        )

    # 4 + 5. Create each v5-shaped business table and populate it in one
    # set-based INSERT.

    if present["keyword_chunks"]:
        conn.execute(
            """
            CREATE TABLE keyword_chunks (
                kw_id INTEGER NOT NULL REFERENCES keywords(id),
                doc_id INTEGER NOT NULL REFERENCES docs(id),
                chunk_id INTEGER NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (kw_id, doc_id, chunk_id)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO keyword_chunks
                (kw_id, doc_id, chunk_id, occurrence_count)
            SELECT k.id, d.id, sc.chunk_id, sc.occurrence_count
            FROM v4__keyword_chunks sc
            JOIN keywords k ON k.kw = sc.keyword
            JOIN directories dir ON dir.path = sc.directory
            JOIN docs d ON d.dir_id = dir.id AND d.doc_path = sc.doc_path
            """
        )
        conn.execute("DROP TABLE v4__keyword_chunks")

    if present["chunks"]:
        conn.execute(
            """
            CREATE TABLE chunks (
                doc_id INTEGER NOT NULL REFERENCES docs(id),
                chunk_id INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                char_count INTEGER NOT NULL,
                PRIMARY KEY (doc_id, chunk_id)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO chunks
                (doc_id, chunk_id, start_line, end_line, char_count)
            SELECT d.id, sc.chunk_id, sc.start_line, sc.end_line, sc.char_count
            FROM v4__chunks sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN docs d ON d.dir_id = dir.id AND d.doc_path = sc.doc_path
            """
        )
        conn.execute("DROP TABLE v4__chunks")

    if present["keyword_pairs"]:
        for stmt in _split_sql(_CREATE_KEYWORD_PAIRS):
            conn.execute(stmt)
        conn.execute(
            """
            INSERT OR IGNORE INTO keyword_pairs
                (dir_id, kw_a_id, kw_b_id, source,
                 cooccur_count, npmi, updated_at)
            SELECT dir.id, ka.id, kb.id, sc.source,
                   sc.cooccur_count, sc.npmi, sc.updated_at
            FROM v4__keyword_pairs sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN keywords ka ON ka.kw = sc.keyword_a
            JOIN keywords kb ON kb.kw = sc.keyword_b
            """
        )
        conn.execute("DROP TABLE v4__keyword_pairs")

    if present["heading_docs"]:
        conn.execute(
            """
            CREATE TABLE heading_docs (
                doc_id INTEGER NOT NULL REFERENCES docs(id),
                heading_path TEXT NOT NULL,
                chapter_id TEXT NOT NULL DEFAULT '',
                start_line INTEGER NOT NULL DEFAULT 0,
                end_line INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (doc_id, heading_path, chapter_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO heading_docs
                (doc_id, heading_path, chapter_id,
                 start_line, end_line, updated_at)
            SELECT d.id, sc.heading_path, sc.chapter_id,
                   sc.start_line, sc.end_line, sc.updated_at
            FROM v4__heading_docs sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN docs d ON d.dir_id = dir.id AND d.doc_path = sc.doc_path
            """
        )
        conn.execute("DROP TABLE v4__heading_docs")

    if present["llm_extension_votes"]:
        legacy_cols = _table_columns(conn, "v4__llm_extension_votes")
        has_promoted_at = "promoted_at" in legacy_cols
        conn.execute(
            """
            CREATE TABLE llm_extension_votes (
                dir_id INTEGER NOT NULL REFERENCES directories(id),
                kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
                kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
                vote_count REAL NOT NULL DEFAULT 0,
                last_voted TEXT,
                promoted_at TEXT,
                PRIMARY KEY (dir_id, kw_a_id, kw_b_id)
            )
            """
        )
        promoted_expr = "sc.promoted_at" if has_promoted_at else "NULL"
        conn.execute(
            f"""
            INSERT OR IGNORE INTO llm_extension_votes
                (dir_id, kw_a_id, kw_b_id, vote_count, last_voted, promoted_at)
            SELECT dir.id, ka.id, kb.id, sc.vote_count, sc.last_voted,
                   {promoted_expr}
            FROM v4__llm_extension_votes sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN keywords ka ON ka.kw = sc.keyword_a
            JOIN keywords kb ON kb.kw = sc.keyword_b
            """
        )
        conn.execute("DROP TABLE v4__llm_extension_votes")

    if present["keyword_counts"]:
        conn.execute(
            """
            CREATE TABLE keyword_counts (
                scope TEXT NOT NULL DEFAULT '',
                dir_id INTEGER NOT NULL REFERENCES directories(id),
                kw_id INTEGER NOT NULL REFERENCES keywords(id),
                count REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (scope, dir_id, kw_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO keyword_counts
                (scope, dir_id, kw_id, count, updated_at)
            SELECT sc.scope, dir.id, k.id, sc.count, sc.updated_at
            FROM v4__keyword_counts sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN keywords k ON k.kw = sc.term
            """
        )
        conn.execute("DROP TABLE v4__keyword_counts")

    if present["keyword_sources"]:
        conn.execute(
            """
            CREATE TABLE keyword_sources (
                dir_id INTEGER NOT NULL REFERENCES directories(id),
                kw_id INTEGER NOT NULL REFERENCES keywords(id),
                path TEXT NOT NULL,
                line_number INTEGER NOT NULL DEFAULT 0,
                count REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (dir_id, kw_id, path, line_number)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO keyword_sources
                (dir_id, kw_id, path, line_number, count, updated_at)
            SELECT dir.id, k.id, sc.path, sc.line_number, sc.count, sc.updated_at
            FROM v4__keyword_sources sc
            JOIN directories dir ON dir.path = sc.directory
            JOIN keywords k ON k.kw = sc.keyword
            """
        )
        conn.execute("DROP TABLE v4__keyword_sources")

    # 7. Create any table that didn't exist in the source DB, plus all
    # indexes (idempotent).
    _create_current_tables(conn)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user table name in the database (debug helper)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# v7 → v8 keyword-primary migration.
#
# Design: docs/design/plan-2026-07-12-keyword-primary.md §6.
# Runs inside the caller's migration transaction (BEGIN IMMEDIATE from
# ``_migrate_forward``). Two operations:
#
#   1. Backfill: derive ``keyword_stats`` (per-scope chunk_df / doc_df /
#      total_occurrences) from ``keyword_chunks`` + ``docs``, and
#      ``scope_stats`` (n_chunks / n_docs / vocab_size) from ``chunks`` +
#      ``docs`` + the just-backfilled ``keyword_stats``.
#
#   2. Lossy prune: DELETE ``keyword_pairs`` rows with source='chunk_cooccur'
#      that fall below the v8 admission threshold (BUILD_MIN_PAIR_COUNT /
#      BUILD_MIN_NPMI). ``llm_promoted`` rows are untouched. Idempotent — a
#      re-run finds nothing to delete.
#
# NB Phase 1 does *not* drop ``keyword_chunks`` / ``chunks`` — build and
# related still read them until Phase 2/3 rewires the code paths. This
# keeps the schema migration reversible (roll back user_version, tables
# are still there) and lets us stage the rollout.
# ---------------------------------------------------------------------------


def _migrate_v7_to_v8_inplace(conn: sqlite3.Connection) -> None:
    """Backfill v8 asset tables from v7 shape and prune sub-threshold pairs.

    Idempotent: ``INSERT OR REPLACE`` on ``keyword_stats`` / ``scope_stats``
    lets a re-run refresh the values; the pair DELETE is a no-op the second
    time (rows already gone). Callers must hold the migration transaction.

    Backfill semantics:

    * ``keyword_stats(dir_id, kw_id)``:
        - ``chunk_df`` = COUNT(DISTINCT chunk) from ``keyword_chunks`` in scope
        - ``doc_df``   = COUNT(DISTINCT doc)   from ``keyword_chunks`` in scope
        - ``total_occurrences`` = SUM(occurrence_count) in scope

    * ``scope_stats(dir_id)``:
        - ``n_chunks`` = COUNT of ``chunks`` rows in scope (npmi denominator N)
        - ``n_docs``   = COUNT of ``docs`` rows in scope
        - ``vocab_size`` = DISTINCT kw_id count in this scope's keyword_stats

    Prune semantics: rows with ``source='chunk_cooccur'`` are removed when
    they fail any of the three write-time gates — independence
    (``cooccur_count >= MAX(chunk_df_a, chunk_df_b)``), the scope-adaptive
    count floor (from ``scope_stats.n_chunks``), or the npmi floor
    (``npmi IS NULL`` / ``< BUILD_MIN_NPMI``). See
    :func:`_prune_low_score_pairs`. This is the "storage contract" — the
    resulting pair table represents "knowledge that cleared the write-time
    admission gates at migration time".

    Index-build ordering: the two new ``(dir_id, kw_a_id/kw_b_id, npmi DESC)``
    indexes are DROPPED before the prune DELETE and re-CREATED afterwards.
    Building them on the pre-prune 2.79M-row table only to have DELETE
    invalidate 94% of the entries is wasted work; deferring keeps the
    index build linear in the *surviving* pair count.
    """
    now = _utc_now_iso()

    # If the v7 keyword_chunks table is missing (fresh install path where we
    # jumped straight into v8 tables), skip backfill — there is nothing to
    # derive from. Callers on a fresh DB will just have empty v8 tables.
    if not _table_exists(conn, "keyword_chunks"):
        # Still run the pair prune (idempotent no-op if pairs empty).
        _prune_low_score_pairs(conn)
        return

    # 1. Backfill keyword_stats. Group by (docs.dir_id, kw_id).
    conn.execute(
        """
        INSERT OR REPLACE INTO keyword_stats
            (dir_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at)
        SELECT d.dir_id, kc.kw_id,
               COUNT(*) AS chunk_df,
               COUNT(DISTINCT kc.doc_id) AS doc_df,
               COALESCE(SUM(kc.occurrence_count), 0) AS total_occurrences,
               ?
        FROM keyword_chunks kc
        JOIN docs d ON d.id = kc.doc_id
        GROUP BY d.dir_id, kc.kw_id
        """,
        (now,),
    )

    # 2. Backfill scope_stats. Two subqueries because ``chunks`` and ``docs``
    #    have independent scopes-per-scope aggregates.
    if _table_exists(conn, "chunks"):
        conn.execute(
            """
            INSERT OR REPLACE INTO scope_stats
                (dir_id, n_chunks, n_docs, vocab_size, updated_at)
            SELECT
                d.dir_id,
                (SELECT COUNT(*) FROM chunks c
                 JOIN docs d2 ON d2.id = c.doc_id
                 WHERE d2.dir_id = d.dir_id) AS n_chunks,
                COUNT(DISTINCT d.id) AS n_docs,
                (SELECT COUNT(*) FROM keyword_stats ks
                 WHERE ks.dir_id = d.dir_id) AS vocab_size,
                ?
            FROM docs d
            GROUP BY d.dir_id
            """,
            (now,),
        )
    else:
        # Extremely unusual: v7 without chunks. Backfill scope_stats without
        # n_chunks (leaves 0; query path will fall back to legacy code until
        # a rebuild fixes it).
        conn.execute(
            """
            INSERT OR REPLACE INTO scope_stats
                (dir_id, n_chunks, n_docs, vocab_size, updated_at)
            SELECT d.dir_id,
                   0,
                   COUNT(DISTINCT d.id),
                   (SELECT COUNT(*) FROM keyword_stats ks
                    WHERE ks.dir_id = d.dir_id),
                   ?
            FROM docs d
            GROUP BY d.dir_id
            """,
            (now,),
        )

    # 3. Prune sub-threshold chunk_cooccur pairs. The two npmi-ordered
    # indexes over ``keyword_pairs`` are deliberately NOT in the shared
    # ``_CREATE_INDEXES`` DDL — they're expensive to build on the pre-prune
    # ~2.79M-row table only to have DELETE invalidate ~94% of the entries
    # (and force per-row index maintenance across millions of DELETEs).
    # Own them here so they're built exactly once, on the post-prune
    # surviving pair set. Fresh installs create them via
    # :func:`_ensure_npmi_indexes` (idempotent no-op if already present).
    _prune_low_score_pairs(conn)
    _ensure_npmi_indexes(conn)


def _ensure_npmi_indexes(conn: sqlite3.Connection) -> None:
    """Create the two `(dir_id, kw_a_id/kw_b_id, npmi DESC)` indexes if missing.

    Owned separately from ``_CREATE_INDEXES`` so a v7→v8 migration can
    build them on the post-prune (~170k row) survivor set instead of the
    pre-prune (~2.79M row) table. Fresh v9 installs also call this so
    the indexes are always present after any successful migration.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kp_a_npmi "
        "ON keyword_pairs (dir_id, kw_a_id, npmi DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kp_b_npmi "
        "ON keyword_pairs (dir_id, kw_b_id, npmi DESC)"
    )


def _prune_low_score_pairs(conn: sqlite3.Connection) -> int:
    """Delete ``chunk_cooccur`` pairs failing the v8+ admission gates.

    Returns the number of rows deleted (for logging / tests). Safe to call
    on an empty pair table (returns 0). ``llm_promoted`` rows are never
    touched — their npmi is NULL by design.

    Three gates match the build-time ones in
    :func:`grga.graph.build._finalize_pairs_from_temp`:

    * **Gate 1 · independence (parameter-free)**: drop pairs where the two
      words are perfectly glued at chunk level —
      ``chunk_df_a == chunk_df_b == cooccur_count`` (three-way equality).
      This is the exact npmi=1.0 degenerate condition
      (``p_ab == p_a == p_b``) and is scope- / size-independent. Strict
      equality rather than ``co >= max(df_a, df_b)`` so incremental builds
      whose ``cooccur_count`` is grow-only against a freshly-recomputed
      ``chunk_df`` are not misclassified. Added in v3.2.

    * **Gate 2 · scope-adaptive count floor**: since v3.2 the write floor
      scales with ``scope_stats.n_chunks`` via
      :func:`grga.graph.build.resolve_min_pair_count`. On pre-v3.2 DBs
      where scope_stats is absent or shows n_chunks=0, we fall back to the
      historical fixed floor (:data:`BUILD_MIN_PAIR_COUNT` = 5).

    * **Gate 3 · npmi floor**: ``npmi IS NULL`` OR
      ``npmi < BUILD_MIN_NPMI``. Unchanged.

    Runs one DELETE per scope (bounded by ``directories`` cardinality —
    typically 1-10) so the per-scope adaptive floor can be applied cleanly.
    ``keyword_stats`` lookups use its ``(dir_id, kw_id)`` PRIMARY KEY
    (WITHOUT ROWID) so Gate 1 is a bounded log-time probe per row.
    """
    from grga.graph.build import resolve_min_pair_count  # avoid cycle at import

    total_deleted = 0

    # Enumerate scopes that have any chunk_cooccur pairs. Adaptive floor
    # comes from scope_stats; missing rows fall back to the fixed floor.
    scope_rows = list(conn.execute(
        "SELECT DISTINCT dir_id FROM keyword_pairs WHERE source = ?",
        (CHUNK_COOCCUR_SOURCE,),
    ))
    for (dir_id,) in scope_rows:
        row = conn.execute(
            "SELECT n_chunks FROM scope_stats WHERE dir_id = ?",
            (int(dir_id),),
        ).fetchone()
        if row is None or not row[0]:
            # No scope_stats yet — use pre-v3.2 fixed floor. Gate 1 still
            # applies (parameter-free).
            floor = int(BUILD_MIN_PAIR_COUNT)
        else:
            floor = resolve_min_pair_count(int(row[0]))

        # Gate 1 (strict): three-way equality co == df_a == df_b. If either
        # keyword_stats row is missing, its chunk_df is treated as 0; the
        # equality then requires co == 0, which pairs never satisfy
        # (cooccur_count is always >= 1) — so a missing endpoint no longer
        # forces a defensive drop (fixes the pre-v3.2 asymmetry with the
        # build path).
        cur = conn.execute(
            """
            DELETE FROM keyword_pairs
            WHERE dir_id = ?
              AND source = ?
              AND (
                cooccur_count < ?
                OR npmi IS NULL
                OR npmi < ?
                OR (
                    cooccur_count = COALESCE(
                        (SELECT chunk_df FROM keyword_stats
                          WHERE dir_id = keyword_pairs.dir_id
                            AND kw_id = keyword_pairs.kw_a_id), 0)
                    AND
                    cooccur_count = COALESCE(
                        (SELECT chunk_df FROM keyword_stats
                          WHERE dir_id = keyword_pairs.dir_id
                            AND kw_id = keyword_pairs.kw_b_id), 0)
                )
              )
            """,
            (int(dir_id), CHUNK_COOCCUR_SOURCE, floor, float(BUILD_MIN_NPMI)),
        )
        total_deleted += cur.rowcount or 0

    return total_deleted


def _utc_now_iso() -> str:
    """UTC timestamp in ``YYYY-MM-DDTHH:MM:SSZ`` form (matches v5+ convention)."""
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# v8 → v9 keyword-primary migration (Phase 3b-3).
#
# Design: docs/design/plan-2026-07-12-keyword-primary.md §4.3. Two ops:
#
#   1. Backfill ``keyword_docs`` from the still-present ``keyword_chunks``:
#      collapse the chunk-level index to a doc-level inverted index, with
#      ``chunk_count`` preserving the "how many chunks in this doc contain
#      this kw" signal that the old ``keyword_chunks`` COUNT(*) gave.
#
#   2. DROP TABLE ``keyword_chunks`` and ``chunks``. Their sole readers
#      (build's ``recompute_pairs`` / ``recompute_scope_assets`` and
#      ``related._docs_for_keywords_chunks`` / ``inspect._cooccurrences``)
#      switch to ``keyword_docs`` + in-RAM pair derivation in the same
#      commit. ``chunks.n_chunks_in_scope`` is already carried by
#      ``scope_stats.n_chunks`` (populated in Phase 1) so nothing else
#      depends on the raw chunks table.
#
# The migration is idempotent — a re-run finds no keyword_chunks table,
# skips backfill, and the DROPs are no-ops.
# ---------------------------------------------------------------------------


def _migrate_v8_to_v9_inplace(conn: sqlite3.Connection) -> None:
    """Backfill ``keyword_docs`` + ``docs.n_chunks_with_kw``, correct
    ``scope_stats.n_chunks``, then drop the two deprecated tables.

    Runs inside the outer migration transaction (BEGIN IMMEDIATE from
    ``_migrate_forward``). Safe on already-v9 shape: ``keyword_chunks`` is
    already gone → backfill/correction are no-ops; ``DROP TABLE IF EXISTS``
    is safe.

    Semantics correction: Phase 1's v7→v8 migration populated
    ``scope_stats.n_chunks`` from ``COUNT(*) FROM chunks`` — which counts
    *every* chunk including empty lines with no keywords. But the historical
    npmi denominator used by pair recomputation was "chunks with at least
    one keyword" (see the pre-v9 ``recompute_pairs`` SQL). v9 consolidates
    on the chunks-with-keyword definition: ``scope_stats.n_chunks`` is now
    the authoritative source, and after this migration it matches the pair
    table's npmi denominator exactly.
    """
    if _table_exists(conn, "keyword_chunks"):
        # 1. Backfill keyword_docs: collapse chunk-level rows to (kw, doc).
        conn.execute(
            """
            INSERT OR REPLACE INTO keyword_docs
                (dir_id, kw_id, doc_id, occurrence_count, chunk_count)
            SELECT d.dir_id, kc.kw_id, kc.doc_id,
                   COALESCE(SUM(kc.occurrence_count), 0),
                   COUNT(*)
            FROM keyword_chunks kc
            JOIN docs d ON d.id = kc.doc_id
            GROUP BY d.dir_id, kc.kw_id, kc.doc_id
            """,
        )
        # 2. Backfill docs.n_chunks_with_kw: distinct (doc, chunk) pairs
        #    where the chunk contains ≥ 1 keyword, grouped by doc.
        conn.execute(
            """
            UPDATE docs SET n_chunks_with_kw = COALESCE((
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM keyword_chunks kc
                    WHERE kc.doc_id = docs.id
                    GROUP BY kc.chunk_id
                )
            ), 0)
            """,
        )
        # 3. Recompute scope_stats.n_chunks from the corrected per-doc counts
        #    so npmi denominator matches pair-recomputation semantics.
        conn.execute(
            """
            UPDATE scope_stats SET
                n_chunks = COALESCE((
                    SELECT SUM(d.n_chunks_with_kw) FROM docs d
                    WHERE d.dir_id = scope_stats.dir_id
                ), 0)
            """,
        )

    # 4. DROP the deprecated chunk-level tables. Idempotent via IF EXISTS.
    conn.execute("DROP TABLE IF EXISTS keyword_chunks")
    conn.execute("DROP TABLE IF EXISTS chunks")


def vacuum_db(db_path: Union[str, Path]) -> Dict[str, int]:
    """Run ``VACUUM`` on the graph DB and report the size delta.

    ``VACUUM`` cannot run inside a transaction, so this opens its own
    dedicated connection (in autocommit mode) and closes it when done. The
    schema-v5 migration leaves ~240 MB of freelist pages on a large corpus;
    this reclaims them. The operation is safe to run at any time.

    Returns ``{"before_bytes": int, "after_bytes": int, "freed_bytes": int}``
    for the caller to report. Freed bytes may be negative if the DB was
    already tightly packed and VACUUM incurred small overhead.
    """
    resolved = Path(db_path).expanduser()
    before = os.path.getsize(str(resolved)) if resolved.exists() else 0
    # ``isolation_level=None`` puts the connection in autocommit mode, which
    # is required by VACUUM. We do not go through :func:`connect` because
    # that sets ``row_factory``; VACUUM does not need it.
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
