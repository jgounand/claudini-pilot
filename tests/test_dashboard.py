#!/usr/bin/env python3
"""Tests for the dashboard's figures: hand-built histories, a fixed "now".

    python3 tests/test_dashboard.py
"""

import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "claudini_history", os.path.join(HERE, os.pardir, "src", "claudini_history.py"))
ch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ch)
cu = ch.cu

NOW = 1_800_000_000
HOUR = 3600


def sample(at, **weekly):
    return {"at": at, "usage": {name: {"weekly_all": pct, "at": at} for name, pct in weekly.items()}}


class Cycles(unittest.TestCase):
    def fold(self, values):
        rollup = cu.empty_rollup()
        for i, pct in enumerate(values):
            cu.roll_up(rollup, NOW + i * HOUR, "a", pct)
        return ch.weekly_cycles(rollup, "a")

    def test_a_week_counts_what_it_used_before_its_reset(self):
        done, current = self.fold([40, 70, 90, 3, 20])
        self.assertEqual([c["used_pct"] for c in done], [90])
        self.assertEqual(current, 20)

    def test_a_small_dip_is_not_a_reset(self):
        done, current = self.fold([50, 45, 60])
        self.assertEqual(done, [])
        self.assertEqual(current, 60)


class Pace(unittest.TestCase):
    def test_only_rises_count(self):
        samples = [sample(NOW - 10 * HOUR, a=10), sample(NOW - 5 * HOUR, a=30),
                   sample(NOW, a=0)]                     # 0: a reset, not use
        self.assertAlmostEqual(ch.burn_per_hour(samples, {"a"}, NOW), 20 / 10)

    def test_old_use_is_outside_the_window(self):
        samples = [sample(NOW - 100 * HOUR, a=0), sample(NOW - 90 * HOUR, a=80),
                   sample(NOW, a=80)]
        self.assertEqual(ch.burn_per_hour(samples, {"a"}, NOW), 0)


class Forecast(unittest.TestCase):
    def test_runs_out_when_the_pace_cannot_be_met(self):
        # One account, 80 points left under a line of 100, 10 points an hour.
        points, run_out = ch.forecast([(20, NOW + 100 * HOUR)], 10, NOW, 100)
        self.assertEqual(run_out, NOW + 8 * HOUR)
        self.assertEqual(points[0]["reserve"], 0.8)

    def test_a_reset_gives_the_week_back(self):
        points, run_out = ch.forecast([(100, NOW + 2 * HOUR)], 0, NOW, 95)
        self.assertEqual(points[0]["reserve"], 0)
        self.assertEqual(points[2]["reserve"], 1)
        self.assertIsNone(run_out)

    def test_spends_what_resets_soonest_first(self):
        points, _ = ch.forecast([(0, NOW + 1 * HOUR), (0, NOW + 150 * HOUR)], 50, NOW, 100)
        # Hour 0's 50 points come off the account that resets at hour 1, so by
        # then both are whole again; taken from the other, 1.5 would be left.
        self.assertEqual(points[1]["reserve"], 2.0)


class Capacity(unittest.TestCase):
    def test_an_unread_account_keeps_its_last_figure(self):
        samples = [sample(NOW, a=20, b=50), sample(NOW + HOUR, a=40)]
        points = ch.capacity_series(samples, {"a", "b"})
        self.assertEqual([p["reserve"] for p in points], [1.3, 1.1])

    def test_accounts_outside_the_rotation_are_left_out(self):
        points = ch.capacity_series([sample(NOW, a=20, gone=0)], {"a"})
        self.assertEqual(points[0]["reserve"], 0.8)


class Prices(unittest.TestCase):
    def test_team_seats_and_personal_plans(self):
        self.assertEqual(ch.monthly_price({"tier": "default_raven", "seat": "team_tier_1"}), 125)
        self.assertEqual(ch.monthly_price({"tier": "default_raven", "seat": "team_standard"}), 25)
        self.assertEqual(ch.monthly_price({"tier": "default_claude_max_20x"}), 200)
        self.assertIsNone(ch.monthly_price({"tier": "something_new"}))


