"""L2 graph layer (normalized, schema v5).

This package hosts the offline-built graph index that powers ``related()``
and the L3 vote-in path. The tables live in the same SQLite file that v1's
``deep_search.KeywordLibrary`` uses (default ``~/.config/grga/deep_keywords.db``),
so both layers share a single artefact.

Schema v5 normalizes the repeated ``directory`` / ``keyword`` / ``doc_path``
TEXT columns into three intern tables (``directories`` / ``keywords`` /
``docs``); every business table (``chunks`` / ``keyword_chunks`` /
``keyword_pairs`` / ``heading_docs`` / ``llm_extension_votes`` /
``keyword_counts`` / ``keyword_sources``) references them by integer id.
:func:`grga.graph.schema.ensure_schema` manages the tables and performs a
one-shot chained migration (v1/v3 → v4 → v5) in a single atomic
transaction. :class:`grga.graph.ids.IdInterner` handles string↔id
translation at every query and output boundary.
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
