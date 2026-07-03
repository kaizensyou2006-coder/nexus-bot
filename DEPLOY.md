# 🚀 Déploiement & maintenance — NEXUS Bot

Service live : **https://nexus-bot-8iuv.onrender.com** · Repo : `kaizensyou2006-coder/nexus-bot`

---

## 1. Variables d'environnement (Render → Service → Environment)

Le bot lit **d'abord** les variables d'environnement, puis `nexus_config.json` en secours.
En production, tout doit venir des variables Render (le fichier `nexus_config.json` n'est **pas** dans le repo).

Liste complète : voir [`.env.example`](.env.example).

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | Token du bot Discord |
| `DISCORD_CHANNEL` / `PANEL_CHANNEL` / `REPORT_CHANNEL` | IDs des salons import / panneau / rapports |
| `PUBLIC_URL` | `https://nexus-bot-8iuv.onrender.com` |
| `AUTH_TOKEN` | Jeton API/app — **valeur longue aléatoire** |
| `BITGET_KEY` / `BITGET_SECRET` / `BITGET_PASS` | API Bitget (proxy signé) |
| `OCR_API_KEY` | Lecture des captures MoMo |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Persistance de l'état |

---

## 2. ⚠️ Rotation des clés (à faire par toi — les anciennes ont fuité)

Les clés Bitget/Discord ont été exposées par le passé. Tant qu'elles ne sont pas régénérées, elles restent compromises.

- [ ] **Bitget** : API Management → supprimer l'ancienne clé → en créer une nouvelle (droits *lecture seule* si tu ne trades pas via le bot) → coller `BITGET_KEY/SECRET/PASS` dans Render.
- [ ] **Discord** : Developer Portal → Bot → *Reset Token* → coller `DISCORD_TOKEN` dans Render.
- [ ] **Upstash** : Console → *Reset token* → coller `UPSTASH_REDIS_REST_TOKEN`.
- [ ] **AUTH_TOKEN** : remplacer `nexus229` par une valeur longue aléatoire.
- [ ] Après chaque changement dans Render : *Manual Deploy → Clear cache & deploy*.

> Astuce `AUTH_TOKEN` : génère-en un avec `python -c "import secrets;print(secrets.token_urlsafe(32))"`.

---

## 3. Keep-alive (éviter le spin-down du plan gratuit)

Sur le plan gratuit Render, l'instance s'endort après ~15 min d'inactivité (le bot Discord se déconnecte).

**Solution simple :** créer un ping externe sur **cron-job.org** (gratuit) :
- URL : `https://nexus-bot-8iuv.onrender.com/health`
- Intervalle : toutes les **10 minutes**

Le endpoint `/health` (et `/ping`) répond déjà côté serveur.
Pour un vrai 24/7 sans latence, passer l'instance en payant (~7 $/mois).

---

## 4. Lancement local

```bash
pip install -r requirements.txt
# renseigner nexus_config.json OU exporter les variables du .env.example
python nexus_server.py
```
