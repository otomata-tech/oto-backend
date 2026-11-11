"""Les rôles PLATEFORME ne passent pas un jeton de délégation (`verrou_org.role_plateforme`).

Le verrou d'org bornait le rôle d'ORG du porteur ; `super_admin`/`admin` restaient les
siens sous le jeton d'un travail du runner, et avec eux toute garde plateforme. Un seul
point les borne, `access.scope.role_effectif`, d'où dérivent `get_user_role`,
`is_super_admin`, `is_platform_operator`, `roles.is_platform_admin` et
`is_operator_role` — les règles `PLATFORM_ADMIN`/`SUPER_ADMIN` le lisent.

`OTO_VERROU_ROLES_PLATEFORME` : `enforce` (défaut) les retire, `report` (posé
explicitement) le journalise sans rien retirer, `off` le coupe. Aucun accès base : `db.get_user` est remplacé.
"""
from __future__ import annotations

import logging

import pytest

from oto_mcp import access, roles, verrou_org
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities._types import AuthzDenied, RawCtx

PORTEUR = "sub-roles-porteur"
TIERS = "sub-roles-tiers"
ENV = "OTO_VERROU_ROLES_PLATEFORME"


@pytest.fixture
def comptes(monkeypatch):
    rangs = {PORTEUR: "super_admin", TIERS: "admin"}
    # La FAÇADE, jamais un sous-module (`tests/test_facades_lecteurs_cible.py`).
    monkeypatch.setattr(access.db, "get_user",
                        lambda sub: {"sub": sub, "role": rangs.get(sub, "member")})
    # Les règles lisent aussi l'org de l'appel : hors sujet ici.
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.delenv("OTO_MCP_ADMIN_SUB", raising=False)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("OTO_VERROU_ORG_DELEGATION", raising=False)


@pytest.fixture
def delegue(comptes):
    """Le porteur sous le jeton d'un travail de l'org 42 — posé comme le bord le pose."""
    t = verrou_org.poser(verrou_org.Verrou(sub=PORTEUR, org_id=42, job_id=7))
    yield
    verrou_org.lever(t)


def _raw(sub):
    return RawCtx(sub=sub)


def test_enforce_ramene_le_porteur_a_member(delegue, monkeypatch):
    monkeypatch.setenv(ENV, "enforce")
    assert access.get_user_role(PORTEUR) == "member"
    assert not access.is_super_admin(PORTEUR)
    assert not access.is_platform_operator(PORTEUR)
    assert not roles.is_platform_admin(PORTEUR)
    assert not access.is_operator_role(PORTEUR, "super_admin")
    with pytest.raises(AuthzDenied):
        _authz.SUPER_ADMIN(_raw(PORTEUR))
    with pytest.raises(AuthzDenied):
        _authz.PLATFORM_ADMIN(_raw(PORTEUR))


def test_enforce_ne_touche_pas_un_tiers(delegue, monkeypatch):
    """Lister les opérateurs d'une org, lire le rôle d'un autre compte : exact."""
    monkeypatch.setenv(ENV, "enforce")
    assert access.get_user_role(TIERS) == "admin"
    assert access.is_platform_operator(TIERS)
    assert access.is_operator_role(TIERS, "admin")


def test_enforce_borne_aussi_le_sub_d_amorcage(delegue, monkeypatch):
    """`OTO_MCP_ADMIN_SUB` force super_admin AVANT la borne : il ne lui échappe pas."""
    monkeypatch.setenv(ENV, "enforce")
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", PORTEUR)
    assert access.get_user_role(PORTEUR) == "member"
    assert access.role_effectif(PORTEUR, None) == "member"
    assert not access.is_operator_role(PORTEUR, None)


def test_enforce_par_defaut(delegue):
    """Variable absente ou vide : le mode sûr, sans rien poser."""
    assert verrou_org.mode_roles() == "enforce"
    assert access.get_user_role(PORTEUR) == "member"
    assert not access.is_super_admin(PORTEUR)
    with pytest.raises(AuthzDenied):
        _authz.SUPER_ADMIN(_raw(PORTEUR))


def test_vide_vaut_enforce(delegue, monkeypatch):
    monkeypatch.setenv(ENV, "  ")
    assert verrou_org.mode_roles() == "enforce"
    assert not access.is_platform_operator(PORTEUR)


def test_report_pose_laisse_passer_et_le_dit_une_fois(delegue, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "report")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        assert access.is_super_admin(PORTEUR)
        assert access.is_platform_operator(PORTEUR)
        assert roles.is_platform_admin(PORTEUR)
        assert _authz.SUPER_ADMIN(_raw(PORTEUR)).sub == PORTEUR
    lignes = [r.getMessage() for r in caplog.records
              if "rôles plateforme (report)" in r.getMessage()]
    assert len(lignes) == 1, lignes   # une ligne par requête, pas une par prédicat
    assert "travail 7" in lignes[0] and "super_admin" in lignes[0]


