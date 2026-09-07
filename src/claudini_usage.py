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
import contextlib
import datetime as dt
import fcntl
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
# The token endpoint lives on platform.claude.com, not on the API host, and it
# sits behind Cloudflare: a request without a User-Agent is answered with
# "error code: 1010" (client banned) long before Anthropic sees it, which reads
# exactly like a rejected token if you are not looking closely.
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code's public client_id, the one visible in the OAuth login URL.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"
USER_AGENT = "claudini-pilot/1.0 (+https://github.com/jgounand/claudini-pilot)"

ACCOUNT = getpass.getuser()
REFRESH_LOCK = threading.Lock()
REFRESH_LOCK_FILE = os.path.join(CLAUDINI_HOME, "refresh.lock")


@contextlib.contextmanager
def refresh_guard():
    """Serialise token refreshes across processes, not merely across threads.

    The refresh token rotates on every use, and the console, the menu bar and
    a one-off CLI run are three separate processes. Two of them refreshing the
    same profile at once means the loser presents a token the winner already
    consumed — which comes back as a dead credential on a perfectly good
    account.
    """
    with REFRESH_LOCK:
        with open(REFRESH_LOCK_FILE, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

# The model worth protecting: its dedicated weekly quota runs out long before
# the general limits do.
PREFERRED_MODEL = "Fable"
GENERAL_KINDS = ("session", "weekly_all")
LIMIT_LABELS = {"session": "Session (5h)", "weekly_all": "Week (all models)"}

# Rate limit tiers seen in the wild, biggest allowance first: (substring in the
# tier name, rank, display label). Percentages are only comparable within a
# tier — 20% of a team seat is not 20% of a Max 20x.
PLAN_TIERS = (("max_20x", 4, "max 20x"), ("max_5x", 3, "max 5x"),
              ("raven", 2, "team"), ("pro", 1, "pro"))
# An unrecognised tier is never demoted for something we simply don't know:
# it ranks with the best, which falls back to plain model-first behaviour.
UNKNOWN_RANK = max(rank for _, rank, _ in PLAN_TIERS)

# On a team plan the seat decides which models you get: a standard seat has no
# premium model, so a missing quota line there means "no access", not "quota
# untouched". Upgrading the seat flips this back on its own.
BASIC_SEAT = "standard"
SEAT_LABELS = {"standard": "std", "premium": "prem"}

# Freshness. SUCCESS_TTL sits just *below* the interfaces' poll period on
# purpose: a poller that wakes at P always finds its own row too old and
# refetches, while a second interface polling out of phase lands inside the
# window and pays nothing. Set the TTL above P instead and nothing ever
# refetches at all; set it far below and the cache stops mattering.
POLL_SEC = 300            # what the interfaces are expected to poll at
SUCCESS_TTL = POLL_SEC - 20
IDENTITY_TTL = 24 * 3600  # an account's org doesn't move: re-read once a day
THROTTLE_SEC = 180        # after a 429, stop touching the API entirely

# A broken account is retried on a widening delay. Each retry costs a token
# refresh that is bound to fail against the endpoint that rate-limits fastest,
# so a flat interval burns hundreds of doomed requests a week for nothing.
# reconnect() clears the entry, so a real login is picked up on the next pass
# however deep the backoff went.
FAIL_TTL = 15 * 60
MAX_FAIL_TTL = 6 * 3600

# Status is a closed set the machine reads; `detail` carries the sentence for
# the human. Keeping them in one string meant every consumer had to classify
# by negation, and a passing network blip was shown as a credentials problem.
OK = "ok"
RATE_LIMITED = "rate limited"
NEEDS_LOGIN = "login required"
UNREACHABLE = "unreachable"
BLOCKED = "oauth blocked"

# Two ways to be optimal, because there are two different goals.
#   model      — protect the preferred model's weekly quota above all else.
#   endurance  — never stop working: spend the allowance that is about to
#                reset before it evaporates, and keep the long-dated ones.
MODE_MODEL = "model"
MODE_ENDURANCE = "endurance"
MODES = (MODE_MODEL, MODE_ENDURANCE)

DEFAULT_STATE = {
    "enabled": False,      # is auto-switching armed?
    "mode": MODE_MODEL,
    "min_margin": 5,       # % of headroom below which an account counts as spent
    "cooldown_min": 10,    # minimum delay between two automatic switches
    "last_switch": 0,
}

CACHE_VERSION = 4
EMPTY_CACHE = {"profiles": {}, "identity": {}, "throttled_until": 0}


def needs_login(row):
    """An account no amount of re-reading will fix: it needs a login."""
    return row["status"] == NEEDS_LOGIN


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


_META_CACHE = {}


def profile_meta(name):
    """(email, /limit-reset available) as the profile's claude.json knows them.

    The email is only a display fallback — the server is authoritative, see
    fetch_identity. The flag, on the other hand, exists nowhere else.

    That file is ~100 kB and only changes on a switch or a login, so it is
    re-parsed on mtime rather than on every pass.
    """
    path = profile_config(name)
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None, None
    cached = _META_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]

    data = _read_json(path, {})
    flag = (data.get("cachedGrowthBookFeatures") or {}).get(LIMIT_RESET_FLAG)
    meta = ((data.get("oauthAccount") or {}).get("emailAddress"),
            flag.get("enabled") if isinstance(flag, dict) else None)
    _META_CACHE[path] = (stamp, meta)
    return meta


