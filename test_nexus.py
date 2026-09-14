#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests des briques metier de NEXUS : analyse des SMS/releves MoMo, releve NSIA,
soldes par reseau, consolidation, authentification.

Lancement :  python test_nexus.py     (aucune dependance hors requirements.txt)

Ces fonctions decident du montant du patrimoine affiche : une regression dans une
regex se traduit par un chiffre faux, pas par une erreur visible.
"""
import os, unittest

os.environ.setdefault("AUTH_TOKEN", "jeton-de-test-suffisamment-long")
os.environ.setdefault("STATE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_state"))
os.makedirs(os.environ["STATE_DIR"], exist_ok=True)

import nexus_server as N

N.UPSTASH_URL = ""          # aucun appel reseau pendant les tests
N.UPSTASH_TOKEN = ""


def reset():
    N.STATE.update({"momo": [], "nsia": None, "bitget": None, "balances": {},
                    "patrimoine": None, "history": [], "business": None})


class TestMontants(unittest.TestCase):
    def test_money_separateurs(self):
        for txt, val in [("12 500", 12500), ("12,500", 12500), ("12.500", 12500),
                         ("5000", 5000), ("1 234 567", 1234567)]:
            self.assertEqual(N._money(txt), val, txt)

    def test_eur_dernier_separateur_est_la_decimale(self):
        self.assertAlmostEqual(N._eur("218 248,59"), 218248.59)
        self.assertAlmostEqual(N._eur("218 248.59"), 218248.59)
        self.assertAlmostEqual(N._eur("9 163,1487"), 9163.1487)
        self.assertEqual(N._eur(""), 0.0)

    def test_montants_ignore_les_valeurs_liquidatives(self):
        # 4 decimales = quantite ou VL, pas un montant : ne doit pas etre retenu
        self.assertEqual(N._montants("VL 9 163,1487 montant 218 248,59"), [218248.59])


class TestSmsMoMo(unittest.TestCase):
    def setUp(self):
        reset()

    def test_depense_avec_frais_et_solde(self):
        sms = ("Paiement de 25 000 FCFA effectue chez BOUTIQUE MARIE. "
               "Frais: 125 FCFA. Nouveau solde: 340 500 FCFA. Transaction ID: 1234567890")
        p = N._parse_line(sms)
        self.assertIsNotNone(p)
        self.assertEqual(p["type"], "exp")
        self.assertEqual(p["amount"], 25000)        # ni les frais, ni le solde
        self.assertEqual(p["fee"], 125)
        self.assertEqual(p["balance"], 340500)
        self.assertEqual(p["ref"], "1234567890")

    def test_revenu(self):
        p = N._parse_line("Vous avez recu 50 000 FCFA de JEAN. Nouveau solde: 90 000 FCFA")
        self.assertEqual(p["type"], "inc")
        self.assertEqual(p["amount"], 50000)

    def test_les_frais_deviennent_une_operation_distincte(self):
        ops = N.parse_momo_text("Retrait de 10 000 FCFA. Frais: 200 FCFA. Ref: ABCD1234")
        self.assertEqual(len(ops), 2)
        self.assertEqual({o["amount"] for o in ops}, {10000, 200})
        self.assertTrue(all(o["type"] == "exp" for o in ops))

    def test_ligne_sans_montant_ignoree(self):
        self.assertIsNone(N._parse_line("Bonjour, merci de votre confiance."))
        self.assertIsNone(N._parse_line(""))

    def test_detection_reseau(self):
        self.assertEqual(N.detect_network("Transaction Moov Money reussie"), "moov")
        self.assertEqual(N.detect_network("MTN MoMo"), "mtn")

    def test_solde_capture_plausible_accepte(self):
        r = N.detect_balance("MTN MoMo\nSolde: 340 500 FCFA")
        self.assertIsNotNone(r)
        self.assertEqual(r[1], 340500)

    def test_solde_capture_aberrant_refuse(self):
        # 161 281 445 (référence/OCR lu comme solde) -> refusé, ne gonfle pas le patrimoine
        self.assertIsNone(N.detect_balance("MTN\nSolde 161 281 445"))
        self.assertGreater(N.MOMO_BALANCE_MAX, 0)


class TestReleveMoMo(unittest.TestCase):
    RELEVE = ("Details de la transaction\n"
              "12 mars 2026 14:30 Paiement marchand -25000 1234567890 125 FCFA\n"
              "13 mars 2026 09:15 Depot +50000 1234567891 0 FCFA\n")

    def setUp(self):
        reset()

    def test_le_signe_donne_le_sens(self):
        rows = N.parse_momo_statement(self.RELEVE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "exp")
        self.assertEqual(rows[0]["amount"], 25000)
        self.assertEqual(rows[0]["date"], "2026-03-12")
        self.assertEqual(rows[1]["type"], "inc")
        self.assertEqual(rows[1]["date"], "2026-03-13")

    def test_reimport_du_meme_releve_ne_cree_pas_de_doublon(self):
        premier = N.add_momo(N.parse_momo_text(self.RELEVE))
        self.assertGreater(premier, 0)
        self.assertEqual(N.add_momo(N.parse_momo_text(self.RELEVE)), 0)
        self.assertEqual(len(N.STATE["momo"]), premier)

    def test_nettoyage_des_doublons(self):
        N.add_momo(N.parse_momo_text(self.RELEVE))
        N.STATE["momo"].append(dict(N.STATE["momo"][0]))   # doublon injecte a la main
        self.assertEqual(N.clean_duplicates(), 1)


class TestSoldes(unittest.TestCase):
    """Regression : un solde capture est un STOCK. Avant, il etait ecrase des qu'une
    transaction existait pour le reseau, et remplace par la somme des FLUX."""

    def setUp(self):
        reset()

    def test_sans_capture_on_retombe_sur_le_net_des_flux(self):
        N.STATE["momo"] = [{"net": "mtn", "type": "inc", "amount": 10000, "ts": 100},
                           {"net": "mtn", "type": "exp", "amount": 4000, "ts": 200}]
        self.assertEqual(N.momo_balance_by_net()["mtn"], 6000)
        self.assertEqual(N.momo_balance_sources()["mtn"], "flux")

    def test_le_solde_capture_fait_foi(self):
        N.STATE["balances"] = {"mtn": {"amount": 500000, "ts": 1000}}
        N.STATE["momo"] = [{"net": "mtn", "type": "exp", "amount": 4000, "ts": 500}]  # avant la photo
        self.assertEqual(N.momo_balance_by_net()["mtn"], 500000)
        self.assertEqual(N.momo_balance_sources()["mtn"], "capture")

    def test_seules_les_operations_posterieures_sont_ajoutees(self):
        N.STATE["balances"] = {"mtn": {"amount": 500000, "ts": 1000}}
        N.STATE["momo"] = [{"net": "mtn", "type": "exp", "amount": 4000, "ts": 500},
                           {"net": "mtn", "type": "exp", "amount": 7000, "ts": 2000}]
        self.assertEqual(N.momo_balance_by_net()["mtn"], 493000)

    def test_ancien_format_de_solde_encore_lu(self):
        N.STATE["balances"] = {"moov": 250000}   # nombre nu : etats sauvegardes avant le correctif
        self.assertEqual(N.momo_balance_by_net()["moov"], 250000)

    def test_ancien_format_avec_operations_ne_gonfle_pas_le_total(self):
        """Regression constatee en production : un solde d'avant l'horodatage est un
        simple nombre (ts=0). On en deduisait qu'aucune operation n'y figurait encore
        et on les rajoutait TOUTES par-dessus -> alerte patrimoine a +11725 %.
        Sans horodatage, la photo du compte vaut seule."""
        N.STATE["balances"] = {"mtn": 340500}
        N.STATE["momo"] = ([{"net": "mtn", "type": "exp", "amount": 5000, "ts": 1000 + i}
                            for i in range(50)]
                           + [{"net": "mtn", "type": "inc", "amount": 200000, "ts": 5000 + i}
                              for i in range(30)])
        self.assertEqual(N.momo_balance_by_net()["mtn"], 340500)

    def test_consolidation(self):
        N.STATE["balances"] = {"mtn": {"amount": 100000, "ts": 0}}
        N.STATE["nsia"] = {"total": 218248}
        N.STATE["bitget"] = {"total": 1000}
        N._USD_XOF_LIVE = 600.0
        c = N.consolidated_total()
        self.assertEqual(c["bitget_xof"], 600000)
        self.assertEqual(c["total"], 100000 + 218248 + 600000)


class TestNsia(unittest.TestCase):
    RELEVE = ("NSIA Gestion d'Actifs - Releve de Portefeuille\n"
              "Fonds AURORE OPCVM  VL 9 163,1487\n"
              "Total portefeuille 218 248,59 200 000,00 18 248,59\n"
              "12/01/2026 Souscription 100 000,00 0,00\n"
              "05/03/2026 Rachat 20 000,00 1 500,00\n")

    def setUp(self):
        reset()

    def test_extraction(self):
        r = N.parse_nsia(self.RELEVE)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["total"], 218248.59)
        self.assertAlmostEqual(r["pv_latente"], 18248.59)
        self.assertEqual(len(r["operations"]), 2)
        self.assertAlmostEqual(r["invested"], 80000.0)   # 100 000 souscrits - 20 000 rachetes

    def test_texte_hors_sujet_ignore(self):
        self.assertIsNone(N.parse_nsia("Bonjour, votre commande est prete."))
        self.assertIsNone(N.parse_nsia(""))


class TestPeriodes(unittest.TestCase):
    def setUp(self):
        reset()

    def test_fenetre_glissante(self):
        import datetime
        today = N.today_wat()
        vieux = (today - datetime.timedelta(days=40)).isoformat()
        N.STATE["momo"] = [
            {"net": "mtn", "type": "inc", "amount": 1000, "date": today.isoformat(), "ts": 0},
            {"net": "mtn", "type": "inc", "amount": 9999, "date": vieux, "ts": 0},
        ]
        inc, exp, net, cnt = N.momo_totals_period(7)
        self.assertEqual((inc, cnt), (1000, 1))
        inc365, _, _, cnt365 = N.momo_totals_period(365)
        self.assertEqual((inc365, cnt365), (10999, 2))

    def test_heure_du_benin_est_tz_aware(self):
        self.assertIsNotNone(N.now_wat().tzinfo)


class FakeReq:
    def __init__(self, headers=None, query=None, cookies=None):
        self.headers, self.query, self.cookies = headers or {}, query or {}, cookies or {}


class TestAuth(unittest.TestCase):
    def test_bearer_x_auth_cookie_et_query(self):
        tok = N.AUTH_TOKEN
        for req in (FakeReq(headers={"Authorization": "Bearer " + tok}),
                    FakeReq(headers={"x-auth": tok}),
                    FakeReq(cookies={N.AUTH_COOKIE: tok}),
                    FakeReq(query={"token": tok})):
            self.assertTrue(N.check_token(req))

    def test_mauvais_jeton_refuse(self):
        self.assertFalse(N.check_token(FakeReq(query={"token": "mauvais"})))
        self.assertFalse(N.check_token(FakeReq()))

    def test_sans_auth_token_configure_tout_est_refuse(self):
        """Avant : AUTH_TOKEN vide => check_token renvoyait True => API grande ouverte."""
        old = N.AUTH_TOKEN
        try:
            N.AUTH_TOKEN = ""
            self.assertFalse(N.check_token(FakeReq(query={"token": ""})))
        finally:
            N.AUTH_TOKEN = old


class TestDivisionCrypto(unittest.TestCase):
    def test_agents_crypto_definis(self):
        self.assertEqual(set(N.CRYPTO_ORDER), set(N.CRYPTO_AGENTS.keys()))
        self.assertIn("integrite", N.CRYPTO_AGENTS)      # agent d'exactitude
        for meta in N.CRYPTO_AGENTS.values():
            self.assertTrue(meta.get("sys") and meta.get("name") and meta.get("emoji"))

    def test_run_division_batch_un_seul_appel(self):
        """Le mode batch parse une division entière depuis UN objet {agents, synthese}."""
        import asyncio, json
        calls = {"n": 0}
        async def fake_ai(session, system, user, **k):
            calls["n"] += 1
            return json.dumps({"agents": [
                {"cle": "patrimoine", "score": 70, "resume": "ok",
                 "recommandations": [{"titre": "T", "detail": "d", "impact": "n/d", "priorite": "haute"}]},
                {"cle": "depenses", "score": 55, "resume": "ok2", "recommandations": []}],
                "synthese": {"score": 62, "resume": "s",
                             "recommandations": [{"titre": "S", "detail": "d", "priorite": "haute"}]}})
        old_ai, old_batch = N._ai_text, N.AI_BATCH_AGENTS
        try:
            N._ai_text = fake_ai
            N.AI_BATCH_AGENTS = True
            agents, synth = asyncio.new_event_loop().run_until_complete(
                N.run_division(None, ["patrimoine", "depenses"], N.AGENTS, "SNAP", "Finance"))
            self.assertEqual(calls["n"], 1)                       # UN seul appel pour 2 agents
            self.assertEqual(agents[0]["name"], N.AGENTS["patrimoine"]["name"])
            self.assertEqual(agents[0]["score"], 70)
            self.assertTrue(synth and synth.get("recommandations"))
        finally:
            N._ai_text, N.AI_BATCH_AGENTS = old_ai, old_batch

    def test_business_agents_definis(self):
        self.assertEqual(set(N.BUSINESS_ORDER), set(N.BUSINESS_AGENTS.keys()))
        for k in ("revenus", "tresorerie", "dettes", "fiscalite", "securite"):
            self.assertIn(k, N.BUSINESS_AGENTS)
        # 3 divisions de 6 agents = 18 au total
        self.assertEqual(len(N.AGENTS) + len(N.CRYPTO_AGENTS) + len(N.BUSINESS_AGENTS), 18)
        for reg, order in ((N.AGENTS, N.AGENT_ORDER), (N.CRYPTO_AGENTS, N.CRYPTO_ORDER), (N.BUSINESS_AGENTS, N.BUSINESS_ORDER)):
            self.assertEqual(set(reg.keys()), set(order))

    def test_coin_base(self):
        self.assertEqual(N._coin_base("BTC ⟢Earn"), "BTC")
        self.assertEqual(N._coin_base("eth"), "ETH")

    def test_reconcile_sans_cles_bitget(self):
        import asyncio
        old = (N.BITGET_KEY, N.BITGET_SECRET, N.BITGET_PASS)
        try:
            N.BITGET_KEY = N.BITGET_SECRET = N.BITGET_PASS = ""
            r = asyncio.new_event_loop().run_until_complete(N.bitget_reconcile(None))
            self.assertFalse(r["ok"])
            self.assertTrue(any(f["niveau"] == "erreur" for f in r["findings"]))
        finally:
            N.BITGET_KEY, N.BITGET_SECRET, N.BITGET_PASS = old


class TestPanneauDiscord(unittest.TestCase):
    """Discord limite chaque rangée d'un View à 5 boutons. Un 6e -> ValueError au
    démarrage, qui bloquait on_ready (panneau + tâches de fond). Garde-fou statique."""

    def test_max_5_boutons_par_rangee(self):
        import re, collections
        src = open(N.__file__, encoding="utf-8").read()
        counts = collections.Counter(re.findall(r"row=(\d)", src))
        for row, n in sorted(counts.items()):
            self.assertLessEqual(n, 5, "rangée %s du panneau : %d boutons (max 5)" % (row, n))


class TestProxyBitget(unittest.TestCase):
    def test_chemins_de_lecture_uniquement(self):
        self.assertIn("/api/v2/account/all-account-balance", N.BITGET_READ_PATHS)
        for dangereux in ("/api/v2/spot/trade/place-order", "/api/v2/spot/wallet/withdrawal"):
            self.assertFalse(any(dangereux.startswith(p) for p in N.BITGET_READ_PATHS), dangereux)


class TestAgentsIA(unittest.TestCase):
    """Le moteur d'agents : construction de la photo chiffree + parsing des reponses.
    (Les appels reseau a Claude ne sont pas testes ici : logique deterministe seulement.)"""

    def setUp(self):
        reset()

    def test_finance_snapshot_contient_les_postes(self):
        N.STATE["balances"] = {"mtn": {"amount": 100000, "ts": 0}}
        N.STATE["nsia"] = {"total": 218248, "invested": 200000, "pv_latente": 18248}
        N.STATE["bitget"] = {"total": 500, "holdings": [{"coin": "BTC", "val": 400},
                                                        {"coin": "ETH", "val": 100}]}
        N._USD_XOF_LIVE = 600.0
        snap = N.finance_snapshot()
        self.assertIn("PATRIMOINE", snap)
        self.assertIn("NSIA", snap)
        self.assertIn("BTC", snap)                 # position Bitget listee
        self.assertIn("FLUX MOBILE MONEY", snap)   # les fenetres glissantes

    def test_parse_agent_json_direct(self):
        obj = N._parse_agent_json('{"score": 72, "resume": "ok", "recommandations": []}')
        self.assertEqual(obj["score"], 72)

    def test_parse_agent_json_avec_bloc_de_code(self):
        txt = "```json\n{\"score\": 40, \"resume\": \"x\", \"recommandations\": []}\n```"
        self.assertEqual(N._parse_agent_json(txt)["score"], 40)

    def test_parse_agent_json_entoure_de_texte(self):
        txt = "Voici mon analyse : {\"score\": 55, \"recommandations\": []} — fin."
        self.assertEqual(N._parse_agent_json(txt)["score"], 55)

    def test_parse_agent_json_illisible_ne_leve_pas(self):
        obj = N._parse_agent_json("pas du tout du json")
        self.assertIn("recommandations", obj)      # repli structure, jamais d'exception
        self.assertEqual(obj["recommandations"], [])

    def test_agents_definis(self):
        self.assertEqual(set(N.AGENT_ORDER), set(N.AGENTS.keys()))
        self.assertIn("objectifs", N.AGENTS)          # 5e agent
        for meta in N.AGENTS.values():
            self.assertTrue(meta.get("sys") and meta.get("name") and meta.get("emoji"))


class TestObjectifsEtActions(unittest.TestCase):
    def setUp(self):
        reset()
        N.STATE["objectifs"] = None
        N.STATE["catexp"] = None
        N.STATE["ai_snaps"] = []
        N.STATE["ai_actions"] = []

    def test_sanitize_objectifs_borne_et_typé(self):
        o = N.sanitize_objectifs({"cur": "XOF",
            "budgets": [{"cat": "Loisirs", "limit": "50000", "spent": 61000}],
            "goals": [{"name": "Urgence", "current": 300000, "target": 1000000, "deadline": "2026-12-31"}],
            "dcas": [{"asset": "BTC", "amount": 20000, "freq": "Mensuel", "auto": True}]})
        self.assertEqual(o["budgets"][0]["limit"], 50000)
        self.assertEqual(o["goals"][0]["target"], 1000000)
        self.assertTrue(o["dcas"][0]["auto"])

    def test_clean_action_valide_et_rejette(self):
        self.assertIsNone(N._clean_action({"type": "achat", "actif": "BTC"}))         # type interdit
        self.assertIsNone(N._clean_action({"type": "budget", "categorie": "X"}))      # montant manquant
        a = N._clean_action({"type": "budget", "categorie": "Loisirs", "montant": 45000})
        self.assertEqual(a["type"], "budget")
        self.assertIn("label", a)

    def test_collect_actions_dedoublonne(self):
        data = {"synthese": {"recommandations": [
                    {"titre": "x", "action": {"type": "budget", "categorie": "Loisirs", "montant": 45000}}]},
                "agents": [{"recommandations": [
                    {"titre": "y", "action": {"type": "budget", "categorie": "Loisirs", "montant": 45000}},  # doublon
                    {"titre": "z", "action": {"type": "dca", "actif": "BTC", "montant": 20000, "frequence": "Mensuel"}}]}]}
        acts = N.collect_actions(data)
        self.assertEqual(len(acts), 2)               # le doublon budget est fusionné

    def test_memoire_evolution_par_categorie(self):
        N.STATE["ai_snaps"] = [{"d": "2000-01-01", "total": 1000000, "exp30": 200000,
                                "expM": 180000, "revM": 300000,
                                "catexp": {"Loisirs": 50000}}]
        N.STATE["catexp"] = {"Loisirs": 61000}
        bloc = N._evolution_block()
        self.assertIn("Loisirs", bloc)
        self.assertIn("+22%", bloc)                  # 50k -> 61k

    def test_objectifs_block_signale_depassement(self):
        N.STATE["objectifs"] = N.sanitize_objectifs({"budgets": [{"cat": "Loisirs", "limit": 50000, "spent": 61000}]})
        self.assertIn("DÉPASSÉ", N._objectifs_block())


class TestFournisseurIA(unittest.TestCase):
    """Le fournisseur d'IA est configurable : Anthropic OU compatible OpenAI (DeepSeek...)."""

    def test_openai_url_construite(self):
        old = N.AI_BASE_URL
        try:
            N.AI_BASE_URL = "https://api.deepseek.com"
            self.assertEqual(N._openai_url(), "https://api.deepseek.com/chat/completions")
            N.AI_BASE_URL = "https://openrouter.ai/api/v1"
            self.assertEqual(N._openai_url(), "https://openrouter.ai/api/v1/chat/completions")
            N.AI_BASE_URL = "http://localhost:11434/v1/chat/completions"   # deja complet
            self.assertEqual(N._openai_url(), "http://localhost:11434/v1/chat/completions")
        finally:
            N.AI_BASE_URL = old

    def test_parse_json_accolade_dupliquee(self):
        # Sortie malformee constatee sur un modele gratuit : '{' en double au debut.
        brut = '{\n{\n  "score": 82, "resume": "ok", "recommandations": [{"titre": "T"}]}'
        obj = N._parse_agent_json(brut)
        self.assertEqual(obj.get("score"), 82)
        self.assertEqual(obj["recommandations"][0]["titre"], "T")

    def test_parse_json_avec_texte_autour(self):
        obj = N._parse_agent_json('Voici le JSON : {"score": 5, "recommandations": []} merci')
        self.assertEqual(obj.get("score"), 5)

    def test_flatten_content(self):
        self.assertEqual(N._flatten_content("bonjour"), "bonjour")
        self.assertEqual(N._flatten_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "ab")

    def test_ai_enabled_selon_fournisseur(self):
        old_p, old_a, old_k, old_b = N.AI_PROVIDER, N.ANTHROPIC_API_KEY, N.AI_API_KEY, N.AI_BASE_URL
        try:
            N.AI_PROVIDER = "anthropic"; N.ANTHROPIC_API_KEY = ""
            self.assertFalse(N.ai_enabled())
            N.ANTHROPIC_API_KEY = "sk-ant-xxx"
            self.assertTrue(N.ai_enabled())
            N.AI_PROVIDER = "openai"; N.AI_API_KEY = ""; N.AI_BASE_URL = "https://api.deepseek.com"
            self.assertFalse(N.ai_enabled())          # cle fournisseur requise
            N.AI_API_KEY = "sk-deepseek"
            self.assertTrue(N.ai_enabled())
            N.AI_API_KEY = ""; N.AI_BASE_URL = "http://localhost:11434/v1"
            self.assertTrue(N.ai_enabled())           # Ollama local : aucune cle
        finally:
            N.AI_PROVIDER, N.ANTHROPIC_API_KEY, N.AI_API_KEY, N.AI_BASE_URL = old_p, old_a, old_k, old_b


if __name__ == "__main__":
    unittest.main(verbosity=2)
