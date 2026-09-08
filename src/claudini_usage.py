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

import collections
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import getpass
import importlib.util
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

# Guards must always be taken in this order. Re-taking one you already hold
# deadlocks silently, and taking them out of order deadlocks two processes
# against each other — both are the kind of bug that shows up once a month at
# a customer's, so the rule is checked rather than described.
GUARD_ORDER = {"switch": 1, "refresh": 2, "cache": 3, "state": 4, "history": 5}
HELD = threading.local()


def lock_path(name):
    return os.path.join(CLAUDINI_HOME, "%s.lock" % name)


def _rank(name):
    return GUARD_ORDER[name.split("-")[0]]


@contextlib.contextmanager
def guard(name):
    """An exclusive lock held across processes, not merely across threads.

    The console, the menu bar and a one-off CLI run are three processes over
    one set of files and keychain entries, so a threading.Lock protects
    nothing. The lock file is never unlinked on purpose: removing it while
    another process holds a handle on that inode lets a third create a fresh
    one and lock a different file, so two holders think they are alone.

    flock is held per open file description and each entry opens its own, so
    two threads of one process already exclude each other through it — no
    second in-process lock is needed. Nesting is allowed but only in
    GUARD_ORDER, which is asserted here so a mistake raises at the offending
    line instead of hanging.
    """
    held = getattr(HELD, "stack", None)
    if held is None:
        held = HELD.stack = []
    if held and _rank(name) <= _rank(held[-1]):
        raise RuntimeError("guard %r taken while holding %r — see GUARD_ORDER"
                           % (name, held[-1]))

    os.makedirs(CLAUDINI_HOME, exist_ok=True)
    held.append(name)
    try:
        with open(lock_path(name), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        held.pop()


def refresh_guard(name):
    """One refresh at a time per profile — the token rotates on every use, so
    the loser of a race presents one the winner already spent."""
    return guard("refresh-" + name)

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



# Status is a closed set the machine reads; `detail` carries the sentence for
# the human. Keeping them in one string meant every consumer had to classify
# by negation, and a passing network blip was shown as a credentials problem.
OK = "ok"
RATE_LIMITED = "rate_limited"
NEEDS_LOGIN = "needs_login"
UNREACHABLE = "unreachable"
BLOCKED = "oauth_blocked"

# The tokens above are for the machine; this is what a person reads when the
# failure carries no more specific detail of its own.
STATUS_TEXT = {
    OK: "ok",
    RATE_LIMITED: "rate limited",
    NEEDS_LOGIN: "login required",
    UNREACHABLE: "unreachable",
    BLOCKED: "usage not readable for this workspace right now",
}


def status_text(row):
    return row.get("detail") or STATUS_TEXT.get(row["status"], row["status"])


# How soon to try a failed account again — (first delay, ceiling), doubling in
# between. What matters is whether waiting can plausibly fix it: a dropped
# network heals by itself in seconds, a dead token needs you, and an
# organisation's OAuth policy will not change this afternoon. Treating them
# alike either burns doomed requests or hides an account for hours after a
# two-second outage.
RETRY = {
    UNREACHABLE: (20, 5 * 60),
    RATE_LIMITED: (THROTTLE_SEC, THROTTLE_SEC),
    NEEDS_LOGIN: (15 * 60, 6 * 3600),
    # Observed to clear on its own within the hour, so this is not the
    # permanent org policy its wording suggests. Backing off for hours would
    # keep showing an error long after the account started answering again.
    BLOCKED: (5 * 60, 60 * 60),
}
DEFAULT_RETRY = (15 * 60, 6 * 3600)

# Two ways to be optimal, because there are two different goals.
#   model      — protect the preferred model's weekly quota above all else.
#   endurance  — never stop working: spend the allowance that is about to
#                reset before it evaporates, and keep the long-dated ones.
MODE_MODEL = "model"
MODE_ENDURANCE = "endurance"
MODES = (MODE_MODEL, MODE_ENDURANCE)
MODE_TITLES = {
    MODE_MODEL: "model — keep {model} available",
    MODE_ENDURANCE: "endurance — spend what resets soonest",
}


def mode_title(mode):
    return MODE_TITLES.get(mode, mode).format(model=PREFERRED_MODEL)

DEFAULT_STATE = {
    "enabled": False,      # is auto-switching armed?
    "mode": MODE_MODEL,
    # An account counts as spent once its worst window reaches this. Expressed
    # as usage, like every figure on screen — the old setting said the same
    # thing as remaining margin, which read backwards against the display.
    # Lower it to move off an account before you hit the wall rather than at it.
    "max_usage": 95,
    "cooldown_min": 10,    # minimum delay between two automatic switches
    "last_switch": 0,
}

CACHE_VERSION = 7


def needs_login(row):
    """An account no amount of re-reading will fix: it needs a login."""
    return row["status"] == NEEDS_LOGIN


# --- files -------------------------------------------------------------------

def _write_json(path, data, indent=None):
    """Atomic write: never leave a half-written file behind.

    The staging file is named per writer. Three processes share these files,
    and a fixed ".tmp" name meant two of them streamed JSON into one buffer
    and installed the splice — after which load_cache sees invalid JSON,
    silently discards everything, and every profile is refetched at once,
    which is precisely what earns the 429 the cache exists to avoid.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(handle, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())     # a crash here would otherwise install an empty file
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_text(path, body):
    """The same temp-file-then-rename discipline as _write_json, for the log."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(handle, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_state():
    stored = _read_json(STATE_FILE, {})
    if "min_margin" in stored and "max_usage" not in stored:
        stored["max_usage"] = 100 - stored.pop("min_margin")   # older wording
    return dict(DEFAULT_STATE, **stored)


def save_state(state):
    _write_json(STATE_FILE, state, indent=2)


def load_cache():
    """Last known read per profile, the accounts' identities, and how long the
    API asked us to stay quiet. Stamped: an entry written by an earlier version
    doesn't carry the same fields, so it is dropped."""
    data = _read_json(CACHE_FILE, {})
    if data.get("version") != CACHE_VERSION:
        data = {}
    return {"profiles": data.get("profiles", {}),
            "identity": data.get("identity", {}),
            "throttled_until": data.get("throttled_until", 0)}


def save_cache(cache):
    _write_json(CACHE_FILE, dict(cache, version=CACHE_VERSION))


@contextlib.contextmanager
def open_cache():
    """The cache, loaded and saved under its guard."""
    with guard("cache"):
        disk = load_cache()
        yield disk
        save_cache(disk)


def merge_cache(mine):
    """Fold a finished pass into whatever is on disk now.

    A pass holds its copy across seconds of HTTP, so plainly saving it
    overwrites anything another process learned meanwhile. The worst loss was
    `throttled_until`: erasing another process's 429 back-off makes both keep
    calling through the pause the API asked for, which is self-reinforcing.
    A login could be lost the same way — reconnect() drops the stale entry so
    the fresh credentials are picked up, and an in-flight pass put it back.
    """
    with open_cache() as disk:
        for name, entry in mine["profiles"].items():
            known = disk["profiles"].get(name)
            if not known or entry["at"] >= known["at"]:
                disk["profiles"][name] = entry
        for name, entry in mine["identity"].items():
            known = disk["identity"].get(name)
            if not known or entry["at"] >= known["at"]:
                disk["identity"][name] = entry
        disk["throttled_until"] = max(disk["throttled_until"], mine["throttled_until"])


HISTORY_FILE = os.path.join(CLAUDINI_HOME, "history.jsonl")
HISTORY_DAYS = 30
HISTORY_GAP = 5 * 60           # don't record the same picture twice in a row
HISTORY_MAX_BYTES = 4 << 20    # trim once the log passes this


def record(entry):
    """Append one line to the history, and keep it from growing forever.

    Append-only and one JSON object per line, so two processes writing at once
    interleave whole lines rather than corrupting each other — the file is a
    log, not a document, and losing a sample costs nothing.
    """
    entry["at"] = int(dt.datetime.now().timestamp())
    with guard("history"):
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        if os.path.getsize(HISTORY_FILE) > HISTORY_MAX_BYTES:
            trim_history()


def trim_history():
    kept = read_history()
    _write_text(HISTORY_FILE, "".join(json.dumps(line) + "\n" for line in kept))


def read_history():
    """Every entry still within the window, oldest first."""
    cutoff = dt.datetime.now().timestamp() - HISTORY_DAYS * 86400
    try:
        with open(HISTORY_FILE) as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    return [e for e in entries if e.get("at", 0) >= cutoff]


def last_sample_at():
    """When we last recorded a picture, from the tail of the log.

    Parsing the whole file to read one timestamp meant every pass — in each
    long-running process — walked a log we let grow to four megabytes.
    """
    try:
        with open(HISTORY_FILE, "rb") as f:
            f.seek(max(0, os.path.getsize(HISTORY_FILE) - 8192))
            tail = f.read().decode("utf8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(tail):
        if '"usage"' in line:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(line).get("at", 0)
    return 0


def record_usage(rows):
    """Sample what every account looks like, at most every HISTORY_GAP."""
    fresh = [r for r in rows if r["status"] == OK and r["limits"]]
    if not fresh or dt.datetime.now().timestamp() - last_sample_at() < HISTORY_GAP:
        return
    record({"usage": {r["name"]: {l["kind"]: l["percent"] for l in r["limits"]}
                      for r in fresh},
            "active": (active_row(rows) or {}).get("name")})


def forget(name):
    """Drop what we know about a profile, without clobbering the rest."""
    with open_cache() as disk:
        disk["profiles"].pop(name, None)
        disk["identity"].pop(name, None)


def update_state(**changes):
    """Change settings without reverting anything written meanwhile.

    Every writer used to load, modify and save the whole file. A tick holding
    a snapshot across a network pass would write back the state from before
    your keypress — turning auto-switching back on by itself — or drop the
    `last_switch` a concurrent tick had just recorded, erasing the cooldown.
    """
    with guard("state"):
        state = load_state()
        state.update(changes)
        save_state(state)
        return state


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


_CONFIG_CACHE = {}


def profile_document(name):
    """The profile's claude.json, parsed once per change.

    It is ~100 kB and holds two things we want — the account's email and the
    /limit-reset flag, plus the usage Claude Code cached there. Parsing it
    twice per pass was most of the cost of reading a profile.
    """
    path = profile_config(name)
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return {}
    cached = _CONFIG_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    document = _read_json(path, {})
    _CONFIG_CACHE[path] = (stamp, document)
    return document


def profile_meta(name):
    """(email, /limit-reset available) as the profile's claude.json knows them.

    The email is only a display fallback — the server is authoritative, see
    fetch_identity. The flag, on the other hand, exists nowhere else.

    That file is ~100 kB and only changes on a switch or a login, so it is
    re-parsed on mtime rather than on every pass.
    """
    data = profile_document(name)
    flag = (data.get("cachedGrowthBookFeatures") or {}).get(LIMIT_RESET_FLAG)
    return ((data.get("oauthAccount") or {}).get("emailAddress"),
            flag.get("enabled") if isinstance(flag, dict) else None)


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


def refresh(name, service, creds):
    """Refresh an expired access token and write it back to the keychain.

    Returns (token, status, detail) — status is None when it worked.
    """
    oauth = creds["claudeAiOauth"]
    if not oauth.get("refreshToken"):
        return None, NEEDS_LOGIN, "no refresh token stored"

    # Refreshes are rare and the endpoint rate-limits fast: one at a time. The
    # timeout is short because this lock is held across it, and a stalled
    # refresh would block every other profile behind it.
    with refresh_guard(name):
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

        # Still inside the guard. The check above is the whole safety
        # mechanism, and it reads the keychain — so the winner must have
        # written before the next contender looks, or the loser presents a
        # token the server has already rotated and reads a healthy account
        # as needing a login.
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
        "account_uuid": acct.get("account_uuid"),
        "email": email,
        "space": "personal" if personal else org,
        "plan": (acct.get("organization_type") or "").replace("claude_", ""),
        "tier": acct.get("organization_rate_limit_tier"),
        "seat": acct.get("seat_tier"),
    }


# Claude Code caches the usage response it gets into the profile's claude.json
# while it runs, so for the account you are actually working on the numbers are
# already on disk, fresher than anything we would poll for. They are only
# trustworthy for the *active* profile: switching copies the running session's
# file into the outgoing profile, so an idle profile's copy usually belongs to
# whichever account was live at the time. The account uuid is checked anyway.
LOCAL_USAGE_TTL = 10 * 60


def local_usage(name, expected_uuid):
    """The usage Claude Code already fetched for this profile, or None."""
    cached = profile_document(name).get("cachedUsageUtilization") or {}
    if not expected_uuid or cached.get("accountUuid") != expected_uuid:
        return None
    age = dt.datetime.now().timestamp() - cached.get("fetchedAtMs", 0) / 1000
    if age > LOCAL_USAGE_TTL:
        return None
    return cached.get("utilization")


def parse_usage(out, data):
    """Fill a row from a usage payload — the API and the local copy share it."""
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
    return out


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
    out["source"] = "api"

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

    # One retry, and only one: an expired token is refreshed up front, and a
    # token the server rejects is refreshed once before giving up. The active
    # profile is never refreshed here — Claude Code owns that slot.
    for attempt in range(2):
        if (expired or attempt) and not is_active:
            token, status, detail = refresh(name, service, creds)
            if not token:
                return _failed(out, status, detail)
        try:
            data = http_json(USAGE_URL, token=token)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return _failed(out, RATE_LIMITED, "the API is rate limiting us")
            body = (e.read() or b"").decode("utf8", "replace")[:400]
            # A permission error is the organisation refusing OAuth, not a bad
            # token — refreshing or logging in again cannot fix it.
            if e.code == 403 and "permission_error" in body:
                # The account itself still works — inference and the profile
                # endpoint answer fine; it is this reporting endpoint the
                # workspace is refusing, and it has been seen to come back.
                return _failed(out, BLOCKED, api_message(body))
            if e.code in (401, 403) and not is_active and not attempt:
                continue                     # stale token: refresh and retry
            if e.code in (401, 403):
                return _failed(out, NEEDS_LOGIN, "credentials rejected by the API")
            return _failed(out, UNREACHABLE, "HTTP error %d" % e.code)
        except Exception as e:
            return _failed(out, UNREACHABLE, "unreachable (%s)" % type(e).__name__)

    fresh_identity = None
    if identity is None:
        identity = fresh_identity = fetch_identity(token)
    if identity:
        out.update(identity)

    return parse_usage(out, data), fresh_identity


def fail_delay(failures, status=None):
    """How long to leave a failed account alone, per what went wrong."""
    first, ceiling = RETRY.get(status, DEFAULT_RETRY)
    return min(first * 2 ** max(0, failures - 1), ceiling)


def collect(force=False):
    """Every profile's usage, hitting the API as little as possible.

    `force` is a person asking for it now: it skips the per-account waiting
    periods so a account that failed while the network was down comes back
    immediately rather than at the end of its backoff. The 429 guard still
    applies — asking harder does not make the API answer.

    A good read is served for SUCCESS_TTL, a broken one for a delay that widens
    with each consecutive failure, and after a 429 the API isn't called at all
    until the guard expires. The cache is read and written here and nowhere
    else: the parallel reads never touch the file.
    """
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
        known = identities.get(name)
        identity = known["info"] if known and now - known["at"] < IDENTITY_TTL else None

        # Claude Code keeps the running account's usage on disk and updates it
        # as you work, so for that profile there is nothing to ask for. This
        # sits above the cache gate on purpose: waiting for the entry to
        # expire would serve numbers minutes older than the ones already here.
        if name == act and identity:
            local = local_usage(name, identity.get("account_uuid"))
            if local is not None:
                row = parse_usage(dict(base_row(name, True), source="local"), local)
                row.update(identity)
                entries[name] = {"at": now, "row": row, "fails": 0}
                return row

        if muted:
            return (served(entry, name) if entry else
                    base_row(name, name == act, RATE_LIMITED, "paused after a 429"))
        if entry and not force:
            ok = entry["row"]["status"] == OK
            age_limit = (SUCCESS_TTL if ok
                         else fail_delay(entry.get("fails", 1), entry["row"]["status"]))
            if now - entry["at"] < age_limit:
                return served(entry, name)

        row, fresh = fetch(name, name == act, identity)
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
    merge_cache(cache)
    record_usage(rows)
    return rows


def throttled_for():
    """Seconds left before the API may be called again. 0 when free."""
    return max(0, int(load_cache()["throttled_until"] - dt.datetime.now().timestamp()))


def active_row(rows):
    return next((p for p in rows if p["active"]), None)


# --- switching policy --------------------------------------------------------

def general_headroom(p):
    """Headroom on the general limits (5h session + week), in %.
    None when the account said nothing."""
    worst = worst_general(p)
    return None if worst is None else 100 - worst


def seat_kind(p):
    """"standard", "premium" or "" — the seat, normalised once."""
    return (p.get("seat") or "").lower().replace("team_", "")


def has_preferred_model(p):
    """Whether this seat gets the preferred model at all."""
    return seat_kind(p) != BASIC_SEAT


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
    seat = SEAT_LABELS.get(seat_kind(p), "")
    return (label + " " + seat) if seat else label


def plan_rank(p):
    """How big this account's allowance is, as a comparable number."""
    found = _tier_of(p)
    return found[1] if found else UNKNOWN_RANK


def weekly_reset(p):
    """When this account's weekly allowance resets, as a unix timestamp."""
    weekly = next((l for l in p["limits"] if l["kind"] == "weekly_all"), None)
    return epoch_of(weekly["resets_at"]) if weekly else None


def worst_general(p):
    """The fuller of the two general windows — the one that stops you first."""
    general = [l for l in p["limits"] if l["kind"] in GENERAL_KINDS]
    if p["status"] != OK or not general:
        return None
    return max(l["percent"] for l in general)


def usable_accounts(rows, max_usage):
    """Accounts with room on both general windows.

    The test is on the fuller of the two, so clearing it means neither the
    five-hour nor the weekly window is near its limit.
    """
    return [p for p in rows
            if worst_general(p) is not None and worst_general(p) < max_usage]


def preference(rows, state):
    """The usable accounts, best first, in the order the mode prefers them.

    Ranking rather than picking, so an interface can show the order and let
    the choice explain itself — otherwise the greenest-looking account not
    being chosen just looks like a bug.
    """
    usable = usable_accounts(rows, state["max_usage"])
    if not usable:
        return []
    if state.get("mode") == MODE_ENDURANCE:
        return _by_endurance(usable)
    return _by_model(usable)


def pick_target(rows, state):
    """The account we ought to be on, per the configured mode."""
    order = preference(rows, state)
    return order[0] if order else None


def _by_model(usable):
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
        return sorted(with_model, key=lambda p: -(model_headroom(p) or 0))
    return sorted(usable, key=lambda p: -(general_headroom(p) or 0))


def _by_endurance(usable):
    """Spend what is about to expire.

    A weekly allowance that resets tonight is worth nothing kept, while one
    that resets in five days is a reserve. So burn the soonest-resetting
    account first, and among accounts resetting together take the one with the
    most room so the next wall is furthest away.
    """
    horizon = dt.datetime.now().timestamp() + 30 * 86400
    return sorted(usable, key=lambda p: (weekly_reset(p) or horizon,
                                         -(general_headroom(p) or 0)))


def switch_trigger(active, target, state):
    """Why the active account should be abandoned, or None to stay."""
    if active["status"] != OK:
        return "current account unreachable"
    if (worst_general(active) or 100) >= state["max_usage"]:
        return "general limits at %d%%" % (worst_general(active) or 100)
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


def staying(rows, plan):
    """Is the plan to keep the account we are already on?"""
    active = active_row(rows)
    return bool(plan[0] and active and plan[0]["name"] == active["name"])


def recovers_at(p, max_usage):
    """When this account becomes usable again.

    An account can be over the line on both windows at once, and clearing the
    five-hour one then buys nothing — the weekly still blocks it. So recovery
    is the *latest* of the resets that are actually in the way.
    """
    blocking = [l["resets_at"] for l in p["limits"]
                if l["kind"] in GENERAL_KINDS and l["percent"] >= max_usage]
    resets = [epoch_of(iso) for iso in blocking]
    resets = [e for e in resets if e is not None]
    return max(resets) if resets else None


def first_to_recover(rows, max_usage):
    """Of the accounts we can read, the one whose block lifts soonest.

    When nothing has room, this is the only useful thing to say: naming the
    account and the wait beats reporting that everything is full.
    """
    waiting = [(recovers_at(p, max_usage), p) for p in rows if p["status"] == OK]
    waiting = [(at, p) for at, p in waiting if at]
    return min(waiting, key=lambda pair: pair[0])[1] if waiting else None


def ranked(rows, state):
    """Rows in the order the policy prefers them.

    The account in use, then the ones the mode would take, best first, then
    those it cannot use, then those it cannot read. Alphabetical order made
    the greenest-looking account appear above the one actually chosen.
    """
    order = {p["name"]: i for i, p in enumerate(preference(rows, state))}

    def rank(p):
        if p["active"]:
            return (0, 0, p["name"])
        if p["name"] in order:
            return (1, order[p["name"]], p["name"])
        return (2 if p["status"] == OK else 3, 0, p["name"])

    return sorted(rows, key=rank)


def fleet(rows, state):
    """Whether you are heading for an outage, in one line's worth of facts.

    The five-hour windows recycle and are staggered, so an account sitting at
    100% session is on a short pause, not out. What actually runs out is the
    weekly allowance, added up across the accounts — that number is the answer
    to "can I keep working", and nothing else in the interface was saying it.
    """
    readable = [p for p in rows if p["status"] == OK and p["limits"]]
    reserve = sum(100 - l["percent"]
                  for p in readable for l in p["limits"] if l["kind"] == "weekly_all")
    spent = [p for p in readable if (worst_general(p) or 100) >= state["max_usage"]]
    waiting = first_to_recover(spent, state["max_usage"])
    return {
        "usable": len(usable_accounts(rows, state["max_usage"])),
        "total": len(rows),
        # In whole accounts: 191% is nearly two untouched weeks of allowance.
        "weekly_reserve": reserve,
        "next_free": ({"name": waiting["name"],
                       "in_sec": seconds_until_epoch(
                           recovers_at(waiting, state["max_usage"]))}
                      if waiting else None),
    }


def fleet_line(summary):
    parts = ["%d/%d usable" % (summary["usable"], summary["total"]),
             "%d%% weekly in reserve" % summary["weekly_reserve"]]
    if summary["next_free"]:
        parts.append("%s frees up in %s" % (summary["next_free"]["name"],
                                            until(summary["next_free"]["in_sec"])))
    return " · ".join(parts)


def runner_up(rows, state):
    """The best account other than the one in use.

    Shown when we are staying put, so the line says where you would go next
    instead of repeating the account you are already on.
    """
    active = active_row(rows)
    others = [p for p in rows if not active or p["name"] != active["name"]]
    return pick_target(others, state)


def plan_switch(rows, state):
    """What the next session gets: (target, why, blocked_by).

    `target` is always the best account to be on, even when we won't move to
    it — the interface should name it rather than guess. `blocked_by` says
    what stops the automatic switch, and is None when it would go ahead.
    """
    active = active_row(rows)
    target = pick_target(rows, state)
    if target is None:
        # Everything is spent. The account to name is the one whose window
        # reopens first, with the wait — that is the only actionable fact.
        waiting = first_to_recover(rows, state["max_usage"])
        if waiting:
            return (waiting,
                    "everything is spent — first to free up, in %s"
                    % until(seconds_until_epoch(recovers_at(waiting, state["max_usage"]))),
                    "waiting for a reset")
        return None, "no account can be read right now", "nothing to switch to"
    if active is None or target["name"] == active["name"]:
        return target, "already the best account available", None

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


# --- managing profiles -------------------------------------------------------

LIVE_SERVICE = "Claude Code-credentials"
CLAUDE_JSON = os.path.expanduser("~/.claude.json")


def _point_at(name):
    """Aim ~/.claude.json at a profile, atomically.

    Claude Code reads that one path, so a profile is active precisely when the
    link points at its file. Replacing the link rather than writing through it
    means a session reading mid-switch sees one file or the other, never a
    half-written one.
    """
    if os.path.exists(CLAUDE_JSON) and not os.path.islink(CLAUDE_JSON):
        raise RuntimeError("%s is a real file, not a profile link — refusing to "
                           "replace it" % CLAUDE_JSON)
    staging = "%s.switching.%d" % (CLAUDE_JSON, os.getpid())
    with contextlib.suppress(OSError):
        os.unlink(staging)           # a previous run of *this* pid, or a crash
    try:
        os.symlink(profile_config(name), staging)
        os.replace(staging, CLAUDE_JSON)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(staging)
        raise


def switch(name):
    """Make `name` the active profile.

    The outgoing profile's credentials are saved first: Claude Code refreshes
    the live slot as it runs, so the copy sitting in the profile is behind by
    however long that profile was active.

    Held under a lock, and the current profile and live credentials are read
    inside it. Two switches overlapping — a click landing on an automatic
    tick — otherwise let the second one save the *incoming* account's
    credentials over the outgoing profile's, destroying a refresh token that
    exists nowhere else.
    """
    if name not in profiles():
        return False
    with guard("switch"):
        current = active_profile()
        if current == name:
            return True

        target = keychain_read(profile_service(name))
        if not target:
            return False
        live = keychain_read(LIVE_SERVICE)
        if current and live:
            keychain_write(profile_service(current), live)

        if not keychain_write(LIVE_SERVICE, target):
            return False
        try:
            _point_at(name)
        except (OSError, RuntimeError):
            if live:                       # put the live slot back as it was
                keychain_write(LIVE_SERVICE, live)
            return False

        _write_json(CONFIG, dict(_read_json(CONFIG, {}), active_profile=name), indent=2)
        _CONFIG_CACHE.clear()
        return True


def add_profile(name):
    """Save the credentials in use right now as a new profile."""
    with guard("switch"):
        if name in profiles():
            raise ValueError("profile %r already exists" % name)
        live = keychain_read(LIVE_SERVICE)
        if not live:
            raise RuntimeError("no credentials in use to save")
        keychain_write(profile_service(name), live)
        _write_json(profile_config(name), _read_json(CLAUDE_JSON, {}), indent=2)


def rename_profile(old, new):
    with guard("switch"):
        if old not in profiles():
            raise ValueError("no profile named %r" % old)
        if new in profiles():
            raise ValueError("profile %r already exists" % new)
        credentials = keychain_read(profile_service(old))
        if credentials:
            keychain_write(profile_service(new), credentials)
            keychain_delete(profile_service(old))
        os.rename(os.path.dirname(profile_config(old)), os.path.dirname(profile_config(new)))
        with contextlib.suppress(OSError):
            os.unlink(lock_path("refresh-" + old))
        _CONFIG_CACHE.clear()
        if active_profile() == old:
            _point_at(new)
            _write_json(CONFIG, dict(_read_json(CONFIG, {}), active_profile=new), indent=2)


def remove_profile(name):
    with guard("switch"):
        if name not in profiles():
            raise ValueError("no profile named %r" % name)
        if active_profile() == name:
            raise ValueError("%r is in use — switch away from it first" % name)
        keychain_delete(profile_service(name))
        shutil.rmtree(os.path.dirname(profile_config(name)), ignore_errors=True)
        with contextlib.suppress(OSError):
            os.unlink(lock_path("refresh-" + name))
        _CONFIG_CACHE.clear()
        forget(name)


def auto_tick(rows=None):
    """One turn of the auto loop. Returns (rows, switched, message, plan).

    The plan comes back so callers can render it without working it out a
    second time over the same rows.
    """
    state = load_state()
    if rows is None:
        rows = collect()
    plan = plan_switch(rows, state)
    target, why, blocked = plan
    active = active_row(rows)

    if not state["enabled"] or blocked or staying(rows, plan) or target is None:
        return rows, False, blocked or why, plan
    if not switch(target["name"]):
        return rows, False, "`claudini use %s` failed" % target["name"], plan

    update_state(last_switch=dt.datetime.now().timestamp())
    record({"switch": {"from": active["name"], "to": target["name"], "why": why}})
    # Only the active account changed: no need to read everything again.
    for row in rows:
        row["active"] = (row["name"] == target["name"])
    return rows, True, "%s -> %s (%s)" % (active["name"], target["name"], why), plan


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
        creds = keychain_read(sorted(created)[0])

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
        # The throwaway config dir gets its own keychain entry holding a real,
        # working refresh token. Removing it mid-body left it behind on every
        # path that returned early, and a stranded credential is invisible —
        # nobody would ever notice. Every entry this login created goes, not
        # just the first one found.
        for stray in claude_credential_services() - before:
            keychain_delete(stray)
        shutil.rmtree(workdir, ignore_errors=True)
        forget(name)


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


def seconds_until_epoch(epoch):
    """Seconds until a unix timestamp. None when there isn't one."""
    return None if epoch is None else max(0, int(epoch - dt.datetime.now().timestamp()))


def seconds_until(iso):
    """Seconds until an ISO date from the API. None if absent or unreadable."""
    return seconds_until_epoch(epoch_of(iso))


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


def render(rows, plan=None):
    state = load_state()
    target, why, blocked = plan or plan_switch(rows, state)
    print("\033[1mmode\033[0m %s   \033[1mauto\033[0m %s   \033[1mnext\033[0m %s — %s"
          % (state["mode"], "on" if state["enabled"] else "off",
             target["name"] if target else "?", blocked or why))
    print("\033[2m%s\033[0m" % fleet_line(fleet(rows, state)))
    left = throttled_for()
    if left:
        print("\033[33m%s\033[0m" % throttle_notice(left))
    print()
    ansi = {"critical": "\033[31m", "warning": "\033[33m", "ok": "\033[32m"}
    for r in ranked(rows, state):
        head = "%s %-14s %s" % ("●" if r["active"] else "○", r["name"], r["email"] or "?")
        if r.get("space"):
            head += "  ·  %s" % r["space"]
        if plan_label(r):
            head += " (%s)" % plan_label(r)
        if r.get("source") == "local":
            head += "  \033[2m· read from disk\033[0m\033[1m"
        print("\033[1m%s\033[0m" % head)

        if r["status"] != OK:
            print("    %s" % status_text(r))
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


def binding_limit(p):
    """The general window that will stop you first — the one worth showing.

    Which one it is changes through the day: the 5-hour window is usually the
    binding one, but late in the week the weekly window takes over.
    """
    general = [l for l in p["limits"] if l["kind"] in GENERAL_KINDS]
    if not general:
        return None
    worst = max(general, key=lambda l: l["percent"])
    return {"label": SHORT_LABELS.get(worst["kind"], worst["label"]),
            "percent": worst["percent"],
            "headroom": 100 - worst["percent"],
            "level": level(worst["percent"]),
            "resets_at_epoch": epoch_of(worst["resets_at"])}


def for_json(rows, state, plan=None):
    """The contract with the menu bar app.

    Every key here is named on purpose. Spreading the internal row instead
    published whatever the row happened to carry, which is how two derived
    fields ended up being computed for nobody.
    """
    target, why, blocked = plan or plan_switch(rows, state)
    pause = throttled_for()
    put_off = staying(rows, (target, why, blocked))
    preferred = {r["name"]: preferred_limit(r) for r in rows}
    return {
        "profiles": [{
            "name": r["name"],
            "email": r["email"],
            "active": r["active"],
            "status": r["status"],
            "detail": status_text(r),
            "source": r.get("source"),
            "space": r.get("space"),
            "plan_label": plan_label(r),
            "limit_reset": r.get("limit_reset"),
            "needs_login": needs_login(r),
            "binding": binding_limit(r),
            "saturated": saturated_models(r),
            "limits": [{
                "kind": l["kind"],
                "label": l["label"],
                "short_label": SHORT_LABELS.get(l["kind"], l["label"]),
                "percent": l["percent"],
                "level": level(l["percent"]),
                "resets_at_epoch": epoch_of(l["resets_at"]),
                # The app groups on these rather than restating which kinds
                # are general and which limit is the preferred model.
                "general": l["kind"] in GENERAL_KINDS,
                "preferred": l is preferred[r["name"]],
            } for l in r["limits"]],
        } for r in ranked(rows, state)],
        "auto": state["enabled"],
        "actions": actions_json(),
        "fleet": fleet_line(fleet(rows, state)),
        "mode": state["mode"],
        "modes": [{"name": m, "title": mode_title(m)} for m in MODES],
        # Only meaningful while the policy is protecting that model.
        "show_saturated": state["mode"] == MODE_MODEL,
        "throttled_for": pause,
        "throttle_notice": throttle_notice(pause) if pause else None,
        # The app polls on this rather than hardcoding a copy of the period.
        "poll_after_sec": POLL_SEC,
        "next": {"name": target["name"] if target else None,
                 "reason": why, "blocked_by": blocked,
                 "staying": put_off,
                 # Where you would go if this account ran out, so the line is
                 # worth reading even when nothing is about to change.
                 "after": (runner_up(rows, state) or {}).get("name") if put_off else None},
    }


# Named once, so the help text, the menu and the code cannot drift apart.
ACTIONS = [
    ("", "what every account has left, as a table"),
    ("--json", "the same, machine-readable"),
    ("--force", "re-read now, skipping the per-account waiting periods"),
    ("--switch NAME", "make that profile the active one"),
    ("--reconnect NAME", "log a profile back in, active account untouched"),
    ("--add NAME", "save the credentials in use now as a new profile"),
    ("--rename OLD NEW", "rename a profile"),
    ("--remove NAME", "delete a profile (refused while it is in use)"),
    ("--auto on|off", "arm or disarm automatic switching"),
    ("--mode model|endurance", "which goal the policy optimises for"),
    ("--tick", "run one auto-switch decision now"),
    ("--history", "write and open the usage history page"),
    ("--actions", "this list, as JSON"),
]


def actions_json():
    return [{"command": command, "about": about} for command, about in ACTIONS]

def main():
    args = sys.argv[1:]
    command = args[0] if args else None

    if command == "--switch":
        sys.exit(0 if switch(args[1]) else 1)

    if command == "--reconnect":
        reconnect(args[1])
        return

    if command == "--history":
        # Loaded by path: the entry point is a symlink, so the script's
        # directory is not where its siblings live.
        beside = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                              "claudini_history.py")
        spec = importlib.util.spec_from_file_location("claudini_history", beside)
        page = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(page)
        path = page.write()
        print(path)
        subprocess.run(["/usr/bin/open", path])
        return

    if command == "--actions":
        json.dump(actions_json(), sys.stdout)
        return

    if command in ("--add", "--rename", "--remove"):
        try:
            if command == "--add":
                add_profile(args[1])
            elif command == "--rename":
                rename_profile(args[1], args[2])
            else:
                remove_profile(args[1])
        except (ValueError, RuntimeError, OSError) as e:
            sys.exit(str(e))
        print("%s: done" % command.lstrip("-"))
        return

    if command == "--auto":                      # --auto on | off | status
        state = load_state()
        if len(args) > 1 and args[1] in ("on", "off"):
            state = update_state(enabled=args[1] == "on")
        if "--json" in args:
            json.dump(for_json(collect(), state), sys.stdout)
            return
        print("auto: %s (%s mode)" % ("on" if state["enabled"] else "off", state["mode"]))
        return

    if command == "--mode":                      # --mode model | endurance
        state = load_state()
        if len(args) > 1:
            if args[1] not in MODES:
                sys.exit("mode must be one of: %s" % ", ".join(MODES))
            state = update_state(mode=args[1])
        if "--json" in args:
            json.dump(for_json(collect(), state), sys.stdout)
            return
        print("mode: %s" % state["mode"])
        return

    if command == "--tick":                      # one decision, on its own
        print(auto_tick()[2])
        return

    rows, plan = collect("--force" in args), None
    # `--json --tick`: one API pass feeding both the auto loop and the display.
    if "--tick" in args:
        rows, _, _, plan = auto_tick(rows)

    if "--json" in args:
        json.dump(for_json(rows, load_state(), plan), sys.stdout)
    else:
        render(rows, plan)


if __name__ == "__main__":
    main()
