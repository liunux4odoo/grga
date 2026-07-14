"""Scope-management operations for the v10 graph.

Public API used by the ``grga scope`` CLI subcommands. All operations
are read-only or metadata-level unless the caller explicitly opts into
knowledge deletion (``rm --knowledge``).

Design: ``docs/design/plan-2026-07-14-scope-directory-split.md`` §5.3.

The knowledge / evidence boundary is enforced here — bind/unbind and
rename never touch the knowledge assets; only ``remove_scope`` with
``purge_knowledge=True`` deletes ``keyword_pairs`` / ``keyword_stats``
/ ``scope_stats`` / ``auto_ngram_dict`` / ``llm_extension_votes`` for
the target scope.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema, resolve_scope_name


@dataclass
class DirectoryBinding:
    """One local filesystem path bound to a scope (evidence layer)."""

    directory: str
    role: Optional[str]
    indexed_at: Optional[str]

    def to_dict(self) -> dict:
        return {
            "directory": self.directory,
            "role": self.role,
            "indexed_at": self.indexed_at,
        }


@dataclass
class ScopeInfo:
    """Full introspection of one logical scope.

    ``directories``: local path bindings (evidence — may be empty on a
    scope that was just ``grga index import``-ed and not yet re-bound).
    ``tags``: multi-value labels attached to this scope (schema v11 /
    plan-2026-07-14-scope-tags). Knowledge layer — travels with the
    scope in a knowledge pack.
    ``n_pairs`` / ``n_keywords`` / ``n_docs``: quick sizing signals.
    """

    name: str
    kind: Optional[str] = None
    created_at: Optional[str] = None
    directories: List[DirectoryBinding] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    n_pairs: int = 0
    n_keywords: int = 0
    n_docs: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "created_at": self.created_at,
            "directories": [d.to_dict() for d in self.directories],
            "tags": list(self.tags),
            "n_pairs": int(self.n_pairs),
            "n_keywords": int(self.n_keywords),
            "n_docs": int(self.n_docs),
        }


@dataclass
class RemoveStats:
    """Summary of a ``remove_scope`` call."""

    scope: str
    purged_knowledge: bool
    directory_bindings_removed: int = 0
    docs_removed: int = 0
    keyword_docs_removed: int = 0
    heading_docs_removed: int = 0
    pairs_removed: int = 0
    stats_rows_removed: int = 0
    votes_removed: int = 0
    ngram_rows_removed: int = 0
    tags_removed: int = 0
    scope_row_removed: bool = False

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "purged_knowledge": self.purged_knowledge,
            "directory_bindings_removed": self.directory_bindings_removed,
            "docs_removed": self.docs_removed,
            "keyword_docs_removed": self.keyword_docs_removed,
            "heading_docs_removed": self.heading_docs_removed,
            "pairs_removed": self.pairs_removed,
            "stats_rows_removed": self.stats_rows_removed,
            "votes_removed": self.votes_removed,
            "ngram_rows_removed": self.ngram_rows_removed,
            "tags_removed": self.tags_removed,
            "scope_row_removed": self.scope_row_removed,
        }


# ---------------------------------------------------------------------------
# Read: list / info
# ---------------------------------------------------------------------------


def list_scopes(
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[ScopeInfo]:
    """Return every scope in the DB with its bindings + sizing signals.

    Ordered by scope name ascending. Empty list on a fresh DB.
    """
    def _do(c: sqlite3.Connection) -> List[ScopeInfo]:
        ensure_schema(c)
        rows = c.execute(
            "SELECT id, name, kind, created_at "
            "FROM scopes ORDER BY name ASC"
        ).fetchall()
        out: List[ScopeInfo] = []
        for row in rows:
            scope_id = int(row["id"])
            info = ScopeInfo(
                name=str(row["name"]),
                kind=row["kind"],
                created_at=row["created_at"],
            )
            for b in c.execute(
                "SELECT directory, role, indexed_at "
                "FROM scope_directories WHERE scope_id = ? "
                "ORDER BY indexed_at DESC NULLS LAST, directory ASC",
                (scope_id,),
            ):
                info.directories.append(DirectoryBinding(
                    directory=str(b["directory"]),
                    role=b["role"],
                    indexed_at=b["indexed_at"],
                ))
            for t in c.execute(
                "SELECT tag FROM scope_tags WHERE scope_id = ? "
                "ORDER BY tag ASC",
                (scope_id,),
            ):
                info.tags.append(str(t["tag"]))
            info.n_pairs = int(c.execute(
                "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])
            info.n_keywords = int(c.execute(
                "SELECT COUNT(*) FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])
            info.n_docs = int(c.execute(
                "SELECT COUNT(*) FROM docs WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])
            out.append(info)
        return out

    if conn is not None:
        return _do(conn)
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        return _do(owned)
    finally:
        owned.close()


# ---------------------------------------------------------------------------
# Write: bind / unbind (evidence-layer metadata only)
# ---------------------------------------------------------------------------


def bind_directory(
    scope: str,
    directory: Union[str, Path],
    *,
    role: Optional[str] = None,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record (or refresh) a scope → local directory binding.

    Idempotent: repeat calls with the same ``(scope, directory)`` refresh
    ``indexed_at`` / ``role``. Creates the scope on demand if it does not
    exist yet — matching ``grga index build`` semantics.
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")
    directory_str = str(Path(directory).expanduser().resolve())

    def _do(c: sqlite3.Connection) -> None:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id(scope_name)
        interner.bind_directory(scope_id, directory_str, role=role)

    if conn is not None:
        _do(conn)
        conn.commit()
        return
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        _do(owned)
        owned.commit()
    finally:
        owned.close()


def unbind_directory(
    scope: str,
    directory: Optional[Union[str, Path]] = None,
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Remove one (or every) directory binding for a scope.

    Returns the number of rows deleted. Does not touch knowledge assets
    or evidence (``docs`` / ``keyword_docs`` stay put — use
    :func:`remove_scope` for those). Missing scope is a no-op (returns 0).
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")
    directory_str = (
        str(Path(directory).expanduser().resolve()) if directory else None
    )

    def _do(c: sqlite3.Connection) -> int:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id_ro(scope_name)
        if scope_id is None:
            return 0
        return interner.unbind_directory(scope_id, directory_str)

    if conn is not None:
        n = _do(conn)
        conn.commit()
        return n
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        n = _do(owned)
        owned.commit()
        return n
    finally:
        owned.close()


# ---------------------------------------------------------------------------
# Tags: multi-value scope labels (schema v11 — knowledge layer)
# ---------------------------------------------------------------------------


def add_tags(
    scope: str,
    tags: Sequence[str],
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Attach one or more tag labels to a scope.

    Idempotent accumulate-only write (``INSERT OR IGNORE``). Empty /
    whitespace tags are skipped. Returns the number of tag rows actually
    inserted (existing labels count as 0). Creates the scope on demand
    if it does not exist yet — matches ``grga index build`` /
    ``grga scope bind`` semantics.
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")

    def _do(c: sqlite3.Connection) -> int:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id(scope_name)
        return interner.bind_tags(scope_id, tags)

    if conn is not None:
        n = _do(conn)
        conn.commit()
        return n
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        n = _do(owned)
        owned.commit()
        return n
    finally:
        owned.close()


def remove_tags(
    scope: str,
    tags: Optional[Sequence[str]] = None,
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Remove tag labels from a scope.

    ``tags=None`` (or empty) removes **every** tag for the scope. Returns
    the number of rows deleted. Does not touch knowledge assets. Missing
    scope is a no-op (returns 0).
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")

    def _do(c: sqlite3.Connection) -> int:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id_ro(scope_name)
        if scope_id is None:
            return 0
        return interner.unbind_tags(scope_id, tags)

    if conn is not None:
        n = _do(conn)
        conn.commit()
        return n
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        n = _do(owned)
        owned.commit()
        return n
    finally:
        owned.close()


def list_tags(
    scope: str,
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[str]:
    """Return every tag attached to a scope (ascending). Empty if missing."""
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")

    def _do(c: sqlite3.Connection) -> List[str]:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id_ro(scope_name)
        if scope_id is None:
            return []
        return interner.list_tags(scope_id)

    if conn is not None:
        return _do(conn)
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        return _do(owned)
    finally:
        owned.close()


def rename_tag(
    scope: str,
    old_tag: str,
    new_tag: str,
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Rename a single tag on a scope (``old_tag`` -> ``new_tag``).

    Strict semantics matching :func:`rename_scope`:

    * :class:`ValueError` — any of ``scope`` / ``old_tag`` / ``new_tag``
      is empty / whitespace, or old and new tags are identical.
    * :class:`LookupError` — the scope does not exist, or the scope has
      no tag matching ``old_tag``.
    * :class:`FileExistsError` — ``new_tag`` is already attached to this
      scope (would be a duplicate row). The message points at
      ``grga scope tag rm`` for the merge-into-existing case.

    Does NOT touch knowledge assets — it is a single-row UPDATE on
    ``scope_tags``. Tag semantics stay per-scope: renaming ``科幻`` to
    ``sci-fi`` on scope ``three-body`` does not touch the same tag on
    other scopes.
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")
    old_clean = str(old_tag).strip() if old_tag is not None else ""
    new_clean = str(new_tag).strip() if new_tag is not None else ""
    if not old_clean or not new_clean:
        raise ValueError("tag names must be non-empty")
    if old_clean == new_clean:
        raise ValueError(
            f"old and new tag names are identical: {old_clean!r}"
        )

    def _do(c: sqlite3.Connection) -> None:
        ensure_schema(c)
        interner = IdInterner(c)
        scope_id = interner.scope_id_ro(scope_name)
        if scope_id is None:
            raise LookupError(f"scope not found: {scope_name!r}")
        interner.rename_tag(scope_id, old_clean, new_clean)

    if conn is not None:
        _do(conn)
        conn.commit()
        return
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        _do(owned)
        owned.commit()
    finally:
        owned.close()


def resolve_scope_ids_by_tags(
    conn: sqlite3.Connection,
    tags: Sequence[str],
    *,
    logic: str = "or",
) -> List[int]:
    """Resolve a tag filter to the matching ``scope_id`` set.

    This is the query-side counterpart of :func:`add_tags`. It never
    touches the knowledge tables — it only maps ``--tag`` labels to the
    scope ids that carry them, so the caller can feed the result into the
    existing ``keyword_pairs.scope_id IN (...)`` filter.

    Args:
        conn: an open connection (caller owns it; schema assumed ensured).
        tags: tag labels to match. Empty / whitespace items are dropped.
            An empty (or all-blank) input is a documented no-op — the
            function returns ``[]`` rather than raising, so any caller
            can pass forward-normalised tag lists without a pre-check.
        logic: ``"or"`` (default) → scopes carrying **any** listed tag;
            ``"and"`` → scopes carrying **every** listed tag. Duplicate
            tags in the input are de-duped so ``and`` counts distinct tags.

    Returns:
        Sorted list of matching scope ids. Empty list when no scope
        matches OR when ``tags`` resolves to zero non-empty labels.

    Raises:
        ValueError: ``logic`` is neither ``"and"`` nor ``"or"``.
    """
    if logic not in ("and", "or"):
        raise ValueError(f"tag logic must be 'and' or 'or', got {logic!r}")
    cleaned = list(dict.fromkeys(
        t.strip() for t in tags if t and str(t).strip()
    ))
    if not cleaned:
        return []
    placeholders = ",".join("?" for _ in cleaned)
    if logic == "and":
        rows = conn.execute(
            f"SELECT scope_id FROM scope_tags WHERE tag IN ({placeholders}) "
            f"GROUP BY scope_id HAVING COUNT(*) = ?",
            (*cleaned, len(cleaned)),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT DISTINCT scope_id FROM scope_tags "
            f"WHERE tag IN ({placeholders})",
            cleaned,
        ).fetchall()
    return sorted(int(r[0]) for r in rows)


# ---------------------------------------------------------------------------
# Rename: scope-level relabel
# ---------------------------------------------------------------------------


def rename_scope(
    old_name: str,
    new_name: str,
    *,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Rename a scope from ``old_name`` to ``new_name``.

    Because ``scopes.name`` is UNIQUE and all business tables reference
    ``scope_id`` (not the name), this is a single-row UPDATE — knowledge
    survives intact. Raises:

    * :class:`ValueError` if either name is empty or they are equal.
    * :class:`LookupError` if ``old_name`` does not exist.
    * :class:`FileExistsError` if ``new_name`` is already taken.
    """
    old_resolved = resolve_scope_name(old_name)
    new_resolved = resolve_scope_name(new_name)
    if old_resolved is None or new_resolved is None:
        raise ValueError("scope names must be non-empty")
    if old_resolved == new_resolved:
        raise ValueError(
            f"old and new scope names are identical: {old_resolved!r}"
        )

    def _do(c: sqlite3.Connection) -> None:
        ensure_schema(c)
        row = c.execute(
            "SELECT id FROM scopes WHERE name = ?", (old_resolved,)
        ).fetchone()
        if row is None:
            raise LookupError(f"scope not found: {old_resolved!r}")
        clash = c.execute(
            "SELECT 1 FROM scopes WHERE name = ?", (new_resolved,)
        ).fetchone()
        if clash is not None:
            raise FileExistsError(
                f"target scope name already exists: {new_resolved!r}. "
                "Use `grga scope rm` first, or pick another name."
            )
        c.execute(
            "UPDATE scopes SET name = ? WHERE id = ?",
            (new_resolved, int(row[0])),
        )

    if conn is not None:
        _do(conn)
        conn.commit()
        return
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        _do(owned)
        owned.commit()
    finally:
        owned.close()


