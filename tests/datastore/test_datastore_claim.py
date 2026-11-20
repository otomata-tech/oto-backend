"""Réserver une ligne depuis une application web (signal #362).

`claim_next` et le bail existaient côté MCP seulement : un front pouvait lire la
file et libérer, jamais RÉSERVER — d'où des verrous réécrits dans les données de la
ligne, non atomiques. Ces tests figent les trois gestes et leurs gardes : réserver
la suivante, réserver une ligne nommée, et ne pas libérer le bail d'un autre.

Les deux claims sont des CAPACITÉS (une surface neuve naît capacité) → handlers
appelés en direct. La LIBÉRATION l'est devenue à son tour le 2026-08-12 (#302) :
elle s'exerce ici par sa route, sur la vraie chaîne de l'adaptateur REST — c'est là
que vivent son corps optionnel et son refus de jeton porté. Seams PG monkeypatchés
(le chemin SQL est vérifié au deploy).
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.datastore import core as D
from oto_mcp.auth import token_scopes
from oto_mcp.capabilities.datastore import claim as dsc
from oto_mcp.capabilities.datastore import rows as dsr
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.datastore.core import DatastoreNotFound, RowClaimed, RowNotFound

# ── Le store : réserver une ligne NOMMÉE ─────────────────────────────────────

def _store(monkeypatch, ns_id=7):
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: ns_id)
    monkeypatch.setattr(s, "_ns_of", lambda _id: {"datastore": "vivier", "schema": None})
    return s


def test_claim_row_returns_the_row_with_its_lease(monkeypatch):
    seen = {}

    def _claim(ns_id, row_id, *, worker, lease_seconds, **k):
        seen.update(ns_id=ns_id, row_id=row_id, worker=worker, lease=lease_seconds)
        # ⚠️ Le faux rend EXACTEMENT ce que `db.datastore_claim_row` rend, `claimed_run`
        # compris : un faux qui projette moins que le vrai fait passer au vert du code
        # qui lève en production.
        return {"row_id": "r1", "created_at": "c", "updated_at": "u",
                "data": {"nom": "ACME"}, "claimed_by": "sarah",
                "claimed_until": "t+15", "claimed_run": None,
                # La fraîcheur du bail vient désormais du SELECT (`claim_active`) :
                # un faux qui l'omet lève, exactement comme en production.
                "claim_active": True}

    monkeypatch.setattr(D.db, "datastore_claim_row", _claim)
    out = _store(monkeypatch).claim_row("vivier", "r1", worker="sarah", lease_s=300)
    assert out == {"_id": "r1", "_created_at": "c", "_updated_at": "u", "nom": "ACME",
                   "_claimed_by": "sarah", "_claimed_until": "t+15",
                   # Réservation à la main (pas de `_run_id`) : `null` DIT « ce bail
                   # n'appartient à aucun run », il ne l'omet pas.
                   "_claimed_run": None}
    assert (seen["ns_id"], seen["row_id"], seen["worker"], seen["lease"]) == (7, "r1", "sarah", 300)


def test_claim_row_distinguishes_absent_from_taken(monkeypatch):
    """Un `None` commun ne dit pas à l'utilisateur ce qui s'est passé : ligne
    disparue ou collègue plus rapide sont deux réponses différentes."""
    monkeypatch.setattr(D.db, "datastore_claim_row", lambda *a, **k: None)
    monkeypatch.setattr(D.db, "datastore_get_row", lambda ns_id, row_id: None)
    with pytest.raises(RowNotFound):
        _store(monkeypatch).claim_row("vivier", "r1", worker="sarah")

    monkeypatch.setattr(D.db, "datastore_get_row", lambda ns_id, row_id: {
        "row_id": "r1", "claimed_by": "jules", "claimed_until": "t+9"})
    with pytest.raises(RowClaimed) as e:
        _store(monkeypatch).claim_row("vivier", "r1", worker="sarah")
    assert (e.value.claimed_by, e.value.claimed_until) == ("jules", "t+9")


def test_claim_row_requires_a_worker(monkeypatch):
    """Sans libellé de porteur, le bail n'a plus de garde au release."""
    monkeypatch.setattr(D.db, "datastore_claim_row", lambda *a, **k: {})
    with pytest.raises(ValueError):
        _store(monkeypatch).claim_row("vivier", "r1", worker="   ")


