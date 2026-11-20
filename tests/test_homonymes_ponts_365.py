"""#365 — aucun pont vers un tableau ne se résout par son NOM chez l'appelant.

Le motif, trouvé sur les lignes d'un nœud-tableau (v1.134.0) : un pont désignait son
tableau par un NOM, et le store résout un nom dans la portée de l'APPELANT (perso > org
> partages). « vivier », « leads », « contacts » y existent couramment en plusieurs
exemplaires : deux homonymes atteignables suffisaient à servir — ou à écrire dans — un
AUTRE tableau que celui désigné, sans erreur, avec des colonnes plausibles.

Deux parties :

1. **le comportement, sur le montage réel** (PostgreSQL, vrais tableaux, vraie
   recopie en nœuds) : pour chaque pont, un homonyme dans un autre scope atteignable
   n'est PAS servi ;
2. **la garde** qui repère un pont neuf par nom (`test_*_garde_*`, en bas).
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
PAQUET = RACINE / "oto_mcp"


# ── 1. Le montage réel ────────────────────────────────────────────────────────

def _ligne(ns_id: int, nom: str) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                  (ns_id, uuid.uuid4().hex, json.dumps({"nom": nom})))


@pytest.fixture(scope="module")
def monde(live):
    """Une personne, membre de trois orgs, qui a sous la main un « vivier » PERSO et le
    « vivier » de son org active — les deux atteignables, le perso mieux classé."""
    from oto_mcp import db, group_store, org_store
    from oto_mcp.db import nodes as db_nodes
    from oto_mcp.db._conn import _connect

    u = uuid.uuid4().hex[:8]
    moi = f"moi_{u}"
    db.upsert_user(moi, email=f"{moi}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=moi)
    org_b = org_store.create_org(f"b_{u}", created_by=moi)
    org_c = org_store.create_org(f"c_{u}", created_by=moi)
    for o in (org_a, org_b, org_c):
        org_store.add_org_member(o, moi)
    org_store.set_active_org(moi, org_a)
    equipe = group_store.create_group(org_a, f"g_{u}")
    group_store.add_group_member(equipe, moi)

    m = {"moi": moi, "org_a": org_a, "org_b": org_b, "org_c": org_c, "equipe": equipe}
    m["ns_org"] = db.create_datastore("org", str(org_a), "vivier")
    m["ns_perso"] = db.create_datastore("user", moi, "vivier")
    m["ns_equipe"] = db.create_datastore("group", str(equipe), "vivier")
    _ligne(m["ns_org"], "chez l'org")
    _ligne(m["ns_perso"], "chez moi")
    # Un nom qui n'existe QUE dans une autre org.
    m["ns_leads_b"] = db.create_datastore("org", str(org_b), "leads")
    # Deux « partage » de deux orgs, tous deux partagés à l'org A : même rang.
    m["ns_partage_b"] = db.create_datastore("org", str(org_b), "partage")
    m["ns_partage_c"] = db.create_datastore("org", str(org_c), "partage")
    for ns in (m["ns_partage_b"], m["ns_partage_c"]):
        db.grant_resource("datastore_namespace", str(ns), "org", str(org_a), role="viewer")

    with _connect() as c:
        db_nodes.convert_tables(c)
        m["noeud_org"] = c.execute(
            "SELECT public_id FROM nodes WHERE props->>'legacy' = 'tbl' "
            "AND props->>'legacy_id' = %s", (str(m["ns_org"]),)).fetchone()["public_id"]
    return m


def _ctx(m):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=m["moi"], org_id=m["org_a"])


def _resout(m, adresse: str) -> int:
    """Ce que le store fait de l'adresse servie — le geste du client suivant."""
    from oto_mcp.datastore import core as ds
    return int(ds.make_store(m["moi"])._resolve(adresse))


