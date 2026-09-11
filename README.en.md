# grga

> **A zero-cost, document-learning, embeddings-free retriever that travels well.** — Automatically discovers conceptual connections from your documents, and the learned knowledge can be directly applied to same-domain document retrieval. Runs entirely locally, no embeddings or LLM required.

`grga` is an agent-friendly full-text search tool built on `ripgrep-all`, offering both a CLI entry point and a Tkinter GUI. It wraps underlying tools like `rga` and `rg`, supports searching plain text, Markdown, PDF, Office documents, and more, and also supports path-only file search.

The project goal is to package common search capabilities, toolchain checks, and built-in tool management into a single Python package, with standalone executable support, minimizing the cost of manual system command setup.

### How is it different from plain grep?

grga is "the outermost wrapper of the grep family": it reuses `ripgrep`'s speed and `ripgrep-all`'s document adapters at the bottom, then stacks several layers on top — **context expansion**, **co-occurrence graph**, and **LLM deep search**. The table below compares each tool's capabilities in the order of "from grep all the way up to grga", with corresponding grga commands and dependencies:

| Capability | grep | `rg` | `rga` | grga command / usage | Dependency / config |
|---|:---:|:---:|:---:|---|---|
| Plain text / source code search | ✅ | ✅ | ✅ | `grga search keyword` | None |
| Large corpus speed (parallel + `.gitignore`) | ❌ single-threaded | ✅ multi-threaded | ✅ inherits rg | Same as above, transparently uses `rg` | `rg` (auto-downloaded on first `grga tools install`) |
| PDF / Office / EPUB / archives / sqlite | ❌ | ❌ | ✅ built-in adapters | Same as above, transparently uses `rga` | `rga` + `rga-preproc` + `pandoc` + `pdftotext` (auto-downloaded) |
| **Enhanced Office docs** (native table/slide/Word structure extraction for xlsx/pptx/doc/xls/ppt) | ❌ | ❌ | ⚠️ rga extracts plain text only | `grga search` automatically uses custom `pandoc_office` adapter | `pandoc` (included in auto-downloaded toolchain) |
| Query expressions (phrase / regex / glob / `\|\|` / `&&` / parentheses) | ⚠️ regex only | ⚠️ regex only | ⚠️ regex only | Default multi-word **AND**; `--implicit-or` restores v1 OR semantics | None |
| Path search (filename only, no content read) | ⚠️ needs `find` | ⚠️ needs `--files \| rg` | ⚠️ same as rg | `grga search --path-only` | None |
| Preview & probe (check scale before deciding to read) | ❌ | ❌ | ❌ | `grga preview` / `grga probe` | None |
| Paragraph / chapter context expansion for Markdown / PDF | ❌ | ❌ | ❌ | `grga expand` (line / paragraph / chapter / TOC逐级) |  |
| Keyword co-occurrence graph (offline index + sub-millisecond queries) | ❌ | ❌ | ❌ | `grga index build --scope <name>` → `grga related` / `grga graph inspect` | |
| LLM deep search, query reformulation, result reranking | ❌ | ❌ | ❌ | `grga deep-search` / `grga suggest` | OpenAI-compatible API; at minimum `GRGA_OPENAI_API_KEY` + `GRGA_OPENAI_MODEL` (see [docs/env-vars.md](docs/env-vars.md)) |
| **MCP Server** (Model Context Protocol, 6 built-in tools) | ❌ | ❌ | ❌ | `grga mcp start` launches stdio MCP server | None (MCP client such as Claude Desktop, etc.) |
| Agent-friendly (`--json` + stable exit code contract + Skill pack) | ❌ | ⚠️ partial | ⚠️ partial | `grga skill install` distributes built-in agent skill pack + `--json` structured output | Error contract at [docs/exit-codes.md](docs/exit-codes.md) |

All environment variables can be written to `$XDG_CONFIG_HOME/grga/.env` (a commented template is auto-generated on first run). The full list is in [docs/env-vars.md](docs/env-vars.md). More CLI parameter details are in [docs/CLI.md](docs/CLI.md).

> **Runtime download of external tools**: Since 0.2.0, the wheel no longer embeds binaries like `rg` / `rga` / `pandoc` / `fzf` / `pdftotext`. They are downloaded on-demand to the user cache directory on first use (see below), or you can pre-install them with `grga tools install`.
>
> **License**: `grga` itself is released under the **MIT** license (see [LICENSE](LICENSE)). The external tools it invokes follow their respective upstream licenses (`rg`: MIT/Unlicense; `rga`: AGPL; `pandoc`: GPL; `fzf`: MIT). Please comply with each.

[中文版](README.md) · English

### How is it different from other GraphRAGs?
- No LLM-based entity extraction
- No embeddings or vector databases
- Learns phrases and phrase self-connections statistically from the corpus; what's learned is stored in SQLite, decoupled from the original corpus
- You can learn from a representative subset of documents and apply it to other same-domain document libraries. The trade-off is theoretical precision drift, which is entirely acceptable in practice. If drift becomes unacceptable, just re-do incremental learning on the new corpus.
- Doesn't obsess over recall accuracy; provides 5 primitive verbs and relies on the agent to do multi-round retrieval loops to gather evidence.

