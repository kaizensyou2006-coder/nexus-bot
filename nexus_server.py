#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================
NEXUS SERVER  —  Serveur tout-en-un (a heberger sur Google Cloud 24/7)
========================================================================
1) BOT DISCORD — salon d'IMPORT (DISCORD_CHANNEL) : tu deposes PDF / capture
   / releve NSIA. Le bot detecte CHAQUE depense, retrait et frais, evite les
   doublons (cle = reference) et les note automatiquement sur ton dashboard.
2) BOT DISCORD — salon PANNEAU (PANEL_CHANNEL) : un panneau de controle avec
   boutons (Rapport, Recap MoMo, NSIA, Bitget live, Synchroniser, Nettoyer
   doublons, Etat serveur, Ouvrir l'app) + slash-commands /panel et /rapport.
3) PROXY BITGET SECURISE : signe les requetes Bitget COTE SERVEUR.
4) RECEPTION SMS MoMo : /momo — MacroDroid envoie les SMS du telephone.
5) API : /state, /bitget/<path>, /momo, /momo/inbox, /ping, /health

Config : variables d'environnement OU fichier nexus_config.json.
Cles config utiles : DISCORD_TOKEN, DISCORD_CHANNEL, PANEL_CHANNEL, GUILD_ID,
   PUBLIC_URL, AUTH_TOKEN, BITGET_KEY/SECRET/PASS, OCR_API_KEY, PORT.
========================================================================
"""
import os, sys, json, time, hmac, hashlib, base64, asyncio, re, io, itertools, datetime
import secrets, signal, threading
import urllib.request
from urllib.parse import parse_qs
import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

# ----------------------- Logging -----------------------
# Logs structures (horodatage + niveau) visibles dans les logs Render.
# Niveau ajustable via la variable d'env LOG_LEVEL (INFO par defaut).
import logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nexus")

try:
    import discord
except Exception:
    discord = None

# ----------------------- Config -----------------------
_CFG = {}
try:
    _cfgpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus_config.json")
    with open(_cfgpath, "rb") as _f:
        _b = _f.read()
    for _enc in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
        try:
            _CFG = json.loads(_b.decode(_enc).strip())
            log.info("nexus_config.json charge OK (%d cles, %s)", len(_CFG), _enc)
            break
        except Exception:
            _CFG = {}
    if not _CFG:
        log.warning("nexus_config.json introuvable/illisible — lecture depuis les variables d'environnement.")
except Exception as _e:
    log.warning("Pas de nexus_config.json (%s) — lecture depuis les variables d'environnement.", _e)
    _CFG = {}

def _conf(key, default=""):
    v = os.environ.get(key, "")
    if v:
        return v.strip()
    v = _CFG.get(key, default)
    return (str(v).strip() if v is not None else default)

DISCORD_TOKEN   = _conf("DISCORD_TOKEN")
DISCORD_CHANNEL = _conf("DISCORD_CHANNEL")            # salon d'IMPORT (PDF/captures/NSIA)
PANEL_CHANNEL   = _conf("PANEL_CHANNEL")              # salon du PANNEAU de controle (boutons)
REPORT_CHANNEL  = _conf("REPORT_CHANNEL")             # salon des RETOURS du bot (rapports/recaps/PDF)
UPSTASH_URL     = _conf("UPSTASH_REDIS_REST_URL")     # base de donnees Upstash (persistance)
UPSTASH_TOKEN   = _conf("UPSTASH_REDIS_REST_TOKEN")
GUILD_ID        = _conf("GUILD_ID")                   # optionnel : sync rapide des slash-commands
PUBLIC_URL      = _conf("PUBLIC_URL").rstrip("/")     # ex: http://34.x.x.x:8080  (pour le bouton "Ouvrir l'app")
AUTH_TOKEN      = _conf("AUTH_TOKEN")
# Jeton par defaut connu publiquement (present dans le depot) = aucun secret.
# On le refuse : sans AUTH_TOKEN valide, l'API repond 401 au lieu de s'ouvrir.
AUTH_WEAK       = (not AUTH_TOKEN) or AUTH_TOKEN == "nexus229" or len(AUTH_TOKEN) < 16
BITGET_KEY      = _conf("BITGET_KEY")
BITGET_SECRET   = _conf("BITGET_SECRET")
BITGET_PASS     = _conf("BITGET_PASS")
OCR_API_KEY     = _conf("OCR_API_KEY")
PORT            = int(_conf("PORT") or os.environ.get("SERVER_PORT") or "8080")
USD_XOF         = float(_conf("USD_XOF") or "640")   # taux de repli USD->FCFA si le live echoue
_USD_XOF_LIVE   = USD_XOF                             # taux USD->FCFA en direct (memes source que l'app)
_USD_XOF_TS     = 0                                   # horodatage du dernier rafraichissement
BITGET_BASE     = "https://api.bitget.com"
# Chemins Bitget joignables via le proxy /bitget (GET uniquement) : consultation
# de solde et de prix, rien qui puisse deplacer des fonds.
BITGET_READ_PATHS = (
    "/api/v2/account/all-account-balance",
    "/api/v2/spot/account/assets",
    "/api/v2/spot/market/tickers",
    "/api/v2/earn/account/assets",
    "/api/v2/mix/account/accounts",
    "/api/v2/spot/account/bills",
)
BITGET_ALLOW_WRITE = (_conf("BITGET_ALLOW_WRITE") or "").lower() in ("1", "true", "yes")
GEN_CMD = "python -c 'import secrets;print(secrets.token_urlsafe(32))'"

# ----------------------- IA / Agents (Claude) -----------------------
# La cle Claude vit COTE SERVEUR : l'app appelle /ai (proxy) au lieu d'appeler
# api.anthropic.com directement. Avant, la cle etait dans le navigateur (localStorage
# + en-tete anthropic-dangerous-direct-browser-access) : une XSS, une extension ou un
# acces a l'appareil l'exfiltrait, et elle facture le compte Anthropic sans plafond.
# --- Fournisseur d'IA : "anthropic" (Claude) OU "openai" (compatible OpenAI :
#     DeepSeek, OpenRouter, Groq, Together, Ollama local... = pas besoin de Claude). ---
AI_PROVIDER       = (_conf("AI_PROVIDER") or "anthropic").lower()
ANTHROPIC_API_KEY = _conf("ANTHROPIC_API_KEY") or _conf("CLAUDE_API_KEY")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# fallbacks:"default" (repli serveur sur refus de politique) -> ce header exact.
ANTHROPIC_BETA    = "server-side-fallback-2026-07-01"
# --- Config du mode OpenAI-compatible ---
# Base par defaut : DeepSeek. Exemples :
#   DeepSeek    : https://api.deepseek.com          modele deepseek-chat
#   OpenRouter  : https://openrouter.ai/api/v1      modele deepseek/deepseek-chat-v3.1:free (gratuit)
#   Groq        : https://api.groq.com/openai/v1    modele llama-3.3-70b-versatile
#   Ollama local: http://localhost:11434/v1         modele llama3.1  (aucune cle)
AI_BASE_URL       = (_conf("AI_BASE_URL") or "https://api.deepseek.com").rstrip("/")
AI_API_KEY        = _conf("AI_API_KEY") or _conf("DEEPSEEK_API_KEY") or _conf("OPENROUTER_API_KEY")
AI_JSON_MODE      = (_conf("AI_JSON_MODE") or "1").lower() in ("1", "true", "yes")  # response_format json
_def_model        = "deepseek-chat" if AI_PROVIDER == "openai" else "claude-opus-5"
AI_MODEL          = _conf("AI_MODEL") or _def_model
# Modeles acceptes par le proxy en mode Anthropic (le client ne force pas un modele arbitraire).
AI_MODEL_ALLOW    = {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                     "claude-opus-4-8", "claude-fable-5-1"}
# Ces modeles gerent thinking adaptatif + output_config.effort (pas Haiku 4.5).
AI_THINK_MODELS   = {"claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5-1"}
AI_MAX_TOKENS     = int(_conf("AI_MAX_TOKENS") or "8000")   # plafond de sortie du proxy
AI_EFFORT         = (_conf("AI_EFFORT") or "high").lower()  # low|medium|high|xhigh|max (Anthropic)

def ai_enabled():
    """L'IA est-elle utilisable ? Anthropic -> cle Claude ; OpenAI-compatible -> cle
    fournisseur, OU base locale (Ollama sans cle)."""
    if AI_PROVIDER == "openai":
        return bool(AI_API_KEY) or ("localhost" in AI_BASE_URL) or ("127.0.0.1" in AI_BASE_URL)
    return bool(ANTHROPIC_API_KEY)
AI_BRIEF_HOUR     = int(_conf("AI_BRIEF_HOUR") or "8")      # heure (WAT) du brief auto
AI_BRIEF_EVERY    = int(_conf("AI_BRIEF_EVERY_DAYS") or "1")  # cadence en jours (1=quotidien)
AI_BRIEF_AUTO     = (_conf("AI_BRIEF_AUTO") or "1").lower() in ("1", "true", "yes")

# Session HTTP partagee (definie au demarrage) — utilisee par le bot Discord pour Bitget/OCR.
HTTP_SESSION = None
START_TS = int(time.time())

STATE_DIR  = _conf("STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(STATE_DIR, "nexus_state.json")

# Compteur global : ids uniques meme si plusieurs items arrivent dans la meme milliseconde
_id_seq = itertools.count()
def new_id():
    return int(time.time() * 1000) * 1000 + (next(_id_seq) % 1000)

# ----------------------- Etat persistant -----------------------
STATE = {
    "patrimoine": None,
    "momo": [],
    "nsia": None,
    "bitget": None,      # {total, ts, holdings:[{coin,amt,val}]}
    "business": None,    # resume revenus/depenses pousse par l'app : {revM,expM,netM,mrr,arr,caTotal,depTotal,cur,ts}
    "objectifs": None,   # budgets/objectifs/dettes/DCA pousses par l'app (agent Objectifs)
    "catexp": None,      # depenses du mois par categorie {nom: montant} (memoire des agents)
    "ai_snaps": [],      # memoire des agents : photos chiffrees successives (comparaison inter-briefs)
    "ai_actions": [],    # file d'actions proposees par l'IA a appliquer dans l'app
    "balances": {},      # soldes captures par reseau : {"mtn":..,"moov":..}
    "panel_msg": None,   # id du message du panneau de controle (pour le re-editer)
    "updatedAt": None,
}

# ----------------------- Dates -----------------------
# datetime.utcnow() est deprecie depuis Python 3.12 (et renvoyait un datetime naif
# qu'on decalait a la main). On travaille en heure du Benin (UTC+1), explicitement.
WAT = datetime.timezone(datetime.timedelta(hours=1))

def now_wat():
    """Maintenant, en heure du Benin (UTC+1)."""
    return datetime.datetime.now(WAT)

def today_wat():
    return now_wat().date()

# ----------------------- Formatage -----------------------
def fmt_xof(n):
    try:
        return format(int(round(float(n))), ",d").replace(",", " ") + " FCFA"
    except Exception:
        return "%s FCFA" % n

def fmt_usd(n):
    try:
        return "${:,.2f}".format(float(n))
    except Exception:
        return "$%s" % n

def _upstash(cmd):
    """Execute une commande Redis via l'API REST Upstash (synchrone). cmd = liste de chaines."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    req = urllib.request.Request(
        UPSTASH_URL.rstrip("/"),
        data=json.dumps(cmd).encode("utf-8"),
        headers={"Authorization": "Bearer " + UPSTASH_TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8")).get("result")

# Vrai/faux : la lecture initiale d'Upstash a-t-elle abouti ?
# Si Upstash est configure mais injoignable au demarrage, l'ancien code repartait
# sur l'etat local (souvent vide sur Render, disque ephemere) PUIS ecrasait la
# base distante a la premiere sauvegarde : tout l'historique disparaissait a cause
# d'une simple coupure reseau. Tant que ce drapeau est faux, on n'ecrit plus rien
# vers Upstash (lecture seule distante) et on le crie dans les logs.
_UPSTASH_OK = True

def load_state():
    global STATE, _UPSTASH_OK
    # 1) Base de donnees Upstash si configuree (persiste meme apres redemarrage)
    if UPSTASH_URL and UPSTASH_TOKEN:
        last = None
        for essai in range(3):                    # 3 essais : un timeout ne doit pas couter l'historique
            try:
                raw = _upstash(["GET", "nexus_state"])
                if raw:
                    STATE.update(json.loads(raw))
                    log.info("etat charge depuis Upstash (%d octets)", len(raw))
                    prune_state()
                    return
                log.info("Upstash joignable mais vide — premier demarrage ?")
                last = None
                break                             # base vide : ce n'est PAS une panne
            except Exception as e:
                last = e
                log.warning("lecture Upstash (essai %d/3): %s", essai + 1, e)
                time.sleep(1.5 * (essai + 1))
        if last is not None:
            _UPSTASH_OK = False
            log.critical("Upstash INJOIGNABLE au demarrage (%s). L'etat distant ne sera PAS "
                         "ecrase (sauvegarde locale uniquement) pour ne pas perdre l'historique. "
                         "Redemarre le service une fois Upstash de nouveau accessible.", last)
    # 2) Sinon fichier local
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE.update(json.load(f))
        log.info("etat charge depuis %s", STATE_FILE)
    except FileNotFoundError:
        log.info("aucun etat local (%s) — demarrage a vide.", STATE_FILE)
    except Exception as e:
        # Fichier present mais illisible/corrompu : le signaler fort, sinon on
        # repart a zero en silence et la premiere sauvegarde ecrase le fichier.
        log.error("etat local illisible (%s) : %s — demarrage a vide.", STATE_FILE, e)
    prune_state()

def prune_state():
    """Remet l'etat dans ses bornes et dans le bon type apres chargement.

    Deux raisons : (1) un etat sauvegarde AVANT l'ajout des plafonds peut contenir
    des dizaines de milliers d'operations ou de points d'historique, qui repartent
    ensuite a chaque sauvegarde ; (2) une valeur d'un type inattendu (base modifiee
    a la main, JSON tronque) faisait planter tous les calculs plus loin."""
    if not isinstance(STATE.get("momo"), list):
        if STATE.get("momo") is not None:
            log.error("etat: 'momo' n'est pas une liste (%s) — reinitialise.", type(STATE.get("momo")).__name__)
        STATE["momo"] = []
    STATE["momo"] = [m for m in STATE["momo"] if isinstance(m, dict)]
    if len(STATE["momo"]) > MOMO_MAX:
        log.warning("etat: %d operations MoMo -> plafonne a %d", len(STATE["momo"]), MOMO_MAX)
        STATE["momo"] = STATE["momo"][-MOMO_MAX:]
    if not isinstance(STATE.get("balances"), dict):
        STATE["balances"] = {}
    hist = STATE.get("history")
    if not isinstance(hist, list):
        hist = []
    hist = [p for p in hist if isinstance(p, dict) and p.get("d")]
    if len(hist) > HISTORY_MAX:
        log.warning("etat: %d points d'historique -> plafonne a %d", len(hist), HISTORY_MAX)
    STATE["history"] = hist[-HISTORY_MAX:]
    if not isinstance(STATE.get("recap_keys"), dict):
        STATE["recap_keys"] = {}
    # alert_flags ne servait qu'a ne pas repeter l'alerte du jour : tout le reste
    # est du poids mort qui grossit indefiniment dans la base.
    prune_alert_flags()

def prune_alert_flags():
    """Ne garde que le drapeau d'alerte du jour (les autres ne servent plus a rien)."""
    flags = STATE.get("alert_flags")
    if not isinstance(flags, dict):
        STATE["alert_flags"] = {}
        return
    key = "alert_" + today_wat().isoformat()
    for k in list(flags):
        if k != key:
            flags.pop(k, None)

# --- Ecriture Upstash asynchrone ---------------------------------------------
# save_state() etait appele depuis les handlers HTTP et les boutons Discord, et
# faisait un urlopen() BLOQUANT (jusqu'a 10 s) : pendant ce temps toute la boucle
# asyncio etait figee — heartbeat Discord compris. C'est la cause la plus probable
# des deconnexions que le backoff ne faisait que rattraper.
# Desormais : le fichier local est ecrit tout de suite (rapide), et l'envoi reseau
# part dans un thread dedie qui regroupe les ecritures rapprochees.
_save_lock    = threading.Lock()
_save_wake    = threading.Event()
_save_payload = {"data": None}
_save_thread  = None

def _save_worker():
    while True:
        _save_wake.wait()
        _save_wake.clear()
        time.sleep(1.0)                      # regroupe les rafales d'ecritures
        with _save_lock:
            data = _save_payload["data"]
            _save_payload["data"] = None
        if data is None:
            continue
        try:
            _upstash(["SET", "nexus_state", data])
        except Exception as e:
            log.warning("upstash save: %s", e)

def flush_state():
    """Pousse l'etat en attente vers Upstash en bloquant. Utilise a l'arret."""
    with _save_lock:
        data = _save_payload["data"]
        _save_payload["data"] = None
    if data is None or not (UPSTASH_URL and UPSTASH_TOKEN) or not _UPSTASH_OK:
        return
    try:
        _upstash(["SET", "nexus_state", data])
        log.info("etat sauvegarde avant l'arret")
    except Exception as e:
        log.error("flush_state: %s", e)

def save_state():
    global _save_thread
    STATE["updatedAt"] = int(time.time() * 1000)
    data = json.dumps(STATE, ensure_ascii=False)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        log.warning("ecriture fichier etat: %s", e)
    if not (UPSTASH_URL and UPSTASH_TOKEN) or not _UPSTASH_OK:
        return
    with _save_lock:
        _save_payload["data"] = data
    if _save_thread is None:
        _save_thread = threading.Thread(target=_save_worker, name="nexus-state", daemon=True)
        _save_thread.start()
    _save_wake.set()

def reset_state():
    """Remet les DONNEES COURANTES a zero (MoMo, NSIA, Bitget, soldes, patrimoine).

    Volontairement CONSERVE :
      - history : le releve quotidien du patrimoine est la seule donnee qu'on ne
        peut pas reconstruire (les operations, elles, se re-scannent depuis le
        salon d'import). L'effacer par confort ferait perdre des mois de courbe.
      - panel_msg : le message du panneau existe toujours cote Discord.
    Renvoie un resume de ce qui a ete efface, pour pouvoir l'annoncer."""
    efface = {"momo": len(STATE.get("momo") or []),
              "nsia": bool(STATE.get("nsia")),
              "bitget": bool(STATE.get("bitget")),
              "balances": len(STATE.get("balances") or {}),
              "patrimoine": bool(STATE.get("patrimoine")),
              "business": bool(STATE.get("business")),
              "history_conserve": len(STATE.get("history") or [])}
    STATE["momo"] = []
    STATE["nsia"] = None
    STATE["bitget"] = None
    STATE["balances"] = {}
    STATE["patrimoine"] = None
    STATE["business"] = None
    STATE["objectifs"] = None
    STATE["catexp"] = None
    STATE["ai_actions"] = []      # ai_snaps (memoire) conserve, comme history
    STATE["recap_keys"] = {}
    STATE["alert_flags"] = {}
    save_state()
    log.warning("RESET des donnees demande : %s", efface)
    return efface

# ----------------------- Bitget : signature serveur -----------------------
def bitget_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(BITGET_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()

async def bitget_request(session, method, path, body=""):
    ts = str(int(time.time() * 1000))
    sign = bitget_sign(ts, method, path, body)
    headers = {
        "ACCESS-KEY": BITGET_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_PASS,
        "Content-Type": "application/json",
        "locale": "fr-FR",
    }
    url = BITGET_BASE + path
    async with session.request(method, url, headers=headers,
                               data=(body if body else None),
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
        text = await r.text()
        return r.status, text

# Chaque appel a bitget_overview() tape 4 endpoints Bitget. Le rapport, le recap,
# le panneau et /solde l'appelaient chacun de leur cote : quelques clics de suite
# suffisaient a frôler la limite de debit. Cache court partage par tous.
_BG_CACHE = {"ts": 0.0, "data": None}
BG_CACHE_TTL = float(_conf("BG_CACHE_TTL") or "60")

async def bitget_overview(session, force=False):
    """Vue complete du compte Bitget en USDT.
    Renvoie un dict {total, spot, earn, others, holdings:[(coin,amt,val)]} ou None.
    'total' = valeur de TOUS les comptes (spot + earn + bots + futures...), pas juste le spot.
    Resultat mis en cache BG_CACHE_TTL secondes ; force=True pour ignorer le cache."""
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return None
    if not force and _BG_CACHE["data"] is not None and time.time() - _BG_CACHE["ts"] < BG_CACHE_TTL:
        return _BG_CACHE["data"]
    try:
        await refresh_usd_xof(session)   # aligne le taux FCFA du Discord sur celui de l'app
    except Exception:
        pass
    res = {"total": 0.0, "spot": 0.0, "earn": 0.0, "others": 0.0, "holdings": [], "change": {}}
    got_total = False
    # 1) Total fiable par type de compte
    try:
        _, txt = await bitget_request(session, "GET", "/api/v2/account/all-account-balance")
        j = json.loads(txt)
        if j.get("code") == "00000":
            for b in j.get("data", []):
                t = str(b.get("accountType", "")).lower()
                v = float(b.get("usdtBalance", 0) or 0)
                res["total"] += v
                if t == "spot":
                    res["spot"] = v
                elif t == "earn":
                    res["earn"] = v
                else:
                    res["others"] += v
            got_total = True
    except Exception as e:
        log.warning("bitget all-account-balance: %s", e)
    # 2) Prix pour valoriser les positions
    prices = {}
    try:
        _, t2 = await bitget_request(session, "GET", "/api/v2/spot/market/tickers")
        for t in json.loads(t2).get("data", []):
            sym = t.get("symbol", "")
            prices[sym] = float(t.get("lastPr", 0) or 0)
            if sym.endswith("USDT"):
                try:
                    res["change"][sym[:-4]] = float(t.get("change24h", t.get("changeUtc24h", 0)) or 0) * 100.0
                except Exception:
                    pass
    except Exception as e:
        log.warning("bitget tickers: %s", e)
    def _val(coin, amt):
        return amt if coin in ("USDT", "USDC", "USD", "BUSD") else amt * prices.get(coin + "USDT", 0.0)
    # 3) Detail Spot
    try:
        _, t3 = await bitget_request(session, "GET", "/api/v2/spot/account/assets")
        for a in json.loads(t3).get("data", []):
            amt = (float(a.get("available", 0) or 0) + float(a.get("frozen", 0) or 0)
                   + float(a.get("locked", 0) or 0))
            if amt > 0:
                c = str(a.get("coin", "")).upper()
                res["holdings"].append((c, amt, _val(c, amt)))
    except Exception as e:
        log.warning("bitget spot assets: %s", e)
    # 4) Detail Earn (epargne / DCA)
    try:
        _, t4 = await bitget_request(session, "GET", "/api/v2/earn/account/assets")
        for e in json.loads(t4).get("data", []):
            amt = float(e.get("amount", 0) or 0)
            if amt > 0:
                c = str(e.get("coin", "")).upper()
                res["holdings"].append((c + " ⟢Earn", amt, _val(c, amt)))
    except Exception as e:
        log.warning("bitget earn assets: %s", e)
    # Fallback : si all-account-balance vide, total = somme des positions
    if not got_total or res["total"] <= 0:
        res["total"] = sum(v for _, _, v in res["holdings"])
    # Calage : le STAKING / On-chain Earn n'est PAS expose par l'API Bitget.
    # On AJOUTE le staking (STATE["bg_calib"]["staking"]) au live (spot + Earn flexible) au lieu de figer le total.
    # -> le spot/earn reste live (trades, DCA, depots se voient tout seuls), seul le staking est fixe.
    try:
        calib = STATE.get("bg_calib") or {}
        staking = calib.get("staking") or {}
        extra = float(calib.get("extra", 0) or 0)
        # Agrege les positions live par coin (spot + earn flexible -> une ligne par coin)
        agg = {}
        for c, amt, val in res["holdings"]:
            base = str(c).split(" ")[0].upper()
            a, v = agg.get(base, (0.0, 0.0))
            agg[base] = (a + amt, v + val)
        stk_val = 0.0
        for coin, qty in (staking or {}).items():
            cu = str(coin).upper()
            qf = float(qty or 0)
            if qf <= 0:
                continue
            v = qf if cu in ("USDT", "USDC", "USD", "BUSD") else qf * prices.get(cu + "USDT", 0.0)
            a, vv = agg.get(cu, (0.0, 0.0))
            agg[cu] = (a + qf, vv + v)   # quantite affichee = live + staking
            stk_val += v
        if staking:
            res["holdings"] = [(c, a, v) for c, (a, v) in agg.items()]
        res["total"] += stk_val + extra
        res["earn"] = max(0.0, res["total"] - res["spot"] - res["others"])  # staking compte comme Earn
    except Exception as e:
        log.warning("bitget calage staking: %s", e)
    res["holdings"].sort(key=lambda x: -x[2])
    _BG_CACHE["data"], _BG_CACHE["ts"] = res, time.time()
    return res

# ----------------------- MoMo : detection transactions -----------------------
# Mots-cles qui donnent le SENS de l'operation (revenu vs depense/retrait).
MOMO_KW_INC = ("recu", "reçu", "reçue", "received", "credite", "crédite", "crédité",
               "credité", "depot", "dépot", "dépôt", "depose", "déposé", "approvision")
MOMO_KW_EXP = ("paiement", "paye", "payé", "achat", "transfert", "transfere", "transféré",
               "envoi", "envoye", "envoyé", "retrait", "retire", "retiré", "debit", "débit",
               "facture", "withdraw", "withdrawal", "souscription", "frais de")

# Un montant FCFA : "5 000", "5,000", "5.000", "12500", insecables compris.
_MONEY = r"\d{1,3}(?:[   .,]\d{3})+|\d+"
_CUR   = r"(?:fcfa|f\.?cfa|xof|cfa|f\b)"

def _money(s):
    """'12 500' / '12,500' / '12.500' -> 12500.0  (XOF = sans centimes)."""
    d = re.sub(r"[^\d]", "", s or "")
    return float(d) if d else 0.0

def _after(label_re, text):
    """Valeur monetaire qui suit un libelle (frais, solde, id...)."""
    m = re.search(label_re + r"\s*[:=]?\s*(" + _MONEY + r")", text, re.I)
    return _money(m.group(1)) if m else None

def _detect_type(low):
    # retrait/paiement/transfert priment si presents en meme temps qu'un "recu" de confirmation
    if any(k in low for k in MOMO_KW_EXP):
        return "exp"
    if any(k in low for k in MOMO_KW_INC):
        return "inc"
    return None

def _parse_line(line):
    """Analyse UNE ligne (ou un SMS court) -> dict operation, ou None."""
    l = line.strip()
    if not l:
        return None
    low = l.lower()
    typ = _detect_type(low)
    if not typ:
        return None
    fee     = _after(r"frais", low) or 0.0
    balance = _after(r"(?:solde|nouveau solde|balance)", low)
    # Reference : le libelle DOIT etre suivi d'un separateur (: . = #) pour eviter
    # de capturer "uveau" dans "Nouveau solde". La ref sert de cle anti-doublon.
    refm = re.search(r"(?:transaction(?:\s*id)?|txn|r[ée]f(?:[ée]rence)?|id|n[°o])\s*[:.#=]\s*([A-Za-z0-9]{4,})", l, re.I)
    ref = refm.group(1) if refm else ""
    # Montant principal : le 1er montant qui n'est ni les frais ni le solde.
    amount = 0.0
    for tok in re.findall(_MONEY, l):
        v = _money(tok)
        if v < 50:
            continue
        if balance is not None and abs(v - balance) < 0.5:
            continue
        if fee and abs(v - fee) < 0.5:
            continue
        amount = v
        break
    if amount <= 0:
        return None
    # Contrepartie (best-effort) : nom apres de/a/chez/to/from (lettres uniquement,
    # coupe aux mots parasites comme "solde", "frais", "effectue"...).
    pm = re.search(r"(?:de|à|a|chez|pour|to|from)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ '\-]{2,40})", l)
    payee = ""
    if pm:
        payee = re.split(r"\b(?:solde|frais|effectu\w*|nouveau|ref|id|txn|transaction|le|la|du|de)\b",
                         pm.group(1), 1, flags=re.I)[0].strip(" .,-")[:60]
    # Date eventuelle dd/mm[/yyyy]
    dm = re.search(r"(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?", l)
    date = None
    if dm:
        d, mo, y = dm.group(1), dm.group(2), dm.group(3)
        if y:
            y = ("20" + y) if len(y) == 2 else y
            try:
                date = "%04d-%02d-%02d" % (int(y), int(mo), int(d))
            except Exception:
                date = None
    return {
        "type": typ, "amount": amount, "fee": fee, "balance": balance,
        "payee": payee, "ref": ref, "date": date, "text": l[:180],
    }

# --- Releve MTN MoMo officiel (PDF tableau "Détails de la transaction") ---
MONTHS_FR = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
             "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
             "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)\s+(\d{4})\s+(\d{1,2}):(\d{2})", re.I)
# Une ligne d'operation = montant SIGNE, ID de transaction (10-12 chiffres), frais ... FCFA
_TX_RE = re.compile(r"([+-]\d+)\s+(\d{10,12})\s+(\d[\d ]*?)\s*FCFA", re.I)

def parse_momo_statement(text):
    """Releve MTN MoMo : le signe du montant donne le sens (- depense, + revenu),
    l'ID de transaction sert de cle anti-doublon. Renvoie une liste de dicts."""
    rows = []
    for m in _TX_RE.finditer(text):
        amt_raw, txid, fee_raw = m.groups()
        amount = _money(amt_raw)
        if amount < 1:
            continue
        typ = "inc" if amt_raw.strip()[0] == "+" else "exp"
        fee = _money(fee_raw)
        before = text[:m.start()]
        dm = None
        for x in _DATE_RE.finditer(before):
            dm = x  # garde la derniere date avant l'operation
        date = None
        if dm:
            d, mois, y, _hh, _mn = dm.groups()
            mo = MONTHS_FR.get(mois.lower())
            if mo:
                date = "%04d-%02d-%02d" % (int(y), mo, int(d))
        rows.append({"type": typ, "amount": amount, "fee": fee, "balance": None,
                     "payee": "", "ref": txid, "date": date,
                     "text": "MoMo %s #%s" % (typ, txid)})
    return rows

def detect_network(text):
    """Detecte l'operateur Mobile Money depuis le texte ('moov' ou 'mtn' par defaut)."""
    return "moov" if re.search(r"moov", text or "", re.I) else "mtn"

def detect_balance(text):
    """Capture d'accueil (MTN/Moov) : extrait le SOLDE -> (net, montant) ou None."""
    m = re.search(r"solde[^\d]{0,18}(\d[\d   .,]*\d|\d)", text or "", re.I)
    if not m:
        return None
    val = _eur(m.group(1))
    if val <= 0:
        return None
    return detect_network(text), val

def _emit_ops(parsed, net="mtn"):
    """Transforme une liste d'operations analysees en entrees MoMo (+ frais separes)."""
    out = []
    base_ts = int(time.time() * 1000)
    label = "Moov" if net == "moov" else "MoMo"
    for p in parsed:
        out.append({
            "id": new_id(), "ts": base_ts, "net": net,
            "type": p["type"], "amount": p["amount"], "cur": "XOF",
            "payee": p.get("payee", ""), "ref": p.get("ref", ""), "date": p.get("date"),
            "balance": p.get("balance"), "text": (p.get("text") or "")[:180], "src": "discord",
        })
        if p.get("fee") and p["fee"] > 0:
            ref = p.get("ref", "")
            out.append({
                "id": new_id(), "ts": base_ts, "net": net,
                "type": "exp", "amount": p["fee"], "cur": "XOF",
                "payee": "Frais " + label, "ref": (ref + "_fee") if ref else "",
                "date": p.get("date"), "balance": None,
                "text": ("Frais — " + (p.get("text") or ""))[:180], "src": "discord",
            })
    return out

def parse_momo_text(text, net=None):
    """Analyse un texte MoMo/Moov. Essaie d'abord le format RELEVE officiel (tableau PDF),
    sinon retombe sur l'analyse ligne-par-ligne (SMS / captures OCR)."""
    if not text:
        return []
    if net is None:
        net = detect_network(text)
    stmt = parse_momo_statement(text)
    if stmt:
        return _emit_ops(stmt, net)
    rows = [p for p in (_parse_line(r) for r in re.split(r"[\n\r]+", text)) if p]
    return _emit_ops(rows, net)

def _dedup_key(e):
    """Cle d'unicite : la reference si dispo, sinon (type, montant, libelle)."""
    ref = (e.get("ref") or "").strip()
    if ref:
        return ("ref", ref)
    return ("txt", e.get("type"), e.get("amount"), (e.get("text") or "")[:120])

def add_momo(items):
    if not items:
        return 0
    existing = {_dedup_key(e) for e in STATE["momo"]}
    n = 0
    for it in items:
        key = _dedup_key(it)
        if key in existing:
            continue
        STATE["momo"].append(it)
        existing.add(key)
        n += 1
    # 800 operations, c'etait ~3 mois d'usage : /recap 365 et les rapports annuels
    # travaillaient sur un historique deja ampute, sans le dire.
    if len(STATE["momo"]) > MOMO_MAX:
        drop = len(STATE["momo"]) - MOMO_MAX
        log.warning("historique MoMo plafonne : %d operation(s) les plus anciennes ecartees", drop)
        STATE["momo"] = STATE["momo"][-MOMO_MAX:]
    if n:
        save_state()
    return n

MOMO_MAX = int(_conf("MOMO_MAX") or "5000")   # operations conservees (~3 ans)

_recent_raw = []  # [(ts, texte)] dedup des SMS bruts recus dans les 5 dernieres minutes

def momo_ingest_text(text, src="sms"):
    """Recoit un SMS brut (MacroDroid -> /momo), deduplique, parse et stocke."""
    text = (text or "").strip()
    if not text:
        return 0
    now = int(time.time() * 1000)
    global _recent_raw
    _recent_raw = [(t, x) for (t, x) in _recent_raw if now - t < 300000]
    if any(x == text for (_, x) in _recent_raw):
        return 0
    _recent_raw.append((now, text))
    items = parse_momo_text(text)
    for it in items:
        it["src"] = src
    n = add_momo(items)
    if not n:
        log.info("SMS recu mais aucun montant detecte: %r", text[:120])
    return n

# ----------------------- NSIA : releve de portefeuille -----------------------
# Nombre avec decimale virgule OU point (tolerant OCR) : "218 248,59", "218 248.59", "9 163,1487".
_NUM = re.compile(r"\d{1,3}(?:[ \u202f\u00a0]\d{3})*[.,]\d+|\d+[.,]\d+")

def _dec_len(tok):
    last = max(tok.rfind(","), tok.rfind("."))
    return len(re.sub(r"[^\d]", "", tok[last + 1:])) if last != -1 else 0

def _eur(s):
    """Convertit un montant (FR ou OCR) en float. Le DERNIER separateur est la decimale."""
    s = re.sub(r"[^\d.,]", "", (s or "").strip())
    if not s:
        return 0.0
    last = max(s.rfind(","), s.rfind("."))
    if last == -1:
        try:
            return float(s)
        except Exception:
            return 0.0
    intpart = re.sub(r"[^\d]", "", s[:last])
    dec = re.sub(r"[^\d]", "", s[last + 1:])
    try:
        return float((intpart or "0") + (("." + dec) if dec else ""))
    except Exception:
        return 0.0

def _montants(s):
    """Nombres a EXACTEMENT 2 decimales = montants (exclut VL/quantites a 4 decimales)."""
    out = []
    for m in _NUM.finditer(s or ""):
        tok = m.group()
        if _dec_len(tok) == 2:
            v = _eur(tok)
            if v > 0:
                out.append(v)
    return out

def parse_nsia(text):
    if not text:
        return None
    low = text.lower()
    if not any(k in low for k in ("nsia", "opcvm", "portefeuille", "aurore", "fonds", "fcp",
                                  "valeur liquidative", "souscription", "rachat", "valorisation",
                                  "plus-value", "plus value", "montant net", "opportunites", "opportunités")):
        return None
    lines = re.split(r"[\n\r]+", text)
    valorisation = pv_latente = prix_revient = None
    # 1) Ligne "Total portefeuille" (ou la ligne de position) -> valorisation + plus-value latente
    total_line = None
    for ln in lines:
        l = ln.lower()
        if "total" in l and ("portefeuille" in l or "porte" in l):  # tolere l'OCR
            total_line = ln
            break
    if total_line is None:
        for ln in lines:
            l = ln.lower()
            if ("aurore" in l or "opcvm" in l or "opportun" in l) and re.search(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}", ln):
                total_line = ln
                break
    if total_line:
        vals = _montants(total_line)
        if vals:
            valorisation = vals[0]
            if len(vals) >= 3:
                prix_revient, pv_latente = vals[-2], vals[-1]
            elif len(vals) == 2:
                pv_latente = vals[-1]
    # Fallback : plus gros montant 2-decimales du document
    if not valorisation:
        allv = [v for v in _montants(text) if 1000 <= v <= 5_000_000_000]
        if allv:
            valorisation = max(allv)
    # 2) Historique des operations (Souscription / Rachat)
    ops = []
    for ln in lines:
        dm = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", ln)
        ll = ln.lower()
        sens = "Souscription" if ("souscription" in ll or "souscri" in ll) else ("Rachat" if "rachat" in ll else None)
        if not (dm and sens):
            continue
        money2 = _montants(ln)
        if not money2:
            continue
        montant = money2[0]
        pv = money2[1] if len(money2) > 1 else 0.0
        d, mo, y = dm.groups()
        ops.append({"date": "%s-%s-%s" % (y, mo, d), "label": sens,
                    "sens": ("inc" if sens == "Souscription" else "exp"),
                    "montant": montant, "pv": pv})
    if not valorisation and not ops:
        return None
    invested = (sum(o["montant"] for o in ops if o["sens"] == "inc")
                - sum(o["montant"] for o in ops if o["sens"] == "exp")) if ops else None
    pv_realisee = sum(o["pv"] for o in ops) if ops else None
    return {"source": "nsia", "total": valorisation or 0, "cur": "XOF",
            "pv_latente": pv_latente, "prix_revient": prix_revient,
            "invested": invested, "pv_realisee": pv_realisee,
            "operations": ops[-60:], "ts": int(time.time() * 1000),
            "text": "NSIA - Releve de Portefeuille"}

def set_nsia(obj):
    if not obj:
        return False
    STATE["nsia"] = obj
    save_state()
    return True

# ----------------------- PDF -----------------------
def parse_pdf_bytes(data):
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        log.warning("lecture PDF impossible: %s", e)
        return None, ""
    m = re.search(r"NEXUS_DATA\s*=\s*(\{.*\})", text, re.S)
    if m:
        try:
            return json.loads(m.group(1)), text
        except Exception:
            pass
    return None, text

# ----------------------- OCR image (ocr.space) -----------------------
async def ocr_image(session, data, filename, is_pdf=False):
    if not OCR_API_KEY:
        return ""
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_API_KEY)
    form.add_field("language", "fre")
    form.add_field("OCREngine", "2")
    form.add_field("scale", "true")
    form.add_field("isTable", "true")
    form.add_field("detectOrientation", "true")
    if is_pdf:
        form.add_field("filetype", "PDF")   # ocr.space sait OCR-iser un PDF scanné directement
    form.add_field("file", data, filename=filename or ("doc.pdf" if is_pdf else "img.png"),
                   content_type="application/pdf" if is_pdf else "application/octet-stream")
    try:
        async with session.post("https://api.ocr.space/parse/image", data=form,
                                timeout=aiohttp.ClientTimeout(total=40)) as r:
            j = await r.json()
            res = j.get("ParsedResults") or []
            return " ".join(p.get("ParsedText", "") for p in res)
    except Exception as e:
        log.warning("ocr.space: %s", e)
        return ""

# ----------------------- Serveur HTTP -----------------------
def cors(resp, request=None):
    """CORS restreint. L'app est servie par CE serveur : la seule origine legitime
    est PUBLIC_URL. Sans PUBLIC_URL on n'autorise AUCUNE origine tierce (au lieu de "*",
    qui laissait n'importe quel site appeler l'API avec le jeton de l'utilisateur)."""
    allowed = [o for o in (PUBLIC_URL, "http://localhost:%d" % PORT, "http://127.0.0.1:%d" % PORT) if o]
    origin = (request.headers.get("Origin", "") if request is not None else "")
    if origin and origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
    elif PUBLIC_URL:
        resp.headers["Access-Control-Allow-Origin"] = PUBLIC_URL
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, x-auth"
    resp.headers["Access-Control-Max-Age"] = "600"
    # Ces reponses contiennent des soldes : ni cache navigateur, ni cache proxy,
    # et pas de referer sortant (l'URL peut encore contenir ?token= si l'utilisateur
    # est arrive par le bouton Discord).
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp

AUTH_COOKIE = "nexus_auth"

def _token_of(request):
    """Extrait le jeton presente par le client, du plus sur au moins sur :
       1) Authorization: Bearer <token>   2) x-auth:   3) cookie   4) ?token= (compat)."""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    return (bearer
            or request.headers.get("x-auth", "")
            or request.cookies.get(AUTH_COOKIE, "")
            or request.query.get("token", ""))

def check_token(request):
    """Verifie le jeton. Deux durcissements vs la version precedente :
       - comparaison a temps constant (une comparaison ==  fuit la longueur du prefixe
         correct et rend une attaque par mesure de temps possible) ;
       - si AUTH_TOKEN n'est pas configure, on REFUSE tout au lieu de tout ouvrir."""
    if AUTH_WEAK or not AUTH_TOKEN:
        # 'nexus229' figure en clair dans le depot : l'accepter revient a n'avoir
        # aucune authentification. Mieux vaut un service en panne, bruyant dans les
        # logs, qu'un tableau de bord financier ouvert a qui lit le README.
        return False
    return hmac.compare_digest(_token_of(request), AUTH_TOKEN)

async def h_options(request):
    return cors(web.Response(status=204), request)

# ----------------------- Anti-force brute -----------------------
# Le jeton etait devinable a l'infini : aucune limite de tentatives. On ajoute
# une fenetre glissante par IP (memoire process, suffisant pour une instance).
RATE_MAX_REQ   = int(_conf("RATE_MAX_REQ") or "120")   # requetes / minute / IP
AUTH_MAX_FAIL  = int(_conf("AUTH_MAX_FAIL") or "10")   # echecs d'auth avant blocage
AUTH_BLOCK_SEC = int(_conf("AUTH_BLOCK_SEC") or "900") # duree du blocage (15 min)
_rate = {}        # ip -> [timestamps]
_authfail = {}    # ip -> (nb_echecs, ts_dernier)

def _client_ip(request):
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.remote or "?")

