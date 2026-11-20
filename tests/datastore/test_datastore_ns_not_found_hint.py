"""Un 404 datastore dit OÙ vit le tableau quand il est dans une autre org (signal #316).

L'API REST résout le store sur l'org ACTIVE du porteur du token ; viser le tableau
d'une autre org demande l'en-tête `X-Oto-Org`, qui n'était nommé nulle part. Un
datastore bien réel répondait « datastore_not_found » — lu comme « il n'existe pas ».

⚠️ Le chemin de lecture est passé en CAPACITÉ le 2026-08-12 (#302). L'indice a suivi
(`capabilities/datastore/common.ns_not_found`) : ces tests le prouvent sur la nouvelle
chaîne, sans rien changer à ce qu'ils exigent — c'était la condition de la migration.
"""
import pytest

from _datastore_rest import Boom, call, stub_authz

from oto_mcp.capabilities.datastore import common as dc
from oto_mcp.datastore import hors_org
from oto_mcp.capabilities.datastore import rows as dsr
from oto_mcp.datastore.core import DatastoreNotFound


@pytest.fixture(autouse=True)
def _sans_db(monkeypatch):
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: Boom(DatastoreNotFound("nope")))


def _get_rows(monkeypatch, *, orgs, datastores, ns="42"):
    monkeypatch.setattr(hors_org.org_store, "list_orgs_for_user", lambda sub: orgs)
    monkeypatch.setattr(hors_org.db, "list_datastores_for_owners",
                        lambda owners: datastores)
    return call("me.datastore.list_rows", path_params={"datastore": ns})


def test_hint_names_the_org_and_the_header(monkeypatch):
    status, corps = _get_rows(
        monkeypatch,
        orgs=[{"org_id": 2, "name": "Otomata Admin"}, {"org_id": 81, "name": "Marché"}],
        datastores=[{"id": 42, "datastore": "leads-accords-dormants", "owner_type": "org",
                     "owner_id": "81"}],
    )
    assert (status, corps["error"]) == (404, "datastore_not_found")
    assert "X-Oto-Org: 81" in corps["detail"]
    assert "Marché" in corps["detail"]          # nommer l'org, pas seulement son id


def test_no_hint_when_the_datastore_exists_nowhere(monkeypatch):
    assert _get_rows(monkeypatch, orgs=[{"org_id": 2, "name": "Otomata Admin"}],
                     datastores=[]) == (404, {"error": "datastore_not_found",
                                              "detail": None})


def test_no_hint_for_another_datastore(monkeypatch):
    """On ne suggère que le datastore DEMANDÉ — pas un voisin qui traîne."""
    out = _get_rows(monkeypatch,
                    orgs=[{"org_id": 2, "name": "Otomata Admin"}],
                    datastores=[{"id": 7, "datastore": "autre-chose", "owner_type": "org",
                                 "owner_id": "2"}])
    assert out == (404, {"error": "datastore_not_found", "detail": None})


def test_lookup_failure_degrades_to_the_bare_404(monkeypatch):
    """Un indice ne doit jamais transformer un 404 en 500."""
    def boom(sub):
        raise RuntimeError("DB down")

    monkeypatch.setattr(hors_org.org_store, "list_orgs_for_user", boom)
    assert call("me.datastore.list_rows",
                path_params={"datastore": "999999999"}) == (
                    404, {"error": "datastore_not_found", "detail": None})


def test_only_orgs_the_caller_belongs_to_are_probed(monkeypatch):
    """L'indice ne doit rien révéler que le porteur ne puisse déjà lister : les
    owners interrogés sont EXACTEMENT ses orgs."""
    seen = {}

    def capture(owners):
        seen["owners"] = owners
        return []

    monkeypatch.setattr(hors_org.org_store, "list_orgs_for_user",
                        lambda sub: [{"org_id": 2, "name": "A"},
                                     {"org_id": 81, "name": "B"}])
    monkeypatch.setattr(hors_org.db, "list_datastores_for_owners", capture)
    call("me.datastore.list_rows", path_params={"datastore": "999999999"})
    assert seen["owners"] == [("org", "2"), ("org", "81")]
