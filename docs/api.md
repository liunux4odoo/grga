# grga JSON API (`--json` output contract)

> Version: v0.1 (Phase 6.2)
> Every grga v2 subcommand accepts `--json` and emits a single JSON document
> to stdout. This document declares every field so a caller agent (or `jq`)
> can rely on a stable schema. Fields not listed here are not part of the
> contract and may change.

## Conventions

- On **success** the process exits `0` and prints one JSON value.
- On **failure**:
  - `search` / `deep-search` / `suggest` print a JSON object with
    `"ok": false` and an `"error"` string, and exit non-zero.
  - `expand` / `related` / `index build` / `index promote` print a plain
    error string to stderr and exit non-zero (their success JSON has no
    top-level `ok`).
- All line numbers are **1-based**.
- `range` / `chapter_range` are `[start_line, end_line]` inclusive arrays.
- Paths in `search` results are **relative to the search `--directory`**
  unless absolute inputs were given.

---

## `grga search` / `grga deep-search`

Both return the same envelope shape (`deep-search` adds a few planning
fields). Shape:

```jsonc
{
  "ok": true,                         // bool — false on error
  "error": "…",                       // string — present only when ok=false
  "patterns": ["安装"],               // string[] — compiled rga patterns
  "directory": "/abs/path",           // string — resolved search root
  "warning": "…",                     // string? — non-fatal notice
  "skipped_files": [ … ],             // any[]? — records rga could not read
  "answer": { … },                    // object? — deep-search only (see below)
  "matches": [ Match, … ]             // Match[] — see below
}
```

### `Match` object

Context lines (`is_context: true`) carry only the base fields. Real matches
additionally carry the Layer 2 / Layer 1 metadata (present only when
`--metadata`, the default, is on).

Base fields (always present):

| field | type | meaning |
|---|---|---|
| `path` | string | file path (relative to `directory`) |
| `line_number` | int\|null | 1-based line; null for path-only rows |
| `text` | string | the matched or context line text |
| `is_context` | bool | `true` for `-A`/`-B` context lines |
| `absolute_offset` | int\|null | byte offset from rga, when available |
| `submatches` | object[] | `[{match, start, end}, …]` char spans |

Metadata fields (real matches only, `--metadata` on):

| field | type | meaning |
|---|---|---|
| `occurrence_index` | int | 1-based rank of this hit within its file |
| `total_occurrences` | int | total real hits for the query in this file |
| `line_ratio` | float | `line_number / total_lines`, clamped `[0,1]` |
| `nearby_hits` | int | other real hits within ±5 lines in the file |
| `matched_terms` | string[] | raw query terms this hit satisfied |
| `heading_path` | string[]\|absent | Markdown heading chain, e.g. `["安装","macOS"]` |
| `chapter_id` | string\|absent | stable chapter slug (Markdown) |
| `chapter_range` | [int,int]\|absent | `[start_line, end_line]` of the chapter |

Non-Markdown files (or unparsable Markdown) simply omit the three structure
fields; callers should treat "absent" as `null`.

### `answer` object (deep-search only)

| field | type | meaning |
|---|---|---|
| `text` | string | formatted natural-language answer |
| `fields` | object | extracted structured fields per sub-question |
| `citations` | object[] | supporting hit references |
| `reasoning_chain` | object[]? | multihop only — per-hop reasoning |

Deep-search also adds top-level `query`, `strategy`
(`horizontal`\|`multihop`), `initial_query_expression`, `plan`,
`iterations`, `stop_reason`.

---

## `grga expand`

```jsonc
{
  "hit": Hit,                 // the anchor hit (see Hit below)
  "text": "…",               // string — expanded context text
  "level": "chapter",        // string — level ACTUALLY achieved after degrade
  "range": [1, 31],          // [int,int] — inclusive line range
  "truncated": false         // bool — true if a document-level read was capped
}
```

`level` is one of `lines | paragraph | chapter | document`. It may be
**smaller** than requested because of the degrade chain
`chapter → paragraph → lines(±size) → lines(±0)`. Always read it back.

### `Hit` object (serialised dataclass)