class Summary(unittest.TestCase):
    def fold(self, readings):
        rollup = cu.empty_rollup()
        for at, pct in readings:
            cu.roll_up(rollup, at, "a", pct)
        return rollup

    def test_rises_are_spending_and_a_fall_closes_the_week(self):
        rollup = self.fold([(NOW, 10), (NOW + HOUR, 40), (NOW + 2 * HOUR, 90), (NOW + 3 * HOUR, 5)])
        self.assertEqual([c["used_pct"] for c in rollup["cycles"]], [90])
        day = next(iter(rollup["days"].values()))["a"]
        self.assertEqual(day["used"], 30 + 50 + 5)       # the first reading is a baseline
        self.assertEqual(day["max"], 90)

    def test_readings_flipping_between_two_workspaces_are_not_spending(self):
        """30, 80, 30, 80…: one reset at most, then the flips count nothing."""
        rollup = self.fold([(NOW + i * HOUR, pct) for i, pct in enumerate([30, 80, 30, 80, 30, 80])])
        self.assertEqual(len(rollup["cycles"]), 1)
        self.assertEqual(sum(cell["used"] for cells in rollup["days"].values()
                             for cell in cells.values()), 50 + 30 + 50)

    def test_a_reading_is_folded_in_once(self):
        rollup = self.fold([(NOW, 10), (NOW + HOUR, 40), (NOW + HOUR, 40), (NOW, 10)])
        self.assertEqual(next(iter(rollup["days"].values()))["a"]["used"], 30)

    def test_it_can_be_rebuilt_from_the_readings(self):
        entries = [sample(NOW, a=10), sample(NOW + HOUR, a=60)]
        rebuilt = cu.rebuild_rollup(entries)
        self.assertEqual(next(iter(rebuilt["days"].values()))["a"]["used"], 50)


class WeekByWeek(unittest.TestCase):
    def test_weeks_add_up_days_and_price_what_was_left(self):
        rollup = cu.empty_rollup()
        # Monday 2026-11-16 to Sunday 2026-11-22: "a" read every day, 60 points spent.
        for i in range(7):
            rollup["days"]["2026-11-%02d" % (16 + i)] = {"a": {"used": 10 if i < 6 else 0, "max": 0, "seen": 1}}
        rollup["days"]["2026-11-23"] = {"a": {"used": 5, "max": 0, "seen": 1}}
        weeks = ch.week_by_week(rollup, lambda name: 52, "2026-11-23")
        self.assertEqual([w["start"] for w in weeks], ["2026-11-16", "2026-11-23"])
        self.assertEqual(weeks[0]["used"], 0.6)
        self.assertEqual(weeks[0]["capacity"], 1.0)
        self.assertAlmostEqual(weeks[0]["unused_value"], 12 * 0.4)    # 52 $/month = 12 $/week
        self.assertFalse(weeks[0]["partial"])
        self.assertTrue(weeks[1]["partial"])


class Whole(unittest.TestCase):
    def test_it_serialises_with_no_history(self):
        row = {"name": "a", "email": "a@x", "active": True, "status": cu.OK, "detail": None,
               "limit_reset": None, "tier": "default_raven", "seat": "team_tier_1",
               "limits": [{"kind": "weekly_all", "label": "7d", "percent": 30, "model": None,
                           "resets_at": "2027-03-01T00:00:00+00:00"}]}
        out = ch.dashboard([row], dict(cu.DEFAULT_STATE), [], now=NOW, rollup=cu.empty_rollup())
        json.dumps(out)
        self.assertEqual(out["seats"][0]["avg_used_pct"], None)
        self.assertEqual(out["totals"]["monthly_price"], 125)
        self.assertEqual(out["capacity"]["forecast"][0]["reserve"], 0.7)
        self.assertEqual(out["weeks"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
