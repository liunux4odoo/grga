"""Tests for the v4 → v5 normalization migration (schema.py).

Focus areas beyond :mod:`tests.test_graph_schema`:

* a fully-populated v4 database migrates to v5 with **bit-exact** row
  content when joined back to text;
* the intern tables deduplicate repeated directory / keyword / doc_path
  values;
* v1 and v3 legacy databases converge on v5 in a single hop;
* the ``IdInterner`` get-or-insert / reverse-lookup helper is consistent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.ids import IdInterner
from grga.graph.schema import (
    INTERN_TABLES,
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
)


def _make_v4_db(path: Path) -> None:
    """Build a populated v4-shape database (denormalized TEXT columns)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE keyword_pairs (
            scope TEXT NOT NULL DEFAULT '', directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            source TEXT NOT NULL, cooccur_count REAL NOT NULL DEFAULT 0,
            npmi REAL, updated_at TEXT,
            PRIMARY KEY (directory, keyword_a, keyword_b, source)
        );
        CREATE TABLE chunks (
            scope TEXT NOT NULL DEFAULT '', directory TEXT NOT NULL,
            doc_path TEXT NOT NULL, chunk_id INTEGER NOT NULL,
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
            char_count INTEGER NOT NULL,
            PRIMARY KEY (directory, doc_path, chunk_id)
        );
        CREATE TABLE keyword_chunks (
            directory TEXT NOT NULL, keyword TEXT NOT NULL,
            doc_path TEXT NOT NULL, chunk_id INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (directory, keyword, doc_path, chunk_id)
        );
        CREATE TABLE heading_docs (
            directory TEXT NOT NULL, heading_path TEXT NOT NULL,
            doc_path TEXT NOT NULL, chapter_id TEXT NOT NULL DEFAULT '',
            start_line INTEGER NOT NULL DEFAULT 0, end_line INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, heading_path, doc_path, chapter_id)
        );
        CREATE TABLE llm_extension_votes (
            directory TEXT NOT NULL, keyword_a TEXT NOT NULL,
            keyword_b TEXT NOT NULL, vote_count REAL NOT NULL DEFAULT 0,
            last_voted TEXT,
            PRIMARY KEY (directory, keyword_a, keyword_b)
        );
        CREATE TABLE keyword_counts (
            scope TEXT NOT NULL, directory TEXT NOT NULL, term TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, term)
        );
        CREATE TABLE keyword_sources (
            directory TEXT NOT NULL, keyword TEXT NOT NULL, path TEXT NOT NULL,
            line_number INTEGER NOT NULL DEFAULT 0, count REAL NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, keyword, path, line_number)
        );
        """
    )
    d1, d2 = "/corpus_a", "/corpus_b"
    # keyword_chunks: shared keyword across scopes + docs.
    kc_rows = [
        (d1, "alpha", "a.md", 0, 3),
        (d1, "beta", "a.md", 0, 1),
        (d1, "alpha", "b.md", 1, 2),
        (d2, "alpha", "c.md", 0, 5),
        (d2, "gamma", "c.md", 0, 1),
    ]
    conn.executemany(
        "INSERT INTO keyword_chunks (directory, keyword, doc_path, chunk_id, occurrence_count) "
        "VALUES (?, ?, ?, ?, ?)",
        kc_rows,
    )
    conn.executemany(
        "INSERT INTO chunks (directory, doc_path, chunk_id, start_line, end_line, char_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (d1, "a.md", 0, 1, 1, 40),
            (d1, "b.md", 1, 5, 5, 30),
            (d2, "c.md", 0, 1, 1, 55),
        ],
    )
    conn.executemany(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, source, "
        "cooccur_count, npmi, updated_at) VALUES ('', ?, ?, ?, ?, ?, ?, ?)",
        [
            # NB fixture values chosen to also clear the v8 admission threshold
            # (cooccur >= BUILD_MIN_PAIR_COUNT=5 AND npmi >= BUILD_MIN_NPMI=0.2)
            # so the "bit-exact through migration chain" assertion downstream
            # still holds after ensure_schema() runs the v7 → v8 hop
            # (docs/design/plan-2026-07-12-keyword-primary.md §6).
            (d1, "alpha", "beta", "chunk_cooccur", 6.0, 0.42, "2026-01-01"),
            (d2, "alpha", "gamma", "llm_promoted", 5.0, None, "2026-01-02"),
        ],
    )
    conn.executemany(
        "INSERT INTO heading_docs (directory, heading_path, doc_path, chapter_id, "
        "start_line, end_line, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (d1, "Install > macOS", "a.md", "install/macos", 1, 10, "2026-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO llm_extension_votes (directory, keyword_a, keyword_b, vote_count, last_voted) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (d1, "alpha", "beta", 2.0, "2026-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO keyword_counts (scope, directory, term, count, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("global", d1, "alpha", 7.0, "2026-01-01"),
            ("global", d1, "beta", 3.0, "2026-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO keyword_sources (directory, keyword, path, line_number, count, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (d1, "alpha", "/corpus_a/a.md", 3, 1.0, "2026-01-01"),
        ],
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()


def test_v4_to_v5_bit_exact(tmp_path: Path):
    db = tmp_path / "v4.db"
    _make_v4_db(db)

    # Capture the v4 view before migrating.
    raw = sqlite3.connect(str(db))
    raw.row_factory = sqlite3.Row
    v4_kc = [
        tuple(r) for r in raw.execute(
            "SELECT directory, keyword, doc_path, chunk_id, occurrence_count "
            "FROM keyword_chunks ORDER BY 1,2,3,4"
        )
    ]
    v4_pairs = [
        tuple(r) for r in raw.execute(
            "SELECT directory, keyword_a, keyword_b, source, cooccur_count, npmi "
            "FROM keyword_pairs ORDER BY 1,2,3,4"
        )
    ]
    v4_counts = [
        tuple(r) for r in raw.execute(
            "SELECT scope, directory, term, count FROM keyword_counts ORDER BY 1,2,3"
        )
    ]
    raw.close()

    conn = connect(db)
    try:
        assert read_schema_version(conn) == 4
        ensure_schema(conn)
        conn.commit()
        assert read_schema_version(conn) == SCHEMA_VERSION

        for name in INTERN_TABLES:
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()[0] == 1

        # keyword_chunks joined back == v4.
        v5_kc = [
            tuple(r) for r in conn.execute(
                "SELECT dir.path, k.kw, d.doc_path, kc.chunk_id, kc.occurrence_count "
                "FROM keyword_chunks kc "
                "JOIN keywords k ON k.id = kc.kw_id "
                "JOIN docs d ON d.id = kc.doc_id "
                "JOIN directories dir ON dir.id = d.dir_id "
                "ORDER BY 1,2,3,4"
            )
        ]
        assert v5_kc == v4_kc

        v5_pairs = [
            tuple(r) for r in conn.execute(
                "SELECT dir.path, ka.kw, kb.kw, p.source, p.cooccur_count, p.npmi "
                "FROM keyword_pairs p "
                "JOIN directories dir ON dir.id = p.dir_id "
                "JOIN keywords ka ON ka.id = p.kw_a_id "
                "JOIN keywords kb ON kb.id = p.kw_b_id "
                "ORDER BY 1,2,3,4"
            )
        ]
        assert v5_pairs == v4_pairs

        v5_counts = [
            tuple(r) for r in conn.execute(
                "SELECT c.scope, dir.path, k.kw, c.count "
                "FROM keyword_counts c "
                "JOIN directories dir ON dir.id = c.dir_id "
                "JOIN keywords k ON k.id = c.kw_id "
                "ORDER BY 1,2,3"
            )
        ]
        assert v5_counts == v4_counts
    finally:
        conn.close()


def test_v4_to_v5_interns_deduplicate(tmp_path: Path):
    db = tmp_path / "v4.db"
    _make_v4_db(db)
    conn = connect(db)
    try:
        ensure_schema(conn)
        conn.commit()
        # 2 distinct directories, 4 distinct keywords (alpha/beta/gamma),
        # docs unique per (dir, path).
        assert conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0] == 2
        # alpha, beta, gamma => 3 keywords.
        assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 3
        # a.md/b.md in corpus_a, c.md in corpus_b => 3 docs.
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 3
        # alpha appears in both scopes but is a single keyword row.
        alpha_rows = conn.execute(
            "SELECT COUNT(*) FROM keywords WHERE kw = 'alpha'"
        ).fetchone()[0]
        assert alpha_rows == 1
    finally:
        conn.close()


def test_v4_to_v5_promoted_marker_column_present(tmp_path: Path):
    """v5 builds ``promoted_at`` into llm_extension_votes natively."""
    db = tmp_path / "v4.db"
    _make_v4_db(db)
    conn = connect(db)
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(llm_extension_votes)")}
        assert "promoted_at" in cols
        assert "dir_id" in cols and "kw_a_id" in cols and "kw_b_id" in cols
    finally:
        conn.close()


def test_interner_get_or_insert_stable(tmp_path: Path):
    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        a1 = interner.dir_id("/x")
        a2 = interner.dir_id("/x")
        assert a1 == a2
        k1 = interner.kw_id("alpha")
        k2 = interner.kw_id("alpha")
        assert k1 == k2
        d1 = interner.doc_id(a1, "a.md")
        d2 = interner.doc_id(a1, "a.md")
        assert d1 == d2

        # Reverse lookups round-trip.
        assert interner.dir_text(a1) == "/x"
        assert interner.kw_text(k1) == "alpha"
        assert interner.doc_row(d1) == (a1, "a.md")

        # Read-only miss returns None.
        assert interner.kw_id_ro("never-seen") is None
        assert interner.dir_id_ro("/never") is None
        assert interner.dir_id_ro(None) is None
    finally:
        conn.close()


def test_interner_batch_reverse_lookups(tmp_path: Path):
    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        ids = [interner.kw_id(kw) for kw in ("a", "b", "c")]
        texts = interner.kw_texts(ids)
        assert texts == {ids[0]: "a", ids[1]: "b", ids[2]: "c"}

        got = interner.kw_ids_ro(["a", "c", "missing"])
        assert got == {"a": ids[0], "c": ids[2]}
    finally:
        conn.close()


def test_interner_kw_ids_batches_inserts(tmp_path: Path):
    """``IdInterner.kw_ids`` returns ids for every input and dedups misses."""
    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        got = interner.kw_ids(["alpha", "beta", "gamma", "alpha"])
        assert set(got.keys()) == {"alpha", "beta", "gamma"}
        # Ids stable on second call (get-or-insert idempotent).
        again = interner.kw_ids(["alpha", "beta", "gamma", "delta"])
        assert again["alpha"] == got["alpha"]
        assert again["beta"] == got["beta"]
        assert again["gamma"] == got["gamma"]
        assert "delta" in again
    finally:
        conn.close()


def test_pragma_user_version_atomic_with_migration(tmp_path: Path):
    """Regression for Finding 1: the version stamp is inside the txn.

    Simulate a mid-migration crash by wrapping ``_create_current_tables``
    to throw. After rollback, both the shape rewrite and the version bump
    must be absent; the DB should still be at its pre-migration version.
    """
    import grga.graph.schema as schema_mod

    db = tmp_path / "v4.db"
    _make_v4_db(db)

    original = schema_mod._create_current_tables

    def _explode(conn):
        # Let the intern-table CREATE + bulk copies run so we exercise the
        # non-trivial migration body, then blow up right before the
        # ``PRAGMA user_version`` write inside ``_migrate_forward``.
        original(conn)
        raise RuntimeError("simulated crash")

    schema_mod._create_current_tables = _explode
    try:
        conn = connect(db)
        try:
            with pytest.raises(RuntimeError):
                ensure_schema(conn)
            # Migration rolled back: version is unchanged, no v5 shape.
            assert read_schema_version(conn) == 4
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='directories'"
            ).fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        schema_mod._create_current_tables = original


def test_vacuum_db_reports_savings(tmp_path: Path):
    """``vacuum_db`` compacts and returns before/after sizes."""
    from grga.graph.schema import vacuum_db

    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        # Insert then delete a bunch of rows to create freelist pages.
        conn.execute("INSERT OR IGNORE INTO directories(path) VALUES ('/scratch')")
        did = conn.execute(
            "SELECT id FROM directories WHERE path='/scratch'"
        ).fetchone()[0]
        for i in range(500):
            conn.execute("INSERT INTO keywords(kw) VALUES (?)", (f"kw{i:04d}",))
        conn.commit()
        conn.execute("DELETE FROM keywords WHERE id > 100")
        conn.commit()
    finally:
        conn.close()

    result = vacuum_db(db)
    assert result["before_bytes"] > 0
    assert result["after_bytes"] > 0
    # After a real DELETE + VACUUM the DB should not grow.
    assert result["after_bytes"] <= result["before_bytes"]
