"""PPR "collapse" regression coverage (plan-2026-07-11 P0).

The 2026-07-11 investigation showed ``related --mode graph`` returning
noise documents at the top even though ``graph inspect`` on the same
seed produced clean neighbours. Root cause: the doc-scoring aggregate
in :func:`grga.related._related_graph_impl` multiplied PPR keyword
weights by the *raw* ``occurrence_count``, so a single file that
repeated one stopword hundreds of times (e.g. ``if`` × 980 in a
minified JS bundle) drowned out every genuinely related document.

The fix is a pure mathematical normalisation:
``weight * log1p(occurrence_count)`` in place of ``weight * occ``. It
compresses the runaway tail without introducing corpus-specific
stopword lists or file-type blacklists — see ``docs/design/plan-
2026-07-11.md`` §P0 for the boundary rules.

The tests below encode the plan's cross-scenario matrix as executable
fixtures:

* **Scenario 1 — code repo**: a Python-project-shaped corpus with a
  noise file that inflates a single stopword to thousands of hits.
  Verifies the noise file no longer occupies the top result for a
  low-frequency seed.
* **Scenario 2 — narrative corpus**: many small chapters plus one
  large filler chapter that repeats the seed's PPR-adjacent hub word
  many times. Verifies chapters that actually contain the seed still
  outrank the hub-only filler.

The tests only assert relative ordering / membership, not exact
scores, so ``log1p`` can be swapped for another monotonically damped
transform later without breaking the guardrail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.related import related


@pytest.fixture
def code_repo_scope(monkeypatch, tmp_path):
    """A code-repo shaped corpus containing one high-repetition noise file.

    ``grga_docs.md`` and ``cli_notes.md`` mention the low-frequency seed
    ``qwenpaw`` alongside genuinely related terms. ``vendor.min.js``
    contains no seed but repeats one stopword-shaped token many times
    to imitate a minified bundle. Prior to the P0 fix, the stopword
    accumulated enough weight through PPR + linear ``occurrence_count``
    to float ``vendor.min.js`` to the top.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "repo"
    scope.mkdir()

    (scope / "grga_docs.md").write_text(
        "# grga docs\n\n"
        "qwenpaw is an agent that uses grga to search docs.\n"
        "qwenpaw calls grga tools install and grga search.\n"
        "docs describe qwenpaw workflows and grga install steps.\n",
        encoding="utf-8",
    )
    (scope / "cli_notes.md").write_text(
        "# cli notes\n\n"
        "qwenpaw invokes grga tools and grga install commands.\n"
        "grga tools install ripgrep pandoc for qwenpaw workflows.\n",
        encoding="utf-8",
    )
    (scope / "readme.md").write_text(
        "# readme\n\n"
        "grga tools install pandoc rga ripgrep. install docs.\n"
        "grga install describes tools and workflows.\n",
        encoding="utf-8",
    )
    # A "minified bundle" style noise file: many repetitions of a single
    # short token that will end up co-occurring with grga via other docs
    # (through common connectives) but never with qwenpaw directly.
    (scope / "vendor.min.js").write_text(
        "if if if if if if if if if if " * 200 + "\n"
        "else else else else else " * 100,
        encoding="utf-8",
    )

    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"scope": scope, "db": db}


def test_low_freq_seed_does_not_collapse_to_noise(code_repo_scope):
    """Regression: qwenpaw-shaped seed must not put the min.js noise on top.

    The scenario mirrors ``docs/design/plan-2026-07-11.md`` P0 scenario 1
    (code repo). Before the log1p normalisation the noise file dominated
    because it repeated a hub stopword hundreds of times; afterwards the
    top result is an actual document mentioning the seed.
    """
    results = related(
        ["qwenpaw"],
        mode="graph",
        db_path=code_repo_scope["db"],
        directory=code_repo_scope["scope"],
        limit=10,
    )
    assert results, "expected at least one graph result"
    top_paths = [r.doc_path for r in results[:5]]
    # The noise file may still appear somewhere, but must not be top-1.
    assert not top_paths[0].endswith(".min.js"), (
        f"noise file collapsed the ranking: {top_paths}"
    )
    # At least three of the top-5 should be non-noise files (the plan's
    # cross-scenario acceptance shape).
    clean = [p for p in top_paths if not p.endswith(".min.js")]
    assert len(clean) >= 3, f"too many noise files in top-5: {top_paths}"


