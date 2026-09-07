#!/usr/bin/env python3
"""
claudini_usage — read how much is left on every claudini profile.

Each claudini profile keeps its OAuth credentials in the macOS keychain under
the service "claudini-profile-<name>". We read that token, ask the usage
endpoint what is left, and print a table (or JSON for the menu bar app).

Usage:
  claudini_usage.py               # readable table
  claudini_usage.py --json        # machine-readable, for ClaudiniBar
  claudini_usage.py --switch <profile>
  claudini_usage.py --reconnect <profile>
  claudini_usage.py --auto on|off
  claudini_usage.py --tick        # run one auto-switch decision
"""

import concurrent.futures
import datetime as dt
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

CLAUDINI_HOME = os.path.expanduser("~/.claudini")
PROFILES_DIR = os.path.join(CLAUDINI_HOME, "profiles")
CONFIG = os.path.join(CLAUDINI_HOME, "config.json")
STATE_FILE = os.path.join(CLAUDINI_HOME, "auto.json")
CACHE_FILE = os.path.join(CLAUDINI_HOME, "usage-cache.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BOOTSTRAP_URL = "https://api.anthropic.com/api/claude_cli/bootstrap"
REFRESH_URL = "https://console.anthropic.com/v1/oauth/token"
# Claude Code's public client_id, the one visible in the OAuth login URL.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"

ACCOUNT = getpass.getuser()
REFRESH_LOCK = threading.Lock()

# The model worth protecting: its dedicated weekly quota runs out long before
# the general limits do.
PREFERRED_MODEL = "Fable"
GENERAL_KINDS = ("session", "weekly_all")
LIMIT_LABELS = {"session": "Session (5h)", "weekly_all": "Week (all models)"}

# Rate limit tiers seen in the wild, biggest allowance first. Percentages are
# only comparable within a tier — 20% of a team seat is not 20% of a Max 20x.
PLAN_RANKS = (("max_20x", 4), ("max_5x", 3), ("raven", 2), ("pro", 1))
# An unrecognised tier is never demoted for something we simply don't know:
# it ranks with the best, which falls back to plain model-first behaviour.
UNKNOWN_RANK = max(rank for _, rank in PLAN_RANKS)

# On a team plan the seat decides which models you get: a standard seat has no
# premium model, so a missing quota line there means "no access", not "quota
# untouched". Upgrading the seat flips this back on its own.
BASIC_SEAT = "standard"
SEAT_LABELS = {"standard": "std", "premium": "prem"}

SUCCESS_TTL = 45          # a good read is served as-is for this long
IDENTITY_TTL = 24 * 3600  # an account's org doesn't move: re-read once a day
FAIL_TTL = 15 * 60        # a failing account is left alone for this long
THROTTLE_SEC = 180        # after a 429, stop touching the API entirely

# Two statuses mean something to the machine; every other value describes a
# failure to read and amounts to "this account needs a login".
OK = "ok"
RATE_LIMITED = "rate limited"

DEFAULT_STATE = {
    "enabled": False,      # is auto-switching armed?
    "min_margin": 5,       # % of headroom below which an account counts as spent
    "cooldown_min": 10,    # minimum delay between two automatic switches
    "last_switch": 0,
}

CACHE_VERSION = 4
EMPTY_CACHE = {"profiles": {}, "identity": {}, "throttled_until": 0}


def needs_login(row):
    """An account no amount of re-reading will fix: it needs a login."""
    return row["status"] not in (OK, RATE_LIMITED)


# --- files -------------------------------------------------------------------

def _write_json(path, data, indent=None):
    """Atomic write: never leave a half-written file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_state():
    return dict(DEFAULT_STATE, **_read_json(STATE_FILE, {}))


def save_state(state):
    _write_json(STATE_FILE, state, indent=2)


def load_cache():
    """Last known read per profile, the accounts' identities, and how long the
    API asked us to stay quiet. Stamped: an entry written by an earlier version
    doesn't carry the same fields, so it is dropped."""
    data = _read_json(CACHE_FILE, {})
    if data.get("version") != CACHE_VERSION:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in EMPTY_CACHE.items()}
    return {"profiles": data.get("profiles", {}),
            "identity": data.get("identity", {}),
            "throttled_until": data.get("throttled_until", 0)}


