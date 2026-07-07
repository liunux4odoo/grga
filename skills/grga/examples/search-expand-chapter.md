# Pattern: search → expand chapter

**When to use.** The user asks "what does the docs say about X?" and needs
the **surrounding chapter**, not just the matching line. Works best on a
Markdown corpus; degrades cleanly to `paragraph` on non-Markdown files.

## Storyboard

1. User: *"How is grga installed on macOS?"*
2. Agent runs `grga search` for the keyword.
3. Agent picks the first hit inside a Markdown file (`heading_path` present).
4. Agent expands that hit to `--level chapter` and quotes the block back.

## Commands

```bash
# 1) Find the hit
grga search "安装" \
    --directory tests/fixtures/docs \
    --json > /tmp/hits.json

# 2) Read the top Markdown hit
jq '.matches
    | map(select(.is_context == false and (.heading_path // null) != null))
    | .[0] | {path, line_number, heading_path}' /tmp/hits.json
# -> {"path":"./install.md","line_number":1,"heading_path":["安装指南"]}

# 3) Expand to the enclosing chapter
grga expand "install.md:5" \
    --directory tests/fixtures/docs \
    --level chapter \
    --json
```

## Expected shape of the expand output

```json
{
  "hit": {"path": "install.md", "line_number": 5, "text": "", "…": "…"},
  "text": "# 安装指南\n\n本文档介绍如何安装本项目。\n\n## macOS\n\n在 macOS 上使用 brew 安装：\n\n```bash\nbrew install grga\n```\n…",
  "level": "chapter",
  "range": [1, 31],
  "truncated": false
}
```

Notice `level: "chapter"` — the actually-achieved level. If the caller had
passed a non-Markdown file, the same call would return
`level: "paragraph"` (or `"lines"` if there was no paragraph), and the
caller code should branch on `level` rather than assume the request was
honoured.

## Caller pseudocode

```python
hits = json.loads(run("grga search '安装' -d docs --json"))["matches"]
md_hit = next((h for h in hits
               if not h["is_context"] and h.get("heading_path")), None)
if md_hit is None:
    # No structured hit: fall back to paragraph on the very first hit
    md_hit = hits[0]
    level = "paragraph"
else:
    level = "chapter"

ctx = json.loads(run(
    f"grga expand '{md_hit['path']}:{md_hit['line_number']}' "
    f"-d docs --level {level} --json"))

quote = ctx["text"]
achieved = ctx["level"]   # may differ from `level`!
```

## When *not* to use this pattern

- The user only wants a **one-line answer** — quoting `matches[i].text`
  directly is enough; skipping `expand` saves an I/O round trip.
- The user wants **multiple related chapters** — use
  `search → related --mode heading` instead (see the sibling example).
