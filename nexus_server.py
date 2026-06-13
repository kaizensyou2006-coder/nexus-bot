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
import urllib.request
from urllib.parse import parse_qs
import aiohttp
from aiohttp import web

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
            print("[config] nexus_config.json charge OK (%d cles, %s)" % (len(_CFG), _enc))
            break
        except Exception:
            _CFG = {}
    if not _CFG:
        sys.stderr.write("[config] ERREUR: impossible de lire/parser nexus_config.json\n")
except Exception as _e:
    sys.stderr.write("[config] ERREUR lecture nexus_config.json: %s\n" % _e)
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
AUTH_TOKEN      = _conf("AUTH_TOKEN", "nexus229")
BITGET_KEY      = _conf("BITGET_KEY")
BITGET_SECRET   = _conf("BITGET_SECRET")
BITGET_PASS     = _conf("BITGET_PASS")
OCR_API_KEY     = _conf("OCR_API_KEY")
PORT            = int(_conf("PORT") or os.environ.get("SERVER_PORT") or "8080")
USD_XOF         = float(_conf("USD_XOF") or "640")   # taux approx USD->FCFA pour le total consolide
BITGET_BASE     = "https://api.bitget.com"

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
    "balances": {},      # soldes captures par reseau : {"mtn":..,"moov":..}
    "panel_msg": None,   # id du message du panneau de controle (pour le re-editer)
    "updatedAt": None,
}

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

def load_state():
    global STATE
    # 1) Base de donnees Upstash si configuree (persiste meme apres redemarrage)
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            raw = _upstash(["GET", "nexus_state"])
            if raw:
                STATE.update(json.loads(raw))
                print("[state] charge depuis Upstash (%d octets)" % len(raw))
                return
        except Exception as e:
            sys.stderr.write("[state] upstash load: %s\n" % e)
    # 2) Sinon fichier local
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE.update(json.load(f))
    except Exception:
        pass

def save_state():
    STATE["updatedAt"] = int(time.time() * 1000)
    data = json.dumps(STATE, ensure_ascii=False)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        sys.stderr.write("[state] file save error: %s\n" % e)
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            _upstash(["SET", "nexus_state", data])
        except Exception as e:
            sys.stderr.write("[state] upstash save: %s\n" % e)

def reset_state():
    """Remet tout a zero (MoMo, NSIA, Bitget, soldes, patrimoine) — efface aussi la base."""
    STATE["momo"] = []
    STATE["nsia"] = None
    STATE["bitget"] = None
    STATE["balances"] = {}
    STATE["patrimoine"] = None
    STATE["recap_keys"] = {}
    save_state()

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

async def bitget_overview(session):
    """Vue complete du compte Bitget en USDT.
    Renvoie un dict {total, spot, earn, others, holdings:[(coin,amt,val)]} ou None.
    'total' = valeur de TOUS les comptes (spot + earn + bots + futures...), pas juste le spot."""
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return None
    res = {"total": 0.0, "spot": 0.0, "earn": 0.0, "others": 0.0, "holdings": []}
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
        sys.stderr.write("[bitget] all-account-balance: %s\n" % e)
    # 2) Prix pour valoriser les positions
    prices = {}
    try:
        _, t2 = await bitget_request(session, "GET", "/api/v2/spot/market/tickers")
        for t in json.loads(t2).get("data", []):
            prices[t.get("symbol", "")] = float(t.get("lastPr", 0) or 0)
    except Exception as e:
        sys.stderr.write("[bitget] tickers: %s\n" % e)
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
        sys.stderr.write("[bitget] spot assets: %s\n" % e)
    # 4) Detail Earn (epargne / DCA)
    try:
        _, t4 = await bitget_request(session, "GET", "/api/v2/earn/account/assets")
        for e in json.loads(t4).get("data", []):
            amt = float(e.get("amount", 0) or 0)
            if amt > 0:
                c = str(e.get("coin", "")).upper()
                res["holdings"].append((c + " ⟢Earn", amt, _val(c, amt)))
    except Exception as e:
        sys.stderr.write("[bitget] earn assets: %s\n" % e)
    # Fallback : si all-account-balance vide, total = somme des positions
    if not got_total or res["total"] <= 0:
        res["total"] = sum(v for _, _, v in res["holdings"])
    res["holdings"].sort(key=lambda x: -x[2])
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
    STATE["momo"] = STATE["momo"][-800:]
    if n:
        save_state()
    return n

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
        sys.stderr.write("[momo] SMS recu mais aucun montant detecte: %r\n" % text[:120])
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
        sys.stderr.write("[pdf] %s\n" % e)
        return None, ""
    m = re.search(r"NEXUS_DATA\s*=\s*(\{.*\})", text, re.S)
    if m:
        try:
            return json.loads(m.group(1)), text
        except Exception:
            pass
    return None, text

