"""MCP (Model Context Protocol) server for grga.

Exposes grga's five retrieval verbs (probe / search / expand / related /
suggest) plus ``scope list`` as MCP tools over stdio. Design:
``docs/design/plan-2026-09-02-mcp-server.md``.

The module is an optional extra (``pip install grga[mcp]``); the base
package never imports ``mcp`` at load time.
"""

__all__ = ["server", "tools", "utils"]
