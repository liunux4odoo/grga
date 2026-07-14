"""Knowledge pack export / import (plan-2026-07-14 §5).

A *knowledge pack* is a self-contained SQLite file (``.grgapkg``) that
carries the **knowledge layer** for **exactly one scope** in the source
DB, stripped of every ``evidence`` row. It lets you learn a
co-occurrence dictionary on one machine and reuse it on another without
dragging along that machine's ``docs`` / ``keyword_docs`` /
``scope_directories`` metadata.

Multi-scope (``--all``) writes to a **directory** of per-scope packs
(one ``<scope>.grgapkg`` per scope) — see :func:`export_all_scopes`.
Before the D14 review the ``--all`` path silently emitted a single
multi-scope pack that its own import path then rejected; the ``1 pack =
1 scope`` invariant is now enforced end-to-end.

Layer boundary is enforced strictly (plan-2026-07-14 §1.1):

* **Knowledge (in the pack)**: ``scopes`` (selected) + referenced
  ``keywords`` + ``keyword_pairs`` + ``keyword_stats`` + ``scope_stats``
  + ``auto_ngram_dict`` + ``llm_extension_votes`` + ``scope_tags`` +
  ``meta``.
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
    n_tags: int = 0
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
            "n_tags": int(self.n_tags),
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
    n_tags: int = 0
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
            "n_tags": int(self.n_tags),
            "scope_stats_written": self.scope_stats_written,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_knowledge_pack(
    *,
    out_path: Union[str, Path],
    scope: str,
    db_path: Union[str, Path, None] = None,
) -> ExportStats:
    """Write a single-scope knowledge pack (``1 pack = 1 scope``).

    Args:
        out_path: destination ``.grgapkg`` file. Written atomically via
            a same-directory tempfile + ``os.replace``, so a crash mid-
            write leaves the previous good pack (if any) intact — never
            a partial file.
        scope: **required** logical scope name. Multi-scope export goes
            through :func:`export_all_scopes` (writes one pack per scope
            into a directory) — passing a comma-separated list here is
            NOT accepted (the ``1 pack = 1 scope`` invariant lets
            :func:`import_knowledge_pack` stay deterministic).
        db_path: source SQLite path (defaults to the shared deep-search DB).

    Returns:
        :class:`ExportStats` with per-table counts.

    Raises:
        LookupError: named scope does not exist.
        ValueError: ``scope`` is empty / whitespace, or source DB is empty.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    resolved_scope = resolve_scope_name(scope)
    if resolved_scope is None:
        raise ValueError(
            "export_knowledge_pack: scope must be a non-empty name; "
            "use export_all_scopes to write every scope in the DB"
        )

    started = time.perf_counter()
    out_resolved = Path(out_path).expanduser().resolve()
    out_resolved.parent.mkdir(parents=True, exist_ok=True)

    source_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    src = connect(source_db)
    try:
        ensure_schema(src)
        row = src.execute(
            "SELECT id FROM scopes WHERE name = ?", (resolved_scope,)
        ).fetchone()
        if row is None:
            raise LookupError(
                f"scope not found in source DB: {resolved_scope!r}"
            )
        scope_id = int(row[0])
        stats = ExportStats(
            out_path=str(out_resolved),
            scopes_included=[resolved_scope],
        )

        # Atomic write: build the pack in a same-directory tempfile, then
        # os.replace onto the final path. Guarantees the caller either
        # sees the previous good pack (if any) or the newly-written one —
        # never a partial file. Same-directory tempfile keeps the
        # replace on-device (POSIX rename is atomic within a filesystem;
        # ``os.replace`` is atomic on both POSIX and Windows).
        tmp_fd = None
        tmp_path = None
        try:
            import os
            import tempfile
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{out_resolved.name}.",
                suffix=".tmp",
                dir=str(out_resolved.parent),
            )
            os.close(tmp_fd)
            tmp_fd = None
            tmp_path = Path(tmp_name)

            dst = connect(tmp_path)
            try:
                ensure_schema(dst)
                _fill_pack_via_attach(
                    dst, source_db, [scope_id], [resolved_scope], src, stats,
                )
            finally:
                dst.close()

            os.replace(str(tmp_path), str(out_resolved))
            tmp_path = None  # replaced; no cleanup needed
        finally:
            # Best-effort cleanup on error.
            if tmp_fd is not None:
                try:
                    import os as _os
                    _os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    finally:
        src.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


