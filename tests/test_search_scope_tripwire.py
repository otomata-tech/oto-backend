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
    rec = {"owners": [], "principals": [], "granted_want": [], "createurs": []}

    monkeypatch.setattr(ownership.roles, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(ownership.group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 3}])
    monkeypatch.setattr(ownership.group_store, "list_groups", lambda org: [])
    monkeypatch.setattr(ownership.db, "list_projects_for_owners",
                        lambda owners, createur=None, **k: (
                            rec["owners"].append(list(owners)),
                            rec["createurs"].append(createur))
                        and [{"id": 11}, {"id": 12}])
    # Org 7 = une org de TRAVAIL (l'org perso de u1 est la 5) : décision du 28/09/2026,
    # aucun objet personnel ni partage nominatif n'y entre. Les tests d'org perso
    # passent `org=5`.
    monkeypatch.setattr(ownership.org_store, "home_org", lambda sub: 5)
    monkeypatch.setattr(ownership.db, "list_projects_granted_to",
                        lambda principals: rec["principals"].append(list(principals)) or
                        [{"id": 20, "permission": "read"},
                         {"id": 21, "permission": "write"}])
    return rec


def test_accessible_ids_read_and_write(calls):
    ids = ownership.accessible_project_ids("u1", 7, want="read")
    # owned (org+pôles) ∪ grants
    assert ids == [11, 12, 20, 21]
    # write : owned (je les possède) ; seuls les GRANTS write s'ajoutent
    ids_w = ownership.accessible_project_ids("u1", 7, want="write")
    assert ids_w == [11, 12, 21]


def test_scope_parity_with_op_list(calls):
    """PARITÉ : les owners et les principals de grants utilisés par la recherche sont
    EXACTEMENT ceux d'`oto_project op=list` (le drift de l'un ferait mentir
    « cherchable ⇔ lisible »)."""
    ownership.accessible_project_ids("u1", 7)
    search_owners, search_principals = calls["owners"][-1], calls["principals"][-1]

    # côté op=list : mêmes seams (project_list_owners + principaux_de_liste)
    assert search_owners == ownership.project_list_owners("u1", 7)
    assert search_principals == ownership.principaux_de_liste("u1", 7)
    assert calls["createurs"][-1] == ownership.mes_objets_ici("u1", 7)
    # org de travail : owners = org active + mes groupes ; principals = org + mes
    # groupes — JAMAIS moi (28/09/2026). Mes projets perso créés ICI passent par
    # `createur` (29/09/2026), qui ne porte que sur moi.
    assert search_owners == [("org", "7"), ("group", "3")]
    assert search_principals == [("org", "7"), ("group", "3")]
    assert calls["createurs"][-1] == ("u1", 7, False)


def test_org_perso_ajoute_moi_comme_proprietaire_et_destinataire(calls):
    """Dans MON org perso, la liste (et donc la recherche) rend ce qui m'est partagé en
    personne (28/09/2026) et mes projets perso SANS org de création — `createur` à
    `sans_org`. Plus ceux créés ailleurs (07/10/2026) : la clause SQL ne les prend
    qu'à `context_org_id` nul (`test_liste_s_en_tient_a_l_org`, base réelle)."""
    ownership.accessible_project_ids("u1", 5)
    assert calls["owners"][-1] == [("org", "5"), ("group", "3")]
    assert calls["createurs"][-1] == ("u1", 5, True)
    assert calls["principals"][-1] == [("org", "5"), ("user", "u1"), ("group", "3")]


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
    # Le CONTENU d'un fichier (#298) suit EXACTEMENT le scope de son nom : un fichier
    # n'est pas plus lisible par son texte que par son titre. Une source ajoutée sans
    # entrer ici est le trou que ce tripwire existe pour attraper.
    monkeypatch.setattr(S.db, "search_file_contents",
                        lambda q, pids, limit: rec.setdefault("content_pids", pids) and [])
    monkeypatch.setattr(S.ownership, "principaux_de_liste",
                        lambda sub, org: [("org", "7"), ("user", "u1")])
    monkeypatch.setattr(S.ownership, "perso_de_la_liste",
                        lambda sub, org: [("user", "u1")])
    # Mes tableaux perso créés dans l'org (29/09/2026 : une org perso est une org comme
    # une autre) : la recherche lit la MÊME source que la liste (`mes_tableaux_ici`).
    monkeypatch.setattr(S.ownership, "mes_objets_ici",
                        lambda sub, org: rec.setdefault("createur", (sub, org)) and ("u1", 7, True))
    monkeypatch.setattr(S.ownership, "mes_tableaux_ici",
                        _cap("ds_crees_ici", [{"id": 104, "datastore": "le_mien"}]))
    monkeypatch.setattr(S.db, "list_datastores_shared_to_user",
                        _cap("ds_to_me", [{"id": 103, "datastore": "recu"}]))
    monkeypatch.setattr(S.db, "list_datastores_for_owners",
                        _cap("ds_owners_a", [{"id": 101, "datastore": "prospects"}]))
    monkeypatch.setattr(S.db, "list_datastores_granted_to",
                        _cap("ds_granted_a", [{"id": 102, "datastore": "leads"}]))
    monkeypatch.setattr(S.db, "search_datastore_rows_fts",
                        lambda q, ns_ids, limit=20: rec.setdefault("rows_ns", ns_ids) and [])
    monkeypatch.setattr(S.db, "project_labels", lambda ids: {})

    S.search("u1", 7, "prospection")
    assert rec["want"] == "read"
    # docs/briefs/fichiers : le MÊME ensemble accessible (jamais un scope à part)
    assert (rec["docs_pids"] == rec["briefs_pids"] == rec["files_pids"]
            == rec["content_pids"] == [11, 12])
    # procédures : l'org active, rien d'autre
    assert rec["proc_org"] == 7
    # guides : org active + sub (org perso simulée : le personnel y entre)
    assert rec["guides"] == (7, "u1")
    # tableaux : propriétaires COLLECTIFS de la liste (jamais moi : mes tableaux perso
    # passent par `mes_tableaux_ici`, 07/10/2026) + grants scopés org/groupes + (org
    # perso) partagés à moi
    assert rec["ds_owners_a"][0] == [("org", "7")]
    assert rec["ds_granted_a"] == ("u1", [7], [])
    assert rec["ds_to_me"] == ("u1",)
    assert rec["createur"] == ("u1", 7) and rec["ds_crees_ici"] == (("u1", 7, True),)
    # lignes (#67 V2.1) : héritent de l'accès du datastore → scope = ids des
    # datastores accessibles (owners ∪ mes tableaux créés ici ∪ grants), JAMAIS un
    # scope à part
    assert rec["rows_ns"] == [101, 104, 102, 103]


def test_project_scope_restricts_to_one_project(monkeypatch):
    rec = {}
    monkeypatch.setattr(ownership, "accessible_project_ids",
                        lambda *a, **k: pytest.fail("scope=project ne doit PAS élargir"))
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: rec.setdefault("pids", pids) and [])
    monkeypatch.setattr(S.db, "project_labels", lambda ids: {})
    S.search("u1", 7, "x y", scope="project", project_id=42, kinds=["page"])
    assert rec["pids"] == [42]
