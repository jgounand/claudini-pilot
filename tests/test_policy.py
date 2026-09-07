#!/usr/bin/env python3
"""Tests for the decision-making half of the engine.

Everything here runs on hand-built rows: no network, no keychain, no clock
beyond a fixed "now". That is deliberate — the parts worth pinning down are the
ones that decide which account you land on, and those are pure functions over a
row. The I/O around them is exercised by using the tool.

    python3 tests/test_policy.py
"""

import datetime as dt
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "cu", os.path.join(HERE, os.pardir, "src", "claudini_usage.py"))
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)

NOW = dt.datetime.now(dt.timezone.utc)


def when(**delta):
    return (NOW + dt.timedelta(**delta)).isoformat()


def limit(kind, percent, label=None, resets_in_days=5):
    return {"kind": kind,
            "model": label if kind not in cu.GENERAL_KINDS else None,
            "label": label or cu.LIMIT_LABELS.get(kind, kind),
            "percent": percent,
            "resets_at": when(days=resets_in_days)}


def account(name, session=10, weekly=10, model=None, weekly_in=5,
            tier="default_claude_max_20x", seat=None, status=cu.OK, active=False):
    """A row shaped like the ones fetch() produces."""
    limits = []
    if status == cu.OK:
        limits = [limit("session", session, resets_in_days=0.2),
                  limit("weekly_all", weekly, resets_in_days=weekly_in)]
        if model is not None:
            limits.append(limit("weekly_scoped", model, label=cu.PREFERRED_MODEL,
                                resets_in_days=weekly_in))
    return {"name": name, "email": name + "@example.com", "active": active,
            "status": status, "detail": None, "limit_reset": None,
            "limits": limits, "tier": tier, "seat": seat}


def state(mode=cu.MODE_MODEL, **over):
    return dict(cu.DEFAULT_STATE, mode=mode, **over)


class Headroom(unittest.TestCase):
    def test_worst_general_is_the_fuller_window(self):
        self.assertEqual(cu.worst_general(account("a", session=30, weekly=70)), 70)
        self.assertEqual(cu.worst_general(account("a", session=90, weekly=20)), 90)

    def test_unreadable_account_has_no_headroom(self):
        self.assertIsNone(cu.worst_general(account("a", status=cu.UNREACHABLE)))
        self.assertIsNone(cu.general_headroom(account("a", status=cu.NEEDS_LOGIN)))

    def test_headroom_is_the_complement(self):
        self.assertEqual(cu.general_headroom(account("a", session=30, weekly=70)), 30)


class PreferredModel(unittest.TestCase):
    def test_reported_quota_wins(self):
        self.assertEqual(cu.model_headroom(account("a", model=40)), 60)

    def test_no_quota_line_means_untouched(self):
        self.assertEqual(cu.model_headroom(account("a")), 100)

    def test_but_a_standard_team_seat_has_no_access(self):
        """A missing line on a basic seat means "not available", not "unused" —
        the mistake that made a team seat outrank a Max account."""
        seat = account("a", seat="team_standard", tier="default_raven")
        self.assertIsNone(cu.model_headroom(seat))
        self.assertFalse(cu.has_preferred_model(seat))

    def test_a_premium_seat_does_have_access(self):
        self.assertTrue(cu.has_preferred_model(account("a", seat="team_premium")))


class Plans(unittest.TestCase):
    def test_bigger_tier_outranks(self):
        big = account("a", tier="default_claude_max_20x")
        small = account("b", tier="default_raven", seat="team_standard")
        self.assertGreater(cu.plan_rank(big), cu.plan_rank(small))

    def test_unknown_tier_is_not_demoted(self):
        """We never punish an account for a tier we simply do not recognise."""
        self.assertEqual(cu.plan_rank(account("a", tier="something_new")),
                         cu.UNKNOWN_RANK)

    def test_labels(self):
        self.assertEqual(cu.plan_label(account("a")), "max 20x")
        self.assertEqual(cu.plan_label(account("a", tier="default_raven",
                                               seat="team_standard")), "team std")


