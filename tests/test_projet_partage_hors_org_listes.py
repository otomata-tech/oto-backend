"""Projet PARTAGÉ à un non-membre : sous `_project=`, ni le catalogue ni les tableaux de l'org.

Trou relevé à la conception de l'accès invité. `_pin_project` co-pose l'org
propriétaire du projet comme org de l'appel, sans appartenance : c'est voulu (les slots,
l'identité épinglée du projet s'y résolvent), et aucun compte de cette org ne lui sert
(#1499, D1). Mais les seams de LISTE et de résolution lisaient encore
cette org comme celle de l'appelant :

- `data_list_datastores` rendait tout le catalogue de l'org (nom, propriétaire,
  schéma) — `principaux_de_liste` = `[("org", X)]` ;
- un tableau de l'org se lisait par son numéro (`_active_scope` = `[X]`, et la lecture
  ne repasse pas par `can_access`).

La règle : un bénéficiaire HORS de l'org (marque `session_org.current_call_hors_org`)
n'y a pour principal que lui-même — la pièce `("org", X)` sort de toutes les listes et
de la portée, par UN seam (`access.scope.org_d_appartenance`). Ce qui lui est partagé s'ouvre toujours ; un membre
de l'org n'y perd rien.

Base réelle, vraie pose de l'axe (`call_axes.PROJECT.pin`), vrais outils.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp import call_axes, session_org


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store, ownership
    u = uuid.uuid4().hex[:8]
    proprio, invite = f"proprio_{u}", f"invite_{u}"
    for s in (proprio, invite):
        db.upsert_user(s, email=f"{s}@example.test")
    x = org_store.create_org(f"x_{u}", created_by=proprio)
    org_store.add_org_member(x, proprio, "org_admin")
    b = org_store.create_org(f"b_{u}", created_by=invite)
    org_store.add_org_member(b, invite)
    tds = ownership.TYPE_RESSOURCE_DATASTORE
    t = {k: db.create_datastore("org", str(x), f"{k}_{u}")
         for k in ("un", "deux", "partage", "a_l_org_b")}
    ownership.grant(tds, str(t["partage"]), "user", invite, "read", granted_by=proprio)
    ownership.grant(tds, str(t["a_l_org_b"]), "org", str(b), "read", granted_by=proprio)
    pid = int(db.create_project("org", str(x), f"p_{u}", created_by=proprio))
    ownership.grant("project", str(pid), "user", invite, role="editor", granted_by=proprio)
    db.add_project_link(pid, "tableau", str(t["un"]), label="vivier", slot="vivier")
    autre = int(db.create_project("org", str(x), f"autre_{u}", created_by=proprio))
    return {"proprio": proprio, "invite": invite, "x": x, "b": b, "pid": pid,
            "autre": autre, "t": t}


def _sous_projet(m: dict, sub: str, monkeypatch, fn):
    """`fn()` dans un appel de `sub` portant `_project=<projet>` — la vraie pose."""
    from oto_mcp import access
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: sub)
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: sub)

    async def _appel():
        undo = await call_axes.PROJECT.pin(m["pid"])
        try:
            return fn()
        finally:
            for reset, tok in reversed(undo):
                reset(tok)
    return asyncio.run(_appel())


def _outil(nom: str):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as surface
    mcp = FastMCP("hors-org")
    surface.register(mcp)
    return asyncio.run(mcp.get_tool(nom)).fn


def _catalogue(m: dict, sub: str, monkeypatch) -> set[int]:
    lister = _outil("data_list_datastores")
    return {int(e["id"]) for e in _sous_projet(m, sub, monkeypatch,
                                               lambda: lister()["datastores"])}


def test_le_contexte_est_bien_celui_d_un_beneficiaire_hors_org(monde, monkeypatch):
    """Contrôle du banc : l'axe pose l'org du projet ET la marque « hors org »."""
    from oto_mcp import access
    org, hors = _sous_projet(monde, monde["invite"], monkeypatch, lambda: (
        access.current_org(monde["invite"]), session_org.current_call_hors_org()))
    assert org == monde["x"]
    assert hors == (monde["invite"], monde["x"])


# ── Le trou : rouge sur le code d'avant ────────────────────────────────────────

def test_la_liste_des_tableaux_ne_rend_pas_le_catalogue_de_l_org(monde, monkeypatch):
    vus = _catalogue(monde, monde["invite"], monkeypatch)
    fuite = vus & set(monde["t"].values())
    assert not fuite, f"catalogue de l'org servi à un non-membre : {sorted(fuite)}"


def test_un_tableau_de_l_org_ne_se_lit_pas_par_son_numero(monde, monkeypatch):
    lire = _outil("data_rows")
    for k in ("un", "deux", "a_l_org_b"):
        with pytest.raises(Exception) as e:
            _sous_projet(monde, monde["invite"], monkeypatch,
                         lambda k=k: lire(datastore=str(monde["t"][k])))
            pytest.fail(f"tableau `{k}` de l'org lu par un non-membre")
        # Le refus ordinaire d'un tableau hors de portée : il n'en confirme pas l'existence.
        assert "unknown" in str(e.value), str(e.value)


def test_le_slot_du_projet_ne_rouvre_pas_un_tableau_non_partage(monde, monkeypatch):
    """Le projet est partagé, pas les tableaux qu'il lie : `slot:` LOCALISE, il ne
    donne aucun droit (même règle que le bail de run, #631)."""
    lire = _outil("data_rows")
    with pytest.raises(Exception):
        _sous_projet(monde, monde["invite"], monkeypatch,
                     lambda: lire(datastore="slot:vivier"))
        pytest.fail("tableau lié au projet lu par un non-membre sans partage")


def test_les_seams_de_liste_ne_rendent_pas_l_org(monde, monkeypatch):
    """Les autres listes lisent les mêmes seams : la recherche (« cherchable ⇔
    lisible ») et les projets ne rendent rien de l'org non plus."""
    from oto_mcp import ownership, search
    sub, x = monde["invite"], monde["x"]
    principaux, tableaux, projets = _sous_projet(monde, sub, monkeypatch, lambda: (
        ownership.principaux_de_liste(sub, x),
        {int(r["id"]) for r in search._accessible_namespaces(sub, x)},
        set(ownership.accessible_project_ids(sub, x))))
    assert ("org", str(x)) not in principaux
    assert not tableaux & set(monde["t"].values())
    assert monde["autre"] not in projets, "projet de l'org non partagé, listé"


# ── Ce qui ne bouge pas ────────────────────────────────────────────────────────

def test_ce_qui_lui_est_partage_s_ouvre_toujours(monde, monkeypatch):
    lire = _outil("data_rows")
    r = _sous_projet(monde, monde["invite"], monkeypatch,
                     lambda: lire(datastore=str(monde["t"]["partage"])))
    assert int(r["ns_id"]) == monde["t"]["partage"]


def test_un_membre_de_l_org_voit_toujours_son_catalogue(monde, monkeypatch):
    vus = _catalogue(monde, monde["proprio"], monkeypatch)
    assert set(monde["t"].values()) <= vus
    lire = _outil("data_rows")
    r = _sous_projet(monde, monde["proprio"], monkeypatch,
                     lambda: lire(datastore="slot:vivier"))
    assert int(r["ns_id"]) == monde["t"]["un"]


def test_les_listes_hors_data_ne_portent_pas_l_axe_projet():
    """Projets, pages, procédures, recherche : l'axe `_project=` ne s'y applique pas
    (il est retiré sans effet) — aucune pose d'org de projet n'y entre."""
    for nom in ("oto_project", "oto_doc", "oto_procedure", "oto_search", "oto_node"):
        assert call_axes.PROJECT not in call_axes.axes_for_call(nom), nom
