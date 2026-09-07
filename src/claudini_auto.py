#!/usr/bin/env python3
"""
claudini-auto — a console for steering your Claude accounts.

Shows what is left on every subscription, switches with a single keypress, and
hosts the auto-switch toggle: stay on an account that still has the preferred
model, fall back to the freshest account overall once that model is spent.

Keys: a auto · r refresh · 1-9 switch or reconnect · q quit
"""

import curses
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import claudini_usage as cu  # noqa: E402

COLORS = {"ok": 1, "warning": 2, "critical": 3}
# getch gives up this often; the frame is only actually repainted when
# something changed, so a short wait costs a wakeup and no work.
POLL_MS = 2000

# Table layout: (gap before, width, heading). Positions are derived, so a
# column cannot be widened without moving the ones after it.
COLUMNS = [
    (5, 15, "profile"),
    (1, 22, "account"),
    (1, 12, "workspace"),
    (1, 9, "plan"),
    (2, 7, "session"),
    (1, 7, "week"),
    (1, 6, cu.PREFERRED_MODEL),
    (1, 7, "reset"),
    (2, 1, "⟲"),
]


def _layout():
    x, columns = 0, {}
    for gap, width, name in COLUMNS:
        x += gap
        columns[name] = (x, width)
        x += width
    return columns


LAYOUT = _layout()
COL = {name: x for name, (x, _) in LAYOUT.items()}
WIDTH = {name: w for name, (_, w) in LAYOUT.items()}
COL_LIMITS = {"session": COL["session"], "weekly_all": COL["week"]}
FOOTER = (" a auto · m mode · r refresh · 1-9 switch or reconnect · q quit"
          "   ⟲ = /limit-reset ")


def header_line():
    line = "  #"
    for name, (x, _) in LAYOUT.items():
        line = line.ljust(x) + name
    return line


HEADER = header_line()


