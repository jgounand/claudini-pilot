#!/usr/bin/env python3
"""
claudini-auto — console de pilotage des comptes Claude.

Affiche la conso de chaque abonnement, permet de basculer en une touche, et
propose un mode automatique : rester sur un compte qui a encore du Fable,
et retomber sur le compte le plus frais tous modèles confondus quand Fable
est épuisé partout.

Touches : a auto · r rafraîchir · 1-9 basculer · q quitter
"""

import curses
import importlib.util
import os
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location("cu", os.path.join(HERE, "claudini_usage.py"))
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)

REFRESH_SEC = 90


class Console:
    def __init__(self):
        self.rows = []
        self.state = cu.load_state()
        self.message = "chargement…"
        self.busy = True
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # --- données -------------------------------------------------------------

    def refresh(self, run_auto=True):
        with self.lock:
            self.busy = True
        rows = cu.collect()
        msg = ""
        if run_auto and cu.load_state()["enabled"]:
            moved, msg = cu.auto_tick(rows)
            if moved:
                rows = cu.collect()          # l'actif a changé, on relit
                msg = "bascule auto : " + msg
        with self.lock:
            self.rows = rows
            self.state = cu.load_state()
            self.message = msg or time.strftime("mis à jour à %H:%M:%S")
            self.busy = False

    def loop(self):
        while not self.stop.wait(REFRESH_SEC):
            self.refresh()

    # --- rendu ---------------------------------------------------------------

    def pair(self, pct):
        if pct >= 95:
            return curses.color_pair(3)
        if pct >= 75:
            return curses.color_pair(2)
        return curses.color_pair(1)

    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        with self.lock:
            rows, state, message, busy = self.rows, self.state, self.message, self.busy

        active = next((p for p in rows if p["active"]), None)
        target = cu.pick_target(rows, state["min_margin"]) if rows else None

        # en-tête
        auto_on = state["enabled"]
        scr.addstr(0, 0, " claudini ", curses.A_REVERSE | curses.A_BOLD)
        scr.addstr(0, 11, "actif: ")
        scr.addstr(active["name"] if active else "?", curses.A_BOLD)
        scr.addstr(0, 40, "auto: ")
        scr.addstr("ACTIF  " if auto_on else "inactif",
                   curses.color_pair(1) | curses.A_BOLD if auto_on else curses.A_DIM)
        if target and active and target["name"] != active["name"]:
            scr.addstr(0, 60, "→ %s" % target["name"], curses.color_pair(2))

        y = 2
        scr.addstr(y, 0, "  #  profil          compte                       session   semaine    Fable   reset    ⟲",
                   curses.A_DIM)
        y += 1

        for i, p in enumerate(rows, 1):
            if y >= h - 3:
                break
            mark = "●" if p["active"] else " "
            scr.addstr(y, 0, " %s%d " % (mark, i), curses.A_BOLD if p["active"] else 0)
            scr.addstr(y, 5, p["name"][:15], curses.A_BOLD if p["active"] else 0)
            scr.addstr(y, 21, (p["email"] or "?")[:28], curses.A_DIM)

            if p["status"] != "ok":
                note = p["status"] + (" (cache)" if p.get("cached") else "")
                scr.addstr(y, 51, note[:28], curses.color_pair(3))
                y += 1
                continue

            cols = {"session": 51, "weekly_all": 61}
            for limit in p["limits"]:
                col = cols.get(limit["kind"], 71)
                if limit["kind"] not in cols and limit["label"].lower() != "fable":
                    continue
                scr.addstr(y, col, "%3d%%" % limit["percent"], self.pair(limit["percent"]))
            reset = next((l["resets_at"] for l in p["limits"] if l["kind"] == "session"), None)
            scr.addstr(y, 79, cu.until(reset)[:8], curses.A_DIM)
            if p.get("limit_reset"):
                scr.addstr(y, 88, "⟲", curses.color_pair(1))
            y += 1

        # pied de page
        scr.addstr(h - 2, 0, ("⏳ " if busy else "   ") + message[:w - 4], curses.A_DIM)
        scr.addstr(h - 1, 0,
                   " a auto · r rafraîchir · 1-9 basculer · q quitter    ⟲ = /limit-reset dispo ",
                   curses.A_REVERSE)
        scr.refresh()

    # --- boucle clavier ------------------------------------------------------

    def run(self, scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(i, c, -1)
        scr.nodelay(True)

        threading.Thread(target=self.refresh, daemon=True).start()
        threading.Thread(target=self.loop, daemon=True).start()

        while True:
            self.draw(scr)
            time.sleep(0.2)
            try:
                key = scr.getkey()
            except curses.error:
                continue

            if key in ("q", "Q"):
                self.stop.set()
                return
            if key in ("r", "R"):
                threading.Thread(target=self.refresh, daemon=True).start()
            elif key in ("a", "A"):
                state = cu.load_state()
                state["enabled"] = not state["enabled"]
                cu.save_state(state)
                with self.lock:
                    self.state = state
                    self.message = "auto %s" % ("activé" if state["enabled"] else "désactivé")
                if state["enabled"]:
                    threading.Thread(target=self.refresh, daemon=True).start()
            elif key.isdigit() and key != "0":
                idx = int(key) - 1
                with self.lock:
                    name = self.rows[idx]["name"] if idx < len(self.rows) else None
                if name:
                    ok = cu.switch(name)
                    with self.lock:
                        self.message = ("bascule sur %s — relance `claude` dans tes terminaux"
                                        % name) if ok else "échec de la bascule sur %s" % name
                    threading.Thread(target=lambda: self.refresh(run_auto=False),
                                     daemon=True).start()


if __name__ == "__main__":
    curses.wrapper(Console().run)
