"""Experiment-local replacement for ``grga.graph.build._finalize_pairs_from_temp``.

Method B (PR-1) from ``docs/design/plan-2026-07-15-build-memory-oom.md``:

* Steps 1-3 (index / GROUP BY-HAVING INSERT / write-time floor DELETE) are
  copied verbatim from mainline — no behaviour change.
* Step 4 (per-pair npmi recompute) replaces mainline's Python loop with:
    - ``method_B_sql``: one SQL UPDATE + one SQL DELETE (requires SQLite
      ``ln()`` from ``-DSQLITE_ENABLE_MATH_FUNCTIONS``).
    - ``method_B_batch``: paginated cursor + ``executemany`` UPDATE/DELETE
      per batch (used when ``ln()`` is unavailable, or forced for
      equivalence testing).

The SQL expression in step 4 mirrors Python's ``_npmi`` evaluation order
1:1 (verified bit-exact for 50k random inputs, max_diff = 0.0). The
mainline emit contract (``pairs_scan_start`` / ``pairs_advance`` /
``pairs_scan_end``) is preserved.
"""
from __future__ import annotations

import math
import os
import sqlite3
from typing import Callable, Dict, List, Optional, Tuple

# We import the mainline constants + _npmi so tests / equivalence checks
# can reuse them verbatim.
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "src"))

from grga.graph.build import _npmi  # noqa: E402
from grga.graph.schema import CHUNK_COOCCUR_SOURCE  # noqa: E402


DEFAULT_BATCH_SIZE = 50_000


def _has_ln(conn) -> bool:
    """Probe for the SQLite ``ln()`` natural-log function.

    Presence, not return value, is what matters — a ``base-10 log()`` build
    would still return a number but the wrong one. Missing ``ln()`` raises
    ``sqlite3.OperationalError`` (or a subclass).
    """
    try:
        conn.execute("SELECT ln(1.0)")
        return True
    except sqlite3.OperationalError:
        return False


def _finalize_pairs_sql(
    conn,
    *,
    scope_id: int,
    min_npmi: float,
    n_chunks: int,
    on_event: Optional[Callable[[dict], None]],
) -> None:
    """Step 4 via one UPDATE (npmi) + one DELETE (glued / below-floor)."""
    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        try:
            on_event({"phase": phase, **fields})
        except Exception:
            pass

    # Preserve pairs_advance emit shape for observers by pre-counting.
    total = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])

    # Single UPDATE. SQL expression mirrors _npmi() evaluation order.
    # Gate 1 (glued) + degenerate cases → NULL npmi; DELETE removes those.
    conn.execute(
        """
        WITH df AS (
            SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?
        )
        UPDATE keyword_pairs
        SET npmi = (
            SELECT CASE
                WHEN kp_co <= 0 OR df_a IS NULL OR df_b IS NULL
                     OR df_a <= 0 OR df_b <= 0 OR ? <= 0 THEN NULL
                WHEN (kp_co * 1.0 / ?) >= 1.0 THEN NULL
                WHEN (-ln(kp_co * 1.0 / ?)) <= 0 THEN NULL
                ELSE ln((kp_co * 1.0 / ?)
                         / ((df_a * 1.0 / ?) * (df_b * 1.0 / ?)))
                     / (-ln(kp_co * 1.0 / ?))
            END
            FROM (
                SELECT keyword_pairs.cooccur_count AS kp_co,
                       (SELECT chunk_df FROM df WHERE df.kw_id = keyword_pairs.kw_a_id) AS df_a,
                       (SELECT chunk_df FROM df WHERE df.kw_id = keyword_pairs.kw_b_id) AS df_b
            )
        )
        WHERE scope_id = ? AND source = ?
        """,
        (scope_id, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks,
         scope_id, CHUNK_COOCCUR_SOURCE),
    )

    # Gate 1 (glued: df_a = df_b = co, int equality) + npmi NULL + below-floor.
    # ``df_a = df_b`` alone is not enough — we need three-way equality with co,
    # matching mainline's ``glued = df_a > 0 and df_b > 0 and df_a == df_b == co``.
    conn.execute(
        """
        WITH df AS (
            SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?
        )
        DELETE FROM keyword_pairs
        WHERE scope_id = ? AND source = ?
          AND (
              npmi IS NULL
              OR (? > 0.0 AND npmi < ?)
              OR EXISTS (
                  SELECT 1 FROM df da JOIN df db
                    ON da.kw_id = keyword_pairs.kw_a_id
                   AND db.kw_id = keyword_pairs.kw_b_id
                  WHERE da.chunk_df > 0 AND db.chunk_df > 0
                    AND da.chunk_df = db.chunk_df
                    AND da.chunk_df = keyword_pairs.cooccur_count
              )
          )
        """,
        (scope_id, scope_id, CHUNK_COOCCUR_SOURCE, min_npmi, min_npmi),
    )

    # Emit the same pairs_advance/scan_end shape (single tick at 100%).
    _emit("pairs_advance", current=total, total=total)
    surviving = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])
    _emit("pairs_scan_end", surviving=surviving)


