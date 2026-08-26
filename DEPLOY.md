# 🚀 Déploiement & maintenance — NEXUS Bot

Service live : **https://nexus-bot-8iuv.onrender.com** · Repo : `kaizensyou2006-coder/nexus-bot`

---

## ⚠️ À faire en priorité après ce déploiement

Le durcissement décrit plus bas change deux comportements. Sans ces deux actions, le service **ne répondra plus** :

1. **Définir un `AUTH_TOKEN` d'au moins 16 caractères** dans Render → Environment.
   Auparavant, un `AUTH_TOKEN` vide ouvrait l'API à tout le monde et la valeur de repli
   `nexus229` est publique (elle était dans le dépôt). Le serveur refuse désormais toutes
   les requêtes tant qu'un vrai jeton n'est pas défini.
   ```bash
   python -c "import secrets;print(secrets.token_urlsafe(32))"
   ```
2. **Rouvrir l'app depuis le bouton « Ouvrir l'app » du panneau Discord** (une seule fois
   par appareil). Le nouveau jeton y est déjà intégré, et un cookie prend le relais ensuite.

---

## 1. Variables d'environnement (Render → Service → Environment)

Le bot lit **d'abord** les variables d'environnement, puis `nexus_config.json` en secours.
En production, tout doit venir des variables Render (`nexus_config.json` n'est **pas** dans le repo).

Liste complète : voir [`.env.example`](.env.example).

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | Token du bot Discord |
| `DISCORD_CHANNEL` / `PANEL_CHANNEL` / `REPORT_CHANNEL` | IDs des salons import / panneau / rapports |
| `PUBLIC_URL` | `https://nexus-bot-8iuv.onrender.com` |
| `AUTH_TOKEN` | **Obligatoire**, 16+ caractères aléatoires |
| `BITGET_KEY` / `BITGET_SECRET` / `BITGET_PASS` | API Bitget (proxy signé, lecture seule) |
| `OCR_API_KEY` | Lecture des captures MoMo |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Persistance de l'état |

---

## 2. Modèle de sécurité (ce que le serveur protège, et comment)

| Surface | Règle |
|---|---|
| `GET /` (l'app) | Exige le jeton. Sans lui : page de connexion, `401`. Le jeton n'est injecté dans la page **qu'après** authentification, puis déposé dans un cookie `HttpOnly` (90 j). |
| `/state`, `/momo`, `/bgcalib`, `/bitget/*` | Jeton requis, comparaison à temps constant. Accepté via `Authorization: Bearer`, `x-auth`, cookie, ou `?token=` (compat). |
| `/bitget/*` | **Lecture seule.** Seuls les chemins de consultation de solde et de prix passent ; `place-order`, `withdrawal`, etc. renvoient `403`. `BITGET_ALLOW_WRITE=1` lève la restriction — à n'activer que sciemment. |
| Toutes les routes | `120` requêtes/min/IP (`RATE_MAX_REQ`), puis `429`. Après `AUTH_MAX_FAIL` jetons invalides, l'IP est bloquée `AUTH_BLOCK_SEC` secondes. |
| CORS | Restreint à `PUBLIC_URL` (plus de repli sur `*`). |
| Logs | Le jeton n'est plus imprimé au démarrage. |

**Si tu es verrouillé par le blocage anti-force-brute** : attends 15 minutes, ou redéploie
(le compteur est en mémoire du process).

---

## 3. ⚠️ Rotation des clés (à faire par toi — les anciennes ont fuité)

- [ ] **Bitget** : API Management → supprimer l'ancienne clé → en créer une nouvelle
      (droits *lecture seule*) → coller `BITGET_KEY/SECRET/PASS` dans Render.
- [ ] **Discord** : Developer Portal → Bot → *Reset Token* → coller `DISCORD_TOKEN`.
- [ ] **Upstash** : Console → *Reset token* → coller `UPSTASH_REDIS_REST_TOKEN`.
- [ ] **AUTH_TOKEN** : remplacer `nexus229` par une valeur longue aléatoire (cf. §1).
- [ ] Après chaque changement : *Manual Deploy → Clear cache & deploy*.

> Tant qu'une clé n'est pas régénérée, elle reste compromise — le durcissement du code
> n'y change rien.

---

## 4. Keep-alive (éviter le spin-down du plan gratuit)

Sur le plan gratuit Render, l'instance s'endort après ~15 min d'inactivité (le bot Discord
se déconnecte). L'auto-ping interne ne suffit pas : une instance endormie ne peut pas se
pinger elle-même.

**Solution :** un ping externe sur **cron-job.org** (gratuit) :
- URL : `https://nexus-bot-8iuv.onrender.com/health`
- Intervalle : toutes les **10 minutes**

`/health` et `/ping` répondent sans authentification, exprès pour ça.
Pour un vrai 24/7 sans latence, passer l'instance en payant (~7 $/mois).

---

## 5. Lancement local

```bash
pip install -r requirements.txt
export AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
python nexus_server.py
# puis http://127.0.0.1:8080/?token=<le jeton affiché par la commande ci-dessus>
```

## 6. Tests

```bash
python test_nexus.py
```

Couvre l'analyse des SMS et relevés MoMo, le relevé NSIA, le calcul des soldes par réseau,
la consolidation du patrimoine et l'authentification. **À lancer avant chaque déploiement** :
ces fonctions décident du montant affiché, et une régression y est silencieuse — un chiffre
faux, pas une erreur.