# ----------------------- OCR image (ocr.space) -----------------------
async def ocr_image(session, data, filename):
    if not OCR_API_KEY:
        return ""
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_API_KEY)
    form.add_field("language", "fre")
    form.add_field("OCREngine", "2")
    form.add_field("scale", "true")
    form.add_field("isTable", "true")
    form.add_field("detectOrientation", "true")
    form.add_field("file", data, filename=filename or "img.png",
                   content_type="application/octet-stream")
    try:
        async with session.post("https://api.ocr.space/parse/image", data=form,
                                timeout=aiohttp.ClientTimeout(total=40)) as r:
            j = await r.json()
            res = j.get("ParsedResults") or []
            return " ".join(p.get("ParsedText", "") for p in res)
    except Exception as e:
        sys.stderr.write("[ocr] %s\n" % e)
        return ""

# ----------------------- Serveur HTTP -----------------------
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp

def check_token(request):
    tok = request.query.get("token", "") or request.headers.get("x-auth", "")
    return (not AUTH_TOKEN) or tok == AUTH_TOKEN

async def h_options(request):
    return cors(web.Response(status=204))

async def h_ping(request):
    return cors(web.Response(text="NEXUS server OK"))

async def h_state(request):
    if not check_token(request):
        return cors(web.json_response({"ok": False, "error": "bad token"}, status=401))
    if request.method == "POST":
        try:
            body = await request.json()
            STATE["patrimoine"] = body.get("patrimoine", STATE["patrimoine"])
            save_state()
        except Exception as e:
            return cors(web.json_response({"ok": False, "error": str(e)}, status=400))
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
        "balances": STATE.get("balances") or {},
        "updatedAt": STATE["updatedAt"],
    }))

async def h_momo_ingest(request):
    """Reception d'un SMS MoMo depuis le telephone (MacroDroid).
    GET  /momo?token=...&text=LE_SMS   ou   POST /momo?token=... (corps texte/json/form)."""
    if not check_token(request):
        return cors(web.json_response({"ok": False, "error": "bad token"}, status=401))
    text = request.query.get("text", "") or request.query.get("sms", "")
    src = request.query.get("src", "sms")
    if request.method == "POST" and not text:
        body = (await request.read())[:65536].decode("utf-8", "ignore")
        ct = request.headers.get("Content-Type", "")
        text = body
        if "json" in ct:
            try:
                text = json.loads(body).get("text", body)
            except Exception:
                pass
        elif "form-urlencoded" in ct:
            pq = parse_qs(body)
            text = (pq.get("text") or pq.get("sms") or [body])[0]
    n = momo_ingest_text(text, src)
    return cors(web.json_response({"ok": True, "added": n}))

async def h_momo_inbox(request):
    if not check_token(request):
        return cors(web.json_response({"ok": False, "error": "bad token"}, status=401))
    since = 0
    try:
        since = int(request.query.get("since", "0"))
    except Exception:
        since = 0
    data = [m for m in STATE["momo"] if m.get("ts", 0) > since]
    return cors(web.json_response({"ok": True, "data": data, "count": len(data)}))