def test_claim_row_needs_write_access(monkeypatch):
    seen = {}

    def _resolve(ns, write=False):
        seen["write"] = write
        return 7

    monkeypatch.setattr(D.db, "datastore_claim_row", lambda *a, **k: {
        "row_id": "r1", "created_at": "c", "updated_at": "u", "data": {},
        "claimed_by": "sarah", "claimed_until": "t", "claimed_run": None,
        "claim_active": True})
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", _resolve)
    monkeypatch.setattr(s, "_ns_of", lambda _id: {"datastore": "vivier", "schema": None})
    s.claim_row("vivier", "r1", worker="sarah")
    assert seen["write"] is True    # poser un bail = écrire, un partage en lecture ne réserve pas


def test_claims_report_the_configuration_that_breaks_auto_release(monkeypatch):
    """Le warning de `data_claim_next` vaut pour les DEUX claims : la ligne d'un
    tableau sans état terminal ne se libère jamais toute seule."""
    monkeypatch.setattr(D.db, "datastore_claim_row", lambda *a, **k: {
        "row_id": "r1", "created_at": "c", "updated_at": "u", "data": {},
        "claimed_by": "sarah", "claimed_until": "t", "claimed_run": None,
        "claim_active": True})
    monkeypatch.setattr(D.dsv2, "queue_release_warning", lambda schema: "pas d'état terminal")
    warnings: list = []
    _store(monkeypatch).claim_row("vivier", "r1", worker="sarah", warnings=warnings)
    assert warnings == ["pas d'état terminal"]


# ── Les capacités : réserver depuis un front ─────────────────────────────────

class _Store:
    """Store de test : enregistre les appels, rejoue les réponses programmées."""
    # Relevé de résolution du store (`DatastorePg.dernier_tableau`) : les
    # remises y prennent l'IDENTITÉ du tableau — nom canonique + `ns_id`.
    dernier_tableau = {"ns_id": 174, "datastore": "vivier"}

    def __init__(self, **outcomes):
        self.outcomes = outcomes
        self.calls: list = []

    def _play(self, name, kwargs):
        self.calls.append((name, kwargs))
        out = self.outcomes.get(name)
        if isinstance(out, Exception):
            raise out
        return out

    def claim_next(self, datastore, **k):
        return self._play("claim_next", {"datastore": datastore, **k})

    def claim_row(self, datastore, row_id, **k):
        return self._play("claim_row", {"datastore": datastore, "row_id": row_id, **k})

    def release_claim(self, datastore, row_id, **k):
        return self._play("release_claim", {"datastore": datastore, "row_id": row_id, **k})

    def force_release(self, datastore, row_id, **k):
        return self._play("force_release", {"datastore": datastore, "row_id": row_id, **k})


def _cap(monkeypatch, store, handler, Input, **fields):
    monkeypatch.setattr(dsc, "make_store", lambda sub: store)
    monkeypatch.setattr(dsc.datastore_journal, "record", lambda *a, **k: None)
    return handler(ResolvedCtx(sub="u-1"), Input(**fields))


def _claim_next(monkeypatch, store, **fields):
    return _cap(monkeypatch, store, dsc._claim_next, dsc.ClaimNextInput, **fields)


def _claim_row(monkeypatch, store, **fields):
    return _cap(monkeypatch, store, dsc._claim_row, dsc.ClaimRowInput, **fields)


ROW = {"_id": "r1", "nom": "ACME", "_claimed_by": "sarah", "_claimed_until": "t+15"}


