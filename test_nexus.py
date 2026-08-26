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


class TestProxyBitget(unittest.TestCase):
    def test_chemins_de_lecture_uniquement(self):
        self.assertIn("/api/v2/account/all-account-balance", N.BITGET_READ_PATHS)
        for dangereux in ("/api/v2/spot/trade/place-order", "/api/v2/spot/wallet/withdrawal"):
            self.assertFalse(any(dangereux.startswith(p) for p in N.BITGET_READ_PATHS), dangereux)


if __name__ == "__main__":
    unittest.main(verbosity=2)
