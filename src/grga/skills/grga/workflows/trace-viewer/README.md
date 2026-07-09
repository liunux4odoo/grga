# trace-viewer

**Visualise how a grga session expanded the keyword co-occurrence graph.**

This workflow takes a JSONL trace produced during a grga session and renders
a single self-contained HTML file showing the graph nodes activated, the
edges traversed, and how they accumulated across multi-turn calls.

## Who this is for

- **grga developers** inspecting `related --mode graph` behaviour on real
  queries (does PPR pick the right neighbours? does the fallback fire?)
- **End users** who ran a multi-turn search and want a "what did the agent
  actually look at" recap — friendlier than reading raw JSON logs

Not for real-time monitoring, not for production dashboards. If you need
that, wire up your own consumer against `graph_actions`.

## Prerequisites

- grga ≥ (whichever version ships trace-signals v1)
- Python 3.8+ **stdlib only** — the renderer has zero pip dependencies
- No network access needed. Cytoscape.js is vendored under `vendor/` and
  inlined into the output HTML.

## Three-step workflow

### 1. Enable trace collection in your grga session

Every grga call that emits `graph_actions` (currently `related --json` and
`suggest --json`) must have its output appended to a session-scoped JSONL
file. See `collect.md` for the exact append record shape and the caller-side
protocol.

Convention: `.grga/trace/<session-id>.jsonl`. `session-id` is any stable
string the caller picks (a UUID, a timestamp, a task name).

### 2. Run the renderer

```bash
python workflows/trace-viewer/render.py \
    .grga/trace/<session-id>.jsonl \
    -o trace.html
```

- Input: one `.jsonl` file (one JSON record per line, format in `collect.md`)
- Output: one `trace.html` file, self-contained (open with a double-click)
- Exit code 0 on success, non-zero on unreadable input or empty trace

### 3. Open `trace.html`

- Double-click, or serve any way you like — no server required, no CDN
  requests
- Graph area shows nodes/edges; time-slider on the top replays the session
  step by step; clicking a node reveals its `meta`

## What the HTML shows

- **Nodes**: keyword (`kw:*`), file (`file:*`), heading (`head:*`) — three
  visual styles
- **Edges**: `traverse_edge` actions, coloured by `edge_kind`; edges added
  in later steps come in later on the timeline
- **Score halos**: `score_node` results shown as node border thickness /
  opacity
- **LLM proposals**: `llm_propose` shown as a dashed edge from the origin
  query to the proposed keyword
- **Timeline**: horizontal slider on top; dragging left shows earlier state,
  right shows the accumulated final state. A step counter shows which grga
  call is currently highlighted.

## Files in this workflow

| File | Role |
|---|---|
| `README.md` | This file — when and how to use the workflow |
| `collect.md` | JSONL append record format and caller-side accumulation protocol |
| `IMPLEMENTATION.md` | Contract for `render.py` and `template.html` (what code agents implement) |
| `render.py` | The renderer — reads JSONL, emits self-contained HTML |
| `template.html` | HTML skeleton with placeholders; `render.py` fills them in |
| `vendor/cytoscape.min.js` | Vendored graph library (MIT, inlined at render time) |
| `example/sample.jsonl` | A demo trace file for smoke-testing the renderer |

## Guarantees

- **No network at render time** — everything vendored / inlined
- **No writes outside `-o`** — read-only on input, single-file output
- **stdlib-only** — safe to run in any Python 3.8+ environment
- **Deterministic** — same input JSONL → same output HTML (modulo the
  generation timestamp comment at the top)

## Non-goals

- No live/streaming update. If the JSONL grows while `render.py` is running,
  the renderer sees only what was on disk at read time.
- No editing UI. The HTML is a viewer, not an interactive graph editor.
- No cross-session comparison. Each render is one session; diffing is a
  future concern.
