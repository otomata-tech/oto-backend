"""Le plafond de création d'espaces ne s'applique ni au super_admin ni à l'admin de
SON tenant — ce sont eux qui montent les espaces des clients.

Banc sans base : l'exemption est un prédicat de RÔLE (`access.is_super_admin`,
`db.is_tenant_admin`), pas un prédicat SQL — le compte lui-même est exercé contre un
PostgreSQL réel par `test_org_quota_archivage.py`. On stubbe donc le compte (au
plafond) et les deux lectures de rôle, et on vérifie la création ET la liste, qui
lisent le même `org_quota`.
"""
from __future__ import annotations

import pytest

from oto_mcp import tenancy
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.orgs import core as orgs

PLAFOND = 3
SUB = "sub-test-1"


class _Registre:
    """Le tenant se lit sur le PRÉFIXE du sub — même contrat que le registre servi."""
    def tenant_of(self, sub: str) -> str:
        return sub.split(":", 1)[0] if ":" in sub else tenancy.PRIMARY_SLUG


@pytest.fixture()
def banc(monkeypatch):
    """Compte au plafond ; aucun rôle par défaut. Retourne les deux leviers de rôle."""
    roles = {"super": set(), "tenant_admins": set()}
    monkeypatch.setattr(orgs, "_MAX_ORGS_PER_USER", PLAFOND)
    monkeypatch.setattr(orgs.org_store, "count_orgs_created_by", lambda sub: PLAFOND)
    monkeypatch.setattr(orgs.access, "is_super_admin", lambda sub: sub in roles["super"])
    monkeypatch.setattr(orgs.db, "is_tenant_admin",
                        lambda slug, sub: (slug, sub) in roles["tenant_admins"])
    monkeypatch.setattr(orgs.tenancy, "current", lambda: _Registre())
    monkeypatch.setattr(orgs.config, "front_for", lambda sub: (None, None))
    monkeypatch.setattr(orgs.org_store, "create_org", lambda name, **kw: 42)
    monkeypatch.setattr(orgs.org_store, "add_org_member", lambda *a, **k: None)
    monkeypatch.setattr(orgs.org_store, "set_active_org", lambda *a: None)
    return roles


def _creer(sub: str) -> dict:
    return orgs._create_org(ResolvedCtx(sub=sub), orgs.CreateOrgInput(name="Client"))


def test_un_compte_ordinaire_au_plafond_est_refuse(banc):
    with pytest.raises(AuthzDenied) as refus:
        _creer(SUB)
    assert refus.value.status == 429 and refus.value.code == "org_quota"
    assert orgs.org_quota(SUB) == {"created": PLAFOND, "cap": PLAFOND, "remaining": 0}


def test_le_super_admin_cree_au_dela_du_plafond(banc):
    banc["super"].add(SUB)
    assert _creer(SUB)["org_id"] == 42
    assert orgs.org_quota(SUB) == {"created": PLAFOND, "cap": orgs.PLAFOND_LEVE,
                                   "remaining": orgs.PLAFOND_LEVE - PLAFOND}


def test_l_admin_de_son_tenant_cree_au_dela_du_plafond(banc):
    sub = "pilote:abc"
    banc["tenant_admins"].add(("pilote", sub))
    assert _creer(sub)["org_id"] == 42
    assert orgs.org_quota(sub)["cap"] == orgs.PLAFOND_LEVE


def test_admin_d_un_AUTRE_tenant_reste_plafonne(banc):
    """Le tenant vient du préfixe du sub, jamais d'une ligne `tenant_admins` qui
    désignerait un autre slug : même invariant que `_authz.TENANT_ADMIN_OF`."""
    sub = "pilote:abc"
    banc["tenant_admins"].add(("autre", sub))
    with pytest.raises(AuthzDenied) as refus:
        _creer(sub)
    assert refus.value.code == "org_quota"


def test_un_compte_hors_plafond_recoit_des_entiers():
    """Contrat de `GET /api/me/orgs` : `cap`/`remaining` sont des ENTIERS REQUIS que des
    fronts épinglent — jamais `null`, même hors plafond (décision du 25/09/2026)."""
    from oto_mcp.capabilities.orgs.reads import OrgQuota
    champs = OrgQuota.model_json_schema()
    assert {"cap", "remaining"} <= set(champs["required"])
    assert champs["properties"]["cap"]["type"] == "integer"
    assert champs["properties"]["remaining"]["type"] == "integer"
    with pytest.raises(Exception):
        OrgQuota(created=12, cap=None, remaining=None)