def test_claim_next_reserves_and_returns_the_row(monkeypatch):
    store = _Store(claim_next=ROW)
    out = _claim_next(monkeypatch, store, datastore="vivier", worker="sarah",
                      filter={"statut": "a-appeler"}, lease_s=300)
    # La remise porte l'IDENTITÉ du tableau : son nom canonique ET son numéro —
    # c'est ici que l'agent apprend la forme d'adresse qui remplace le nom.
    # `namespace` est doublée le temps du préavis (08/11/2026) : c'est la seule
    # clé que le renommage RETIRAIT, donc la seule panne muette de la bascule —
    # un consommateur qui la lit recevrait `undefined`, sans erreur ni journal.
    assert out == {"datastore": "vivier",
                   "ns_id": 174, "row": ROW}
    _, kw = store.calls[0]
    assert (kw["worker"], kw["filter"], kw["lease_s"]) == ("sarah", {"statut": "a-appeler"}, 300)


def test_claim_next_on_an_empty_queue_says_so(monkeypatch):
    out = _claim_next(monkeypatch, _Store(claim_next=None), datastore="vivier", worker="sarah")
    assert out["row"] is None
    assert "hint" in out           # file vide ≠ erreur, mais ça se dit


def test_claim_lease_is_optional_and_defaults_server_side(monkeypatch):
    """Ne pas relayer un `lease_s` absent : le défaut appartient au store, pas au front."""
    store = _Store(claim_next=ROW)
    _claim_next(monkeypatch, store, datastore="vivier", worker="sarah")
    assert "lease_s" not in store.calls[0][1]


def test_claim_next_reports_the_configuration_warning(monkeypatch):
    store = _Store(claim_next=ROW)

    def _claim(datastore, **k):
        k["warnings"].append("statut sans état terminal")
        return ROW

    monkeypatch.setattr(store, "claim_next", _claim)
    assert _claim_next(monkeypatch, store, datastore="vivier",
                       worker="sarah")["warning"] == "statut sans état terminal"


@pytest.mark.parametrize("worker", ["", "   "])
def test_claim_without_worker_is_refused(monkeypatch, worker):
    """`worker` EST la garde du bail — sans lui, la file redevient coopérative."""
    store = _Store()
    for handler in (
        lambda: _claim_next(monkeypatch, store, datastore="vivier", worker=worker),
        lambda: _claim_row(monkeypatch, store, datastore="vivier", row_id="r1", worker=worker),
    ):
        with pytest.raises(AuthzDenied) as e:
            handler()
        assert (e.value.status, e.value.code) == (400, "worker_required")
        assert "release" in e.value.message      # un refus qui dit à quoi sert le champ
    assert store.calls == []                     # rien n'a été réservé


def test_claim_row_conflict_names_who_holds_the_lease(monkeypatch):
    store = _Store(claim_row=RowClaimed("r1", "jules", "t+9"))
    with pytest.raises(AuthzDenied) as e:
        _claim_row(monkeypatch, store, datastore="vivier", row_id="r1", worker="sarah")
    assert (e.value.status, e.value.code) == (409, "row_claimed")
    assert "jules" in e.value.message


