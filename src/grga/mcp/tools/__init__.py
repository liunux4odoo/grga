"""MCP tool adapters: one module per grga tool.

Each module holds a single async function with typed parameters. The
function body does only three things (plan §3): validate parameters →
call the corresponding grga internal function (offloaded with
``asyncio.to_thread`` so the stdio event loop stays responsive during
subprocess-backed calls) → format the return value. No business logic.
"""