async def h_bitget(request):
    if not check_token(request):
        return cors(web.json_response({"ok": False, "error": "bad token"}, status=401))
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return cors(web.json_response({"code": "NO_KEYS", "msg": "Cles Bitget non configurees"}, status=500))
    path = request.match_info.get("path", "")
    full = "/" + path
    if request.query_string:
        qs = "&".join(p for p in request.query_string.split("&") if not p.startswith("token="))
        if qs:
            full += "?" + qs
    body = ""
    if request.method == "POST":
        body = await request.text()
    session = request.app["session"]
    try:
        status, text = await bitget_request(session, request.method, full, body)
    except Exception as e:
        return cors(web.json_response({"code": "PROXY_ERR", "msg": str(e)}, status=502))
    resp = web.Response(text=text, status=status, content_type="application/json")
    return cors(resp)

async def h_app(request):
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PATRIMOINE_OS.html")
        with open(p, "rb") as f:
            data = f.read()
        # Le serveur sert SA propre app : il injecte TOUJOURS l'URL + le token,
        # pour que ça marche sur tout appareil (PC, téléphone) sans saisir de token.
        if AUTH_TOKEN:
            boot = ("<script>try{localStorage.setItem('nexus_srv_url',location.origin);"
                    "localStorage.setItem('nexus_srv_token',%s);"
                    "if(location.search.indexOf('token=')>=0)history.replaceState(null,'',location.pathname);}catch(e){}</script>"
                    % json.dumps(AUTH_TOKEN)).encode("utf-8")
            data = data.replace(b"<head>", b"<head>" + boot, 1)
        return web.Response(body=data, content_type="text/html", charset="utf-8")
    except Exception as e:
        return web.Response(text="App introuvable: %s" % e, status=404)

async def start_http():
    global HTTP_SESSION
    app = web.Application()
    app["session"] = aiohttp.ClientSession()
    HTTP_SESSION = app["session"]
    app.router.add_route("OPTIONS", "/{tail:.*}", h_options)
    app.router.add_get("/ping", h_ping)
    app.router.add_get("/health", h_ping)
    app.router.add_get("/", h_app)
    app.router.add_get("/app", h_app)
    app.router.add_get("/state", h_state)
    app.router.add_post("/state", h_state)
    app.router.add_get("/momo", h_momo_ingest)
    app.router.add_post("/momo", h_momo_ingest)
    app.router.add_get("/momo/inbox", h_momo_inbox)
    app.router.add_route("GET", "/bitget/{path:.*}", h_bitget)
    app.router.add_route("POST", "/bitget/{path:.*}", h_bitget)

    async def _close_session(app):
        await app["session"].close()
    app.on_cleanup.append(_close_session)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print("[http] API NEXUS sur le port %d" % PORT)
    return app

# ----------------------- Statistiques / nettoyage (sans dependance Discord) -----------------------
def _momo_totals():
    momo = STATE.get("momo") or []
    inc = sum(float(m.get("amount", 0) or 0) for m in momo if m.get("type") == "inc")
    exp = sum(float(m.get("amount", 0) or 0) for m in momo if m.get("type") == "exp")
    return inc, exp, inc - exp, len(momo)