def test_claim_row_404_when_the_row_is_gone(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        _claim_row(monkeypatch, _Store(claim_row=RowNotFound("r1")),
                   datastore="vivier", row_id="r1", worker="sarah")
    assert (e.value.status, e.value.code) == (404, "row_not_found")


def test_claim_on_an_unknown_datastore_is_a_404(monkeypatch):
    """Un tableau hors périmètre ne se distingue pas d'un tableau inexistant."""
    with pytest.raises(AuthzDenied) as e:
        _claim_next(monkeypatch, _Store(claim_next=DatastoreNotFound("nope")),
                    datastore="inconnu", worker="sarah")
    assert (e.value.status, e.value.code) == (404, "datastore_not_found")


def test_claim_on_a_read_only_share_is_refused(monkeypatch):
    """Poser un bail est une écriture : un tableau partagé en lecture ne se réserve pas."""
    with pytest.raises(AuthzDenied) as e:
        _claim_next(monkeypatch, _Store(claim_next=D.DatastoreReadOnly("vivier")),
                    datastore="vivier", worker="sarah")
    assert (e.value.status, e.value.code) == (403, "datastore_read_only")


def test_the_claims_are_rest_only_capabilities():
    """Opt-out MCP explicite : `data_claim_next` tient déjà la face agent."""
    caps = {c.key: c for c in dsc.CAPABILITIES if c.key.startswith("me.datastore.claim")}
    assert set(caps) == {"me.datastore.claim_next", "me.datastore.claim_row"}
    for cap in caps.values():
        assert cap.mcp is None
        assert [b.verb for b in cap.rest_bindings()] == ["POST"]
        assert cap.Output is not None     # la forme de la réponse est déclarée


# ── Release : deux régimes, selon ce que l'appelant sait ──────────────────────
# Capacité depuis #302 (`me.datastore.release_claim`), même chemin qu'avant.

def _call(monkeypatch, store, body, *, no_body=False):
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: store)
    monkeypatch.setattr(dsr.datastore_journal, "record", lambda *a, **k: None)
    return call("me.datastore.release_claim",
                path_params={"datastore": "174", "row_id": "r1"},
                body=body, no_body=no_body)


# ⚠️ `release_claim` rend `{released, reason, lease}` depuis le 29/08 (#517) : le
# booléen seul mêlait « rien à rendre » et « la ligne est tenue par un autre ».
_LIBERE = {"released": True, "reason": None, "lease": None}
_AUTRE = {"released": False, "reason": "held_by_other",
          "lease": {"claimed_by": "w-9", "claimed_until": "2026-08-29 18:00:00"}}


def test_release_with_a_worker_is_guarded(monkeypatch):
    store = _Store(release_claim=_LIBERE)
    status, _ = _call(monkeypatch, store, {"worker": "sarah"})
    assert status == 200
    name, kw = store.calls[0]
    assert (name, kw["worker"]) == ("release_claim", "sarah")   # pas force_release


def test_guarded_release_of_someone_elses_lease_changes_nothing(monkeypatch):
    store = _Store(release_claim=_AUTRE)
    status, payload = _call(monkeypatch, store, {"worker": "sarah"})
    assert status == 200
    assert payload["released"] is False
    # La réponse NOMME le cas et dit qui tient la ligne — elle ne mêle plus « rien à
    # rendre » et « bail d'un autre » dans une seule phrase (#517).
    assert payload["reason"] == "held_by_other"
    assert "w-9" in payload["hint"]


def test_release_without_body_is_the_supervision_gesture(monkeypatch):
    """Le dashboard (session interactive) garde la libération forcée d'un bail bloqué.
    Il poste SANS corps : la capacité doit s'en accommoder, pas exiger `{}`."""
    store = _Store(force_release=True)
    token_scopes.set_current(None)
    status, _ = _call(monkeypatch, store, None, no_body=True)
    assert status == 200
    assert store.calls[0][0] == "force_release"


def test_a_scoped_token_cannot_force_release(monkeypatch):
    """Un jeton porté = une intégration multi-utilisateurs : y laisser la
    libération forcée, c'est laisser chacun retirer la ligne de son collègue."""
    store = _Store(force_release=True)
    token_scopes.set_current({"datastores": {"vivier": "write"}})
    try:
        status, corps = _call(monkeypatch, store, {})
    finally:
        token_scopes.set_current(None)
    assert (status, corps["error"]) == (400, "worker_required")
    assert store.calls == []


def test_a_scoped_token_releases_its_own_lease(monkeypatch):
    store = _Store(release_claim=_LIBERE)
    token_scopes.set_current({"datastores": {"vivier": "write"}})
    try:
        status, _ = _call(monkeypatch, store, {"worker": "sarah"})
    finally:
        token_scopes.set_current(None)
    assert status == 200
    assert store.calls[0][0] == "release_claim"
