"""Lire UN projet sur une URL qui le nomme (`GET /api/me/projects/{id}`).

Pourquoi cette route existe alors que `POST /api/me/projects {"op":"get"}` rend
déjà le même payload : parce que la portée d'un jeton (`token_scopes`) se décide
sur la méthode et le CHEMIN. La forme POST porte sa cible dans le corps — on ne
saurait pas la borner. Confier un projet (et lui seul) à une intégration demande
donc que le projet s'adresse, comme un tableau se nomme déjà dans son URL.

Grain unitaire, style monkeypatch maison : aucune DB.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

# `_view` lit les partages vivants du projet (#1367) : aucun ici, sans base.
pytestmark = pytest.mark.usefixtures("projets_sans_partages")

CTX = ResolvedCtx(sub="oto", org_id=1)

ROW = {"id": 12, "name": "Vivier accords dormants", "icon": "📇",
       "brief_md": "Appeler les entreprises dont l'accord dort.",
       "owner_type": "org", "owner_id": "1"}

LINKS = [
    {"target_type": "tableau", "target_ref": "11", "label": "Vivier", "slot": "vivier"},
    {"target_type": "tableau", "target_ref": "12", "label": "Sorties", "slot": "sorties"},
    {"target_type": "procedure", "target_ref": "77", "label": "Script d'appel"},
]


@pytest.fixture
def wired(monkeypatch):
    """Projet #12 lisible dans l'org active, lié aux entités ci-dessus."""
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW) if pid == 12 else None)
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: list(LINKS))
    monkeypatch.setattr(P.ownership, "visible_in_org", lambda sub, org, rt, rid: True)
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, rt, rid, perm: True)
    from oto_mcp import project_audit
    monkeypatch.setattr(project_audit, "audit_project", lambda pid, links, **_k: {})


# ── Le descripteur : une URL qui nomme sa cible, et pas de doublon MCP ────────

def _cap():
    return next(c for c in CAPABILITIES if c.key == "me.project_read")


def test_route_names_its_target_by_id():
    [binding] = _cap().rest_bindings()
    assert (binding.verb, binding.path) == ("GET", "/api/me/projects/{project_id:int}")


def test_no_second_mcp_surface():
    """Les agents ont déjà `oto_project op=get` : en ajouter un doublon MCP ne
    ferait qu'un choix de plus à faire pour le même geste."""
    assert _cap().mcp is None


def test_the_post_form_still_carries_the_whole_capability():
    """La route par id ne remplace rien : elle s'ajoute."""
    post = next(c for c in CAPABILITIES if c.key == "me.project")
    assert [(b.verb, b.path) for b in post.rest_bindings()] == [("POST", "/api/me/projects")]


# ── Le contenu : le brief et les liens, tels que `op=get` les rend ────────────

def test_read_returns_the_brief_and_the_links(wired):
    out = P._project_read(CTX, P.ProjectReadInput(project_id=12))
    assert out["name"] == "Vivier accords dormants"
    assert out["brief_md"] == "Appeler les entreprises dont l'accord dort."
    assert out["links"] == LINKS


def test_read_carries_the_linked_tables(wired):
    """Ce que scout vient chercher : de quels tableaux ce projet est fait."""
    tables = [l for l in P._project_read(CTX, P.ProjectReadInput(project_id=12))["links"]
              if l["target_type"] == "tableau"]
    assert [t["slot"] for t in tables] == ["vivier", "sorties"]


def test_read_accepts_the_spine(wired, monkeypatch):
    monkeypatch.setattr(P.db, "project_spine", lambda pid, from_doc, depth: [{"title": "Brief"}])
    out = P._project_read(CTX, P.ProjectReadInput(project_id=12, include=["spine"]))
    assert out["spine"] == [{"title": "Brief"}]


# ── Les refus : ceux de `op=get`, sans redite ─────────────────────────────────

def test_unknown_project_is_refused(wired):
    with pytest.raises(AuthzDenied) as e:
        P._project_read(CTX, P.ProjectReadInput(project_id=99))
    assert e.value.status == 404


def test_project_of_another_org_is_refused(wired, monkeypatch):
    """Le gate de contexte d'org vaut pour cette route comme pour l'autre — c'est
    le même handler, il ne peut pas en diverger."""
    monkeypatch.setattr(P.ownership, "visible_in_org", lambda sub, org, rt, rid: False)
    monkeypatch.setattr(P.ownership, "owner_of", lambda rt, rid: ("org", "2"))
    monkeypatch.setattr(P.org_store, "get_org", lambda oid: {"name": "une autre"})
    with pytest.raises(AuthzDenied) as e:
        P._project_read(CTX, P.ProjectReadInput(project_id=12))
    assert e.value.status == 403
