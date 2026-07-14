"""Knowledge pack export / import (plan-2026-07-14 §5).

A *knowledge pack* is a self-contained SQLite file (``.grgapkg``) that
carries the **knowledge layer** for one (or every) scope in the source
DB, stripped of every ``evidence`` row. It lets you learn a
co-occurrence dictionary on one machine and reuse it on another without
dragging along that machine's ``docs`` / ``keyword_docs`` /
``scope_directories`` metadata.

Layer boundary is enforced strictly (plan-2026-07-14 §1.1):

* **Knowledge (in the pack)**: ``scopes`` (selected) + referenced
  ``keywords`` + ``keyword_pairs`` + ``keyword_stats`` + ``scope_stats``
  + ``auto_ngram_dict`` + ``llm_extension_votes`` + ``meta``.
* **Evidence (never in the pack)**: ``scope_directories`` / ``docs`` /
  ``keyword_docs`` / ``heading_docs`` / ``keyword_sources`` /
  ``keyword_counts``.

A pack **is itself a v10 SQLite DB with the evidence tables empty**, so
consumers can ``ATTACH DATABASE`` and copy rows directly — no bespoke
parser needed. The ``meta`` table carries a ``grga_pack.*`` namespace
recording who exported what, when.

Import policy (design D9): only ``--as <new-name>`` is supported this
round. ``--merge-into <existing>`` needs an npmi-merge policy (MAX vs
weighted vs replace) whose semantics are undecided; the API rejects it
with an explicit hint until we have a caller pushing for it.

Public API:

* :func:`export_knowledge_pack` — write ``.grgapkg``.
* :func:`import_knowledge_pack` — load ``.grgapkg`` into a target DB
  under a new scope name.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Union

from grga.graph.schema import (
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
    resolve_scope_name,
)


# Pack format constants.
PACK_FORMAT = "grga-knowledge-pack"
PACK_FORMAT_VERSION = "1"

# Tables copied verbatim (mapped via kw_id / scope_id remap during import).
_KNOWLEDGE_TABLES = (
    "scopes",
    "keywords",
    "keyword_pairs",
    "keyword_stats",
    "scope_stats",
    "auto_ngram_dict",
    "llm_extension_votes",
)


@dataclass
class ExportStats:
    """Summary of an :func:`export_knowledge_pack` call."""

    out_path: str
    scopes_included: List[str] = field(default_factory=list)
    n_keywords: int = 0
    n_pairs: int = 0
    n_stats: int = 0
    n_scope_stats: int = 0
    n_auto_ngram: int = 0
    n_votes: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "out_path": self.out_path,
            "scopes_included": list(self.scopes_included),
            "n_keywords": int(self.n_keywords),
            "n_pairs": int(self.n_pairs),
            "n_stats": int(self.n_stats),
            "n_scope_stats": int(self.n_scope_stats),
            "n_auto_ngram": int(self.n_auto_ngram),
            "n_votes": int(self.n_votes),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class ImportStats:
    """Summary of an :func:`import_knowledge_pack` call."""

    pack_path: str
    into_scope: str
    n_keywords_new: int = 0
    n_pairs: int = 0
    n_stats: int = 0
    n_auto_ngram: int = 0
    n_votes: int = 0
    scope_stats_written: bool = False
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pack_path": self.pack_path,
            "into_scope": self.into_scope,
            "n_keywords_new": int(self.n_keywords_new),
            "n_pairs": int(self.n_pairs),
            "n_stats": int(self.n_stats),
            "n_auto_ngram": int(self.n_auto_ngram),
            "n_votes": int(self.n_votes),
            "scope_stats_written": self.scope_stats_written,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_knowledge_pack(
    *,
    out_path: Union[str, Path],
    scope: Optional[str] = None,
    db_path: Union[str, Path, None] = None,
) -> ExportStats:
    """Write a knowledge pack.

    Args:
        out_path: destination ``.grgapkg`` file. Overwritten if it exists.
        scope: single scope name to export, or ``None`` to export
            **every** scope in the source DB (``grga index export --all``).
        db_path: source SQLite path (defaults to the shared deep-search DB).

    Returns:
        :class:`ExportStats` with per-table counts.

    Raises:
        LookupError: named scope does not exist.
        ValueError: source DB has no scopes to export.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    out_resolved = Path(out_path).expanduser().resolve()
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    if out_resolved.exists():
        out_resolved.unlink()

    source_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    src = connect(source_db)
    try:
        ensure_schema(src)
        scope_ids, scope_names = _resolve_export_scope_set(src, scope)
        stats = ExportStats(
            out_path=str(out_resolved),
            scopes_included=scope_names,
        )

        # Fresh v10 shell for the pack. Evidence tables stay empty by
        # construction — we only INSERT into knowledge tables below.
        dst = connect(out_resolved)
        try:
            ensure_schema(dst)
            with dst:  # atomic transaction on the destination
                _copy_scopes(src, dst, scope_ids)
                stats.n_keywords = _copy_keywords_referenced(
                    src, dst, scope_ids
                )
                stats.n_pairs = _copy_pairs(src, dst, scope_ids)
                stats.n_stats = _copy_keyword_stats(src, dst, scope_ids)
                stats.n_scope_stats = _copy_scope_stats(src, dst, scope_ids)
                stats.n_auto_ngram = _copy_auto_ngram(src, dst, scope_ids)
                stats.n_votes = _copy_votes(src, dst, scope_ids)
                _write_pack_meta(src, dst, scope_names)
        finally:
            dst.close()
    finally:
        src.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