# --- API ---------------------------------------------------------------------

def http_json(url, token=None, data=None, timeout=20):
    req = urllib.request.Request(url)
    req.add_header("anthropic-beta", BETA)
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def refresh(service, creds):
    """Refresh an expired access token and write it back to the keychain.

    Returns (token, status, detail) — status is None when it worked.
    """
    oauth = creds["claudeAiOauth"]
    if not oauth.get("refreshToken"):
        return None, NEEDS_LOGIN, "no refresh token stored"

    # Refreshes are rare and the endpoint rate-limits fast: one at a time. The
    # timeout is short because this lock is held across it, and a stalled
    # refresh would block every other profile behind it.
    with refresh_guard():
        # Someone may have refreshed this profile while we waited for the
        # lock; their token is the live one, ours is already spent.
        stored = (keychain_read(service) or {}).get("claudeAiOauth") or {}
        if stored.get("accessToken") and stored["accessToken"] != oauth.get("accessToken"):
            return stored["accessToken"], None, None
        try:
            new = http_json(REFRESH_URL, timeout=5, data={
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": CLIENT_ID,
            })
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return None, RATE_LIMITED, None
            # Only the endpoint saying the grant is bad means a login is
            # actually needed. Anything else is our side of the wire.
            body = (e.read() or b"").decode("utf8", "replace")[:200]
            if e.code == 400 and "invalid_grant" in body:
                return None, NEEDS_LOGIN, "refresh token expired or revoked"
            return None, UNREACHABLE, "refresh refused (HTTP %d) %s" % (e.code, body.strip())
        except Exception as e:
            return None, UNREACHABLE, "refresh endpoint unreachable (%s)" % type(e).__name__

    oauth["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        oauth["refreshToken"] = new["refresh_token"]
    if new.get("expires_in"):
        oauth["expiresAt"] = int((dt.datetime.now().timestamp() + new["expires_in"]) * 1000)
    keychain_write(service, creds)
    return oauth["accessToken"], None, None


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


def base_row(name, is_active, status=OK, detail=None):
    email, limit_reset = profile_meta(name)
    return {"name": name, "email": email, "active": is_active,
            "limit_reset": limit_reset, "status": status, "detail": detail,
            "limits": []}


def api_message(body):
    """The human-readable half of an Anthropic error body."""
    try:
        return json.loads(body).get("error", {}).get("message")
    except (ValueError, AttributeError):
        return None


def _failed(out, status, detail=None):
    out["status"], out["detail"] = status, detail
    return out, None


def fetch(name, is_active, identity=None):
    """Read one profile's usage. Returns (row, fresh identity or None).

    The caller owns the cache: we never open it here, we hand back what it
    should store.
    """
    out = base_row(name, is_active)

    # The active profile also lives in Claude Code's own keychain slot, kept
    # current continuously — prefer it over claudini's snapshot.
    service = profile_service(name)
    creds = keychain_read("Claude Code-credentials") if is_active else None
    if creds is None:
        creds = keychain_read(service)
    if creds is None or "claudeAiOauth" not in creds:
        return _failed(out, NEEDS_LOGIN, "no credentials in the keychain")

    token = creds["claudeAiOauth"].get("accessToken")
    expired = creds["claudeAiOauth"].get("expiresAt", 0) / 1000 < dt.datetime.now().timestamp()

    def read_usage(tok):
        return http_json(USAGE_URL, token=tok)

    # One retry, and only one: an expired token is refreshed up front, and a
    # token the server rejects is refreshed once before giving up. The active
    # profile is never refreshed here — Claude Code owns that slot.
    for attempt in range(2):
        if (expired or attempt) and not is_active:
            token, status, detail = refresh(service, creds)
            if not token:
                return _failed(out, status, detail)
            expired = False
        try:
            data = read_usage(token)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return _failed(out, RATE_LIMITED)
            body = (e.read() or b"").decode("utf8", "replace")[:400]
            # A permission error is the organisation refusing OAuth, not a bad
            # token — refreshing or logging in again cannot fix it.
            if e.code == 403 and "permission_error" in body:
                return _failed(out, BLOCKED,
                               api_message(body) or "OAuth not allowed for this organisation")
            if e.code in (401, 403) and not is_active and not attempt:
                continue                     # stale token: refresh and retry
            if e.code in (401, 403):
                return _failed(out, NEEDS_LOGIN, "credentials rejected by the API")
            return _failed(out, UNREACHABLE, "HTTP error %d" % e.code)
        except Exception as e:
            return _failed(out, UNREACHABLE, "unreachable (%s)" % type(e).__name__)
    else:
        return _failed(out, NEEDS_LOGIN, "credentials rejected by the API")

    fresh_identity = None
    if identity is None:
        identity = fresh_identity = fetch_identity(token)
    if identity:
        out.update(identity)

    for lim in data.get("limits") or []:
        scope = (lim.get("scope") or {}).get("model") or {}
        out["limits"].append({
            "kind": lim.get("kind"),
            # `model` is what the policy keys on; `label` is for humans only.
            "model": scope.get("id") or scope.get("display_name"),
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


def fail_delay(failures, status=None):
    """How long to leave a broken account alone after N consecutive failures.

    An organisation that forbids OAuth won't change its mind in a quarter of
    an hour, so that one goes straight to the ceiling.
    """
    if status == BLOCKED:
        return MAX_FAIL_TTL
    return min(FAIL_TTL * 2 ** max(0, failures - 1), MAX_FAIL_TTL)


_throttled_until = None


def collect():
    """Every profile's usage, hitting the API as little as possible.

    A good read is served for SUCCESS_TTL, a broken one for a delay that widens
    with each consecutive failure, and after a 429 the API isn't called at all
    until the guard expires. The cache is read and written here and nowhere
    else: the parallel reads never touch the file.
    """
    global _throttled_until
    act = active_profile()
    cache = load_cache()
    entries, identities = cache["profiles"], cache["identity"]
    now = dt.datetime.now().timestamp()
    muted = now < cache["throttled_until"]
    hit_limit = threading.Event()

    def served(entry, name):
        return dict(entry["row"], cached=True, active=(name == act))

    def one(name):
        entry = entries.get(name)
        if muted:
            return served(entry, name) if entry else base_row(name, name == act, RATE_LIMITED)
        if entry:
            ok = entry["row"]["status"] == OK
            age_limit = (SUCCESS_TTL if ok
                         else fail_delay(entry.get("fails", 1), entry["row"]["status"]))
            if now - entry["at"] < age_limit:
                return served(entry, name)

        known = identities.get(name)
        usable = known["info"] if known and now - known["at"] < IDENTITY_TTL else None
        row, fresh = fetch(name, name == act, usable)
        failures = 0 if row["status"] == OK else (entry or {}).get("fails", 0) + 1
        entries[name] = {"at": now, "row": row, "fails": failures}
        if fresh:
            identities[name] = {"at": now, "info": fresh}
        if row["status"] == RATE_LIMITED:
            hit_limit.set()
        return row

    # Two requests in flight is enough: the wall-clock gain of going wider is
    # under a second, and burstiness is what trips the limiter.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(one, profiles()))

    # Only a real 429 re-arms the guard: a row served from cache during the
    # pause would extend it forever.
    if hit_limit.is_set():
        cache["throttled_until"] = now + THROTTLE_SEC
    save_cache(cache)
    _throttled_until = cache["throttled_until"]
    return rows


def throttled_for():
    """Seconds left before the API may be called again. 0 when free."""
    until_when = _throttled_until
    if until_when is None:
        until_when = load_cache()["throttled_until"]
    return max(0, int(until_when - dt.datetime.now().timestamp()))


def active_row(rows):
    return next((p for p in rows if p["active"]), None)


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


def preferred_limit(p):
    """The limit line for the preferred model, if the account reports one."""
    return next((l for l in p["limits"]
                 if l["kind"] not in GENERAL_KINDS
                 and (l.get("model") or l["label"]).lower() == PREFERRED_MODEL.lower()),
                None)


def model_headroom(p):
    """Headroom on the preferred model's weekly quota. None when unknown."""
    if p["status"] != OK:
        return None
    found = preferred_limit(p)
    if found:
        return 100 - found["percent"]          # a reported quota settles it
    if not has_preferred_model(p):
        return None                            # basic seat: no access, not free quota
    # No dedicated quota reported means nothing is holding that model back.
    return 100 if p["limits"] else None


def saturated_models(p):
    """Models whose dedicated weekly quota is spent."""
    return [l["label"] for l in p["limits"]
            if l["kind"] not in GENERAL_KINDS and l["percent"] >= 100]


def _tier_of(p):
    tier = (p.get("tier") or "").lower()
    return next((row for row in PLAN_TIERS if row[0] in tier), None)


def plan_label(p):
    """Short, comparable name for what this account is paying for."""
    found = _tier_of(p)
    if not found:
        return p.get("plan") or ""
    label = found[2]
    seat = SEAT_LABELS.get((p.get("seat") or "").replace("team_", ""), "")
    return (label + " " + seat) if seat else label


def plan_rank(p):
    """How big this account's allowance is, as a comparable number."""
    found = _tier_of(p)
    return found[1] if found else UNKNOWN_RANK


def weekly_reset(p):
    """When this account's weekly allowance resets, as a unix timestamp."""
    weekly = next((l for l in p["limits"] if l["kind"] == "weekly_all"), None)
    return epoch_of(weekly["resets_at"]) if weekly else None


def usable_accounts(rows, min_margin):
    """Accounts with room on both general windows.

    general_headroom is 100 minus the *worst* of the session and weekly
    percentages, so clearing this bar means neither is near its limit.
    """
    return [p for p in rows
            if p["status"] == OK and (general_headroom(p) or 0) > min_margin]


def pick_target(rows, state):
    """The account we ought to be on, per the configured mode."""
    usable = usable_accounts(rows, state["min_margin"])
    if not usable:
        return None
    if state.get("mode") == MODE_ENDURANCE:
        return _pick_endurance(usable)
    return _pick_model(usable)


def _pick_model(usable):
    """Protect the preferred model.

    Prefer an account that still has it, but only among those on the largest
    plan available — a team seat with untouched Fable is worth less than a
    Max 20x with real headroom left. When no account on that plan has the
    model, fall back to whichever is freshest overall.
    """
    best_plan = max(plan_rank(p) for p in usable)
    with_model = [p for p in usable
                  if (model_headroom(p) or 0) > 0 and plan_rank(p) >= best_plan]
    if with_model:
        return max(with_model, key=model_headroom)
    return max(usable, key=general_headroom)


def _pick_endurance(usable):
    """Spend what is about to expire.

    A weekly allowance that resets tonight is worth nothing kept, while one
    that resets in five days is a reserve. So burn the soonest-resetting
    account first, and among accounts resetting at the same time take the one
    with the most room so the next wall is furthest away.
    """
    horizon = dt.datetime.now().timestamp() + 30 * 86400
    return min(usable, key=lambda p: (weekly_reset(p) or horizon,
                                      -(general_headroom(p) or 0)))


def switch_trigger(active, target, state):
    """Why the active account should be abandoned, or None to stay."""
    min_margin = state["min_margin"]
    if active["status"] != OK:
        return "current account unreachable"
    if (general_headroom(active) or 0) <= min_margin:
        return "general limits maxed out (%d%% left)" % (general_headroom(active) or 0)
    if state.get("mode") == MODE_ENDURANCE:
        # Here moving is the whole point: the target's allowance expires first,
        # so spending it now is what keeps the long-dated ones in reserve.
        mine, theirs = weekly_reset(active), weekly_reset(target)
        if mine and theirs and theirs < mine:
            return "%s resets in %s, spend it before it is lost" % (
                target["name"], until(int(theirs - dt.datetime.now().timestamp())))
        return "%s has more room left" % target["name"]
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
    active = active_row(rows)
    target = pick_target(rows, state)
    if target is None:
        return None, "no account has headroom left", "nothing to switch to"
    if active is None or target["name"] == active["name"]:
        return target, "already the best account available", "staying put"

    trigger = switch_trigger(active, target, state)
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

    active = active_row(rows)
    target, why, blocked = plan_switch(rows, state)
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

def epoch_of(iso):
    """An API ISO date as a unix timestamp, so a renderer can count down from
    it whenever it draws rather than from when we read it."""
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


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
    state = load_state()
    target, why, blocked = plan_switch(rows, state)
    print("\033[1mmode\033[0m %s   \033[1mauto\033[0m %s   \033[1mnext\033[0m %s — %s"
          % (state["mode"], "on" if state["enabled"] else "off",
             target["name"] if target else "?",
             why if blocked == "staying put" else (blocked or why)))
    left = throttled_for()
    if left:
        print("\033[33m%s\033[0m" % throttle_notice(left))
    print()
    ansi = {"critical": "\033[31m", "warning": "\033[33m", "ok": "\033[32m"}
    for r in rows:
        head = "%s %-14s %s" % ("●" if r["active"] else "○", r["name"], r["email"] or "?")
        if r.get("space"):
            head += "  ·  %s" % r["space"]
        if plan_label(r):
            head += " (%s)" % plan_label(r)
        print("\033[1m%s\033[0m" % head)

        if r["status"] != OK:
            print("    %s" % (r.get("detail") or r["status"]))
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


SHORT_LABELS = {"session": "5h", "weekly_all": "7d"}


def for_json(rows, state):
    """The contract with the menu bar app.

    Everything derived here is derived once: the app holds no policy and no
    thresholds of its own. Reset times go out as absolute timestamps so the
    app can count down from a stale snapshot without drifting.
    """
    target, why, blocked = plan_switch(rows, state)
    pause = throttled_for()
    out = []
    for r in rows:
        headroom = general_headroom(r)
        out.append(dict(
            r,
            limits=[dict(l, resets_at_epoch=epoch_of(l["resets_at"]),
                         short_label=SHORT_LABELS.get(l["kind"], l["label"]),
                         level=level(l["percent"]))
                    for l in r["limits"]],
            headroom=headroom,
            headroom_level=level(100 - headroom) if headroom is not None else None,
            plan_label=plan_label(r),
            saturated=saturated_models(r),
            needs_login=needs_login(r)))
    return {
        "profiles": out,
        "auto": state["enabled"],
        "mode": state["mode"],
        "modes": list(MODES),
        "throttled_for": pause,
        "throttle_notice": throttle_notice(pause) if pause else None,
        # The app polls on this rather than hardcoding a copy of the TTL.
        "poll_after_sec": POLL_SEC,
        "stale_after_sec": SUCCESS_TTL,
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
        print("auto: %s (%s mode)" % ("on" if state["enabled"] else "off", state["mode"]))
        return

    if command == "--mode":                      # --mode model | endurance
        state = load_state()
        if len(args) > 1:
            if args[1] not in MODES:
                sys.exit("mode must be one of: %s" % ", ".join(MODES))
            state["mode"] = args[1]
            save_state(state)
        print("mode: %s" % state["mode"])
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
