"""SVG generator: emit three separate SVG fragments for pan/zoom + frozen axes.

Layout (vertical time axis):
  svg_header : family column labels + colored bar     (frozen on Y)
  svg_yaxis  : step number ticks                       (frozen on X)
  svg_main   : lane backgrounds + grid + edges + nodes (fully pannable/zoomable)

All three share the same LANE_W / STEP_H units so they align when drawn side by side.

Input: a session dict conforming to schemas/trace-viz-session-v1.json.
"""
import html

# shared layout parameters (canvas-unit pixels; screen size differs after zoom)
LANE_W = 92
STEP_H = 34
HEADER_H = 46
YAXIS_W = 46


def canvas_size(session):
    return LANE_W * len(session["family_order"]), STEP_H * len(session["steps"])


def render_header(session):
    canvas_w, _ = canvas_size(session)
    family_order  = session["family_order"]
    family_colors = session["family_colors"]
    p = [f'<svg id="svg-header" viewBox="0 0 {canvas_w} {HEADER_H}" '
         f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet">']
    p.append('<g id="header-canvas">')
    for li, fam in enumerate(family_order):
        x = li * LANE_W
        colour = family_colors[fam]
        bg = "#f7f9fb" if li % 2 == 0 else "#eef2f5"
        p.append(f'<rect x="{x}" y="0" width="{LANE_W}" height="{HEADER_H}" fill="{bg}"/>')
        p.append(f'<rect x="{x + 4}" y="{HEADER_H - 6}" width="{LANE_W - 8}" height="3" '
                 f'fill="{colour}" opacity="0.7"/>')
        weight = "700" if "detour" in fam else "600"
        label_len = sum(2 if ord(ch) > 127 else 1 for ch in fam)
        fs = 13 if label_len <= 10 else 12
        cx = x + LANE_W / 2
        p.append(f'<text x="{cx}" y="{HEADER_H - 16}" text-anchor="middle" font-size="{fs}" '
                 f'fill="#263238" font-weight="{weight}" font-family="sans-serif">'
                 f'{html.escape(fam)}</text>')
    p.append('</g>')
    p.append('</svg>')
    return "\n".join(p)


def render_yaxis(session):
    _, canvas_h = canvas_size(session)
    steps = session["steps"]
    p = [f'<svg id="svg-yaxis" viewBox="0 0 {YAXIS_W} {canvas_h}" '
         f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet">']
    p.append('<g id="yaxis-canvas">')
    p.append(f'<line x1="{YAXIS_W - 1}" y1="0" x2="{YAXIS_W - 1}" y2="{canvas_h}" '
             f'stroke="#cfd8dc" stroke-width="1"/>')
    for s in steps:
        y = (s["step"] - 1) * STEP_H + STEP_H / 2
        p.append(f'<text x="{YAXIS_W - 8}" y="{y + 4}" text-anchor="end" font-size="12" '
                 f'fill="#607d8b" font-weight="600" font-family="sans-serif">{s["step"]}</text>')
        p.append(f'<line x1="{YAXIS_W - 4}" y1="{y}" x2="{YAXIS_W - 1}" y2="{y}" '
                 f'stroke="#cfd8dc" stroke-width="1"/>')
    p.append('</g>')
    p.append('</svg>')
    return "\n".join(p)


def render_main(session):
    canvas_w, canvas_h = canvas_size(session)
    family_order  = session["family_order"]
    family_colors = session["family_colors"]
    kind_markers  = session["kind_markers"]
    steps = session["steps"]
    lane_x = {fam: i for i, fam in enumerate(family_order)}
    p = [f'<svg id="svg-main" viewBox="0 0 {canvas_w} {canvas_h}" '
         f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet">']
    p.append('<g id="main-canvas">')

    for fam, li in lane_x.items():
        x = li * LANE_W
        bg = "#fafbfc" if li % 2 == 0 else "#f2f4f6"
        p.append(f'<rect x="{x}" y="0" width="{LANE_W}" height="{canvas_h}" fill="{bg}"/>')

    for i in range(1, len(steps)):
        y = i * STEP_H
        p.append(f'<line x1="0" y1="{y}" x2="{canvas_w}" y2="{y}" '
                 f'stroke="#eceff1" stroke-width="1"/>')

    for a, b in zip(steps, steps[1:]):
        x1 = lane_x[a["family"]] * LANE_W + LANE_W / 2
        y1 = (a["step"] - 1) * STEP_H + STEP_H / 2
        x2 = lane_x[b["family"]] * LANE_W + LANE_W / 2
        y2 = (b["step"] - 1) * STEP_H + STEP_H / 2
        if a["family"] != b["family"]:
            my = (y1 + y2) / 2
            p.append(f'<path d="M{x1},{y1} C{x1},{my} {x2},{my} {x2},{y2}" '
                     f'fill="none" stroke="#607d8b" stroke-width="1.6" '
                     f'stroke-dasharray="4,3" opacity="0.7"/>')
        else:
            p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="#90a4ae" stroke-width="1.6" opacity="0.7"/>')

    for s in steps:
        x = lane_x[s["family"]] * LANE_W + LANE_W / 2
        y = (s["step"] - 1) * STEP_H + STEP_H / 2
        colour = family_colors[s["family"]]
        is_gold = "🎯" in str(s.get("signal") or "")
        radius = 15 if s["kind"] == "synthesize" else 13 if is_gold else 11
        if s["kind"] == "antipattern":
            stroke, sw = "#111", 2
        elif is_gold:
            stroke, sw = "#ffc107", 3.2
        else:
            stroke, sw = "#fff", 1.8
        p.append(f'<circle class="node" data-step="{s["step"]}" cx="{x}" cy="{y}" r="{radius}" '
                 f'fill="{colour}" stroke="{stroke}" stroke-width="{sw}"/>')
        marker = kind_markers.get(s["kind"], "?")
        fsize = 13 if s["kind"] == "synthesize" else 12
        p.append(f'<text x="{x}" y="{y + 4.5}" text-anchor="middle" font-size="{fsize}" '
                 f'fill="white" font-weight="bold" font-family="sans-serif" '
                 f'pointer-events="none">{marker}</text>')
        sig = s.get("signal")
        if sig is not None and sig != "":
            sig_str = str(sig).replace("🎯 ", "")
            if len(sig_str) > 12:
                sig_str = sig_str[:11] + "…"
            p.append(f'<text x="{x + radius + 4}" y="{y + 4}" font-size="10.5" '
                     f'fill="#455a64" font-weight="500" font-family="sans-serif" '
                     f'pointer-events="none">{html.escape(sig_str)}</text>')

    p.append('</g>')
    p.append('</svg>')
    return "\n".join(p)


def render_all(session):
    """Return dict: header_svg / yaxis_svg / main_svg / canvas_w / canvas_h / header_h / yaxis_w."""
    cw, ch = canvas_size(session)
    return {
        "header": render_header(session),
        "yaxis":  render_yaxis(session),
        "main":   render_main(session),
        "canvas_w": cw,
        "canvas_h": ch,
        "header_h": HEADER_H,
        "yaxis_w":  YAXIS_W,
    }
