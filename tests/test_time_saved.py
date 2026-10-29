"""Le temps gagné des agents — compté depuis ce qu'ils ont FAIT (`time_saved.py`).

Ce que ce banc verrouille, sur le VRAI registre (les annotations que les outils servent,
pas une table recopiée ici) : SEUL ce qu'un outil déclare compte — un outil sans
annotation ne rapporte rien ; sur un outil qui agit, seul un envoi est un envoi
(`gmail_act op=archive|trash` ne l'est pas) ; préparer un brouillon ne compte pas ; la
tenue de la plateforme ne compte pas ; un nom préfixé se résout ; la recherche est
plafonnée par run ; une org surcharge les minutes, jamais l'inverse.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import time_saved, tool_registry

from _mcp_app import static_mcp


@pytest.fixture(scope="module")
def _effets_servis():
    """Les effets que le registre boot calcule, depuis les outils réellement servis."""
    outils = asyncio.run(static_mcp().list_tools(run_middleware=False))
    return {t.name: tool_registry._effet(t) for t in outils}


@pytest.fixture(autouse=True)
def _registre(monkeypatch, _effets_servis):
    monkeypatch.setattr(tool_registry, "_EFFETS", dict(_effets_servis))


def test_le_registre_reel_porte_les_annotations_attendues():
    """Ce sur quoi le banc s'appuie : si une annotation change, il le dit ici d'abord."""
    e = tool_registry.effet_de
    assert (e("serper_search"), e("gmail"), e("gmail_act"), e("whatsapp"),
            e("whatsapp_act")) == ("read", "simulation", "act", "read", "act")
    assert e("hubspot_object") is None                              # ne déclare rien
    assert e("http_get") == "read"          # déclaré en lecture : il cherche


@pytest.mark.parametrize("nom, op, attendu", [
    ("serper_search", None, "research"),        # LECTURE
    ("gmail", "search", "research"),            # SIMULATION : ses lectures cherchent
    ("gmail", "attachment", "research"),
    ("gmail", "draft", None),                   # préparer ne compte pas : l'envoi comptera
    ("gmail", "accounts", None),
    ("gmail_act", "send", "action"),            # ACTE : seul l'envoi est un envoi
    ("gmail_act", "archive", None),
    ("gmail_act", "trash", None),
    ("outlook_act", "move", None),
    ("whatsapp", "list", "research"),           # LECTURE : un lecteur de messagerie
    ("whatsapp_act", "send", "action"),         # ACTE : l'envoi
    ("apollo_act", "send_email", "action"),     # l'op porte son objet : le verbe compte
    ("linkedin_unipile_act", "publish_post", "action"),
    ("linkedin_unipile_act", "comment_post", "action"),
    ("linkedin_unipile_act", "cancel_invitation", None),
    ("data_write", None, "record"),             # écriture du tableau, nommée
    ("data_rows", None, None),                  # la tenue du tableau de l'agent
    ("data_claim_next", None, None),
    ("hubspot_object", "update", None),         # ne déclare rien : ne rapporte rien
    ("http_get", None, "research"),             # LECTURE déclarée : une recherche
    ("oto_doc", None, None),                    # la plateforme
    ("run_start", None, None),
    ("inconnu_partout", None, None),            # on ne devine pas
])
def test_le_type_vient_de_ce_que_l_outil_declare(nom, op, attendu):
    assert time_saved.kind_of(nom, op) == attendu


def test_un_nom_prefixe_se_resout_au_plus_long_connu():
    """Le préfixe d'un tenant ou d'un client ne change pas le type."""
    assert time_saved.resolve("acme_serper_search") == "serper_search"
    assert time_saved.kind_of("acme_serper_search") == "research"
    assert time_saved.kind_of("demo-connecteur_data_claim_next") is None


def test_un_outil_qui_ne_declare_rien_ne_rapporte_rien_meme_en_masse():
    gagne, tally = time_saved.for_agent(
        [[time_saved.Call("hubspot_object", "update", key=f"c{i}") for i in range(50)]
         + [time_saved.Call("serper_search")]], time_saved.standards(None))
    assert gagne == 0 and tally == {}


def test_archiver_ou_jeter_n_est_pas_un_envoi():
    C, m = time_saved.Call, time_saved.standards(None)
    gagne, tally = time_saved.for_agent(
        [[C("gmail_act", "archive", key="m1"), C("gmail_act", "trash", key="m2")]], m)
    assert gagne == 0 and tally == {}
    gagne, tally = time_saved.for_agent([[C("gmail_act", "send", key="m3")]], m)
    assert tally == {"action": {"count": 1, "minutes": m["action"]}}


def test_les_surcharges_d_une_org_remplacent_les_defauts_et_rien_d_autre():
    m = time_saved.standards({"record": 5, "inconnu": 99, "action": "x"})
    assert m["record"] == 5.0
    assert m["action"] == time_saved.KINDS["action"]["minutes"]
    assert "inconnu" not in m and "enrichment" not in m


# ── les surcharges d'une org : oto_org_settings domain=time_saved ─────────────

from oto_mcp.capabilities import org_console as OC  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx  # noqa: E402


@pytest.fixture
def stockage(monkeypatch):
    from oto_mcp import db
    etat = {"v": {}}
    monkeypatch.setattr(db, "minutes_gagnees_de_l_org", lambda org: dict(etat["v"]))
    monkeypatch.setattr(db, "poser_minutes_gagnees", lambda org, m: etat.update(v=m or {}) or True)
    return etat


def _reglage(**kw):
    return OC._temps_gagne(ResolvedCtx(sub="admin", org_id=7),
                           OC.OrgSettingsInput(domain="time_saved", org_id=7, **kw))


def test_get_rend_les_defauts_sans_rien_poser(stockage):
    rep = _reglage(op="get")
    assert all(not k["overridden"] for k in rep["kinds"])
    assert {k["kind"] for k in rep["kinds"]} == set(time_saved.KINDS)


def test_set_surcharge_un_type_et_null_le_rend(stockage):
    _reglage(op="set", minutes={"record": 5})
    assert stockage["v"] == {"record": 5.0}
    rep = _reglage(op="set", minutes={"record": None})
    assert stockage["v"] == {} and not any(k["overridden"] for k in rep["kinds"])


@pytest.mark.parametrize("minutes, code", [
    ({"inconnu": 3}, "unknown_kind"),
    ({"record": 999}, "minutes_out_of_range"),
    ({"record": -1}, "minutes_out_of_range"),
])
def test_set_refuse_avant_toute_ecriture(stockage, minutes, code):
    with pytest.raises(AuthzDenied) as e:
        _reglage(op="set", minutes=minutes)
    assert e.value.code == code and stockage["v"] == {}


# ── les règles revues sur des agents réels (balayages, multiplexés, rapports) ──

C = time_saved.Call
M = time_saved.standards(None)


def test_un_balayage_qui_ne_produit_rien_ne_gagne_rien():
    """Un run horaire qui regarde et ne trouve rien : sa recherche ne compte pas."""
    gagne, tally = time_saved.for_agent([[C("serper_search")] * 6, [C("serper_search")]], M)
    assert gagne == 0 and tally == {}


def test_un_run_somme_ses_resultats_et_plafonne_sa_recherche():
    gagne, tally = time_saved.for_agent(
        [[C("data_write", key=f"r{i}") for i in range(4)] + [C("serper_search")] * 30], M)
    assert {k: v["count"] for k, v in tally.items()} == {"record": 4, "research": 30}
    assert gagne == pytest.approx(4 * M["record"] + time_saved.RESEARCH_CAP_PER_RUN)


def test_un_meme_enregistrement_compte_une_fois_par_jour():
    runs = [[C("data_write", key="p1", day="2026-10-01"), C("serper_search")],
            [C("data_write", key="p1", day="2026-10-01")],     # même jour : rien de neuf
            [C("data_write", key="p1", day="2026-10-02")]]     # lendemain : compte
    gagne, tally = time_saved.for_agent(runs, M)
    assert tally["record"]["count"] == 2
    assert tally["research"]["count"] == 1      # seul le premier run a produit


def test_un_appel_refuse_ne_compte_pas():
    gagne, tally = time_saved.for_agent([[C("data_write", ok=False), C("data_write")]], M)
    assert tally["record"]["count"] == 1


def test_chercher_puis_envoyer_est_un_rapport_une_fois_par_run():
    gagne, tally = time_saved.for_agent(
        [[C("serper_search"), C("serper_search"), C("whatsapp_act", "send")]], M)
    assert tally == {"report": {"count": 1, "minutes": M["report"]}}
    assert gagne == M["report"]


def test_des_envois_sans_recherche_restent_des_envois():
    gagne, tally = time_saved.for_agent([[C("whatsapp_act", "send")] * 3], M)
    assert tally["action"]["count"] == 3 and "report" not in tally
