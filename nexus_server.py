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
import os, sys, json, time, hmac, hashlib, base64, asyncio, re, io, itertools
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
GUILD_ID        = _conf("GUILD_ID")                   # optionnel : sync rapide des slash-commands
PUBLIC_URL      = _conf("PUBLIC_URL").rstrip("/")     # ex: http://34.x.x.x:8080  (pour le bouton "Ouvrir l'app")
AUTH_TOKEN      = _conf("AUTH_TOKEN", "nexus229")
BITGET_KEY      = _conf("BITGET_KEY")
BITGET_SECRET   = _conf("BITGET_SECRET")
BITGET_PASS     = _conf("BITGET_PASS")
OCR_API_KEY     = _conf("OCR_API_KEY")
PORT            = int(_conf("PORT") or os.environ.get("SERVER_PORT") or "8080")
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

def load_state():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE.update(json.load(f))
    except Exception:
        pass

def save_state():
    STATE["updatedAt"] = int(time.time() * 1000)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("[state] save error: %s\n" % e)

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

async def bitget_spot_total(session):
    """Valeur totale du spot Bitget en USDT + detail des positions.
    Renvoie (total_usdt, [(coin, montant, valeur_usdt), ...]) ou (None, []) si clecs absentes."""
    if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
        return None, []
    assets = []
    try:
        _, txt = await bitget_request(session, "GET", "/api/v2/spot/account/assets")
        j = json.loads(txt)
        if j.get("code") == "00000":
            for a in j.get("data", []):
                amt = (float(a.get("available", 0) or 0)
                       + float(a.get("frozen", 0) or 0)
                       + float(a.get("locked", 0) or 0))
                if amt > 0:
                    assets.append((str(a.get("coin", "")).upper(), amt))
    except Exception as e:
        sys.stderr.write("[bitget] assets: %s\n" % e)
        return None, []
    if not assets:
        return 0.0, []
    prices = {}
    try:
        _, txt2 = await bitget_request(session, "GET", "/api/v2/spot/market/tickers")
        j2 = json.loads(txt2)
        for t in j2.get("data", []):
            prices[t.get("symbol", "")] = float(t.get("lastPr", 0) or 0)
    except Exception as e:
        sys.stderr.write("[bitget] tickers: %s\n" % e)
    total, holdings = 0.0, []
    for coin, amt in assets:
        if coin in ("USDT", "USDC", "USD", "BUSD"):
            val = amt
        else:
            val = amt * prices.get(coin + "USDT", 0.0)
        total += val
        holdings.append((coin, amt, val))
    holdings.sort(key=lambda x: -x[2])
    return total, holdings

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

def _emit_ops(parsed):
    """Transforme une liste d'operations analysees en entrees MoMo (+ frais separes)."""
    out = []
    base_ts = int(time.time() * 1000)
    for p in parsed:
        out.append({
            "id": new_id(), "ts": base_ts,
            "type": p["type"], "amount": p["amount"], "cur": "XOF",
            "payee": p.get("payee", ""), "ref": p.get("ref", ""), "date": p.get("date"),
            "balance": p.get("balance"), "text": (p.get("text") or "")[:180], "src": "discord",
        })
        if p.get("fee") and p["fee"] > 0:
            ref = p.get("ref", "")
            out.append({
                "id": new_id(), "ts": base_ts,
                "type": "exp", "amount": p["fee"], "cur": "XOF",
                "payee": "Frais MoMo", "ref": (ref + "_fee") if ref else "",
                "date": p.get("date"), "balance": None,
                "text": ("Frais — " + (p.get("text") or ""))[:180], "src": "discord",
            })
    return out

