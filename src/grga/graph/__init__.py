"""L2 graph layer (chunk-level, schema v4).

This package hosts the offline-built graph index that powers ``related()``
and the L3 vote-in path. The tables live in the same SQLite file that v1's
``deep_search.KeywordLibrary`` uses (default ``~/.config/grga/deep_keywords.db``),
so both layers share a single artefact.

The graph tables (``chunks`` / ``keyword_chunks`` / ``keyword_pairs`` /
``heading_docs`` / ``llm_extension_votes``) are managed by
:func:`grga.graph.schema.ensure_schema`, which also performs a one-shot
migration from the doc-level layout (dropping ``keyword_docs`` and rebuilding
``keyword_pairs`` with a ``source`` column). The v1 counter tables
(``keyword_counts`` / ``keyword_sources``) are preserved and never touched.
"""
from grga.graph.schema import (
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
)

__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "ensure_schema",
    "read_schema_version",
]