def _resolve_export_scope_set(
    src: sqlite3.Connection,
    scope: Optional[str],
) -> tuple[List[int], List[str]]:
    """Return ``(scope_ids, scope_names)`` for the export set.

    ``scope=None`` selects every scope. A named scope that doesn't exist
    raises :class:`LookupError`. An empty source DB raises :class:`ValueError`.
    """
    if scope is None:
        rows = src.execute(
            "SELECT id, name FROM scopes ORDER BY name ASC"
        ).fetchall()
        if not rows:
            raise ValueError(
                "source DB has no scopes to export — run "
                "`grga index build --scope <name> --directory <path>` first"
            )
        return [int(r["id"]) for r in rows], [str(r["name"]) for r in rows]
    resolved = resolve_scope_name(scope)
    if resolved is None:
        raise ValueError("scope must be a non-empty name")
    row = src.execute(
        "SELECT id FROM scopes WHERE name = ?", (resolved,)
    ).fetchone()
    if row is None:
        raise LookupError(f"scope not found in source DB: {resolved!r}")
    return [int(row["id"])], [resolved]


def _copy_scopes(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> None:
    """Copy ``scopes`` rows preserving id + name.

    Ids stay stable inside a pack so we can join against them in the same
    file without a remap; on ``import`` the destination will remap ids
    into its own space.
    """
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"SELECT id, name, kind, created_at FROM scopes WHERE id IN ({ph})",
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO scopes (id, name, kind, created_at) "
        "VALUES (?, ?, ?, ?)",
        [
            (int(r["id"]), str(r["name"]), r["kind"], r["created_at"])
            for r in rows
        ],
    )