@pytest.fixture
def narrative_scope(monkeypatch, tmp_path):
    """A narrative corpus: many small chapters + one filler chapter.

    Mimics the凡人 (Chinese novel) shape from the plan's scenario 2 while
    staying pure ASCII for cross-platform test stability. ``hero`` is a
    hub character; ``mentor`` is a mid-frequency character introduced in
    a few chapters. ``filler.txt`` repeats ``hero`` and generic
    connectives without the mentor — modestly, in line with the actual
    fanren-corpus evidence (an untargetted chapter has hub occurrences
    on the same order of magnitude as targetted chapters, not orders of
    magnitude larger). Pathological ratios (20×+) are the domain of P3
    (df-based discrimination) and intentionally out of scope here.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "novel"
    scope.mkdir()

    (scope / "ch01.txt").write_text(
        "hero meets mentor in the village. mentor teaches hero herbs.\n"
        "hero and mentor talk. mentor gives hero a book. hero thanks mentor.\n"
        "the mentor was known for medicine. hero listened to mentor closely.\n"
        * 2,
        encoding="utf-8",
    )
    (scope / "ch02.txt").write_text(
        "hero and mentor travel together. mentor explains the path.\n"
        "mentor points at a mountain. hero follows mentor toward it.\n"
        * 2,
        encoding="utf-8",
    )
    (scope / "ch03.txt").write_text(
        "hero fights alone. hero uses techniques. hero wins the duel.\n" * 5,
        encoding="utf-8",
    )
    # Filler / narrative-connective chapter: several ``hero`` mentions but
    # no mentor. Before the P0 fix, even this mild bloat pushed filler
    # above the real mentor-featuring chapters through the linear
    # ``ppr_weight * occurrence_count`` amplifier. Kept intentionally
    # modest (10× base line) so the guardrail rests on real-corpus
    # evidence, not adversarial extremes.
    (scope / "filler.txt").write_text(
        "hero walked. hero paused. hero thought. hero moved on.\n" * 10,
        encoding="utf-8",
    )

    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"scope": scope, "db": db}


def test_mid_freq_seed_prefers_chapters_containing_seed(narrative_scope):
    """Regression: filler chapter must not outrank chapters with the seed.

    Chapters that actually contain the seed keyword ``mentor`` must rank
    above ``filler.txt`` (which lacks the seed entirely but is bloated
    with the hub neighbour ``hero`` at a realistic — not adversarial —
    ratio). This mirrors the plan's scenario 2 acceptance shape on the
    real凡人 corpus.

    Extreme hub concentrations (20×+ ratios) are explicitly out of scope:
    the plan §P3 freezes the ``df``-based discrimination that would
    handle them until a second-scenario caller study confirms it is a
    real recurring problem, not just a synthetic worst case.
    """
    results = related(
        ["mentor"],
        mode="graph",
        db_path=narrative_scope["db"],
        directory=narrative_scope["scope"],
        limit=10,
    )
    assert results, "expected at least one graph result for mentor"
    scores = {r.doc_path: r.score for r in results}
    # Chapters that mention mentor directly.
    seed_chapters = ["ch01.txt", "ch02.txt"]
    filler_score = scores.get("filler.txt", 0.0)
    for ch in seed_chapters:
        assert ch in scores, f"expected {ch} in results, got {list(scores)}"
        assert scores[ch] > filler_score, (
            f"filler out-scored {ch}: filler={filler_score} vs "
            f"{ch}={scores[ch]}"
        )


def test_no_graph_signal_fallback_still_engaged(tmp_path, monkeypatch):
    """Boundary: seed with no graph neighbours still degrades gracefully.

    The plan's edge-case checklist ("单元素" / "降级路径") requires that
    a seed contained in exactly one document — with no meaningful
    co-occurrences elsewhere — still returns that document via the
    direct-seed fallback rather than emitting nothing. This lets the
    log1p change coexist with the pre-existing fallback branch.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "lonely"
    scope.mkdir()
    (scope / "only.md").write_text(
        "solitary keyword lives here alone.\n", encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    results = related(
        ["solitary"],
        mode="graph",
        db_path=db,
        directory=scope,
        limit=5,
    )
    assert results, "single-doc seed should still return via fallback"
    assert any(r.doc_path == "only.md" for r in results)