| field | type | meaning |
|---|---|---|
| `path` | string | file path |
| `line_number` | int | 1-based anchor line |
| `text` | string | anchor line text (empty when built from `PATH:LINE`) |
| `char_span` | [int,int] | matched char range |
| `occurrence_index` | int | Layer 2 rank (0 if not annotated) |
| `total_occurrences` | int | Layer 2 total (0 if not annotated) |
| `line_ratio` | float | Layer 2 ratio (0.0 if not annotated) |
| `nearby_hits` | int | Layer 2 density (0 if not annotated) |
| `heading_path` | string[]\|null | Layer 1 heading chain |
| `chapter_id` | string\|null | Layer 1 chapter slug |
| `chapter_range` | [int,int]\|null | Layer 1 chapter range |
| `matched_terms` | string[] | raw query terms |
| `is_llm_generated` | bool | `true` if the hit came from L3 |
| `is_context` | bool | context-line flag |
| `absolute_offset` | int\|null | byte offset |
| `submatches` | object[] | char spans |
| `from_query` | string\|null | which query produced it (deep-search) |
| `from_tier` | int\|null | which tier produced it (deep-search) |

---

## `grga related`

Returns a **JSON array** (not an envelope) of `Related` objects, sorted by
`score` descending. Empty array `[]` when the graph index is missing or no
neighbours are found.

```jsonc
[
  {
    "doc_path": "uninstall.md",   // string — related document
    "score": 1.596026,            // float — rounded to 6 dp
    "mode": "graph",              // string — graph | dir | heading
    "reason": "ppr",              // string — provenance tag
    "keywords": ["brew","linux"]  // string[] — matched keywords/headings
  }
]
```

`reason` values by mode:

- `graph`   → `"ppr"` (PageRank hit) or `"direct-seed"` (fallback).
- `dir`     → `"dir:<parent>"`.
- `heading` → `"shared-heading"` (in this case `keywords[]` holds the shared
  heading paths, not keywords).

**Precondition**: run `grga index build` first; otherwise the array is empty.

---

## `grga suggest`

```jsonc
{
  "intent": "expand",            // string — echoes request
  "keywords": ["…"],            // string[] — new keywords (expand)
  "queries": ["…"],             // string[] — reformulated queries (reformulate)
  "keyword_pairs": [["a","b"]],  // [str,str][] — pairs voted into the graph
  "ranking": [2, 0, 1],          // int[] — hit indices, best first (rerank)
  "scores": {"0": 0.9},          // {intIdx: float} — per-hit relevance (rerank)
  "reason": "…",                // string — model's short justification
  "llm_generated": true,         // bool — always true for suggest
  "tokens_used": 0,              // int — total tokens
  "prompt_tokens": 0,            // int
  "completion_tokens": 0,        // int
  "model": "…",                 // string — model name (or "fake-llm" in tests)
  "votes": {                     // object|null — present unless --no-votes
    "pairs_seen": 0,
    "pairs_recorded": 0,
    "votes_added": 0.0
  }
}
```

On error: `{"ok": false, "error": "…"}` and non-zero exit.

`ranking` indices refer to positions in the `hits` array the caller supplied
via `--hits`. `scores` keys are those same indices (as JSON string keys),
values clamped to `[0, 1]`.

---

## `grga index build`

```jsonc
{
  "scope": "/abs/dir",            // string — resolved index scope
  "files_scanned": 4,             // int
  "files_indexed": 4,             // int — files that produced keyword rows
  "files_skipped": 0,             // int
  "keyword_docs_written": 20,     // int — rows written to keyword_docs
  "heading_docs_written": 13,     // int — rows written to heading_docs
  "md_files_indexed": 3,          // int — Markdown files with structure
  "elapsed_seconds": 0.0658,      // float — rounded to 4 dp
  "reasons": { "no_keywords": 1 } // {reason: count} — skip breakdown
}
```

Writes only the v2 tables (`keyword_docs`, `heading_docs`); v1 tables
(`keyword_counts`, `keyword_pairs`) are never touched.

---

## `grga index promote`

```jsonc
{
  "threshold": 3.0,                    // float — vote gate
  "dry_run": false,                    // bool
  "candidates": 5,                     // int — pairs at/above threshold
  "promoted": 4,                       // int — pairs merged into keyword_pairs
  "skipped_already_promoted": 1,       // int
  "pairs": [
    {
      "directory": "/abs/dir",         // string — scope of the vote
      "keyword_a": "brew",             // string
      "keyword_b": "install",          // string
      "vote_count": 4.0,               // float
      "already_promoted": false        // bool
    }
  ]
}
```

Non-destructive: `llm_extension_votes` rows are never deleted; a
`promoted_at` marker prevents double counting unless `--repromote` is set.

---

## `grga check` / `grga tools check`

`check` returns `{ok, paths:{name:path|null}, warnings[], errors[]}`.
`tools check` returns `{platform, tools_dir, tools:{name:{path, source, version}}}`.
These are environment-diagnostic commands, not part of the four-verb search
contract, and are documented here only for completeness.
