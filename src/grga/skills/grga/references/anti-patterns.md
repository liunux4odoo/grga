# Caller anti-patterns

Referenced from "Caller anti-patterns" in `SKILL.md`. The SKILL.md body
keeps the meta-rule and a one-line index of the four traps; this file
holds the full case-by-case explanation, symptoms, and legitimate
exceptions.

The first trap is a *query-design* issue; traps 2-4 are all variants of
one *tool-substitution* pattern — dropping out of grga into a low-level
primitive (`grep`, `sqlite3`, `cp`, `find`, …) mid-workflow.

**Meta-rule for the tool-substitution family** (2-4 below): when tempted
to drop into a lower-level tool, name the specific grga verb being
bypassed and why it is genuinely insufficient. If no such verb can be
named, the urge is muscle-memory, not necessity. Dropping out silently
gives up the skill's stability guarantees, position metadata, and
partition semantics.

## 1. Do not pre-disambiguate identity questions

When the question is *"are A and B the same entity?"* / *"is X actually
Y?"*, do not design the search around that binary. Feeding a hypothesis
into the query re-injects the ambiguity being resolved — every match
must then be interpreted through the hypothesis, and evidence starts
getting filtered to fit it.

Instead, run **two independent evidence chains** — one for A, one for B
— through `probe → search → expand` on their own terms (first
appearance, characteristic contexts, endings). Let the evidence meet on
its own. In domains where identity is intrinsically fuzzy (fiction with
alter-egos, reincarnation, dubbed aliases; near-homonym symbols across a
large codebase; user IDs vs. session IDs in log data), the "same or not
same" answer is often **emergent**, not decidable up front.

**Trap signal**: catching oneself writing a search plan that reads *"if
we find X then A=B, otherwise A≠B"*. Split it into two independent
explorations.

## 2. Do not fall back to `grep` mid-workflow

Inside a grga session, stay in grga. Reaching for `grep -n | head` "just
to look at raw text" silently collapses the reflection loop:

- `grga search -B 0 -A 0 --max-total N --json` is the direct equivalent
  of `grep -n | head -N`, works on PDF / EPUB / Office where `grep`
  fails, and preserves `occurrence_index` / `line_ratio` / `nearby_hits`
  / `heading_path` — precisely the signals a multi-round loop needs.
- To read text around a hit, use `grga expand --level lines --size N`
  (or `paragraph` / `chapter`).

Rule of thumb: **`grep` for a one-shot recon known not to iterate.
`grga` for anything that will be refined, expanded, or navigated.**

## 3. Do not open the shared DB with `sqlite3`

`~/.config/grga/deep_keywords.db` is grga's **internal storage**, not a
caller-facing interface. Reaching for `sqlite3 <db>` — even for a
harmless-looking `.tables` or a peek at the co-occurrence rows — couples
the workflow to internal storage that is free to change between grga
releases (columns renamed, tables split, indices replaced).

Two failure modes hidden by this habit:

- **Wrong verb was chosen.** If the task does not use `related`, the
  co-occurrence graph is irrelevant; peeking at the DB is pure ceremony.
  Ask *"does this task actually need the co-occurrence graph?"* first.
  If no, the DB never needed to be inspected at all.
- **Right question, wrong tool.** For a keyword's scope distribution,
  top co-occurring keywords, or polysemy signal, the stable public verb
  is `grga graph inspect <keyword> --db <path> --json`. Its output is
  contract; the underlying storage is not. Scope names (for `related
  --scope`) come from `grga scope list --json` — never from the DB.

Legitimate exceptions (few and explicit): actively developing grga
itself, or debugging a suspected corruption / migration bug after `grga
graph inspect` has already returned inconsistent output. General "let me
just peek at what's in there" curiosity does not qualify.

## 4. Do not copy source data to a "clean" path

`grga -d <dir>` accepts any directory. Non-ASCII paths, full-width
punctuation, spaces, colons in filenames — quote the argument once
(`-d "/path/with 全角标点：foo.txt"`) and grga (via `rga`) handles them
fine. Copying a source file into `/tmp/clean/` "to make life easier"
looks harmless but has three real costs:

- **Disk redundancy** — two copies of the same content to maintain.
- **Silent staleness** — the copy diverges when the original is edited,
  with no warning.
- **Broken scope bindings for the shared graph.** The knowledge graph
  is keyed by a **logical scope name**, and the physical path lives in
  a machine-local scope→directory binding (queryable via
  `grga scope list`). If the user's offline index was built against
  the real path, `related` candidates resolve their `scopes[]` back to
  that binding — a `/tmp` copy is not bound to any scope, so
  `--mode heading` and doc-locating ripgrep miss it. If the user later
  re-indexes the `/tmp` copy, the evidence layer will point at a path
  that vanishes on the next `/tmp` sweep, silently staling everything.

If a directory contains noise that must be excluded, do it at query
time: `-g '*needle*'` (glob filter passed through to rga), `--type
<lang>`, or a narrower `-d` subpath. Do not copy.

Legitimate exceptions: source is on read-only or network-latency media
that genuinely slows queries, or a frozen snapshot is needed for
reproducibility that survives edits to the original. Both should be
named explicitly, not defaulted to.
