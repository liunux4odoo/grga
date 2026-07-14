"""``suggest()`` API - L3 LLM extension (Phase 5.1).

This is the only grga v2 primitive that calls an LLM. It never runs
autonomously: the caller agent decides when L1/L2 evidence is
insufficient and dispatches ``suggest()`` with one of three intents.

Intents (see ``docs/design/v2-design.md`` §4.3 and §7.4):

* ``expand``      - propose additional keywords / follow-up queries given
                    the current hits.
* ``rerank``      - rerank the supplied hits by semantic relevance.
* ``reformulate`` - rewrite a vague query into concrete keyword expressions.

Every :class:`Suggestion` returned is stamped ``llm_generated=True`` so
downstream code can tell L3 output apart. Any keyword pairs the LLM emits
(as ``keyword_pairs``) or implies (via ``keywords`` cliques) are recorded
in ``llm_extension_votes`` through :func:`grga.graph.votes.record_votes`;
they do not enter ``keyword_pairs`` directly (that requires the
:func:`grga.graph.promote.promote_votes` gate).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from grga.graph.votes import VoteResult, record_votes, record_votes_from_keywords
from grga.models import Hit
from grga.prompts import available_intents, load_prompt

_logger = logging.getLogger("grga.suggest")

VALID_INTENTS = available_intents()
DEFAULT_MAX_HITS = 20
DEFAULT_SNIPPET_CHARS = 400


@dataclass
class Suggestion:
    """A single L3 suggestion output.

    ``intent`` echoes the request. ``keywords`` and ``queries`` are the
    actionable outputs the caller should feed back into ``search()`` /
    ``related()``. ``ranking`` / ``scores`` are populated for ``rerank``.
    ``keyword_pairs`` is what got voted into ``llm_extension_votes``.
    """

    intent: str
    keywords: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    keyword_pairs: List[Tuple[str, str]] = field(default_factory=list)
    ranking: List[int] = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    reason: str = ""
    llm_generated: bool = True
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw: dict = field(default_factory=dict)
    votes: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "keywords": list(self.keywords),
            "queries": list(self.queries),
            "keyword_pairs": [list(p) for p in self.keyword_pairs],
            "ranking": list(self.ranking),
            "scores": dict(self.scores),
            "reason": self.reason,
            "llm_generated": self.llm_generated,
            "tokens_used": self.tokens_used,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "model": self.model,
            "votes": self.votes,
        }


# --------------------------------------------------------------------------- #
# LLM call glue                                                               #
# --------------------------------------------------------------------------- #

def _hit_to_snippet(hit: Union[Hit, Mapping[str, Any]], max_chars: int) -> dict:
    """Normalise a Hit (dataclass or dict) into a compact payload for the LLM."""
    if isinstance(hit, Hit):
        payload = {
            "path": hit.path,
            "line": hit.line_number,
            "text": hit.text[:max_chars] if hit.text else "",
        }
        if hit.heading_path:
            payload["heading_path"] = list(hit.heading_path)
        if hit.matched_terms:
            payload["matched_terms"] = list(hit.matched_terms)
        return payload
    text = str(hit.get("text", ""))
    payload = {
        "path": str(hit.get("path", "")),
        "line": hit.get("line_number") or hit.get("line") or 0,
        "text": text[:max_chars],
    }
    if hit.get("heading_path"):
        payload["heading_path"] = list(hit["heading_path"])
    if hit.get("matched_terms"):
        payload["matched_terms"] = list(hit["matched_terms"])
    return payload


def _call_llm(
    llm_callable: Optional[Callable[[str, str], dict]],
    system_prompt: str,
    user_payload: dict,
    llm_base_url: Optional[str],
    llm_api_key: Optional[str],
    llm_model_name: Optional[str],
    debug: bool,
) -> Tuple[dict, int, int, int, str]:
    """Dispatch to a fake callable (tests) or the real OpenAI client.

    Returns ``(json_object, tokens_used, prompt_tokens, completion_tokens, model_name)``.

    ``debug=True`` mirrors :func:`grga.deep_search._chat_json`'s behaviour:
    system + user payload and the raw model response are echoed through the
    ``grga.suggest`` logger so operators can trace what the L3 layer sent.
    """
    user_content = json.dumps(user_payload, ensure_ascii=False)

    if debug:
        _logger.debug("suggest LLM request system=%s", system_prompt[:500])
        _logger.debug("suggest LLM request user=%s", user_content[:1000])

    if llm_callable is not None:
        # Tests pass a plain function ``(system, user_json) -> dict`` so no
        # network or real LLM is required. Token accounting stays zero
        # unless the fake returns ``__tokens__`` in its payload.
        result = llm_callable(system_prompt, user_content)
        if not isinstance(result, dict):
            result = {}
        tokens = int(result.pop("__tokens__", 0)) if "__tokens__" in result else 0
        if debug:
            _logger.debug("suggest LLM fake response tokens=%d payload=%s", tokens, str(result)[:1000])
        return result, tokens, tokens, 0, "fake-llm"

    # Real path: reuse the deep_search client factory so credentials, base
    # URL, and model resolution stay consistent with the rest of grga.
    from grga.deep_search import _make_openai_client

    client, model_name = _make_openai_client(llm_base_url, llm_api_key, llm_model_name)
    # ``_chat_json`` throws away the raw ``response`` object, so we recreate
    # the OpenAI call here to capture ``usage``. On failure fall back to
    # ``_chat_json`` (which handles the older ``response_format`` rejection).
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # pragma: no cover - depends on backend
        if "response_format" not in str(exc):
            raise
        response = client.chat.completions.create(
            model=model_name, messages=messages, temperature=0.2
        )
    content = response.choices[0].message.content or "{}"
    if debug:
        _logger.debug("suggest LLM response=%s", content[:1000])
    from grga.deep_search import _loads_json_object

    payload = _loads_json_object(content)
    usage = getattr(response, "usage", None)
    prompt_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0
    completion_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0
    total = int(getattr(usage, "total_tokens", prompt_tok + completion_tok) or 0) if usage is not None else 0
    return payload, total, prompt_tok, completion_tok, model_name


# --------------------------------------------------------------------------- #
# Payload normalisation                                                       #
# --------------------------------------------------------------------------- #

def _clean_str_list(values: Any, *, max_len: int = 40, cap: int = 32, strip_quotes: bool = True) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    seen: set = set()
    out: List[str] = []
    for value in values:
        text = str(value).strip()
        if strip_quotes:
            text = text.strip("'\"")
        if not text or len(text) > max_len or len(text) < 1:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _clean_query_list(values: Any, *, max_len: int = 500, cap: int = 16) -> List[str]:
    """Query expressions may legitimately start with a quote (``"phrase" && x``),
    so never strip outer quotes and allow much longer strings than plain keywords."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    seen: set = set()
    out: List[str] = []
    for value in values:
        text = str(value).strip()
        if not text or len(text) > max_len:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _clean_pairs(values: Any, *, cap: int = 64) -> List[Tuple[str, str]]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []
    out: List[Tuple[str, str]] = []
    seen: set = set()
    for entry in values:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            a, b = str(entry[0]).strip(), str(entry[1]).strip()
        elif isinstance(entry, dict):
            a, b = str(entry.get("a", "")).strip(), str(entry.get("b", "")).strip()
        else:
            continue
        if not a or not b:
            continue
        key = tuple(sorted((a.casefold(), b.casefold())))
        if key[0] == key[1] or key in seen:
            continue
        seen.add(key)
        out.append((a, b))
        if len(out) >= cap:
            break
    return out