def test_le_monde_porte_bien_deux_homonymes_atteignables(monde):
    """L'instrument d'abord : sans l'homonyme perso mieux classé, les tests suivants
    seraient verts sur l'ancien code pour de mauvaises raisons. Le store, lui, refuse
    le nom depuis le 08/11/2026 : c'est le résolveur de la base qui montre l'homonyme."""
    from oto_mcp import db
    from oto_mcp.datastore.errors import TableauParNom
    vu = db.resolve_datastore_ns("vivier", sub=monde["moi"], org_ids=[monde["org_a"]],
                                 group_ids=[monde["equipe"]])
    assert int(vu["id"]) == monde["ns_perso"]
    with pytest.raises(TableauParNom):
        _resout(monde, "vivier")


# Pont ① — les LIGNES d'un nœud-tableau (`node_rows`).
def test_les_lignes_du_noeud_sont_celles_de_SON_tableau(monde):
    """L'ancienne garde résolvait le titre puis refusait (404) s'il tombait ailleurs :
    sûre, mais aveugle au bon tableau. Par la clé, le bon tableau est servi, et
    l'homonyme perso jamais."""
    from oto_mcp.capabilities import node_rows as R
    page = R._compose(_ctx(monde), R.NodeRowsInput(node_id=monde["noeud_org"]))
    assert [r["cells"].get("nom") for r in page["items"]] == ["chez l'org"]


# Pont ② — la poignée `datastore` de la fiche (`node_view`).
def test_la_poignee_de_la_fiche_mene_a_SON_tableau(monde):
    from oto_mcp.capabilities import node_view as V
    fiche = V._compose(_ctx(monde), monde["noeud_org"])
    assert fiche["datastore"] is not None
    assert _resout(monde, fiche["datastore"]) == monde["ns_org"], (
        "la poignée servie mène à l'homonyme de l'appelant")


# Pont ③ — `slot:` (le tableau bindé par le projet actif).
def test_le_slot_mene_au_tableau_BINDE(monde, monkeypatch):
    from oto_mcp import access, db
    pid = db.create_project("org", str(monde["org_a"]), f"slot_{uuid.uuid4().hex[:6]}",
                            created_by=monde["moi"])
    db.add_project_link(pid, "tableau", str(monde["ns_org"]), slot="sortie")
    monkeypatch.setattr(access, "current_project", lambda: pid)
    assert _resout(monde, access.resolve_datastore_ref("slot:sortie")) == monde["ns_org"]


def _projet_lie_par_nom(monde, nom: str, *, owner=("org", None)) -> int:
    from oto_mcp import db
    otype, oid = owner
    pid = db.create_project(otype, oid or str(monde["org_a"]),
                            f"p_{uuid.uuid4().hex[:6]}", created_by=monde["moi"])
    db.add_project_link(pid, "tableau", nom, slot="sortie")
    return pid


def _lien(pid: int) -> dict:
    from oto_mcp import db
    return next(l for l in db.list_project_links(pid) if l["target_type"] == "tableau")


# Pont ④ — un lien de projet posé par NOM (`db.list_project_links`).
def test_un_lien_par_nom_ne_vit_pas_d_un_homonyme_d_une_AUTRE_org(monde):
    """« leads » n'existe que dans l'org B ; le projet est dans l'org A. Le lien ne
    résout pas — il ne « vit » pas par la simple existence du nom ailleurs."""
    from oto_mcp.project_audit import audit_project
    pid = _projet_lie_par_nom(monde, "leads")
    lien = _lien(pid)
    assert "datastore" not in lien and "datastore_id" not in lien
    morts = audit_project(pid, light=True)["dead_links"]
    assert [d["target_ref"] for d in morts] == ["leads"]


def test_un_nom_AMBIGU_dans_la_portee_n_est_resolu_vers_aucun(monde, monkeypatch):
    """Deux « partage » reçus de deux orgs, au même rang : le plus petit identifiant
    l'emportait en silence. Aucun n'est servi, et `slot:` le refuse en le nommant."""
    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError
    pid = _projet_lie_par_nom(monde, "partage")
    lien = _lien(pid)
    assert lien.get("datastore_ambigu") is True
    assert "datastore_id" not in lien and "datastore" not in lien
    monkeypatch.setattr(access, "current_project", lambda: pid)
    with pytest.raises(McpError) as e:
        access.resolve_datastore_ref("slot:sortie")
    assert "“partage”" in str(e.value)


