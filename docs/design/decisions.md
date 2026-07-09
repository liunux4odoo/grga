# Design decisions log

Records deviations and explicit decisions made outside the main plan
documents. Each entry names the plan step it relates to, the decision,
and the rationale.

## 2026-07-09 · `--prefer-system` CLI coverage

**Plan step**: `PLAN-0.2.0-followup.md` Task 3 lists `search / deep-search
/ probe / expand / related / suggest / tools install` for the new
`--prefer-system` flag.

**Decision**: Added `--prefer-system` to the commands that actually
resolve external tools:

* `grga search`
* `grga deep-search`
* `grga probe`
* `grga tools check`

Skipped `expand`, `related`, `suggest` (they operate on the SQLite graph
DB / local files / LLM only — no `rg` / `rga` invocation, so the flag
would be a no-op that lies about behaviour). Skipped `tools install` /
`tools update` because those commands unconditionally target the managed
cache; a system-preference has no meaning there.

**Rationale**: A CLI option that silently does nothing on some commands
is worse UX than a targeted flag that only appears where it matters. The
underlying `runtime_env.prefer_system_tools()` + `GRGA_PREFER_SYSTEM=1`
still provide a global escape hatch for anyone who wants to force system
resolution across every command (including the graph/LLM subcommands, in
case future refactors ever route tools through them).

**Impact**: Documented; tests exercise the flag on `grga search` and
`grga tools check` and additionally verify the env-only path.

## 2026-07-09 · Skill install exit-code contract

**Plan step**: `PLAN-0.2.0-followup.md` Task 1 asked whether the JSON
mode should emit a second `emit_error` payload when a skill already
exists.

**Decision**: JSON mode stays with a single top-level `reports` array
carrying the per-skill `status` (`installed` / `exists`). The process
exit code (`3` for any `exists` without `--force`, `0` otherwise) is the
only success/collision discriminator; the JSON payload is not duplicated
by `emit_error`.

**Rationale**: `reports` is already structured and richer than a flat
error payload — agents can inspect which specific skill collided and
which succeeded. Duplicating that as an error object would force
consumers to reconcile two shapes.

**Impact**: Documented at the docstring of
`cli.skill_install_command`; covered by
`tests/test_skill_install_cli.py`.
