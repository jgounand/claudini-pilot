#!/usr/bin/env python3
"""
claudini_usage — lit la conso (limites d'abonnement) de chaque profil claudini.

Chaque profil claudini garde ses credentials OAuth dans le trousseau macOS
sous le service "claudini-profile-<nom>". On lit ce token, on interroge
/api/oauth/usage, et on rend un tableau (ou du JSON pour l'app menu bar).

Usage:
  claudini_usage.py            # tableau lisible
  claudini_usage.py --json     # JSON pour ClaudiniBar
  claudini_usage.py --switch <profil>
"""

import concurrent.futures
import threading
import datetime as dt
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CLAUDINI_HOME = os.path.expanduser("~/.claudini")
PROFILES_DIR = os.path.join(CLAUDINI_HOME, "profiles")
CONFIG = os.path.join(CLAUDINI_HOME, "config.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_URL = "https://console.anthropic.com/v1/oauth/token"
# client_id public de Claude Code (visible dans l'URL de login OAuth)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"

ACCOUNT = getpass.getuser()
REFRESH_LOCK = threading.Lock()

STATE_FILE = os.path.join(CLAUDINI_HOME, "auto.json")
CACHE_FILE = os.path.join(CLAUDINI_HOME, "usage-cache.json")

DEFAULT_STATE = {
    "enabled": False,      # bascule automatique active ?
    "min_margin": 5,       # % de marge en dessous duquel un compte est considéré à sec
    "cooldown_min": 10,    # délai minimum entre deux bascules
    "last_switch": 0,
    "last_switch_to": None,
}

# Un compte qui refuse de se rafraîchir le refera au prochain quart d'heure,
# pas à chaque tick : l'endpoint de refresh est vite rate-limité.
FAIL_TTL = 15 * 60


def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


CACHE_VERSION = 2


def load_cache():
    """Cache des lectures ratées. Estampillé : une entrée d'une version
    antérieure n'a pas les mêmes champs, on la jette."""
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("profiles", {})


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": CACHE_VERSION, "profiles": cache}, f)
    os.replace(tmp, CACHE_FILE)


# --- trousseau ---------------------------------------------------------------

def keychain_read(service):
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", ACCOUNT, "-w"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def keychain_write(service, payload):
    """-U met à jour l'entrée existante en conservant sa ACL."""
    r = subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U",
         "-s", service, "-a", ACCOUNT, "-w", json.dumps(payload)],
        capture_output=True, text=True)
    return r.returncode == 0


# --- profils -----------------------------------------------------------------

def active_profile():
    try:
        with open(CONFIG) as f:
            return json.load(f).get("active_profile")
    except OSError:
        return None


# `/limit-reset` (remise à zéro de la fenêtre de 5 h, une fois par semaine) est
# ouvert compte par compte côté serveur. Claude Code met la décision en cache
# dans le claude.json du profil ; on la relit pour dire où la commande existe.
LIMIT_RESET_FLAG = "tengu_nifty_lemur"


