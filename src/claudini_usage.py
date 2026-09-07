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
BOOTSTRAP_URL = "https://api.anthropic.com/api/claude_cli/bootstrap"
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

SUCCESS_TTL = 45       # une lecture réussie reste servie telle quelle ce temps
ORG_TTL = 24 * 3600    # l'organisation d'un compte ne bouge pas : relue une fois par jour
FAIL_TTL = 15 * 60     # un compte en échec n'est pas réessayé avant
THROTTLE_SEC = 180     # après un 429, on ne touche plus du tout à l'API
RATE_LIMITED = "rate limited"


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


CACHE_VERSION = 3
EMPTY_CACHE = {"profiles": {}, "orgs": {}, "throttled_until": 0}


def load_cache():
    """Dernière lecture connue de chaque profil, plus la date jusqu'à laquelle
    l'API nous a demandé de nous taire. Estampillé : une entrée écrite par une
    version antérieure n'a pas les mêmes champs, on la jette."""
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(EMPTY_CACHE)
    if data.get("version") != CACHE_VERSION:
        return dict(EMPTY_CACHE)
    return {"profiles": data.get("profiles", {}),
            "orgs": data.get("orgs", {}),
            "throttled_until": data.get("throttled_until", 0)}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(cache, version=CACHE_VERSION), f)
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
            return None, RATE_LIMITED if e.code == 429 else "reconnexion requise"
        except Exception:
            return None, "refresh injoignable"

    oauth["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        oauth["refreshToken"] = new["refresh_token"]
    if new.get("expires_in"):
        oauth["expiresAt"] = int((dt.datetime.now().timestamp() + new["expires_in"]) * 1000)
    keychain_write(service, creds)
    return oauth["accessToken"], None


def fetch_org(token):
    """Organisation et formule réelles du compte.

    Le claude.json d'un profil contient bien un nom d'organisation, mais il
    peut avoir été écrasé par une session tournant sur un autre profil : seul
    le serveur fait foi.
    """
    try:
        acct = (http_json(BOOTSTRAP_URL, token=token) or {}).get("oauth_account") or {}
    except Exception:
        return None
    org, email = acct.get("organization_name"), acct.get("account_email")
    # "<email>'s Organization" = l'espace personnel, pas une vraie organisation.
    personal = bool(org and email and org.startswith(email))
    return {
        "org": org,
        "space": "perso" if personal else org,
        "plan": (acct.get("organization_type") or "").replace("claude_", ""),
    }


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
        elif e.code == 429:
            out["status"] = RATE_LIMITED
            return out
        else:
            out["status"] = "erreur HTTP %d" % e.code
            return out
    except Exception as e:
        out["status"] = "injoignable (%s)" % type(e).__name__
        return out

    known = load_cache().get("orgs", {}).get(name)
    if known and dt.datetime.now().timestamp() - known["at"] < ORG_TTL:
        out.update(known["info"])
    else:
        info = fetch_org(token)
        if info:
            out.update(info)
            cache = load_cache()
            cache.setdefault("orgs", {})[name] = {"at": dt.datetime.now().timestamp(),
                                                  "info": info}
            save_cache(cache)

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
    """Conso de tous les profils, en tapant sur l'API le moins possible.

    Une lecture réussie est resservie pendant SUCCESS_TTL, un compte en échec
    est laissé tranquille FAIL_TTL, et si l'API a renvoyé un 429 on ne
    l'appelle plus du tout tant que le garde-fou n'est pas expiré.
    """
    act = active_profile()
    cache = load_cache()
    entries = cache["profiles"]
    now = dt.datetime.now().timestamp()
    muted = now < cache["throttled_until"]

    def cached_row(name):
        entry = entries.get(name)
        if not entry:
            return None
        row = dict(entry["row"])
        row["cached"] = True
        row["age"] = int(now - entry["at"])
        row["active"] = (name == act)
        return row

    def one(name):
        entry = entries.get(name)
        if entry:
            age = now - entry["at"]
            fresh_ok = entry["status"] == "ok" and age < SUCCESS_TTL
            failed_recently = entry["status"] != "ok" and age < FAIL_TTL
            if muted or fresh_ok or failed_recently:
                return cached_row(name)
        if muted:
            email, org, reset = profile_meta(name)
            return {"name": name, "email": email, "org": org, "active": name == act,
                    "limit_reset": reset, "status": RATE_LIMITED, "limits": [],
                    "muted": True}
        row = fetch(name, name == act)
        entries[name] = {"at": now, "status": row["status"], "row": row}
        return row

    # Trois requêtes en vol suffisent : au-delà l'API nous claque la porte.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = [r for r in pool.map(one, profiles()) if r]

    # Seul un vrai 429 réarme le garde-fou : une ligne resservie depuis le
    # cache pendant la pause le prolongerait indéfiniment.
    if any(r["status"] == RATE_LIMITED and not r.get("muted") and not r.get("cached")
           for r in rows):
        cache["throttled_until"] = now + THROTTLE_SEC
    save_cache(cache)
    return rows


def throttled_for():
    """Secondes restantes avant de pouvoir réinterroger l'API. 0 si libre."""
    left = load_cache()["throttled_until"] - dt.datetime.now().timestamp()
    return max(0, int(left))


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


def reconnect(name):
    """Refait le login OAuth d'un profil sans changer de compte actif.

    Le login se fait forcément dans le slot actif : on note le profil courant,
    on met le profil à reconnecter en place le temps du login, on range
    nous-mêmes les credentials fraîches dans son entrée de trousseau, puis on
    remet le slot d'origine — y compris si le login échoue ou si tu fais
    Ctrl-C en plein milieu.
    """
    previous = active_profile()
    known = name in profiles()

    print("\033[1mReconnexion de %s\033[0m" % name)
    if previous and previous != name:
        print("Le compte actif (%s) sera remis en place à la fin." % previous)
    print("\033[33mPendant le login, ne lance pas de nouvelle session `claude` "
          "dans un autre terminal : elle écrirait dans le mauvais profil.\033[0m\n")

    try:
        if not known:
            # Profil inconnu : claudini sait le créer et lancer le login.
            subprocess.run(["claudini", "profile", "add", name, "--login"])
        else:
            if previous != name and not switch(name):
                print("\033[31mImpossible de passer sur %s.\033[0m" % name)
                return
            subprocess.run(["claude", "auth", "login"])
            # On range les credentials nous-mêmes plutôt que de parier sur ce
            # que claudini enregistre en quittant le profil.
            creds = keychain_read("Claude Code-credentials")
            if creds and "claudeAiOauth" in creds:
                keychain_write("claudini-profile-" + name, creds)
                print("credentials de %s enregistrées" % name)
            else:
                print("\033[31mAucune credential lisible après le login.\033[0m")
    except KeyboardInterrupt:
        print("\ninterrompu")
    finally:
        if previous and active_profile() != previous:
            print("\nRetour sur %s…" % previous)
            if not switch(previous):
                print("\033[31mLe retour sur %s a échoué — fais `claudini use %s`.\033[0m"
                      % (previous, previous))

        # La lecture en cache de ce profil ne vaut plus rien.
        cache = load_cache()
        cache["profiles"].pop(name, None)
        cache.get("orgs", {}).pop(name, None)
        save_cache(cache)


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
    left = throttled_for()
    if left:
        print("\033[33mAPI en pause %ds (429) — affichage depuis le cache\033[0m\n" % left)
    for r in rows:
        mark = "●" if r["active"] else "○"
        space = r.get("space")
        head = "%s %-14s %s%s" % (mark, r["name"], r["email"] or "?",
                                  ("  ·  %s" % space) if space else "")
        if r.get("plan"):
            head += " (%s)" % r["plan"]
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

    if args and args[0] == "--reconnect":
        reconnect(args[1])
        return

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
                   "auto": state["enabled"], "throttled_for": throttled_for(),
                   "suggestion": target["name"] if target else None},
                  sys.stdout)
    else:
        render(rows)


if __name__ == "__main__":
    main()
