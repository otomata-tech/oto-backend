"""Les lignes, en capacité : mêmes chemins, mêmes réponses, mêmes refus (#302).

Huit chemins ont quitté les routes écrites à la main pour
`capabilities/datastore/rows.py`. Ces tests font tourner la vraie chaîne de
l'adaptateur REST et lisent ce qui part sur le fil — c'est la seule preuve qui vaille
pour une migration qui promet d'être invisible.

Trois points valaient une attention particulière, parce qu'ils ne survivaient PAS à
une migration naïve :

1. le CORPS d'un ajout ou d'une modification est la ligne elle-même (colonnes libres) ;
2. `?limit=beaucoup` retombait sur le défaut, il ne cassait pas la requête ;
3. les paramètres JSON de la query string ont chacun leur refus nommé
   (`invalid_filters` / `invalid_metrics` / `invalid_filter`), que le cockpit
   distingue.

Le journal du datastore, les refus d'écriture actionnables et l'indice du 404 ont
leurs propres fichiers (`test_datastore_activity_journal`, `…_rest_input_errors`,
`…_ns_not_found_hint`), rejoués eux aussi sur cette chaîne.
"""
from __future__ import annotations

import pytest

from _datastore_rest import Boom, call, cap, stub_authz

from oto_mcp.capabilities.datastore import rows as dsr
from oto_mcp.datastore.core import NamespaceNotFound, NamespaceReadOnly, RowNotFound

NS = {"namespace": "vivier"}
ROW = {"namespace": "vivier", "row_id": "r1"}


class _Store:
    """Enregistre l'appel, rend ce que le test a posé."""

    def __init__(self, **verdicts):
        self.v = verdicts
        self.calls: list = []

    def _out(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        out = self.v.get(name)
        if isinstance(out, Exception):
            raise out
        return out

    def page_rows(self, ns, **kw):
        return self._out("page_rows", ns, **kw) or {"rows": [], "total": 0,
                                                    "offset": kw["offset"],
                                                    "limit": kw["limit"]}

    def aggregate(self, ns, **kw):
        return self._out("aggregate", ns, **kw) or []

    def queue(self, ns):
        return self._out("queue", ns) or []

    def get_row(self, ns, row_id, *, layers="flat"):
        return self._out("get_row", ns, row_id)

    def append_row(self, ns, data, *, trace=None, readonly_override=False):
        self._out("append_row", ns, data)
        return {"_id": "r9", "_created_at": "2026-08-12 09:00:00", **data}

    def update_row(self, ns, row_id, patch, *, trace=None, readonly_override=False):
        self._out("update_row", ns, row_id, patch)
        return {"_id": row_id, "_updated_at": "2026-08-12 09:00:00", **patch}

    def delete_row(self, ns, row_id, *, trace=None):
        return self._out("delete_row", ns, row_id)

    # #658 : la surface REST relit ce relevé pour sa ligne de journal.
    off_forced: list = []

    def off_schema_report(self):
        return self.v.get("off_schema_report") or {}


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: s)
    monkeypatch.setattr(dsr.datastore_journal, "record", lambda *a, **k: None)
    return s


def _boom(monkeypatch, exc):
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: Boom(exc))


# --- la page ---------------------------------------------------------------------

def test_les_defauts_de_pagination_sont_ceux_davant(store):
    call("me.datastore.list_rows", path_params=NS)
    _, _, kw = store.calls[0]
    assert (kw["offset"], kw["limit"], kw["order_dir"]) == (0, 50, "desc")
    assert kw["order_by"] is None and kw["q"] is None and kw["filters"] is None


@pytest.mark.parametrize("query,attendu", [
    (b"limit=10&offset=20", (20, 10)),
    (b"limit=9000", (0, 500)),        # borné à 500
    (b"limit=0", (0, 1)),             # borné à 1
    (b"offset=-5", (0, 50)),          # jamais négatif
    (b"limit=beaucoup", (0, 50)),     # un entier illisible retombe sur le défaut…
    (b"offset=", (0, 50)),            # …et une valeur vide aussi
])
def test_les_bornes_de_pagination_sont_reproduites(store, query, attendu):
    """`?limit=beaucoup` rendait une page, pas un 400 : typer `int` sec aurait changé
    ça sans que rien ne le dise."""
    call("me.datastore.list_rows", path_params=NS, query=query)
    _, _, kw = store.calls[0]
    assert (kw["offset"], kw["limit"]) == attendu