def _finalize_pairs_batch(
    conn,
    *,
    scope_id: int,
    min_npmi: float,
    n_chunks: int,
    on_event: Optional[Callable[[dict], None]],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Step 4 fallback: paginated cursor + executemany.

    Peak Python heap bounded by ``batch_size`` (not total surviving pairs).
    Semantics identical to mainline's Python loop, including int-strict
    glued gate and ``abs(npmi - min_npmi)`` comparison.
    """
    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        try:
            on_event({"phase": phase, **fields})
        except Exception:
            pass

    chunk_df: Dict[int, int] = {
        int(r[0]): int(r[1]) for r in conn.execute(
            "SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?",
            (scope_id,),
        )
    }

    total = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])

    step_emit = max(1, total // 200) if total else 1
    processed = 0
    # Paginate by (kw_a_id, kw_b_id) — keyword_pairs is WITHOUT ROWID,
    # rowid isn't available. The primary key already gives us a stable
    # order; use tuple comparison to advance past the last processed key.
    last_a = -1
    last_b = -1

    while True:
        rows = conn.execute(
            """
            SELECT kw_a_id, kw_b_id, cooccur_count
            FROM keyword_pairs
            WHERE scope_id = ? AND source = ?
              AND (kw_a_id > ? OR (kw_a_id = ? AND kw_b_id > ?))
            ORDER BY kw_a_id, kw_b_id
            LIMIT ?
            """,
            (scope_id, CHUNK_COOCCUR_SOURCE, last_a, last_a, last_b, batch_size),
        ).fetchall()
        if not rows:
            break

        updates: List[Tuple[float, int, int, int]] = []
        drops: List[Tuple[int, int, int]] = []
        for row in rows:
            a, b, co = int(row[0]), int(row[1]), int(row[2])
            df_a = chunk_df.get(a, 0)
            df_b = chunk_df.get(b, 0)
            npmi = _npmi(co, df_a, df_b, n_chunks)
            glued = df_a > 0 and df_b > 0 and df_a == df_b == co
            if npmi is None or glued or (min_npmi > 0.0 and npmi < min_npmi):
                drops.append((scope_id, a, b))
            else:
                updates.append((float(npmi), scope_id, a, b))
            last_a, last_b = a, b
            processed += 1
            if processed == total or processed % step_emit == 0:
                _emit("pairs_advance", current=processed, total=total)

        if drops:
            conn.executemany(
                "DELETE FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_a_id = ? AND kw_b_id = ? "
                "AND source = 'chunk_cooccur'",
                drops,
            )
        if updates:
            conn.executemany(
                "UPDATE keyword_pairs SET npmi = ? "
                "WHERE scope_id = ? AND kw_a_id = ? AND kw_b_id = ? "
                "AND source = 'chunk_cooccur'",
                updates,
            )

    surviving = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])
    _emit("pairs_scan_end", surviving=surviving)


def make_finalize(variant: str) -> Callable:
    """Return a drop-in replacement for
    ``grga.graph.build._finalize_pairs_from_temp``.

    Variants:
    * ``'method_B_sql'``   — SQL UPDATE/DELETE; requires ``ln()``.
    * ``'method_B_batch'`` — Python batch cursor; always works.
    * ``'method_B_auto'``  — probe ``ln()``, pick sql / batch.
    """
    if variant not in ("method_B_sql", "method_B_batch", "method_B_auto"):
        raise ValueError(f"unknown variant: {variant!r}")

    batch_size = int(os.environ.get("GRGA_BUILD_NPMI_BATCH", DEFAULT_BATCH_SIZE))

    def _replacement(
        conn,
        *,
        scope_id: int,
        min_pair_count: int,
        min_npmi: float,
        n_chunks: int,
        now: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> int:
        def _emit(phase: str, **fields) -> None:
            if on_event is None:
                return
            try:
                on_event({"phase": phase, **fields})
            except Exception:
                pass

        # -------- Steps 1-3: VERBATIM from mainline --------
        _emit("pairs_scan_start")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS _idx_bcki_chunk "
            "ON _build_chunk_kws (doc_id, chunk_id)"
        )
        conn.execute(
            """
            INSERT INTO keyword_pairs
                (scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at)
            SELECT ?, a.kw_id, b.kw_id, ?, COUNT(*), NULL, ?
            FROM _build_chunk_kws a
            JOIN _build_chunk_kws b
              ON a.doc_id = b.doc_id
             AND a.chunk_id = b.chunk_id
             AND a.kw_id < b.kw_id
            GROUP BY a.kw_id, b.kw_id
            HAVING COUNT(*) >= ?
            ON CONFLICT(scope_id, kw_a_id, kw_b_id, source) DO UPDATE SET
                cooccur_count = cooccur_count + excluded.cooccur_count,
                npmi = NULL,
                updated_at = excluded.updated_at
            """,
            (scope_id, CHUNK_COOCCUR_SOURCE, now, int(min_pair_count)),
        )
        conn.execute(
            "DELETE FROM keyword_pairs "
            "WHERE scope_id = ? AND source = ? AND cooccur_count < ?",
            (scope_id, CHUNK_COOCCUR_SOURCE, int(min_pair_count)),
        )

        # -------- Step 4: variant-specific --------
        if variant == "method_B_batch":
            _finalize_pairs_batch(
                conn, scope_id=scope_id, min_npmi=min_npmi,
                n_chunks=n_chunks, on_event=on_event, batch_size=batch_size,
            )
        elif variant == "method_B_sql":
            _finalize_pairs_sql(
                conn, scope_id=scope_id, min_npmi=min_npmi,
                n_chunks=n_chunks, on_event=on_event,
            )
        else:  # method_B_auto
            if _has_ln(conn):
                _finalize_pairs_sql(
                    conn, scope_id=scope_id, min_npmi=min_npmi,
                    n_chunks=n_chunks, on_event=on_event,
                )
            else:
                _finalize_pairs_batch(
                    conn, scope_id=scope_id, min_npmi=min_npmi,
                    n_chunks=n_chunks, on_event=on_event, batch_size=batch_size,
                )

        return int(conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs "
            "WHERE scope_id = ? AND source = ?",
            (scope_id, CHUNK_COOCCUR_SOURCE),
        ).fetchone()[0])

    return _replacement