class Eligibility(unittest.TestCase):
    def test_spent_on_either_window_is_out(self):
        rows = [account("fresh", session=10, weekly=10),
                account("session-gone", session=99, weekly=10),
                account("week-gone", session=10, weekly=99)]
        usable = [p["name"] for p in cu.usable_accounts(rows, 95)]
        self.assertEqual(usable, ["fresh"])

    def test_threshold_is_usage_not_margin(self):
        rows = [account("a", session=90)]
        self.assertEqual(len(cu.usable_accounts(rows, 95)), 1)
        self.assertEqual(len(cu.usable_accounts(rows, 85)), 0)


class ModelMode(unittest.TestCase):
    def test_prefers_the_account_with_model_left(self):
        rows = [account("spent", model=100), account("free", model=20)]
        self.assertEqual(cu.pick_target(rows, state())["name"], "free")

    def test_only_among_the_largest_plan(self):
        """A team seat with untouched Fable is worth less than a Max account
        with real headroom: percentages are not comparable across plans."""
        rows = [account("team", session=78, weekly=26, tier="default_raven",
                        seat="team_standard"),
                account("max", session=14, weekly=64, model=100)]
        self.assertEqual(cu.pick_target(rows, state())["name"], "max")

    def test_falls_back_to_headroom_when_nobody_has_the_model(self):
        rows = [account("tight", session=80, model=100),
                account("roomy", session=20, model=100)]
        self.assertEqual(cu.pick_target(rows, state())["name"], "roomy")


class EnduranceMode(unittest.TestCase):
    def setUp(self):
        self.state = state(cu.MODE_ENDURANCE)

    def test_spends_what_expires_first(self):
        """Not the greenest account — the one whose allowance is about to be
        lost. This is the whole point of the mode."""
        rows = [account("greener", session=27, weekly=47, weekly_in=6),
                account("expiring", session=7, weekly=69, weekly_in=5)]
        self.assertEqual(cu.pick_target(rows, self.state)["name"], "expiring")

    def test_ties_go_to_the_one_with_more_room(self):
        rows = [account("tight", session=80, weekly=20, weekly_in=5),
                account("roomy", session=5, weekly=20, weekly_in=5)]
        self.assertEqual(cu.pick_target(rows, self.state)["name"], "roomy")

    def test_never_an_account_that_is_already_spent(self):
        rows = [account("expiring-but-full", session=100, weekly=99, weekly_in=1),
                account("usable", session=20, weekly=20, weekly_in=6)]
        self.assertEqual(cu.pick_target(rows, self.state)["name"], "usable")


class Planning(unittest.TestCase):
    def test_staying_put_names_the_successor(self):
        rows = [account("here", session=10, active=True), account("next", session=20)]
        plan = cu.plan_switch(rows, state())
        self.assertEqual(plan[0]["name"], "here")
        self.assertTrue(cu.staying(rows, plan))
        self.assertEqual(cu.runner_up(rows, state())["name"], "next")

    def test_moving_explains_itself(self):
        rows = [account("done", session=99, active=True), account("free", session=10)]
        target, why, blocked = cu.plan_switch(rows, state(enabled=True))
        self.assertEqual(target["name"], "free")
        self.assertIn("99", why)          # names how full it actually got
        self.assertIsNone(blocked)

    def test_auto_off_is_reported_as_the_blocker(self):
        rows = [account("done", session=99, active=True), account("free", session=10)]
        blocked = cu.plan_switch(rows, state(enabled=False))[2]
        self.assertEqual(blocked, "auto-switching is off")

    def test_cooldown_blocks_without_hiding_the_target(self):
        rows = [account("done", session=99, active=True), account("free", session=10)]
        recent = state(enabled=True, last_switch=dt.datetime.now().timestamp())
        target, _, blocked = cu.plan_switch(rows, recent)
        self.assertEqual(target["name"], "free")
        self.assertIn("cooldown", blocked)

    def test_when_everything_is_spent_it_names_the_first_to_recover(self):
        """Reporting "nothing left" leaves you with nothing to act on."""
        rows = [account("late", session=100, weekly=100, weekly_in=6, active=True),
                account("soon", session=100, weekly=100, weekly_in=1)]
        target, why, blocked = cu.plan_switch(rows, state(enabled=True))
        self.assertEqual(target["name"], "soon")
        self.assertIn("free up", why)
        self.assertEqual(blocked, "waiting for a reset")


