"""Un refus d'ENTRÉE du store est un 400 sur REST, jamais un 500 (#390).

Trouvé au smoke prod de v1.80.0, invisible aux tests du store : la garde `_id`
protégeait bien (rien n'était inséré), mais la face REST rendait « Internal Server
Error ». Le store lève `ValueError`, que `data_write` traduisait côté MCP
(`INVALID_PARAMS`) et que les routes n'attrapaient pas — deux faces du même métier,
une seule sachant dire pourquoi elle refuse.

Deux conséquences, et la seconde est la pire : l'appelant reçoit une erreur opaque
là où le message dit exactement quoi corriger, et Sentry compte une faute d'appel
comme un bug backend (le 500 remonte, le 400 non).

Couvre aussi la collision de clé métier sur PATCH, qui passait par le même trou.

⚠️ Écrire une ligne est passé en CAPACITÉ le 2026-08-12 (#302) — la traduction est
descendue avec (`_write_refusal`), et l'ordre des branches y est le contrat :
`RowValidationError` DÉRIVE de `ValueError`, l'inverser rendrait `invalid_row_input`
là où le cockpit attend `row_invalid`. Dernier test.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities.datastore import rows as dsr
from oto_mcp.datastore.core import RowValidationError


@pytest.fixture(autouse=True)
def _sans_db(monkeypatch):
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr.datastore_journal, "record", lambda *a, **k: None)


class _Store:
    """Store qui refuse comme le vrai : `ValueError` sur une entrée invalide."""

    MSG = ("`_id` ('019f-x') posé DANS `row` : il y serait ignoré et ton écriture "
           "INSÉRERAIT une nouvelle ligne au lieu de modifier celle-là.")

    def __init__(self, exc=None):
        self.exc = exc or ValueError(self.MSG)

    def append_row(self, namespace, data, *, trace=None, readonly_override=False, origine_override=False):
        raise self.exc

    def update_row(self, namespace, row_id, patch, *, trace=None, readonly_override=False, origine_override=False):
        raise self.exc

    def off_schema_report(self):
        return {}


def _store(monkeypatch, exc=None):
    monkeypatch.setattr(dsr, "make_store", lambda sub: _Store(exc))


def test_append_refusal_is_an_actionable_400(monkeypatch):
    _store(monkeypatch)
    status, corps = call("me.datastore.append_row", path_params={"namespace": "160"},
                         body={"_id": "019f-x", "statut": "e"})
    assert (status, corps["error"]) == (400, "invalid_row_input")
    # le message du store arrive JUSQU'À l'appelant : c'est ce qui rend la reprise
    # mécanique, et c'est précisément ce que le 500 mangeait.
    assert "INSÉRERAIT" in corps["detail"]


def test_patch_refusal_is_an_actionable_400(monkeypatch):
    _store(monkeypatch)
    status, corps = call("me.datastore.update_row",
                         path_params={"namespace": "160", "row_id": "row-1"},
                         body={"_id": "019f-autre"})
    assert (status, corps["error"]) == (400, "invalid_row_input")
    assert corps["detail"]


def test_business_key_collision_on_patch_is_also_a_400(monkeypatch):
    """Même trou, autre cause : `update_row` convertit la violation d'unicité en
    `ValueError` actionnable — elle ressortait en 500."""
    _store(monkeypatch, ValueError("un autre enregistrement porte déjà siren=111 "
                                   "(clé métier unique) — impossible de dupliquer"))
    status, corps = call("me.datastore.update_row",
                         path_params={"namespace": "160", "row_id": "row-1"},
                         body={"siren": "111"})
    assert (status, corps["error"]) == (400, "invalid_row_input")
    assert "clé métier unique" in corps["detail"]


def test_a_schema_refusal_keeps_its_own_code(monkeypatch):
    """`RowValidationError` dérive de `ValueError` : si la branche générique passait
    devant, le cockpit recevrait `invalid_row_input` là où il attend `row_invalid`
    (c'est ce code qui lui fait dire « ce retour n'est plus possible »)."""
    _store(monkeypatch, RowValidationError(["statut : transition 'clos' → 'neuf' "
                                            "non déclarée"]))
    status, corps = call("me.datastore.update_row",
                         path_params={"namespace": "160", "row_id": "row-1"},
                         body={"statut": "neuf"})
    assert (status, corps["error"]) == (400, "row_invalid")
    assert "transition" in corps["detail"]