def _wat_today():
    """Date du jour en heure du Bénin (UTC+1)."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=1)).date()

def _item_date(m):
    d = m.get("date")
    if d:
        try:
            return datetime.date.fromisoformat(d)
        except Exception:
            pass
    ts = (m.get("ts", 0) or 0) / 1000
    try:
        return (datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=1)).date()
    except Exception:
        return _wat_today()

def momo_totals_period(days, net=None):
    """Fenetre glissante de N jours -> (entrees, sorties, net, nb). net='mtn'|'moov'|None(tous)."""
    today = _wat_today()
    start = today - datetime.timedelta(days=int(days) - 1)
    items = STATE.get("momo") or []
    sel = [m for m in items if _item_date(m) >= start and (net is None or m.get("net", "mtn") == net)]
    inc = sum(float(m.get("amount", 0) or 0) for m in sel if m.get("type") == "inc")
    exp = sum(float(m.get("amount", 0) or 0) for m in sel if m.get("type") == "exp")
    return inc, exp, inc - exp, len(sel)

def momo_balance_by_net():
    """Solde par reseau : net des transactions si presentes, sinon solde capture."""
    momo = STATE.get("momo") or []
    nets = {}
    for m in momo:
        n = m.get("net", "mtn")
        a = float(m.get("amount", 0) or 0)
        nets[n] = nets.get(n, 0.0) + (a if m.get("type") == "inc" else -a)
    bal = dict(STATE.get("balances") or {})
    out = {}
    for n in set(list(nets.keys()) + list(bal.keys())):
        out[n] = nets[n] if n in nets else bal.get(n, 0.0)
    return out

def consolidated_total():
    """Patrimoine total combine en FCFA + le detail par poste."""
    bynet = momo_balance_by_net()
    momo_tot = sum(bynet.values())
    nsia = float((STATE.get("nsia") or {}).get("total", 0) or 0)
    bg_usd = float((STATE.get("bitget") or {}).get("total", 0) or 0)
    bg_xof = bg_usd * USD_XOF
    total = momo_tot + nsia + bg_xof
    return {"total": total, "momo": momo_tot, "bynet": bynet,
            "nsia": nsia, "bitget_usd": bg_usd, "bitget_xof": bg_xof}

def _ascii(s):
    """Nettoie pour le PDF (police latin-1)."""
    return (str(s or "")).encode("latin-1", "replace").decode("latin-1")

def build_pdf_report(days=30):
    """Genere un rapport patrimoine PDF stylise avec graphiques (octets)."""
    from fpdf import FPDF
    GOLD, GREEN, RED = (201, 162, 39), (22, 163, 74), (220, 38, 38)
    PURPLE, AMBER, DARK, GREY, LIGHT = (124, 58, 237), (245, 158, 11), (24, 24, 34), (120, 120, 140), (244, 244, 248)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    W, M = 210, 12
    CW = W - 2 * M
    c = consolidated_total()
    inc, exp, net, cnt = momo_totals_period(days)
    mtn, moov = momo_totals_period(days, "mtn"), momo_totals_period(days, "moov")

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
    # ---- Dernieres operations ----
    momo = STATE.get("momo") or []
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
        try:
            ov = await bitget_overview(HTTP_SESSION)
        except Exception as ex:
            ov = None
            sys.stderr.write("[report] bitget: %s\n" % ex)
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
        e.add_field(name="Valeur totale (tous comptes)", value="**%s**" % fmt_usd(ov["total"]), inline=False)
        e.add_field(name="Répartition",
                    value="Spot **%s** · Earn **%s** · Autres **%s**"
                          % (fmt_usd(ov["spot"]), fmt_usd(ov["earn"]), fmt_usd(ov["others"])), inline=False)
        body = "\n".join("• **%s** — %s (%s)"
                         % (c, ("%.6f" % a).rstrip("0").rstrip("."), fmt_usd(v))
                         for c, a, v in ov["holdings"][:12] if v > 0.01) or "Aucune position."
        e.add_field(name="Positions (Spot + Earn)", value=body[:1024], inline=False)
        e.timestamp = discord.utils.utcnow()
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
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
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
                sys.stderr.write("[recap] %s\n" % e)
            await asyncio.sleep(3600)  # vérifie toutes les heures

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
            sys.stderr.write("[report] envoi: %s\n" % e)
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
                        sys.stderr.write("[rescan] %s\n" % ex)
        except Exception as e:
            sys.stderr.write("[rescan] salon: %s\n" % e)
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

    class PanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            if PUBLIC_URL:
                self.add_item(discord.ui.Button(label="Ouvrir l'app", emoji="🌐",
                              url="%s/?token=%s" % (PUBLIC_URL, AUTH_TOKEN), row=1))

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
                data = build_pdf_report(30)
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

    async def post_or_update_panel(client):
        if not PANEL_CHANNEL or not str(PANEL_CHANNEL).isdigit():
            return
        try:
            ch = client.get_channel(int(PANEL_CHANNEL)) or await client.fetch_channel(int(PANEL_CHANNEL))
        except Exception as e:
            sys.stderr.write("[panel] salon introuvable: %s\n" % e)
            return
        emb = discord.Embed(
            title="🛰️ NEXUS — Panneau de contrôle",
            description=("Pilote ta **holding & finance** d'un clic.\n\n"
                         "📊 **Rapport complet** · 💸 **Récap MoMo** · 🏛 **NSIA** · 📈 **Bitget**\n"
                         "🔄 **Synchroniser** · 🧹 **Nettoyer les doublons** · ⚙️ **État serveur**\n\n"
                         "📥 *Dépose tes PDF / captures / relevés NSIA dans le salon d'import : "
                         "chaque dépense et retrait est noté automatiquement sur ton dashboard.*"),
            color=GOLD)
        emb.set_footer(text="NEXUS • toujours en ligne")
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
            print("[panel] panneau publié dans le salon %s" % PANEL_CHANNEL)
        except Exception as ex:
            sys.stderr.write("[panel] envoi impossible: %s\n" % ex)

    async def process_attachment(message, att):
        """Traite une piece jointe (PDF/image) -> texte de resume, et ajoute les reactions."""
        name = (att.filename or "").lower()
        data = await att.read()
        if name.endswith(".pdf"):
            obj, text = parse_pdf_bytes(data)
            if obj is not None:
                STATE["patrimoine"] = obj
                save_state()
                await message.add_reaction("✅")
                return "🗂 Patrimoine mis à jour depuis le PDF."
            nsia = parse_nsia(text)
            if nsia:
                set_nsia(nsia)
                await message.add_reaction("🏛")
                return "🏛 NSIA : total portefeuille **%s**." % fmt_xof(nsia["total"])
            n = add_momo(parse_momo_text(text))
            await message.add_reaction("📄")
            return ("📄 **%d** opération(s) détectée(s) dans le PDF." % n) if n else "📄 Aucune opération détectée dans le PDF."
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
                STATE.setdefault("balances", {})[net] = val
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

    @client.event
    async def on_ready():
        print("[discord] connecte comme %s — import: %s · panneau: %s"
              % (client.user, import_chan_id, PANEL_CHANNEL or "—"))
        try:
            client.add_view(PanelView())  # rend les boutons persistants apres redemarrage
            client.add_view(ReportActionView())  # boutons ✅/❌ des rapports persistants
        except Exception as e:
            sys.stderr.write("[discord] add_view: %s\n" % e)
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
            sys.stderr.write("[discord] sync commands: %s\n" % e)
        await post_or_update_panel(client)
        # Démarre le planificateur de récaps auto (une seule fois, même après reconnexion)
        if not getattr(client, "_recap_started", False):
            client._recap_started = True
            asyncio.create_task(recap_scheduler(client))
            print("[recap] planificateur de récaps auto démarré (jour/semaine/mois)")

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
                    sys.stderr.write("[discord] attach err: %s\n" % ex)
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
                # Retour posté dans le salon de RAPPORTS (REPORT_CHANNEL) avec boutons ✅/❌
                posted = await post_report(client, embed=emb, view=ReportActionView())
                if posted is None:
                    await message.channel.send(embed=emb)
        except Exception as e:
            sys.stderr.write("[discord] on_message err: %s\n" % e)

    try:
        await client.start(DISCORD_TOKEN)
    except Exception as e:
        sys.stderr.write("[discord] connexion impossible: %s\n" % e)
        print("[discord] echec connexion - l'API HTTP et le proxy Bitget restent actifs.")

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
    print(" - App auto-config  : http://TON_IP:%d/?token=%s" % (PORT, AUTH_TOKEN))
    print(" - MacroDroid (SMS) : http://TON_IP:%d/momo?token=%s&text={sms}" % (PORT, AUTH_TOKEN))
    print("=" * 58)
    app = await start_http()
    await run_discord(app["session"])
    # garde le process vivant meme si Discord est desactive
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArret.")