# ---------------------------------------------------------------------------
# Remove: scope-level delete
# ---------------------------------------------------------------------------


def remove_scope(
    scope: str,
    *,
    purge_knowledge: bool = False,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> RemoveStats:
    """Delete a scope's data.

    By default deletes only the **evidence** layer (``scope_directories`` /
    ``docs`` / ``keyword_docs`` / ``heading_docs``) — knowledge assets
    stay memorised (design D6 / plan-2026-07-14 §1.1). Pass
    ``purge_knowledge=True`` to additionally wipe ``keyword_pairs`` /
    ``keyword_stats`` / ``scope_stats`` / ``auto_ngram_dict`` /
    ``llm_extension_votes`` **and** the ``scopes`` row itself.

    Missing scope is a no-op that returns an all-zero :class:`RemoveStats`.
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError("scope must be a non-empty name")

    def _do(c: sqlite3.Connection) -> RemoveStats:
        ensure_schema(c)
        stats = RemoveStats(scope=scope_name, purged_knowledge=purge_knowledge)
        row = c.execute(
            "SELECT id FROM scopes WHERE name = ?", (scope_name,)
        ).fetchone()
        if row is None:
            return stats
        scope_id = int(row[0])
        stats.scope_found = True

        # Evidence: doc-keyed tables (heading_docs, keyword_docs)
        # then docs, then scope_directories.
        cur = c.execute(
            "DELETE FROM heading_docs WHERE doc_id IN "
            "(SELECT id FROM docs WHERE scope_id = ?)",
            (scope_id,),
        )
        stats.heading_docs_removed = int(cur.rowcount or 0)
        cur = c.execute(
            "DELETE FROM keyword_docs WHERE scope_id = ?", (scope_id,)
        )
        stats.keyword_docs_removed = int(cur.rowcount or 0)
        cur = c.execute(
            "DELETE FROM docs WHERE scope_id = ?", (scope_id,)
        )
        stats.docs_removed = int(cur.rowcount or 0)
        cur = c.execute(
            "DELETE FROM scope_directories WHERE scope_id = ?", (scope_id,)
        )
        stats.directory_bindings_removed = int(cur.rowcount or 0)
        # v1 deep-search side tables are evidence too.
        c.execute(
            "DELETE FROM keyword_sources WHERE scope_id = ?", (scope_id,)
        )
        c.execute(
            "DELETE FROM keyword_counts WHERE scope_id = ?", (scope_id,)
        )

        if purge_knowledge:
            cur = c.execute(
                "DELETE FROM keyword_pairs WHERE scope_id = ?", (scope_id,)
            )
            stats.pairs_removed = int(cur.rowcount or 0)
            cur = c.execute(
                "DELETE FROM keyword_stats WHERE scope_id = ?", (scope_id,)
            )
            stats.stats_rows_removed = int(cur.rowcount or 0)
            c.execute(
                "DELETE FROM scope_stats WHERE scope_id = ?", (scope_id,)
            )
            cur = c.execute(
                "DELETE FROM llm_extension_votes WHERE scope_id = ?",
                (scope_id,),
            )
            stats.votes_removed = int(cur.rowcount or 0)
            cur = c.execute(
                "DELETE FROM auto_ngram_dict WHERE scope_id = ?", (scope_id,)
            )
            stats.ngram_rows_removed = int(cur.rowcount or 0)
            # scope_tags is knowledge-layer (travels in a knowledge pack)
            # — wipe alongside the other knowledge assets when the scope
            # row itself is going away.
            cur = c.execute(
                "DELETE FROM scope_tags WHERE scope_id = ?", (scope_id,)
            )
            stats.tags_removed = int(cur.rowcount or 0)
            c.execute("DELETE FROM scopes WHERE id = ?", (scope_id,))
            stats.scope_row_removed = True
        return stats

    if conn is not None:
        stats = _do(conn)
        conn.commit()
        return stats
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        stats = _do(owned)
        owned.commit()
        return stats
    finally:
        owned.close()