def _copy_keywords_referenced(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    """Copy every ``keywords`` row referenced by any exported knowledge row.

    Pack size stays small on selective exports: only the vocab actually
    reachable from ``scope_ids`` is included, not the source's entire
    ``keywords`` table.
    """
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT id, kw FROM keywords WHERE id IN (
            SELECT kw_a_id FROM keyword_pairs WHERE scope_id IN ({ph})
            UNION SELECT kw_b_id FROM keyword_pairs WHERE scope_id IN ({ph})
            UNION SELECT kw_id  FROM keyword_stats  WHERE scope_id IN ({ph})
            UNION SELECT kw_a_id FROM llm_extension_votes WHERE scope_id IN ({ph})
            UNION SELECT kw_b_id FROM llm_extension_votes WHERE scope_id IN ({ph})
        )
        """,
        scope_ids * 5,
    ).fetchall()
    dst.executemany(
        "INSERT INTO keywords (id, kw) VALUES (?, ?)",
        [(int(r["id"]), str(r["kw"])) for r in rows],
    )
    return len(rows)


def _copy_pairs(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT scope_id, kw_a_id, kw_b_id, source,
               cooccur_count, npmi, updated_at
        FROM keyword_pairs WHERE scope_id IN ({ph})
        """,
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO keyword_pairs (scope_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    return len(rows)


def _copy_keyword_stats(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT scope_id, kw_id, chunk_df, doc_df, total_occurrences,
               updated_at
        FROM keyword_stats WHERE scope_id IN ({ph})
        """,
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO keyword_stats (scope_id, kw_id, chunk_df, doc_df, "
        "total_occurrences, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    return len(rows)


def _copy_scope_stats(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT scope_id, n_chunks, n_docs, vocab_size, updated_at
        FROM scope_stats WHERE scope_id IN ({ph})
        """,
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO scope_stats (scope_id, n_chunks, n_docs, vocab_size, "
        "updated_at) VALUES (?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    return len(rows)


def _copy_auto_ngram(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT scope_id, phrase, n, count, npmi, branch_ent, updated_at
        FROM auto_ngram_dict WHERE scope_id IN ({ph})
        """,
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO auto_ngram_dict (scope_id, phrase, n, count, npmi, "
        "branch_ent, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    return len(rows)


def _copy_votes(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_ids: List[int],
) -> int:
    """Copy votes AND their ``promoted_at`` markers (decision D12).

    Votes are knowledge; ``promoted_at`` is the idempotency marker that
    keeps ``grga index promote`` from double-graduating a pair. Since
    the ``llm_promoted`` pair rows travel with the votes in the same
    pack, ``promoted_at`` stays semantically consistent on the target.
    """
    ph = ",".join("?" for _ in scope_ids)
    rows = src.execute(
        f"""
        SELECT scope_id, kw_a_id, kw_b_id, vote_count, last_voted, promoted_at
        FROM llm_extension_votes WHERE scope_id IN ({ph})
        """,
        scope_ids,
    ).fetchall()
    dst.executemany(
        "INSERT INTO llm_extension_votes (scope_id, kw_a_id, kw_b_id, "
        "vote_count, last_voted, promoted_at) VALUES (?, ?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    return len(rows)


def _write_pack_meta(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    scope_names: List[str],
) -> None:
    """Stamp the pack's ``meta`` namespace so importers can validate it."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pairs = [
        ("grga_pack.format", PACK_FORMAT),
        ("grga_pack.format_version", PACK_FORMAT_VERSION),
        ("grga_pack.schema_version", str(SCHEMA_VERSION)),
        ("grga_pack.exported_at", now),
        ("grga_pack.scopes", ",".join(scope_names)),
    ]
    # Carry through source's toolchain fingerprint if present — helps
    # downstream diagnose "why do my newly built pairs look different".
    row = src.execute(
        "SELECT value FROM meta WHERE key = 'toolchain_fingerprint'"
    ).fetchone()
    if row is not None:
        pairs.append(("grga_pack.source_toolchain_fingerprint", str(row[0])))
    dst.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        pairs,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_knowledge_pack(
    pack_path: Union[str, Path],
    *,
    as_scope: Optional[str] = None,
    merge_into: Optional[str] = None,
    db_path: Union[str, Path, None] = None,
) -> ImportStats:
    """Load a knowledge pack into a target DB.

    Args:
        pack_path: source ``.grgapkg`` file.
        as_scope: target scope name to create (**required**). The pack
            must contain exactly one scope; the caller re-labels it here
            to avoid collisions when moving between projects.
        merge_into: **not implemented this round** (design D9). Reserved
            for a future release once npmi-merge semantics are decided;
            passing it raises :class:`NotImplementedError` with a hint.
        db_path: destination SQLite path (defaults to shared deep-search DB).

    Returns:
        :class:`ImportStats` with per-table counts.

    Raises:
        FileNotFoundError: pack file missing.
        ValueError: pack format/version mismatch, pack has ≠ 1 scope, or
            ``as_scope`` collides with an existing scope in the target DB.
        NotImplementedError: ``merge_into`` was set.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    pack_resolved = Path(pack_path).expanduser().resolve()
    if not pack_resolved.exists():
        raise FileNotFoundError(f"knowledge pack not found: {pack_resolved}")

    if merge_into is not None:
        raise NotImplementedError(
            "`--merge-into` needs an npmi merge policy (MAX / weighted / "
            "overwrite) whose semantics are undecided. Use `--as <name>` "
            "to import as a fresh scope, then reason about merging "
            "externally. See docs/design/plan-2026-07-14-scope-directory"
            "-split.md §5.2 (design D9)."
        )
    resolved_as = resolve_scope_name(as_scope)
    if resolved_as is None:
        raise ValueError("`--as <name>` is required to import a knowledge pack")

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH

    pack = connect(pack_resolved)
    try:
        _validate_pack(pack)
        # Every pack contains exactly one scope by design (export writes
        # per-scope). Even the ``--all`` export path yields one pack per
        # scope in a future release; for now, we assert one scope so
        # import stays deterministic.
        scope_rows = pack.execute(
            "SELECT id, name, kind, created_at FROM scopes"
        ).fetchall()
        if len(scope_rows) != 1:
            raise ValueError(
                f"pack must contain exactly 1 scope for import --as, "
                f"got {len(scope_rows)}: "
                f"{[str(r['name']) for r in scope_rows]}"
            )
        pack_scope_id = int(scope_rows[0]["id"])
        pack_scope_meta = scope_rows[0]

        dst = connect(target_db)
        try:
            ensure_schema(dst)
            existing = dst.execute(
                "SELECT 1 FROM scopes WHERE name = ?", (resolved_as,)
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"target DB already has scope {resolved_as!r}. "
                    "Pick a different --as name, or run `grga scope rm "
                    f"{resolved_as} --knowledge --yes` first."
                )
            with dst:
                stats = ImportStats(
                    pack_path=str(pack_resolved),
                    into_scope=resolved_as,
                )
                # 1. Create the new scope row in the destination.
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                dst.execute(
                    "INSERT INTO scopes (name, kind, created_at) "
                    "VALUES (?, ?, ?)",
                    (resolved_as, pack_scope_meta["kind"], now),
                )
                new_scope_id = int(dst.execute(
                    "SELECT id FROM scopes WHERE name = ?", (resolved_as,)
                ).fetchone()[0])

                # 2. Remap keywords: pack.kw_id → dst.kw_id via
                #    get-or-insert on ``keywords.kw``.
                kw_remap, stats.n_keywords_new = _import_keywords(pack, dst)

                # 3. Copy each knowledge table, rewriting scope_id and
                #    kw_id references through the remap.
                stats.n_pairs = _import_pairs(
                    pack, dst, pack_scope_id, new_scope_id, kw_remap
                )
                stats.n_stats = _import_stats(
                    pack, dst, pack_scope_id, new_scope_id, kw_remap
                )
                stats.scope_stats_written = _import_scope_stats(
                    pack, dst, pack_scope_id, new_scope_id
                )
                stats.n_auto_ngram = _import_auto_ngram(
                    pack, dst, pack_scope_id, new_scope_id
                )
                stats.n_votes = _import_votes(
                    pack, dst, pack_scope_id, new_scope_id, kw_remap
                )
        finally:
            dst.close()
    finally:
        pack.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


def _validate_pack(pack: sqlite3.Connection) -> None:
    """Confirm the file is a v10 knowledge pack from this tool."""
    version = read_schema_version(pack)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"pack schema version {version} != current {SCHEMA_VERSION}. "
            "Re-export from a matching grga version."
        )
    rows = dict(pack.execute(
        "SELECT key, value FROM meta "
        "WHERE key IN ('grga_pack.format', 'grga_pack.format_version')"
    ).fetchall())
    if rows.get("grga_pack.format") != PACK_FORMAT:
        raise ValueError(
            "file is not a grga knowledge pack "
            f"(missing meta grga_pack.format = {PACK_FORMAT!r}). "
            "Re-run `grga index export` to produce a valid pack."
        )
    if rows.get("grga_pack.format_version") != PACK_FORMAT_VERSION:
        raise ValueError(
            f"pack format version {rows.get('grga_pack.format_version')!r} "
            f"!= supported {PACK_FORMAT_VERSION!r}."
        )


def _import_keywords(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> tuple[dict, int]:
    """Remap keywords from the pack's id-space to the destination's.

    Returns ``(pack_kw_id → dst_kw_id, n_newly_inserted)``. Never inserts
    duplicate ``kw`` strings — the destination's ``UNIQUE(kw)`` constraint
    keeps the vocab tidy; existing rows are reused. The insert count comes
    from ``sqlite3.Cursor.rowcount`` on the INSERT OR IGNORE, which
    reports rows *actually* inserted (not skipped by the OR IGNORE) — the
    right accounting for reporting new vs. shared vocabulary to the user.

    Uses one bulk-INSERT-OR-IGNORE + one bulk-SELECT for O(1) round-trips
    regardless of vocab size (chunked at 900 params to stay under
    SQLITE_MAX_VARIABLE_NUMBER's default).
    """
    pack_rows = pack.execute("SELECT id, kw FROM keywords").fetchall()
    if not pack_rows:
        return {}, 0
    kw_texts = [str(r["kw"]) for r in pack_rows]
    n_new = 0
    for text in kw_texts:
        cur = dst.execute(
            "INSERT OR IGNORE INTO keywords (kw) VALUES (?)", (text,)
        )
        n_new += int(cur.rowcount or 0)
    # Bulk lookup: destination ids in one shot per chunk.
    dst_ids: dict = {}
    chunk = 900
    for i in range(0, len(kw_texts), chunk):
        piece = kw_texts[i:i + chunk]
        ph = ",".join("?" for _ in piece)
        for r in dst.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({ph})", piece
        ):
            dst_ids[str(r["kw"])] = int(r["id"])
    remap = {
        int(r["id"]): dst_ids[str(r["kw"])]
        for r in pack_rows
    }
    return remap, n_new


def _remap_ids(
    rows: List[sqlite3.Row],
    kw_cols: Iterable[str],
    kw_remap: dict,
    new_scope_id: int,
    scope_col: str = "scope_id",
) -> List[tuple]:
    """Rewrite a batch of rows through ``kw_remap`` + ``new_scope_id``."""
    kw_cols = list(kw_cols)
    out: List[tuple] = []
    for row in rows:
        d = dict(row)
        d[scope_col] = new_scope_id
        for col in kw_cols:
            d[col] = kw_remap[int(d[col])]
        out.append(tuple(d.values()))
    return out


def _import_pairs(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
    kw_remap: dict,
) -> int:
    rows = pack.execute(
        "SELECT scope_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi, updated_at "
        "FROM keyword_pairs WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchall()
    remapped = _remap_ids(rows, ("kw_a_id", "kw_b_id"), kw_remap, new_scope_id)
    dst.executemany(
        "INSERT INTO keyword_pairs (scope_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        remapped,
    )
    return len(remapped)


def _import_stats(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
    kw_remap: dict,
) -> int:
    rows = pack.execute(
        "SELECT scope_id, kw_id, chunk_df, doc_df, total_occurrences, "
        "updated_at FROM keyword_stats WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchall()
    remapped = _remap_ids(rows, ("kw_id",), kw_remap, new_scope_id)
    dst.executemany(
        "INSERT INTO keyword_stats (scope_id, kw_id, chunk_df, doc_df, "
        "total_occurrences, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        remapped,
    )
    return len(remapped)


def _import_scope_stats(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
) -> bool:
    row = pack.execute(
        "SELECT n_chunks, n_docs, vocab_size, updated_at "
        "FROM scope_stats WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchone()
    if row is None:
        return False
    dst.execute(
        "INSERT INTO scope_stats (scope_id, n_chunks, n_docs, vocab_size, "
        "updated_at) VALUES (?, ?, ?, ?, ?)",
        (new_scope_id, int(row["n_chunks"]), int(row["n_docs"]),
         int(row["vocab_size"]), row["updated_at"]),
    )
    return True


def _import_auto_ngram(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
) -> int:
    rows = pack.execute(
        "SELECT phrase, n, count, npmi, branch_ent, updated_at "
        "FROM auto_ngram_dict WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchall()
    dst.executemany(
        "INSERT INTO auto_ngram_dict (scope_id, phrase, n, count, npmi, "
        "branch_ent, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (new_scope_id, str(r["phrase"]), int(r["n"]), int(r["count"]),
             float(r["npmi"]), float(r["branch_ent"]), r["updated_at"])
            for r in rows
        ],
    )
    return len(rows)


def _import_votes(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
    kw_remap: dict,
) -> int:
    rows = pack.execute(
        "SELECT scope_id, kw_a_id, kw_b_id, vote_count, last_voted, "
        "promoted_at FROM llm_extension_votes WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchall()
    remapped = _remap_ids(
        rows, ("kw_a_id", "kw_b_id"), kw_remap, new_scope_id
    )
    dst.executemany(
        "INSERT INTO llm_extension_votes (scope_id, kw_a_id, kw_b_id, "
        "vote_count, last_voted, promoted_at) VALUES (?, ?, ?, ?, ?, ?)",
        remapped,
    )
    return len(remapped)
