"""Une sonde dit ce qu'elle COUVRE — pour qu'un vert dise ce qu'il vaut (oto#57).

Deux sondes voisines faisaient des choses différentes et rien ne les distinguait vu de
l'extérieur : l'une lit un solde, l'autre liste des objets — ce qui répond parfaitement
sur un compte à sec. Mesuré le 04/09/2026 : un préflight tout vert, puis 402 après
quatre espaces de travail, quatre tables et 28 lignes créés.

⚠️ **La sonde n'avait pas menti.** Elle avait rapporté un vert qui ne voulait pas dire
ce qu'on croyait. C'est la nuance qui décide du remède : il ne faut pas plus de sondes,
il faut qu'une sonde dise ce qu'elle couvre — pour qu'un appelant sache **ce qu'il ne
sait pas**.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.connectors import verify as V


@pytest.fixture(autouse=True)
def _registre_intact():
    """Le registre est un état de module : on le restaure, sinon un test qui
    enregistre une sonde bidon la laisse pour tous les suivants."""
    probes, couv = dict(V._REGISTRY), dict(V._COUVERTURE)
    yield
    V._REGISTRY.clear(), V._REGISTRY.update(probes)
    V._COUVERTURE.clear(), V._COUVERTURE.update(couv)


def test_le_defaut_est_auth_le_moins_promettant():
    """Une sonde ne prouve que ce qu'elle a mesuré. Déclarer `auth+quota` par défaut
    fabriquerait exactement le vert trompeur qu'on supprime."""
    V.register("bidon", lambda f, c: None)
    assert V.couverture("bidon") == V.AUTH


def test_une_couverture_se_declare_explicitement():
    V.register("bidon", lambda f, c: None, couvre=V.AUTH_QUOTA)
    assert V.couverture("bidon") == V.AUTH_QUOTA


def test_une_couverture_inconnue_est_REFUSEE_a_l_enregistrement():
    """Au chargement du module, devant celui qui peut corriger — jamais acceptée
    puis inerte, ce qui redonnerait une déclaration à laquelle on ne peut pas se fier."""
    with pytest.raises(ValueError):
        V.register("bidon", lambda f, c: None, couvre="auth+tout")


def test_sans_sonde_la_couverture_est_NULLE_pas_auth():
    """`None` = « je n'ai pas pu mesurer », `auth` = « j'ai mesuré l'authentification ».
    Les confondre ferait croire à un contrôle qui n'a pas eu lieu."""
    assert V.couverture("connecteur-qui-n-existe-pas") is None


def test_toutes_les_sondes_reelles_declarent_une_couverture_valide():
    """Garde d'inventaire : une sonde enregistrée sans couverture valide ferait
    répondre `null` à la capacité, donc « aucune sonde » sur un connecteur qui en a une."""
    from fastmcp import FastMCP

    from oto_mcp.tools import register_all
    register_all(FastMCP("t"))
    assert V._REGISTRY, "aucune sonde chargée — l'inventaire ne prouverait rien"
    for nom in V._REGISTRY:
        assert V.couverture(nom) in V.COUVERTURES, nom


#: Les sondes qui ont le DROIT de promettre `auth+quota`. Une entrée ici est une
#: affirmation : « celle-ci lit vraiment un solde ET lève quand il est vide ».
#:
#: ⚠️ Ajouter un nom sans la preuve qui va avec rendrait ce banc décoratif — c'est
#: exactement le vert trompeur qu'oto#57 supprime. Chaque entrée doit avoir son
#: épreuve de compte à sec, citée en regard.
QUOTA_AUTORISE = {
    # `tests/test_sonde_hunter.py::test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH`
    "hunter",
    # `tests/test_sonde_serpapi.py::test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH`
    "serpapi",
}