def rate_ok(request):
    """False si l'IP depasse le quota ou est bloquee pour echecs d'auth repetes."""
    ip = _client_ip(request)
    now = time.time()
    fails, last = _authfail.get(ip, (0, 0))
    if fails >= AUTH_MAX_FAIL and now - last < AUTH_BLOCK_SEC:
        return False
    hits = [t for t in _rate.get(ip, []) if now - t < 60]
    hits.append(now)
    _rate[ip] = hits
    if len(_rate) > 2000:                      # purge des IP inactives
        for k in [k for k, v in _rate.items() if not v or now - v[-1] > 300]:
            _rate.pop(k, None); _authfail.pop(k, None)
    return len(hits) <= RATE_MAX_REQ

def note_auth_fail(request):
    ip = _client_ip(request)
    fails, last = _authfail.get(ip, (0, 0))
    now = time.time()
    if now - last > AUTH_BLOCK_SEC:
        fails = 0
    _authfail[ip] = (fails + 1, now)
    log.warning("auth refusee (%s) — %d echec(s)", ip, fails + 1)

def note_auth_ok(request):
    _authfail.pop(_client_ip(request), None)

def guard(request):
    """Controle commun a toutes les routes protegees.
    Renvoie None si l'appel est autorise, sinon la reponse d'erreur a retourner."""
    if not rate_ok(request):
        return cors(web.json_response({"ok": False, "error": "too many requests"}, status=429), request)
    if not check_token(request):
        note_auth_fail(request)
        return cors(web.json_response({"ok": False, "error": "bad token"}, status=401), request)
    note_auth_ok(request)
    return None

async def h_ping(request):
    # Publique (c'est la sonde du cron externe) mais soumise a la limite de debit :
    # sans elle, /ping restait un point d'entree a marteler gratuitement.
    if not rate_ok(request):
        return web.Response(text="rate limited", status=429)
    return cors(web.Response(text="NEXUS server OK"), request)

async def h_health(request):
    """Sonde de supervision : uniquement des infos non sensibles (pas de montants,
    pas de jeton) pour pouvoir etre appelee par un cron externe sans authentification."""
    if not rate_ok(request):
        return web.Response(text="rate limited", status=429)
    return cors(web.json_response({
        "ok": True,
        "uptime": int(time.time()) - START_TS,
        "discord": bool(DISCORD_TOKEN and discord is not None),
        "bitget": bool(BITGET_KEY and BITGET_SECRET and BITGET_PASS),
        "ia": ai_enabled(),
        "auth": (not AUTH_WEAK),
        "persistance": ("upstash" if (UPSTASH_URL and UPSTASH_TOKEN and _UPSTASH_OK)
                        else ("upstash-degrade" if (UPSTASH_URL and UPSTASH_TOKEN) else "fichier")),
        "updatedAt": STATE.get("updatedAt"),
    }), request)

# ---- Validation des donnees poussees par l'app -------------------------------
# /state POST acceptait N'IMPORTE QUEL corps JSON et le recopiait tel quel dans
# l'etat, qui est ensuite serialise a chaque sauvegarde et renvoye a tous les
# clients : un corps de 50 Mo, ou un type inattendu, suffisait a saturer la
# memoire de l'instance ou a faire planter les calculs.
STATE_MAX_BYTES = int(_conf("STATE_MAX_KB") or "512") * 1024
BUSINESS_NUM_KEYS = ("revM", "expM", "netM", "mrr", "arr", "caTotal", "depTotal")

def _num_or_none(v):
    """Nombre fini, ou None. Refuse NaN/Infini (json.dumps les ecrirait tels quels,
    et le JSON produit devient illisible par le navigateur)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f

def sanitize_business(b):
    """Ne garde que les champs attendus du resume revenus/depenses, en nombres surs."""
    if not isinstance(b, dict):
        raise ValueError("business doit etre un objet")
    out = {}
    for k in BUSINESS_NUM_KEYS:
        if k in b:
            n = _num_or_none(b.get(k))
            if n is not None:
                out[k] = n
    cur = b.get("cur", "XOF")
    out["cur"] = str(cur)[:8] if cur else "XOF"
    ts = _num_or_none(b.get("ts"))
    out["ts"] = int(ts) if ts is not None else int(time.time() * 1000)
    return out

def _s(v, n=60):
    return str(v if v is not None else "")[:n]

def sanitize_objectifs(o):
    """Budgets / objectifs / dettes / DCA poussés par l'app -> structure bornée et sûre.
    Sert l'agent Objectifs. Listes plafonnées (60 entrées) pour ne pas gonfler l'état."""
    if not isinstance(o, dict):
        raise ValueError("objectifs doit etre un objet")
    def num(v):
        n = _num_or_none(v)
        return int(round(n)) if n is not None else 0
    budgets, goals, debts, dcas = [], [], [], []
    for b in (o.get("budgets") or [])[:60]:
        if isinstance(b, dict):
            budgets.append({"cat": _s(b.get("cat")), "limit": num(b.get("limit")), "spent": num(b.get("spent"))})
    for g in (o.get("goals") or [])[:60]:
        if isinstance(g, dict):
            goals.append({"name": _s(g.get("name")), "current": num(g.get("current")),
                          "target": num(g.get("target")), "deadline": _s(g.get("deadline"), 20)})
    for d in (o.get("debts") or [])[:60]:
        if isinstance(d, dict):
            debts.append({"name": _s(d.get("name")), "balance": num(d.get("balance")),
                          "original": num(d.get("original")), "due": _s(d.get("due"), 20)})
    for d in (o.get("dcas") or [])[:60]:
        if isinstance(d, dict):
            dcas.append({"asset": _s(d.get("asset"), 16), "amount": num(d.get("amount")),
                         "freq": _s(d.get("freq"), 16), "next": _s(d.get("next"), 20), "auto": bool(d.get("auto"))})
    cur = o.get("cur", "XOF")
    return {"cur": str(cur)[:8] if cur else "XOF", "budgets": budgets, "goals": goals,
            "debts": debts, "dcas": dcas, "ts": int(time.time() * 1000)}

def sanitize_catexp(m):
    """Dépenses du mois par catégorie {nom: montant} -> bornée (40 entrées, nombres)."""
    if not isinstance(m, dict):
        raise ValueError("catExp doit etre un objet")
    out = {}
    for k, v in list(m.items())[:40]:
        n = _num_or_none(v)
        if n is not None:
            out[_s(k, 40)] = int(round(n))
    return out

def _json_depth(o, limit=40, d=0):
    """Profondeur d'un objet JSON : au-dela de `limit`, la serialisation recursive
    de save_state() partirait en RecursionError a CHAQUE sauvegarde ensuite."""
    if d > limit:
        raise ValueError("structure trop imbriquee")
    if isinstance(o, dict):
        for v in o.values():
            _json_depth(v, limit, d + 1)
    elif isinstance(o, list):
        for v in o:
            _json_depth(v, limit, d + 1)
    return True

def validate_patrimoine(obj):
    """Le patrimoine vient de l'app : structure libre, mais bornee en taille et en
    profondeur, et jamais autre chose qu'un objet ou une liste."""
    if not isinstance(obj, (dict, list)):
        raise ValueError("patrimoine doit etre un objet ou une liste")
    _json_depth(obj)
    taille = len(json.dumps(obj, ensure_ascii=False))
    if taille > STATE_MAX_BYTES:
        raise ValueError("patrimoine trop volumineux (%d octets, max %d)" % (taille, STATE_MAX_BYTES))
    return obj