def profile_meta(name):
    """(email, organisation, /limit-reset dispo) — dispo vaut None si inconnu."""
    path = os.path.join(PROFILES_DIR, name, "claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None, None
    acct = data.get("oauthAccount") or {}
    flag = (data.get("cachedGrowthBookFeatures") or {}).get(LIMIT_RESET_FLAG)
    reset = flag.get("enabled") if isinstance(flag, dict) else None
    return acct.get("emailAddress"), acct.get("organizationName"), reset


def profiles():
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(d for d in os.listdir(PROFILES_DIR)
                  if os.path.isfile(os.path.join(PROFILES_DIR, d, "claude.json")))


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
    """Rafraîchit un access token expiré et réécrit le trousseau.

    Renvoie (token, erreur) — erreur non nulle si le refresh a échoué.
    """
    oauth = creds["claudeAiOauth"]
    if not oauth.get("refreshToken"):
        return None, "pas de refresh token"

    # Les refresh sont rares et l'endpoint est vite rate-limité : un à la fois.
    with REFRESH_LOCK:
        try:
            new = http_json(REFRESH_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": CLIENT_ID,
            })
        except urllib.error.HTTPError as e:
            return None, "rate limited, reessayer" if e.code == 429 else "reconnexion requise"
        except Exception:
            return None, "refresh injoignable"

    oauth["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        oauth["refreshToken"] = new["refresh_token"]
    if new.get("expires_in"):
        oauth["expiresAt"] = int((dt.datetime.now().timestamp() + new["expires_in"]) * 1000)
    keychain_write(service, creds)
    return oauth["accessToken"], None


def fetch(name, is_active):
    email, org, limit_reset = profile_meta(name)
    out = {"name": name, "email": email, "org": org, "active": is_active,
           "limit_reset": limit_reset, "status": "ok", "limits": []}

    # Le profil actif est aussi dans l'entrée vivante de Claude Code, tenue à
    # jour en continu — on la préfère à l'instantané claudini.
    service = "claudini-profile-" + name
    creds = keychain_read("Claude Code-credentials") if is_active else None
    if creds is None:
        creds = keychain_read(service)
    if creds is None or "claudeAiOauth" not in creds:
        out["status"] = "no-credentials"
        return out

    token = creds["claudeAiOauth"].get("accessToken")
    expired = creds["claudeAiOauth"].get("expiresAt", 0) / 1000 < dt.datetime.now().timestamp()
    if expired and not is_active:
        fresh, err = refresh(service, creds)
        if not fresh:
            out["status"] = err
            return out
        token = fresh

    try:
        data = http_json(USAGE_URL, token=token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and not is_active:
            token, err = refresh(service, creds)
            if not token:
                out["status"] = err
                return out
            try:
                data = http_json(USAGE_URL, token=token)
            except Exception:
                out["status"] = "reconnexion requise"
                return out
        else:
            out["status"] = "erreur HTTP %d" % e.code
            return out
    except Exception as e:
        out["status"] = "injoignable (%s)" % type(e).__name__
        return out

    for lim in data.get("limits") or []:
        scope = (lim.get("scope") or {}).get("model") or {}
        out["limits"].append({
            "kind": lim.get("kind"),
            "label": scope.get("display_name") or {
                "session": "Session (5h)",
                "weekly_all": "Semaine (tous modèles)",
            }.get(lim.get("kind"), lim.get("kind")),
            "percent": lim.get("percent") or 0,
            "severity": lim.get("severity"),
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


def collect():
    """Conso de tous les profils. Les comptes en échec sont mis en quarantaine
    FAIL_TTL secondes pour ne pas marteler l'endpoint de refresh."""
    act = active_profile()
    cache = load_cache()
    now = dt.datetime.now().timestamp()

    def one(name):
        cached = cache.get(name)
        if (cached and cached.get("status") != "ok"
                and now - cached.get("at", 0) < FAIL_TTL and name != act):
            row = dict(cached["row"])
            row["cached"] = True
            return row
        row = fetch(name, name == act)
        cache[name] = {"at": now, "status": row["status"], "row": row}
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, profiles()))
    save_cache(cache)
    return rows


# --- politique de bascule ----------------------------------------------------

GENERAL_KINDS = ("session", "weekly_all")


def general_headroom(p):
    """Marge sur les limites générales (session 5h + semaine), en %."""
    general = [l for l in p["limits"] if l["kind"] in GENERAL_KINDS]
    if p["status"] != "ok" or not general:
        return -1
    return 100 - max(l["percent"] for l in general)


def model_headroom(p, model="Fable"):
    """Marge sur le quota hebdo dédié à un modèle. -1 si le compte est muet."""
    if p["status"] != "ok":
        return -1
    for l in p["limits"]:
        if l["kind"] not in GENERAL_KINDS and l["label"].lower() == model.lower():
            return 100 - l["percent"]
    # Pas de quota dédié annoncé = rien qui bloque ce modèle.
    return 100 if p["limits"] else -1


def pick_target(rows, min_margin, model="Fable"):
    """Le compte sur lequel il faudrait être.

    D'abord ceux qui ont encore du Fable ; sinon le plus frais tous modèles
    confondus. Dans les deux cas on écarte ceux dont les limites générales
    sont déjà au plafond — un compte plein de Fable mais à 0% de session
    n'avance à rien.
    """
    usable = [p for p in rows if p["status"] == "ok" and general_headroom(p) > min_margin]
    if not usable:
        return None
    with_model = [p for p in usable if model_headroom(p, model) > 0]
    if with_model:
        return max(with_model, key=lambda p: model_headroom(p, model))
    return max(usable, key=general_headroom)


def should_switch(active, target, min_margin, model="Fable"):
    """On ne bascule que si le compte courant est réellement bloqué."""
    if target is None or active is None or target["name"] == active["name"]:
        return False, ""
    if active["status"] != "ok":
        return True, "compte courant injoignable"
    if general_headroom(active) <= min_margin:
        return True, "limites generales au plafond (%d%% de marge)" % general_headroom(active)
    if model_headroom(active, model) <= 0 < model_headroom(target, model):
        return True, "%s epuise ici, dispo sur %s" % (model, target["name"])
    return False, ""


def switch(name):
    return subprocess.run(["claudini", "use", name],
                          capture_output=True, text=True).returncode == 0


def auto_tick(rows=None, force=False):
    """Un tour de la boucle auto. Renvoie (a_bascule, message)."""
    state = load_state()
    if not state["enabled"] and not force:
        return False, "auto desactive"

    if rows is None:
        rows = collect()
    active = next((p for p in rows if p["active"]), None)
    target = pick_target(rows, state["min_margin"])
    go, why = should_switch(active, target, state["min_margin"])
    if not go:
        return False, why or "rien a faire"

    waited = dt.datetime.now().timestamp() - state["last_switch"]
    if not force and waited < state["cooldown_min"] * 60:
        return False, "cooldown (%d min restantes)" % ((state["cooldown_min"] * 60 - waited) // 60 + 1)

    if not switch(target["name"]):
        return False, "echec de `claudini use %s`" % target["name"]

    state["last_switch"] = dt.datetime.now().timestamp()
    state["last_switch_to"] = target["name"]
    save_state(state)
    return True, "%s -> %s (%s)" % (active["name"] if active else "?", target["name"], why)


# --- rendu -------------------------------------------------------------------

def until(iso):
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    delta = t - dt.datetime.now(dt.timezone.utc)
    mins = int(delta.total_seconds() // 60)
    if mins <= 0:
        return "maintenant"
    if mins < 60:
        return "%dmin" % mins
    if mins < 60 * 24:
        return "%dh%02d" % (mins // 60, mins % 60)
    return "%dj" % (mins // 1440)


def bar(pct):
    filled = int(round(pct / 10.0))
    return "█" * filled + "░" * (10 - filled)


def render(rows):
    for r in rows:
        mark = "●" if r["active"] else "○"
        head = "%s %-14s %s" % (mark, r["name"], r["email"] or "?")
        print("\033[1m%s\033[0m" % head)
        if r["status"] != "ok":
            print("    %s" % r["status"])
        for lim in r["limits"]:
            pct = lim["percent"]
            color = "\033[31m" if pct >= 95 else "\033[33m" if pct >= 75 else "\033[32m"
            print("    %-24s %s%s %3d%%\033[0m  reset dans %s"
                  % (lim["label"], color, bar(pct), pct, until(lim["resets_at"])))
        if r.get("limit_reset"):
            print("    \033[36m/limit-reset disponible\033[0m       "
                  "remet la fenêtre de 5 h à zéro · 1×/semaine · puise dans le quota hebdo")
        if r.get("extra_credits"):
            e = r["extra_credits"]
            print("    Crédits extra           %s / %s %s"
                  % (e["used"], e["limit"], e["currency"] or ""))
        print()


def main():
    args = sys.argv[1:]

    if args and args[0] == "--switch":
        sys.exit(0 if switch(args[1]) else 1)

    if args and args[0] == "--auto":            # --auto on | off | status
        state = load_state()
        if len(args) > 1 and args[1] in ("on", "off"):
            state["enabled"] = args[1] == "on"
            save_state(state)
        print("auto: %s" % ("actif" if state["enabled"] else "inactif"))
        return

    if args and args[0] == "--tick":            # un tour de boucle seul
        moved, msg = auto_tick(force="--force" in args)
        print(msg)
        return

    rows = collect()
    # `--json --tick` : une seule passe API pour la boucle auto et l'affichage.
    if "--tick" in args:
        moved, msg = auto_tick(rows)
        if moved:
            rows = collect()

    if "--json" in args:
        state = load_state()
        target = pick_target(rows, state["min_margin"])
        json.dump({"active": active_profile(), "profiles": rows,
                   "auto": state["enabled"], "suggestion": target["name"] if target else None},
                  sys.stdout)
    else:
        render(rows)


if __name__ == "__main__":
    main()
