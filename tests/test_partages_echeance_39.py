"""L'échéance d'un partage de ressource (otomata-tech/oto#39, décision du 24/09/2026).

Un partage (audience × rôle, ADR 0048) peut porter `expires_at` ; passée, il ne donne
plus RIEN — ni le contenu d'un projet, d'un tableau ou d'une page, ni les listes « reçu »,
ni le prêt des clés d'un projet. Sans rappel au propriétaire (il attend oto#272).

Deux parties :

1. **la garde** — le refus vit dans CHAQUE lecture de `resource_grants`, donc une
   lecture ajoutée demain qui oublie le prédicat rouvrirait la ressource. Le balayage
   ci-dessous lit tout le SQL de `oto_mcp/` et refuse une fonction qui lit la table sans
   `PARTAGE_VIVANT`, sauf exemption motivée ;
2. **le comportement**, contre un PostgreSQL réel : un partage échu ne donne plus
   accès (projet, tableau, page, héritage des clés), un partage non échu et un partage
   sans échéance fonctionnent comme avant.
"""
from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
PAQUET = RACINE / "oto_mcp"

# ── 1. La garde ───────────────────────────────────────────────────────────────

#: Une LECTURE de la table : `FROM` ou `JOIN` (un `DELETE FROM` est une écriture).
_LECTURE = re.compile(r"(?<!DELETE )\bFROM\s+resource_grants\b|\bJOIN\s+resource_grants\b")

#: Les fonctions qui lisent `resource_grants` SANS le prédicat, et pourquoi. Une entrée
#: ici est une décision : elle se relit en revue, elle ne se pose pas pour faire passer.
EXEMPTIONS = {
    ("oto_mcp/db/datastore_ns.py", "list_resource_grants"):
        "la liste de gouvernance d'`op=get` : rend les partages ÉCHUS, marqués "
        "`expired`, pour que le propriétaire les constate (comme un jeton expiré). "
        "Elle n'ouvre aucun accès.",
}


def _lectures_sans_predicat() -> tuple[set, set]:
    fautives, vues = set(), set()
    for chemin in sorted(PAQUET.rglob("*.py")):
        rel = chemin.relative_to(RACINE).as_posix()
        if "/migrations/" in rel:
            continue
        source = chemin.read_text(encoding="utf-8")
        if not _LECTURE.search(source):
            continue
        arbre = ast.parse(source)
        couvert = [0] * (source.count("\n") + 2)
        for noeud in ast.walk(arbre):
            if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
                segment = ast.get_source_segment(source, noeud) or ""
                for i in range(noeud.lineno, (noeud.end_lineno or noeud.lineno) + 1):
                    couvert[i] = 1
                if not _LECTURE.search(segment):
                    continue
                # La plus petite fonction qui porte la lecture répond d'elle.
                if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n is not noeud and _LECTURE.search(
                           ast.get_source_segment(source, n) or "")
                       for n in ast.walk(noeud)):
                    continue
                vues.add((rel, noeud.name))
                if "PARTAGE_VIVANT" not in segment and (rel, noeud.name) not in EXEMPTIONS:
                    fautives.add((rel, noeud.name))
        hors_fonction = "\n".join(l for i, l in enumerate(source.splitlines(), 1)
                                  if not couvert[i])
        if _LECTURE.search(hors_fonction):
            fautives.add((rel, "<module>"))
    return fautives, vues


def test_chaque_lecture_de_resource_grants_filtre_les_partages_echus():
    fautives, _ = _lectures_sans_predicat()
    assert not fautives, (
        f"lecture(s) de `resource_grants` sans le prédicat du partage vivant : "
        f"{sorted(fautives)}. Un partage échu y donnerait encore quelque chose. Ajoute "
        f"`PARTAGE_VIVANT` / `PARTAGE_VIVANT_G` (`oto_mcp/db/_partage_vivant.py`) à la "
        f"requête, ou une exemption MOTIVÉE dans `EXEMPTIONS`.")


def test_la_garde_voit_les_lectures_connues_et_aucune_exemption_morte():
    """Un balayage qui ne verrait rien serait vert pour rien : il doit trouver les
    lectures relevées le 24/09, et chaque exemption doit encore désigner une lecture."""
    _, vues = _lectures_sans_predicat()
    attendues = {
        ("oto_mcp/db/datastore_ns.py", "resolve_datastore_ns"),
        ("oto_mcp/db/datastore_ns.py", "get_resource_grant"),
        ("oto_mcp/db/projects.py", "list_projects_granted_to"),
        ("oto_mcp/db/doc_grants.py", "list_docs_granted_to"),
        ("oto_mcp/db/shell.py", "direct_grants"),
    }
    assert attendues <= vues, f"le balayage ne voit plus : {sorted(attendues - vues)}"
    assert set(EXEMPTIONS) <= vues, f"exemption morte : {sorted(set(EXEMPTIONS) - vues)}"


