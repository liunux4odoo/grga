"""``grga index export-candidates`` — dream raw-material export (D5).

Design: ``docs/design/plan-2026-10-14-dream-ontology.md``.

grga's only job here is to produce a JSONL "candidate package" from the L2
co-occurrence graph plus L1 context snippets. It never runs an LLM, never
writes to the DB, and never touches the read-side verbs (``search`` /
``probe`` / ``expand`` / ``related`` behaviour is byte-identical whether or
not this command has ever run). The organising — entity merging, relation
predicates, confidence — is the caller's LLM's job, per D4.

JSONL schema (one candidate per line):

.. code-block:: json

    {"seed": "韩立", "candidate": "李化元", "npmi": 0.42,
     "cooccur_count": 87, "source_kws": ["韩立", "李化元"],
     "scope": "work", "context": "韩立拜入李化元门下，习得青元剑诀……"}

Guarantees (hard constraints §):
- zero LLM calls (never reads ``GRGA_OPENAI_API_KEY``);
- zero DB writes (pure SELECTs; ``ensure_schema`` is a no-op on a built DB);
- atomic output: the JSONL is accumulated in memory, then written to a
  same-directory temp file and ``os.replace``d — Ctrl-C before the rename
  leaves ``--out`` untouched (absent or with its previous content);
- idempotent: same parameters → byte-identical output, safely replayable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from grga.file_read import read_file_lines
from grga.graph.build import _COMPLEMENTARY_STOPWORDS, _INDEX_STOPWORDS
from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema
from grga.related import (
    _default_min_npmi,
    _normalise_scopes,
    _related_keywords_impl,
    _resolve_seed_args,
)

DEFAULT_TOP = 20
DEFAULT_MAX_CONTEXT_CHARS = 2000
DEFAULT_CONTEXT_LINES = 3


@dataclass
class CandidateExport:
    """Result of :func:`export_candidates`.

    ``rows`` is the JSONL payload (one dict per candidate). ``seeds`` is
    the folded, de-duplicated seed set actually queried; ``scopes`` is the
    resolved scope-name filter (``None`` → cross-scope union). The
    ``unknown`` / ``isolated`` probes come straight from the related
    neighbourhood query so callers can tell a misspelt seed from an
    isolated one (same disambiguation as ``related --mode graph``).
    """

    rows: List[dict] = field(default_factory=list)
    seeds: List[str] = field(default_factory=list)
    scopes: Optional[List[str]] = None
    unknown_seeds: List[str] = field(default_factory=list)
    isolated_seeds: List[str] = field(default_factory=list)


def export_candidates(
    seeds: Sequence[str],
    *,
    top: int = DEFAULT_TOP,
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    db_path: Union[str, Path, None] = None,
    scopes: Optional[Sequence[str]] = None,
    cross_scope: bool = False,
    auto_seed: Optional[int] = None,
) -> CandidateExport:
    """Build a dream candidate package.

    Reuses the ``related`` L2 neighbourhood query (same per-seed,
    no-fusion semantics, same ``--scope`` filter) and attaches an L1
    context snippet for each candidate by reading the seed's best evidence
    document and slicing a line window around the first hit line.

    Args:
        seeds:          seed keywords (folded internally). Mutually
                        exclusive with ``auto_seed``.
        top:            max candidates per seed (npmi-ordered).
        max_context_chars: per-candidate context char cap.
        db_path:        SQLite graph library path (defaults to the shared
                        deep-search DB).
        scopes:         optional scope-name filter. With ``cross_scope``
                        this is ignored (union). At least one scope is
                        required unless ``cross_scope`` is set.
        cross_scope:    union across every indexed scope.
        auto_seed:      instead of ``seeds``, cold-start-select this many
                        seeds from the scopes' high-chunk-df vocabulary.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_names = None if cross_scope else _normalise_scopes(scopes)

    conn = connect(target_db)
    try:
        ensure_schema(conn)

        if auto_seed is not None:
            scope_ids = _resolve_scope_ids(conn, scope_names)
            effective_seeds = _auto_select_seeds(conn, scope_ids, int(auto_seed))
        else:
            effective_seeds = _resolve_seed_args(conn, seeds)

        if not effective_seeds:
            return CandidateExport(
                seeds=[], scopes=scope_names, rows=[], unknown_seeds=[],
                isolated_seeds=[],
            )

        result = _related_keywords_impl(
            conn,
            effective_seeds,
            limit_per_seed=int(top),
            min_npmi=_default_min_npmi(),
            scope_names=scope_names,
            tag_names=None,
        )

        interner = IdInterner(conn)
        scope_id_by_name = {
            str(r[0]): int(r[1]) for r in conn.execute(
                "SELECT name, id FROM scopes"
            )
        }
        context_cache: Dict[Tuple[int, int], Tuple[Path, List[str]]] = {}

        rows: List[dict] = []
        for r in result.results:
            if not r.scopes:
                context = ""
            else:
                first_scope_name = r.scopes[0]
                first_scope_id = scope_id_by_name.get(first_scope_name)
                context = _context_for_pair(
                    conn, interner, first_scope_id,
                    r.via_seed, r.kw, max_context_chars, context_cache,
                )
            rows.append({
                "seed": r.via_seed,
                "candidate": r.kw,
                "npmi": round(float(r.npmi), 6),
                "cooccur_count": float(r.cooccur_count),
                "source_kws": [r.via_seed, r.kw],
                "scope": ",".join(r.scopes),
                "context": context,
            })

        unknown = [s.seed for s in result.resolved_seeds if not s.in_index]
        produced = {r.via_seed for r in result.results}
        isolated = [
            s.seed for s in result.resolved_seeds
            if s.in_index and s.seed not in produced
        ]
        return CandidateExport(
            rows=rows,
            seeds=list(effective_seeds),
            scopes=scope_names,
            unknown_seeds=unknown,
            isolated_seeds=isolated,
        )
    finally:
        conn.close()


