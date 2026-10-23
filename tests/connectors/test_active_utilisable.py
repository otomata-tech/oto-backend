"""Active = utilisable (22/10/2026) — la sélection suit les clés que le membre peut utiliser.

La règle (`connectors/usable.py`) : un connecteur est ACTIF pour un membre, dans une org,
exactement quand une clé y est utilisable pour lui — la sienne, celle de son équipe, de
l'org, du tenant, de la plateforme — ou qu'aucune n'est requise. Le tableau de bord n'a
plus d'interrupteur ; ce banc tient ce qui rend la règle vraie pour TOUT membre et tout
run hébergé, pas seulement pour qui ouvre la page des connecteurs :

- la table de la règle (`verdict`) et le plan (`plan`), purs ;
- la réconciliation sur un vrai PostgreSQL : ce qu'elle active, met en pause, laisse ;
- la marque par (sub, org) : posée, vieillie, effacée par une écriture du coffre ;
- le refus de mettre en pause ou de retirer un connecteur utilisable.
"""
from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from oto_mcp.connectors import usable as U

# La règle est coupée hors de ses bancs (conftest `_sans_reconciliation`) : ici on l'éprouve.
pytestmark = pytest.mark.reconciliation


def _con(secret_kind="api_key", namespaces=("x",)):
    return SimpleNamespace(secret_kind=secret_kind, namespaces=namespaces)


# ── la règle, connecteur par connecteur ────────────────────────────────────────

@pytest.mark.parametrize("mode", ["user", "group", "org", "tenant", "platform", "over_quota"])
def test_un_palier_qui_repond_rend_utilisable(mode):
    assert U.verdict(_con(), {"mode": mode}) == U.USABLE


def test_une_cle_refusee_reste_utilisable():
    """Elle est là pour être réparée : l'agent doit voir l'échec, pas perdre l'outil."""
    assert U.verdict(_con(), {"mode": "org", "health_ko": True}) == U.USABLE


def test_rien_ne_repond_rend_non_utilisable():
    assert U.verdict(_con(), {"mode": "forbidden"}) == U.NOT_USABLE


@pytest.mark.parametrize("kind", ["none", "settings"])
def test_sans_cle_a_apporter_est_utilisable_quoi_que_dise_l_instantane(kind):
    assert U.verdict(_con(kind), None) == U.USABLE
    assert U.verdict(_con(kind), {"mode": "forbidden"}) == U.USABLE


def test_une_etape_en_attente_ne_decide_rien():
    """Un canal hébergé dont le compte n'est pas lié : « il reste une étape », jamais
    une bascule — ni allumé, ni éteint."""
    assert U.verdict(_con(), {"mode": "platform", "pending_action": "Connect"}) == U.UNKNOWN


def test_pas_de_ligne_dans_l_instantane_ne_decide_rien():
    """Non calculé n'est pas « rien ne répond »."""
    assert U.verdict(_con(), None) == U.UNKNOWN
    assert U.verdict(_con(), {"mode": "quelque_chose_de_neuf"}) == U.UNKNOWN


def test_un_connecteur_sans_outil_ne_decide_rien():
    """Une clé de modèle (agents, jamais un outil) : sa sélection ne monte rien."""
    assert U.verdict(_con(namespaces=()), {"mode": "forbidden"}) == U.UNKNOWN
    assert U.verdict(None, {"mode": "org"}) == U.UNKNOWN


# ── le plan ─────────────────────────────────────────────────────────────────────

def test_le_plan_active_l_utilisable_et_met_en_pause_l_actif_sans_cle():
    registry = {n: _con() for n in ("a", "b", "c", "d", "e", "f")}
    registry["f"] = _con("none")
    snapshot = {
        "a": {"mode": "org"},          # utilisable, non sélectionné → activé
        "b": {"mode": "platform"},     # utilisable, en pause → réactivé
        "c": {"mode": "forbidden"},    # actif sans clé → en pause
        "d": {"mode": "forbidden"},    # en pause sans clé → laissé
        "e": {"mode": "platform", "pending_action": "Connect"},  # actif, en attente → laissé
    }
    selection = {"b": "paused", "c": "active", "d": "paused", "e": "active"}
    exposed = {"a", "b", "c", "d", "e", "f"}
    assert U.plan(snapshot, selection, exposed, registry) == (["a", "b", "f"], ["c"])


def test_le_plan_ne_touche_pas_ce_que_l_org_n_expose_pas():
    """Non exposé, il ne monte rien quoi que dise sa ligne ; l'installer remettrait ce
    que l'org a coupé."""
    registry = {"a": _con(), "c": _con()}
    snapshot = {"a": {"mode": "org"}, "c": {"mode": "forbidden"}}
    assert U.plan(snapshot, {"c": "active"}, set(), registry) == ([], [])


