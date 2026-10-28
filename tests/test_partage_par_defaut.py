"""Le partage par défaut d'une org (`capabilities/orgs/partage_par_defaut.py`).

Privé par défaut, toujours : seul ce réglage, posé par un admin de l'org, ouvre d'office
un objet neuf — `{agent: aucun|lecture}`. `ecriture` est refusé pour un agent (un run
planifié tourne sous son propriétaire), une valeur stockée illisible lève, et un membre
qui n'est pas admin ne le pose pas. La naissance d'un agent sous ce réglage :
`test_partage_agents.py` ; en base : `test_agents_org_default_db.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access, detenteurs, roles
from oto_mcp.capabilities import org_console as OC
from oto_mcp.capabilities.orgs import partage_par_defaut as P
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

ORG = 7


@pytest.fixture
def stockage(monkeypatch):
    """La colonne `orgs.default_sharing` en mémoire : `{"v": stocké}`."""
    etat = {"v": None, "ecrit": 0}

    def _poser(org, valeur):
        etat["v"], etat["ecrit"] = valeur, etat["ecrit"] + 1
        return org == ORG
    monkeypatch.setattr(P.db, "partage_par_defaut_stocke",
                        lambda org: etat["v"] if org == ORG else None)
    monkeypatch.setattr(P.db, "poser_partage_par_defaut", _poser)
    return etat


def _console(op, **kw):
    return OC._org_settings(ResolvedCtx(sub="admin", org_id=ORG), OC.OrgSettingsInput(
        op=op, domain="partage_par_defaut", org_id=ORG, **kw))


def test_sans_reglage_tout_type_vaut_aucun(stockage):
    assert _console("get") == {"org_id": ORG,
                               "partage_par_defaut": {"agent": "aucun", "app": "aucun"},
                               "values": {"agent": ["aucun", "lecture"],
                                          "app": ["aucun", "lecture", "ecriture"]}}
    assert P.role_a_la_naissance(ORG, "agent") is None
    assert P.role_a_la_naissance(ORG, "app") is None


def test_lecture_se_pose_et_aucun_le_retire(stockage):
    assert _console("set", partage={"agent": "lecture"})["partage_par_defaut"] == {
        "agent": "lecture", "app": "aucun"}
    assert stockage["v"] == {"agent": "lecture"}
    assert P.role_a_la_naissance(ORG, "agent") == "viewer"
    _console("set", partage={"agent": "aucun"})
    assert stockage["v"] is None, "`aucun` partout = rien de stocké, comme avant"


def test_une_app_peut_naitre_partagee_en_ecriture(stockage):
    """Une app n'a pas d'identité à elle : chaque geste agit au nom de son lecteur,
    borné par son auteur — l'écriture d'office n'y prête l'identité de personne."""
    _console("set", partage={"app": "ecriture"})
    assert stockage["v"] == {"app": "ecriture"}
    assert P.role_a_la_naissance(ORG, "app") == "editor"
    assert P.role_a_la_naissance(ORG, "agent") is None


@pytest.mark.parametrize("partage,code", [
    ({"agent": "ecriture"}, "default_sharing_write_unsupported"),
    ({"agent": "tous"}, "invalid_default_sharing"),
    ({"tableau": "lecture"}, "unknown_type"),
    ({}, "missing_default_sharing"),
    (None, "missing_default_sharing"),
])
def test_set_refuse_avant_toute_ecriture(stockage, partage, code):
    with pytest.raises(AuthzDenied) as e:
        _console("set", partage=partage)
    assert (e.value.status, e.value.code) == (400, code)
    assert stockage["ecrit"] == 0, "rien n'est écrit sur un refus"


@pytest.mark.parametrize("stocke", [{"agent": "ecriture"}, {"agent": "tous"},
                                    {"tableau": "lecture"}, ["agent"], "lecture", 3])
def test_une_valeur_stockee_illisible_leve_sans_repli(stockage, stocke):
    stockage["v"] = stocke
    for geste in (lambda: _console("get"), lambda: P.role_a_la_naissance(ORG, "agent")):
        with pytest.raises(AuthzDenied) as e:
            geste()
        assert (e.value.status, e.value.code) == (409, "default_sharing_unreadable")


# ── qui le pose : l'admin de l'org, et lui seul ──────────────────────────────

@pytest.fixture
def membre_seul(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == ORG)
    # Le refus nomme les admins de l'org : ce banc n'a pas de base.
    monkeypatch.setattr(detenteurs, "admins_de_l_org", lambda sub, org: [])


def _autz(sub, op):
    cap = next(c for c in OC.CAPABILITIES if c.key == "org.settings.console")
    return cap.authz(RawCtx(sub=sub), SimpleNamespace(
        op=op, domain="partage_par_defaut", org_id=ORG))


def test_un_membre_lit_le_reglage_mais_ne_le_pose_pas(membre_seul):
    assert _autz("membre", "get").org_id == ORG
    with pytest.raises(AuthzDenied) as e:
        _autz("membre", "set")
    assert e.value.status == 403
    assert _autz("admin", "set").org_id == ORG