def export_all_scopes(
    *,
    out_dir: Union[str, Path],
    db_path: Union[str, Path, None] = None,
) -> List[ExportStats]:
    """Write one ``.grgapkg`` per scope into ``out_dir``.

    Each output file is named ``<scope>.grgapkg`` (scope names are
    sanitised for cross-platform filesystem safety). This is the
    multi-scope entry point that replaces the pre-review ``scope=None``
    codepath: since :func:`import_knowledge_pack` requires exactly one
    scope per pack, ``--all`` must emit multiple files, not a single
    multi-scope blob.

    Returns:
        One :class:`ExportStats` per scope written, in export order
        (ascending scope name).

    Raises:
        ValueError: source DB has no scopes to export.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    out_root = Path(out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    source_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    src = connect(source_db)
    try:
        ensure_schema(src)
        rows = src.execute(
            "SELECT name FROM scopes ORDER BY name ASC"
        ).fetchall()
    finally:
        src.close()
    if not rows:
        raise ValueError(
            "source DB has no scopes to export — run "
            "`grga index build --scope <name> --directory <path>` first"
        )

    out: List[ExportStats] = []
    for row in rows:
        scope_name = str(row["name"])
        pack_path = out_root / f"{_safe_pack_filename(scope_name)}.grgapkg"
        out.append(export_knowledge_pack(
            out_path=pack_path,
            scope=scope_name,
            db_path=db_path,
        ))
    return out


def _safe_pack_filename(scope_name: str) -> str:
    """Sanitise a scope name for use as a filename (cross-platform).

    Replaces every character that would be problematic on any of Linux /
    macOS / Windows (``/`` ``\\`` ``:`` ``*`` ``?`` ``"`` ``<`` ``>``
    ``|`` NUL) with ``_``. Preserves ASCII letters / digits / dash /
    underscore / dot verbatim; CJK etc. characters are kept as-is because
    modern filesystems handle Unicode filenames fine.
    """
    bad = set('/\\:*?"<>|\x00')
    return "".join("_" if c in bad or ord(c) < 32 else c for c in scope_name) or "scope"


# ATTACH-based fast path: keep row copy in SQLite's C layer instead of
# round-tripping through Python. Export never remaps ids (scope_id and
# kw_id stay stable inside a pack), so a straight ``INSERT ... SELECT``
# with an ATTACHED source is safe. Only ``keywords`` needs a subquery
# to restrict to the referenced vocabulary; the other six tables use
# a direct scope_id filter.

_ATTACH_COPY_TABLES = {
    # table_name → column-list SQL (fetched from schema — kept explicit
    # so a schema change breaks tests loudly rather than silently
    # copying garbage columns).
    "keyword_pairs": "scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at",
    "keyword_stats": "scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at",
    "scope_stats":   "scope_id, n_chunks, n_docs, vocab_size, updated_at",
    "auto_ngram_dict": "scope_id, phrase, n, count, npmi, branch_ent, updated_at",
    "llm_extension_votes": "scope_id, kw_a_id, kw_b_id, vote_count, last_voted, promoted_at",
    # scope_tags is knowledge-layer (v11): a tag describes what the scope
    # IS, so it travels with the pack. Only ``scope_id`` + ``tag``; no
    # kw_id remap needed on either export or import.
    "scope_tags": "scope_id, tag",
}


def _fill_pack_via_attach(
    dst: sqlite3.Connection,
    source_db_path: Path,
    scope_ids: List[int],
    scope_names: List[str],
    src_conn: sqlite3.Connection,
    stats: "ExportStats",
) -> None:
    """Populate ``dst`` (fresh v10 pack) from ``source_db_path`` via ATTACH.

    Uses a single ATTACH + one ``INSERT ... SELECT`` per knowledge table
    so row copy happens entirely in the C layer. Falls back to no-op on
    tables that end up empty (still records 0 in stats).

    ``src_conn`` is passed only so :func:`_write_pack_meta` can inspect
    the source's ``meta`` (for the toolchain fingerprint carry-through).
    """
    dst.execute(
        "ATTACH DATABASE ? AS src",
        (str(source_db_path),),
    )
    try:
        with dst:  # atomic on the pack DB
            ph = ",".join("?" for _ in scope_ids)
            # 1) scopes — id/name/kind/created_at preserved verbatim.
            dst.execute(
                f"INSERT INTO scopes (id, name, kind, created_at) "
                f"SELECT id, name, kind, created_at FROM src.scopes "
                f"WHERE id IN ({ph})",
                scope_ids,
            )
            # 2) referenced keywords — union of every kw_id touched by
            #    the exported scopes' business tables.
            cur = dst.execute(
                f"""
                INSERT INTO keywords (id, kw)
                SELECT id, kw FROM src.keywords WHERE id IN (
                    SELECT kw_a_id FROM src.keyword_pairs WHERE scope_id IN ({ph})
                    UNION SELECT kw_b_id FROM src.keyword_pairs WHERE scope_id IN ({ph})
                    UNION SELECT kw_id   FROM src.keyword_stats  WHERE scope_id IN ({ph})
                    UNION SELECT kw_a_id FROM src.llm_extension_votes WHERE scope_id IN ({ph})
                    UNION SELECT kw_b_id FROM src.llm_extension_votes WHERE scope_id IN ({ph})
                )
                """,
                scope_ids * 5,
            )
            stats.n_keywords = int(cur.rowcount or 0)
            # 3-7) Copy each knowledge asset table via _ATTACH_COPY_TABLES.
            for table, cols in _ATTACH_COPY_TABLES.items():
                cur = dst.execute(
                    f"INSERT INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} "
                    f"WHERE scope_id IN ({ph})",
                    scope_ids,
                )
                n = int(cur.rowcount or 0)
                # Wire the count into the stats object under a
                # consistent naming scheme. Kept explicit so the JSON
                # contract stays stable.
                if table == "keyword_pairs":
                    stats.n_pairs = n
                elif table == "keyword_stats":
                    stats.n_stats = n
                elif table == "scope_stats":
                    stats.n_scope_stats = n
                elif table == "auto_ngram_dict":
                    stats.n_auto_ngram = n
                elif table == "llm_extension_votes":
                    stats.n_votes = n
                elif table == "scope_tags":
                    stats.n_tags = n
            _write_pack_meta(src_conn, dst, scope_names)
    finally:
        # DETACH is a no-op if the ATTACH failed; wrap defensively so a
        # partial ATTACH does not leak into subsequent calls.
        try:
            dst.execute("DETACH DATABASE src")
        except sqlite3.OperationalError:
            pass


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
            # Collision check + INSERT run inside a single write txn so
            # two concurrent imports can not both pass the check before
            # either INSERTs (the "SELECT then INSERT" TOCTOU window is
            # what BEGIN IMMEDIATE closes). We still translate any
            # IntegrityError from the UNIQUE(name) constraint into the
            # friendly ValueError as defense-in-depth: if a third writer
            # slips in between our BEGIN IMMEDIATE and INSERT (should be
            # impossible under SQLite's single-writer lock but easy to
            # verify), the caller-facing error stays actionable instead
            # of surfacing a raw sqlite3.IntegrityError.
            dst.execute("BEGIN IMMEDIATE")
            try:
                existing = dst.execute(
                    "SELECT 1 FROM scopes WHERE name = ?", (resolved_as,)
                ).fetchone()
                if existing is not None:
                    raise ValueError(
                        f"target DB already has scope {resolved_as!r}. "
                        "Pick a different --as name, or run `grga scope rm "
                        f"{resolved_as} --knowledge --yes` first."
                    )
                stats = ImportStats(
                    pack_path=str(pack_resolved),
                    into_scope=resolved_as,
                )
                # 1. Create the new scope row in the destination.
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    dst.execute(
                        "INSERT INTO scopes (name, kind, created_at) "
                        "VALUES (?, ?, ?)",
                        (resolved_as, pack_scope_meta["kind"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        f"target DB already has scope {resolved_as!r} "
                        "(concurrent import). Pick a different --as name."
                    ) from exc
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
                stats.n_tags = _import_tags(
                    pack, dst, pack_scope_id, new_scope_id
                )
                dst.execute("COMMIT")
            except Exception:
                # Any failure inside the txn rolls back everything —
                # including the scope INSERT so the target DB stays clean.
                try:
                    dst.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
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
    keeps the vocab tidy; existing rows are reused.

    Uses one bulk ``executemany`` INSERT OR IGNORE + one bulk SELECT
    chunked at 900 params (SQLITE_MAX_VARIABLE_NUMBER default). New-row
    count comes from ``dst.total_changes`` deltas around the executemany,
    which mirrors ``rowcount`` semantics on INSERT OR IGNORE (skipped
    rows are NOT counted) without paying the per-row execute cost that
    a naive rowcount loop would incur.
    """
    pack_rows = pack.execute("SELECT id, kw FROM keywords").fetchall()
    if not pack_rows:
        return {}, 0
    kw_texts = [str(r["kw"]) for r in pack_rows]
    before = dst.total_changes
    dst.executemany(
        "INSERT OR IGNORE INTO keywords (kw) VALUES (?)",
        [(t,) for t in kw_texts],
    )
    n_new = dst.total_changes - before
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


def _import_tags(
    pack: sqlite3.Connection,
    dst: sqlite3.Connection,
    pack_scope_id: int,
    new_scope_id: int,
) -> int:
    """Copy the scope's tag labels into the destination under the new id.

    Knowledge-layer (v11): only ``scope_id`` is rewritten — ``tag`` is a
    plain string with no id-space to remap. ``INSERT OR IGNORE`` guards
    against a rare tag collision if the destination scope already carried
    the label (fresh ``--as`` import never has, but stay defensive).
    """
    rows = pack.execute(
        "SELECT tag FROM scope_tags WHERE scope_id = ?",
        (pack_scope_id,),
    ).fetchall()
    if not rows:
        return 0
    dst.executemany(
        "INSERT OR IGNORE INTO scope_tags (scope_id, tag) VALUES (?, ?)",
        [(new_scope_id, str(r["tag"])) for r in rows],
    )
    return len(rows)