# Pont ⑤ — l'endpoint partagé et la page partagée : la MÊME résolution que le rail.
def test_l_endpoint_partage_expose_le_tableau_du_PROJET(monde, monkeypatch):
    """Projet d'ÉQUIPE lié par nom à « vivier » : l'équipe et son org en ont chacune
    un. Le projet désigne celui de l'équipe ; l'endpoint anonyme cherchait dans l'org
    et exposait l'autre."""
    from oto_mcp import share_ui, subdomain_project
    from oto_mcp import db
    from oto_mcp.tools import datastore as TD
    pid = _projet_lie_par_nom(monde, "vivier", owner=("group", str(monde["equipe"])))
    assert _lien(pid)["datastore_id"] == monde["ns_equipe"]
    monkeypatch.setattr(subdomain_project, "current_anon_org", lambda: monde["org_a"])
    assert TD._anon_project_tableau_ns_ids(pid) == frozenset({monde["ns_equipe"]})
    assert [t["id"] for t in share_ui._tableau_entries(db.list_project_links(pid))] \
        == [monde["ns_equipe"]]


# L'adresse par identifiant : des chiffres qui sont AUSSI le nom d'un autre tableau.
def test_un_tableau_NOMME_comme_l_identifiant_d_un_autre_ne_le_capte_pas(monde):
    """Les ponts adressent désormais un tableau par son identifiant en chiffres. Un
    tableau NOMMÉ « 77 » — que n'importe quel membre peut créer — captait tout ce qui
    visait le tableau 77 : c'était le nom qui gagnait, en silence."""
    from oto_mcp import db
    from oto_mcp.datastore.errors import DatastoreAmbigu
    cible = db.create_datastore("org", str(monde["org_a"]), f"cible_{uuid.uuid4().hex[:6]}")
    leurre = db.create_datastore("user", monde["moi"], str(cible))
    try:
        with pytest.raises(DatastoreAmbigu) as e:
            _resout(monde, str(cible))
        assert str(cible) in e.value.indice and str(leurre) in e.value.indice
    finally:
        db.delete_datastore_by_id(leurre)


# ── 2. La garde : un pont neuf par nom ne passe pas inaperçu ──────────────────

#: Les résolveurs par NOM d'un tableau. Tout appel hors de ces lieux déclarés est un
#: pont neuf à justifier — une entrée ici est une décision, relue en revue.
_RESOLVEURS_PAR_NOM = {"get_datastore", "resolve_datastore_ns", "resolve_datastore_ids_by_name"}

EXEMPTIONS = {
    ("oto_mcp/datastore/core.py", "_trouver"):
        "l'adressage du store lui-même (`_resolve` le compose) : l'identifiant que "
        "l'APPELANT a tapé, résolu dans SA portée — un nom y est refusé avant "
        "(`TableauParNom`, 08/11/2026)",
    ("oto_mcp/db/projects.py", "list_project_links_many"):
        "LE lieu où un lien de projet posé par nom se résout, dans la portée du "
        "propriétaire de SON projet (groupe par groupe quand plusieurs projets se lisent "
        "en lot), ambiguïté refusée (#365)",
    ("oto_mcp/capabilities/projects.py", "_resolve_tableau_id"):
        "retrouver sous son identifiant un lien STOCKÉ par nom avant la normalisation, "
        "sur les propriétaires du projet (`_ref_canonizer`) ; un nom passé par "
        "l'appelant est refusé avant (`datastore_by_name`)",
    ("oto_mcp/capabilities/_lignes_reservables.py", "cle_a_la_declaration"):
        "résolution nom→id À LA DÉCLARATION d'une automatisation, dans la portée du "
        "déclarant, ambiguïté refusée (#1067) ; l'identifiant retenu est gardé et servi",
    ("oto_mcp/capabilities/_lignes_reservables.py", "_cle_heritee"):
        "reprise des automatisations d'avant #1067 qui ne gardent qu'un nom : refusée si "
        "l'ancienne règle et celle de la déclaration divergent, fixée au premier "
        "armement ou travail — à retirer quand plus aucune ne garde un nom",
}