def test_la_garde_refuse_une_lecture_sans_predicat(tmp_path, monkeypatch):
    """Vue refuser au moins une fois : un faux module qui lit la table nu."""
    import sys
    faux = tmp_path / "oto_mcp" / "db"
    faux.mkdir(parents=True)
    (faux / "x.py").write_text(
        'def f(c):\n    return c.execute("SELECT 1 FROM resource_grants g '
        'WHERE g.resource_id = %s")\n', encoding="utf-8")
    ce_module = sys.modules[__name__]
    monkeypatch.setattr(ce_module, "PAQUET", tmp_path / "oto_mcp")
    monkeypatch.setattr(ce_module, "RACINE", tmp_path)
    fautives, _ = _lectures_sans_predicat()
    assert fautives == {("oto_mcp/db/x.py", "f")}


# ── 2. L'entrée : stricte, et réservée au partage à un principal ──────────────

@pytest.mark.parametrize("brut", [0, -3, "30", True, 1.5])
def test_ttl_days_illisible_est_refuse_pas_lu_comme_sans_echeance(brut):
    """Contrairement aux jetons : une échéance mal écrite serait un partage éternel que
    son auteur croit borné."""
    from pydantic import ValidationError

    from oto_mcp.capabilities import resources as R
    with pytest.raises(ValidationError):
        R.ResourceInput(op="share", resource_type="project", resource_id="7",
                        email="a@x.co", ttl_days=brut)


def test_ttl_days_est_servi_sur_les_deux_surfaces():
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities import resources_v2 as V
    for modele in (R.ResourceInput, V.ResourceInputV2):
        champ = modele.model_json_schema()["properties"]["ttl_days"]
        assert "expire" in champ["description"].lower()
    for cle in ("resources.govern", "resources.govern.v2"):
        cap = next(c for c in R.CAPABILITIES if c.key == cle)
        assert "ttl_days" in cap.description and "NO reminder" in cap.description


# ── 3. Le comportement, contre un PostgreSQL réel ─────────────────────────────

@pytest.fixture
def monde(live):
    """Org A propriétaire (projet, tableau, page) ; P, hors de l'org A, membre de
    l'org B et de son équipe G — les trois types de bénéficiaire."""
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:8]
    owner, benef = f"owner_{u}", f"benef_{u}"
    for s in (owner, benef):
        db.upsert_user(s, email=f"{s}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=owner)
    org_b = org_store.create_org(f"b_{u}", created_by=benef)
    org_store.add_org_member(org_a, owner, "org_admin")
    org_store.add_org_member(org_b, owner)
    org_store.add_org_member(org_b, benef)
    team = group_store.create_group(org_b, f"g_{u}")
    group_store.add_group_member(team, benef)
    pid = db.create_project("org", str(org_a), f"p_{u}", created_by=owner)
    ns = db.create_datastore("org", str(org_a), f"t_{u}")
    doc = db.create_doc(pid, f"page_{u}", body_md="x", created_by=owner)
    return {"owner": owner, "benef": benef, "org_a": org_a, "org_b": org_b,
            "team": team, "pid": pid, "ns": ns, "doc": doc}


def _dest(m: dict, type_: str) -> dict:
    return {"person": {"email": f"{m['benef']}@example.test"},
            "team": {"group_id": m["team"]},
            "org": {"org_id": m["org_b"]}}[type_]


def _principal(m: dict, type_: str) -> tuple[str, str]:
    return {"person": ("user", m["benef"]), "team": ("group", str(m["team"])),
            "org": ("org", str(m["org_b"]))}[type_]


def _partager(m: dict, rtype: str, rid, type_: str = "person", **kw):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    return R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org_a"]),
                        R.ResourceInput(op="share", resource_type=rtype,
                                        resource_id=str(rid), role="viewer",
                                        **_dest(m, type_), **kw))


def _lire(m: dict, rtype: str, rid) -> dict:
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    return R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org_a"]),
                        R.ResourceInput(op="get", resource_type=rtype,
                                        resource_id=str(rid)))


def _echoir(kind: str, rid, principal: tuple[str, str]) -> None:
    """L'échéance passée, posée en base : `ttl_days` n'admet pas le passé."""
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        n = c.execute("UPDATE resource_grants SET expires_at = NOW() - INTERVAL '1 hour' "
                      "WHERE resource_type = %s AND resource_id = %s "
                      "AND principal_type = %s AND principal_id = %s",
                      (kind, str(rid), *principal)).rowcount
    assert n == 1


@pytest.mark.parametrize("type_", ("person", "team", "org"))
def test_projet_echu_ne_donne_plus_acces(monde, type_):
    from oto_mcp import db, ownership
    m, benef = monde, monde["benef"]
    _partager(m, "project", m["pid"], type_)
    assert ownership.can_access(benef, "project", str(m["pid"]))
    _echoir("project", m["pid"], _principal(m, type_))
    assert not ownership.can_access(benef, "project", str(m["pid"]))
    assert m["pid"] not in [r["id"] for r in db.list_projects_granted_to([_principal(m, type_)])]
    assert db.project_grant_counts([m["pid"]]).get(m["pid"], 0) == 0


