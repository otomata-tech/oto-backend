"""L'option `excluded_url_prefixes` d'un projet (#605) — posée par `oto_project op=update`,
sans republication, normalisée à la pose, affichée dans la fiche.

Handler sync ; on monkeypatche db/ownership (les seams), pas de DB — même patron que
`tests/test_projects.py`. Ce que ce fichier fige : la pose normalise (une forme
canonique stockée, pas la saisie), `[]` retire, un motif trop large est REFUSÉ à la pose
en disant la forme explicite, et `get`/`list` rendent l'option.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

# `_view` lit les partages vivants du projet (#1367) : aucun ici, sans base.
pytestmark = pytest.mark.usefixtures("projets_sans_partages")

CTX = ResolvedCtx(sub="u1", org_id=99)
ROW = {"id": 7, "owner_type": "org", "owner_id": "99", "name": "Campagne", "brief_md": "b",
       "created_by": "u1", "archived_at": None, "created_at": "2026-08-29",
       "updated_at": "2026-08-29", "excluded_url_prefixes": []}


@pytest.fixture
def seams(monkeypatch):
    store = {"row": dict(ROW), "set": []}
    monkeypatch.setattr(P.db, "get_project_by_id",
                        lambda pid: dict(store["row"], id=pid) if pid == 7 else None)
    monkeypatch.setattr(P.db, "update_project", lambda pid, **kw: None)
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [])

    def _set(pid, prefixes):
        store["set"].append((pid, list(prefixes)))
        store["row"]["excluded_url_prefixes"] = list(prefixes)
    monkeypatch.setattr(P.db, "set_project_excluded_url_prefixes", _set)
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.ownership, "can_govern", lambda sub, t, rid: True)
    monkeypatch.setattr(P.ownership, "accessor_scope", lambda sub, rt, rid: True)
    monkeypatch.setattr("oto_mcp.project_audit.audit_project",
                        lambda pid, links=None, **_k: {"dead_links": [],
                                                                 "unbound_slots": [],
                                                                 "inert_procedures": []})
    return store


def test_update_stores_the_canonical_form_and_shows_it(seams):
    out = P._project(CTX, P.ProjectInput(
        op="update", project_id=7,
        excluded_url_prefixes=["https://www.LinkedIn.com/in", "linkedin.com/in/",
                               "viadeo.com/*"]))
    assert seams["set"] == [(7, ["linkedin.com/in/", "viadeo.com/*"])]
    assert out["excluded_url_prefixes"] == ["linkedin.com/in/", "viadeo.com/*"]
    # pas de republication : rien de la publication n'a bougé
    assert out["mcp_access"] == "off"


def test_update_without_the_field_leaves_it_alone(seams):
    seams["row"]["excluded_url_prefixes"] = ["linkedin.com/in/"]
    out = P._project(CTX, P.ProjectInput(op="update", project_id=7, name="Renommé"))
    assert seams["set"] == []
    assert out["excluded_url_prefixes"] == ["linkedin.com/in/"]


def test_empty_list_clears_the_option(seams):
    seams["row"]["excluded_url_prefixes"] = ["linkedin.com/in/"]
    out = P._project(CTX, P.ProjectInput(op="update", project_id=7,
                                         excluded_url_prefixes=[]))
    assert seams["set"] == [(7, [])] and out["excluded_url_prefixes"] == []


@pytest.mark.parametrize("bad,code", [
    (["linkedin.com"], "bare_host"),
    (["linkedin.com/in/", "viadeo.com/"], "bare_host"),
    (["linkedin.com/in/*"], "wildcard"),
    (["/in/"], "bad_host"),
])
def test_a_too_broad_prefix_is_refused_at_pose_and_nothing_is_stored(seams, bad, code):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="update", project_id=7, excluded_url_prefixes=bad))
    assert e.value.code == "invalid_url_prefix"
    assert code == "bare_host" and "/*" in e.value.message or code != "bare_host"
    assert seams["set"] == []


def test_update_needs_write_access(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied):
        P._project(CTX, P.ProjectInput(op="update", project_id=7,
                                       excluded_url_prefixes=["linkedin.com/in/"]))
    assert seams["set"] == []


def test_get_and_view_carry_the_option(seams):
    seams["row"]["excluded_url_prefixes"] = ["linkedin.com/in/"]
    got = P._project(CTX, P.ProjectInput(op="get", project_id=7))
    assert got["excluded_url_prefixes"] == ["linkedin.com/in/"]
    # une ligne legacy sans la clé : liste vide, jamais None
    assert P._view({**ROW, "excluded_url_prefixes": None})["excluded_url_prefixes"] == []
    assert P.ProjectRead.model_fields["excluded_url_prefixes"].default == []


def test_the_tool_description_says_the_option_in_one_sentence():
    cap = next(c for c in P.CAPABILITIES if c.mcp == "oto_project")
    assert "excluded_url_prefixes" in cap.description