def _clean_ranking(values: Any, *, cap: int) -> List[int]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []
    out: List[int] = []
    seen: set = set()
    for value in values:
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
        if len(out) >= cap:
            break
    return out


def _clean_scores(values: Any) -> dict:
    if not isinstance(values, Mapping):
        return {}
    out: dict = {}
    for key, val in values.items():
        try:
            idx = int(key)
            score = float(val)
        except (TypeError, ValueError):
            continue
        out[idx] = max(0.0, min(1.0, score))
    return out


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

def suggest(
    query: str,
    hits: Sequence[Union[Hit, Mapping[str, Any]]] = (),
    *,
    intent: str = "expand",
    scope: Optional[str] = None,
    max_hits: int = DEFAULT_MAX_HITS,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    record_votes_enabled: bool = True,
    vote_weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    llm_base_url: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_model_name: Optional[str] = None,
    llm_callable: Optional[Callable[[str, str], dict]] = None,
    debug: bool = False,
) -> Suggestion:
    """Ask an LLM to expand / rerank / reformulate a search.

    Args:
        query: user's original query (natural language or keyword expression).
        hits: current hits (:class:`Hit` objects or v1 dicts). Empty for
            cold-start ``reformulate`` requests.
        intent: one of ``expand``, ``rerank``, ``reformulate``.
        scope: logical scope name (v10) to attribute vote records to.
            ``None`` disables vote recording (no scope to attribute
            knowledge to).
        max_hits: cap on how many hits get shipped to the LLM.
        snippet_chars: per-hit text truncation before serialisation.
        record_votes_enabled: when ``True`` (default), emitted keyword pairs
            are voted into ``llm_extension_votes``. Set ``False`` for a
            read-only dry run.
        vote_weight: passed through to :func:`record_votes`.
        db_path: SQLite path for votes (defaults to shared deep-search DB).
        llm_*: OpenAI-compatible client knobs; fall back to env vars via
            :func:`grga.deep_search._make_openai_client`.
        llm_callable: **test hook** - a ``(system_prompt, user_json) -> dict``
            callable that bypasses the real LLM. Enables deterministic tests.
        debug: forwarded to the underlying LLM call.

    Raises:
        ValueError: on unknown ``intent``.
    """
    if intent not in VALID_INTENTS:
        raise ValueError(f"unknown intent: {intent!r} (expected {VALID_INTENTS})")

    system_prompt = load_prompt(intent)
    snippets = [_hit_to_snippet(h, snippet_chars) for h in list(hits)[:max_hits]]
    numbered = [dict(idx=i, **snip) for i, snip in enumerate(snippets)]
    user_payload = {
        "task": f"grga L3 suggest / intent={intent}",
        "query": query,
        "intent": intent,
        "hits": numbered,
    }

    raw, total_tok, prompt_tok, completion_tok, model_name = _call_llm(
        llm_callable,
        system_prompt,
        user_payload,
        llm_base_url,
        llm_api_key,
        llm_model_name,
        debug,
    )

    keywords = _clean_str_list(raw.get("keywords"))
    queries = _clean_query_list(raw.get("queries") or raw.get("query"))
    pairs = _clean_pairs(raw.get("keyword_pairs"))
    ranking = _clean_ranking(raw.get("ranking"), cap=len(numbered) or 32)
    scores = _clean_scores(raw.get("scores"))
    reason = str(raw.get("reason", "")).strip()

    suggestion = Suggestion(
        intent=intent,
        keywords=keywords,
        queries=queries,
        keyword_pairs=pairs,
        ranking=ranking,
        scores=scores,
        reason=reason,
        tokens_used=total_tok,
        prompt_tokens=prompt_tok,
        completion_tokens=completion_tok,
        model=model_name,
        raw=raw,
    )

    if record_votes_enabled:
        votes_summary: dict = {"pairs_seen": 0, "pairs_recorded": 0, "votes_added": 0.0}
        # Explicit pairs get a full-weight vote each; keyword cliques get a
        # fractional weight so a 5-keyword list doesn't outweigh explicit
        # pair signals from the LLM. Both writes share one connection so a
        # single ``suggest()`` call only opens the DB once.
        if pairs or (keywords and len(keywords) >= 2):
            from grga.graph.schema import connect, ensure_schema

            from grga.deep_search import DEFAULT_DB_PATH

            target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
            vote_conn = connect(target_db)
            try:
                ensure_schema(vote_conn)
                if pairs:
                    pair_result = record_votes(
                        pairs,
                        scope=scope,
                        weight=vote_weight,
                        conn=vote_conn,
                    )
                    _merge_vote_summary(votes_summary, pair_result)
                if keywords and len(keywords) >= 2:
                    clique_result = record_votes_from_keywords(
                        keywords,
                        scope=scope,
                        weight=vote_weight * 0.5,
                        conn=vote_conn,
                    )
                    _merge_vote_summary(votes_summary, clique_result)
            finally:
                vote_conn.close()
        suggestion.votes = votes_summary

    return suggestion


def _merge_vote_summary(summary: dict, result: VoteResult) -> None:
    summary["pairs_seen"] += result.pairs_seen
    summary["pairs_recorded"] += result.pairs_recorded
    summary["votes_added"] = round(summary["votes_added"] + result.votes_added, 4)