def test_tableau_echu_ne_donne_plus_acces(monde):
    from oto_mcp import db, ownership
    m, benef = monde, monde["benef"]
    _partager(m, "datastore_namespace", m["ns"])
    assert ownership.can_access(benef, "datastore_namespace", str(m["ns"]))
    ns_nom = db.get_datastore_by_id(m["ns"])["datastore"]
    assert db.resolve_datastore_ns(ns_nom, sub=benef, org_ids=[], group_ids=[])
    _echoir("datastore_namespace", m["ns"], ("user", benef))
    assert not ownership.can_access(benef, "datastore_namespace", str(m["ns"]))
    assert db.resolve_datastore_ns(ns_nom, sub=benef, org_ids=[], group_ids=[]) is None
    assert db.resolve_datastore_ids_by_name(
        [ns_nom], sub=benef, org_ids=[], group_ids=[]) == ({}, set())
    assert db.list_datastores_shared_to_user(benef) == []


def test_tableau_echu_a_une_org_sort_de_sa_liste(monde):
    from oto_mcp import db
    m = monde
    _partager(m, "datastore_namespace", m["ns"], "org")
    assert [r["id"] for r in db.list_datastores_granted_to(m["benef"], [m["org_b"]], [])] == [m["ns"]]
    _echoir("datastore_namespace", m["ns"], ("org", str(m["org_b"])))
    assert db.list_datastores_granted_to(m["benef"], [m["org_b"]], []) == []


def test_page_echue_ne_donne_plus_acces(monde):
    from oto_mcp import db, ownership
    from oto_mcp.db import shell as db_shell
    m, benef = monde, monde["benef"]
    _partager(m, "doc", m["doc"])
    assert ownership.can_access(benef, "doc", str(m["doc"]))
    assert [r["id"] for r in db.list_docs_granted_to([("user", benef)])] == [m["doc"]]
    assert [r["id"] for r in db.list_shared_docs(None) if r["id"] == m["doc"]] == [m["doc"]]
    assert [g["resource_id"] for g in db_shell.direct_grants(benef)] == [str(m["doc"])]
    _echoir("doc", m["doc"], ("user", benef))
    assert not ownership.can_access(benef, "doc", str(m["doc"]))
    assert db.list_docs_granted_to([("user", benef)]) == []
    assert [r for r in db.list_shared_docs(None) if r["id"] == m["doc"]] == []
    assert db_shell.direct_grants(benef) == []


def test_partage_non_echu_et_sans_echeance_fonctionnent_comme_avant(monde):
    from oto_mcp import ownership
    m, benef = monde, monde["benef"]
    borne = _partager(m, "project", m["pid"], "team", ttl_days=30)
    assert borne["expires_at"] is not None
    libre = _partager(m, "project", m["pid"], "person")
    assert libre["expires_at"] is None
    assert ownership.can_access(benef, "project", str(m["pid"]))
    grants = {g["principal_type"]: g for g in _lire(m, "project", m["pid"])["grants"]}
    assert grants["group"]["expires_at"] is not None and grants["group"]["expired"] is False
    assert grants["user"]["expires_at"] is None and grants["user"]["expired"] is False


def test_le_partage_echu_reste_liste_marque(monde):
    """Comme un jeton expiré : le propriétaire le constate au lieu de le perdre."""
    m = monde
    _partager(m, "project", m["pid"], ttl_days=1)
    _echoir("project", m["pid"], ("user", m["benef"]))
    [g] = _lire(m, "project", m["pid"])["grants"]
    assert g["expired"] is True and g["expires_at"] is not None


def test_re_partager(monde):
    """Omis, `ttl_days` laisse à un partage VIVANT son échéance ; un partage ÉCHU
    re-partagé est rouvert sans échéance."""
    from oto_mcp import ownership
    m = monde
    pose = _partager(m, "project", m["pid"], ttl_days=5)["expires_at"]
    assert pose is not None
    assert _partager(m, "project", m["pid"])["expires_at"] == pose
    _echoir("project", m["pid"], ("user", m["benef"]))
    assert _partager(m, "project", m["pid"])["expires_at"] is None
    assert ownership.can_access(m["benef"], "project", str(m["pid"]))


def test_la_cascade_porte_l_echeance(monde):
    from oto_mcp import db
    m = monde
    db.add_project_link(m["pid"], "tableau", str(m["ns"]), label="t")
    _partager(m, "project", m["pid"], ttl_days=3, cascade=True)
    [g] = db.list_resource_grants("datastore_namespace", str(m["ns"]))
    assert g["expires_at"] is not None


@pytest.mark.parametrize("kw", [
    {"op": "share", "audience": "public"},
    {"op": "unshare"},
])
def test_ttl_days_hors_d_un_partage_a_un_principal_est_refuse(monde, kw):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    m = monde
    with pytest.raises(AuthzDenied) as e:
        R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org_a"]),
                     R.ResourceInput(resource_type="project", resource_id=str(m["pid"]),
                                     email=f"{m['benef']}@example.test", ttl_days=3, **kw))
    assert (e.value.status, e.value.code) == (400, "ttl_days_grant_only")