async def _read_json(request, maxi=None):
    """Lit un corps JSON en refusant tout ce qui depasse `maxi` octets, SANS le
    charger d'abord en entier (await request.json() lisait tout, quelle que soit
    la taille annoncee)."""
    maxi = maxi or STATE_MAX_BYTES
    if (request.content_length or 0) > maxi:
        raise ValueError("corps trop volumineux (%s octets, max %d)" % (request.content_length, maxi))
    raw = await request.content.read(maxi + 1)
    if len(raw) > maxi:
        raise ValueError("corps trop volumineux (max %d octets)" % maxi)
    if not raw:
        raise ValueError("corps vide")
    body = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(body, dict):
        raise ValueError("le corps doit etre un objet JSON")
    return body

async def h_state(request):
    denied = guard(request)
    if denied is not None:
        return denied
    if request.method == "POST":
        try:
            body = await _read_json(request)
            if body.get("patrimoine") is not None:
                # .get(k, defaut) recopiait un null envoye par erreur et EFFACAIT
                # le patrimoine ; on n'ecrit que sur une valeur reellement fournie.
                STATE["patrimoine"] = validate_patrimoine(body["patrimoine"])
            if body.get("business") is not None:
                STATE["business"] = sanitize_business(body["business"])
            if body.get("objectifs") is not None:
                STATE["objectifs"] = sanitize_objectifs(body["objectifs"])
            if body.get("catExp") is not None:
                STATE["catexp"] = sanitize_catexp(body["catExp"])
            # Accuse reception des actions IA appliquees cote app : on les marque 'done'.
            ack = body.get("ackActions")
            if isinstance(ack, list) and ack:
                done = set(str(x) for x in ack)
                for a in (STATE.get("ai_actions") or []):
                    if a.get("id") in done:
                        a["status"] = "done"
            save_state()
        except ValueError as e:
            log.warning("/state POST refuse (%s): %s", _client_ip(request), e)
            return cors(web.json_response({"ok": False, "error": str(e)}, status=400), request)
        except Exception as e:
            log.error("/state POST: %s", e)
            return cors(web.json_response({"ok": False, "error": "corps invalide"}, status=400), request)
    since = 0
    try:
        since = int(request.query.get("since", "0"))
    except Exception:
        since = 0
    momo = [m for m in STATE["momo"] if m.get("ts", 0) > since]
    return cors(web.json_response({
        "ok": True,
        "patrimoine": STATE["patrimoine"],
        "momo": momo,
        "nsia": STATE["nsia"],
        # L'etat interne stocke {amount, ts} depuis le correctif des soldes ; l'app
        # attend des nombres simples. On aplatit ici pour ne rien casser cote client.
        "balances": {k: _balance_entry(v)[0] for k, v in (STATE.get("balances") or {}).items()},
        "balanceSources": momo_balance_sources(),
        "bgCalib": STATE.get("bg_calib") or {"extra": 0, "staking": {}},
        # Actions IA approuvees sur Discord et pas encore appliquees : l'app les execute
        # puis renvoie ackActions.
        "aiActions": [a for a in (STATE.get("ai_actions") or []) if a.get("status") == "approved"],
        "updatedAt": STATE["updatedAt"],
    }), request)

async def h_bgcalib(request):
    """Calage Bitget (staking non vu par l'API) partage entre tous les appareils.
    GET  /bgcalib?token=...            -> renvoie {extra, override}
    POST /bgcalib?token=... {extra,override} -> enregistre."""
    denied = guard(request)
    if denied is not None:
        return denied
    if request.method == "POST":
        try:
            body = await _read_json(request, 64 * 1024)
            extra = _num_or_none(body.get("extra", 0)) or 0.0
            staking = body.get("staking") or {}
            if not isinstance(staking, dict):
                raise ValueError("staking doit etre un objet {coin: quantite}")
            if len(staking) > 200:
                raise ValueError("trop de lignes de staking (max 200)")
            # Les quantites servent a valoriser le patrimoine : un texte ou un NaN
            # ici, et le total consolide devenait faux ou plantait plus loin.
            clean = {}
            for coin, qty in staking.items():
                q = _num_or_none(qty)
                if q is None or q < 0:
                    raise ValueError("quantite de staking invalide pour %r" % str(coin)[:20])
                clean[str(coin).upper()[:16]] = q
            STATE["bg_calib"] = {"extra": extra, "staking": clean}
            save_state()
        except ValueError as e:
            log.warning("/bgcalib POST refuse (%s): %s", _client_ip(request), e)
            return cors(web.json_response({"ok": False, "error": str(e)}, status=400), request)
        except Exception as e:
            log.error("/bgcalib POST: %s", e)
            return cors(web.json_response({"ok": False, "error": "corps invalide"}, status=400), request)
    return cors(web.json_response({"ok": True, "bgCalib": STATE.get("bg_calib") or {"extra": 0, "staking": {}}}), request)

async def h_momo_ingest(request):
    """Reception d'un SMS MoMo depuis le telephone (MacroDroid).
    GET  /momo?token=...&text=LE_SMS   ou   POST /momo?token=... (corps texte/json/form)."""
    denied = guard(request)
    if denied is not None:
        return denied
    SMS_MAX = 65536
    text = (request.query.get("text", "") or request.query.get("sms", ""))[:SMS_MAX]
    src = str(request.query.get("src", "sms"))[:16]
    if request.method == "POST" and not text:
        # request.read() chargeait TOUT le corps avant de le tronquer : un POST de
        # 100 Mo passait quand meme par la memoire de l'instance.
        body = (await request.content.read(SMS_MAX + 1))[:SMS_MAX].decode("utf-8", "ignore")
        ct = request.headers.get("Content-Type", "")
        text = body
        if "json" in ct:
            try:
                j = json.loads(body)
                text = j.get("text", body) if isinstance(j, dict) else body
            except Exception as e:
                log.info("/momo: corps JSON illisible, traite comme du texte brut (%s)", e)
        elif "form-urlencoded" in ct:
            pq = parse_qs(body)
            text = (pq.get("text") or pq.get("sms") or [body])[0]
    if not isinstance(text, str):
        text = str(text)
    n = momo_ingest_text(text[:SMS_MAX], src)
    return cors(web.json_response({"ok": True, "added": n}), request)

async def h_momo_inbox(request):
    denied = guard(request)
    if denied is not None:
        return denied
    since = 0
    try:
        since = int(request.query.get("since", "0"))
    except Exception:
        since = 0
    data = [m for m in STATE["momo"] if m.get("ts", 0) > since]
    return cors(web.json_response({"ok": True, "data": data, "count": len(data)}), request)

async def h_bitget(request):
    denied = guard(request)
    if denied is not None:
        return denied
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return cors(web.json_response({"code": "NO_KEYS", "msg": "Cles Bitget non configurees"}, status=500),
                    request)
    path = request.match_info.get("path", "")
    full = "/" + path
    # Un chemin du genre "/api/v2/spot/market/tickers/../../trade/place-order"
    # commence bien par un prefixe autorise, mais Bitget le resout ailleurs :
    # la liste blanche ci-dessous serait contournee. On refuse toute traversee.
    if ".." in full or "//" in full or not re.fullmatch(r"[A-Za-z0-9/_.\-]*", full):
        log.warning("proxy bitget: chemin malforme refuse: %r (%s)", full, _client_ip(request))
        return cors(web.json_response({"code": "BAD_PATH", "msg": "Chemin invalide"}, status=400), request)
    # Le proxy signait N'IMPORTE QUEL chemin Bitget avec les cles du serveur :
    # quiconque avait le jeton pouvait passer des ordres ou declencher un retrait.
    # On restreint a la lecture. BITGET_ALLOW_WRITE=1 leve la restriction sciemment.
    if not BITGET_ALLOW_WRITE:
        if request.method != "GET" or not any(full.startswith(p) for p in BITGET_READ_PATHS):
            log.warning("proxy bitget refuse: %s %s (%s)", request.method, full, _client_ip(request))
            return cors(web.json_response(
                {"code": "FORBIDDEN_PATH",
                 "msg": "Proxy en lecture seule. Chemin non autorise : %s %s" % (request.method, full)},
                status=403), request)
    if request.query_string:
        qs = "&".join(p for p in request.query_string.split("&") if not p.startswith("token="))
        if qs:
            full += "?" + qs
    body = ""
    if request.method == "POST":
        raw = await request.content.read(65537)          # corps borne (cf. /momo)
        if len(raw) > 65536:
            return cors(web.json_response({"code": "BODY_TOO_LARGE", "msg": "Corps trop volumineux"},
                                          status=413), request)
        body = raw.decode("utf-8", "replace")
    session = request.app["session"]
    try:
        status, text = await bitget_request(session, request.method, full, body)
    except Exception as e:
        log.warning("proxy bitget %s: %s", full.split("?")[0], e)
        return cors(web.json_response({"code": "PROXY_ERR", "msg": str(e)}, status=502), request)
    resp = web.Response(text=text, status=status, content_type="application/json")
    return cors(resp, request)

LOGIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NEXUS — Acces</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#0B0B12;color:#E8E8F0;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
.c{width:100%;max-width:360px;padding:32px;background:#14141F;border:1px solid #24243A;border-radius:16px}
h1{margin:0 0 6px;font-size:20px}p{margin:0 0 20px;color:#8A8AA8;font-size:13px;line-height:1.5}
input{width:100%;padding:12px 14px;background:#0B0B12;border:1px solid #24243A;border-radius:10px;
color:#E8E8F0;font-size:15px;margin-bottom:12px}input:focus{outline:none;border-color:#C9A227}
button{width:100%;padding:12px;background:#C9A227;border:0;border-radius:10px;color:#0B0B12;
font-size:15px;font-weight:700;cursor:pointer}.e{color:#F87171;font-size:13px;margin-bottom:12px}</style>
</head><body><div class="c"><h1>🛰️ NEXUS</h1>
<p>Tableau de bord prive. Entre le jeton d'acces, ou passe par le bouton
<strong>Ouvrir l'app</strong> du panneau Discord.</p>
__ERR__<form method="post" action="/"><input type="password" name="token" placeholder="Jeton d'acces"
autofocus autocomplete="current-password"><button type="submit">Entrer</button></form></div></body></html>"""

APP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PATRIMOINE_OS.html")
_APP_CACHE = {"mtime": 0.0, "size": 0, "body": None}

def _read_app_html():
    """Lit PATRIMOINE_OS.html (≈350 Ko) en le gardant en memoire tant que le fichier
    n'a pas change : sans cela, chaque rechargement de l'app relisait tout le disque
    DANS la boucle asyncio (donc heartbeat Discord bloque pendant la lecture)."""
    st = os.stat(APP_HTML)
    c = _APP_CACHE
    if c["body"] is None or c["mtime"] != st.st_mtime or c["size"] != st.st_size:
        with open(APP_HTML, "rb") as f:
            c["body"] = f.read()
        c["mtime"], c["size"] = st.st_mtime, st.st_size
        log.info("PATRIMOINE_OS.html relu (%d octets)", len(c["body"]))
    return c["body"]

def _login_page(err=""):
    body = LOGIN_PAGE.replace("__ERR__", err).encode("utf-8")
    resp = web.Response(body=body, content_type="text/html", charset="utf-8", status=401)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

async def h_app(request):
    """Sert l'app. AVANT : la page etait publique ET contenait le AUTH_TOKEN en clair,
    donc n'importe quel visiteur de PUBLIC_URL repartait avec la cle de toute l'API
    (etat, proxy Bitget signe, ingestion MoMo). MAINTENANT : il faut presenter le jeton,
    et il est depose dans un cookie HttpOnly pour que les visites suivantes passent
    sans le remettre dans l'URL.

    Le formulaire est en POST : en GET, le jeton finissait dans l'historique du
    navigateur, dans le Referer et dans les logs d'acces. Le ?token= reste accepte
    pour le bouton "Ouvrir l'app" du panneau Discord."""
    if not rate_ok(request):
        return web.Response(text="Trop de tentatives. Reessaie plus tard.", status=429,
                            headers={"Retry-After": "60"})
    presente = request.query.get("token") or ""
    if request.method == "POST":
        try:
            form = await request.post()
            presente = str(form.get("token") or "")
        except Exception as e:
            log.warning("h_app: formulaire illisible: %s", e)
            presente = ""
        if not (AUTH_TOKEN and not AUTH_WEAK and hmac.compare_digest(presente, AUTH_TOKEN)):
            note_auth_fail(request)
            return _login_page('<div class="e">Jeton invalide.</div>')
        note_auth_ok(request)
        # 303 + cookie : la page suivante est un GET propre, sans jeton nulle part.
        resp = web.HTTPSeeOther("/")
        _set_auth_cookie(resp)
        return resp
    if not check_token(request):
        note_auth_fail(request)
        if AUTH_WEAK:
            log.error("acces refuse : AUTH_TOKEN absent ou trop faible — voir les logs de demarrage.")
        return _login_page('<div class="e">Jeton invalide.</div>' if presente else "")
    note_auth_ok(request)
    try:
        data = _read_app_html()
        # Le jeton n'est injecte dans la page qu'apres authentification.
        boot = ("<script>try{localStorage.setItem('nexus_srv_url',location.origin);"
                "localStorage.setItem('nexus_srv_token',%s);"
                "if(location.search.indexOf('token=')>=0)history.replaceState(null,'',location.pathname);}catch(e){}</script>"
                % json.dumps(AUTH_TOKEN)).encode("utf-8")
        data = data.replace(b"<head>", b"<head>" + boot, 1)
        resp = web.Response(body=data, content_type="text/html", charset="utf-8")
        resp.headers["Cache-Control"] = "no-store"      # la page embarque le jeton
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        _set_auth_cookie(resp)
        return resp
    except FileNotFoundError:
        log.error("PATRIMOINE_OS.html introuvable a cote de nexus_server.py (%s)", APP_HTML)
        return web.Response(text="App introuvable", status=404)
    except Exception as e:
        log.error("h_app: %s", e)
        return web.Response(text="Erreur serveur", status=500)

def _set_auth_cookie(resp):
    """Cookie de session : evite de retrimballer ?token= dans l'URL (et donc dans
    l'historique du navigateur, le referer et les logs Render)."""
    resp.set_cookie(AUTH_COOKIE, AUTH_TOKEN, max_age=90 * 86400, httponly=True,
                    samesite="Lax", secure=PUBLIC_URL.startswith("https://"), path="/")

class SafeAccessLogger(AbstractAccessLogger):
    """Journal d'acces qui ne recopie JAMAIS la chaine de requete (?token=...)."""
    def log(self, request, response, duree):
        try:
            self.logger.info('%s "%s %s" %s %s %.0fms', _client_ip(request), request.method,
                             request.path, response.status, response.body_length, duree * 1000)
        except Exception:
            pass   # un log ne doit jamais faire tomber une requete

async def start_http():
    global HTTP_SESSION
    app = web.Application()
    app["session"] = aiohttp.ClientSession()
    HTTP_SESSION = app["session"]
    try:
        await refresh_usd_xof(HTTP_SESSION)   # taux FCFA live des le demarrage
    except Exception as e:
        log.warning("taux USD/XOF indisponible au demarrage, repli sur %s: %s", USD_XOF, e)
    app.router.add_route("OPTIONS", "/{tail:.*}", h_options)
    app.router.add_get("/ping", h_ping)
    app.router.add_get("/health", h_health)
    app.router.add_get("/", h_app)
    app.router.add_post("/", h_app)          # formulaire de connexion (jeton hors URL)
    app.router.add_get("/app", h_app)
    app.router.add_get("/state", h_state)
    app.router.add_post("/state", h_state)
    app.router.add_get("/bgcalib", h_bgcalib)
    app.router.add_post("/bgcalib", h_bgcalib)
    app.router.add_get("/momo", h_momo_ingest)
    app.router.add_post("/momo", h_momo_ingest)
    app.router.add_get("/momo/inbox", h_momo_inbox)
    app.router.add_route("GET", "/bitget/{path:.*}", h_bitget)
    app.router.add_route("POST", "/bitget/{path:.*}", h_bitget)
    app.router.add_post("/ai", h_ai)              # proxy Claude (cle cote serveur)
    app.router.add_get("/agents", h_agents)       # moteur d'agents (a la demande)
    app.router.add_post("/agents", h_agents)

    async def _close_session(app):
        await app["session"].close()
    app.on_cleanup.append(_close_session)
    # Journal d'acces SANS la chaine de requete : "GET /?token=xxxx" ecrivait le
    # jeton en clair dans les logs Render, consultables bien apres coup.
    runner = web.AppRunner(app, access_log_class=SafeAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("API NEXUS a l'ecoute sur le port %d", PORT)
    return app

# ----------------------- Statistiques / nettoyage (sans dependance Discord) -----------------------
def _momo_totals():
    momo = STATE.get("momo") or []
    inc = sum(float(m.get("amount", 0) or 0) for m in momo if m.get("type") == "inc")
    exp = sum(float(m.get("amount", 0) or 0) for m in momo if m.get("type") == "exp")
    return inc, exp, inc - exp, len(momo)

def _wat_today():
    """Date du jour en heure du Bénin (UTC+1)."""
    return today_wat()

def _item_date(m):
    d = m.get("date")
    if d:
        try:
            return datetime.date.fromisoformat(d)
        except Exception:
            pass
    ts = (m.get("ts", 0) or 0) / 1000
    try:
        return datetime.datetime.fromtimestamp(ts, WAT).date()
    except Exception:
        return _wat_today()

def momo_totals_period(days, net=None, items=None):
    """Fenetre glissante de N jours -> (entrees, sorties, net, nb). net='mtn'|'moov'|None(tous).
    `items` permet de travailler sur une copie figee de la liste : le PDF est genere
    dans un thread pendant que la boucle asyncio peut encore y ajouter des operations."""
    today = _wat_today()
    start = today - datetime.timedelta(days=int(days) - 1)
    if items is None:
        items = STATE.get("momo") or []
    sel = [m for m in items if _item_date(m) >= start and (net is None or m.get("net", "mtn") == net)]
    inc = sum(float(m.get("amount", 0) or 0) for m in sel if m.get("type") == "inc")
    exp = sum(float(m.get("amount", 0) or 0) for m in sel if m.get("type") == "exp")
    return inc, exp, inc - exp, len(sel)

def _balance_entry(v):
    """Un solde capture est soit un nombre (ancien format), soit {amount, ts}."""
    if isinstance(v, dict):
        return float(v.get("amount", 0) or 0), int(v.get("ts", 0) or 0)
    try:
        return float(v or 0), 0
    except Exception:
        return 0.0, 0

def momo_balance_by_net():
    """Solde par reseau, en FCFA.

    AVANT : des qu'une seule transaction existait pour un reseau, le solde capture
    (la vraie photo du compte, lue sur une capture d'accueil) etait IGNORE et
    remplace par la SOMME DES FLUX de la periode importee. Un flux n'est pas un
    solde : le total consolide pouvait etre negatif ou tres loin du reel.

    MAINTENANT : si un solde a ete capture, il fait foi ; on lui ajoute seulement
    les operations posterieures a la capture. Sans capture, on retombe sur le net
    des flux (approximation, signalee par momo_balance_sources())."""
    momo = STATE.get("momo") or []
    bal = STATE.get("balances") or {}
    nets = set([m.get("net", "mtn") for m in momo]) | set(bal.keys())
    out = {}
    for net in nets:
        amount, ts0 = _balance_entry(bal.get(net))
        has_capture = net in bal
        # Un solde enregistre AVANT l'ajout de l'horodatage est un simple nombre :
        # ts0 vaut alors 0, et on ne sait pas quelles operations il reflete deja.
        # Les ajouter toutes gonflait le patrimoine (constate en production : une
        # alerte a +11725 %). Sans horodatage, la photo du compte vaut seule.
        if has_capture and ts0 <= 0:
            out[net] = amount
            continue
        total = amount if has_capture else 0.0
        for m in momo:
            if m.get("net", "mtn") != net:
                continue
            if has_capture and (m.get("ts", 0) or 0) <= ts0:
                continue          # deja reflete dans le solde capture
            a = float(m.get("amount", 0) or 0)
            total += a if m.get("type") == "inc" else -a
        out[net] = total
    return out

def momo_balance_sources():
    """Pour chaque reseau : 'capture' (solde reel lu) ou 'flux' (estime). Sert a
    afficher honnetement quand un chiffre est une approximation."""
    bal = STATE.get("balances") or {}
    nets = set([m.get("net", "mtn") for m in (STATE.get("momo") or [])]) | set(bal.keys())
    return {net: ("capture" if net in bal else "flux") for net in nets}

async def refresh_usd_xof(session):
    """Recupere le taux USD->FCFA en direct (meme source que l'app) et le met en cache.
    Garde le total Discord aligne sur l'app au lieu d'un taux fige. Cache 30 min."""
    global _USD_XOF_LIVE, _USD_XOF_TS
    if time.time() - _USD_XOF_TS < 1800 and _USD_XOF_TS:
        return _USD_XOF_LIVE
    for url in ("https://api.exchangerate-api.com/v4/latest/USD",
                "https://open.er-api.com/v6/latest/USD"):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                d = await r.json()
                rate = (d.get("rates") or d.get("conversion_rates") or {}).get("XOF")
                if rate and float(rate) > 400:
                    _USD_XOF_LIVE = float(rate)
                    _USD_XOF_TS = int(time.time())
                    return _USD_XOF_LIVE
        except Exception as e:
            log.warning("taux USD/XOF (%s): %s", url, e)
    return _USD_XOF_LIVE

def get_usd_xof():
    """Taux USD->FCFA courant (live si dispo, sinon repli)."""
    return _USD_XOF_LIVE or USD_XOF

def consolidated_total():
    """Patrimoine total combine en FCFA + le detail par poste."""
    bynet = momo_balance_by_net()
    momo_tot = sum(bynet.values())
    nsia = float((STATE.get("nsia") or {}).get("total", 0) or 0)
    bg_usd = float((STATE.get("bitget") or {}).get("total", 0) or 0)
    bg_xof = bg_usd * get_usd_xof()
    total = momo_tot + nsia + bg_xof
    return {"total": total, "momo": momo_tot, "bynet": bynet,
            "nsia": nsia, "bitget_usd": bg_usd, "bitget_xof": bg_xof}

# ----------------------- Historique du patrimoine -----------------------
HISTORY_MAX = 400                                   # ~13 mois de points quotidiens
ALERT_PCT   = float(_conf("ALERT_PCT") or "5")      # seuil d'alerte variation 24h (%)

def record_snapshot():
    """Enregistre (ou met a jour) le point du JOUR dans STATE["history"].
    1 point par jour : {d, total, momo, nsia, bg (en FCFA)}. Retourne (point, veille)."""
    c = consolidated_total()
    today = today_wat().isoformat()
    hist = STATE.setdefault("history", [])
    point = {"d": today, "total": int(round(c["total"])), "momo": int(round(c["momo"])),
             "nsia": int(round(c["nsia"])), "bg": int(round(c["bitget_xof"]))}
    if hist and hist[-1].get("d") == today:
        prev = hist[-2] if len(hist) >= 2 else None
        hist[-1] = point
    else:
        prev = hist[-1] if hist else None
        hist.append(point)
    del hist[:-HISTORY_MAX]
    save_state()
    return point, prev

def sparkline(vals):
    """Mini graphe texte (▁▂▃▄▅▆▇█) a partir d'une liste de valeurs."""
    if not vals:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return blocks[3] * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in vals)

async def refresh_bitget_state(session):
    """Recalcule Bitget en direct (live + staking calé) et met STATE["bitget"] a jour.
    A appeler AVANT consolidated_total() pour que le total consolide ne soit jamais perime."""
    try:
        ov = await bitget_overview(session, force=True)
        if ov:
            STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                               "others": ov["others"], "ts": int(time.time() * 1000),
                               "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
            save_state()
        return ov
    except Exception as e:
        log.warning("rafraichissement Bitget: %s", e)
        return None

def business_field():
    """Texte du champ Revenus & Depenses (resume pousse par l'app), ou None."""
    b = STATE.get("business") or {}
    if not b:
        return None
    c = b.get("cur", "XOF")
    def f(n):
        try:
            n = float(n or 0)
        except Exception:
            n = 0.0
        if c == "XOF":
            return fmt_xof(n)
        return "%s %s" % (format(int(round(n)), ",d").replace(",", " "), c)
    lines = ["Revenus (mois) : **%s**" % f(b.get("revM")),
             "Dépenses (mois) : **%s**" % f(b.get("expM")),
             "Net (mois) : **%s**" % f(b.get("netM"))]
    if b.get("mrr"):
        lines.append("MRR : **%s** · ARR %s" % (f(b.get("mrr")), f(b.get("arr"))))
    return "\n".join(lines)

def _bar(pct, width=12):
    """Barre de progression unicode pour les embeds Discord."""
    try:
        pct = max(0.0, min(100.0, float(pct or 0)))
    except Exception:
        pct = 0.0
    fill = int(round(pct / 100.0 * width))
    return "█" * fill + "░" * (width - fill)

def _chg(pct):
    """Petit indicateur de variation 24h."""
    try:
        p = float(pct or 0)
    except Exception:
        p = 0.0
    return ("🟢▲" if p > 0.05 else ("🔴▼" if p < -0.05 else "⚪")) + ("%+.1f%%" % p)

def _ascii(s):
    """Nettoie pour le PDF (police latin-1)."""
    return (str(s or "")).encode("latin-1", "replace").decode("latin-1")

# ========================================================================
# MOTEUR D'AGENTS IA (Claude)
# ------------------------------------------------------------------------
# 4 agents specialises + une synthese. Chacun recoit une PHOTO chiffree du
# patrimoine (finance_snapshot) et rend des recommandations STRUCTUREES (JSON).
# Appele a la demande (app -> /agents) et en automatique (brief Discord).
# ========================================================================

def ai_metrics():
    """Metriques cles du moment, pour la MEMOIRE des agents (comparaison inter-briefs)."""
    c = consolidated_total()
    _, exp7, _, _ = momo_totals_period(7)
    _, exp30, _, _ = momo_totals_period(30)
    b = STATE.get("business") or {}
    return {
        "total": int(round(c["total"])), "momo": int(round(c["momo"])),
        "nsia": int(round(c["nsia"])), "bitget_usd": round(c["bitget_usd"], 2),
        "exp7": int(round(exp7)), "exp30": int(round(exp30)),
        "revM": int(round(float(b.get("revM", 0) or 0))),
        "expM": int(round(float(b.get("expM", 0) or 0))),
        "netM": int(round(float(b.get("netM", 0) or 0))),
        "catexp": dict(STATE.get("catexp") or {}),
    }

def _prev_ai_snap():
    """Dernier bilan enregistre un AUTRE jour (le 'brief precedent'), sinon le dernier."""
    snaps = STATE.get("ai_snaps") or []
    if not snaps:
        return None
    today = today_wat().isoformat()
    for s in reversed(snaps):
        if s.get("d") != today:
            return s
    return snaps[-1]

def record_ai_snapshot():
    """Enregistre (ou met a jour) le bilan chiffre du JOUR dans la memoire des agents."""
    snaps = STATE.setdefault("ai_snaps", [])
    today = today_wat().isoformat()
    point = {"d": today, **ai_metrics()}
    if snaps and snaps[-1].get("d") == today:
        snaps[-1] = point
    else:
        snaps.append(point)
    del snaps[:-30]           # ~30 bilans conserves
    save_state()
    return point

def _evolution_block():
    """Texte de comparaison au bilan precedent : deltas patrimoine, depenses, categories."""
    prev = _prev_ai_snap()
    if not prev:
        return ""
    cur = ai_metrics()
    lines = ["\n# ÉVOLUTION DEPUIS LE DERNIER BILAN (%s)" % prev.get("d", "?")]
    def line(lbl, now, before, better_down=False):
        if not before:
            return None
        pct = (now - before) / abs(before) * 100.0
        arrow = "▲" if now > before else ("▼" if now < before else "=")
        return "  %s : %s -> %s (%s%+.1f%%)" % (lbl, fmt_xof(before), fmt_xof(now), arrow, pct)
    for lbl, k in (("Patrimoine total", "total"), ("Dépenses 30 j", "exp30"),
                   ("Dépenses du mois", "expM"), ("Revenus du mois", "revM")):
        l = line(lbl, cur.get(k, 0), prev.get(k, 0))
        if l:
            lines.append(l)
    # Deltas par categorie de depense (permet "resto +18%")
    pc, cc = prev.get("catexp") or {}, cur.get("catexp") or {}
    deltas = []
    for cat, val in cc.items():
        old = pc.get(cat)
        if old:
            pct = (val - old) / abs(old) * 100.0
            if abs(pct) >= 10:
                deltas.append((abs(pct), "  %s : %s -> %s (%+.0f%%)" % (cat, fmt_xof(old), fmt_xof(val), pct)))
    if deltas:
        lines.append("  Catégories qui bougent le plus :")
        for _, txt in sorted(deltas, reverse=True)[:5]:
            lines.append("  " + txt)
    return "\n".join(lines) if len(lines) > 1 else ""

def _objectifs_block():
    """Texte des budgets / objectifs / dettes / DCA (agent Objectifs)."""
    o = STATE.get("objectifs") or {}
    if not o:
        return ""
    lines = ["\n# BUDGETS, OBJECTIFS & DETTES (suivi app)"]
    for b in (o.get("budgets") or []):
        lim, sp = b.get("limit", 0), b.get("spent", 0)
        pct = (sp / lim * 100.0) if lim else 0
        flag = " ⚠️ DÉPASSÉ" if lim and sp > lim else (" (proche)" if pct >= 80 else "")
        lines.append("  Budget %s : %s / %s (%.0f%%)%s" % (b.get("cat", "?"), fmt_xof(sp), fmt_xof(lim), pct, flag))
    for g in (o.get("goals") or []):
        cur_, tgt = g.get("current", 0), g.get("target", 0)
        pct = (cur_ / tgt * 100.0) if tgt else 0
        dl = (" · échéance %s" % g.get("deadline")) if g.get("deadline") else ""
        lines.append("  Objectif %s : %s / %s (%.0f%%)%s" % (g.get("name", "?"), fmt_xof(cur_), fmt_xof(tgt), pct, dl))
    for d in (o.get("debts") or []):
        lines.append("  Dette %s : reste %s (initiale %s)%s"
                     % (d.get("name", "?"), fmt_xof(d.get("balance", 0)), fmt_xof(d.get("original", 0)),
                        (" · échéance %s" % d.get("due")) if d.get("due") else ""))
    for d in (o.get("dcas") or []):
        lines.append("  DCA %s : %s / %s%s"
                     % (d.get("asset", "?"), fmt_xof(d.get("amount", 0)), d.get("freq", "?"),
                        " (auto)" if d.get("auto") else ""))
    return "\n".join(lines) if len(lines) > 1 else ""

def finance_snapshot():
    """Photo chiffree complete de la situation, en texte compact pour le modele.
    Toutes les briques de calcul deja fiabilisees (et testees) sont reutilisees :
    consolidation, soldes par reseau (capture vs flux), periodes glissantes, NSIA,
    Bitget, resume business, historique/tendance."""
    c = consolidated_total()
    src = momo_balance_sources()
    lines = ["# PATRIMOINE (au %s, heure Benin)" % now_wat().strftime("%d/%m/%Y %H:%M")]
    lines.append("Total consolide : %s" % fmt_xof(c["total"]))
    lines.append("  - Mobile Money : %s" % fmt_xof(c["momo"]))
    for net, v in (c.get("bynet") or {}).items():
        lines.append("      %s : %s (%s)" % (net.upper(), fmt_xof(v),
                     "solde reel" if src.get(net) == "capture" else "estime d'apres les flux"))
    lines.append("  - NSIA OPCVM   : %s" % fmt_xof(c["nsia"]))
    lines.append("  - Bitget crypto: %s (= %s au taux %.0f FCFA/USD)"
                 % (fmt_usd(c["bitget_usd"]), fmt_xof(c["bitget_xof"]), get_usd_xof()))

    # Flux Mobile Money sur plusieurs fenetres
    lines.append("\n# FLUX MOBILE MONEY (fenetres glissantes)")
    for d, lbl in ((7, "7 jours"), (30, "30 jours"), (90, "90 jours")):
        inc, exp, net, cnt = momo_totals_period(d)
        lines.append("  %s : entrees %s | sorties %s | net %s | %d operation(s)"
                     % (lbl, fmt_xof(inc), fmt_xof(exp), fmt_xof(net), cnt))

    # NSIA detail
    nsia = STATE.get("nsia") or {}
    if nsia:
        lines.append("\n# NSIA (OPCVM)")
        lines.append("  Total %s | investi %s | +/- value latente %s | +/- value realisee %s"
                     % (fmt_xof(nsia.get("total", 0)), fmt_xof(nsia.get("invested", 0)),
                        fmt_xof(nsia.get("pv_latente", 0)), fmt_xof(nsia.get("pv_realisee", 0))))

    # Bitget : principales positions
    bg = STATE.get("bitget") or {}
    hold = sorted((bg.get("holdings") or []), key=lambda h: -float(h.get("val", 0) or 0))[:8]
    if hold:
        lines.append("\n# BITGET — principales positions (USD)")
        for h in hold:
            lines.append("  %s : %s" % (str(h.get("coin", "?")), fmt_usd(h.get("val", 0))))

    # Business (revenus / depenses pousses par l'app)
    b = STATE.get("business") or {}
    if b:
        curb = b.get("cur", "XOF")
        def _fb(n):
            try:
                return fmt_xof(n) if curb == "XOF" else "%s %s" % (int(round(float(n or 0))), curb)
            except Exception:
                return str(n)
        lines.append("\n# REVENUS & DEPENSES (mois courant)")
        lines.append("  Revenus %s | Depenses %s | Net %s" % (_fb(b.get("revM")), _fb(b.get("expM")), _fb(b.get("netM"))))
        if b.get("mrr"):
            lines.append("  MRR %s | ARR %s" % (_fb(b.get("mrr")), _fb(b.get("arr"))))

    # Historique / tendance
    hist = STATE.get("history") or []
    if len(hist) >= 2:
        last = hist[-1]["total"]
        def _delta(days):
            if len(hist) > days and hist[-1 - days].get("total"):
                base = hist[-1 - days]["total"]
                return "%+.1f%% (%s -> %s)" % ((last - base) / base * 100.0, fmt_xof(base), fmt_xof(last))
            return "n/d"
        lines.append("\n# TENDANCE PATRIMOINE")
        lines.append("  Sur 7 j : %s" % _delta(7))
        lines.append("  Sur 30 j : %s" % _delta(30))
        lines.append("  Courbe (30 derniers points) : %s" % sparkline([p["total"] for p in hist[-30:]]))
    obj = _objectifs_block()
    if obj:
        lines.append(obj)
    evo = _evolution_block()
    if evo:
        lines.append(evo)
    return "\n".join(lines)


# Regle de sortie commune : chaque agent DOIT renvoyer un objet JSON de cette forme.
_AGENT_JSON_RULE = (
    "Reponds UNIQUEMENT par un objet JSON valide, sans texte autour, sans bloc de code, "
    "de la forme exacte : "
    '{"score": <entier 0-100>, "resume": "<1-2 phrases>", '
    '"recommandations": [{"titre": "<court>", "detail": "<explication concrete>", '
    '"impact": "<gain chiffre estime, ex: +45 000 FCFA/mois ou n/d>", '
    '"priorite": "haute|moyenne|basse", '
    '"action": <null OU un objet applicable dans l\'app>}]}. '
    "Le champ 'action' est OPTIONNEL : mets-le a null sauf si la recommandation se traduit "
    "par une action concrete et sure a proposer. Formes autorisees UNIQUEMENT : "
    '{"type":"budget","categorie":"<nom>","montant":<FCFA/mois>} pour plafonner une categorie ; '
    '{"type":"objectif","nom":"<nom>","cible":<FCFA>,"echeance":"<AAAA-MM-JJ ou vide>"} pour creer un objectif d\'epargne ; '
    '{"type":"dca","actif":"<BTC|ETH|SOL...>","montant":<FCFA>,"frequence":"Hebdomadaire|Mensuel"} pour programmer un DCA. '
    "N'invente pas d'autres types. "
    "Donne 2 a 4 recommandations, classees de la plus prioritaire a la moins prioritaire. "
    "Chiffre l'impact quand c'est possible a partir des donnees. Ecris en francais, montants en FCFA. "
    "Ne conseille jamais un produit financier precis ni un ordre d'achat/vente nominal : tu informes, tu n'es pas conseiller agree."
)

AGENTS = {
    "patrimoine": {
        "emoji": "🏦", "name": "Patrimoine & projection",
        "sys": ("Tu es l'agent PATRIMOINE de NEXUS. Tu consolides Mobile Money + NSIA + Bitget, "
                "tu juges la structure (repartition, liquidite, concentration, part crypto volatile), "
                "tu reperes toute variation anormale et tu proposes comment stabiliser et faire croitre "
                "le patrimoine total. " + _AGENT_JSON_RULE)},
    "depenses": {
        "emoji": "💸", "name": "Optimisation depenses",
        "sys": ("Tu es l'agent DEPENSES de NEXUS. A partir des flux Mobile Money et du resume "
                "revenus/depenses, tu reperes les postes qui derivent, les sorties recurrentes ou "
                "evitables, et tu proposes des coupes concretes et chiffrees sans degrader le niveau de vie. "
                + _AGENT_JSON_RULE)},
    "epargne": {
        "emoji": "🎯", "name": "Epargne & auto-financement",
        "sys": ("Tu es l'agent EPARGNE de NEXUS. Tu calcules le taux d'epargne (net/revenus) et le taux "
                "d'auto-financement (revenus passifs / depenses), tu evalues la trajectoire vers "
                "l'independance financiere et tu proposes comment augmenter l'epargne et les revenus passifs. "
                + _AGENT_JSON_RULE)},
    "invest": {
        "emoji": "📈", "name": "Investissement",
        "sys": ("Tu es l'agent INVESTISSEMENT de NEXUS. Tu analyses le portefeuille Bitget + NSIA : "
                "diversification, concentration, part de stablecoins, positions en perte, exposition au risque, "
                "et pertinence du rythme de DCA. Tu proposes des ajustements de repartition et de timing, "
                "en termes generaux (classes d'actifs, %), jamais un ordre nominal. " + _AGENT_JSON_RULE)},
    "objectifs": {
        "emoji": "🎯", "name": "Objectifs & budgets",
        "sys": ("Tu es l'agent OBJECTIFS de NEXUS. Tu suis les BUDGETS (dépassements, catégories proches "
                "de la limite), les OBJECTIFS d'épargne (avancement vs échéance : est-il tenable ?), les DETTES "
                "(rythme de remboursement) et les plans DCA. Tu alertes sur chaque dérapage et proposes des "
                "ajustements concrets (créer/relever un budget, créer un objectif, cadencer un DCA). " + _AGENT_JSON_RULE)},
}
AGENT_ORDER = ["patrimoine", "depenses", "epargne", "invest", "objectifs"]


def _ai_headers():
    return {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json", "anthropic-beta": ANTHROPIC_BETA}


async def _anthropic_text(session, system, user, max_tokens=3000, model=None, effort=None):
    """Un appel Claude non-stream cote serveur -> texte du 1er bloc 'text'.
    Leve une exception en cas d'erreur (a rattraper par l'appelant)."""
    model = model or AI_MODEL
    payload = {"model": model, "max_tokens": min(int(max_tokens), AI_MAX_TOKENS),
               "system": system, "messages": [{"role": "user", "content": user}]}
    if model in AI_THINK_MODELS:
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": (effort or AI_EFFORT)}
    if model in ("claude-opus-5", "claude-fable-5-1"):
        payload["fallbacks"] = "default"     # repli serveur sur refus (cf. skill claude-api)
    async with session.post(ANTHROPIC_URL, json=payload, headers=_ai_headers(),
                            timeout=aiohttp.ClientTimeout(total=180)) as r:
        data = await r.json()
    if r.status >= 400:
        raise RuntimeError((data.get("error") or {}).get("message") or ("HTTP %d" % r.status))
    for blk in (data.get("content") or []):
        if blk.get("type") == "text":
            return blk.get("text") or ""
    return ""


# ----------------------- Fournisseur OpenAI-compatible (DeepSeek, OpenRouter, Groq, Ollama...) -----------------------
def _openai_url():
    """URL du endpoint chat/completions a partir de AI_BASE_URL (regle simple et sure)."""
    b = AI_BASE_URL
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def _openai_headers():
    h = {"content-type": "application/json"}
    if AI_API_KEY:
        h["Authorization"] = "Bearer " + AI_API_KEY
    return h


async def _openai_text(session, system, user, max_tokens=3000, want_json=True):
    """Un appel non-stream vers une API compatible OpenAI -> texte de la reponse.
    Reessaie sans response_format si le fournisseur ne le supporte pas."""
    base = {"model": AI_MODEL, "max_tokens": min(int(max_tokens), AI_MAX_TOKENS),
            "temperature": 0.4,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    attempts = []
    if want_json and AI_JSON_MODE:
        attempts.append(dict(base, response_format={"type": "json_object"}))
    attempts.append(base)
    last_err = None
    for payload in attempts:
        try:
            async with session.post(_openai_url(), json=payload, headers=_openai_headers(),
                                    timeout=aiohttp.ClientTimeout(total=180)) as r:
                data = await r.json()
            if r.status >= 400:
                last_err = (data.get("error") or {}).get("message") or ("HTTP %d" % r.status)
                continue        # p.ex. response_format non supporte -> on retombe sur le payload nu
            ch = (data.get("choices") or [{}])[0]
            return ((ch.get("message") or {}).get("content")) or ""
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err or "erreur fournisseur IA")


async def _ai_text(session, system, user, max_tokens=3000, effort=None, want_json=True):
    """Appel IA non-stream, agnostique du fournisseur (Anthropic OU OpenAI-compatible)."""
    if AI_PROVIDER == "openai":
        return await _openai_text(session, system, user, max_tokens=max_tokens, want_json=want_json)
    return await _anthropic_text(session, system, user, max_tokens=max_tokens, effort=effort)


def _flatten_content(c):
    """Contenu d'un message (chaine, ou blocs facon Anthropic) -> chaine simple."""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return str(c or "")


async def _openai_proxy(request, body, session):
    """Relaie le chat de l'app vers une API compatible OpenAI, en TRADUISANT :
       - la requete (system+messages facon Anthropic) -> chat/completions ;
       - le flux SSE OpenAI -> evenements SSE facon Anthropic que l'app sait lire.
    Ainsi l'app et sa boucle de streaming restent inchangees quel que soit le fournisseur."""
    system = body.get("system")
    omsgs = []
    if system:
        omsgs.append({"role": "system", "content": _flatten_content(system)})
    for m in (body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        omsgs.append({"role": role, "content": _flatten_content(m.get("content"))})
    try:
        max_tok = min(int(body.get("max_tokens") or AI_MAX_TOKENS), AI_MAX_TOKENS)
    except Exception:
        max_tok = AI_MAX_TOKENS
    want_stream = bool(body.get("stream"))
    payload = {"model": AI_MODEL, "messages": omsgs, "max_tokens": max_tok,
               "temperature": 0.4, "stream": want_stream}
    fin_map = {"length": "max_tokens", "stop": "end_turn", "content_filter": "refusal"}

    if not want_stream:
        async with session.post(_openai_url(), json=payload, headers=_openai_headers(),
                                timeout=aiohttp.ClientTimeout(total=180)) as up:
            data = await up.json()
            status = up.status
        if status >= 400:
            msg = (data.get("error") or {}).get("message") or ("HTTP %d" % status)
            return cors(web.json_response({"error": {"message": msg}}, status=status), request)
        ch = (data.get("choices") or [{}])[0]
        txt = ((ch.get("message") or {}).get("content")) or ""
        # Forme Anthropic -> claudeText() cote app fonctionne a l'identique.
        return cors(web.json_response({"content": [{"type": "text", "text": txt}],
                                       "stop_reason": "end_turn"}), request)

    up = await session.post(_openai_url(), json=payload, headers=_openai_headers(),
                            timeout=aiohttp.ClientTimeout(total=180))
    resp = web.StreamResponse(status=(200 if up.status < 400 else up.status))
    resp.headers["Content-Type"] = "text/event-stream"
    cors(resp, request)
    await resp.prepare(request)

    async def emit(obj):
        await resp.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))

    try:
        if up.status >= 400:
            try:
                data = await up.json()
                msg = (data.get("error") or {}).get("message") or ("HTTP %d" % up.status)
            except Exception:
                msg = "HTTP %d" % up.status
            await emit({"type": "error", "error": {"message": msg}})
            await resp.write_eof()
            return resp
        buf = ""
        async for chunk in up.content.iter_any():
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                ds = line[5:].strip()
                if ds == "[DONE]":
                    continue
                try:
                    ev = json.loads(ds)
                except Exception:
                    continue
                ch = (ev.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    await emit({"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}})
                fr = ch.get("finish_reason")
                if fr:
                    await emit({"type": "message_delta", "delta": {"stop_reason": fin_map.get(fr, "end_turn")}})
        await resp.write_eof()
    except Exception as e:
        try:
            await emit({"type": "error", "error": {"message": str(e)}})
            await resp.write_eof()
        except Exception:
            pass
    finally:
        up.release()
    return resp


def _scan_balanced_json(t):
    """Renvoie le 1er objet JSON EQUILIBRE et parseable trouve dans `t`, en scannant
    depuis chaque '{' (en respectant les chaines). Rattrape les sorties malformees
    frequentes des modeles : accolade dupliquee en tete ('{ { ... }'), texte autour,
    accolade parasite. Renvoie None si rien de parseable."""
    start = t.find("{")
    while start != -1:
        depth = 0
        instr = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            else:
                if ch == '"':
                    instr = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start:i + 1])
                        except Exception:
                            break            # ce '{' ne donne pas un objet valide -> suivant
        start = t.find("{", start + 1)
    return None

def _parse_agent_json(text):
    """Extrait l'objet JSON d'une reponse d'agent, defensivement (le modele peut
    entourer le JSON de texte, d'un bloc de code, ou dupliquer une accolade)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    obj = _scan_balanced_json(t)
    if isinstance(obj, dict):
        return obj
    return {"score": None, "resume": t[:400], "recommandations": []}


async def run_agent(session, key, question="", snap=None, registry=None):
    """Lance un agent et renvoie son resultat structure (jamais d'exception).
    `registry` permet de choisir la famille d'agents (AGENTS par defaut, CRYPTO_AGENTS...)."""
    registry = registry if registry is not None else AGENTS
    meta = registry.get(key)
    if not meta:
        return {"key": key, "error": "agent inconnu"}
    if snap is None:
        snap = finance_snapshot()
    user = "Voici la situation a analyser :\n\n" + snap
    if question:
        user += "\n\nQuestion prioritaire de l'utilisateur : " + question
    try:
        txt = await _ai_text(session, meta["sys"], user, max_tokens=2500)
        obj = _parse_agent_json(txt)
    except Exception as e:
        log.warning("agent %s: %s", key, e)
        return {"key": key, "emoji": meta["emoji"], "name": meta["name"], "error": str(e)}
    recs = obj.get("recommandations") or obj.get("recommendations") or []
    return {"key": key, "emoji": meta["emoji"], "name": meta["name"],
            "score": obj.get("score"), "resume": obj.get("resume") or "",
            "recommandations": recs[:4]}


async def _synthesize(session, agents, snap, lead_sys=None):
    """Consolide les recommandations d'un groupe d'agents en un plan priorise unique.
    Filet garanti : si le modele ne rend rien, on reconstruit depuis les recos 'haute'."""
    synth = None
    digest = []
    for a in agents:
        if a.get("error"):
            continue
        for r in (a.get("recommandations") or []):
            digest.append("[%s] %s — %s (impact %s, priorite %s)"
                          % (a.get("name", ""), r.get("titre", ""), r.get("detail", ""),
                             r.get("impact", "n/d"), r.get("priorite", "?")))
    if digest:
        sys_p = lead_sys or ("Tu es l'ORCHESTRATEUR de NEXUS. On te donne les recommandations de plusieurs "
                             "agents financiers. Tu produis un plan d'action unique et priorise. " + _AGENT_JSON_RULE
                             + " Le champ 'recommandations' contient les 3 a 5 actions les PLUS importantes, tous agents confondus.")
        try:
            txt = await _ai_text(session, sys_p, "Recommandations des agents :\n" + "\n".join(digest)
                                 + "\n\nSituation :\n" + snap, max_tokens=2000)
            synth = _parse_agent_json(txt)
            if isinstance(synth, dict):
                synth["recommandations"] = (synth.get("recommandations")
                                            or synth.get("recommendations") or [])[:5]
        except Exception as e:
            log.warning("synthese: %s", e)
    if not (synth and synth.get("recommandations")):
        top = [r for a in agents for r in (a.get("recommandations") or [])
               if (r.get("priorite") or "").lower() == "haute"]
        if not top:
            top = [a["recommandations"][0] for a in agents if a.get("recommandations")]
        if top:
            synth = {"score": None, "resume": "Actions prioritaires consolidées à partir des agents.",
                     "recommandations": top[:5]}
    return synth


async def run_all_agents(session, question=""):
    """Lance les 4 agents en parallele + une synthese globale des priorites."""
    if not ai_enabled():
        return {"ok": False, "error": "IA non configuree (definir la cle du fournisseur : ANTHROPIC_API_KEY, ou AI_PROVIDER=openai + AI_API_KEY)."}
    snap = finance_snapshot()
    results = await asyncio.gather(*[run_agent(session, k, question, snap) for k in AGENT_ORDER])
    agents = list(results)
    synth = await _synthesize(session, agents, snap)
    try:
        record_ai_snapshot()     # memoire : ce brief devient le point de comparaison suivant
    except Exception as e:
        log.warning("record_ai_snapshot: %s", e)
    return {"ok": True, "agents": agents, "synthese": synth, "ts": int(time.time())}


# ========================================================================
# DIVISION CRYPTO (Bitget) — contrôle d'exactitude + analyse dédiée
# ========================================================================
_STABLES = {"USDT", "USDC", "USD", "BUSD", "DAI", "TUSD", "FDUSD"}

def _coin_base(c):
    """Nom de coin nu (retire le suffixe '⟢Earn' et met en majuscules)."""
    return str(c or "").split(" ")[0].upper()

async def bitget_reconcile(session):
    """VÉRIFICATION DÉTERMINISTE (pas de l'IA) de l'exactitude des données Bitget :
    compare le direct (API Bitget signée) à l'état stocké/affiché, et signale tout écart,
    péremption, ou couverture manquante (staking non vu par l'API). Base de l'agent Intégrité."""
    findings = []
    live = None
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        findings.append({"niveau": "erreur", "msg": "Clés Bitget non configurées côté serveur."})
        return {"live": None, "stored": STATE.get("bitget") or {}, "findings": findings, "ok": False}
    try:
        live = await bitget_overview(session, force=True)   # force = ignore le cache
    except Exception as e:
        findings.append({"niveau": "erreur", "msg": "API Bitget injoignable : %s" % e})
    stored = STATE.get("bitget") or {}
    calib = STATE.get("bg_calib") or {}
    now = time.time()
    # 1) Fraîcheur de l'état stocké
    ts = (stored.get("ts") or 0) / 1000.0
    if ts:
        age_min = (now - ts) / 60.0
        if age_min > 15:
            findings.append({"niveau": "attention", "msg": "État Bitget stocké vieux de %d min (resynchroniser)." % age_min})
        else:
            findings.append({"niveau": "ok", "msg": "État stocké frais (%d min)." % age_min})
    if live:
        lt = float(live.get("total") or 0)
        st = float(stored.get("total") or 0)
        # 2) Écart total direct vs stocké
        if st and abs(lt - st) / max(st, 1e-9) > 0.02:
            findings.append({"niveau": "attention",
                             "msg": "Écart total direct vs stocké : %s vs %s (%.1f%%) — resync conseillé."
                                    % (fmt_usd(lt), fmt_usd(st), (lt - st) / st * 100.0)})
        elif st:
            findings.append({"niveau": "ok", "msg": "Total direct ≈ stocké (%s)." % fmt_usd(lt)})
        # 3) Cohérence interne : somme des positions (spot+earn) vs total tous comptes
        hold = live.get("holdings") or []
        hsum = sum(float(v or 0) for _, _, v in hold)
        others = float(live.get("others") or 0)
        gap = lt - hsum
        if others > 1 and abs(gap - others) / max(others, 1e-9) > 0.15:
            findings.append({"niveau": "info",
                             "msg": "%s hors spot/earn (bots/futures) non détaillés par position." % fmt_usd(others)})
        # 4) Positions valorisées à 0 (prix manquant) = chiffre potentiellement faux
        zeros = [_coin_base(c) for c, a, v in hold if float(a or 0) > 0 and float(v or 0) <= 0 and _coin_base(c) not in _STABLES]
        if zeros:
            findings.append({"niveau": "attention",
                             "msg": "Position(s) sans prix (valorisées à 0) : %s — total sous-estimé." % ", ".join(sorted(set(zeros))[:8])})
        # 5) Calage staking manuel (le staking hors API n'est compté que s'il est calé)
        stk = {k: v for k, v in (calib.get("staking") or {}).items() if v}
        extra = float(calib.get("extra") or 0)
        if stk or extra:
            det = ", ".join("%s=%s" % (k, v) for k, v in stk.items())
            findings.append({"niveau": "info", "msg": "Calage manuel actif%s%s."
                             % ((" (staking : %s)" % det) if det else "", (" · extra %s" % fmt_usd(extra)) if extra else "")})
        else:
            findings.append({"niveau": "info",
                             "msg": "Aucun calage staking manuel : un staking non remonté par l'API ne serait pas compté."})
    return {"live": live, "stored": stored, "calib": calib,
            "findings": findings, "ok": not any(f["niveau"] == "erreur" for f in findings)}

async def crypto_snapshot(session):
    """Photo détaillée du compte crypto (Bitget) pour la division crypto, avec le bloc
    de contrôle d'exactitude déterministe. Renvoie (texte, reconcile)."""
    rec = await bitget_reconcile(session)
    live = rec.get("live") or {}
    stored = rec.get("stored") or {}
    hold = live.get("holdings") or [(_coin_base(h.get("coin")), h.get("amt"), h.get("val"))
                                    for h in (stored.get("holdings") or [])]
    total = float(live.get("total") or stored.get("total") or 0)
    lines = ["# COMPTE BITGET (crypto) — au %s" % now_wat().strftime("%d/%m/%Y %H:%M")]
    lines.append("Total tous comptes : %s (= %s)" % (fmt_usd(total), fmt_xof(total * get_usd_xof())))
    lines.append("  Spot %s · Earn %s · Autres (bots/futures) %s"
                 % (fmt_usd(live.get("spot", 0)), fmt_usd(live.get("earn", 0)), fmt_usd(live.get("others", 0))))
    # Agrégation par coin (spot + earn regroupés)
    agg = {}
    for c, a, v in hold:
        b = _coin_base(c)
        agg[b] = agg.get(b, 0.0) + float(v or 0)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1])
    stable_val = sum(v for c, v in agg.items() if c in _STABLES)
    lines.append("Part stablecoins : %.0f%% · positions distinctes : %d"
                 % ((stable_val / total * 100) if total else 0, len([c for c in agg if c not in _STABLES])))
    if ranked and total:
        lines.append("Concentration 1re position (%s) : %.0f%%" % (ranked[0][0], ranked[0][1] / total * 100))
    ch = live.get("change") or {}
    lines.append("\n# POSITIONS (valeur, poids, variation 24h)")
    for c, v in ranked[:15]:
        w = (v / total * 100) if total else 0
        chg = ch.get(c)
        chs = (" · %+.1f%%/24h" % chg) if isinstance(chg, (int, float)) else ""
        lines.append("  %s : %s · %.1f%%%s" % (c, fmt_usd(v), w, chs))
    dcas = ((STATE.get("objectifs") or {}).get("dcas")) or []
    if dcas:
        lines.append("\n# PLANS DCA ACTIFS")
        for d in dcas:
            lines.append("  %s : %s / %s%s" % (d.get("asset"), fmt_xof(d.get("amount", 0)),
                                               d.get("freq", "?"), " (auto)" if d.get("auto") else ""))
    else:
        lines.append("\n# PLANS DCA ACTIFS : aucun")
    lines.append("\n# CONTRÔLE D'EXACTITUDE DES DONNÉES (déterministe, calculé par le serveur)")
    for f in rec["findings"]:
        lines.append("  [%s] %s" % (f["niveau"].upper(), f["msg"]))
    if not rec["findings"]:
        lines.append("  RAS.")
    return "\n".join(lines), rec

_CRYPTO_RULE = (_AGENT_JSON_RULE +
    " CONTEXTE CRYPTO : marché très volatil ; tu raisonnes en construction de portefeuille "
    "(poids, risque, diversification, discipline), JAMAIS en prédiction de prix ni en ordre nominal "
    "d'achat/vente. Les actions 'dca' que tu proposes sont des automatisations que l'utilisateur "
    "choisit d'activer, pas un ordre. Rappelle la prudence quand c'est pertinent.")

CRYPTO_AGENTS = {
    "integrite": {
        "emoji": "🔎", "name": "Intégrité des données Bitget",
        "sys": ("Tu es l'agent INTÉGRITÉ de la division crypto de NEXUS. On te donne un CONTRÔLE "
                "D'EXACTITUDE déterministe (direct API Bitget vs état affiché). Ton rôle : dire si on "
                "PEUT SE FIER aux chiffres affichés, expliquer chaque écart/anomalie en clair, et proposer "
                "la correction concrète (resynchroniser, caler le staking manquant, revérifier une position "
                "sans prix). Le 'score' = niveau de confiance dans les données (0=faux, 100=exact). " + _CRYPTO_RULE)},
    "allocation": {
        "emoji": "⚖️", "name": "Allocation & risque",
        "sys": ("Tu es l'agent ALLOCATION de la division crypto. Tu analyses la répartition : concentration "
                "sur une position, part de stablecoins, diversification, exposition au risque. Tu proposes des "
                "ajustements de poids en termes généraux (%, classes), pour un portefeuille plus robuste. " + _CRYPTO_RULE)},
    "dca": {
        "emoji": "🤖", "name": "Stratégie DCA",
        "sys": ("Tu es l'agent DCA de la division crypto. Tu évalues les plans DCA actuels (cadence, montant, "
                "actifs) et la trésorerie disponible, et tu proposes des ajustements : augmenter/lisser un DCA, "
                "en créer un sur un actif sous-pondéré, adapter la fréquence. Chiffre l'effort mensuel. " + _CRYPTO_RULE)},
    "performance": {
        "emoji": "📈", "name": "Performance & rééquilibrage",
        "sys": ("Tu es l'agent PERFORMANCE de la division crypto. Tu repères les positions en forte hausse/baisse "
                "(via la variation 24h et les poids), les surpondérations à alléger et les sous-pondérations, et tu "
                "proposes une logique de rééquilibrage prudente (bandes de tolérance), sans timing de marché. " + _CRYPTO_RULE)},
    "opportunites": {
        "emoji": "💡", "name": "Analyse par actif",
        "sys": ("Tu es l'agent ANALYSE PAR ACTIF de la division crypto. Pour les principales cryptos détenues, tu "
                "décris le rôle de chacune dans le portefeuille (cœur, satellite, stable, spéculatif), son poids et "
                "son risque relatif, et tu suggères si sa place mérite d'être renforcée, tenue ou allégée — en termes "
                "de construction de portefeuille, jamais comme un conseil d'achat nominal. " + _CRYPTO_RULE)},
}
CRYPTO_ORDER = ["integrite", "allocation", "dca", "performance", "opportunites"]

async def run_crypto_division(session, question=""):
    """La division crypto au complet : contrôle d'exactitude + 4 agents d'analyse + synthèse."""
    if not ai_enabled():
        return {"ok": False, "error": "IA non configurée."}
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return {"ok": False, "error": "Bitget non configuré (clés serveur)."}
    snap, rec = await crypto_snapshot(session)
    results = await asyncio.gather(*[run_agent(session, k, question, snap, registry=CRYPTO_AGENTS)
                                     for k in CRYPTO_ORDER])
    agents = list(results)
    lead = ("Tu es le CHEF DE LA DIVISION CRYPTO de NEXUS. On te donne les analyses de tes agents "
            "(intégrité des données, allocation, DCA, performance, analyse par actif). Tu produis le plan "
            "d'action crypto priorisé. " + _CRYPTO_RULE
            + " 'recommandations' = les 3 à 5 actions crypto les plus importantes.")
    synth = await _synthesize(session, agents, snap, lead_sys=lead)
    # Le contrôle d'exactitude déterministe est renvoyé tel quel (source de vérité, pas de l'IA).
    return {"ok": True, "agents": agents, "synthese": synth,
            "reconcile": rec.get("findings"), "reconcile_ok": rec.get("ok"), "ts": int(time.time())}


# ----------------------- Actions IA (proposees -> appliquees dans l'app) -----------------------
_VALID_ACTION_TYPES = ("budget", "objectif", "dca")

def _clean_action(a):
    """Valide/normalise une action proposee par un agent. None si invalide."""
    if not isinstance(a, dict):
        return None
    t = str(a.get("type") or "").lower()
    if t not in _VALID_ACTION_TYPES:
        return None
    mont = _num_or_none(a.get("montant"))
    if t == "budget":
        cat = _s(a.get("categorie") or a.get("cat"))
        if not cat or mont is None or mont <= 0:
            return None
        return {"type": "budget", "categorie": cat, "montant": int(round(mont)),
                "label": "Budget %s : %s" % (cat, fmt_xof(mont))}
    if t == "objectif":
        nom = _s(a.get("nom") or a.get("name"))
        cible = _num_or_none(a.get("cible") or a.get("target"))
        if not nom or cible is None or cible <= 0:
            return None
        return {"type": "objectif", "nom": nom, "cible": int(round(cible)),
                "echeance": _s(a.get("echeance") or a.get("deadline"), 20),
                "label": "Objectif %s : %s" % (nom, fmt_xof(cible))}
    if t == "dca":
        actif = _s(a.get("actif") or a.get("asset"), 16).upper()
        freq = _s(a.get("frequence") or a.get("freq"), 16) or "Mensuel"
        if not actif or mont is None or mont <= 0:
            return None
        return {"type": "dca", "actif": actif, "montant": int(round(mont)), "frequence": freq,
                "label": "DCA %s : %s / %s" % (actif, fmt_xof(mont), freq)}
    return None

def collect_actions(data):
    """Extrait de toutes les recommandations (synthese + agents) les actions valides,
    dedoublonnees par label. Renvoie une liste [{type,...,label}]."""
    seen, out = set(), []
    buckets = []
    if data.get("synthese"):
        buckets.append(data["synthese"].get("recommandations") or [])
    for a in (data.get("agents") or []):
        buckets.append(a.get("recommandations") or [])
    for recs in buckets:
        for r in recs:
            act = _clean_action((r or {}).get("action"))
            if act and act["label"] not in seen:
                seen.add(act["label"])
                out.append(act)
    return out

def register_ai_actions(data):
    """Enregistre les actions proposees comme 'pending' dans l'etat (pour les boutons
    Discord). Purge les anciennes non appliquees. Renvoie la liste avec un id stable."""
    acts = collect_actions(data)
    queue = []
    for a in acts[:10]:
        a = dict(a, id=("act%d" % new_id()), status="pending")
        queue.append(a)
    STATE["ai_actions"] = queue      # on ne garde que la derniere fournee
    save_state()
    return queue


# ----------------------- Proxy /ai (clé côté serveur) -----------------------
async def h_ai(request):
    """Relaie les appels Claude de l'app vers l'API Anthropic AVEC la cle du serveur.
    L'app n'a plus la cle : elle s'authentifie a NEXUS (jeton/cookie) et NEXUS signe.
    Supporte le streaming (SSE) pour l'assistant chat."""
    denied = guard(request)
    if denied is not None:
        return denied
    if not ai_enabled():
        return cors(web.json_response(
            {"error": {"message": "IA non configuree : definis ANTHROPIC_API_KEY, ou AI_PROVIDER=openai + AI_API_KEY (DeepSeek, OpenRouter...)."}},
            status=503), request)
    raw = await request.content.read(300001)
    if len(raw) > 300000:
        return cors(web.json_response({"error": {"message": "Requete trop volumineuse."}}, status=413), request)
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return cors(web.json_response({"error": {"message": "JSON invalide."}}, status=400), request)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return cors(web.json_response({"error": {"message": "Corps attendu : {messages:[...]}."}}, status=400), request)
    session = request.app["session"]
    # Fournisseur OpenAI-compatible (DeepSeek, OpenRouter, Groq, Ollama...) : on traduit.
    if AI_PROVIDER == "openai":
        try:
            return await _openai_proxy(request, body, session)
        except Exception as e:
            log.warning("proxy /ai (openai): %s", e)
            return cors(web.json_response({"error": {"message": "Relais IA indisponible : %s" % e}}, status=502), request)
    # ---- Anthropic ----
    # Le client ne choisit pas librement le modele ni la taille de sortie.
    model = body.get("model") if body.get("model") in AI_MODEL_ALLOW else AI_MODEL
    body["model"] = model
    try:
        body["max_tokens"] = min(int(body.get("max_tokens") or AI_MAX_TOKENS), AI_MAX_TOKENS)
    except Exception:
        body["max_tokens"] = AI_MAX_TOKENS
    if model in ("claude-opus-5", "claude-fable-5-1"):
        body.setdefault("fallbacks", "default")
    want_stream = bool(body.get("stream"))
    session = request.app["session"]
    try:
        if want_stream:
            up = await session.post(ANTHROPIC_URL, json=body, headers=_ai_headers(),
                                    timeout=aiohttp.ClientTimeout(total=180))
            resp = web.StreamResponse(status=up.status)
            resp.headers["Content-Type"] = up.headers.get("Content-Type", "text/event-stream")
            cors(resp, request)
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            up.release()
            return resp
        async with session.post(ANTHROPIC_URL, json=body, headers=_ai_headers(),
                                timeout=aiohttp.ClientTimeout(total=180)) as up:
            text = await up.text()
            status = up.status
        return cors(web.Response(text=text, status=status, content_type="application/json"), request)
    except Exception as e:
        log.warning("proxy /ai: %s", e)
        return cors(web.json_response({"error": {"message": "Relais IA indisponible : %s" % e}}, status=502), request)


async def h_agents(request):
    """Lance un agent (ou les 4 + synthese) et renvoie des recommandations structurees."""
    denied = guard(request)
    if denied is not None:
        return denied
    if not ai_enabled():
        return cors(web.json_response({"ok": False, "error": "IA non configuree (definir la cle du fournisseur : ANTHROPIC_API_KEY, ou AI_PROVIDER=openai + AI_API_KEY)."},
                                      status=503), request)
    body = {}
    if request.method == "POST":
        body = await _read_json(request, 20000) or {}
    which = (body.get("agent") or request.query.get("agent") or "all").lower()
    question = (str(body.get("question") or "")[:500]).strip()
    session = request.app["session"]
    try:
        await refresh_bitget_state(session)     # totaux frais avant analyse
    except Exception:
        pass
    try:
        if which in ("all", "brief", "tout"):
            out = await run_all_agents(session, question)
        elif which in ("crypto", "division", "crypto-division"):
            out = await run_crypto_division(session, question)         # la division crypto complète
        elif which in ("bitget", "integrite", "exactitude", "verif"):
            # Contrôle d'exactitude seul : findings déterministes + agent Intégrité.
            snap, rec = await crypto_snapshot(session)
            ag = await run_agent(session, "integrite", question, snap, registry=CRYPTO_AGENTS)
            out = {"ok": True, "agents": [ag], "synthese": None,
                   "reconcile": rec.get("findings"), "reconcile_ok": rec.get("ok")}
        elif which in CRYPTO_AGENTS:
            snap, _ = await crypto_snapshot(session)
            out = {"ok": True, "agents": [await run_agent(session, which, question, snap, registry=CRYPTO_AGENTS)],
                   "synthese": None}
        elif which in AGENTS:
            out = {"ok": True, "agents": [await run_agent(session, which, question)], "synthese": None}
        else:
            return cors(web.json_response({"ok": False, "error": "agent inconnu"}, status=400), request)
    except Exception as e:
        log.warning("/agents: %s", e)
        return cors(web.json_response({"ok": False, "error": str(e)}, status=502), request)
    return cors(web.json_response(out), request)


def build_pdf_report(days=30):
    """Genere un rapport patrimoine PDF stylise avec graphiques (octets).

    SYNCHRONE ET LENT (fpdf trace tout en Python) : ne JAMAIS l'appeler directement
    depuis un handler async — passer par build_pdf_report_async(), qui l'execute dans
    un thread. Appele dans la boucle, il figeait tout le bot (heartbeat Discord
    compris) le temps de la generation, a chaque clic sur un bouton PDF."""
    from fpdf import FPDF
    GOLD, GREEN, RED = (201, 162, 39), (22, 163, 74), (220, 38, 38)
    PURPLE, AMBER, DARK, GREY, LIGHT = (124, 58, 237), (245, 158, 11), (24, 24, 34), (120, 120, 140), (244, 244, 248)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    W, M = 210, 12
    CW = W - 2 * M
    c = consolidated_total()
    # Copie figee : la generation tourne dans un thread, la boucle asyncio peut
    # ajouter des operations pendant ce temps (une liste modifiee en cours
    # d'iteration leve RuntimeError et fait echouer le rapport).
    snap = list(STATE.get("momo") or [])
    inc, exp, net, cnt = momo_totals_period(days, items=snap)
    mtn = momo_totals_period(days, "mtn", items=snap)
    moov = momo_totals_period(days, "moov", items=snap)

    def txt(x, y, s, size=9, style="", color=DARK):
        pdf.set_xy(x, y); pdf.set_font("Helvetica", style, size); pdf.set_text_color(*color)
        pdf.cell(0, 5, _ascii(s))

    # ---- Bandeau titre ----
    pdf.set_fill_color(*GOLD); pdf.rect(0, 0, W, 26, "F")
    txt(M, 6, "NEXUS  -  Rapport patrimoine", 20, "B", (255, 255, 255))
    txt(M, 17, "Fenetre %d jours  -  genere automatiquement" % days, 9, "", (255, 255, 255))
    # ---- Total consolide ----
    txt(M, 31, "PATRIMOINE TOTAL CONSOLIDE", 9, "", GREY)
    txt(M, 37, fmt_xof(c["total"]), 24, "B", DARK)
    # ---- Cartes KPI ----
    y, cw, ch = 53, (CW - 12) / 4, 22
    cards = [("Mobile Money", fmt_xof(c["momo"]), GREEN), ("NSIA", fmt_xof(c["nsia"]), PURPLE),
             ("Bitget", fmt_usd(c["bitget_usd"]), AMBER), ("Bitget en FCFA", fmt_xof(c["bitget_xof"]), GREY)]
    x = M
    for lbl, val, col in cards:
        pdf.set_fill_color(*LIGHT); pdf.rect(x, y, cw, ch, "F")
        pdf.set_fill_color(*col); pdf.rect(x, y, cw, 3, "F")
        txt(x + 3, y + 6, lbl, 7.5, "", GREY)
        txt(x + 3, y + 12, val, 11, "B", DARK)
        x += cw + 4
    # ---- Flux MoMo (barres) ----
    y = 82
    txt(M, y, "Flux Mobile Money (%d jours)" % days, 12, "B", DARK); y += 9
    mxv = max(inc, exp, 1); barmax = CW - 64
    for lbl, val, col in [("Entrees", inc, GREEN), ("Sorties", exp, RED)]:
        txt(M, y, lbl, 9, "", DARK)
        bw = max(barmax * (val / mxv), 0.6)
        pdf.set_fill_color(*col); pdf.rect(M + 24, y + 0.5, bw, 5, "F")
        txt(M + 26 + bw, y, fmt_xof(val), 8, "", GREY)
        y += 8
    txt(M, y, "Net : %s   (%d operations)" % (fmt_xof(net), cnt), 9, "B", GREEN if net >= 0 else RED); y += 12
    # ---- Repartition (barre empilee) ----
    txt(M, y, "Repartition du patrimoine", 12, "B", DARK); y += 9
    parts = [("Mobile Money", max(c["momo"], 0), GREEN), ("NSIA", max(c["nsia"], 0), PURPLE),
             ("Bitget", max(c["bitget_xof"], 0), AMBER)]
    tot = sum(p[1] for p in parts) or 1
    x = M
    for lbl, val, col in parts:
        w = CW * (val / tot)
        if w > 0.3:
            pdf.set_fill_color(*col); pdf.rect(x, y, w, 7, "F"); x += w
    y += 11
    for lbl, val, col in parts:
        pdf.set_fill_color(*col); pdf.rect(M, y + 0.5, 4, 4, "F")
        txt(M + 6, y, "%s : %s  (%.1f%%)" % (lbl, fmt_xof(val), val / tot * 100), 8.5, "", DARK)
        y += 6
    pdf.set_y(y + 2)
    # ---- NSIA ----
    ns = STATE.get("nsia")
    if ns and ns.get("total"):
        pdf.set_x(M); pdf.set_font("Helvetica", "B", 12); pdf.set_text_color(*PURPLE); pdf.cell(0, 7, "NSIA (OPCVM)", ln=1)
        line = "Valorisation %s" % fmt_xof(ns["total"])
        if ns.get("pv_latente") is not None: line += "   |   PV latente %s" % fmt_xof(ns["pv_latente"])
        if ns.get("invested"): line += "   |   Investi %s" % fmt_xof(ns["invested"])
        if ns.get("pv_realisee"): line += "   |   PV realisee %s" % fmt_xof(ns["pv_realisee"])
        pdf.set_x(M); pdf.set_font("Helvetica", "", 9.5); pdf.set_text_color(*DARK); pdf.cell(0, 5, _ascii(line), ln=1)
        pdf.set_font("Helvetica", "", 8); pdf.set_text_color(*GREY)
        for o in (ns.get("operations") or [])[-12:]:
            pdf.set_x(M); pdf.cell(0, 4.5, _ascii("  %s  %s  %s%s" % (o.get("date", ""), o.get("label", ""),
                fmt_xof(o.get("montant", 0)), ("  (pv " + fmt_xof(o["pv"]) + ")") if o.get("pv") else "")), ln=1)
    # ---- Bitget ----
    bg = STATE.get("bitget")
    if bg and bg.get("total"):
        pdf.ln(2); pdf.set_x(M); pdf.set_font("Helvetica", "B", 12); pdf.set_text_color(*AMBER); pdf.cell(0, 7, "Bitget", ln=1)
        pdf.set_x(M); pdf.set_font("Helvetica", "", 9.5); pdf.set_text_color(*DARK)
        pdf.cell(0, 5, _ascii("Total %s   Spot %s   Earn %s" % (fmt_usd(bg["total"]), fmt_usd(bg.get("spot", 0)), fmt_usd(bg.get("earn", 0)))), ln=1)
        pdf.set_font("Helvetica", "", 8); pdf.set_text_color(*GREY)
        for h in (bg.get("holdings") or [])[:10]:
            if h.get("val", 0) > 0.01:
                pdf.set_x(M); pdf.cell(0, 4.5, _ascii("  %s : %s" % (h.get("coin", ""), fmt_usd(h.get("val", 0)))), ln=1)
    # ---- Dernieres operations ---- (sur la copie figee, cf. plus haut)
    momo = snap
    if momo:
        pdf.ln(2); pdf.set_x(M); pdf.set_font("Helvetica", "B", 12); pdf.set_text_color(*DARK)
        pdf.cell(0, 7, "Dernieres operations Mobile Money", ln=1)
        pdf.set_font("Helvetica", "", 7.5)
        for mop in momo[-40:][::-1]:
            inc_ = mop.get("type") == "inc"
            pdf.set_text_color(*(GREEN if inc_ else RED)); pdf.set_x(M)
            pdf.cell(0, 4.3, _ascii("%s %s  [%s]  %s" % ("+" if inc_ else "-", fmt_xof(mop.get("amount", 0)),
                (mop.get("net", "mtn") or "").upper(), (mop.get("payee") or mop.get("text") or "")[:55])), ln=1)
    return bytes(pdf.output())

# Un seul PDF a la fois : trois clics d'affilee lancaient trois generations en
# parallele sur une instance a 512 Mo, chacune avec sa copie de l'etat.
_pdf_lock = asyncio.Semaphore(1)

async def build_pdf_report_async(days=30):
    """Genere le PDF HORS de la boucle asyncio (asyncio.to_thread).
    C'est le seul point d'entree a utiliser depuis un bouton ou une commande."""
    async with _pdf_lock:
        t0 = time.time()
        data = await asyncio.to_thread(build_pdf_report, days)
        log.info("PDF %d j genere en %.1fs (%d Ko)", days, time.time() - t0, len(data) // 1024)
        return data

def clean_duplicates():
    momo = STATE.get("momo") or []
    seen, kept = set(), []
    for e in momo:
        k = _dedup_key(e)
        if k in seen:
            continue
        seen.add(k)
        kept.append(e)
    removed = len(momo) - len(kept)
    if removed:
        STATE["momo"] = kept
        save_state()
    return removed

# ----------------------- Embeds & panneau (dependent de discord) -----------------------
if discord is not None:
    GOLD, GREEN, PURPLE, AMBER, SLATE = 0xC9A227, 0x16A34A, 0x7C3AED, 0xF59E0B, 0x334155

    async def build_report_embed():
        e = discord.Embed(title="📊 NEXUS — Rapport patrimoine", color=GOLD,
                          description="Vue consolidée de ta holding.")
        inc, exp, net, cnt = _momo_totals()
        e.add_field(name="💸 Mobile Money",
                    value="Entrées : **%s**\nSorties : **%s**\nNet : **%s**\n%d opération(s)"
                          % (fmt_xof(inc), fmt_xof(exp), fmt_xof(net), cnt), inline=True)
        ns = STATE.get("nsia")
        if ns and ns.get("total"):
            e.add_field(name="🏛 NSIA (OPCVM)",
                        value="**%s**\nmaj <t:%d:R>" % (fmt_xof(ns["total"]), int(ns.get("ts", 0) / 1000)),
                        inline=True)
        bz = business_field()
        if bz:
            e.add_field(name="💼 Revenus & Dépenses (app)", value=bz, inline=True)
        try:
            ov = await bitget_overview(HTTP_SESSION)
        except Exception as ex:
            ov = None
            log.warning("rapport: bitget indisponible: %s", ex)
        if ov is not None:
            top = "\n".join("• %s : %s" % (c, fmt_usd(v)) for c, a, v in ov["holdings"][:4] if v > 0.01)
            e.add_field(name="📈 Bitget (tous comptes)",
                        value="**%s**\nSpot %s · Earn %s\n%s"
                              % (fmt_usd(ov["total"]), fmt_usd(ov["spot"]), fmt_usd(ov["earn"]), top or "—"),
                        inline=True)
            STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                               "others": ov["others"], "ts": int(time.time() * 1000),
                               "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
            save_state()
        if STATE.get("patrimoine"):
            e.add_field(name="🗂 Patrimoine (app)", value="Synchronisé depuis l'app ✅", inline=False)
        c = consolidated_total()
        ctot = c["total"] or 1.0
        repl = []
        for lbl, val in (("📱 MoMo", c["momo"]), ("🏛 NSIA", c["nsia"]), ("📈 Bitget", c["bitget_xof"])):
            if val > 0:
                p = val / ctot * 100
                repl.append("%s `%s` %.0f%%" % (lbl, _bar(p, 14), p))
        if repl:
            e.add_field(name="🥧 Répartition du patrimoine", value="\n".join(repl), inline=False)
        e.description = "💰 **Patrimoine total consolidé ≈ %s**\n*(MoMo + NSIA + Bitget)*" % fmt_xof(c["total"])
        e.set_footer(text="NEXUS • holding & finance")
        e.timestamp = discord.utils.utcnow()
        return e

    def build_momo_embed():
        e = discord.Embed(title="💸 Récap Mobile Money", color=GREEN)
        momo = STATE.get("momo") or []
        if not momo:
            e.description = "Aucune opération enregistrée pour l'instant."
            return e
        inc, exp, net, cnt = _momo_totals()
        e.description = ("Entrées **%s** · Sorties **%s** · Net **%s** · %d op."
                         % (fmt_xof(inc), fmt_xof(exp), fmt_xof(net), cnt))
        lines = []
        for m in momo[-12:][::-1]:
            sign = "➕" if m.get("type") == "inc" else "➖"
            who = m.get("payee") or ""
            lines.append("%s **%s** %s" % (sign, fmt_xof(m.get("amount", 0)), ("· " + who) if who else ""))
        e.add_field(name="Dernières opérations", value=("\n".join(lines))[:1024], inline=False)
        e.set_footer(text="NEXUS")
        return e

    def build_nsia_embed():
        e = discord.Embed(title="🏛 NSIA — Portefeuille OPCVM", color=PURPLE)
        ns = STATE.get("nsia")
        if not ns or not ns.get("total"):
            e.description = "Aucun relevé NSIA reçu. Dépose ton relevé (PDF/image) dans le salon d'import."
            return e
        e.add_field(name="Valorisation", value="**%s**" % fmt_xof(ns["total"]), inline=True)
        if ns.get("pv_latente") is not None:
            pvl = ns["pv_latente"]
            e.add_field(name="Plus-value latente",
                        value="%s **%s**" % ("🟢" if pvl >= 0 else "🔴", fmt_xof(pvl)), inline=True)
        if ns.get("invested"):
            e.add_field(name="Investi (net)", value=fmt_xof(ns["invested"]), inline=True)
        if ns.get("pv_realisee"):
            e.add_field(name="Plus-value réalisée", value=fmt_xof(ns["pv_realisee"]), inline=True)
        ops = ns.get("operations") or []
        if ops:
            lines = []
            for o in ops[-8:][::-1]:
                sign = "➕" if o.get("sens") == "inc" else "➖"
                extra = (" · +%s pv" % fmt_xof(o["pv"])) if o.get("pv") else ""
                lines.append("%s %s **%s**%s" % (sign, o.get("date", ""), fmt_xof(o.get("montant", 0)), extra))
            e.add_field(name="Évolution (dernières opérations)",
                        value=("\n".join(lines))[:1024], inline=False)
        e.add_field(name="Mise à jour", value="<t:%d:R>" % int(ns.get("ts", 0) / 1000), inline=False)
        return e

    async def build_bitget_embed():
        e = discord.Embed(title="📈 Bitget — compte complet", color=AMBER)
        if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
            e.description = "Clés Bitget non configurées sur le serveur."
            return e
        try:
            ov = await bitget_overview(HTTP_SESSION)
        except Exception as ex:
            e.description = "Erreur Bitget : %s" % ex
            return e
        if ov is None:
            e.description = "Impossible de joindre Bitget (vérifie les clés / la permission Lecture)."
            return e
        tot = ov["total"] or 1.0
        chg = ov.get("change") or {}
        wch = sum((v / tot) * chg.get(str(c).split(" ")[0].upper(), 0.0) for c, a, v in ov["holdings"])
        e.add_field(name="💰 Valeur totale (tous comptes)",
                    value="# %s\n%s sur 24h" % (fmt_usd(ov["total"]), _chg(wch)), inline=False)
        rep = []
        for lbl, val, ic in (("Spot", ov["spot"], "🔵"), ("Earn / Staking", ov["earn"], "🟢"), ("Autres", ov["others"], "🟡")):
            if val > 0.01:
                p = val / tot * 100
                rep.append("%s `%s` **%s** · %.0f%%" % (ic, _bar(p), fmt_usd(val), p))
        e.add_field(name="📊 Répartition par compte", value="\n".join(rep) or "—", inline=False)
        lines = []
        for c, a, v in ov["holdings"][:10]:
            if v <= 0.01:
                continue
            base = str(c).split(" ")[0].upper()
            p = v / tot * 100
            lines.append("`%s` **%s** %.0f%% · %s\n%s — %s"
                         % (_bar(p, 10), base, p, _chg(chg.get(base, 0.0)),
                            ("%.5f" % a).rstrip("0").rstrip("."), fmt_usd(v)))
        e.add_field(name="🪙 Positions par actif", value=("\n".join(lines))[:1024] or "Aucune position.", inline=False)
        e.set_footer(text="NEXUS • staking inclus si calé • en direct")
        e.timestamp = discord.utils.utcnow()
        return e

    async def build_analyse_embed():
        await refresh_bitget_state(HTTP_SESSION)
        bg = STATE.get("bitget") or {}
        bg_usd = float(bg.get("total", 0) or 0); bg_earn = float(bg.get("earn", 0) or 0)
        rate = get_usd_xof(); bg_xof = bg_usd * rate
        nsia = float((STATE.get("nsia") or {}).get("total", 0) or 0)
        momo = max(0.0, sum(momo_balance_by_net().values()))
        total = bg_xof + nsia + momo
        passif_an = bg_earn * rate * 0.04 + nsia * 0.10
        passif_mois = passif_an / 12.0
        seuil = 12000000.0
        chemin = (total / seuil * 100) if seuil else 0
        crypto_part = (bg_xof / total * 100) if total else 0
        e = discord.Embed(title="📈 Analyse — Auto-financement", color=GOLD,
                          description="Ou en es-tu, et quand ton patrimoine se financera-t-il seul ?")
        e.add_field(name="🏦 Patrimoine total",
                    value="**%s**\nCrypto %s (%.0f%%) · NSIA %s · Cash %s"
                          % (fmt_xof(total), fmt_xof(bg_xof), crypto_part, fmt_xof(nsia), fmt_xof(momo)), inline=False)
        e.add_field(name="🔋 Revenu passif estime", value="**%s / mois**\nEarn 4%%/an + OPCVM 10%%/an" % fmt_xof(passif_mois), inline=True)
        e.add_field(name="🎯 Seuil (100k/mois)", value="**12 000 000 FCFA**\n%s\n%s du chemin"
                    % (_bar(chemin), ("%.1f%%" % chemin)), inline=True)
        e.add_field(name="💡 Priorites",
                    value="1. Epargner plus (levier n.1)\n2. Reduire la concentration crypto\n3. Reinvestir 100%% des gains", inline=False)
        e.set_footer(text="Analyse pedagogique, pas un conseil personnalise. PDF complet disponible dans l'app.")
        return e

    def build_status_embed():
        up = int(time.time()) - START_TS
        h, rem = divmod(up, 3600)
        mi = rem // 60
        ok = lambda b: "✅" if b else "❌"
        e = discord.Embed(title="⚙️ État du serveur NEXUS", color=SLATE)
        e.add_field(name="Services", value=(
            "%s Proxy Bitget\n%s OCR (images)\n%s Salon d'import\n%s Panneau"
            % (ok(BITGET_KEY and BITGET_SECRET and BITGET_PASS), ok(OCR_API_KEY),
               ok(DISCORD_CHANNEL), ok(PANEL_CHANNEL))), inline=True)
        e.add_field(name="Données", value=(
            "MoMo : %d op.\nNSIA : %s\nPatrimoine : %s"
            % (len(STATE.get("momo") or []),
               "oui" if STATE.get("nsia") else "non",
               "oui" if STATE.get("patrimoine") else "non")), inline=True)
        e.add_field(name="Uptime", value="%dh %02dmin" % (h, mi), inline=True)
        if PUBLIC_URL:
            e.add_field(name="App", value=PUBLIC_URL + "/", inline=False)
        e.set_footer(text="NEXUS • serveur 24/7")
        return e

    def build_ai_embeds(data):
        """Resultat des agents -> embeds Discord : 1 plan de synthese + 1 par agent."""
        prio = {"haute": "🔴", "moyenne": "🟠", "basse": "🟢"}
        def rec_val(r, maxi=900):
            v = "%s\n*Impact estimé : %s*" % ((r.get("detail", "") or "")[:maxi], r.get("impact", "n/d"))
            act = _clean_action(r.get("action"))
            if act:
                v += "\n🔧 *Applicable : %s*" % act["label"]
            return v
        if not data or not data.get("ok"):
            return [discord.Embed(title="🧠 Optimisations IA",
                    description="IA indisponible : %s" % ((data or {}).get("error") or "erreur"),
                    color=0xE74C3C)]
        embeds = []
        syn = data.get("synthese") or {}
        head = discord.Embed(title="🧠 NEXUS — Plan d'optimisation",
                description=(syn.get("resume") or "Actions prioritaires, tous agents confondus :"),
                color=GOLD)
        # Contrôle d'exactitude des données (déterministe) en tête, si fourni (division crypto).
        rec = data.get("reconcile")
        if rec:
            ic = {"ok": "🟢", "info": "🔵", "attention": "🟠", "erreur": "🔴"}
            txt = "\n".join("%s %s" % (ic.get(f.get("niveau"), "•"), f.get("msg", "")) for f in rec[:6])
            head.add_field(name=("✅ Données Bitget fiables" if data.get("reconcile_ok") else "⚠️ Données Bitget à vérifier"),
                           value=(txt[:1020] or "RAS"), inline=False)
        for r in (syn.get("recommandations") or [])[:5]:
            head.add_field(name="%s %s" % (prio.get((r.get("priorite") or "").lower(), "•"), r.get("titre", "")),
                           value=rec_val(r), inline=False)
        head.set_footer(text="NEXUS IA • analyse non contractuelle • %s" % now_wat().strftime("%d/%m %H:%M"))
        embeds.append(head)
        for a in (data.get("agents") or []):
            if a.get("error"):
                continue
            sc = a.get("score")
            e = discord.Embed(
                title="%s %s%s" % (a.get("emoji", ""), a.get("name", ""),
                    ("  ·  score %s/100" % sc) if isinstance(sc, (int, float)) else ""),
                description=(a.get("resume") or "")[:600], color=0x5865F2)
            for r in (a.get("recommandations") or [])[:3]:
                e.add_field(name="%s %s" % (prio.get((r.get("priorite") or "").lower(), "•"), r.get("titre", "")),
                            value=rec_val(r, 500), inline=False)
            embeds.append(e)
        return embeds[:10]

    def build_actions_view(actions):
        """Vue de boutons « Appliquer » : approuve une action IA, que l'app exécutera
        à la prochaine synchro. Boutons non persistants (valables tant que le bot tourne)."""
        if not actions:
            return None
        view = discord.ui.View(timeout=None)
        for a in actions[:5]:
            btn = discord.ui.Button(label=("✅ " + a["label"])[:80],
                                    style=discord.ButtonStyle.success,
                                    custom_id="nexus:apply:" + a["id"])
            async def _cb(interaction, aid=a["id"], lbl=a["label"]):
                ok = False
                for x in (STATE.get("ai_actions") or []):
                    if x.get("id") == aid:
                        x["status"] = "approved"
                        ok = True
                        break
                save_state()
                await interaction.response.send_message(
                    ("✅ **%s** approuvé — sera appliqué dans l'app à la prochaine synchro." % lbl)
                    if ok else "Action introuvable (déjà traitée ?).", ephemeral=True)
            btn.callback = _cb
            view.add_item(btn)
        return view

    async def send_ai_brief(data, sender, ephemeral=False):
        """Facteur commun : enregistre les actions, envoie les embeds, attache les boutons
        « Appliquer » au 1er embed. `sender` = coroutine(embed, view) -> message."""
        acts = register_ai_actions(data) if data.get("ok") else []
        view = build_actions_view(acts)
        embeds = build_ai_embeds(data)
        for i, emb in enumerate(embeds):
            await sender(emb, view if i == 0 else None)

    async def build_recap_embed(days):
        e = discord.Embed(title="📊 Rapport patrimoine — %d jours" % days, color=GOLD)
        inc, exp, net, cnt = momo_totals_period(days)
        mtn, moov = momo_totals_period(days, "mtn"), momo_totals_period(days, "moov")
        e.add_field(name="💸 Mobile Money (%dj)" % days,
                    value="Entrées **%s**\nSorties **%s**\nNet **%s**\n%d opération(s)"
                          % (fmt_xof(inc), fmt_xof(exp), fmt_xof(net), cnt), inline=True)
        if moov[3] or mtn[3]:
            e.add_field(name="Par réseau",
                        value="📱 MTN : net %s (%d)\n🟠 Moov : net %s (%d)"
                              % (fmt_xof(mtn[2]), mtn[3], fmt_xof(moov[2]), moov[3]), inline=True)
        ns = STATE.get("nsia")
        if ns and ns.get("total"):
            extra = ("\nPV latente %s" % fmt_xof(ns["pv_latente"])) if ns.get("pv_latente") is not None else ""
            e.add_field(name="🏛 NSIA", value="**%s**%s" % (fmt_xof(ns["total"]), extra), inline=True)
        try:
            ov = await bitget_overview(HTTP_SESSION)
        except Exception:
            ov = None
        if ov:
            e.add_field(name="📈 Bitget", value="**%s**\nSpot %s · Earn %s"
                        % (fmt_usd(ov["total"]), fmt_usd(ov["spot"]), fmt_usd(ov["earn"])), inline=True)
            STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                               "others": ov["others"], "ts": int(time.time() * 1000),
                               "holdings": [{"coin": c2, "amt": a2, "val": v2} for c2, a2, v2 in ov["holdings"]]}
            save_state()
        c = consolidated_total()
        e.description = "💰 **Patrimoine total consolidé ≈ %s**" % fmt_xof(c["total"])
        if PUBLIC_URL:
            e.add_field(name="🌐 Ton app", value=PUBLIC_URL + "/", inline=False)
        e.set_footer(text="NEXUS • rapport automatique • fenêtre %d jours" % days)
        e.timestamp = discord.utils.utcnow()
        return e

    class ReportActionView(discord.ui.View):
        """Boutons ✅ Valider / ❌ Refuser attachés à chaque rapport posté."""
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Valider", emoji="✅", style=discord.ButtonStyle.success, custom_id="nexus:rep_ok")
        async def ok(self, interaction, button):
            for c in self.children:
                c.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
            try:
                await interaction.followup.send("✅ Rapport validé.", ephemeral=True)
            except Exception:
                pass

        @discord.ui.button(label="Refuser (ré-analyser)", emoji="❌", style=discord.ButtonStyle.danger, custom_id="nexus:rep_no")
        async def no(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                n_msg, _ = await rescan_import(interaction.client)
                await post_report(interaction.client, embed=await build_report_embed(), view=ReportActionView())
                await interaction.followup.send(
                    "❌ Refusé — j'ai re-scanné %d message(s) et posté un rapport à jour." % n_msg, ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur ré-analyse : %s" % ex, ephemeral=True)

    async def recap_scheduler(client):
        """Poste automatiquement des rapports glissants 7 / 14 / 30 jours à 20h (heure Bénin)."""
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                now = now_wat()
                if now.hour >= 20:
                    keys = STATE.setdefault("recap_keys", {})
                    today = now.date()
                    for days in (7, 14, 30):
                        k = "d%d" % days
                        lastd = None
                        if keys.get(k):
                            try:
                                lastd = datetime.date.fromisoformat(keys[k])
                            except Exception:
                                lastd = None
                        if lastd is None or (today - lastd).days >= days:
                            await post_report(client, embed=await build_recap_embed(days), view=ReportActionView())
                            keys[k] = today.isoformat()
                            save_state()
            except Exception as e:
                log.error("recap_scheduler: %s", e)
            await asyncio.sleep(3600)  # vérifie toutes les heures

    async def history_scheduler(client):
        """Toutes les heures : rafraîchit Bitget, enregistre le point du jour dans
        l'historique, et ALERTE dans le salon rapports si le patrimoine varie de
        plus de ALERT_PCT % par rapport à la veille."""
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await refresh_bitget_state(HTTP_SESSION)
                point, prev = record_snapshot()
                if prev and prev.get("total"):
                    var = (point["total"] - prev["total"]) / prev["total"] * 100.0
                    flags = STATE.setdefault("alert_flags", {})
                    key = "alert_" + point["d"]
                    if abs(var) >= ALERT_PCT and not flags.get(key):
                        for k in list(flags):           # purge les vieux drapeaux
                            if k != key:
                                flags.pop(k, None)
                        flags[key] = True
                        save_state()
                        e = discord.Embed(
                            title="🚨 Alerte patrimoine : %+.1f%% en 24h" % var,
                            description="**%s** → **%s**\n(seuil : ±%.0f%% — réglable via ALERT_PCT)"
                                        % (fmt_xof(prev["total"]), fmt_xof(point["total"]), ALERT_PCT),
                            colour=0x2ECC71 if var >= 0 else 0xE74C3C)
                        await post_report(client, embed=e)
                        log.info("alerte patrimoine envoyée (%+.1f%%)", var)
            except Exception as e:
                log.error("history_scheduler: %s", e)
            await asyncio.sleep(3600)

    async def keepalive_task():
        """Auto-ping du service toutes les 10 min pour limiter le spin-down Render
        (plan gratuit). Complément du ping externe cron-job.org (voir DEPLOY.md)."""
        if not PUBLIC_URL:
            return
        while True:
            try:
                async with HTTP_SESSION.get(PUBLIC_URL + "/ping",
                                            timeout=aiohttp.ClientTimeout(total=15)) as r:
                    await r.read()
            except Exception as e:
                log.warning("keepalive: %s", e)
            await asyncio.sleep(600)

    async def agents_scheduler(client):
        """Brief d'optimisations IA automatique : a AI_BRIEF_HOUR (heure Bénin), tous les
        AI_BRIEF_EVERY jours, poste le plan d'action des 4 agents dans le salon rapports.
        Désactivable via AI_BRIEF_AUTO=0. Ne fait rien si l'IA n'est pas configurée."""
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                if AI_BRIEF_AUTO and ai_enabled():
                    now = now_wat()
                    if now.hour >= AI_BRIEF_HOUR:
                        today = now.date()
                        lastd = None
                        if STATE.get("ai_brief_last"):
                            try:
                                lastd = datetime.date.fromisoformat(STATE["ai_brief_last"])
                            except Exception:
                                lastd = None
                        if lastd is None or (today - lastd).days >= max(1, AI_BRIEF_EVERY):
                            await refresh_bitget_state(HTTP_SESSION)
                            data = await run_all_agents(HTTP_SESSION)
                            if data.get("ok"):
                                async def _send(emb, view):
                                    await post_report(client, embed=emb, view=view)
                                await send_ai_brief(data, _send)
                                STATE["ai_brief_last"] = today.isoformat()
                                save_state()
                                log.info("brief IA automatique envoyé")
            except Exception as e:
                log.error("agents_scheduler: %s", e)
            await asyncio.sleep(3600)

    async def post_report(client, embed=None, content=None, view=None, file=None):
        """Envoie un message dans le salon des RETOURS (REPORT_CHANNEL), sinon le panneau."""
        cid = REPORT_CHANNEL or PANEL_CHANNEL
        if not cid or not str(cid).isdigit():
            return None
        try:
            ch = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
            kw = {}
            if content is not None: kw["content"] = content
            if embed is not None: kw["embed"] = embed
            if view is not None: kw["view"] = view
            if file is not None: kw["file"] = file
            return await ch.send(**kw)
        except Exception as e:
            log.error("envoi dans le salon de rapports impossible: %s", e)
            return None

    async def purge_channels(client):
        """RESET : supprime les rapports postés par le bot + les PDF/captures d'import."""
        deleted = 0
        # Salon de rapports : messages du bot
        for cid in (REPORT_CHANNEL, PANEL_CHANNEL):
            if cid and str(cid).isdigit():
                try:
                    ch = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
                    async for msg in ch.history(limit=200):
                        if msg.author.id == client.user.id and (not STATE.get("panel_msg") or msg.id != STATE.get("panel_msg")):
                            try:
                                await msg.delete(); deleted += 1
                            except Exception:
                                pass
                except Exception as e:
                    sys.stderr.write("[purge] report: %s\n" % e)
        # Salon d'import : messages contenant des pièces jointes (tes PDF/captures)
        if DISCORD_CHANNEL and str(DISCORD_CHANNEL).isdigit():
            try:
                ch = client.get_channel(int(DISCORD_CHANNEL)) or await client.fetch_channel(int(DISCORD_CHANNEL))
                async for msg in ch.history(limit=200):
                    if msg.attachments:
                        try:
                            await msg.delete(); deleted += 1
                        except Exception:
                            pass
            except Exception as e:
                sys.stderr.write("[purge] import: %s\n" % e)
        return deleted

    def search_momo(query):
        """Recherche d'opérations MoMo par nom/texte ou montant."""
        q = (query or "").strip().lower()
        if not q:
            return []
        items = STATE.get("momo") or []
        num = re.sub(r"[^\d]", "", q)
        res = []
        for m in items:
            hay = ((m.get("payee") or "") + " " + (m.get("text") or "") + " " + (m.get("ref") or "")).lower()
            if q in hay or (num and num in str(int(m.get("amount", 0) or 0))):
                res.append(m)
        return res[-25:]

    async def rescan_import(client):
        """Re-scanne le salon d'import et ré-analyse les pièces jointes (dedup → aucun doublon)."""
        n_msg, summaries = 0, []
        if not (DISCORD_CHANNEL and str(DISCORD_CHANNEL).isdigit()):
            return n_msg, summaries
        try:
            ch = client.get_channel(int(DISCORD_CHANNEL)) or await client.fetch_channel(int(DISCORD_CHANNEL))
            async for msg in ch.history(limit=80):
                if msg.author.bot or not msg.attachments:
                    continue
                n_msg += 1
                for att in msg.attachments:
                    try:
                        s = await process_attachment(msg, att)
                        if s:
                            summaries.append(s)
                    except Exception as ex:
                        log.warning("rescan piece jointe: %s", ex)
        except Exception as e:
            log.error("rescan du salon d'import: %s", e)
        return n_msg, summaries

    class ConfirmResetView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="⚠️ Oui, tout effacer", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction, button):
            reset_state()
            await interaction.response.edit_message(
                content="🧨 Données remises à zéro. Suppression des rapports et PDF en cours…", view=None)
            try:
                n = await purge_channels(interaction.client)
            except Exception:
                n = 0
            try:
                await interaction.followup.send(
                    "🧨 Tout effacé : données à zéro + **%d** message(s)/PDF supprimé(s) des salons." % n, ephemeral=True)
            except Exception:
                pass

        @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction, button):
            await interaction.response.edit_message(content="Annulé — rien n'a été effacé.", view=None)

    class SearchModal(discord.ui.Modal, title="🔎 Recherche de dépense"):
        q = discord.ui.TextInput(label="Nom, libellé ou montant",
                                 placeholder="ex : SBEE, MARIE, 30000", required=True, max_length=60)

        async def on_submit(self, interaction):
            res = search_momo(str(self.q))
            if not res:
                await interaction.response.send_message("Aucun résultat pour « %s »." % self.q, ephemeral=True)
                return
            lines = []
            for m in res[::-1]:
                sign = "➕" if m.get("type") == "inc" else "➖"
                lines.append("%s **%s** [%s] · %s" % (sign, fmt_xof(m.get("amount", 0)),
                             (m.get("net", "mtn") or "").upper(), (m.get("payee") or m.get("text") or "")[:40]))
            e = discord.Embed(title="🔎 Résultats : %s" % self.q, color=GREEN,
                              description=("\n".join(lines))[:3900])
            e.set_footer(text="%d résultat(s)" % len(res))
            await interaction.response.send_message(embed=e, ephemeral=True)

    def build_history_embed(d):
        """Embed d'évolution du patrimoine sur d jours (graphe + postes + projection).
        Retourne None s'il n'y a pas encore assez de points quotidiens."""
        hist = STATE.get("history") or []
        cutoff = (today_wat() - datetime.timedelta(days=d)).isoformat()
        pts = [p for p in hist if p.get("d", "") >= cutoff]
        if len(pts) < 2:
            return None
        first, last = pts[0], pts[-1]
        delta = int(last["total"] - first["total"])
        var = (delta / first["total"] * 100.0) if first["total"] else 0.0
        span = max(1, (datetime.date.fromisoformat(last["d"])
                       - datetime.date.fromisoformat(first["d"])).days)
        rythme_mois = delta / span * 30.0
        e = discord.Embed(title="📈 Évolution du patrimoine — %d derniers jours" % d,
                          colour=0x2ECC71 if delta >= 0 else 0xE74C3C)
        e.add_field(name="Du %s au %s" % (first["d"], last["d"]),
                    value="**%s** → **%s**\nVariation : **%s** (%+.1f%%)"
                          % (fmt_xof(first["total"]), fmt_xof(last["total"]),
                             ("+" if delta >= 0 else "−") + fmt_xof(abs(delta)), var),
                    inline=False)
        e.add_field(name="Graphe (total)",
                    value="`%s`" % sparkline([p["total"] for p in pts]), inline=False)
        det = []
        for key, lab in (("momo", "📱 MoMo"), ("nsia", "🏛 NSIA"), ("bg", "📈 Bitget")):
            dd = int(last.get(key, 0) - first.get(key, 0))
            det.append("%s : %s" % (lab, ("+" if dd >= 0 else "−") + fmt_xof(abs(dd))))
        e.add_field(name="Par poste", value="\n".join(det), inline=False)
        SEUIL = 12_000_000  # patrimoine cible (100k/mois passif — cf. /analyse)
        if last["total"] >= SEUIL:
            e.add_field(name="🎯 Objectif 12M", value="Seuil déjà atteint 🎉", inline=False)
        elif rythme_mois > 0:
            eta = datetime.date.fromisoformat(last["d"]) + datetime.timedelta(
                days=int((SEUIL - last["total"]) / rythme_mois * 30))
            e.add_field(name="🎯 Projection auto-financement (12M)",
                        value="Rythme actuel : **+%s / mois** → seuil atteint vers **%s**"
                              % (fmt_xof(rythme_mois), eta.strftime("%m/%Y")), inline=False)
        else:
            e.add_field(name="🎯 Projection",
                        value="Rythme actuel : **−%s / mois** (négatif sur la fenêtre)"
                              % fmt_xof(abs(rythme_mois)), inline=False)
        return e

    class PanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            if PUBLIC_URL:
                self.add_item(discord.ui.Button(label="Ouvrir l'app", emoji="🌐",
                              url="%s/?token=%s" % (PUBLIC_URL, AUTH_TOKEN), row=1))

        @discord.ui.button(label="Historique", emoji="📉",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:histo", row=3)
        async def b_histo(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await refresh_bitget_state(HTTP_SESSION)
                record_snapshot()
                emb = build_history_embed(30)
                if emb is None:
                    await interaction.followup.send(
                        "Pas encore assez d'historique — le bot enregistre 1 point par jour, "
                        "reviens demain 😉", ephemeral=True)
                else:
                    await interaction.followup.send(embed=emb, ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur historique : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Rapport complet", emoji="📊",
                           style=discord.ButtonStyle.primary, custom_id="nexus:report", row=0)
        async def b_report(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                emb = await build_report_embed()
                posted = await post_report(interaction.client, embed=emb, view=ReportActionView())
                if posted is None:
                    await interaction.followup.send(embed=emb, ephemeral=True)
                else:
                    await interaction.followup.send("📊 Rapport complet posté dans le salon de rapports.", ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur rapport : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Récap MoMo", emoji="💸",
                           style=discord.ButtonStyle.success, custom_id="nexus:momo", row=0)
        async def b_momo(self, interaction, button):
            await interaction.response.send_message(embed=build_momo_embed(), ephemeral=True)

        @discord.ui.button(label="NSIA", emoji="🏛",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:nsia", row=0)
        async def b_nsia(self, interaction, button):
            await interaction.response.send_message(embed=build_nsia_embed(), ephemeral=True)

        @discord.ui.button(label="Bitget", emoji="📈",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:bitget", row=0)
        async def b_bitget(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await interaction.followup.send(embed=await build_bitget_embed(), ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur Bitget : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Synchroniser", emoji="🔄",
                           style=discord.ButtonStyle.primary, custom_id="nexus:sync", row=1)
        async def b_sync(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                ov = await bitget_overview(HTTP_SESSION)
                if ov is not None:
                    STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                                       "others": ov["others"], "ts": int(time.time() * 1000),
                                       "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
                    save_state()
                    await interaction.followup.send(
                        "🔄 Synchronisé. Bitget total : **%s** (Spot %s · Earn %s)."
                        % (fmt_usd(ov["total"]), fmt_usd(ov["spot"]), fmt_usd(ov["earn"])), ephemeral=True)
                else:
                    await interaction.followup.send("Synchro : Bitget indisponible (clés ?).", ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur synchro : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Nettoyer doublons", emoji="🧹",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:clean", row=1)
        async def b_clean(self, interaction, button):
            removed = clean_duplicates()
            await interaction.response.send_message(
                ("🧹 %d doublon(s) supprimé(s)." % removed) if removed else "Aucun doublon trouvé ✅",
                ephemeral=True)

        @discord.ui.button(label="État serveur", emoji="⚙️",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:status", row=1)
        async def b_status(self, interaction, button):
            await interaction.response.send_message(embed=build_status_embed(), ephemeral=True)

        @discord.ui.button(label="Optimisations IA", emoji="🧠",
                           style=discord.ButtonStyle.primary, custom_id="nexus:optim", row=4)
        async def b_optim(self, interaction, button):
            if not ai_enabled():
                await interaction.response.send_message(
                    "🧠 IA non configurée : définis **ANTHROPIC_API_KEY**, ou **AI_PROVIDER=openai** + **AI_API_KEY** (DeepSeek/OpenRouter...).", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await refresh_bitget_state(HTTP_SESSION)
                data = await run_all_agents(HTTP_SESSION)
            except Exception as e:
                await interaction.followup.send("Erreur IA : %s" % e, ephemeral=True)
                return
            async def _send(emb, view):
                await interaction.followup.send(embed=emb, ephemeral=True, **({"view": view} if view else {}))
            await send_ai_brief(data, _send)

        @discord.ui.button(label="Redémarrer (re-scan)", emoji="♻️",
                           style=discord.ButtonStyle.primary, custom_id="nexus:rescan", row=2)
        async def b_rescan(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                n_msg, _ = await rescan_import(interaction.client)
                try:
                    ov = await bitget_overview(HTTP_SESSION)
                    if ov:
                        STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                                           "others": ov["others"], "ts": int(time.time() * 1000),
                                           "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
                        save_state()
                except Exception:
                    pass
                inc, exp, net, cnt = _momo_totals()
                await interaction.followup.send(
                    "♻️ Re-scan terminé : %d message(s) relu(s). MoMo : %d opération(s) (net %s). Synchro relancée."
                    % (n_msg, cnt, fmt_xof(net)), ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur re-scan : %s" % ex, ephemeral=True)

        @discord.ui.button(label="RESET (tout à 0)", emoji="🧨",
                           style=discord.ButtonStyle.danger, custom_id="nexus:reset", row=2)
        async def b_reset(self, interaction, button):
            await interaction.response.send_message(
                "⚠️ Confirmer la **remise à zéro de toutes tes données** (MoMo, NSIA, Bitget) ? Action irréversible.",
                view=ConfirmResetView(), ephemeral=True)

        @discord.ui.button(label="Rapport 7j", emoji="📅",
                           style=discord.ButtonStyle.success, custom_id="nexus:recap_7", row=3)
        async def b_recap_7(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await post_report(interaction.client, embed=await build_recap_embed(7), view=ReportActionView())
            await interaction.followup.send("📅 Rapport 7 jours posté dans le salon de rapports.", ephemeral=True)

        @discord.ui.button(label="Rapport 14j", emoji="🗓️",
                           style=discord.ButtonStyle.success, custom_id="nexus:recap_14", row=3)
        async def b_recap_14(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await post_report(interaction.client, embed=await build_recap_embed(14), view=ReportActionView())
            await interaction.followup.send("🗓️ Rapport 14 jours posté dans le salon de rapports.", ephemeral=True)

        @discord.ui.button(label="Rapport 30j", emoji="📆",
                           style=discord.ButtonStyle.success, custom_id="nexus:recap_30", row=3)
        async def b_recap_30(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await post_report(interaction.client, embed=await build_recap_embed(30), view=ReportActionView())
            await interaction.followup.send("📆 Rapport 30 jours posté dans le salon de rapports.", ephemeral=True)

        @discord.ui.button(label="Rapport PDF", emoji="📄",
                           style=discord.ButtonStyle.primary, custom_id="nexus:pdf", row=4)
        async def b_pdf(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await refresh_bitget_state(HTTP_SESSION)
                data = await build_pdf_report_async(30)
                f = discord.File(io.BytesIO(data), filename="rapport_nexus_30j.pdf")
                await post_report(interaction.client, content="📄 **Rapport patrimoine détaillé (30 jours)**",
                                  file=f, view=ReportActionView())
                await interaction.followup.send("📄 Rapport PDF posté dans le salon de rapports.", ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur PDF : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Recherche dépense", emoji="🔎",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:search", row=4)
        async def b_search(self, interaction, button):
            await interaction.response.send_modal(SearchModal())

        @discord.ui.button(label="Lien du site", emoji="🔗",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:link", row=2)
        async def b_link(self, interaction, button):
            url = (PUBLIC_URL or "") + "/" if PUBLIC_URL else "(PUBLIC_URL non configuré)"
            await interaction.response.send_message(
                "🔗 **Ton tableau de bord** (clique ou copie) :\n%s" % url, ephemeral=True)

        @discord.ui.button(label="Patrimoine total", emoji="🏦",
                           style=discord.ButtonStyle.primary, custom_id="nexus:patri", row=0)
        async def b_patri(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await refresh_bitget_state(HTTP_SESSION)   # Bitget frais (live + staking) avant le total
            c = consolidated_total()
            e = discord.Embed(title="🏦 Patrimoine total consolidé", color=GOLD,
                              description="# %s" % fmt_xof(c["total"]))
            e.add_field(name="📱 Mobile Money", value=fmt_xof(c["momo"]), inline=True)
            e.add_field(name="🏛 NSIA", value=fmt_xof(c["nsia"]), inline=True)
            e.add_field(name="📈 Bitget", value="%s\n(%s)" % (fmt_usd(c["bitget_usd"]), fmt_xof(c["bitget_xof"])), inline=True)
            await interaction.followup.send(embed=e, ephemeral=True)

        @discord.ui.button(label="Tout actualiser", emoji="✨",
                           style=discord.ButtonStyle.primary, custom_id="nexus:refreshall", row=1)
        async def b_refreshall(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                n_msg, _ = await rescan_import(interaction.client)
                try:
                    ov = await bitget_overview(HTTP_SESSION)
                    if ov:
                        STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                                           "others": ov["others"], "ts": int(time.time() * 1000),
                                           "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
                        save_state()
                except Exception:
                    pass
                c = consolidated_total()
                await interaction.followup.send(
                    "✨ Tout actualisé : MoMo relu (%d msg), Bitget %s. **Patrimoine total : %s**."
                    % (n_msg, fmt_usd(c["bitget_usd"]), fmt_xof(c["total"])), ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur actualisation : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Solde MoMo", emoji="💱",
                           style=discord.ButtonStyle.success, custom_id="nexus:momobal", row=2)
        async def b_momobal(self, interaction, button):
            bynet = momo_balance_by_net()
            lines = "\n".join("• **%s** : %s" % (("Moov" if k == "moov" else "MTN"), fmt_xof(v))
                              for k, v in bynet.items()) or "Aucun solde enregistré."
            e = discord.Embed(title="💱 Soldes Mobile Money", description=lines, color=GREEN)
            await interaction.response.send_message(embed=e, ephemeral=True)

        @discord.ui.button(label="Rafraîchir panneau", emoji="🔁",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:repanel", row=2)
        async def b_repanel(self, interaction, button):
            await interaction.response.defer(ephemeral=True)
            STATE["panel_msg"] = None
            await post_or_update_panel(interaction.client)
            await interaction.followup.send("🔁 Panneau rafraîchi ✅", ephemeral=True)

        @discord.ui.button(label="Rapport 90j", emoji="📊",
                           style=discord.ButtonStyle.success, custom_id="nexus:recap_90", row=3)
        async def b_recap_90(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await post_report(interaction.client, embed=await build_recap_embed(90), view=ReportActionView())
            await interaction.followup.send("📊 Rapport 90 jours posté dans le salon de rapports.", ephemeral=True)

        @discord.ui.button(label="PDF 7j", emoji="🗒️",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:pdf7", row=4)
        async def b_pdf7(self, interaction, button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await refresh_bitget_state(HTTP_SESSION)
                data = await build_pdf_report_async(7)
                f = discord.File(io.BytesIO(data), filename="rapport_nexus_7j.pdf")
                await post_report(interaction.client, content="🗒️ **Rapport patrimoine (7 jours)**",
                                  file=f, view=ReportActionView())
                await interaction.followup.send("🗒️ PDF 7 jours posté dans le salon de rapports.", ephemeral=True)
            except Exception as ex:
                await interaction.followup.send("Erreur PDF : %s" % ex, ephemeral=True)

        @discord.ui.button(label="Aide", emoji="❓",
                           style=discord.ButtonStyle.secondary, custom_id="nexus:help", row=4)
        async def b_help(self, interaction, button):
            e = discord.Embed(title="❓ Aide — Panneau NEXUS", color=SLATE,
                description=("**Boutons**\n"
                            "🏦 Patrimoine · 📊 Rapport · 💸 MoMo · 🏛 NSIA · 📈 Bitget\n"
                            "✨ Tout actualiser · 🔄 Synchroniser · 🧹 Doublons · ⚙️ État\n"
                            "📅/🗓️/📆/📊 Rapports 7/14/30/90 j · 📄 PDF · 🗒️ PDF 7j\n"
                            "🔎 Recherche · 💱 Solde MoMo · 🔁 Rafraîchir · 🔗 Lien · ♻️ Re-scan\n\n"
                            "**Commandes** : `/panel` `/rapport` `/solde` `/bitget` `/aide`\n\n"
                            "📥 *Dépose un PDF / capture / relevé dans le salon d'import : "
                            "chaque dépense et retrait est noté automatiquement.*"))
            await interaction.response.send_message(embed=e, ephemeral=True)

    async def post_or_update_panel(client):
        if not PANEL_CHANNEL or not str(PANEL_CHANNEL).isdigit():
            return
        try:
            ch = client.get_channel(int(PANEL_CHANNEL)) or await client.fetch_channel(int(PANEL_CHANNEL))
        except Exception as e:
            log.error("panneau: salon introuvable: %s", e)
            return
        emb = discord.Embed(
            title="🛰️ NEXUS — Panneau de contrôle",
            description="Pilote toute ta **holding & finance** d'un clic. Voici ce que fait chaque bouton :",
            color=GOLD)
        # Résumé live : patrimoine consolidé + tendance vs hier + mini-graphe 14 j
        try:
            record_snapshot()
            c = consolidated_total()
            hist = STATE.get("history") or []
            trend = ""
            if len(hist) >= 2 and hist[-2].get("total"):
                dv = hist[-1]["total"] - hist[-2]["total"]
                trend = "  ·  %s%s vs hier" % ("+" if dv >= 0 else "−", fmt_xof(abs(dv)))
            spark = sparkline([p["total"] for p in hist[-14:]]) if len(hist) >= 2 else ""
            emb.add_field(name="💰 Patrimoine actuel",
                          value="**%s**%s%s" % (fmt_xof(c["total"]), trend,
                                                ("\n`%s` (14 j)" % spark) if spark else ""),
                          inline=False)
        except Exception as _e:
            log.warning("panel resume: %s", _e)
        emb.add_field(name="🏦 Voir mes soldes", value=(
            "🏦 **Patrimoine total** — tout consolidé (MoMo + NSIA + Bitget)\n"
            "📈 **Bitget** — détail spot / earn / staking\n"
            "💸 **Récap MoMo** · 🏛 **NSIA** · 💱 **Solde MoMo**"), inline=False)
        emb.add_field(name="📊 Rapports", value=(
            "📊 **Rapport complet** · 📅 **7 j** · 🗓️ **14 j** · 📆 **30 j** · 📊 **90 j**\n"
            "📄 **PDF 30 j** · 🗒️ **PDF 7 j** · 📉 **Historique** (graphe + projection 12M)"), inline=False)
        _ia = "OK" if (ANTHROPIC_API_KEY or ai_enabled()) else "à configurer"
        emb.add_field(name="🧠 Intelligence — agents IA (%s)" % _ia, value=(
            "🧠 **Optimisations IA** (bouton) ou `/optim` — plan d'action des **5 agents** "
            "(patrimoine · dépenses · épargne · investissement · objectifs) + synthèse.\n"
            "🪙 `/crypto` — **division crypto** : contrôle d'**exactitude des données Bitget** "
            "+ agents dédiés (allocation, DCA, performance, analyse par actif).\n"
            "_Analyse non contractuelle — les agents informent, ne passent aucun ordre._"), inline=False)
        emb.add_field(name="⚙️ Contrôle & maintenance", value=(
            "✨ **Tout actualiser** · 🔄 **Synchroniser** · 🔁 **Rafraîchir panneau** · 🧠 **Optimisations IA**\n"
            "🧹 **Nettoyer doublons** · ⚙️ **État serveur** · ♻️ **Re-scan** · 🧨 **RESET**\n"
            "🔎 **Recherche** · 🔗 **Lien** · 🌐 **Ouvrir l'app** · ❓ **Aide**"), inline=False)
        emb.add_field(name="📥 Automatique (zéro effort)", value=(
            "Dépose un **PDF / capture / relevé** dans le salon d'import → chaque "
            "**dépense, retrait, dépôt** est noté tout seul. Une capture d'accueil "
            "**MTN / Moov** met à jour ton **solde**."), inline=False)
        emb.add_field(name="⌨️ Commandes", value=(
            "`/solde` `/bitget` `/momo` `/nsia`\n"
            "`/rapport` `/recap [jours]` `/historique [jours]` `/pdf [jours]`\n"
            "`/analyse` `/optim` `/crypto` `/sync` `/etat` `/panel` `/aide`"), inline=False)
        now = now_wat()
        emb.set_footer(text="NEXUS • alertes auto ±%.0f%% • MAJ %s" % (ALERT_PCT, now.strftime("%d/%m %H:%M")))
        view = PanelView()
        msg_id = STATE.get("panel_msg")
        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))
                await msg.edit(embed=emb, view=view)
                return
            except Exception:
                pass
        try:
            msg = await ch.send(embed=emb, view=view)
            STATE["panel_msg"] = msg.id
            save_state()
            log.info("panneau publie dans le salon %s", PANEL_CHANNEL)
        except Exception as ex:
            log.error("panneau: envoi impossible: %s", ex)

    MAX_ATTACH = int(_conf("MAX_ATTACH_MB") or "12") * 1024 * 1024

    async def process_attachment(message, att):
        """Traite une piece jointe (PDF/image) -> texte de resume, et ajoute les reactions."""
        name = (att.filename or "").lower()
        # Un PDF de 200 Mo etait lu entierement en memoire puis passe a pdfplumber :
        # de quoi faire tomber l'instance Render (512 Mo) avec un seul fichier.
        if (att.size or 0) > MAX_ATTACH:
            await message.add_reaction("⚠️")
            return "⚠️ **%s** ignoré : %.1f Mo, au-dessus de la limite de %d Mo." % (
                att.filename, (att.size or 0) / 1048576.0, MAX_ATTACH // 1048576)
        data = await att.read()
        if name.endswith(".pdf"):
            obj, text = parse_pdf_bytes(data)
            if obj is not None:
                STATE["patrimoine"] = obj
                save_state()
                await message.add_reaction("✅")
                return "🗂 Patrimoine mis à jour depuis le PDF."
            # PDF scanné / image (ex: document WhatsApp) -> pas de texte extractible -> OCR de secours
            if (not text or len(text.strip()) < 40):
                if OCR_API_KEY:
                    ocr_txt = await ocr_image(HTTP_SESSION, data, att.filename, is_pdf=True)
                    if ocr_txt and len(ocr_txt.strip()) > len(text.strip()):
                        text = ocr_txt
                else:
                    await message.add_reaction("❓")
                    return "📄 PDF sans texte (scanné). Ajoute la variable **OCR_API_KEY** au serveur pour l'analyser automatiquement."
            nsia = parse_nsia(text)
            if nsia:
                set_nsia(nsia)
                await message.add_reaction("🏛")
                return "🏛 NSIA : total portefeuille **%s**." % fmt_xof(nsia["total"])
            n = add_momo(parse_momo_text(text))
            await message.add_reaction("📄")
            return ("📄 **%d** opération(s) détectée(s) dans le PDF." % n) if n else "📄 Aucune opération détectée dans le PDF (texte illisible)."
        if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")):
            if not OCR_API_KEY:
                await message.add_reaction("❓")
                return "OCR désactivé : ajoute la variable OCR_API_KEY au serveur."
            text = await ocr_image(HTTP_SESSION, data, att.filename)
            nsia = parse_nsia(text)
            if nsia:
                set_nsia(nsia)
                await message.add_reaction("🏛")
                return "🏛 NSIA : total portefeuille **%s**." % fmt_xof(nsia["total"])
            n = add_momo(parse_momo_text(text))
            if n:
                await message.add_reaction("🧾")
                return "🧾 **%d** opération(s) détectée(s) sur l'image." % n
            # Pas d'opération : capture d'accueil ? -> lire le SOLDE (MTN/Moov)
            bal = detect_balance(text)
            if bal:
                net, val = bal
                # Horodate : momo_balance_by_net() n'ajoute que les operations
                # posterieures a cette photo du compte.
                STATE.setdefault("balances", {})[net] = {"amount": val, "ts": int(time.time() * 1000)}
                save_state()
                await message.add_reaction("💰")
                return "💰 Solde **%s** détecté : **%s** (compte mis à jour)." % (("Moov" if net == "moov" else "MTN"), fmt_xof(val))
            await message.add_reaction("❓")
            return "🧾 Aucune opération ni solde détecté sur l'image."
        return None

async def run_discord(http_session):
    if not discord or not DISCORD_TOKEN or not DISCORD_CHANNEL or not str(DISCORD_CHANNEL).isdigit():
        print("[discord] desactive (token/ID de salon manquant ou invalide). L'API HTTP et le proxy Bitget restent actifs.")
        return
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)
    import_chan_id = int(DISCORD_CHANNEL)

    @tree.command(name="panel", description="(Re)publier le panneau de contrôle NEXUS")
    async def _cmd_panel(interaction):
        await interaction.response.defer(ephemeral=True)
        STATE["panel_msg"] = None  # force un nouveau message
        await post_or_update_panel(client)
        await interaction.followup.send("Panneau republié ✅", ephemeral=True)

    @tree.command(name="rapport", description="Afficher le rapport patrimoine complet")
    async def _cmd_report(interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=await build_report_embed(), ephemeral=True)

    @tree.command(name="analyse", description="Analyse auto-financement : ou en es-tu et quand ton patrimoine se finance seul")
    async def _cmd_analyse(interaction):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=await build_analyse_embed())

    @tree.command(name="optim", description="Optimisations IA : plan d'action sur tes finances (5 agents)")
    @discord.app_commands.describe(agent="Cibler un agent : patrimoine, depenses, epargne, invest, objectifs (vide = tous)")
    async def _cmd_optim(interaction, agent: str = ""):
        if not ai_enabled():
            await interaction.response.send_message(
                "🧠 IA non configurée : définis **ANTHROPIC_API_KEY**, ou **AI_PROVIDER=openai** + **AI_API_KEY** (DeepSeek/OpenRouter...).", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        key = (agent or "").strip().lower()
        try:
            await refresh_bitget_state(HTTP_SESSION)
            if key in AGENTS:
                data = {"ok": True, "agents": [await run_agent(HTTP_SESSION, key)], "synthese": None}
            else:
                data = await run_all_agents(HTTP_SESSION)
        except Exception as e:
            await interaction.followup.send("Erreur IA : %s" % e)
            return
        async def _send(emb, view):
            await interaction.followup.send(embed=emb, **({"view": view} if view else {}))
        await send_ai_brief(data, _send)

    @tree.command(name="crypto", description="Division crypto : contrôle d'exactitude Bitget + analyse dédiée (5 agents)")
    @discord.app_commands.describe(agent="Cibler : integrite, allocation, dca, performance, opportunites (vide = division complète)")
    async def _cmd_crypto(interaction, agent: str = ""):
        if not ai_enabled():
            await interaction.response.send_message(
                "🧠 IA non configurée : définis **ANTHROPIC_API_KEY**, ou **AI_PROVIDER=openai** + **AI_API_KEY**.", ephemeral=True)
            return
        if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
            await interaction.response.send_message("📈 Bitget non configuré (clés serveur).", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        key = (agent or "").strip().lower()
        try:
            if key in CRYPTO_AGENTS:
                snap, rec = await crypto_snapshot(HTTP_SESSION)
                data = {"ok": True, "agents": [await run_agent(HTTP_SESSION, key, "", snap, registry=CRYPTO_AGENTS)],
                        "synthese": None, "reconcile": rec.get("findings"), "reconcile_ok": rec.get("ok")}
            else:
                data = await run_crypto_division(HTTP_SESSION)
        except Exception as e:
            await interaction.followup.send("Erreur division crypto : %s" % e)
            return
        async def _send(emb, view):
            await interaction.followup.send(embed=emb, **({"view": view} if view else {}))
        await send_ai_brief(data, _send)

    @tree.command(name="solde", description="Patrimoine total consolidé (MoMo + NSIA + Bitget)")
    async def _cmd_solde(interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await refresh_bitget_state(HTTP_SESSION)   # Bitget frais avant le total
        c = consolidated_total()
        e = discord.Embed(title="🏦 Patrimoine total", color=GOLD, description="# %s" % fmt_xof(c["total"]))
        e.add_field(name="📱 MoMo", value=fmt_xof(c["momo"]), inline=True)
        e.add_field(name="🏛 NSIA", value=fmt_xof(c["nsia"]), inline=True)
        e.add_field(name="📈 Bitget", value=fmt_usd(c["bitget_usd"]), inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)

    @tree.command(name="bitget", description="Détail complet du compte Bitget")
    async def _cmd_bitget(interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.followup.send(embed=await build_bitget_embed(), ephemeral=True)
        except Exception as ex:
            await interaction.followup.send("Erreur Bitget : %s" % ex, ephemeral=True)

    @tree.command(name="momo", description="Récapitulatif Mobile Money (dépenses, retraits, dépôts)")
    async def _cmd_momo(interaction):
        await interaction.response.send_message(embed=build_momo_embed(), ephemeral=True)

    @tree.command(name="nsia", description="Portefeuille NSIA (OPCVM) — montant pour corriger le total à la main")
    @discord.app_commands.describe(montant="(Optionnel) Corriger le total NSIA en FCFA, ex: 217986.13")
    async def _cmd_nsia(interaction, montant: float = 0.0):
        if montant and montant > 0:
            ns = STATE.get("nsia") or {"source": "nsia", "cur": "XOF", "operations": []}
            ns = dict(ns)
            ns["total"] = float(montant)
            ns["ts"] = int(time.time() * 1000)
            ns["manual"] = True
            STATE["nsia"] = ns
            save_state()
        await interaction.response.send_message(embed=build_nsia_embed(), ephemeral=True)

    @tree.command(name="etat", description="État du serveur NEXUS (uptime, Discord, données)")
    async def _cmd_etat(interaction):
        await interaction.response.send_message(embed=build_status_embed(), ephemeral=True)

    @tree.command(name="sync", description="Synchroniser Bitget maintenant")
    async def _cmd_sync(interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ov = await bitget_overview(HTTP_SESSION)
            if ov:
                STATE["bitget"] = {"total": ov["total"], "spot": ov["spot"], "earn": ov["earn"],
                                   "others": ov["others"], "ts": int(time.time() * 1000),
                                   "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in ov["holdings"]]}
                save_state()
                await interaction.followup.send("🔄 Bitget synchronisé : **%s** (Spot %s · Earn %s)."
                    % (fmt_usd(ov["total"]), fmt_usd(ov["spot"]), fmt_usd(ov["earn"])), ephemeral=True)
            else:
                await interaction.followup.send("Bitget indisponible (clés ?).", ephemeral=True)
        except Exception as ex:
            await interaction.followup.send("Erreur synchro : %s" % ex, ephemeral=True)

    @tree.command(name="recap", description="Rapport patrimoine sur N jours (défaut 7)")
    @discord.app_commands.describe(jours="Nombre de jours (1 à 365, défaut 7)")
    async def _cmd_recap(interaction, jours: int = 7):
        await interaction.response.defer(ephemeral=True, thinking=True)
        d = max(1, min(int(jours), 365))
        await interaction.followup.send(embed=await build_recap_embed(d), ephemeral=True)

    @tree.command(name="historique", description="Évolution du patrimoine : graphe, variation par poste, projection 12M")
    @discord.app_commands.describe(jours="Fenêtre en jours (2 à 365, défaut 30)")
    async def _cmd_histo(interaction, jours: int = 30):
        await interaction.response.defer(ephemeral=True, thinking=True)
        d = max(2, min(int(jours), 365))
        await refresh_bitget_state(HTTP_SESSION)
        record_snapshot()
        emb = build_history_embed(d)
        if emb is None:
            await interaction.followup.send(
                "Pas encore assez d'historique. Le bot enregistre 1 point par jour "
                "automatiquement — reviens demain 😉", ephemeral=True)
            return
        await interaction.followup.send(embed=emb, ephemeral=True)

    @tree.command(name="pdf", description="Rapport patrimoine en PDF sur N jours (défaut 30)")
    @discord.app_commands.describe(jours="Nombre de jours (1 à 365, défaut 30)")
    async def _cmd_pdf(interaction, jours: int = 30):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            d = max(1, min(int(jours), 365))
            await refresh_bitget_state(HTTP_SESSION)
            data = await build_pdf_report_async(d)
            f = discord.File(io.BytesIO(data), filename="rapport_nexus_%dj.pdf" % d)
            await interaction.followup.send(content="📄 **Rapport patrimoine (%d jours)**" % d, file=f, ephemeral=True)
        except Exception as ex:
            await interaction.followup.send("Erreur PDF : %s" % ex, ephemeral=True)

    @tree.command(name="aide", description="Aide complète et liste de toutes les commandes NEXUS")
    async def _cmd_aide(interaction):
        e = discord.Embed(title="📖 NEXUS — Guide complet", color=GOLD,
            description="Ton assistant patrimoine : **MoMo · NSIA · Bitget · rapports**, tout au même endroit.")
        e.add_field(name="🏦 Patrimoine & soldes", value=(
            "`/solde` — patrimoine total consolidé\n"
            "`/bitget` — détail du compte Bitget (spot/earn/staking)\n"
            "`/momo` — récap Mobile Money\n"
            "`/nsia` — portefeuille NSIA"), inline=False)
        e.add_field(name="📊 Rapports", value=(
            "`/rapport` — rapport complet\n"
            "`/recap [jours]` — rapport sur N jours · ex `/recap 30`\n"
            "`/historique [jours]` — évolution + graphe + projection 12M\n"
            "`/pdf [jours]` — rapport PDF · ex `/pdf 7`"), inline=False)
        e.add_field(name="🧠 Optimisations IA", value=(
            "`/optim` — plan d'action des 5 agents (patrimoine, dépenses, épargne, invest, objectifs)\n"
            "`/optim invest` — cibler un seul agent\n"
            "`/crypto` — division crypto : contrôle d'exactitude Bitget + analyse dédiée"), inline=False)
        e.add_field(name="⚙️ Contrôle", value=(
            "`/panel` — panneau de contrôle (tout d'un clic)\n"
            "`/sync` — synchroniser Bitget maintenant\n"
            "`/etat` — état du serveur\n"
            "`/aide` — ce guide"), inline=False)
        e.add_field(name="📥 Automatique (zéro effort)", value=(
            "Dépose un **PDF / capture / relevé** dans le salon d'import → chaque "
            "**dépense, retrait, dépôt** est noté tout seul. Une capture d'accueil "
            "**MTN/Moov** met à jour ton **solde**."), inline=False)
        e.set_footer(text="NEXUS • tape /panel pour le panneau cliquable")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @client.event
    async def on_ready():
        print("[discord] connecte comme %s — import: %s · panneau: %s"
              % (client.user, import_chan_id, PANEL_CHANNEL or "—"))
        try:
            client.add_view(PanelView())  # rend les boutons persistants apres redemarrage
            client.add_view(ReportActionView())  # boutons ✅/❌ des rapports persistants
        except Exception as e:
            log.error("discord add_view (boutons non persistants !): %s", e)
        # Synchronise les slash-commands (rapide si on cible la guilde)
        try:
            g = None
            if GUILD_ID and GUILD_ID.isdigit():
                g = discord.Object(id=int(GUILD_ID))
            else:
                for cid in (PANEL_CHANNEL, DISCORD_CHANNEL):
                    if cid and str(cid).isdigit():
                        ch = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
                        if ch and getattr(ch, "guild", None):
                            g = ch.guild
                            break
            if g:
                tree.copy_global_to(guild=g)
                await tree.sync(guild=g)
            else:
                await tree.sync()
        except Exception as e:
            log.error("discord: synchronisation des slash-commands: %s", e)
        # Le panneau ne doit JAMAIS bloquer le démarrage des tâches de fond : une erreur
        # de construction de la vue (ex. trop de boutons) laissait le bot sans brief auto
        # ni historique. On isole l'échec.
        try:
            await post_or_update_panel(client)
        except Exception as e:
            log.error("post_or_update_panel: %s", e)
        # Démarre le planificateur de récaps auto (une seule fois, même après reconnexion)
        if not getattr(client, "_recap_started", False):
            client._recap_started = True
            asyncio.create_task(recap_scheduler(client))
            asyncio.create_task(history_scheduler(client))
            asyncio.create_task(keepalive_task())
            asyncio.create_task(agents_scheduler(client))
            log.info("tâches de fond démarrées : récaps auto + historique/alertes + keep-alive + brief IA")

    @client.event
    async def on_message(message):
        try:
            if message.author.bot:
                return
            if message.channel.id != import_chan_id:
                return
            summaries = []
            for att in message.attachments:
                try:
                    s = await process_attachment(message, att)
                    if s:
                        summaries.append(s)
                except Exception as ex:
                    summaries.append("⚠️ Erreur sur %s : %s" % (att.filename, ex))
                    log.error("discord: piece jointe %s: %s", att.filename, ex)
            if message.content and re.search(r"fcfa|xof|cfa", message.content, re.I):
                n = add_momo(parse_momo_text(message.content))
                if n:
                    summaries.append("💬 **%d** opération(s) détectée(s) dans le message." % n)
                    await message.add_reaction("💸")
            if summaries:
                inc, exp, net, cnt = _momo_totals()
                emb = discord.Embed(title="📥 Import traité", color=GREEN,
                                    description="\n".join("• " + s for s in summaries))
                emb.add_field(name="Solde MoMo cumulé (net)", value=fmt_xof(net), inline=True)
                emb.add_field(name="Opérations enregistrées", value=str(cnt), inline=True)
                emb.set_footer(text="Importé automatiquement sur ton dashboard")
                # Réponse VISIBLE directement sous le fichier déposé (salon d'import)
                try:
                    await message.reply(embed=emb, mention_author=False)
                except Exception:
                    await message.channel.send(embed=emb)
                # + copie dans le salon de RAPPORTS avec boutons ✅/❌ (si configuré et différent)
                try:
                    if REPORT_CHANNEL and str(REPORT_CHANNEL) != str(import_chan_id):
                        await post_report(client, embed=emb, view=ReportActionView())
                except Exception:
                    pass
        except Exception as e:
            log.error("discord on_message: %s", e)

    # Laisse remonter les erreurs a main() qui gere la strategie de reconnexion.
    try:
        await client.start(DISCORD_TOKEN)
    finally:
        if not client.is_closed():
            try:
                await client.close()
            except Exception:
                pass

# ----------------------- Main -----------------------
async def main():
    load_state()
    print("=" * 58)
    print(" NEXUS SERVER - demarrage")
    print(" - Port HTTP        :", PORT)
    print(" - Auth token       :", "defini" if AUTH_TOKEN else "AUCUN")
    print(" - Bitget           :", "OK" if (BITGET_KEY and BITGET_SECRET and BITGET_PASS) else "non configure")
    print(" - Discord import   :", DISCORD_CHANNEL or "non configure")
    print(" - Discord panneau  :", PANEL_CHANNEL or "non configure")
    print(" - OCR (ocr.space)  :", "OK" if OCR_API_KEY else "non configure")
    print(" - IA / Agents      :", ("OK (%s via %s)" % (AI_MODEL, AI_PROVIDER)) if ai_enabled() else "non configure (definir la cle du fournisseur)")
    print(" - Proxy Bitget     :", "LECTURE+ECRITURE (!)" if BITGET_ALLOW_WRITE else "lecture seule")
    print(" - Historique MoMo  :", MOMO_MAX, "operations max")
    print("=" * 58)
    # Le jeton ne s'imprime plus : les logs Render sont consultables et ces deux
    # lignes suffisaient a le divulguer. On y accede par le bouton "Ouvrir l'app".
    if AUTH_WEAK:
        log.critical("SECURITE — AUTH_TOKEN absent, trop court, ou laisse a 'nexus229' "
                     "(valeur publique, presente dans le depot). L'API refuse toutes les "
                     "requetes tant qu'un jeton d'au moins 16 caracteres n'est pas defini. "
                     "Genere-le avec : %s", GEN_CMD)
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        log.info("Bitget non configure — le proxy /bitget renverra une erreur tant que les cles ne sont pas definies.")
    app = await start_http()
    # ---- Connexion Discord AUTO-RETRY ----
    # Avant : un seul echec de connexion au demarrage laissait le bot hors ligne
    # pour toujours (HTTP vivant, Discord mort). Maintenant : reconnexion sans fin
    # avec backoff progressif ; seuls le token invalide / intents manquants arretent.
    delay = 30
    while True:
        try:
            await run_discord(app["session"])
            log.warning("[discord] session fermee proprement — pas de reconnexion.")
            break
        except discord.LoginFailure as e:
            log.error("[discord] FATAL: TOKEN INVALIDE (DISCORD_TOKEN dans Render a regenerer) : %s", e)
            break
        except discord.PrivilegedIntentsRequired as e:
            log.error("[discord] FATAL: intent 'Message Content' non coche sur "
                      "discord.com/developers -> Bot -> Privileged Gateway Intents : %s", e)
            break
        except Exception as e:
            log.error("[discord] connexion perdue/echouee (%s: %s) — nouvel essai dans %ds",
                      type(e).__name__, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 900)   # 30s -> 60 -> ... -> 15 min max
    # garde le process vivant meme si Discord est desactive
    await asyncio.Event().wait()

def _install_shutdown():
    """Render envoie SIGTERM a chaque deploiement et avant la mise en veille.
    Sans ce hook, la derniere sauvegarde en attente etait perdue."""
    def _bye(signum, frame):
        log.info("signal %s recu — sauvegarde de l'etat", signum)
        try:
            flush_state()
        finally:
            sys.exit(0)
    for name in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), _bye)
        except Exception:
            pass

if __name__ == "__main__":
    _install_shutdown()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Arret.")
    finally:
        flush_state()