def test_la_page_rend_lenveloppe_du_store(monkeypatch, store):
    store.v["page_rows"] = {"rows": [{"_id": "r1"}], "total": 1, "offset": 0,
                            "limit": 50}
    assert call("me.datastore.list_rows", path_params=NS)[1]["total"] == 1


def test_des_filtres_illisibles_sont_un_refus_nomme(store):
    status, corps = call("me.datastore.list_rows", path_params=NS,
                         query=b"filters=%7Bpas-du-json")
    assert (status, corps["error"]) == (400, "invalid_filters")
    assert store.calls == []


def test_des_filtres_qui_ne_sont_pas_une_liste_sont_refuses(store):
    status, corps = call("me.datastore.list_rows", path_params=NS,
                         query=b"filters=%7B%22a%22%3A1%7D")
    assert (status, corps["error"]) == (400, "invalid_filters")


def test_un_refus_du_store_sur_les_filtres_reste_un_400(monkeypatch):
    _boom(monkeypatch, ValueError("colonne inconnue"))
    status, corps = call("me.datastore.list_rows", path_params=NS)
    assert (status, corps["error"]) == (400, "invalid_filters")


# --- l'agrégat -------------------------------------------------------------------

def test_lagregat_decode_ses_trois_parametres_json(store):
    call("me.datastore.aggregate", path_params=NS,
         query=b"group_by=statut&metrics=%5B%7B%22op%22%3A%22count%22%7D%5D"
               b"&filter=%7B%22statut%22%3A%22neuf%22%7D"
               b"&filters=%5B%7B%22field%22%3A%22a%22%7D%5D&q=acme")
    _, _, kw = store.calls[0]
    assert kw["group_by"] == "statut"
    assert kw["metrics"] == [{"op": "count"}]
    assert kw["filter"] == {"statut": "neuf"}
    assert kw["filters"] == [{"field": "a"}]
    assert kw["q"] == "acme"


@pytest.mark.parametrize("query,code", [
    (b"metrics=%7Bpas-du-json", "invalid_metrics"),
    (b"filter=%5B1%5D", "invalid_filter"),        # une liste, pas un objet
    (b"filters=%7B%22a%22%3A1%7D", "invalid_filters"),
])
def test_chaque_parametre_json_garde_son_refus(store, query, code):
    """Trois codes distincts : les fondre en un seul `invalid_input` ferait perdre au
    cockpit la capacité de dire QUEL paramètre est en cause."""
    assert call("me.datastore.aggregate", path_params=NS, query=query)[1]["error"] == code


def test_lagregat_rend_lenveloppe_groups(store):
    store.v["aggregate"] = [{"statut": "neuf", "count": 3}]
    assert call("me.datastore.aggregate", path_params=NS) == (
        200, {"groups": [{"statut": "neuf", "count": 3}]})


def test_un_agregat_refuse_par_le_store_est_un_400(monkeypatch):
    _boom(monkeypatch, ValueError("métrique inconnue"))
    assert call("me.datastore.aggregate", path_params=NS)[1]["error"] == "invalid_aggregate"


# --- la file ---------------------------------------------------------------------

def test_la_file_rend_lenveloppe_rows(store):
    store.v["queue"] = [{"_id": "r1", "_claimed_by": "sarah"}]
    assert call("me.datastore.queue", path_params=NS) == (
        200, {"rows": [{"_id": "r1", "_claimed_by": "sarah"}]})


# --- la fiche --------------------------------------------------------------------

def test_la_fiche_rend_la_ligne_telle_quelle(store):
    store.v["get_row"] = {"_id": "r1", "societe": "ACME"}
    assert call("me.datastore.get_row", path_params=ROW) == (
        200, {"_id": "r1", "societe": "ACME"})


def test_une_ligne_absente_est_un_404_nomme(monkeypatch):
    _boom(monkeypatch, RowNotFound("r1"))
    assert call("me.datastore.get_row", path_params=ROW)[1]["error"] == "row_not_found"


# --- écrire ----------------------------------------------------------------------

