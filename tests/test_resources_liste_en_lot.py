"""La LISTE des tableaux (`op=list`, `datastore_namespace`) se lit EN LOT.

Avant : un `count(*)` et une lecture du propriétaire PAR tableau, chacun sur sa
connexion du pool — sur une liste de plateforme (tous les tableaux), des milliers de
requêtes et des dizaines de secondes par affichage de l'écran qui la demande.

Ce qui est tenu ici :
- la liste rend EXACTEMENT ce que rendait l'enrichissement tableau par tableau ;
- les nombres de lignes viennent d'UNE lecture en lot, jamais du compte unitaire ;
- les comptes propriétaires se résolvent par lots de 200 au plus, une org distincte
  une seule fois ;
- `op=get` (un tableau) garde son chemin unitaire.
"""
from __future__ import annotations

import pytest

from oto_mcp import ownership
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)


def _rows(n_users=250, n_orgs=3):
    rows = []
    for i in range(1, n_users + 1):
        rows.append({"id": i, "datastore": f"t{i}", "owner_type": "user",
                     "owner_id": f"u{i % 230}", "created_at": "2026-11-20 10:00:00"})
    for j in range(n_orgs):
        for k in range(4):
            rows.append({"id": 1000 + j * 10 + k, "datastore": f"o{j}{k}",
                         "owner_type": "org", "owner_id": str(j + 1),
                         "created_at": "2026-11-20 10:00:00"})
    rows.append({"id": 2000, "datastore": "g", "owner_type": "group", "owner_id": "5",
                 "created_at": "2026-11-20 10:00:00"})
    return rows


@pytest.fixture
def base(monkeypatch):
    vu = {"unitaire": 0, "lots": [], "emails": [], "orgs": []}
    comptes = {i: i * 3 for i in range(1, 300)}

    def unitaire(ns_id):
        vu["unitaire"] += 1
        return comptes.get(ns_id, 0)

    def en_lot(ids):
        ids = list(ids)
        vu["lots"].append(len(ids))
        return {i: comptes.get(i, 0) for i in ids}

    def emails(subs):
        assert len(subs) <= 200
        vu["emails"].append(len(subs))
        return {s: f"{s}@x.co" for s in subs if s != "u7"}   # u7 : compte sans email

    def get_user(sub):
        return {"email": f"{sub}@x.co"} if sub != "u7" else {"email": None}

    def get_org(oid):
        vu["orgs"].append(oid)
        return {"name": f"org {oid}"}

    monkeypatch.setattr(R.db, "count_datastore_rows_for_ns", unitaire)
    monkeypatch.setattr(R.db, "count_datastore_rows_for_ns_ids", en_lot)
    monkeypatch.setattr(R.db, "emails_by_subs", emails)
    monkeypatch.setattr(R.db, "get_user", get_user)
    monkeypatch.setattr(R.org_store, "get_org", get_org)
    monkeypatch.setattr(R.group_store, "get_group", lambda gid: {"name": "pôle data"})
    return vu


def test_le_lot_rend_exactement_l_enrichissement_unitaire(base):
    rows = _rows()
    attendu = [R._enrich_datastore(r) for r in rows]
    base["unitaire"] = 0
    assert R._enrich_datastores(rows) == attendu


def test_la_liste_ne_compte_jamais_tableau_par_tableau(base, monkeypatch):
    rows = _rows()
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(R.db, "list_all_datastores", lambda: rows)
    out = R._resources(CTX, R.ResourceInput(op="list", resource_type="datastore_namespace"))
    assert len(out["resources"]) == len(rows)
    assert base["unitaire"] == 0
    assert base["lots"] == [len(rows)]
    assert all(n <= 200 for n in base["emails"]) and sum(base["emails"]) == 230
    assert sorted(base["orgs"]) == [1, 2, 3]                # une fois par org distincte
    r7 = next(r for r in out["resources"] if r["owner_id"] == "u7")
    assert r7["owner_label"] is None                         # comme `_owner_label`


def test_la_liste_d_un_membre_passe_aussi_par_le_lot(base, monkeypatch):
    rows = _rows(n_users=3, n_orgs=0)
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.ownership, "accessor_scope",
                        lambda sub: ownership.AccessorScope(sub, [], []))
    monkeypatch.setattr(R.db, "list_datastores_for_owners", lambda owners: rows)
    out = R._resources(CTX, R.ResourceInput(op="list", resource_type="datastore_namespace"))
    assert [r["row_count"] for r in out["resources"]] == [3, 6, 9, 0]
    assert base["unitaire"] == 0


def test_get_garde_le_chemin_unitaire(base):
    r = R._enrich_datastore(_rows(n_users=1, n_orgs=0)[0])
    assert r["row_count"] == 3 and base["unitaire"] == 1 and base["lots"] == []


def test_lot_vide():
    from oto_mcp.db import datastore_ns
    assert datastore_ns.count_datastore_rows_for_ns_ids([]) == {}