def test_une_valeur_inconnue_vaut_enforce(delegue, monkeypatch):
    monkeypatch.setenv(ENV, "strict")
    assert verrou_org.mode_roles() == "enforce"
    assert not access.is_super_admin(PORTEUR)


def test_off_coupe(delegue, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "off")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        assert access.is_super_admin(PORTEUR)
    assert not [r for r in caplog.records if "rôles plateforme" in r.getMessage()]


def test_le_verrou_d_org_coupe_ne_rend_pas_les_roles(delegue, monkeypatch):
    """Deux réglages, deux verrous : `OTO_VERROU_ORG_DELEGATION=off` ne rend pas au
    porteur ses rôles plateforme."""
    monkeypatch.setenv("OTO_VERROU_ORG_DELEGATION", "off")
    monkeypatch.setenv(ENV, "enforce")
    assert not access.is_super_admin(PORTEUR)


def test_sans_jeton_de_delegation_rien_ne_change(comptes, monkeypatch):
    """Un humain (OAuth) ou un jeton d'API ordinaire : aucun verrou posé."""
    monkeypatch.setenv(ENV, "enforce")
    t = verrou_org.poser(None)
    try:
        assert access.get_user_role(PORTEUR) == "super_admin"
        assert _authz.SUPER_ADMIN(_raw(PORTEUR)).sub == PORTEUR
    finally:
        verrou_org.lever(t)


def test_un_member_reste_member(delegue, monkeypatch):
    monkeypatch.setenv(ENV, "enforce")
    assert access.get_user_role("sub-quelconque") == "member"


# ── Les outils d'admin PERMIS à un agent (revue de #1274) ─────────────────────────────

PERMIS = ["oto_admin_org", "oto_admin_signal"]


@pytest.fixture
def liste_de_l_agent(monkeypatch):
    """La liste d'outils d'admin de l'agent du travail, telle que la base la rend
    (doublure, prouvée par `test_partage_agents_db::test_les_outils_plateforme_ne_valent_
    que_pour_l_agent_au_seul_proprietaire`). `[]` = agent qu'un autre peut changer."""
    etat = {"liste": list(PERMIS), "lus": []}

    def lire(job_id):
        etat["lus"].append(job_id)
        return list(etat["liste"])
    monkeypatch.setattr(access.db, "outils_plateforme_du_travail", lire)
    return etat


def _pendant(name, f):
    jeton = verrou_org.poser_outil(name)
    try:
        return f()
    finally:
        verrou_org.lever_outil(jeton)


def test_un_outil_permis_garde_le_role_et_laisse_une_trace(delegue, liste_de_l_agent,
                                                           monkeypatch, caplog):
    """Le tri des retours exerce `oto_admin_org` avec le rôle de son porteur — tracé."""
    monkeypatch.setenv(ENV, "enforce")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        assert _pendant("oto_admin_org", lambda: access.get_user_role(PORTEUR)) \
            == "super_admin"
        assert _pendant("oto_admin_org",
                        lambda: _authz.SUPER_ADMIN(_raw(PORTEUR)).sub) == PORTEUR
    traces = [r.getMessage() for r in caplog.records
              if "permis par la liste de son agent" in r.getMessage()]
    assert len(traces) == 1, "une ligne par outil et par requête, pas une par prédicat"
    assert "`oto_admin_org`" in traces[0] and "travail 7" in traces[0]
    assert liste_de_l_agent["lus"] == [7], "la liste se lit une fois par requête"


def test_platform_tools_sert_sous_le_defaut(delegue, liste_de_l_agent):
    """Sans variable posée (`enforce` par défaut), l'agent d'un opérateur garde les
    `oto_admin_*` que sa liste nomme, et seulement eux."""
    assert _pendant("oto_admin_org", lambda: access.get_user_role(PORTEUR)) == "super_admin"
    assert _pendant("oto_admin_user", lambda: access.get_user_role(PORTEUR)) == "member"
    assert access.get_user_role(PORTEUR) == "member"


def test_la_trace_reste_en_report(delegue, liste_de_l_agent, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "report")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        assert _pendant("oto_admin_signal", lambda: access.is_super_admin(PORTEUR))
    assert any("`oto_admin_signal`" in r.getMessage()
               and "permis par la liste" in r.getMessage() for r in caplog.records)


