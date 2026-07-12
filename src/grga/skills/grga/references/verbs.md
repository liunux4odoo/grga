# Verb signatures and field reference

Referenced from "The five verbs" section of `SKILL.md`. The SKILL.md body
keeps each verb's one-line purpose and "Use when"; this file holds the
conceptual signatures and per-field semantics. Authoritative JSON schemas
for every command live in `docs/api.md` in the grga repo.

All verbs are **idempotent** and **read-only** by default, except
`suggest` (which may vote into `llm_extension_votes`) and `index build` /
`index promote` (which write to the SQLite graph DB).

## 0. `probe`

```
probe(query: str, scope: dir, *, mode=auto|regex|glob, case_sensitive=False,
      multiline=False, iglob=[], path_only=False, implicit_operator=and|or,
      sample=0, per_file=False, timeout=60) ->
  {ok, directory, patterns, terms, pcre2, implicit_operator, implicit_notice,
   total_lines, file_count, truncated, warning, skipped_files, elapsed_ms,
   per_file?[{path, matches}], sample?{sampled_lines, avg_line_bytes, est_total_bytes}}
```

- `total_lines` — total matching lines across the corpus (the real hit count `search` would return, **before** context expansion and **ignoring** `--max-count` / `--max-total`).
- `file_count` — number of files with at least one hit.
- `--sample N` — additionally sample up to N matched lines to estimate `avg_line_bytes` and `est_total_bytes`; the estimate is order-of-magnitude only, not an exact byte count.
- `--per-file` — attach per-file match counts (descending), useful for spotting a single dominating file.
- Query-semantic flags (`--mode` / `--case-sensitive` / `--iglob` / `--multiline` / `--path-only` / `--implicit-and` / `--implicit-or`) are **identical** to `search`, so the probed query and the executed query are the same. `--timeout` is a global budget covering both the count and the sampling pass.
- On failure the JSON is **homogeneous** with the success shape (same fields, zeroed) plus `ok:false` and `error`.

`probe` makes **no** decision itself — it returns numbers, never rewrites the query, never judges "too many / too few". The caller owns the threshold logic.

## 1. `search`

```
search(query: str, scope: dir, *, mode=auto|regex|glob, case_sensitive=False,
       before_context=3, after_context=3, iglob=[], path_only=False,
       max_total=1000, strict_cap=False, implicit_operator=and|or,
       metadata=True) -> {ok, patterns, directory, truncated, matches[]}
```

**Multi-term semantics (v2, breaking change):** space-separated terms are an
**implicit AND** — `search("韩立 师傅")` returns only lines containing *both*
terms (Google / ripgrep intuition). Use explicit `||` / `OR` for OR, `&&` /
`AND` for AND. Pass `--implicit-or` to restore the old OR-on-space behaviour.
When implicit AND fires, grga prints a one-line notice to stderr.

**Global cap:** `--max-total N` (default `1000`) bounds the *total* number of
real hits returned, streaming and killing rga once the cap is hit; the result
sets `truncated: true`. This keeps response time predictable on huge single
files (novels, GB-scale logs) where the per-file `--max-count / -n` does not
help. Use `--strict-cap` to fail fast with `error: too_many_matches` instead of
returning a partial slice. `0` disables the cap.

Each `matches[i]` (non-context) carries:

- `path`, `line_number`, `text`, `submatches`, `is_context`
- `occurrence_index` / `total_occurrences` — 1-based rank within the file
  (computed **after** any `--max-total` truncation)
- `line_ratio` — `line_number / total_lines`, clamped to `[0, 1]`
- `nearby_hits` — count of other real hits within ±5 lines
- `matched_terms` — the raw query terms this hit satisfied
- `heading_path` / `chapter_id` / `chapter_range` — best-effort, Markdown only

The envelope also carries `truncated` (bool) and, when adjacency was treated
as implicit AND, `implicit_notice` (string).

## 2. `expand`

```
expand(hit: {path, line_number}, level, size=3, *, base_dir) ->
  {hit, text, level, range: [start,end], truncated}
```

Levels degrade predictably: `chapter → paragraph → lines(±3) → lines(±0)`.
The returned `level` field reflects what was **actually** achieved (may be
smaller than requested). Never invokes an LLM. Never modifies anything.

## 3. `related`

```
related(keywords: list[str], *, mode="graph"|"dir"|"heading",
        limit=20, db_path, directory,
        cross_scope=False,   # graph only: expand PPR into other scopes
        same_scope=False,    # heading only: stay inside --directory
        min_npmi=None,       # graph only: npmi edge threshold (default 0.2)
) ->
  # dir | heading (CLI --json: {"results": [...]})
  [{doc_path, score, mode, reason, keywords[], directory, qualified_path}]
  # graph (list variant, no preview)
  [{doc_path, score, mode, reason, keywords[], directory, qualified_path,
    keyword_contributions: [{kw, occurrences}]}]
  # graph via `related_graph_with_preview(..., preview=True)`
  {results: [...],
   crossed_scopes_preview: {would_add, top_other_scopes,
                             top_bridge_keywords} | null,
   expansion: {
     seeds: [{kw, chunk_df?}],
     expanded_keywords: [{kw, ppr_score, hops_from_seed?, chunk_df?,
                          edges_to_seeds: [{seed, npmi, cooccur_count,
                                             source}]}],
     total_chunks_in_scope}}
```