#: Les modules qui lisent un NŒUD. Aucune de leurs fonctions ne mêle le titre d'un
#: nœud et une adresse de tableau : c'était exactement la forme du défaut.
_MODULES_NOEUD = sorted(
    [p for p in (PAQUET / "capabilities").glob("node_*.py")]
    + [PAQUET / "capabilities" / "shell.py", PAQUET / "db" / "node_view.py",
       PAQUET / "db" / "shell.py", PAQUET / "db" / "project_nodes.py"])
_ADRESSAGE = ("make_store(", "_resolve(", *(f"{n}(" for n in _RESOLVEURS_PAR_NOM))


def _fonctions(chemin: Path):
    source = chemin.read_text(encoding="utf-8")
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield noeud, ast.get_source_segment(source, noeud) or ""


def _appelle(noeud: ast.AST, noms: set) -> bool:
    for n in ast.walk(noeud):
        if isinstance(n, ast.Call):
            f = n.func
            nom = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nom in noms:
                return True
    return False


def test_garde_chaque_resolution_par_nom_est_un_lieu_declare():
    trouves = set()
    for chemin in sorted(PAQUET.rglob("*.py")):
        rel = chemin.relative_to(RACINE).as_posix()
        if "/migrations/" in rel:
            continue
        for noeud, _ in _fonctions(chemin):
            if noeud.name in _RESOLVEURS_PAR_NOM:
                continue                                   # la définition elle-même
            if _appelle(noeud, _RESOLVEURS_PAR_NOM):
                # La plus petite fonction qui porte l'appel en répond.
                if any(n is not noeud and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and _appelle(n, _RESOLVEURS_PAR_NOM) for n in ast.walk(noeud)):
                    continue
                trouves.add((rel, noeud.name))
    nouveaux = trouves - set(EXEMPTIONS)
    assert not nouveaux, (
        f"Résolution d'un tableau par NOM hors des lieux déclarés : {sorted(nouveaux)}. "
        "Un nom se résout dans la portée de l'appelant, donc chez son homonyme (#365) : "
        "adresse le tableau par son identifiant, ou déclare le lieu dans EXEMPTIONS "
        "avec la portée qu'il respecte.")
    perimees = set(EXEMPTIONS) - trouves
    assert not perimees, f"Exemptions sans objet, à retirer : {sorted(perimees)}"


def test_garde_un_module_de_noeud_n_adresse_pas_un_tableau_par_son_titre():
    fautives = []
    for chemin in _MODULES_NOEUD:
        for noeud, segment in _fonctions(chemin):
            if any(a in segment for a in _ADRESSAGE) and "title" in segment:
                fautives.append((chemin.relative_to(RACINE).as_posix(), noeud.name))
    assert not fautives, (
        f"{fautives} : une fonction de nœud mêle le TITRE d'un nœud et l'adressage d'un "
        "tableau — le pont par nom de #365. Adresse le tableau par "
        "`node_keys.datastore_de` (son identifiant).")


def test_garde_la_garde_mord():
    """L'instrument : l'ancienne forme de `node_rows._compose` est bien repérée."""
    ancienne = ('def _compose(ctx, inp):\n    namespace = props.get("title") or ""\n'
                '    store = ds.make_store(ctx.sub)\n    ns_id = store._resolve(namespace)\n')
    noeud = ast.parse(ancienne).body[0]
    segment = ast.get_source_segment(ancienne, noeud)
    assert any(a in segment for a in _ADRESSAGE) and "title" in segment