# ── sur un vrai PostgreSQL ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    avant = {k: os.environ.get(k) for k in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    os.environ["DATABASE_URL"] = pg_module_dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64   # le coffre chiffre : une clé de banc
    try:
        from oto_mcp.db import init_db
        from oto_mcp.connectors import activation
        init_db()
        for nom in ("hunter", "kaspr", "folk", "droit"):
            activation.set_activation(nom, True)
        yield
    finally:
        for k, v in avant.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _org(nom: str, *membres: str) -> int:
    from oto_mcp import org_store
    oid = org_store.create_org(nom, created_by=membres[0])
    for m in membres:
        org_store.add_org_member(oid, m, "org_member")
    return oid


def _ligne(sub: str, org: int, connecteur: str):
    from oto_mcp import db
    with db._connect() as c:
        return c.execute(
            "SELECT state, origin FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org, connecteur)).fetchone()


def _instantane(monkeypatch, providers: dict):
    from oto_mcp import access
    monkeypatch.setattr(access, "status_for",
                        lambda sub, org=None, group=None: {"providers": providers})


def test_reconcilier_active_met_en_pause_et_laisse(live, monkeypatch):
    from oto_mcp.connectors import selection as sel
    sub = "membre-reconcilie"
    org = _org("org-reconcilie", sub)
    sel.set_state(sub, "kaspr", sel.ACTIVE, org)       # actif, plus aucune clé
    sel.set_state(sub, "folk", sel.ACTIVE, org)        # actif, étape en attente
    sel.set_state(sub, "hunter", sel.ACTIVE, org)
    sel.unselect(sub, "hunter", org)                   # retiré par le membre, mais utilisable
    _instantane(monkeypatch, {
        "hunter": {"mode": "org"},
        "kaspr": {"mode": "forbidden"},
        "folk": {"mode": "platform", "pending_action": "Connect"},
    })

    activated, paused = U.reconcile(sub, org, None)

    assert "hunter" in activated and "droit" in activated   # open data : rien à apporter
    assert paused == ["kaspr"]
    assert _ligne(sub, org, "hunter") == {"state": "active", "origin": U.ORIGIN}
    assert sel.list_removed(sub, org) == {}                  # le retrait ne tient plus
    assert _ligne(sub, org, "kaspr")["state"] == "paused"
    assert _ligne(sub, org, "folk")["state"] == "active"      # en attente : intouché
    assert not U.due(sub, org)


def test_la_marque_vieillit_et_une_ecriture_du_coffre_l_efface(live, monkeypatch):
    from oto_mcp import credentials_store, db
    sub = "membre-marque"
    org = _org("org-marque", sub)
    _instantane(monkeypatch, {})
    U.reconcile(sub, org, None)
    assert not U.due(sub, org)

    # Vieillie au-delà du TTL → à refaire.
    with db._connect() as c:
        c.execute("UPDATE connector_selection_reconciled SET reconciled_at = NOW() - %s "
                  "WHERE sub = %s AND org_id = %s", (U.TTL + timedelta(minutes=1), sub, org))
    assert U.due(sub, org)

    # Refaite, puis une clé posée par le membre dans CETTE org l'efface aussitôt.
    U.reconcile(sub, org, None)
    assert not U.due(sub, org)
    credentials_store.set_credential(credentials_store.MEMBER,
                                     credentials_store.member_id(org, sub), "hunter", "k-test")
    assert U.due(sub, org)


def test_une_cle_d_org_efface_la_marque_de_tous_ses_membres(live, monkeypatch):
    from oto_mcp import credentials_store
    a, b = "membre-org-a", "membre-org-b"
    org = _org("org-cle-partagee", a, b)
    _instantane(monkeypatch, {})
    U.reconcile(a, org, None)
    U.reconcile(b, org, None)
    credentials_store.set_credential(credentials_store.ORG, str(org), "hunter", "k-org")
    assert U.due(a, org) and U.due(b, org)


# ── le geste qui contredirait la règle est refusé ───────────────────────────────

def test_pause_et_retrait_refuses_sur_un_connecteur_utilisable(monkeypatch):
    from oto_mcp.capabilities.connectors import selection as cap
    from oto_mcp.capabilities._types import AuthzDenied
    ctx = SimpleNamespace(sub="s", org_id=7)
    monkeypatch.setattr(cap.access, "current_group", lambda sub: None)
    monkeypatch.setattr(U, "is_usable", lambda *a: True)
    with pytest.raises(AuthzDenied) as e:
        cap._refuse_if_usable(ctx, "hunter", 7)
    assert e.value.code == "connector_usable" and e.value.status == 409


def test_une_lecture_impossible_laisse_passer_le_geste(monkeypatch):
    """Le refus est une courtoisie : la réconciliation suivante dit la règle."""
    from oto_mcp.capabilities.connectors import selection as cap
    monkeypatch.setattr(cap.access, "current_group", lambda sub: None)

    def _boum(*a):
        raise RuntimeError("instantané illisible")

    monkeypatch.setattr(U, "is_usable", _boum)
    cap._refuse_if_usable(SimpleNamespace(sub="s", org_id=7), "hunter", 7)


def test_la_session_ne_tombe_pas_si_la_reconciliation_echoue(monkeypatch):
    """Fail-INERTE : rien n'est écrit, la boîte monte ce que la table porte."""
    import asyncio
    from oto_mcp import session_visibility as SV

    def _boum(*a, **k):
        raise RuntimeError("panne")

    monkeypatch.setattr(SV.connector_usable, "run_if_due", _boum)
    monkeypatch.setattr(SV.connector_selection, "is_seeded", lambda *a: True)
    monkeypatch.setattr(SV.connector_selection, "list_selection",
                        lambda *a: {"hunter": "active"})

    class _Ctx:
        class _FastMCP:
            async def list_tools(self, run_middleware=False):
                return [type("T", (), {"name": n})() for n in ("hunter_x", "kaspr_x")]
        fastmcp = _FastMCP()

    try:
        caches = asyncio.run(SV.compute_hidden_tools(_Ctx(), "s", org=7))
    except Exception as e:   # pragma: no cover — le diagnostic, si ça casse
        pytest.fail(f"la session est tombée : {e}")
    assert "kaspr_x" in caches


def test_a_la_session_le_kit_sans_cle_passe_en_pause_et_l_open_data_s_active(live, monkeypatch):
    """L'interaction avec le kit, par la vraie poignée de main : le kit installe `kaspr`
    (aucune clé), la session le met en pause ; `droit` (open data) s'active sans geste."""
    import asyncio
    from oto_mcp import org_store, session_visibility as SV
    sub = "membre-kit"
    org = _org("org-kit", sub)
    org_store.set_org_default_connectors(org, ["kaspr"])
    _instantane(monkeypatch, {"kaspr": {"mode": "forbidden"}})

    class _Ctx:
        class _FastMCP:
            async def list_tools(self, run_middleware=False):
                return [type("T", (), {"name": n})() for n in ("kaspr_x", "droit_x")]
        fastmcp = _FastMCP()

    caches = asyncio.run(SV.compute_hidden_tools(_Ctx(), sub, org=org))
    assert _ligne(sub, org, "kaspr") == {"state": "paused", "origin": "kit"}
    assert _ligne(sub, org, "droit")["origin"] == U.ORIGIN
    assert "kaspr_x" in caches and "droit_x" not in caches


def test_une_rafale_ne_calcule_qu_une_fois(live, monkeypatch):
    """Premier déploiement, ou une clé posée : la flotte d'une même personne ouvre ses
    sessions ensemble — le tour est PRIS avant le calcul, un seul le gagne."""
    sub = "membre-rafale"
    org = _org("org-rafale", sub)
    assert U.claim(sub, org) is True
    assert U.claim(sub, org) is False
    assert not U.due(sub, org)


def test_un_echec_rend_le_tour(live, monkeypatch):
    from oto_mcp import access
    sub = "membre-echec"
    org = _org("org-echec", sub)

    def _boum(*a, **k):
        raise RuntimeError("instantané illisible")

    monkeypatch.setattr(access, "status_for", _boum)
    with pytest.raises(RuntimeError):
        U.run_if_due(sub, org, None)
    assert U.due(sub, org)                      # la session suivante réessaie
    _instantane(monkeypatch, {})
    assert U.run_if_due(sub, org, None) is not None
    assert U.run_if_due(sub, org, None) is None  # pas son tour


def test_equipes_illisibles_aucune_pause(live, monkeypatch):
    """`status_for` retombe sur « aucune équipe » quand leur lecture échoue : une clé
    d'équipe y lirait `forbidden`. Cette passe active ce qu'elle peut, ne pause rien."""
    from oto_mcp import group_store
    from oto_mcp.connectors import selection as sel
    sub = "membre-equipes"
    org = _org("org-equipes", sub)
    sel.set_state(sub, "kaspr", sel.ACTIVE, org)
    _instantane(monkeypatch, {"kaspr": {"mode": "forbidden"}, "hunter": {"mode": "org"}})

    def _boum(*a, **k):
        raise RuntimeError("équipes illisibles")

    monkeypatch.setattr(group_store, "list_groups_for_user", _boum)
    activated, paused = U.reconcile(sub, org, None)
    assert "hunter" in activated and paused == []
    assert _ligne(sub, org, "kaspr")["state"] == "active"