## Quick Start

```bash
# install grga
pip install grga
```

After installation, you can **directly search plain-text files** (`.txt` / `.md` / source code / logs / JSON / CSV, etc.) with no extra setup:

```bash
cd /path/to/your/documents
grga search "wall plan"           # full-text search
grga search "wall plan" --path-only  # filename only
grga gui                            # GUI (or grga-gui)
grga --help                        # command help
```

**Connect grga to your AI agent** (two ways, pick one):

- **Skill pack** (for agents that support skills, like Kilo / Claude Code / QwenPaw):
  ```bash
  grga skill install --to ~/.kilocode/skills   # replace with your agent's skill directory
  ```

- **MCP Server** (for clients that support the Model Context Protocol, like Claude Desktop / Cursor):
  Add this to your MCP client config:
  ```json
  { "mcpServers": { "grga": { "command": "grga", "args": ["mcp", "start", "--directory", "/path/to/docs"] } } }
  ```

**Pre-training (optional)** — takes time depending on corpus size, but unlocks graph expansion capabilities:

```bash
# --scope is a logical domain name (migratable across machines, decoupled from local paths, pick any meaningful name);
# --directory defaults to the current directory when omitted (covered by the cd above)
cd /path/to/your/documents
grga index build --scope my-notes
```
The `docs/search-demo` folder provides three search result styles — "LLM-only answer", "plain text retrieval answer", and "graph-expanded answer" — so you can see the difference clearly.

### By Use Case

**Searching PDF / Office / e-books** — install the toolchain once on first use (`rga` / `pandoc` / `pdftotext`, etc., auto-reused afterward):

```bash
grga tools install
grga search "wall plan" -g "*.pdf"
```

**Context expansion** — expand a hit into a paragraph / chapter / table of contents:

```bash
grga expand ...                    # expand by line / paragraph / chapter
grga expand --level toc doc.md   # extract single-document table of contents
```

**Keyword co-occurrence graph** (no LLM needed, build index first then query):

```bash
cd /path/to/your/documents
grga index build --scope my-notes             # --directory defaults to current directory when omitted
grga index build --scope sci-fi --tag scifi --tag novel   # attach multi-value tags to scope
grga related "wall plan" --scope my-notes     # omit --scope to query across all scopes
grga related "wall plan" --tag scifi --tag novel --tag-logic and   # select scopes by tag (order doesn't matter, and/or)
grga graph inspect "wall plan"
```

> **Index build tuning**: On large corpora, build storage (`keyword_pairs` often accounts for 90%+ of the DB), memory, and runtime are highly parameter-sensitive. The core trade-offs — `--min-npmi-build` is an **irreversible** write gate (read-side `related --min-npmi` can only tighten, not loosen); `--batch-chunks` trades recall for lower memory; long-term storage relies on splitting scopes + knowledge pack archiving — see **[docs/build-tuning.md](docs/build-tuning.md)** for the full decision table and scenario recipes.

**LLM deep search** (requires OpenAI-compatible credentials, see [docs/env-vars.md](docs/env-vars.md)):

```bash
export GRGA_OPENAI_API_KEY=sk-...
export GRGA_OPENAI_MODEL=gpt-4o-mini
# or write them to $HOME/.config/grga/.env

grga deep-search "wall plan"
```

> Note: The author hasn't actually used the deep search feature much — plain text search + graph expansion usually solves the problem. This feature mainly exists as a fallback for manual search when no code agent is available.

**Packaging as an executable / offline deployment / using as agent primitives** — see [docs/install-and-usage.md](docs/install-and-usage.md):

```bash
python -m build                       # build wheel
python scripts/build-onefile.py --clean   # build single-file executable
```

> Complete installation, tool download, mirror/package build, and advanced AI agent instructions are all in **[docs/install-and-usage.md](docs/install-and-usage.md)**; environment variable list in [docs/env-vars.md](docs/env-vars.md), CLI parameters in [docs/CLI.md](docs/CLI.md).

## Acknowledgments

`grga` owes its existence to two tools that have continuously boosted daily development:

- **[QwenPaw](https://github.com/liunux4odoo/qwenpaw)** — Local multi-agent framework. Many design decisions in `grga` (skill distribution, exit code contracts, `--json` schema, caller reflection loops) were first honed in QwenPaw's agent scenarios before being codified into specifications.
- **[Kilo Code](https://kilocode.ai)** — AI programming assistant inside the IDE. Most of grga's implementation, testing, and refactoring were handled by it; the author only writes "goals / constraints / boundaries", leaving implementation details to it — this division of labor ("spec gives constraints, implementation gives freedom") is exactly what allows this project to evolve small and fast.
