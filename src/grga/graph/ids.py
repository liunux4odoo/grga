"""ID intern helper for the v10 graph schema (scope / directory split).

Every asset table in the v10 schema references three intern tables by
integer id:

* ``scopes.id``   — logical domain name (portable across machines)
* ``keywords.id`` — casefold vocabulary (portable across machines)
* ``docs.id``     — machine-local file identity (evidence layer)

All consumers translate:

* on the **way in**  — user-supplied strings (a search seed, a scope
  name, a doc path) into ids before hitting the business tables;
* on the **way out** — ids in a result row back into strings before an
  external contract (``--json`` output, GUI display).

:class:`IdInterner` batches both directions and caches lookups for the
lifetime of one call. It never opens its own transaction; the caller
owns the connection.

Design: ``docs/design/plan-2026-07-14-scope-directory-split.md`` §2, §3.
The pre-v10 ``dir_*`` method surface was renamed to ``scope_*`` because
the id now refers to a logical scope, not a filesystem directory. The
``scope_directories`` binding table has its own dedicated helpers below
(:meth:`bind_directory` / :meth:`list_directories`).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class IdInterner:
    """Get-or-insert + reverse-lookup helper backed by the v10 intern tables.

    Instances are cheap: they hold three ``dict`` caches keyed on the
    intern-table string (``scopes.name`` / ``keywords.kw`` /
    ``(scope_id, docs.doc_path)``). Cache misses do one SELECT (or
    ``INSERT OR IGNORE`` + SELECT for get-or-insert variants). All
    methods are safe to call inside an existing caller transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._scope_id: Dict[str, int] = {}
        self._scope_name: Dict[int, str] = {}
        self._kw_id: Dict[str, int] = {}
        self._kw_text: Dict[int, str] = {}
        self._doc_id: Dict[Tuple[int, str], int] = {}
        self._doc_text: Dict[int, Tuple[int, str]] = {}

    # -- scopes ---------------------------------------------------------

    def scope_id(self, name: str, *, kind: Optional[str] = None) -> int:
        """Get-or-insert a logical scope row, return its id.

        ``name`` is stored verbatim; callers should normalise via
        :func:`grga.graph.schema.resolve_scope_name` before calling in.
        ``kind`` is written only on first insert; existing rows keep
        their stored kind.
        """
        hit = self._scope_id.get(name)
        if hit is not None:
            return hit
        now = _utc_now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO scopes (name, kind, created_at) "
            "VALUES (?, ?, ?)",
            (name, kind, now),
        )
        row = self._conn.execute(
            "SELECT id FROM scopes WHERE name = ?", (name,)
        ).fetchone()
        sid = int(row[0])
        self._scope_id[name] = sid
        self._scope_name[sid] = name
        return sid

    def scope_id_ro(self, name: Optional[str]) -> Optional[int]:
        """Look up a scope id without inserting; ``None`` if missing.

        ``name=None`` means "no scope filter" and returns ``None``.
        """
        if name is None:
            return None
        hit = self._scope_id.get(name)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT id FROM scopes WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        sid = int(row[0])
        self._scope_id[name] = sid
        self._scope_name[sid] = name
        return sid

    def scope_ids_ro(self, names: Sequence[str]) -> Dict[str, int]:
        """Batched read-only scope lookup: ``{name: id}`` for known scopes.

        Missing scopes are simply absent from the result. Used by
        ``related --scope a --scope b`` to build a ``scope_id IN (...)``
        filter in one query.
        """
        uniq = [n for n in dict.fromkeys(names) if n]
        if not uniq:
            return {}
        out: Dict[str, int] = {}
        need: List[str] = []
        for name in uniq:
            hit = self._scope_id.get(name)
            if hit is not None:
                out[name] = hit
            else:
                need.append(name)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, name FROM scopes WHERE name IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                sid = int(row[0])
                name = str(row[1])
                self._scope_id[name] = sid
                self._scope_name[sid] = name
                out[name] = sid
        return out

    def scope_name(self, scope_id: int) -> str:
        """Reverse-lookup: return the ``name`` for a scope id.

        Raises :class:`KeyError` if the id is not in the DB (this
        indicates a bug — every id in a query result should resolve).
        """
        hit = self._scope_name.get(scope_id)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT name FROM scopes WHERE id = ?", (scope_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown scope_id: {scope_id}")
        name = str(row[0])
        self._scope_name[scope_id] = name
        self._scope_id[name] = scope_id
        return name

    def scope_names(self, scope_ids: Iterable[int]) -> Dict[int, str]:
        """Batched :meth:`scope_name`. One SELECT covers every missing id."""
        need: List[int] = []
        out: Dict[int, str] = {}
        for sid in scope_ids:
            hit = self._scope_name.get(sid)
            if hit is not None:
                out[sid] = hit
            else:
                need.append(sid)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, name FROM scopes WHERE id IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                sid = int(row[0])
                name = str(row[1])
                self._scope_name[sid] = name
                self._scope_id[name] = sid
                out[sid] = name
        return out

    # -- scope_directories (binding metadata, evidence layer) ----------
    #
    # These helpers manage the scope -> local-filesystem-path binding.
    # The table is machine-local: it is NOT included in a knowledge
    # pack, and the target machine rebuilds bindings via
    # ``grga scope bind`` after import. A single scope may bind
    # multiple directories on this machine (e.g. papers + notes both
    # under ``physics-cn``).

    def bind_directory(
        self,
        scope_id: int,
        directory: str,
        *,
        role: Optional[str] = None,
    ) -> None:
        """Record (or refresh ``indexed_at`` on) a scope -> directory binding.

        ``directory`` is stored verbatim; the caller is expected to
        resolve/normalise the path before calling in (typically at CLI
        parse time). Repeat calls with the same ``(scope_id, directory)``
        pair are UPSERTs — ``indexed_at`` and ``role`` refresh.
        """
        now = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO scope_directories (scope_id, directory, role, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope_id, directory) DO UPDATE SET
                role = COALESCE(excluded.role, role),
                indexed_at = excluded.indexed_at
            """,
            (scope_id, directory, role, now),
        )

    def list_directories(self, scope_id: int) -> List[Dict[str, object]]:
        """Return every directory row bound to ``scope_id``.

        Rows are ordered by ``indexed_at`` (most recent first) so
        callers who want "the primary directory" can just take
        ``[0]``. Empty list when the scope has no binding — that is a
        knowledge-only scope (typical after ``grga index import``).
        """
        rows = self._conn.execute(
            """
            SELECT directory, role, indexed_at
            FROM scope_directories
            WHERE scope_id = ?
            ORDER BY indexed_at DESC NULLS LAST, directory ASC
            """,
            (scope_id,),
        ).fetchall()
        return [
            {
                "directory": str(row["directory"]),
                "role": row["role"],
                "indexed_at": row["indexed_at"],
            }
            for row in rows
        ]

    def unbind_directory(
        self, scope_id: int, directory: Optional[str] = None
    ) -> int:
        """Remove a directory binding (or all bindings) for a scope.

        Returns the number of rows deleted. Does not touch knowledge
        (``keyword_pairs`` etc.). Evidence tables (``docs`` /
        ``keyword_docs`` / ``heading_docs``) are also left alone —
        unbind is metadata-only. Use ``grga index forget`` or
        ``grga scope rm`` for actual data deletion.
        """
        if directory is None:
            cur = self._conn.execute(
                "DELETE FROM scope_directories WHERE scope_id = ?",
                (scope_id,),
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM scope_directories "
                "WHERE scope_id = ? AND directory = ?",
                (scope_id, directory),
            )
        return int(cur.rowcount or 0)

    # -- scope_tags (multi-value scope labels, schema v11) -------------
    #
    # Tags describe *what a scope is* (e.g. ``科幻`` / ``小说``); they are
    # part of the knowledge layer (portable across machines, included in a
    # knowledge pack). The build write path uses :meth:`bind_tags`;
    # query-side ``--tag`` resolution goes through
    # ``grga.graph.scope.resolve_scope_ids_by_tags`` which JOINs the
    # ``scope_tags`` table directly.
    #
    # Tag strings are stored verbatim after a light strip; the caller is
    # responsible for consistent casing / normalisation across build and
    # query (same contract as ``scopes.name``).

    def bind_tags(self, scope_id: int, tags: Sequence[str]) -> int:
        """UPSERT one or more tag labels onto a scope.

        Idempotent: ``INSERT OR IGNORE`` means repeated calls with the
        same ``(scope_id, tag)`` are a no-op. Empty / whitespace tags
        are silently skipped (matches CLI cleanup) so callers can
        forward user input directly. Returns the number of rows actually
        inserted (existing labels count as 0). Duplicate entries within
        one call are de-duped before writing.
        """
        cleaned: List[str] = []
        seen: set = set()
        for raw in tags:
            if raw is None:
                continue
            text = str(raw).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            return 0
        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO scope_tags (scope_id, tag) VALUES (?, ?)",
            [(scope_id, tag) for tag in cleaned],
        )
        return int(self._conn.total_changes - before)

    def list_tags(self, scope_id: int) -> List[str]:
        """Return every tag attached to ``scope_id`` in ascending order."""
        rows = self._conn.execute(
            "SELECT tag FROM scope_tags WHERE scope_id = ? ORDER BY tag ASC",
            (scope_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def unbind_tags(
        self, scope_id: int, tags: Optional[Sequence[str]] = None
    ) -> int:
        """Remove tag labels from a scope.

        ``tags=None`` (or empty) removes **every** tag for the scope
        (mirrors :meth:`unbind_directory`). Returns the number of rows
        deleted. Does not touch knowledge assets.
        """
        if not tags:
            cur = self._conn.execute(
                "DELETE FROM scope_tags WHERE scope_id = ?",
                (scope_id,),
            )
            return int(cur.rowcount or 0)
        cleaned = [str(t).strip() for t in tags if t and str(t).strip()]
        if not cleaned:
            return 0
        placeholders = ",".join("?" for _ in cleaned)
        cur = self._conn.execute(
            f"DELETE FROM scope_tags WHERE scope_id = ? AND tag IN ({placeholders})",
            (scope_id, *cleaned),
        )
        return int(cur.rowcount or 0)

    def rename_tag(
        self, scope_id: int, old_tag: str, new_tag: str,
    ) -> None:
        """Rename a single ``(scope_id, old_tag)`` row to ``new_tag``.

        Strict semantics (mirrors :func:`grga.graph.scope.rename_scope`):

        * :class:`LookupError` if ``(scope_id, old_tag)`` does not exist.
        * :class:`FileExistsError` if ``(scope_id, new_tag)`` already exists.

        A single-row UPDATE on the ``scope_tags`` PK. Both strings are
        stored verbatim after :meth:`str.strip`; the caller is expected
        to have validated non-emptiness and that ``old != new``.
        """
        row = self._conn.execute(
            "SELECT 1 FROM scope_tags WHERE scope_id = ? AND tag = ?",
            (scope_id, old_tag),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"tag not found on scope: {old_tag!r}"
            )
        clash = self._conn.execute(
            "SELECT 1 FROM scope_tags WHERE scope_id = ? AND tag = ?",
            (scope_id, new_tag),
        ).fetchone()
        if clash is not None:
            raise FileExistsError(
                f"target tag already attached to this scope: {new_tag!r}. "
                "Use `grga scope tag rm <scope> <old>` if you want to merge."
            )
        self._conn.execute(
            "UPDATE scope_tags SET tag = ? WHERE scope_id = ? AND tag = ?",
            (new_tag, scope_id, old_tag),
        )

    # -- keywords -------------------------------------------------------

    def kw_id(self, kw: str) -> int:
        """Get-or-insert a keyword row; ``kw`` should already be casefolded."""
        hit = self._kw_id.get(kw)
        if hit is not None:
            return hit
        self._conn.execute(
            "INSERT OR IGNORE INTO keywords (kw) VALUES (?)", (kw,)
        )
        row = self._conn.execute(
            "SELECT id FROM keywords WHERE kw = ?", (kw,)
        ).fetchone()
        kid = int(row[0])
        self._kw_id[kw] = kid
        self._kw_text[kid] = kw
        return kid

    def kw_id_ro(self, kw: str) -> Optional[int]:
        """Read-only keyword lookup; ``None`` if not indexed."""
        hit = self._kw_id.get(kw)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT id FROM keywords WHERE kw = ?", (kw,)
        ).fetchone()
        if row is None:
            return None
        kid = int(row[0])
        self._kw_id[kw] = kid
        self._kw_text[kid] = kw
        return kid

    def kw_ids_ro(self, kws: Sequence[str]) -> Dict[str, int]:
        """Batched read-only lookup: return ``{kw: id}`` for known keywords."""
        need: List[str] = []
        out: Dict[str, int] = {}
        for kw in kws:
            hit = self._kw_id.get(kw)
            if hit is not None:
                out[kw] = hit
            else:
                need.append(kw)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, kw FROM keywords WHERE kw IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                kid = int(row[0])
                kw = str(row[1])
                self._kw_id[kw] = kid
                self._kw_text[kid] = kw
                out[kw] = kid
        return out

    def kw_ids(self, kws: Sequence[str]) -> Dict[str, int]:
        """Batched get-or-insert: return ``{kw: id}`` for every input keyword.

        Hydrates the cache with one ``SELECT ... IN`` for the hits, then
        does a single ``INSERT OR IGNORE ... VALUES`` for the misses and
        one more ``SELECT ... IN`` to read their ids back. This replaces
        N per-keyword ``INSERT + SELECT`` round-trips with 3 statements
        total — matters on the write hot path where a corpus produces
        hundreds of thousands of distinct keywords (``kw`` is already
        casefolded by the caller).
        """
        out = self.kw_ids_ro(kws)
        # De-dup misses while preserving determinism.
        missing = [kw for kw in dict.fromkeys(kws) if kw not in out]
        if missing:
            self._conn.executemany(
                "INSERT OR IGNORE INTO keywords (kw) VALUES (?)",
                [(kw,) for kw in missing],
            )
            placeholders = ",".join("?" for _ in missing)
            rows = self._conn.execute(
                f"SELECT id, kw FROM keywords WHERE kw IN ({placeholders})",
                missing,
            ).fetchall()
            for row in rows:
                kid = int(row[0])
                kw = str(row[1])
                self._kw_id[kw] = kid
                self._kw_text[kid] = kw
                out[kw] = kid
        return out

    def kw_text(self, kw_id: int) -> str:
        """Reverse-lookup keyword text; :class:`KeyError` if not found."""
        hit = self._kw_text.get(kw_id)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT kw FROM keywords WHERE id = ?", (kw_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown kw_id: {kw_id}")
        kw = str(row[0])
        self._kw_text[kw_id] = kw
        self._kw_id[kw] = kw_id
        return kw

    def kw_texts(self, kw_ids: Iterable[int]) -> Dict[int, str]:
        """Batched keyword reverse-lookup."""
        need: List[int] = []
        out: Dict[int, str] = {}
        for kid in kw_ids:
            hit = self._kw_text.get(kid)
            if hit is not None:
                out[kid] = hit
            else:
                need.append(kid)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, kw FROM keywords WHERE id IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                kid = int(row[0])
                kw = str(row[1])
                self._kw_text[kid] = kw
                self._kw_id[kw] = kid
                out[kid] = kw
        return out

    # -- docs -----------------------------------------------------------

    def doc_id(self, scope_id: int, doc_path: str) -> int:
        """Get-or-insert a doc row, return its id."""
        key = (scope_id, doc_path)
        hit = self._doc_id.get(key)
        if hit is not None:
            return hit
        self._conn.execute(
            "INSERT OR IGNORE INTO docs (scope_id, doc_path) VALUES (?, ?)",
            (scope_id, doc_path),
        )
        row = self._conn.execute(
            "SELECT id FROM docs WHERE scope_id = ? AND doc_path = ?",
            (scope_id, doc_path),
        ).fetchone()
        did = int(row[0])
        self._doc_id[key] = did
        self._doc_text[did] = key
        return did

    def doc_row(self, doc_id: int) -> Tuple[int, str]:
        """Reverse-lookup doc: return ``(scope_id, doc_path)``."""
        hit = self._doc_text.get(doc_id)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT scope_id, doc_path FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown doc_id: {doc_id}")
        key = (int(row[0]), str(row[1]))
        self._doc_text[doc_id] = key
        self._doc_id[key] = doc_id
        return key

    def doc_rows(self, doc_ids: Iterable[int]) -> Dict[int, Tuple[int, str]]:
        """Batched doc reverse-lookup: ``{doc_id: (scope_id, doc_path)}``."""
        need: List[int] = []
        out: Dict[int, Tuple[int, str]] = {}
        for did in doc_ids:
            hit = self._doc_text.get(did)
            if hit is not None:
                out[did] = hit
            else:
                need.append(did)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, scope_id, doc_path FROM docs WHERE id IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                did = int(row[0])
                key = (int(row[1]), str(row[2]))
                self._doc_text[did] = key
                self._doc_id[key] = did
                out[did] = key
        return out


def _utc_now_iso() -> str:
    """UTC timestamp in ``YYYY-MM-DDTHH:MM:SSZ`` form (matches v5+ convention)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