def test_un_outil_d_admin_hors_liste_est_lu_member(delegue, liste_de_l_agent,
                                                   monkeypatch, caplog):
    """Une injection de prompt n'atteint pas l'admin que l'agent ne nomme pas."""
    monkeypatch.setenv(ENV, "enforce")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        assert _pendant("oto_admin_user", lambda: access.get_user_role(PORTEUR)) == "member"
        with pytest.raises(AuthzDenied):
            _pendant("oto_admin_user", lambda: _authz.SUPER_ADMIN(_raw(PORTEUR)))
    assert any("`oto_admin_user`" in r.getMessage() and "hors de la liste" in r.getMessage()
               for r in caplog.records), "le refus aussi se trace"


def test_hors_appel_d_un_outil_permis_le_travail_reste_member(delegue, liste_de_l_agent,
                                                              monkeypatch):
    """Un autre outil du même travail (`oto_call` lui-même, un outil métier) et la face
    REST (aucun outil posé) : rien n'est permis."""
    monkeypatch.setenv(ENV, "enforce")
    assert access.get_user_role(PORTEUR) == "member"
    assert _pendant("oto_call", lambda: access.get_user_role(PORTEUR)) == "member"
    assert _pendant("fr_siret", lambda: access.is_platform_operator(PORTEUR)) is False
    assert liste_de_l_agent["lus"] == [], "un appel ordinaire ne lit pas la liste en base"


def test_la_cible_d_oto_call_repose_l_outil(delegue, liste_de_l_agent, monkeypatch):
    """`meta.executer_cible` pose la CIBLE par-dessus `oto_call` : c'est elle qui juge."""
    monkeypatch.setenv(ENV, "enforce")
    dehors = verrou_org.poser_outil("oto_call")
    try:
        assert _pendant("oto_admin_org",
                        lambda: access.get_user_role(PORTEUR)) == "super_admin"
        assert access.get_user_role(PORTEUR) == "member", "rétabli au retour de la cible"
    finally:
        verrou_org.lever_outil(dehors)


def test_un_agent_qu_un_autre_peut_changer_n_a_rien(delegue, liste_de_l_agent, monkeypatch):
    """Un agent tourne sous son propriétaire quel que soit celui qui l'a modifié : la
    liste d'un agent prêté ou sur clé d'org prêterait le rôle plateforme à son éditeur —
    la base la rend vide."""
    monkeypatch.setenv(ENV, "enforce")
    liste_de_l_agent["liste"] = []
    assert _pendant("oto_admin_org", lambda: access.get_user_role(PORTEUR)) == "member"


def test_seuls_les_oto_admin_comptent(delegue, liste_de_l_agent, monkeypatch):
    """Un autre nom stocké n'ouvre rien : la liste ne prête le rôle qu'à l'admin."""
    monkeypatch.setenv(ENV, "enforce")
    liste_de_l_agent["liste"] = ["oto_trigger", "oto_admin_org"]
    assert verrou_org.outils_permis() == frozenset({"oto_admin_org"})
    assert _pendant("oto_trigger", lambda: access.get_user_role(PORTEUR)) == "member"


def test_un_travail_sans_numero_n_a_rien(comptes, liste_de_l_agent, monkeypatch):
    monkeypatch.setenv(ENV, "enforce")
    t = verrou_org.poser(verrou_org.Verrou(sub=PORTEUR, org_id=42, job_id=None))
    try:
        assert _pendant("oto_admin_org", lambda: access.get_user_role(PORTEUR)) == "member"
    finally:
        verrou_org.lever(t)
    assert liste_de_l_agent["lus"] == []


def test_la_lecture_pour_la_liste_ne_trace_rien(delegue, liste_de_l_agent, monkeypatch,
                                                caplog):
    """`tools/list` juge un outil permis au rôle qu'il aura à l'appel, sans l'exercer."""
    from oto_mcp import session_visibility
    monkeypatch.setenv(ENV, "enforce")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.verrou_org"):
        roles_ = session_visibility._roles_des_outils_permis(PORTEUR)
    assert roles_ == {"oto_admin_org": "super_admin", "oto_admin_signal": "super_admin"}
    assert not [r for r in caplog.records if "exerce" in r.getMessage()]
    # `oto_admin_signal` : un plancher plateforme réel (sinon le test ne prouverait rien).
    noms = {"oto_admin_signal", "oto_admin_user"}
    assert session_visibility._hors_de_portee_plateforme(noms, "member") == noms, (
        "témoin : sans la liste, les deux sont masqués")
    masques = session_visibility._hors_de_portee_plateforme(noms, "member", roles_)
    assert masques == {"oto_admin_user"}, (
        "l'outil permis reste visible (un outil masqué est injoignable), l'autre non")