class Console:
    def __init__(self):
        self.rows = []
        self.state = cu.load_state()
        self.plan = (None, "", None)
        self.message = "loading…"
        self.busy = False
        self.version = 0          # bumped by refresh so the drawing loop notices
        self.painted = None
        self.lock = threading.Lock()

    # --- data ----------------------------------------------------------------

    def refresh(self, run_auto=True, force=False):
        with self.lock:
            if self.busy:
                return          # a pass is already in flight; don't stack them
            self.busy = True
        state = cu.load_state()
        rows = cu.collect(force)
        message = ""
        plan, message = None, ""
        if run_auto:
            rows, moved, message, plan = cu.auto_tick(rows)
            if moved:
                message = "auto-switched: " + message
        throttled = cu.throttled_for()
        # Worked out once here, not on every frame.
        plan = plan or cu.plan_switch(rows, state)
        with self.lock:
            self.rows, self.state, self.plan = rows, state, plan
            self.message = (cu.throttle_notice(throttled) if throttled
                            else message or time.strftime("updated at %H:%M:%S"))
            self.busy = False
            self.version += 1

    def spawn(self, run_auto=True, force=False):
        threading.Thread(target=self.refresh, args=(run_auto, force), daemon=True).start()

    def loop(self):
        """A true POLL_SEC period: letting the pass duration add to the sleep
        makes the phase drift against the menu bar's fixed timer, and the two
        eventually land in the window where both miss the cache."""
        while True:
            started = time.monotonic()
            self.refresh()
            time.sleep(max(0, cu.POLL_SEC - (time.monotonic() - started)))

    # --- drawing -------------------------------------------------------------

    @staticmethod
    def clip(text, width):
        """Truncate visibly: an email cut short looks like a different email."""
        text = text or ""
        return text if len(text) <= width else text[:width - 1] + "…"

    @staticmethod
    def put(scr, y, x, text, attr=0):
        """addstr that stays quiet instead of raising on a narrow terminal."""
        h, w = scr.getmaxyx()
        if y >= h or x >= w:
            return
        try:
            scr.addstr(y, x, text[:w - x - 1], attr)
        except curses.error:
            pass

    def draw(self, scr, force=False):
        """Repaint only when something a viewer could see has changed.

        Countdowns move by the minute, so between refreshes almost every frame
        would be byte-identical.
        """
        with self.lock:
            stamp = (self.version, self.busy, self.message, int(time.time() // 30))
        if not force and stamp == self.painted:
            return
        self.painted = stamp

        scr.erase()
        h, _ = scr.getmaxyx()
        with self.lock:
            rows, state, (target, why, blocked) = self.rows, self.state, self.plan
            message, busy = self.message, self.busy

        active = cu.active_row(rows)
        auto_on = state["enabled"]
        self.put(scr, 0, 0, " claudini ", curses.A_REVERSE | curses.A_BOLD)
        self.put(scr, 0, 11, "using: " + (active["name"] if active else "?"), curses.A_BOLD)
        self.put(scr, 0, 30, "auto: " + ("ON" if auto_on else "off"),
                 curses.color_pair(1) | curses.A_BOLD if auto_on else curses.A_DIM)
        self.put(scr, 0, 42, "mode: " + state["mode"], curses.A_BOLD)
        if target:
            same = cu.staying(rows, (target, why, blocked))
            note = "next: %s — %s" % (target["name"], blocked or why)
            self.put(scr, 0, 62, note, curses.A_DIM if same else curses.color_pair(2))

        self.put(scr, 2, 0, HEADER, curses.A_DIM)
        y = 3
        for i, p in enumerate(rows, 1):
            if y >= h - 3:
                break
            self.draw_row(scr, y, i, p)
            y += 1

        self.put(scr, h - 2, 0, ("⏳ " if busy else "   ") + message, curses.A_DIM)
        self.put(scr, h - 1, 0, FOOTER, curses.A_REVERSE)
        scr.refresh()

    def draw_row(self, scr, y, index, p):
        bold = curses.A_BOLD if p["active"] else 0
        self.put(scr, y, 0, " %s%d " % ("●" if p["active"] else " ", index), bold)
        self.put(scr, y, COL["profile"], self.clip(p["name"], WIDTH["profile"]), bold)
        self.put(scr, y, COL["account"], self.clip(p["email"] or "?", WIDTH["account"]),
                 curses.A_DIM)
        # Two profiles can share one email in different workspaces: that column
        # and the plan are what tell them apart.
        self.put(scr, y, COL["workspace"], self.clip(p.get("space"), WIDTH["workspace"]),
                 curses.A_DIM if p.get("space") == "personal" else curses.A_BOLD)
        self.put(scr, y, COL["plan"], self.clip(cu.plan_label(p), WIDTH["plan"]), curses.A_DIM)

        if p["status"] != cu.OK:
            note = (p.get("detail") or p["status"]) + (" (cached)" if p.get("cached") else "")
            self.put(scr, y, COL["session"], note, curses.color_pair(3))
            return

        preferred = cu.preferred_limit(p)
        for limit in p["limits"]:
            column = COL_LIMITS.get(limit["kind"])
            if column is None:
                if limit is not preferred:
                    continue
                column = COL[cu.PREFERRED_MODEL]
            self.put(scr, y, column, "%3d%%" % limit["percent"],
                     curses.color_pair(COLORS[cu.level(limit["percent"])]))

        # The reset shown is the one for the window that actually binds, so it
        # agrees with the percentage the eye lands on first.
        binding = cu.binding_limit(p)
        if binding:
            self.put(scr, y, COL["reset"],
                     cu.until(cu.seconds_until_epoch(binding["resets_at_epoch"]))[:WIDTH["reset"]],
                     curses.A_DIM)
        if p.get("limit_reset"):
            self.put(scr, y, COL["⟲"], "⟲", curses.color_pair(1))

    # --- actions -------------------------------------------------------------

    def reconnect(self, scr, name):
        """Hand the terminal back for the OAuth login, then pick up again."""
        h, _ = scr.getmaxyx()
        self.put(scr, h - 2, 0,
                 " %s needs a login — [l] log in, any other key to cancel " % name,
                 curses.A_REVERSE)
        scr.refresh()
        scr.timeout(-1)
        try:
            answer = scr.getch()
        finally:
            scr.timeout(POLL_MS)
        if answer not in (ord("l"), ord("L")):
            with self.lock:
                self.message = "login cancelled"
            return

        curses.endwin()
        cu.reconnect(name)
        input("\nPress return to go back to the console…")
        scr.clear()
        curses.doupdate()
        self.painted = None
        self.spawn(run_auto=False)

    def choose(self, scr, index):
        with self.lock:
            row = self.rows[index] if index < len(self.rows) else None
        if row is None:
            return
        # An account that needs a login doesn't need a switch, it needs a login.
        if cu.needs_login(row):
            self.reconnect(scr, row["name"])
            return
        ok = cu.switch(row["name"])
        with self.lock:
            self.message = ("switched to %s — relaunch `claude` in your terminals"
                            % row["name"]) if ok else "switch failed"
        self.spawn(run_auto=False)

    def toggle_auto(self):
        state = cu.load_state()
        state["enabled"] = not state["enabled"]
        cu.save_state(state)
        with self.lock:
            self.state = state
            self.message = "auto-switching %s" % ("on" if state["enabled"] else "off")
        if state["enabled"]:
            self.spawn()

    def cycle_mode(self):
        state = cu.load_state()
        following = (cu.MODES.index(state["mode"]) + 1) % len(cu.MODES)
        state["mode"] = cu.MODES[following]
        cu.save_state(state)
        with self.lock:
            self.state = state
            self.plan = cu.plan_switch(self.rows, state)
            self.message = "mode: %s" % state["mode"]

    # --- key loop ------------------------------------------------------------

    def run(self, scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for pair, color in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW),
                            (3, curses.COLOR_RED)):
            curses.init_pair(pair, color, -1)
        scr.timeout(POLL_MS)

        threading.Thread(target=self.loop, daemon=True).start()

        while True:
            self.draw(scr)
            key = scr.getch()
            if key == -1:
                continue          # nothing pressed; draw() decides if anything moved
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), ord("R")):
                self.spawn(force=True)      # asked for by hand: skip the waiting periods
            elif key in (ord("a"), ord("A")):
                self.toggle_auto()
            elif key in (ord("m"), ord("M")):
                self.cycle_mode()
            elif ord("1") <= key <= ord("9"):
                self.choose(scr, key - ord("1"))


if __name__ == "__main__":
    curses.wrapper(Console().run)
