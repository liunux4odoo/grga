"""ID intern helper for the normalized (v5) graph schema.

The v5 layout replaces every occurrence of the ``directory`` / ``keyword`` /
``doc_path`` TEXT columns in the business tables with integer foreign keys
into three intern tables (``directories`` / ``keywords`` / ``docs``). All
consumers must therefore translate:

* on the **way in**  — user-supplied strings (a search seed, a scope
  filter, a doc path) into ids before hitting the business tables;
* on the **way out** — ids in a result row back into strings before an
  external contract (``--json`` output, trace-signals, GUI display).

:class:`IdInterner` batches both directions and caches lookups for the
lifetime of one call. It never opens its own transaction; the caller owns
the connection.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class IdInterner:
    """Get-or-insert + reverse-lookup helper backed by the v5 intern tables.

    Instances are cheap: they hold three ``dict`` caches keyed on the
    intern-table string (``directories.path`` / ``keywords.kw`` /
    ``(dir_id, docs.doc_path)``). Lookups miss to a single-row SELECT
    (or ``INSERT OR IGNORE`` + SELECT for get-or-insert variants).

    All methods are safe to call inside an existing caller transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._dir_id: Dict[str, int] = {}
        self._dir_text: Dict[int, str] = {}
        self._kw_id: Dict[str, int] = {}
        self._kw_text: Dict[int, str] = {}
        self._doc_id: Dict[Tuple[int, str], int] = {}
        self._doc_text: Dict[int, Tuple[int, str]] = {}

    # -- directories ----------------------------------------------------

    def dir_id(self, path: str) -> int:
        """Get-or-insert a directory row, return its id.

        ``path`` is stored verbatim; callers are expected to normalise via
        :func:`grga.graph.schema.scope_write_key` (which produces ``''``
        for "unscoped") before calling in.
        """
        hit = self._dir_id.get(path)
        if hit is not None:
            return hit
        self._conn.execute(
            "INSERT OR IGNORE INTO directories (path) VALUES (?)", (path,)
        )
        row = self._conn.execute(
            "SELECT id FROM directories WHERE path = ?", (path,)
        ).fetchone()
        did = int(row[0])
        self._dir_id[path] = did
        self._dir_text[did] = path
        return did

    def dir_id_ro(self, path: Optional[str]) -> Optional[int]:
        """Look up a directory id without inserting; return ``None`` if missing.

        ``path=None`` means "no scope filter" and returns ``None``.
        """
        if path is None:
            return None
        hit = self._dir_id.get(path)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT id FROM directories WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        did = int(row[0])
        self._dir_id[path] = did
        self._dir_text[did] = path
        return did

    def dir_text(self, dir_id: int) -> str:
        """Reverse-lookup: return the ``path`` for a directory id.

        Raises :class:`KeyError` if the id is not in the DB (this indicates
        a bug — every id in a query result should be resolvable).
        """
        hit = self._dir_text.get(dir_id)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT path FROM directories WHERE id = ?", (dir_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown dir_id: {dir_id}")
        path = str(row[0])
        self._dir_text[dir_id] = path
        self._dir_id[path] = dir_id
        return path

    def dir_texts(self, dir_ids: Iterable[int]) -> Dict[int, str]:
        """Batched :meth:`dir_text`. One SELECT resolves every missing id."""
        need: List[int] = []
        out: Dict[int, str] = {}
        for did in dir_ids:
            hit = self._dir_text.get(did)
            if hit is not None:
                out[did] = hit
            else:
                need.append(did)
        if need:
            placeholders = ",".join("?" for _ in need)
            rows = self._conn.execute(
                f"SELECT id, path FROM directories WHERE id IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                did = int(row[0])
                path = str(row[1])
                self._dir_text[did] = path
                self._dir_id[path] = did
                out[did] = path
        return out

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

        Hydrates the cache with one ``SELECT ... IN`` for the hits, then does
        a single ``INSERT OR IGNORE ... VALUES`` for the misses and one more
        ``SELECT ... IN`` to read their ids back. This replaces N per-keyword
        ``INSERT + SELECT`` round-trips with 3 statements total, which matters
        on the write hot path where a corpus can have hundreds of thousands
        of distinct keywords (``kw`` is already casefolded by the caller).
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

    def doc_id(self, dir_id: int, doc_path: str) -> int:
        """Get-or-insert a doc row, return its id."""
        key = (dir_id, doc_path)
        hit = self._doc_id.get(key)
        if hit is not None:
            return hit
        self._conn.execute(
            "INSERT OR IGNORE INTO docs (dir_id, doc_path) VALUES (?, ?)",
            (dir_id, doc_path),
        )
        row = self._conn.execute(
            "SELECT id FROM docs WHERE dir_id = ? AND doc_path = ?",
            (dir_id, doc_path),
        ).fetchone()
        did = int(row[0])
        self._doc_id[key] = did
        self._doc_text[did] = key
        return did

    def doc_row(self, doc_id: int) -> Tuple[int, str]:
        """Reverse-lookup doc: return ``(dir_id, doc_path)``."""
        hit = self._doc_text.get(doc_id)
        if hit is not None:
            return hit
        row = self._conn.execute(
            "SELECT dir_id, doc_path FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown doc_id: {doc_id}")
        key = (int(row[0]), str(row[1]))
        self._doc_text[doc_id] = key
        self._doc_id[key] = doc_id
        return key

    def doc_rows(self, doc_ids: Iterable[int]) -> Dict[int, Tuple[int, str]]:
        """Batched doc reverse-lookup: ``{doc_id: (dir_id, doc_path)}``."""
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
                f"SELECT id, dir_id, doc_path FROM docs WHERE id IN ({placeholders})",
                need,
            ).fetchall()
            for row in rows:
                did = int(row[0])
                key = (int(row[1]), str(row[2]))
                self._doc_text[did] = key
                self._doc_id[key] = did
                out[did] = key
        return out