def parse_momo_text(text):
    """Analyse un texte MoMo. Essaie d'abord le format RELEVE officiel (tableau PDF),
    sinon retombe sur l'analyse ligne-par-ligne (SMS / captures OCR)."""
    if not text:
        return []
    stmt = parse_momo_statement(text)
    if stmt:
        return _emit_ops(stmt)
    rows = [p for p in (_parse_line(r) for r in re.split(r"[\n\r]+", text)) if p]
    return _emit_ops(rows)

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
def parse_nsia(text):
    if not text:
        return None
    low = text.lower()
    if not any(k in low for k in ("nsia", "opcvm", "portefeuille", "aurore", "fonds", "fcp", "valeur liquidative")):
        return None
    def amt(s):
        s2 = re.sub(r"[ .\u202f\u00a0](?=\d{3})", "", s)
        s2 = re.split(r"[.,]", s2)[0]
        d = re.sub(r"[^\d]", "", s2)
        return float(d) if d else 0
    money_re = r"\d{1,3}(?:[ .\u202f\u00a0]\d{3})+(?:[.,]\d+)?"
    best = None
    # 1) priorite aux lignes "valeur" / "total portefeuille"
    for line in re.split(r"[\n\r]+", text):
        ll = line.lower()
        if "valeur" in ll or ("total" in ll and "portefeuille" in ll):
            for sm in re.findall(money_re, line):
                a = amt(sm)
                if 10000 <= a <= 5000000000:
                    best = a
    # 2) sinon, plus gros montant formate du document
    if not best:
        cands = [amt(sm) for sm in re.findall(money_re, text)]
        cands = [c for c in cands if 10000 <= c <= 5000000000]
        if cands:
            best = max(cands)
    if best and best > 0:
        return {"source": "nsia", "total": best, "cur": "XOF",
                "ts": int(time.time() * 1000), "text": "NSIA - Releve de Portefeuille"}
    return None

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
        # Ouvrir http://SERVEUR/?token=LE_TOKEN configure l'app toute seule
        # (URL du serveur + token enregistres dans le navigateur, puis token retire de la barre d'adresse).
        if AUTH_TOKEN and request.query.get("token", "") == AUTH_TOKEN:
            boot = ("<script>try{localStorage.setItem('nexus_srv_url',location.origin);"
                    "localStorage.setItem('nexus_srv_token',%s);"
                    "history.replaceState(null,'',location.pathname);}catch(e){}</script>"
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
            total, holdings = await bitget_spot_total(HTTP_SESSION)
        except Exception as ex:
            total, holdings = None, []
            sys.stderr.write("[report] bitget: %s\n" % ex)
        if total is not None:
            top = "\n".join("• %s : %s" % (c, fmt_usd(v)) for c, a, v in holdings[:4] if v > 0.01)
            e.add_field(name="📈 Bitget (spot)", value="**%s**\n%s" % (fmt_usd(total), top or "—"), inline=True)
            STATE["bitget"] = {"total": total, "ts": int(time.time() * 1000),
                               "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in holdings]}
            save_state()
        if STATE.get("patrimoine"):
            e.add_field(name="🗂 Patrimoine (app)", value="Synchronisé depuis l'app ✅", inline=False)
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
        else:
            e.add_field(name="Valeur totale", value="**%s**" % fmt_xof(ns["total"]), inline=True)
            e.add_field(name="Mise à jour", value="<t:%d:R>" % int(ns.get("ts", 0) / 1000), inline=True)
        return e

    async def build_bitget_embed():
        e = discord.Embed(title="📈 Bitget — Spot", color=AMBER)
        if not (BITGET_KEY and BITGET_SECRET and BITGET_PASS):
            e.description = "Clés Bitget non configurées sur le serveur."
            return e
        try:
            total, holdings = await bitget_spot_total(HTTP_SESSION)
        except Exception as ex:
            e.description = "Erreur Bitget : %s" % ex
            return e
        if total is None:
            e.description = "Impossible de joindre Bitget (vérifie les clés / la permission Lecture)."
            return e
        e.add_field(name="Valeur totale", value="**%s**" % fmt_usd(total), inline=False)
        body = "\n".join("• **%s** — %s (%s)"
                         % (c, ("%.6f" % a).rstrip("0").rstrip("."), fmt_usd(v))
                         for c, a, v in holdings[:12] if v > 0.01) or "Aucune position."
        e.add_field(name="Positions", value=body[:1024], inline=False)
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
                await interaction.followup.send(embed=await build_report_embed(), ephemeral=True)
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
                total, holdings = await bitget_spot_total(HTTP_SESSION)
                if total is not None:
                    STATE["bitget"] = {"total": total, "ts": int(time.time() * 1000),
                                       "holdings": [{"coin": c, "amt": a, "val": v} for c, a, v in holdings]}
                    save_state()
                    await interaction.followup.send("🔄 Synchronisé. Bitget : **%s**." % fmt_usd(total), ephemeral=True)
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
            await message.add_reaction("🧾" if n else "❓")
            return ("🧾 **%d** opération(s) détectée(s) sur l'image." % n) if n else "🧾 Aucune opération détectée sur l'image."
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
