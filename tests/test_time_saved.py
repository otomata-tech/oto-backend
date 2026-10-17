"""Le temps gagné des agents — estimé depuis ce qu'ils ont FAIT (`time_saved.py`).

Ce que ce banc verrouille : le type de résultat se déduit de ce que l'outil DÉCLARE
(lecture, acte chez un tiers, écriture), jamais d'une table à la main ; la tenue de la
plateforme ne compte pas ; un outil inconnu ne compte pas ; un nom préfixé se résout ;
la recherche est plafonnée par run ; une org surcharge les minutes, jamais l'inverse.
"""
from __future__ import annotations

import pytest

from oto_mcp import time_saved, tool_registry


@pytest.fixture(autouse=True)
def _registre(monkeypatch):
    monkeypatch.setattr(tool_registry, "_EFFETS", {
        "folk_update": "write",
        "dropcontact_enrich": "read",
        "serper_search": "read",
        "email_send": "act",
        "lemlist_act": "act",
        "data_write": "write",
        "data_rows": "read",
        "data_claim_next": "write",
        "oto_doc": "write",
        "run_start": "write",
        "get": "read",
    })


@pytest.mark.parametrize("nom, attendu", [
    ("folk_update", "record"),
    ("dropcontact_enrich", "enrichment"),   # lit chez le fournisseur, mais enrichit
    ("serper_search", "research"),
    ("email_send", "action"),
    ("lemlist_act", "action"),
    ("data_write", "record"),
    ("data_rows", None),                     # la tenue du tableau de l'agent
    ("data_claim_next", None),
    ("oto_doc", None),                       # la plateforme
    ("run_start", None),
    ("inconnu_partout", None),               # on ne devine pas
])
def test_le_type_vient_de_ce_que_l_outil_declare(nom, attendu):
    assert time_saved.kind_of(nom) == attendu


def test_un_nom_prefixe_se_resout_au_plus_long_connu():
    """Le préfixe d'un tenant ou d'un client ne change pas le type ; le suffixe court
    (`get`) ne gagne jamais sur le vrai nom."""
    assert time_saved.resolve("acme_folk_update") == "folk_update"
    assert time_saved.kind_of("demo-connecteur_data_claim_next") is None
    assert time_saved.resolve("x_folk_get") == "get"


def test_un_run_somme_ses_resultats_et_plafonne_sa_recherche():
    m = time_saved.standards(None)
    gagne, comptes = time_saved.for_run(
        {"folk_update": 4, "dropcontact_enrich": 2, "serper_search": 30, "oto_doc": 9, "x": "?"}, m)
    assert comptes == {"record": 4, "enrichment": 2, "research": 30}
    # 4 × 1.5 + 2 × 3 + recherche plafonnée à 10 (pas 60)
    assert gagne == pytest.approx(6 + 6 + time_saved.RESEARCH_CAP_PER_RUN)


def test_les_surcharges_d_une_org_remplacent_les_defauts_et_rien_d_autre():
    m = time_saved.standards({"enrichment": 5, "inconnu": 99, "record": "x"})
    assert m["enrichment"] == 5.0
    assert m["record"] == time_saved.KINDS["record"]["minutes"]
    assert "inconnu" not in m


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
    _reglage(op="set", minutes={"enrichment": 5})
    assert stockage["v"] == {"enrichment": 5.0}
    rep = _reglage(op="set", minutes={"enrichment": None})
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


def test_une_op_qui_lit_sur_un_outil_ecrivain_est_une_recherche():
    """`folk_update op=get` lit : un outil multiplexé ne compte que ses écritures."""
    assert time_saved.kind_of("folk_update", "get") == "research"
    assert time_saved.kind_of("folk_update", "update") == "record"


def test_un_meme_enregistrement_compte_une_fois_par_jour():
    runs = [[C("folk_update", "update", key="p1", day="2026-10-01"), C("serper_search")],
            [C("folk_update", "update", key="p1", day="2026-10-01")],     # même jour : rien de neuf
            [C("folk_update", "update", key="p1", day="2026-10-02")]]     # lendemain : compte
    gagne, tally = time_saved.for_agent(runs, M)
    assert tally["record"]["count"] == 2
    assert tally["research"]["count"] == 1      # seul le premier run a produit


def test_un_appel_refuse_ne_compte_pas():
    gagne, tally = time_saved.for_agent([[C("folk_update", ok=False), C("folk_update")]], M)
    assert tally["record"]["count"] == 1


def test_chercher_puis_poster_est_un_rapport_une_fois_par_run():
    gagne, tally = time_saved.for_agent(
        [[C("serper_search"), C("serper_search"), C("email_send")]], M)
    assert tally == {"report": {"count": 1, "minutes": M["report"]}}
    assert gagne == M["report"]


def test_des_envois_sans_recherche_restent_des_envois():
    gagne, tally = time_saved.for_agent([[C("email_send")] * 3], M)
    assert tally["action"]["count"] == 3 and "report" not in tally