Every `Related` carries `directory` (the indexed scope root) and
`qualified_path` (`<directory>/<doc_path>`) so cross-scope results whose
relative paths collide (e.g. two `README.md`) remain distinct.

Modes:

- `graph` — Personalized PageRank over the segment co-occurrence graph (`keyword_pairs`, `source='chunk_cooccur'`), then materialised to documents through `keyword_chunks`. Best for "conceptually related" navigation. Every `--json` result carries a `keyword_contributions` array of `{kw, occurrences}` (per-doc per-keyword hit counts, sorted `(-occurrences, kw)`). All query-level graph signals live once at the payload root under `expansion`: each seed carries `chunk_df`; each expanded keyword carries `ppr_score`, `hops_from_seed`, `chunk_df`, and `edges_to_seeds: [{seed, npmi, cooccur_count, source}]`. `total_chunks_in_scope` puts `chunk_df` into context. Together these let a caller filter hub-word false positives (e.g. drop expansions with `chunk_df` above a threshold) without querying the DB — see `docs/design/related-signals-v2.md`. Default is scope-isolated; add `--cross-scope` to reach other corpora, and read `crossed_scopes_preview` first (see `references/storage-model.md`).
- `dir`   — documents that live in the **same directory** as documents containing the seed keywords. Best for local repository navigation. Cross-scope has no meaning here (directory proximity is intrinsically scope-local).
- `heading` — documents that share a full or leaf **Markdown heading path** with a seed-carrying document. Cross-scope by default (heading paths are structural filters that resist same-name-different-meaning noise). Add `--same-scope` to keep matches inside `-d`.

Edge filter (`--mode graph` only, mutually exclusive):

- `--min-npmi FLOAT` — drop `chunk_cooccur` edges below this npmi before PPR. Default **0.2** (env `GRGA_PPR_MIN_NPMI`). Raises signal-to-noise by cutting hub/function-word edges; `0` restores pre-2026-07-12 behaviour. `llm_promoted` edges (npmi NULL) always kept.
- `--edge-quantile FLOAT` — convenience: keep top X fraction of edges by npmi (e.g. `0.1` = top 10%), sampled then translated to `min_npmi`. Cannot combine with `--min-npmi` (exit 2).

Backend (env-only): `GRGA_PPR_BACKEND=auto|numpy|py` (default `auto`; numpy used when importable + graph ≥ `GRGA_PPR_NUMPY_MIN_EDGES`, default 50000). numpy is optional (`pip install grga[perf]`); both backends give byte-identical top-K.

**Precondition**: `grga index build -s <dir> --db <path>` must have been run against the corpus (or any indexed scope inside the shared DB) first, otherwise `related` returns an empty result.

## 4. `suggest`

```
suggest(query, hits, *, intent, directory, max_hits=20, no_votes=False,
        llm_base_url, llm_api_key, llm_model, db_path) ->
  {intent, keywords[], queries[], keyword_pairs[[a,b],...],
   ranking[], scores{}, reason, llm_generated: true,
   tokens_used, prompt_tokens, completion_tokens, model, votes: {...}}
```

The **only** verb that calls an LLM. It never runs autonomously; the caller
decides when L1/L2 evidence is insufficient.

Intents:

- `expand`     — propose more keywords / follow-up queries. Feed `keywords` into the next `search` or `related`.
- `rerank`     — return a `ranking` (list of indices into the supplied `hits`) plus per-index `scores`.
- `reformulate` — rewrite a vague natural-language query into concrete search expressions in `queries[]`.

By default, emitted `keyword_pairs` are voted into `llm_extension_votes`. After
enough votes accumulate, run `grga index promote` to graduate them into
`keyword_pairs`, at which point `related --mode graph` picks them up for free.

## Supporting commands (offline / infra)

- `grga index build -s <scope> --db <path> --json` — index a directory scope's segments (`chunks` + `keyword_chunks` + `keyword_pairs[chunk_cooccur]`) and `heading_docs` into the **shared** user knowledge graph. Prerequisite for `related`. Every scope writes into the same DB; scope is stored as a `dir_id` foreign key into the `directories` intern table (v5 normalized layout), not a separate database. `--chunk-mode line` (default, 1 line = 1 chunk) or `--chunk-mode chars --chunk-max-chars 600 --chunk-min-chars 300` (multi-line windows split on heading > blank line > hard char cut).
- `grga index promote --threshold N --db <path> [--dry-run] --json` — merge high-vote pairs from `llm_extension_votes` into `keyword_pairs`. Idempotent; `--dry-run` reports without writing.
- `grga graph inspect <keyword> --db <path> --json` — report a keyword's `docs_by_scope` spread, `top_cooccurring_keywords`, and a `suspected_polysemy` warning. Run it on a bridge keyword before enabling `related --cross-scope`.
