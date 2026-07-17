"""Tripwire d'étanchéité de la recherche (lot 3 Ship 1 — CRITÈRE DE MERGE).

« Cherchable ⇔ lisible » : chaque source de `search.search()` doit recevoir SON
prédicat d'accès, et le scope projets doit être en PARITÉ STRICTE avec
`oto_project op=list` (même owners du contexte, mêmes principals de grants) —
jamais `can_access` (cross-org par construction). Pattern
`test_owner_scope_tripwire.py` : on fige les ARGUMENTS passés aux seams.
"""
import pytest

from oto_mcp import ownership, search as S
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=7)


@pytest.fixture
def calls(monkeypatch):
    rec = {"owners": [], "principals": [], "granted_want": []}

    monkeypatch.setattr(ownership.roles, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(ownership.group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 3}])
    monkeypatch.setattr(ownership.group_store, "list_groups", lambda org: [])
    monkeypatch.setattr(ownership.db, "list_projects_for_owners",
                        lambda owners, **k: rec["owners"].append(list(owners)) or
                        [{"id": 11}, {"id": 12}])
    monkeypatch.setattr(ownership.db, "list_projects_granted_to",
                        lambda principals: rec["principals"].append(list(principals)) or
                        [{"id": 20, "permission": "read"},
                         {"id": 21, "permission": "write"}])
    return rec


def test_accessible_ids_read_and_write(calls):
    ids = ownership.accessible_project_ids("u1", 7, want="read")
    assert ids == [11, 12, 20, 21]
    # write : seuls les grants WRITE s'ajoutent aux owned
    ids_w = ownership.accessible_project_ids("u1", 7, want="write")
    assert ids_w == [11, 12, 21]


def test_scope_parity_with_op_list(calls):
    """PARITÉ : les owners du contexte et les principals de grants utilisés par la
    recherche sont EXACTEMENT ceux d'`oto_project op=list` (le drift de l'un ferait
    mentir « cherchable ⇔ lisible »)."""
    ownership.accessible_project_ids("u1", 7)
    search_owners, search_principals = calls["owners"][-1], calls["principals"][-1]

    # côté op=list : mêmes seams (project_scope_owners + active_org_principals)
    assert search_owners == ownership.project_scope_owners("u1", 7)
    assert search_principals == ownership.active_org_principals("u1", 7)
    # owners = org active + mes groupes ; principals = org + moi + mes groupes
    assert search_owners == [("org", "7"), ("group", "3")]
    assert search_principals == [("org", "7"), ("user", "u1"), ("group", "3")]


def test_org_admin_sees_all_org_groups(calls, monkeypatch):
    # ADR 0049 : l'org_admin voit les projets de TOUS les pôles de l'org (même règle
    # que can_read_group) — mais jamais un groupe d'une AUTRE org.
    monkeypatch.setattr(ownership.roles, "is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(ownership.group_store, "list_groups",
                        lambda org: [{"id": 3}, {"id": 4}])
    assert ownership.project_scope_owners("u1", 7) == [
        ("org", "7"), ("group", "3"), ("group", "4")]


def test_no_org_returns_empty(calls):
    assert ownership.accessible_project_ids("u1", None) == []
    assert ownership.project_scope_owners("u1", None) == []


# ── chaque source reçoit SON prédicat (capture des arguments) ────────────────

def test_each_source_gets_its_predicate(monkeypatch):
    rec = {}

    def _cap(key, ret=None):
        # Capture l'argument de scope et renvoie une liste VIDE (pas les args).
        def f(*a, **k):
            rec[key] = a
            return ret if ret is not None else []
        return f

    monkeypatch.setattr(ownership, "accessible_project_ids",
                        lambda sub, org, want="read": rec.setdefault("want", want) and [11, 12])
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: rec.setdefault("docs_pids", pids) and [])
    monkeypatch.setattr(S.db, "search_project_briefs",
                        lambda q, pids, limit: rec.setdefault("briefs_pids", pids) and [])
    monkeypatch.setattr(S.db, "search_procedures_fts",
                        lambda q, org, limit: rec.setdefault("proc_org", org) and [])
    monkeypatch.setattr(S.db, "search_guides_fts",
                        lambda q, org, sub, limit: rec.setdefault("guides", (org, sub)) and [])
    monkeypatch.setattr(S.db, "search_files_meta",
                        lambda q, pids, limit: rec.setdefault("files_pids", pids) and [])
    monkeypatch.setattr(S.ownership, "active_org_principals",
                        lambda sub, org: [("org", "7"), ("user", "u1")])
    monkeypatch.setattr(S.db, "list_datastore_namespaces_for_owners", _cap("ds_owners_a"))
    monkeypatch.setattr(S.db, "list_datastore_namespaces_granted_to", _cap("ds_granted_a"))
    monkeypatch.setattr(S.db, "project_names", lambda ids: {})

    S.search("u1", 7, "prospection")
    assert rec["want"] == "read"
    # docs/briefs/fichiers : le MÊME ensemble accessible (jamais un scope à part)
    assert rec["docs_pids"] == rec["briefs_pids"] == rec["files_pids"] == [11, 12]
    # procédures : l'org active, rien d'autre
    assert rec["proc_org"] == 7
    # guides : org active + sub
    assert rec["guides"] == (7, "u1")
    # tableaux : principals du contexte + grants scopés org/groupes
    assert rec["ds_owners_a"][0] == [("org", "7"), ("user", "u1")]
    assert rec["ds_granted_a"] == ("u1", [7], [])


def test_project_scope_restricts_to_one_project(monkeypatch):
    rec = {}
    monkeypatch.setattr(ownership, "accessible_project_ids",
                        lambda *a, **k: pytest.fail("scope=project ne doit PAS élargir"))
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: rec.setdefault("pids", pids) and [])
    monkeypatch.setattr(S.db, "project_names", lambda ids: {})
    S.search("u1", 7, "x y", scope="project", project_id=42, kinds=["page"])
    assert rec["pids"] == [42]