def _resolve_scope_ids(
    conn: sqlite3.Connection, scope_names: Optional[List[str]],
) -> List[int]:
    """Resolve a scope-name filter to scope ids (empty list → all scopes)."""
    if scope_names is None:
        return [int(r[0]) for r in conn.execute("SELECT id FROM scopes")]
    if not scope_names:
        return []
    placeholders = ",".join("?" for _ in scope_names)
    return [
        int(r[0]) for r in conn.execute(
            f"SELECT id FROM scopes WHERE name IN ({placeholders})",
            list(scope_names),
        )
    ]


def _auto_select_seeds(
    conn: sqlite3.Connection, scope_ids: List[int], n: int,
) -> List[str]:
    """Cold-start: pick the top-``n`` meaningful keywords in the scopes.

    Heuristic only (caller supplies ``--seeds`` for real control): order
    by ``chunk_df`` DESC while skipping single-char tokens and the global /
    complementary stopword tables — hub function words (``第``/``段``/``他``)
    make terrible seeds.
    """
    if not scope_ids:
        return []
    scope_ph = ",".join("?" for _ in scope_ids)
    stop = sorted(_INDEX_STOPWORDS | _COMPLEMENTARY_STOPWORDS)
    stop_ph = ",".join("?" for _ in stop)
    rows = conn.execute(
        f"""
        SELECT k.kw
        FROM keyword_stats ks
        JOIN keywords k ON k.id = ks.kw_id
        WHERE ks.scope_id IN ({scope_ph})
          AND ks.chunk_df > 0
          AND length(k.kw) >= 2
          AND k.kw NOT IN ({stop_ph})
        ORDER BY ks.chunk_df DESC
        LIMIT ?
        """,
        list(scope_ids) + stop + [int(n)],
    ).fetchall()
    return [str(r[0]) for r in rows]


def _context_for_pair(
    conn: sqlite3.Connection,
    interner: IdInterner,
    scope_id: Optional[int],
    seed_text: str,
    cand_text: str,
    max_chars: int,
    cache: Dict[Tuple[int, int], Tuple[Path, List[str]]],
) -> str:
    """L1 snippet for one candidate: the seed's best evidence doc, ±3 lines.

    Falls back to ``""`` on any failure (silent degradation — the package
    is a hint, not a guarantee, and callers must verify against the raw
    text anyway). File reads are cached per ``(scope_id, kw_id)`` because
    every candidate from the same seed shares the same anchor document.
    """
    if scope_id is None:
        return ""
    seed_id = interner.kw_id_ro(seed_text)
    if seed_id is None:
        return ""
    cache_key = (scope_id, seed_id)
    entry = cache.get(cache_key)
    if entry is None:
        row = conn.execute(
            """
            SELECT d.directory, d.doc_path
            FROM keyword_docs kd
            JOIN docs d ON d.id = kd.doc_id
            WHERE kd.scope_id = ? AND kd.kw_id = ?
            ORDER BY kd.occurrence_count DESC
            LIMIT 1
            """,
            (scope_id, seed_id),
        ).fetchone()
        if row is None:
            cache[cache_key] = (Path(""), [])
        else:
            root = Path(row["directory"]) if row["directory"] else Path("")
            path = root / str(row["doc_path"])
            cache[cache_key] = (path, read_file_lines(path))
        entry = cache[cache_key]

    path, lines = entry
    if not path or not lines:
        return ""
    index = _find_line(lines, seed_text, cand_text)
    if index is None:
        index = _find_line(lines, seed_text, None)
    if index is None:
        return ""
    start = max(0, index - DEFAULT_CONTEXT_LINES)
    end = min(len(lines), index + DEFAULT_CONTEXT_LINES + 1)
    snippet = "\n".join(lines[start:end]).strip()
    return snippet[:max_chars]


def _find_line(lines: Sequence[str], a: str, b: Optional[str]) -> Optional[int]:
    """First line index containing ``a`` (and ``b`` when given)."""
    for i, line in enumerate(lines):
        if a in line and (b is None or b in line):
            return i
    return None


def write_jsonl(rows: Sequence[dict], out: Optional[Union[str, Path]]) -> None:
    """Write ``rows`` as JSONL.

    ``out=None`` streams to stdout. Otherwise the file is written via a
    same-directory temp file + ``os.replace`` so a Ctrl-C mid-write can
    never leave a half-length JSONL at ``out``.
    """
    if out is None:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(out.parent), prefix=f".{out.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