def test_ajouter_une_ligne_rend_201_et_le_corps_est_la_ligne(store):
    status, corps = call("me.datastore.append_row", path_params=NS,
                         body={"societe": "ACME", "statut": "à traiter"})
    assert status == 201                       # le code d'avant la migration
    assert corps["societe"] == "ACME" and corps["_id"] == "r9"
    _, args, _ = store.calls[0]
    assert args[1] == {"societe": "ACME", "statut": "à traiter"}


def test_le_releve_hors_schema_accompagne_lecriture(store):
    """#294 : les deux faces signalent les mêmes champs hors format, ou elles mentent
    l'une des deux."""
    store.v["off_schema_report"] = {"hors_schema": ["actualite_sociale"],
                                    "hors_schema_hint": "colonne absente du schéma"}
    corps = call("me.datastore.append_row", path_params=NS, body={"a": 1})[1]
    assert corps["hors_schema"] == ["actualite_sociale"]


def test_modifier_une_ligne_passe_le_patch_entier(store):
    status, corps = call("me.datastore.update_row", path_params=ROW,
                         body={"statut": "ecarte"})
    assert status == 200 and corps["statut"] == "ecarte"
    _, args, _ = store.calls[0]
    assert args[2] == {"statut": "ecarte"}


def test_supprimer_une_ligne_rend_ok_et_lid(store):
    assert call("me.datastore.delete_row", path_params=ROW) == (
        200, {"ok": True, "id": "r1"})


# Les huit chemins et l'appel minimal qui les atteint (le corps d'un ajout/patch est
# la ligne elle-même ; le reste n'a que ses params de chemin).
_TOUS = [
    ("me.datastore.list_rows", NS, None),
    ("me.datastore.queue", NS, None),
    ("me.datastore.aggregate", NS, None),
    ("me.datastore.get_row", ROW, None),
    ("me.datastore.append_row", NS, {"a": 1}),
    ("me.datastore.update_row", ROW, {"a": 1}),
    ("me.datastore.delete_row", ROW, None),
    ("me.datastore.release_claim", ROW, {}),
]


_ECRITURES = [c for c in _TOUS
              if c[0].endswith(("append_row", "update_row", "delete_row"))]


@pytest.mark.parametrize("cle,params,corps", _ECRITURES)
def test_un_tableau_en_lecture_seule_refuse_lecriture(monkeypatch, cle, params, corps):
    _boom(monkeypatch, NamespaceReadOnly("vivier"))
    status, out = call(cle, path_params=params, body=corps)
    assert (status, out["error"]) == (403, "namespace_read_only")


@pytest.mark.parametrize("cle,params,corps", _TOUS)
def test_un_tableau_hors_perimetre_est_un_404_partout(monkeypatch, cle, params, corps):
    """Le vrai gate n'est pas la garde de capacité (`SUB_ONLY`) mais le store : org
    active + ownership. Un tableau qu'on n'a pas le droit de voir ne se distingue pas
    d'un tableau inexistant — sur les huit chemins, sans exception."""
    _boom(monkeypatch, NamespaceNotFound("vivier"))
    status, out = call(cle, path_params=params, body=corps)
    assert (status, out["error"]) == (404, "namespace_not_found")


# --- le contrat ------------------------------------------------------------------

def test_les_deux_corps_libres_sont_declares_comme_tels():
    """Sans ça, la garde de champ inconnu refuserait chaque colonne du tableau."""
    assert cap("me.datastore.append_row").rest_bindings()[0].body_field == "row"
    assert cap("me.datastore.update_row").rest_bindings()[0].body_field == "patch"


def test_une_colonne_qui_sappelle_comme_un_champ_dapi_reste_une_donnee(store):
    """Un tableau peut avoir une colonne « namespace » ou « limit » : elle appartient
    à l'utilisateur, elle ne doit jamais être confondue avec un paramètre."""
    call("me.datastore.append_row", path_params=NS,
         body={"namespace": "pas le tableau", "limit": 3})
    _, args, _ = store.calls[0]
    assert args[0] == "vivier"                       # le chemin gagne
    assert args[1] == {"namespace": "pas le tableau", "limit": 3}


def test_les_huit_capacites_declarent_leur_sortie_et_restent_rest_only():
    for cle in ("list_rows", "append_row", "get_row", "update_row", "delete_row",
                "release_claim", "queue", "aggregate"):
        c = cap(f"me.datastore.{cle}")
        assert c.Output is not None, cle
        assert c.mcp is None, f"{cle} : ce lot ne migre que la face REST"