def save_cache(cache):
    _write_json(CACHE_FILE, dict(cache, version=CACHE_VERSION))


# --- keychain ----------------------------------------------------------------

def _security(*args):
    return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True)


def keychain_read(service):
    r = _security("find-generic-password", "-s", service, "-a", ACCOUNT, "-w")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def keychain_write(service, payload):
    """-U updates the existing item, which preserves its ACL."""
    return _security("add-generic-password", "-U",
                     "-s", service, "-a", ACCOUNT,
                     "-w", json.dumps(payload)).returncode == 0


def keychain_delete(service):
    _security("delete-generic-password", "-s", service, "-a", ACCOUNT)


CRED_SERVICE_RE = re.compile(r'"svce"<blob>="(Claude Code-credentials[^"]*)"')


def claude_credential_services():
    """Keychain services where Claude Code stores credentials.

    A non-default CLAUDE_CONFIG_DIR gets a suffixed service whose suffix isn't
    guessable — we find it by diffing this list before and after a login.
    """
    return set(CRED_SERVICE_RE.findall(_security("dump-keychain").stdout))


def profile_service(name):
    return "claudini-profile-" + name


# --- profiles ----------------------------------------------------------------

# `/limit-reset` (clearing the 5-hour window, once a week) is opened account by
# account server-side. Claude Code caches that decision in the profile's
# claude.json, so we read it back to say where the command exists.
LIMIT_RESET_FLAG = "tengu_nifty_lemur"


def profile_config(name):
    return os.path.join(PROFILES_DIR, name, "claude.json")


def active_profile():
    return _read_json(CONFIG, {}).get("active_profile")


def profiles():
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(d for d in os.listdir(PROFILES_DIR)
                  if os.path.isfile(profile_config(d)))


def profile_meta(name):
    """(email, /limit-reset available) as the profile's claude.json knows them.

    The email is only a display fallback — the server is authoritative, see
    fetch_identity. The flag, on the other hand, exists nowhere else.
    """
    data = _read_json(profile_config(name), {})
    flag = (data.get("cachedGrowthBookFeatures") or {}).get(LIMIT_RESET_FLAG)
    return ((data.get("oauthAccount") or {}).get("emailAddress"),
            flag.get("enabled") if isinstance(flag, dict) else None)


# --- API ---------------------------------------------------------------------

