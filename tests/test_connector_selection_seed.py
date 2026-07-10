"""Socle curé + backfill de transition (ADR 0050).

Logique pure + SQL simulé par un faux conn (convention du repo : le chemin PG
réel est prouvé au déploiement) : le seed d'un nouveau (sub, org) reçoit le
SOCLE `default_active`, le backfill one-shot reconstitue le VISIBLE d'avant
(exposé − ex-default_hidden) et ne rejoue jamais (sentinelle).
"""
from oto_mcp import connector_selection, providers

# Tripwire de curation : le socle est un choix PRODUIT explicite — le faire
# évoluer = éditer cette liste EN MÊME TEMPS que le registre (pas d'ajout qui
# élargit le départ de tous les nouveaux comptes par accident).
_SOCLE = {
    "serper", "serpapi", "sirene", "droit", "foncier", "urba", "sante", "osm",
    "google", "hunter", "kaspr", "fullenrich", "apollo", "zerobounce",
    "unipile", "folk", "slack", "infosec",
}


def test_default_active_socle_is_the_curated_set():
    assert set(providers.DEFAULT_ACTIVE_CONNECTORS) == _SOCLE


def test_socle_connectors_exist_in_registry():
    names = {c.name for c in providers._REGISTRY_LIST}
    assert providers.DEFAULT_ACTIVE_CONNECTORS <= names


# ── backfill one-shot (faux conn : rejoue le contrat SQL sans PG) ──────────────

class _FakeConn:
    """Simule le `conn` psycopg (rows = dicts, comme _str_dict_row) pour le
    backfill : activation + pairs fournis, écritures capturées."""

    def __init__(self, *, sentinel_present=False, activation=(), pairs=()):
        self.sentinel_present = sentinel_present
        self.activation = [dict(r) for r in activation]
        self.pairs = [dict(r) for r in pairs]
        self.selected: list[tuple] = []      # (sub, org_id, connector)
        self.seeded: list[tuple] = []        # (sub, org_id)

    def execute(self, sql, params=()):
        self._last = (sql, params)
        if sql.startswith("SELECT 1 FROM connector_selection_seeded"):
            return _Cur(one={"?": 1} if self.sentinel_present else None)
        # Table unifiée `connector_availability` (chantier ACL, cadrage 10/07).
        if sql.startswith("SELECT scope_type, scope_id, connector, enabled"):
            return _Cur(all_=self.activation)
        if sql.startswith("SELECT sub, org_id FROM org_members"):
            return _Cur(all_=self.pairs)
        if sql.startswith("INSERT INTO user_selected_connectors"):
            self.selected.append(params[:3])
            return _Cur()
        if sql.startswith("INSERT INTO connector_selection_seeded"):
            self.seeded.append(params)
            return _Cur()
        raise AssertionError(f"SQL inattendu: {sql}")


class _Cur:
    def __init__(self, one=None, all_=None):
        self._one, self._all = one, all_ or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


def test_backfill_noop_when_sentinel_present():
    conn = _FakeConn(sentinel_present=True)
    connector_selection.backfill_preexisting(conn)
    assert conn.selected == [] and conn.seeded == []


def test_backfill_seeds_previously_visible_and_marks_sentinel():
    activation = [
        {"connector": "serper", "scope_type": "platform", "scope_id": "", "enabled": True},
        {"connector": "aiark", "scope_type": "platform", "scope_id": "", "enabled": True},
        {"connector": "attio", "scope_type": "platform", "scope_id": "", "enabled": True},   # ex-default_hidden
        {"connector": "zoho", "scope_type": "platform", "scope_id": "", "enabled": False},
        {"connector": "aiark", "scope_type": "org", "scope_id": "7", "enabled": False},      # override org 7
    ]
    pairs = [{"sub": "u1", "org_id": 7}, {"sub": "u2", "org_id": 0}]
    conn = _FakeConn(activation=activation, pairs=pairs)
    connector_selection.backfill_preexisting(conn)
    got = {(s, o): set() for s, o, _ in conn.selected}
    for s, o, name in conn.selected:
        got[(s, o)].add(name)
    # u1 (org 7) : aiark coupé par l'override, attio jamais visible (ex-hidden)
    assert got[("u1", 7)] == {"serper"}
    # u2 (perso/global) : l'exposé master moins l'ex-hidden
    assert got[("u2", 0)] == {"serper", "aiark"}
    # chaque pair marqué seedé + la sentinelle en dernier
    assert ("u1", 7) in conn.seeded and ("u2", 0) in conn.seeded
    assert conn.seeded[-1] == (connector_selection._BACKFILL_MARK,)


def test_backfill_hidden_set_is_the_frozen_history():
    # Fait HISTORIQUE figé au moment du retrait du flag — ne doit plus bouger
    # (le registre n'a plus de default_hidden ; cette liste appartient à la
    # migration, pas au produit).
    assert connector_selection._BACKFILL_HIDDEN == {
        "attio", "brevoauto", "pennylaneged", "resend", "scaleway", "http", "bridge"}
