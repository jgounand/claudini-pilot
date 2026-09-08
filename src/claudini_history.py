#!/usr/bin/env python3
"""The history page: a local HTML file drawn from the sample log.

Kept out of the engine on purpose. That module is policy and I/O, and it
already draws its rendering boundary at for_json — the menu bar app is a
renderer on the far side of a named contract. A document generator with its
own palette, stylesheet and SVG plotter belongs on the same side as the other
renderers, not inside the thing they render.
"""

import datetime as dt
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "claudini_usage", os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                   "claudini_usage.py"))
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)

HISTORY_COLOURS = ["#4f9cf9", "#f2a541", "#4cc38a", "#e5534b", "#a371f7", "#3fb0b0"]


WIDTH, HEIGHT = 880, 190


def chart(samples, kind, colours):
    """One SVG line chart: an account per line, time across, percent up."""
    width, height = WIDTH, HEIGHT
    span = (samples[0]["at"], samples[-1]["at"])
    reach = max(1, span[1] - span[0])
    x = lambda at: 46 + (at - span[0]) / reach * (width - 60)
    y = lambda pct: 12 + (100 - pct) / 100 * (height - 40)

    parts = ['<svg viewBox="0 0 %d %d" role="img">' % (width, height)]
    for pct in (0, 50, 100):
        parts.append('<line class="grid" x1="46" x2="%d" y1="%.1f" y2="%.1f"/>'
                     % (width - 14, y(pct), y(pct)))
        parts.append('<text class="tick" x="38" y="%.1f">%d%%</text>' % (y(pct) + 4, pct))

    for name in colours:
        points = [(x(s["at"]), y(s["usage"][name][kind]))
                  for s in samples if name in s["usage"] and kind in s["usage"][name]]
        if len(points) > 1:
            parts.append('<polyline stroke="%s" points="%s"/>'
                         % (colours[name],
                            " ".join("%.1f,%.1f" % p for p in points)))
    parts.append("</svg>")
    return "".join(parts)


def write(path=None):
    """A self-contained page: how full each account has been, and when the
    policy moved between them."""
    entries = cu.read_history()
    samples = [e for e in entries if "usage" in e]
    path = path or os.path.join(cu.CLAUDINI_HOME, "history.html")
    if len(samples) < 2:
        _page(path, "<p class='empty'>Not enough history yet — samples are "
                          "taken as the tool reads your accounts. Come back later.</p>")
        return path

    accounts = sorted({name for s in samples for name in s["usage"]})
    colours = {name: HISTORY_COLOURS[i % len(HISTORY_COLOURS)]
               for i, name in enumerate(accounts)}
    switches = [e for e in entries if "switch" in e][-12:]

    legend = "".join('<span><i style="background:%s"></i>%s</span>' % (shade, name)
                     for name, shade in colours.items())
    moves = "".join(
        "<tr><td>%s</td><td>%s → <b>%s</b></td><td>%s</td></tr>"
        % (dt.datetime.fromtimestamp(e["at"]).strftime("%d %b %H:%M"),
           e["switch"]["from"], e["switch"]["to"], e["switch"].get("why", ""))
        for e in reversed(switches))

    body = """
      <p class="meta">%d samples over %s · %d accounts</p>
      <div class="legend">%s</div>
      <h2>Five-hour window</h2>%s
      <h2>Weekly window</h2>%s
      <h2>Switches</h2>%s
    """ % (len(samples),
           cu.until(samples[-1]["at"] - samples[0]["at"]) or "a moment",
           len(colours), legend,
           chart(samples, "session", colours),
           chart(samples, "weekly_all", colours),
           "<table>%s</table>" % moves if moves else "<p class='empty'>None yet.</p>")
    _page(path, body)
    return path


PAGE_HEAD = """<!doctype html><meta charset="utf-8"><title>claudini-pilot history</title>
<style>
 :root { color-scheme: light dark; --ink:#1a1a1a; --dim:#6b6b6b; --line:#d8d8d8; --bg:#fbfbfa }
 @media (prefers-color-scheme: dark) {
   :root { --ink:#e8e8e6; --dim:#9a9a97; --line:#333; --bg:#151514 } }
 body { margin:0; padding:32px; background:var(--bg); color:var(--ink);
        font:14px/1.5 -apple-system, system-ui, sans-serif; max-width:940px }
 h1 { font-size:19px; margin:0 0 4px } h2 { font-size:13px; font-weight:600;
      text-transform:uppercase; letter-spacing:.06em; color:var(--dim); margin:28px 0 8px }
 .meta, .empty { color:var(--dim) } .empty { padding:24px 0 }
 .legend { display:flex; flex-wrap:wrap; gap:14px; margin:14px 0 }
 .legend span { display:flex; align-items:center; gap:6px; font-size:12px }
 .legend i { width:11px; height:3px; border-radius:2px }
 svg { width:100%; height:auto; overflow:visible }
 polyline { fill:none; stroke-width:1.8; stroke-linejoin:round; stroke-linecap:round }
 .grid { stroke:var(--line); stroke-width:1 }
 .tick { fill:var(--dim); font-size:10px; text-anchor:end }
 table { border-collapse:collapse; font-size:13px; width:100% }
 td { padding:6px 10px 6px 0; border-bottom:1px solid var(--line); vertical-align:top }
 td:first-child { color:var(--dim); white-space:nowrap }
</style>
<h1>claudini-pilot</h1>"""


def _page(path, body):
    """The stylesheet is a plain constant, so its percentages need no escaping."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(PAGE_HEAD + body)