def test_aucune_sonde_ne_promet_le_quota_sans_le_mesurer():
    """Le cliquet. Il était à zéro tant qu'aucune sonde ne lisait de solde ; il
    porte maintenant la liste de celles qui en lisent un, et rien d'autre.

    Le message d'origine disait « mets ce banc à jour en même temps que la sonde,
    jamais avant ». C'est fait dans cet ordre : la sonde Hunter lève `QuotaEpuise`
    sur un compte à sec, son épreuve le prouve, et son nom entre ici ensuite."""
    from fastmcp import FastMCP

    from oto_mcp.tools import register_all
    register_all(FastMCP("t"))
    promettent = {n for n in V._REGISTRY if V.couverture(n) == V.AUTH_QUOTA}
    non_autorisees = sorted(promettent - QUOTA_AUTORISE)
    assert not non_autorisees, (
        f"ces sondes promettent le quota sans preuve : {non_autorisees}. Chacune doit "
        "LEVER sur un compte à sec — lire un solde sans tester sa valeur laisse la "
        "sonde verte, et c'est le vert trompeur que ce lot supprime. Écris l'épreuve "
        "de compte à sec, PUIS ajoute le nom à QUOTA_AUTORISE avec son chemin.")
    parties = sorted(QUOTA_AUTORISE - promettent)
    assert not parties, (
        f"{parties} sont listées comme mesurant le quota mais ne le déclarent plus : "
        "la liste ment dans l'autre sens, elle autorise ce qui n'existe plus.")

def test_la_capacite_sert_la_couverture_avec_le_verdict():
    """Servie AVEC `ok`, jamais à côté : un client qui lit le verdict sans la
    couverture croit en savoir plus qu'il n'en sait."""
    from oto_mcp.capabilities.connectors.verify import VerifyResult
    assert "coverage" in VerifyResult.model_fields


# ── le verdict : POURQUOI ça ne marche pas (oto#57, second volet) ─────────────
# Trois causes qui appellent des conduites OPPOSÉES — remplacer la clé, recharger le
# compte, ou ne surtout rien refaire — que le booléen `ok` ne distinguait pas. Le
# 04/09, un 402 et un 403 se ressemblaient exactement depuis l'extérieur.

def test_un_solde_vide_se_classe_no_quota():
    from oto.tools.common.errors import UpstreamHTTPError
    assert V.classer(UpstreamHTTPError(402, "no credits")) == V.NO_QUOTA


def test_un_refus_d_autorisation_se_classe_unauthorized():
    from oto.tools.common.errors import UpstreamHTTPError
    assert V.classer(UpstreamHTTPError(403, "MISSING_SCOPES")) == V.UNAUTHORIZED
    assert V.classer(UpstreamHTTPError(401, "Unauthorized")) == V.UNAUTHORIZED


def test_une_sonde_qui_SAIT_le_dit_sans_passer_par_un_code():
    """La voie explicite prime : elle survit à un amont qui répondrait 200 avec un
    corps d'erreur."""
    assert V.classer(V.QuotaEpuise("à sec")) == V.NO_QUOTA
    assert V.classer(V.NonAutorise("périmètre")) == V.UNAUTHORIZED


def test_ce_qu_on_ne_sait_pas_classer_est_unknown_pas_ok():
    """⚠️ `unknown` est un verdict à part entière. Le replier sur `ok` — ou sur
    `unauthorized` « par défaut » — ferait conclure à une cause qu'on n'a pas mesurée."""
    assert V.classer(ValueError("boom")) == V.UNKNOWN
    assert V.classer(TimeoutError("lent")) == V.UNKNOWN
    from oto.tools.common.errors import UpstreamHTTPError
    assert V.classer(UpstreamHTTPError(500, "amont HS")) == V.UNKNOWN


def test_le_classement_ne_lit_JAMAIS_le_texte_du_message():
    """Un classement bâti sur des mots change de sens au premier reformatage amont,
    et personne ne s'en aperçoit. Un message qui PARLE de crédits sans porter le code
    ne doit rien déclencher."""
    assert V.classer(RuntimeError("insufficient credits, please top up")) == V.UNKNOWN


def test_chaque_verdict_d_echec_porte_une_conduite():
    """Un diagnostic qui ne dit pas quoi faire renvoie chercher — c'est ainsi qu'une
    personne a relancé six fois une connexion parfaitement valide."""
    for v in (V.UNAUTHORIZED, V.NO_QUOTA, V.UNKNOWN):
        assert V.CONDUITE.get(v, "").strip(), v
    assert V.OK not in V.CONDUITE, "il n'y a rien à faire quand ça marche"


def test_la_capacite_sert_verdict_et_conduite():
    from oto_mcp.capabilities.connectors.verify import VerifyResult
    assert {"verdict", "next_step"} <= set(VerifyResult.model_fields)
