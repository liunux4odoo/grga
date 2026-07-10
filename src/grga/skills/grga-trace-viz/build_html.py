"""Assemble template.html + inline SVG (3 fragments) + inline JSON into one HTML.

Usage:
  python3 build_html.py <input-session.json> [output.html]

Default output path: trace_viz.html next to the input JSON.

The input JSON must conform to grga schemas/trace-viz-session-v1.json.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(session_path: Path, out_path: Path):
    from svg_gen import render_all

    with session_path.open(encoding="utf-8") as f:
        session = json.load(f)

    # basic shape check
    if session.get("session_version") != "1.0":
        raise SystemExit(
            f"Unsupported session_version: {session.get('session_version')!r}. "
            f"This tool understands 1.0. See schemas/trace-viz-session-v1.json."
        )

    tmpl = (HERE / "template.html").read_text(encoding="utf-8")
    svgs = render_all(session)

    # data injected into the runtime JS
    steps_by_id = {str(s["step"]): s for s in session["steps"]}
    dims = {
        "canvas_w": svgs["canvas_w"], "canvas_h": svgs["canvas_h"],
        "header_h": svgs["header_h"], "yaxis_w":  svgs["yaxis_w"],
    }
    title    = session.get("title", "grga trace visualization")
    subtitle = session.get("subtitle", "")
    summary  = session.get("summary", {}) or {}
    stats_html         = summary.get("stats_html", "")
    path_features_html = summary.get("path_features_html", "")

    html = tmpl
    html = html.replace("<!-- SVG_HEADER -->", svgs["header"])
    html = html.replace("<!-- SVG_YAXIS -->",  svgs["yaxis"])
    html = html.replace("<!-- SVG_MAIN -->",   svgs["main"])
    html = html.replace("/* DATA_INJECT */ {}",    json.dumps(steps_by_id,             ensure_ascii=False))
    html = html.replace("/* KINDS_INJECT */ {}",   json.dumps(session["kind_labels"],  ensure_ascii=False))
    html = html.replace("/* FAMS_INJECT */ {}",    json.dumps(session["family_colors"],ensure_ascii=False))
    html = html.replace("/* MARKERS_INJECT */ {}", json.dumps(session["kind_markers"], ensure_ascii=False))
    html = html.replace("/* DIMS_INJECT */ {}",    json.dumps(dims))
    html = html.replace("<!-- TITLE -->",    title)
    html = html.replace("<!-- SUBTITLE -->", subtitle)
    html = html.replace("<!-- STATS -->",         stats_html)
    html = html.replace("<!-- PATH_FEATURES -->", path_features_html)

    out_path.write_text(html, encoding="utf-8")
    size = out_path.stat().st_size
    print(f"✓ wrote {out_path} ({size:,} bytes)")


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(argv) >= 2 else 2)
    session_path = Path(argv[1]).resolve()
    if not session_path.is_file():
        raise SystemExit(f"session JSON not found: {session_path}")
    out_path = Path(argv[2]).resolve() if len(argv) >= 3 else session_path.parent / "trace_viz.html"
    build(session_path, out_path)


if __name__ == "__main__":
    main(sys.argv)
