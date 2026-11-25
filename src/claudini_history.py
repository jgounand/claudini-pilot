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


WIDTH, HEIGHT = 880, 210
LEFT, RIGHT, TOP, BOTTOM = 46, 96, 12, 30    # margins; RIGHT holds the end labels


def clock(at, span):
    """A time label: the date only matters once the window crosses a day."""
    moment = dt.datetime.fromtimestamp(at)
    return moment.strftime("%H:%M" if span < 86400 else "%d/%m %H:%M")


def chart(samples, kind, colours):
    """One SVG line chart: an account per line, time across, percent up."""
    first, last = samples[0]["at"], samples[-1]["at"]
    reach = max(1, last - first)
    plot = WIDTH - LEFT - RIGHT
    x = lambda at: LEFT + (at - first) / reach * plot
    y = lambda pct: TOP + (100 - pct) / 100 * (HEIGHT - TOP - BOTTOM)

    parts = ['<svg viewBox="0 0 %d %d" role="img">' % (WIDTH, HEIGHT)]
    for pct in (0, 50, 100):
        parts.append('<line class="grid" x1="%d" x2="%.1f" y1="%.1f" y2="%.1f"/>'
                     % (LEFT, LEFT + plot, y(pct), y(pct)))
        parts.append('<text class="tick" x="%d" y="%.1f">%d%%</text>'
                     % (LEFT - 8, y(pct) + 4, pct))

    # When it happened. Without this the chart says how full each account got
    # but never when, which is most of what a history is for.
    for at in (first, first + reach // 2, last):
        parts.append('<text class="time" x="%.1f" y="%d">%s</text>'
                     % (x(at), HEIGHT - 10, clock(at, reach)))

    ends = []
    for name, shade in colours.items():
        # An account's own reading time when the sample carries one.
        points = [(x(s["usage"][name].get("at") or s["at"]), y(s["usage"][name][kind]))
                  for s in samples if kind in s["usage"].get(name, {})]
        if not points:
            continue
        if len(points) > 1:
            parts.append('<polyline stroke="%s" points="%s"/>'
                         % (shade, " ".join("%.1f,%.1f" % p for p in points)))
        # A dot per sample, so a account with one or two readings is visible
        # at all rather than being an invisible zero-length line.
        if len(points) <= 12:
            parts += ['<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (px, py, shade)
                      for px, py in points]
        ends.append([points[-1][1], name, shade])

    # Name each line where it ends, nudged apart so labels never sit on top of
    # one another — five accounts at 100% would otherwise overprint.
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 12)
    for at_y, name, shade in ends:
        parts.append('<text class="end" x="%.1f" y="%.1f" fill="%s">%s</text>'
                     % (LEFT + plot + 8, at_y + 4, shade, name))

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

    moves = "".join(
        "<tr><td>%s</td><td>%s → <b>%s</b></td><td>%s</td></tr>"
        % (dt.datetime.fromtimestamp(e["at"]).strftime("%d %b %H:%M"),
           e["switch"]["from"], e["switch"]["to"], e["switch"].get("why", ""))
        for e in reversed(switches))

    body = """
      <p class="meta">%d samples over %s · %d accounts</p>
      <h2>Five-hour window</h2>%s
      <h2>Weekly window</h2>%s
      <h2>Switches</h2>%s
    """ % (len(samples),
           cu.until(samples[-1]["at"] - samples[0]["at"]),
           len(colours),
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
 svg { width:100%; height:auto; overflow:visible }
 polyline { fill:none; stroke-width:1.8; stroke-linejoin:round; stroke-linecap:round }
 .grid { stroke:var(--line); stroke-width:1 }
 .tick { fill:var(--dim); font-size:10px; text-anchor:end }
 .time { fill:var(--dim); font-size:10px; text-anchor:middle }
 .end  { font-size:11px; font-weight:500 }
 circle { opacity:.9 }
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


# --- the dashboard's figures --------------------------------------------------
#
# The app draws the dashboard natively (Swift Charts); what it shows is worked
# out here, where the history and the plans are understood, and handed over as
# JSON by `claudini-usage --dashboard-json`. Units follow the panel: a weekly
# allowance is 100 points, and "accounts' worth" is points / 100.

# What each plan costs a month, in USD, for the "paid but unused" figures. List
# prices as invoiced to the author's team; a standard team seat is the cheap one.
PLAN_PRICES = {"max_20x": 200, "max_5x": 100, "pro": 20}
TEAM_PRICES = {"standard": 25, "": 125}

WEEK = 7 * 86400
BURN_WINDOW = 72 * 3600  # the pace is measured over the last three days


def monthly_price(row):
    """What this account's plan costs a month, or None when it is not known."""
    tier = (row.get("tier") or "").lower()
    if "raven" in tier:
        seat = cu.seat_kind(row)
        return TEAM_PRICES.get("standard" if seat == "standard" else "")
    return next((price for key, price in PLAN_PRICES.items() if key in tier), None)


def _readings(samples, accounts):
    """(at, name, weekly) for every weekly reading of the given accounts, in order."""
    for s in samples:
        for name, point in s["usage"].items():
            if name in accounts and "weekly_all" in point:
                yield point.get("at") or s["at"], name, point["weekly_all"]


def capacity_series(samples, accounts, bucket=3600):
    """The weekly allowance left across the accounts, one point per bucket.

    An account not read in a sample keeps its last reading: samples only carry
    the accounts that were fresh at the time.
    """
    last, points = {}, []
    for s in samples:
        for name, point in s["usage"].items():
            if name in accounts and "weekly_all" in point:
                last[name] = point["weekly_all"]
        at = s["at"] // bucket * bucket
        reserve = round(sum(100 - v for v in last.values()) / 100, 2)
        if points and points[-1]["at"] == at:
            points[-1]["reserve"] = reserve
        else:
            points.append({"at": at, "reserve": reserve})
    return points


def burn_per_hour(samples, accounts, now, window=BURN_WINDOW):
    """Weekly points spent per hour across the accounts, over the window.

    Counted the way the daily summary counts (cu.roll_up): only what a week
    gains above its highest point, so a reset or a stray reading is not use.
    """
    recent = [s for s in samples if s["at"] >= now - window]
    if not recent:
        return 0.0
    scratch = cu.empty_rollup()
    for at, name, weekly in _readings(recent, accounts):
        cu.roll_up(scratch, at, name, weekly)
    spent = sum(cell["used"] for cells in scratch["days"].values() for cell in cells.values())
    return spent / max(1.0, (now - recent[0]["at"]) / 3600)


def forecast(accounts, burn, now, line, horizon=WEEK):
    """The reserve over the coming week if the pace holds, and when it runs out.

    `accounts` is [(weekly percent, weekly reset epoch)]. Each hour the pace is
    spent on the accounts whose week resets soonest, up to the line — the way
    endurance mode spends — and an account's week starts again at its reset.
    Runs out at the first hour the pace cannot be met under the line.
    """
    state = [{"pct": pct, "reset": reset} for pct, reset in accounts]
    points, run_out = [], None
    for hour in range(horizon // 3600 + 1):
        at = now + hour * 3600
        for a in state:
            while a["reset"] and at >= a["reset"]:
                a["pct"], a["reset"] = 0, a["reset"] + WEEK
        points.append({"at": at, "reserve": round(sum(100 - a["pct"] for a in state) / 100, 2)})
        need = burn
        for a in sorted(state, key=lambda a: a["reset"] or float("inf")):
            take = min(max(0, line - a["pct"]), need)
            a["pct"] += take
            need -= take
            if need <= 0:
                break
        if need > 1e-6 and run_out is None:
            run_out = at
    return points, run_out


def weekly_cycles(rollup, name):
    """The finished weeks of one account, and the current week's peak so far."""
    done = [{"end_epoch": c["end"], "used_pct": c["used_pct"]}
            for c in rollup["cycles"] if c["name"] == name]
    current = rollup["accounts"].get(name, {}).get("peak")
    return done, current


def week_of(day):
    """The Monday that starts the week of a "YYYY-MM-DD" day."""
    date = dt.date.fromisoformat(day)
    return (date - dt.timedelta(days=date.weekday())).isoformat()


def week_by_week(rollup, price_of, today, count=12):
    """Calendar weeks from the daily summary, oldest first.

    `used` and `capacity` are in accounts' worth: an account read on all seven
    days of a week brings one week of allowance, so capacity follows the
    accounts as they come and go. Unused value is what the part left unused of
    each account's week cost, at its monthly price spread over 52/12 weeks.
    """
    weeks = {}
    for day, cells in rollup["days"].items():
        bucket = weeks.setdefault(week_of(day), {})
        for name, cell in cells.items():
            account = bucket.setdefault(name, {"used": 0, "days": 0})
            account["used"] += cell["used"]
            account["days"] += 1
    current = week_of(today)
    out = []
    for start in sorted(weeks)[-count:]:
        accounts = weeks[start]
        unused = 0.0
        for name, account in accounts.items():
            price = price_of(name)
            if price is not None:
                unused += price * 12 / 52 * max(0.0, account["days"] / 7 - account["used"] / 100)
        out.append({"start": start, "partial": start == current,
                    "used": round(sum(a["used"] for a in accounts.values()) / 100, 2),
                    "capacity": round(sum(a["days"] for a in accounts.values()) / 7, 2),
                    "accounts": {name: a["used"] for name, a in sorted(accounts.items())},
                    "unused_value": round(unused, 2)})
    return out


def dashboard(rows, state, entries, now=None, rollup=None):
    """Everything the dashboard window draws."""
    now = int(now or dt.datetime.now().timestamp())
    samples = [e for e in entries if "usage" in e]
    rollup = rollup if rollup is not None else cu.load_rollup()
    rotation = [r for r in cu.in_rotation(rows, state) if r["status"] == cu.OK and r["limits"]]
    names = {r["name"] for r in rotation}
    line = state["max_usage"]

    def window(row, kind):
        found = next((l for l in row["limits"] if l["kind"] == kind), None)
        return (found["percent"], cu.epoch_of(found["resets_at"])) if found else (None, None)

    burn = burn_per_hour(samples, names, now)
    ahead, run_out = forecast([window(r, "weekly_all") for r in rotation if window(r, "weekly_all")[0] is not None],
                              burn, now, line)

    seats, paid, unused = [], 0.0, 0.0
    for row in rows:
        if row["status"] != cu.OK or not row["limits"]:
            continue
        done, current = weekly_cycles(rollup, row["name"])
        recent = done[-4:]
        average = sum(c["used_pct"] for c in recent) / len(recent) if recent else None
        price = monthly_price(row)
        idle = price * (1 - average / 100) if price is not None and average is not None else None
        if price is not None:
            paid += price
            unused += idle or 0
        seats.append({"name": row["name"], "plan": cu.plan_label(row), "monthly_price": price,
                      "off": cu.is_off(row, state), "cycles": recent,
                      "current_used_pct": current if current is not None else window(row, "weekly_all")[0],
                      "avg_used_pct": round(average, 1) if average is not None else None,
                      "unused_value_month": round(idle, 2) if idle is not None else None})
    seats.sort(key=lambda s: (s["avg_used_pct"] is None, s["avg_used_pct"] or 0))

    resets = []
    for row in rotation:
        weekly, weekly_at = window(row, "weekly_all")
        session, session_at = window(row, "session")
        resets.append({"name": row["name"], "plan": cu.plan_label(row),
                       "weekly_pct": weekly, "weekly_reset_epoch": weekly_at,
                       "session_pct": session, "session_reset_epoch": session_at})
    resets.sort(key=lambda r: r["weekly_reset_epoch"] or float("inf"))

    by_name = {r["name"]: r for r in rows}

    def price_of(name):
        row = by_name.get(name) or rollup["accounts"].get(name) or {}
        return monthly_price(row)

    today = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    return {
        "generated_at": now,
        "weeks": week_by_week(rollup, price_of, today),
        "span": {"from": samples[0]["at"] if samples else None,
                 "to": samples[-1]["at"] if samples else None, "samples": len(samples)},
        "capacity": {"points": capacity_series(samples, names), "forecast": ahead,
                     "burn_per_day": round(burn * 24 / 100, 2), "run_out_epoch": run_out,
                     "line": line},
        "resets": resets,
        "seats": seats,
        "totals": {"monthly_price": round(paid, 2), "unused_value_month": round(unused, 2),
                   "measured": sum(1 for s in seats if s["avg_used_pct"] is not None)},
    }