def http_json(url, token=None, data=None):
    req = urllib.request.Request(url)
    req.add_header("anthropic-beta", BETA)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def refresh(service, creds):
    """Refresh an expired access token and write it back to the keychain.

    Returns (token, error) — error is set when the refresh failed.
    """
    oauth = creds["claudeAiOauth"]
    if not oauth.get("refreshToken"):
        return None, "no refresh token"

    # Refreshes are rare and the endpoint rate-limits fast: one at a time.
    with REFRESH_LOCK:
        try:
            new = http_json(REFRESH_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": CLIENT_ID,
            })
        except urllib.error.HTTPError as e:
            return None, RATE_LIMITED if e.code == 429 else "login required"
        except Exception:
            return None, "refresh unreachable"

    oauth["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        oauth["refreshToken"] = new["refresh_token"]
    if new.get("expires_in"):
        oauth["expiresAt"] = int((dt.datetime.now().timestamp() + new["expires_in"]) * 1000)
    keychain_write(service, creds)
    return oauth["accessToken"], None


def fetch_identity(token):
    """The account's real workspace and plan.

    A profile's claude.json does carry an organization name, but a session
    running on another profile can overwrite it — only the server is
    authoritative.
    """
    try:
        acct = (http_json(BOOTSTRAP_URL, token=token) or {}).get("oauth_account") or {}
    except Exception:
        return None
    org, email = acct.get("organization_name"), acct.get("account_email")
    # "<email>'s Organization" is the personal space, not a real workspace.
    personal = bool(org and email and org.startswith(email))
    return {
        "email": email,
        "space": "personal" if personal else org,
        "plan": (acct.get("organization_type") or "").replace("claude_", ""),
        "tier": acct.get("organization_rate_limit_tier"),
        "seat": acct.get("seat_tier"),
    }


def base_row(name, is_active, status=OK):
    email, limit_reset = profile_meta(name)
    return {"name": name, "email": email, "active": is_active,
            "limit_reset": limit_reset, "status": status, "limits": []}


def fetch(name, is_active, identity=None):
    """Read one profile's usage. Returns (row, fresh identity or None).

    The caller owns the cache: we never open it here, we hand back what it
    should store.
    """
    out = base_row(name, is_active)
    fresh_identity = None

    # The active profile also lives in Claude Code's own keychain slot, kept
    # current continuously — prefer it over claudini's snapshot.
    service = profile_service(name)
    creds = keychain_read("Claude Code-credentials") if is_active else None
    if creds is None:
        creds = keychain_read(service)
    if creds is None or "claudeAiOauth" not in creds:
        out["status"] = "no credentials"
        return out, None

    token = creds["claudeAiOauth"].get("accessToken")
    expired = creds["claudeAiOauth"].get("expiresAt", 0) / 1000 < dt.datetime.now().timestamp()
    if expired and not is_active:
        token, err = refresh(service, creds)
        if not token:
            out["status"] = err
            return out, None

    try:
        data = http_json(USAGE_URL, token=token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and not is_active:
            token, err = refresh(service, creds)
            if not token:
                out["status"] = err
                return out, None
            try:
                data = http_json(USAGE_URL, token=token)
            except Exception:
                out["status"] = "login required"
                return out, None
        elif e.code == 429:
            out["status"] = RATE_LIMITED
            return out, None
        else:
            out["status"] = "HTTP error %d" % e.code
            return out, None
    except Exception as e:
        out["status"] = "unreachable (%s)" % type(e).__name__
        return out, None

    if identity is None:
        identity = fresh_identity = fetch_identity(token)
    if identity:
        out.update(identity)

    for lim in data.get("limits") or []:
        scope = (lim.get("scope") or {}).get("model") or {}
        out["limits"].append({
            "kind": lim.get("kind"),
            "label": scope.get("display_name") or LIMIT_LABELS.get(lim.get("kind"),
                                                                   lim.get("kind")),
            "percent": lim.get("percent") or 0,
            "resets_at": lim.get("resets_at"),
        })

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        out["extra_credits"] = {
            "used": extra.get("used_credits"),
            "limit": extra.get("monthly_limit"),
            "currency": extra.get("currency"),
        }
    return out, fresh_identity


def collect():
    """Every profile's usage, hitting the API as little as possible.

    A good read is served for SUCCESS_TTL, a failing account is left alone for
    FAIL_TTL, and after a 429 the API isn't called at all until the guard
    expires. The cache is read and written here and nowhere else: the parallel
    reads never touch the file.
    """
    act = active_profile()
    cache = load_cache()
    entries, identities = cache["profiles"], cache["identity"]
    now = dt.datetime.now().timestamp()
    muted = now < cache["throttled_until"]
    hit_limit = threading.Event()

    def one(name):
        entry = entries.get(name)
        if entry:
            age = now - entry["at"]
            still_good = age < (SUCCESS_TTL if entry["status"] == OK else FAIL_TTL)
            if muted or still_good:
                return dict(entry["row"], cached=True, active=(name == act))
        if muted:
            return base_row(name, name == act, RATE_LIMITED)

        known = identities.get(name)
        usable = known["info"] if known and now - known["at"] < IDENTITY_TTL else None
        row, fresh = fetch(name, name == act, usable)
        entries[name] = {"at": now, "status": row["status"], "row": row}
        if fresh:
            identities[name] = {"at": now, "info": fresh}
        if row["status"] == RATE_LIMITED:
            hit_limit.set()
        return row

    # Three requests in flight is plenty: beyond that the API slams the door.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(one, profiles()))

    # Only a real 429 re-arms the guard: a row served from cache during the
    # pause would extend it forever.
    if hit_limit.is_set():
        cache["throttled_until"] = now + THROTTLE_SEC
    save_cache(cache)
    return rows


def throttled_for():
    """Seconds left before the API may be called again. 0 when free."""
    left = load_cache()["throttled_until"] - dt.datetime.now().timestamp()
    return max(0, int(left))


# --- switching policy --------------------------------------------------------

def general_headroom(p):
    """Headroom on the general limits (5h session + week), in %.
    None when the account said nothing."""
    general = [l for l in p["limits"] if l["kind"] in GENERAL_KINDS]
    if p["status"] != OK or not general:
        return None
    return 100 - max(l["percent"] for l in general)


def has_preferred_model(p):
    """Whether this seat gets the preferred model at all."""
    return BASIC_SEAT not in (p.get("seat") or "").lower()


def model_headroom(p):
    """Headroom on the preferred model's weekly quota. None when unknown."""
    if p["status"] != OK:
        return None
    for l in p["limits"]:
        if l["kind"] not in GENERAL_KINDS and l["label"].lower() == PREFERRED_MODEL.lower():
            return 100 - l["percent"]          # a reported quota settles it
    if not has_preferred_model(p):
        return None                            # basic seat: no access, not free quota
    # No dedicated quota reported means nothing is holding that model back.
    return 100 if p["limits"] else None


def saturated_models(p):
    """Models whose dedicated weekly quota is spent."""
    return [l["label"] for l in p["limits"]
            if l["kind"] not in GENERAL_KINDS and l["percent"] >= 100]


def plan_label(p):
    """Short, comparable name for what this account is paying for."""
    tier = (p.get("tier") or "").lower()
    seat = SEAT_LABELS.get((p.get("seat") or "").replace("team_", ""), "")
    if "max_20x" in tier:
        return "max 20x"
    if "max_5x" in tier:
        return "max 5x"
    if "raven" in tier:
        return ("team " + seat) if seat else "team"
    return p.get("plan") or ""


def plan_rank(p):
    """How big this account's allowance is, as a comparable number."""
    tier = (p.get("tier") or "").lower()
    for key, rank in PLAN_RANKS:
        if key in tier:
            return rank
    return UNKNOWN_RANK


def pick_target(rows, min_margin):
    """The account we ought to be on.

    Prefer one that still has the preferred model, but only among accounts on
    the largest plan available — a team seat with untouched Fable is worth
    less than a Max 20x with real headroom left. When no account on that plan
    has the model, fall back to whichever is freshest overall. Accounts whose
    general limits are already maxed are out either way: a full model quota is
    worthless when the 5-hour window is at 100%.
    """
    usable = [p for p in rows
              if p["status"] == OK and (general_headroom(p) or 0) > min_margin]
    if not usable:
        return None

    best_plan = max(plan_rank(p) for p in usable)
    with_model = [p for p in usable
                  if (model_headroom(p) or 0) > 0 and plan_rank(p) >= best_plan]
    if with_model:
        return max(with_model, key=model_headroom)
    return max(usable, key=general_headroom)


def switch_trigger(active, target, min_margin):
    """Why the active account should be abandoned, or None to stay."""
    if active["status"] != OK:
        return "current account unreachable"
    if (general_headroom(active) or 0) <= min_margin:
        return "general limits maxed out (%d%% left)" % (general_headroom(active) or 0)
    if not has_preferred_model(active) and has_preferred_model(target):
        return "this seat has no %s; %s does" % (PREFERRED_MODEL, target["name"])
    if plan_rank(target) > plan_rank(active):
        return "%s runs on a larger plan with more headroom" % target["name"]
    if (model_headroom(active) or 0) <= 0 < (model_headroom(target) or 0):
        return "%s spent here, available on %s" % (PREFERRED_MODEL, target["name"])
    return None


def plan_switch(rows, state):
    """What the next session gets: (target, why, blocked_by).

    `target` is always the best account to be on, even when we won't move to
    it — the interface should name it rather than guess. `blocked_by` says
    what stops the automatic switch, and is None when it would go ahead.
    """
    active = next((p for p in rows if p["active"]), None)
    target = pick_target(rows, state["min_margin"])
    if target is None:
        return None, "no account has headroom left", None
    if active is None or target["name"] == active["name"]:
        return target, "already the best account available", None

    trigger = switch_trigger(active, target, state["min_margin"])
    why = trigger or "more headroom than the current account"
    if not trigger:
        return target, why, "current account is not blocked yet"
    if not state["enabled"]:
        return target, why, "auto-switching is off"

    remaining = state["cooldown_min"] * 60 - (dt.datetime.now().timestamp() - state["last_switch"])
    if remaining > 0:
        return target, why, "cooldown, %d min left" % (remaining // 60 + 1)
    return target, why, None


def switch(name):
    return subprocess.run(["claudini", "use", name],
                          capture_output=True, text=True).returncode == 0


def auto_tick(rows=None):
    """One turn of the auto loop. Returns (rows, switched, message)."""
    state = load_state()
    if rows is None:
        rows = collect()
    if not state["enabled"]:
        return rows, False, "auto-switching is off"

    active = next((p for p in rows if p["active"]), None)
    target, why, blocked = plan_switch(rows, state)
    if target is None or active is None or target["name"] == active["name"]:
        return rows, False, why
    if blocked:
        return rows, False, blocked
    if not switch(target["name"]):
        return rows, False, "`claudini use %s` failed" % target["name"]

    state["last_switch"] = dt.datetime.now().timestamp()
    save_state(state)
    # Only the active account changed: no need to read everything again.
    for row in rows:
        row["active"] = (row["name"] == target["name"])
    return rows, True, "%s -> %s (%s)" % (active["name"], target["name"], why)


# --- reconnecting ------------------------------------------------------------

def reconnect(name):
    """Log a profile back in without touching the active account.

    The login runs in a throwaway CLAUDE_CONFIG_DIR: it has its own keychain
    slot and its own claude.json, so neither the active profile nor any
    running `claude` session moves. We then file the fresh credentials into
    the profile we meant to fix.
    """
    expected, _ = profile_meta(name)
    print("\033[1mReconnecting %s\033[0m%s" % (name, " (%s)" % expected if expected else ""))
    print("Your active account (%s) and running sessions stay put.\n"
          % (active_profile() or "?"))

    before = claude_credential_services()
    workdir = tempfile.mkdtemp(prefix="claudini-relogin-")
    try:
        subprocess.run(["claude", "auth", "login"],
                       env=dict(os.environ, CLAUDE_CONFIG_DIR=workdir))

        created = claude_credential_services() - before
        if not created:
            print("\033[31mLogin did not complete: no credentials were created.\033[0m")
            return
        service = created.pop()
        creds = keychain_read(service)
        keychain_delete(service)

        if not creds or "claudeAiOauth" not in creds:
            print("\033[31mCredentials unreadable after the login.\033[0m")
            return

        account = (_read_json(os.path.join(workdir, ".claude.json"), {})
                   .get("oauthAccount") or {})
        got = account.get("emailAddress")
        if expected and got and got != expected:
            print("\033[33mHeads up: %s expected %s, you logged in as %s.\033[0m"
                  % (name, expected, got))

        keychain_write(profile_service(name), creds)
        if account:
            # claudini reads a profile's identity from this file — without it,
            # `claudini profile list` would still show the old account.
            config = _read_json(profile_config(name), {})
            config["oauthAccount"] = account
            _write_json(profile_config(name), config, indent=2)
        print("\033[32m%s reconnected%s.\033[0m" % (name, " (%s)" % got if got else ""))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        cache = load_cache()
        cache["profiles"].pop(name, None)
        cache["identity"].pop(name, None)
        save_cache(cache)


# --- rendering ---------------------------------------------------------------

def seconds_until(iso):
    """Seconds until an ISO date from the API. None if absent or unreadable."""
    if not iso:
        return None
    try:
        target = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return max(0, int((target - dt.datetime.now(dt.timezone.utc)).total_seconds()))


def until(seconds):
    """A delay in seconds, written short."""
    if seconds is None:
        return ""
    mins = seconds // 60
    if mins <= 0:
        return "now"
    if mins < 60:
        return "%dmin" % mins
    if mins < 60 * 24:
        return "%dh%02d" % (mins // 60, mins % 60)
    return "%dd" % (mins // 1440)


def bar(pct):
    filled = int(round(pct / 10.0))
    return "█" * filled + "░" * (10 - filled)


def level(pct):
    """How alarming a usage percentage is. Shared by every renderer."""
    return "critical" if pct >= 95 else "warning" if pct >= 75 else "ok"


def throttle_notice(seconds):
    return "API paused for %ds (429) — showing cached data" % seconds


def render(rows):
    left = throttled_for()
    if left:
        print("\033[33m%s\033[0m\n" % throttle_notice(left))
    ansi = {"critical": "\033[31m", "warning": "\033[33m", "ok": "\033[32m"}
    for r in rows:
        head = "%s %-14s %s" % ("●" if r["active"] else "○", r["name"], r["email"] or "?")
        if r.get("space"):
            head += "  ·  %s" % r["space"]
        if plan_label(r):
            head += " (%s)" % plan_label(r)
        print("\033[1m%s\033[0m" % head)

        if r["status"] != OK:
            print("    %s" % r["status"])
        for lim in r["limits"]:
            pct = lim["percent"]
            print("    %-24s %s%s %3d%%\033[0m  resets in %s"
                  % (lim["label"], ansi[level(pct)], bar(pct), pct,
                     until(seconds_until(lim["resets_at"]))))
        if r.get("limit_reset"):
            print("    \033[36m/limit-reset available\033[0m         "
                  "clears the 5h window · once a week · spends weekly quota")
        if r.get("extra_credits"):
            e = r["extra_credits"]
            print("    Extra credits            %s / %s %s"
                  % (e["used"], e["limit"], e["currency"] or ""))
        print()


def for_json(rows, state):
    """The contract with the menu bar app.

    Everything derived from the limits is computed here, at send time: the app
    has no policy of its own to re-implement, and the countdowns are correct
    even when a row came from the cache.
    """
    target, why, blocked = plan_switch(rows, state)
    out = [dict(r,
                limits=[dict(l, resets_in_sec=seconds_until(l["resets_at"]),
                             level=level(l["percent"]))
                        for l in r["limits"]],
                headroom=general_headroom(r),
                plan_label=plan_label(r),
                saturated=saturated_models(r),
                needs_login=needs_login(r))
           for r in rows]
    return {
        "profiles": out,
        "auto": state["enabled"],
        "throttled_for": throttled_for(),
        "next": {"name": target["name"] if target else None,
                 "reason": why, "blocked_by": blocked},
    }


def main():
    args = sys.argv[1:]
    command = args[0] if args else None

    if command == "--switch":
        sys.exit(0 if switch(args[1]) else 1)

    if command == "--reconnect":
        reconnect(args[1])
        return

    if command == "--auto":                      # --auto on | off | status
        state = load_state()
        if len(args) > 1 and args[1] in ("on", "off"):
            state["enabled"] = args[1] == "on"
            save_state(state)
        print("auto: %s" % ("on" if state["enabled"] else "off"))
        return

    if command == "--tick":                      # one decision, on its own
        print(auto_tick()[2])
        return

    rows = collect()
    # `--json --tick`: one API pass feeding both the auto loop and the display.
    if "--tick" in args:
        rows = auto_tick(rows)[0]

    if "--json" in args:
        json.dump(for_json(rows, load_state()), sys.stdout)
    else:
        render(rows)


if __name__ == "__main__":
    main()
