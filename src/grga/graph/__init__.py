"""L2 graph layer (Phase 4).

This package hosts the offline-built graph index that powers ``related()``
and the L3 vote-in path. The tables live in the same SQLite file that v1's
``deep_search.KeywordLibrary`` uses (default ``~/.config/grga/deep_keywords.db``),
so both layers share a single artefact.

The v2 tables (``keyword_docs`` / ``heading_docs`` / ``llm_extension_votes``)
are additive: :func:`grga.graph.schema.ensure_schema` never drops or alters
the v1 tables (``keyword_counts`` / ``keyword_pairs`` / ``keyword_sources``).
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
