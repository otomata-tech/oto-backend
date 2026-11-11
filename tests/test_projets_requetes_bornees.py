"""Une op projet ne coûte pas une requête par projet, ni par lien (incident du 13/10/2026).

La liste d'une org de vingt projets faisait plus de deux cents requêtes (liens, portée,
droit d'écriture, relus projet par projet) : environ 1,1 s par appel à vide en prod, et
un écran qui demandait le détail de chaque projet en parallèle saturait le serveur. Sur
le montage réel (PostgreSQL), on COMPTE les requêtes : elles ne dépendent ni du nombre
de projets de la liste, ni du nombre de liens du projet lu.

Le lot ne doit rien changer à la règle : un lien se résout dans la portée du propriétaire
de SON projet, même quand un voisin de liste, d'un autre propriétaire, lie la même cible.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest


@pytest.fixture
def requetes(monkeypatch):
    compte = {"n": 0}
    execute = psycopg.Cursor.execute

    def compter(self, *a, **k):
        compte["n"] += 1
        return execute(self, *a, **k)
    monkeypatch.setattr(psycopg.Cursor, "execute", compter)
    return compte


def _org(nom: str, sub: str) -> int:
    from oto_mcp import org_store
    org = org_store.create_org(nom, created_by=sub)
    org_store.add_org_member(org, sub)
    return org


def _projet(org: int, sub: str, nom: str, liens: int) -> int:
    """Un projet d'org avec `liens` tableaux (par id et par nom) et procédures, et un
    connecteur."""
    from oto_mcp import db, org_store
    pid = db.create_project("org", str(org), nom, created_by=sub)
    for i in range(liens):
        ns_nom = f"{nom}_t{i}"
        ns = db.create_datastore("org", str(org), ns_nom)
        db.add_project_link(pid, "tableau", str(ns), None)
        db.add_project_link(pid, "tableau", ns_nom, f"par nom {i}")
        org_store.set_instruction("org", org, f"{nom}-p{i}", "# P\n", title=f"P{i}",
                                  set_by=sub)
        proc = org_store.get_instruction("org", org, f"{nom}-p{i}")["id"]
        db.add_project_link(pid, "procedure", str(proc), None)
    db.add_project_link(pid, "connecteur", "zoho", None)
    return pid


def _moi_dans_une_org(projets: int, liens: int = 1):
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:8]
    moi = f"moi_{u}"
    db.upsert_user(moi, email=f"{moi}@example.test")
    org = _org(f"o_{u}", moi)
    org_store.set_active_org(moi, org)
    pids = [_projet(org, moi, f"p{k}_{u}", liens) for k in range(projets)]
    return moi, org, pids


def _appel(requetes, moi, org, **kw) -> tuple[dict, int]:
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    requetes["n"] = 0
    out = P._project(ResolvedCtx(sub=moi, org_id=org), P.ProjectInput(**kw))
    return out, requetes["n"]


def test_la_liste_ne_coute_pas_une_requete_par_projet(live, requetes):
    petite = _moi_dans_une_org(3)
    grande = _moi_dans_une_org(12)
    _, n_petite = _appel(requetes, *petite[:2], op="list")
    out, n_grande = _appel(requetes, *grande[:2], op="list")
    assert len(out["projects"]) == 12
    assert n_grande == n_petite, (
        f"op=list : {n_petite} requêtes pour 3 projets, {n_grande} pour 12")
    # Les liens et l'audit léger sont déjà chargés pour les pastilles : les rendre ne
    # coûte aucune requête.
    out, n_tout = _appel(requetes, *grande[:2], op="list", include=["links", "audit"])
    assert all("links" in p and "audit" in p for p in out["projects"])
    assert n_tout == n_grande, (
        f"op=list : {n_grande} requêtes, {n_tout} avec include=links,audit")


def test_lire_un_projet_ne_coute_pas_une_requete_par_lien(live, requetes):
    peu = _moi_dans_une_org(1, liens=1)
    beaucoup = _moi_dans_une_org(1, liens=8)
    # Chaud d'abord : la présence de l'index des ouvertures par run se relit au plus une
    # fois par minute et par processus (`usage._ouverture_par_run_prete`) — une requête
    # au premier appel, aucune ensuite. Ce banc compte ce qui suit les LIENS.
    _appel(requetes, *peu[:2], op="get", project_id=peu[2][0])
    _, n_peu = _appel(requetes, *peu[:2], op="get", project_id=peu[2][0])
    out, n_beaucoup = _appel(requetes, *beaucoup[:2], op="get", project_id=beaucoup[2][0])
    assert len(out["links"]) == 8 * 3 + 1
    assert n_beaucoup == n_peu, (
        f"op=get : {n_peu} requêtes pour 4 liens, {n_beaucoup} pour 25")


def test_la_liste_rend_les_liens_d_op_get_sur_demande(live):
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    moi, org, pids = _moi_dans_une_org(3, liens=2)
    ctx = ResolvedCtx(sub=moi, org_id=org)
    sans = P._project(ctx, P.ProjectInput(op="list"))
    assert all("links" not in p for p in sans["projects"])
    avec = P._project(ctx, P.ProjectInput(op="list", include=["links"]))
    for p in avec["projects"]:
        lu = P._project(ctx, P.ProjectInput(op="get", project_id=p["id"]))
        assert p["links"] == lu["links"]
        assert p["links"] and all(l.get("datastore_id") or l.get("procedure_id")
                                  for l in p["links"] if l["target_type"] != "connecteur")


def test_en_lot_chaque_lien_garde_la_portee_de_son_projet(live):
    """Un projet d'une autre org, partagé à la mienne, lie MON tableau : dans la même
    liste, mon projet le résout, le sien le dit hors de sa portée."""
    from oto_mcp import db
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    moi, org, (mien,) = _moi_dans_une_org(1)
    u = uuid.uuid4().hex[:8]
    tiers = f"tiers_{u}"
    db.upsert_user(tiers, email=f"{tiers}@example.test")
    org_b = _org(f"b_{u}", tiers)
    ns_mien = next(int(l["target_ref"]) for l in db.list_project_links(mien)
                   if l["target_type"] == "tableau" and str(l["target_ref"]).isdigit())
    sien = db.create_project("org", str(org_b), f"sien_{u}", created_by=tiers)
    db.add_project_link(sien, "tableau", str(ns_mien), None)
    db.grant_resource("project", str(sien), "org", str(org), role="viewer")

    out = P._project(ResolvedCtx(sub=moi, org_id=org),
                     P.ProjectInput(op="list", include=["links"]))
    par_id = {p["id"]: p for p in out["projects"]}
    assert set(par_id) == {mien, sien}
    du_mien, du_sien = (next(l for l in par_id[p]["links"] if l["target_type"] == "tableau"
                             and l["target_ref"] == str(ns_mien)) for p in (mien, sien))
    assert du_mien["datastore_id"] == ns_mien
    assert not ({"datastore", "datastore_id"} & set(du_sien)) and du_sien["hors_portee"]
    assert par_id[mien]["can_write"] is True and par_id[sien]["can_write"] is False



def test_la_liste_rend_l_audit_leger_d_op_get_sur_demande(live):
    """Un tableau mort et un connecteur hors registre : la liste les dit comme op=get.
    Une procédure morte : op=get seul la dit (une requête par lien), et l'audit de la
    liste se marque léger pour qu'on ne lise pas son absence comme une santé."""
    from oto_mcp import db
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    moi, org, (pid,) = _moi_dans_une_org(1)
    db.add_project_link(pid, "tableau", "2000000000", None)
    db.add_project_link(pid, "connecteur", "connecteur_inexistant", None)
    db.add_project_link(pid, "procedure", "2000000001", None)
    ctx = ResolvedCtx(sub=moi, org_id=org)

    sans = P._project(ctx, P.ProjectInput(op="list"))
    assert all("audit" not in p for p in sans["projects"])
    (projet,) = P._project(ctx, P.ProjectInput(op="list", include=["audit"]))["projects"]
    assert "links" not in projet
    leger = projet["audit"]
    complet = P._project(ctx, P.ProjectInput(op="get", project_id=pid))["audit"]

    assert leger["light"] is True and "light" not in complet
    assert set(leger) - {"light"} == set(complet)
    assert leger["dead_links"] == [d for d in complet["dead_links"]
                                   if d["target_type"] != "procedure"]
    assert {(d["target_type"], str(d["target_ref"])) for d in leger["dead_links"]} == {
        ("tableau", "2000000000"), ("connecteur", "connecteur_inexistant")}
    assert ("procedure", "2000000001") in {
        (d["target_type"], str(d["target_ref"])) for d in complet["dead_links"]}
    assert projet["has_audit"] is True
