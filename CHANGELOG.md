# Changelog

All notable changes to `grga` are recorded here.

## Unreleased

### Breaking changes

- **`grga search` multi-word semantics: space now means AND, not OR.**
  Adjacent terms with no explicit operator (e.g. `grga search "韩立 师傅"`)
  used to compile to `韩立 || 师傅` (union of matches). They now compile to
  `韩立 && 师傅` (both terms on the same line), matching the Google / ripgrep
  intuition and the existing skill docs. When implicit AND fires, grga prints
  a one-line notice to stderr:

  ```
  [grga] multi-term query treated as AND: "韩立 师傅" → "韩立" AND "师傅"
  ```

  Explicit `||` / `OR` and `&&` / `AND` are unchanged. Pass `--implicit-or`
  to restore the v1 space-is-OR behaviour, or `--implicit-and` to force the
  new default and suppress the notice. Python callers can pass
  `search_directory(..., implicit_operator="or")` for the same effect.

  Deep-search subprocess invocations of `grga search` continue to use
  `--implicit-or` internally so the deep-search planner's expressions keep
  their previous semantics.

### Added

- **`--max-total / -N` global result cap on `grga search`.** Defaults to
  `1000` (`0` disables). grga now streams rga's JSON output, and once the
  cap is hit it stops reading, kills the rga subprocess, and returns a
  partial result with `truncated: true` in the envelope. Layer 2 metadata
  (`occurrence_index` / `total_occurrences` / `line_ratio` / `nearby_hits`)
  is computed *after* truncation, so the counts reflect the returned slice
  rather than the untrimmed rga output.

  This closes the "22 MB single novel + two-word query returned 111 525 hits
  in 40 s" pathology that previously wedged caller agents against their
  default 60 s tool timeout.

  Deep-search runs its own generous cap (`DEEP_SEARCH_MAX_TOTAL = 20000` per
  iteration, exposed as the `max_total` parameter on
  `_run_grga_search`) because its planner needs many raw hits per query for
  keyword ranking. Set `max_total=None`/`0` on that helper to disable.

- **`--strict-cap` on `grga search`.** Instead of truncating, run a cheap
  `rga --count-matches` pre-check and return
  `{"ok": false, "error": "too_many_matches", "total_hits": N, "message": …}`
  when the cap is exceeded. Useful when callers would rather fail loudly
  than reason about partial data.

- `search_directory(...)` gains `max_total`, `strict_cap`, and
  `implicit_operator` keyword arguments. `build_rga_command_preview(...)`
  gains `implicit_operator`.

- `grga search --json` output now always includes a `truncated: bool` field
  and, when adjacency was treated as implicit AND, an `implicit_notice`
  string. See `docs/api.md`.

### Fixed

- Very large rga result sets no longer force grga to buffer the entire
  stdout (24 MB in the pathology case) before parsing.
