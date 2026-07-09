"""Graph-action trace signals for L2/L3 primitives.

Implements the ``docs/design/trace-signals.md`` v1 contract. Only two
verbs emit these signals — ``related`` and ``suggest`` — and only under
``--json``. Everything here is **contract surface**: the shape of
:class:`GraphAction`, the allowed ``op`` values, and the accepted node-id
prefixes are frozen for v1.0.

Design decisions summarised:

* Node ids are opaque strings with one of three prefixes (``kw:``,
  ``file:``, ``head:``). Ids that contain whitespace or control chars
  are URL-encoded so a downstream viewer can round-trip them safely.
* Actions are plain dicts on the wire (JSON-friendly), constructed
  through :class:`GraphAction` for typing and validation on the way in.
* Nothing in this module talks to the LLM or the graph DB directly.
  Emitters live next to the verbs that own the underlying computation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Sequence
from urllib.parse import quote

TRACE_SIGNALS_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Frozen vocabulary                                                           #
# --------------------------------------------------------------------------- #

#: Node-id prefixes callers may observe in v1.0. Any other prefix appearing
#: in ``graph_actions.nodes`` is a bug in grga.
NODE_PREFIXES = ("kw:", "file:", "head:")

OP_TRAVERSE_EDGE = "traverse_edge"
OP_SCORE_NODE = "score_node"
OP_LLM_PROPOSE = "llm_propose"

#: The three ops implemented in v1.0. See ``trace-signals.md`` §4.
OPS = (OP_TRAVERSE_EDGE, OP_SCORE_NODE, OP_LLM_PROPOSE)

#: Vocabulary reserved for future upgrades. Implementation MUST NOT emit
#: these; callers MUST NOT rely on them either. Recorded here so both
#: sides share the same "future words" list.
FUTURE_OPS = (
    "activate_keyword",
    "seed_keyword",
    "hit_file",
    "hit_heading",
    "expand_context",
    "co_activate",
    "llm_replace",
)

# --------------------------------------------------------------------------- #
# Node id encoding                                                            #
# --------------------------------------------------------------------------- #

#: Chars that stay literal inside an id body. Path separators and a small
#: set of harmless punctuation are kept readable so callers can eyeball ids.
_SAFE_CHARS = "/-_.~:#@=,;+()[]{}*"


def _escape(value: str) -> str:
    """URL-encode whitespace and control chars while keeping path glyphs."""
    return quote(value, safe=_SAFE_CHARS)


def encode_keyword(term: str) -> str:
    """Encode a keyword node id (``kw:<folded>``)."""
    return "kw:" + _escape(term)


def encode_file(rel_path: str) -> str:
    """Encode a file node id (``file:<posix rel path>``).

    ``rel_path`` is normalised to POSIX separators before encoding so the
    same document has a stable id across platforms. Absolute paths are
    accepted but discouraged — v1 spec says the body is scope-relative.
    """
    posix = PurePosixPath(*rel_path.replace("\\", "/").split("/")).as_posix()
    return "file:" + _escape(posix)


def encode_heading(rel_path: str, slug: str) -> str:
    """Encode a heading node id (``head:<posix rel path>#<slug>``)."""
    posix = PurePosixPath(*rel_path.replace("\\", "/").split("/")).as_posix()
    return "head:" + _escape(posix) + "#" + _escape(slug)


def node_prefix(node_id: str) -> str:
    """Return the ``kw:`` / ``file:`` / ``head:`` prefix, or ``""``."""
    for prefix in NODE_PREFIXES:
        if node_id.startswith(prefix):
            return prefix
    return ""


# --------------------------------------------------------------------------- #
# GraphAction                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class GraphAction:
    """A single ``graph_actions`` entry.

    ``op`` MUST be one of :data:`OPS`. ``nodes`` obeys per-op arity
    (see ``trace-signals.md`` §4). ``meta`` is free-form but per-op
    required keys are enforced by :meth:`to_dict`.
    """

    op: str
    nodes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "nodes": list(self.nodes),
            "meta": dict(self.meta),
        }


def traverse_edge(
    from_node: str,
    to_node: str,
    *,
    edge_kind: str,
    weight: float,
    hop: int,
    **extra: Any,
) -> GraphAction:
    """Build a ``traverse_edge`` action (exactly two nodes, ``[from, to]``)."""
    meta: Dict[str, Any] = {
        "edge_kind": edge_kind,
        "weight": round(float(weight), 6),
        "hop": int(hop),
    }
    meta.update(extra)
    return GraphAction(op=OP_TRAVERSE_EDGE, nodes=[from_node, to_node], meta=meta)


def score_node(
    node: str,
    *,
    algo: str,
    score: float,
    hops_from_seed: int,
    **extra: Any,
) -> GraphAction:
    """Build a ``score_node`` action (exactly one node)."""
    meta: Dict[str, Any] = {
        "algo": algo,
        "score": round(float(score), 6),
        "hops_from_seed": int(hops_from_seed),
    }
    meta.update(extra)
    return GraphAction(op=OP_SCORE_NODE, nodes=[node], meta=meta)


def llm_propose(
    nodes: Sequence[str],
    *,
    origin_query: str,
    rationale: str,
    **extra: Any,
) -> GraphAction:
    """Build a ``llm_propose`` action (≥1 ``kw:*`` nodes).

    ``rationale`` is allowed to be empty but the key must still be present
    on the wire; callers pass ``""`` when the LLM did not justify itself.
    """
    meta: Dict[str, Any] = {
        "origin_query": str(origin_query),
        "rationale": str(rationale),
    }
    meta.update(extra)
    return GraphAction(op=OP_LLM_PROPOSE, nodes=list(nodes), meta=meta)


def actions_to_wire(actions: Sequence[GraphAction]) -> List[dict]:
    """Serialise a sequence of :class:`GraphAction` for JSON output."""
    return [a.to_dict() for a in actions]


def envelope(
    payload: Dict[str, Any],
    actions: Sequence[GraphAction],
) -> Dict[str, Any]:
    """Attach ``trace_signals_version`` + ``graph_actions`` to ``payload``.

    ``payload`` is mutated in-place *and* returned for callers that prefer
    the fluent style. Existing keys are preserved.
    """
    payload["trace_signals_version"] = TRACE_SIGNALS_VERSION
    payload["graph_actions"] = actions_to_wire(actions)
    return payload
