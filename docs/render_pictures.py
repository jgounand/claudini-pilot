#!/usr/bin/env python3
"""Render the README's pictures from made-up accounts, never your own.

    python3 docs/render_pictures.py [/path/to/ClaudiniBar]

The panel comes from docs/sample-snapshot.json with its reset times moved to
just ahead of now (a saved snapshot's times are in the past by the time you
render it). The dashboard comes from a five-week history made up here, run
through the same code the app's dashboard uses.
"""

import datetime as dt
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

DOCS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(DOCS, os.pardir, "src")
APP = sys.argv[1] if len(sys.argv) > 1 else "/Applications/ClaudiniBar.app/Contents/MacOS/ClaudiniBar"


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shifted_snapshot(now):
    snap = json.load(open(os.path.join(DOCS, "sample-snapshot.json")))
    epochs = [l["resets_at_epoch"] for p in snap["profiles"] for l in p["limits"] if l["resets_at_epoch"]]
    shift = now + 2 * 3600 - min(epochs)
    for p in snap["profiles"]:
        for l in p["limits"]:
            if l["resets_at_epoch"]:
                l["resets_at_epoch"] += shift
        if p.get("binding") and p["binding"].get("resets_at_epoch"):
            p["binding"]["resets_at_epoch"] += shift
    snap["summary"] = {"usable": 3, "total": 5, "off": 1, "draining": False, "weekly_reserve": 203,
                       "next_free": {"name": "spare", "at_epoch": now + 26 * 3600}}
    snap["settings"] = {"max_usage": 95, "cooldown_min": 10, "drain": True, "preferred_model": "Fable"}
    return snap


def made_up_dashboard(now):
    """Five weeks of hourly readings for five accounts, each at its own pace."""
    history = load("claudini_history")
    cu = history.cu
    random.seed(7)
    accounts = {  # name: (tier, seat, points per active hour, reset offset in hours)
        "work": ("default_raven", "team_tier_1", 1.6, 30),
        "personal": ("default_claude_max_20x", None, 1.1, 80),
        "team-seat": ("default_raven", "team_standard", 0.5, 120),
        "spare": ("default_claude_max_5x", None, 1.9, 10),
        "side-project": ("default_claude_pro", None, 0.3, 150),
    }
    start = now - 35 * 86400
    entries, level = [], {name: 0.0 for name in accounts}
    for hour in range(35 * 24):
        at = start + hour * 3600
        working = 8 <= dt.datetime.fromtimestamp(at).hour < 20
        usage = {}
        for name, (_, _, pace, offset) in accounts.items():
            if (hour - offset) % 168 == 0:
                level[name] = 0.0
            if working:
                level[name] = min(100.0, level[name] + pace * random.uniform(0.3, 1.7))
            usage[name] = {"weekly_all": int(level[name]), "at": at}
        entries.append({"at": at, "usage": usage})
    rows = []
    for name, (tier, seat, _, offset) in accounts.items():
        hours_to_reset = 168 - ((35 * 24 - offset) % 168)
        resets = dt.datetime.fromtimestamp(now + hours_to_reset * 3600, dt.timezone.utc).isoformat()
        rows.append({"name": name, "email": name + "@example.com", "active": name == "work",
                     "status": cu.OK, "detail": None, "limit_reset": None, "tier": tier, "seat": seat,
                     "limits": [{"kind": "weekly_all", "label": "7d", "model": None,
                                 "percent": int(level[name]), "resets_at": resets}]})
    state = dict(cu.DEFAULT_STATE)
    return history.dashboard(rows, state, entries, now=now, rollup=cu.rebuild_rollup(entries))


def main():
    now = int(dt.datetime.now().timestamp())
    work = tempfile.mkdtemp(prefix="claudini-pictures-")
    try:
        snapshot = os.path.join(work, "snapshot.json")
        dashboard = os.path.join(work, "dashboard.json")
        json.dump(shifted_snapshot(now), open(snapshot, "w"))
        json.dump(made_up_dashboard(now), open(dashboard, "w"))
        subprocess.run([APP, "--menu-preview", work, snapshot], check=True, capture_output=True)
        subprocess.run([APP, "--dashboard-preview", work, dashboard], check=True, capture_output=True)
        shutil.copy(os.path.join(work, "menu.png"), os.path.join(DOCS, "menu.png"))
        shutil.copy(os.path.join(work, "dashboard-light.png"), os.path.join(DOCS, "dashboard.png"))
        print("docs/menu.png, docs/dashboard.png")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