class Ordering(unittest.TestCase):
    def test_active_first_then_policy_then_unusable_then_unreadable(self):
        rows = [account("broken", status=cu.NEEDS_LOGIN),
                account("spent", session=100),
                account("second", session=40, active=False),
                account("here", session=10, active=True)]
        order = [p["name"] for p in cu.ranked(rows, state())]
        self.assertEqual(order[0], "here")
        self.assertEqual(order[1], "second")
        self.assertEqual(order[-1], "broken")


class Retrying(unittest.TestCase):
    def test_a_dropped_network_comes_back_fast(self):
        self.assertLessEqual(cu.fail_delay(1, cu.UNREACHABLE), 60)

    def test_a_dead_token_waits(self):
        self.assertGreaterEqual(cu.fail_delay(1, cu.NEEDS_LOGIN), 10 * 60)

    def test_delays_widen_but_are_capped(self):
        delays = [cu.fail_delay(n, cu.NEEDS_LOGIN) for n in range(1, 9)]
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], cu.RETRY[cu.NEEDS_LOGIN][1])

    def test_needs_login_is_the_only_status_offering_a_login(self):
        for status in (cu.OK, cu.RATE_LIMITED, cu.UNREACHABLE, cu.BLOCKED):
            self.assertFalse(cu.needs_login(account("a", status=status)), status)
        self.assertTrue(cu.needs_login(account("a", status=cu.NEEDS_LOGIN)))


class Reporting(unittest.TestCase):
    def test_detail_is_preferred_over_the_token(self):
        row = account("a", status=cu.UNREACHABLE)
        self.assertEqual(cu.status_text(row), "unreachable")
        row["detail"] = "unreachable (URLError)"
        self.assertEqual(cu.status_text(row), "unreachable (URLError)")

    def test_binding_limit_is_the_fuller_window(self):
        binding = cu.binding_limit(account("a", session=20, weekly=80))
        self.assertEqual(binding["label"], "7d")
        self.assertEqual(binding["percent"], 80)

    def test_fleet_counts_the_weekly_reserve(self):
        rows = [account("a", weekly=60), account("b", weekly=90),
                account("c", status=cu.NEEDS_LOGIN)]
        summary = cu.fleet(rows, state())
        self.assertEqual(summary["weekly_reserve"], 50)     # 40 + 10
        self.assertEqual(summary["total"], 3)

    def test_levels(self):
        self.assertEqual(cu.level(10), "ok")
        self.assertEqual(cu.level(80), "warning")
        self.assertEqual(cu.level(100), "critical")

    def test_durations_read_short(self):
        self.assertEqual(cu.until(0), "now")
        self.assertEqual(cu.until(90 * 60), "1h30")
        self.assertEqual(cu.until(3 * 86400), "3d")
        self.assertEqual(cu.until(None), "")


class Settings(unittest.TestCase):
    def test_the_old_margin_wording_is_converted(self):
        """A config written before the rename still means what it meant."""
        original = cu.STATE_FILE
        try:
            cu.STATE_FILE = os.path.join(HERE, "state-under-test.json")
            cu._write_json(cu.STATE_FILE, {"min_margin": 15})
            self.assertEqual(cu.load_state()["max_usage"], 85)
        finally:
            if os.path.exists(cu.STATE_FILE):
                os.unlink(cu.STATE_FILE)
            cu.STATE_FILE = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
