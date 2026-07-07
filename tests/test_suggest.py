"""Tests for :mod:`grga.suggest` (Phase 5.1).

We use the ``llm_callable`` test hook instead of a real LLM: it takes the
system prompt and the JSON-serialised user payload and returns a canned
dict, so we can assert on both the payload we send and the parsing of the
response we simulate. Each intent gets at least one case, plus vote-
recording and validation edge cases.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from grga.graph.schema import connect, ensure_schema
from grga.models import Hit
from grga.suggest import VALID_INTENTS, Suggestion, suggest


def _rows(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT directory, keyword_a, keyword_b, vote_count FROM llm_extension_votes"
            ).fetchall()
        ]
    finally:
        conn.close()


def _fake_llm(response: dict):
    """Return a canned llm callable that also asserts the user payload shape."""

    captured: dict = {}

    def _callable(system_prompt: str, user_content: str) -> dict:
        captured["system"] = system_prompt
        captured["user"] = json.loads(user_content)
        return dict(response)

    _callable.captured = captured  # type: ignore[attr-defined]
    return _callable


def test_valid_intents_advertised():
    assert set(VALID_INTENTS) == {"expand", "rerank", "reformulate"}


def test_unknown_intent_raises(tmp_path):
    with pytest.raises(ValueError):
        suggest("q", (), intent="hallucinate", db_path=tmp_path / "v.db")


def test_expand_intent_returns_keywords_and_votes(tmp_path):
    db = tmp_path / "votes.db"
    hits = [
        Hit(path="a.md", line_number=1, text="neural network training"),
        Hit(path="b.md", line_number=2, text="training loss curve"),
    ]
    llm = _fake_llm(
        {
            "keywords": ["deep learning", "backprop", "SGD"],
            "keyword_pairs": [
                ["neural network", "deep learning"],
                ["training", "backprop"],
            ],
            "queries": ["deep learning && training"],
            "reason": "expand along ML training semantics",
        }
    )

    suggestion = suggest(
        "neural network training",
        hits,
        intent="expand",
        directory=tmp_path,
        db_path=db,
        llm_callable=llm,
    )
    assert isinstance(suggestion, Suggestion)
    assert suggestion.llm_generated is True
    assert suggestion.intent == "expand"
    assert "deep learning" in suggestion.keywords
    assert suggestion.queries == ["deep learning && training"]
    assert ("neural network", "deep learning") in suggestion.keyword_pairs
    assert suggestion.reason.startswith("expand")

    # System prompt is the expand template.
    assert "L3 语义扩展器" in llm.captured["system"]
    # User payload carries the query + hit snippets with idx labels.
    payload = llm.captured["user"]
    assert payload["query"] == "neural network training"
    assert payload["intent"] == "expand"
    assert payload["hits"] and payload["hits"][0]["idx"] == 0

    # Votes accumulated for both explicit pairs and the keyword clique.
    rows = _rows(db)
    assert rows, "expected keyword-pair votes to be recorded"


def test_rerank_intent_parses_ranking_and_scores(tmp_path):
    db = tmp_path / "votes.db"
    hits = [
        Hit(path="a.md", line_number=1, text="乐观的心态迎接挑战"),
        Hit(path="b.md", line_number=1, text="悲观的世界观"),
        Hit(path="c.md", line_number=1, text="积极向上的态度"),
    ]
    llm = _fake_llm(
        {
            "ranking": [2, 0, 1],
            "scores": {"2": 0.95, "0": 0.9, "1": 0.05},
            "keyword_pairs": [["乐观", "积极"]],
            "reason": "语义命中乐观主题",
        }
    )
    suggestion = suggest(
        "找一个乐观的例子",
        hits,
        intent="rerank",
        db_path=db,
        llm_callable=llm,
        record_votes_enabled=True,
    )
    assert suggestion.ranking == [2, 0, 1]
    assert suggestion.scores == {2: 0.95, 0: 0.9, 1: 0.05}
    assert suggestion.keyword_pairs == [("乐观", "积极")]
    assert "L3 语义重排器" in llm.captured["system"]


def test_reformulate_intent_translates_intent_to_queries(tmp_path):
    db = tmp_path / "votes.db"
    llm = _fake_llm(
        {
            "queries": [
                "\"深度学习\" && 训练",
                "神经网络 || 深度学习",
            ],
            "keywords": ["深度学习", "神经网络"],
            "keyword_pairs": [["深度学习", "神经网络"]],
            "reason": "concrete keywords for a vague intent",
        }
    )
    suggestion = suggest(
        "帮我找一些教深度学习的资料",
        (),
        intent="reformulate",
        db_path=db,
        llm_callable=llm,
    )
    assert suggestion.queries[0].startswith("\"深度学习\"")
    assert "神经网络" in suggestion.keywords
    assert "L3 查询改写器" in llm.captured["system"]


def test_record_votes_disabled_leaves_table_empty(tmp_path):
    db = tmp_path / "votes.db"
    llm = _fake_llm({"keyword_pairs": [["a", "b"], ["b", "c"]], "keywords": ["a", "b", "c"]})
    suggestion = suggest(
        "q", (), intent="expand", db_path=db, llm_callable=llm, record_votes_enabled=False
    )
    assert suggestion.votes is None
    assert _rows(db) == []


def test_pairs_normalised_and_deduplicated(tmp_path):
    db = tmp_path / "votes.db"
    llm = _fake_llm(
        {
            "keyword_pairs": [
                ["A", "B"],
                ["b", "a"],  # duplicate of the above under folding
                ["a", "a"],  # self-pair, dropped
                ["c"],       # malformed, dropped
                {"a": "x", "b": "y"},  # dict form accepted
            ]
        }
    )
    suggestion = suggest("q", (), intent="expand", db_path=db, llm_callable=llm)
    keys = {tuple(sorted((a.lower(), b.lower()))) for a, b in suggestion.keyword_pairs}
    assert ("a", "b") in keys
    assert ("x", "y") in keys
    assert ("a", "a") not in keys


def test_hit_dicts_and_dataclasses_both_supported(tmp_path):
    db = tmp_path / "votes.db"
    dict_hit = {
        "path": "a.md",
        "line_number": 1,
        "text": "hello",
        "heading_path": ["Chapter 1"],
        "matched_terms": ["hello"],
    }
    captured: dict = {}

    def _callable(system_prompt: str, user_content: str) -> dict:
        captured["user"] = json.loads(user_content)
        return {}

    suggest("q", [dict_hit], intent="expand", db_path=db, llm_callable=_callable)
    assert captured["user"]["hits"][0]["heading_path"] == ["Chapter 1"]
    assert captured["user"]["hits"][0]["matched_terms"] == ["hello"]


def test_token_accounting_via_fake_marker(tmp_path):
    db = tmp_path / "votes.db"
    llm = _fake_llm({"keywords": ["x"], "__tokens__": 123})
    suggestion = suggest("q", (), intent="expand", db_path=db, llm_callable=llm)
    assert suggestion.tokens_used == 123
    assert suggestion.model == "fake-llm"
