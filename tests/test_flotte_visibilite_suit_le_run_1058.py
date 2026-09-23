"""#1058 — la boîte d'une session de FLOTTE suit l'org du RUN, pas la maison.

Incident du 22/09 : la maison du compte porteur des workers de flotte a basculé
(2 → 226, sans déploiement, écriture `org_members.is_active`). À l'`initialize` MCP,
aucun jeton d'appel n'existe encore (`_run_id=` n'arrive qu'au premier `call_tool`) :
`session_visibility` dérivait donc TOUJOURS la boîte de la maison — tous les
connecteurs de toutes les flottes du compte ont disparu pendant ~2h, alors que les
APPELS de ces mêmes flottes continuaient de s'exécuter sous l'org du run (`run_org`,
#639). Seule la VISIBILITÉ suivait la maison ; jamais l'exécution.

Le runner ouvre une session PAR APPEL (cf. `run_org`) : il connaît déjà le run au
moment d'ouvrir la connexion, et peut porter `X-Oto-Run` sur la requête `initialize`,
comme la face REST le porte déjà sur chacune des siennes (`X-Oto-Run` documenté pour
REST). Sans cet en-tête (session humaine, dashboard, claude.ai), rien ne change.

Trois niveaux, du plus bas au plus haut :
1. `run_org.resolve_visibility_org` — pure, fail-open (jamais de refus au handshake).
2. `session_visibility.compute_hidden_tools(..., org=...)` — l'override d'org pilote
   bien les couches, indépendamment de la maison.
3. `UserDisabledToolsMiddleware.on_initialize` — le banc demandé par oto cd : la boîte
   d'une session de flotte reste IDENTIQUE quand la maison du compte bascule pendant
   que la flotte tourne.

Logique pure : aucune base, aucun réseau (convention CLAUDE.md §Tests).
"""
from __future__ import annotations

import types
import uuid

import mcp.types as mt
import pytest

from oto_mcp import db, org_store, roles, run_org
from oto_mcp import session_visibility as SV
from oto_mcp.middleware.disabled_tools import UserDisabledToolsMiddleware


# ── 1. `run_org.resolve_visibility_org` — pure, fail-open ──────────────────────

@pytest.fixture
def _run(monkeypatch):
    """Un run connu, dans l'org 226, dont `sub-flotte` est membre."""
    run_id = uuid.uuid4().hex
    monkeypatch.setattr(db, "get_run_head",
                        lambda r: {"sub": "sub-flotte", "org_id": 226} if r == run_id else None)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    run_org._ORG_DU_RUN.clear()
    return run_id


@pytest.mark.asyncio
async def test_sans_en_tete_rien_nest_resolu(monkeypatch):
    monkeypatch.setattr(run_org, "get_http_headers", lambda **_kw: {})
    assert await run_org.resolve_visibility_org("sub-flotte") is None


@pytest.mark.asyncio
async def test_avec_en_tete_lorg_du_run_est_rendue(_run, monkeypatch):
    monkeypatch.setattr(run_org, "get_http_headers", lambda **_kw: {"x-oto-run": _run})
    assert await run_org.resolve_visibility_org("sub-flotte") == 226


@pytest.mark.asyncio
async def test_run_inconnu_ne_leve_pas_rend_none(monkeypatch):
    monkeypatch.setattr(run_org, "get_http_headers",
                        lambda **_kw: {"x-oto-run": uuid.uuid4().hex})
    monkeypatch.setattr(db, "get_run_head", lambda r: None)
    assert await run_org.resolve_visibility_org("sub-flotte") is None


@pytest.mark.asyncio
async def test_non_membre_ne_leve_pas_rend_none(_run, monkeypatch):
    """Fail-open, à la différence de `pin_for_call` (call-time) : au handshake, on ne
    refuse jamais — on retombe sur la dérivation historique (maison)."""
    monkeypatch.setattr(run_org, "get_http_headers", lambda **_kw: {"x-oto-run": _run})
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    assert await run_org.resolve_visibility_org("sub-flotte") is None


@pytest.mark.asyncio
async def test_base_qui_tousse_ne_leve_pas_rend_none(_run, monkeypatch):
    def _boom(r):
        raise RuntimeError("pool timeout")
    monkeypatch.setattr(run_org, "get_http_headers", lambda **_kw: {"x-oto-run": _run})
    monkeypatch.setattr(db, "get_run_head", _boom)
    assert await run_org.resolve_visibility_org("sub-flotte") is None


# ── 2. `compute_hidden_tools(..., org=)` pilote bien les couches ───────────────

class _Ctx:
    class _FastMCP:
        def __init__(self, noms):
            self._noms = noms

        async def list_tools(self, run_middleware=False):
            return [type("T", (), {"name": n})() for n in self._noms]

    def __init__(self, noms):
        self.fastmcp = self._FastMCP(noms)


@pytest.fixture
def socle_org(monkeypatch):
    """Neutralise tous les blocs voisins ; seule la sélection de connecteurs
    (`COUCHE_SELECTION`) reste, gardée par `prof_org` — c'est l'axe qu'on observe."""
    monkeypatch.setattr(SV.access, "current_group", lambda sub: None)
    monkeypatch.setattr(SV.access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(SV.access, "org_admin_hidden_tools", lambda org: set())
    monkeypatch.setattr(SV.access, "group_admin_hidden_tools", lambda g: set())
    monkeypatch.setattr(SV.access, "rbac_denied_connectors", lambda s, o: set())
    monkeypatch.setattr(SV.access, "group_rbac_denied_connectors", lambda s, g: set())
    monkeypatch.setattr(SV.access, "has_option", lambda *a, **k: True)
    monkeypatch.setattr(SV.db, "list_user_disabled_tools", lambda s, o: [])
    monkeypatch.setattr(SV.db, "list_user_enabled_tools", lambda s, o: [])
    monkeypatch.setattr(SV.connector_activation, "exposed_connectors", lambda o: set())
    monkeypatch.setattr(SV.connector_selection, "is_seeded", lambda s, o: True)
    return _Ctx({"fr_directors", "oto_whoami"})


@pytest.mark.asyncio
async def test_lorg_explicite_prime_sur_la_maison_pour_la_selection(socle_org, monkeypatch):
    """La maison (`current_org`) dit 2, mais l'appelant passe `org=226` : la
    sélection de connecteurs doit être lue sous 226, jamais sous 2 — c'est
    exactement ce que #1058 corrige."""
    monkeypatch.setattr(SV.access, "current_org", lambda sub: 2)
    vu = {}

    def list_selection(sub, prof_org):
        vu["prof_org"] = prof_org
        return {}

    monkeypatch.setattr(SV.connector_selection, "list_selection", list_selection)
    await SV.compute_hidden_tools(socle_org, "sub-flotte", org=226)
    assert vu["prof_org"] == 226


@pytest.mark.asyncio
async def test_sans_org_explicite_la_maison_est_dérivée_comme_avant(socle_org, monkeypatch):
    monkeypatch.setattr(SV.access, "current_org", lambda sub: 2)
    vu = {}

    def list_selection(sub, prof_org):
        vu["prof_org"] = prof_org
        return {}

    monkeypatch.setattr(SV.connector_selection, "list_selection", list_selection)
    await SV.compute_hidden_tools(socle_org, "sub-flotte")
    assert vu["prof_org"] == 2


# ── 3. Le banc demandé par oto cd : boîte de flotte INCHANGÉE quand la maison bascule ──

def _ctx_init():
    return types.SimpleNamespace(message=mt.InitializeRequest(
        method="initialize",
        params=mt.InitializeRequestParams(
            protocolVersion="2025-11-25",
            capabilities=mt.ClientCapabilities(),
            clientInfo=mt.Implementation(name="oto-runner", version="0.1"))))


async def _call_next(_ctx):
    return None


@pytest.mark.asyncio
async def test_la_boite_dune_session_de_flotte_ne_bouge_pas_quand_la_maison_bascule(monkeypatch):
    """Le banc demandé par oto cd (mesuré ensuite en préprod). Deux handshakes
    successifs de la MÊME flotte (même `X-Oto-Run`) : entre les deux, la maison du
    compte porteur bascule (2 → 999, comme le 22/09) — la boîte posée doit être
    identique aux deux passages."""
    run_id = uuid.uuid4().hex
    monkeypatch.setattr(
        "oto_mcp.middleware.disabled_tools.current_user_sub_from_token",
        lambda: "sub-flotte")
    monkeypatch.setattr(run_org, "get_http_headers", lambda **_kw: {"x-oto-run": run_id})
    monkeypatch.setattr(db, "get_run_head",
                        lambda r: {"sub": "sub-flotte", "org_id": 226} if r == run_id else None)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    run_org._ORG_DU_RUN.clear()

    poses = []

    async def _capture(ctx, sub, **kwargs):
        poses.append(kwargs.get("org"))

    monkeypatch.setattr(
        "oto_mcp.middleware.disabled_tools.apply_session_visibility", _capture)

    maison = {"org": 2}
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: maison["org"])

    mw = UserDisabledToolsMiddleware()
    context = types.SimpleNamespace(message=_ctx_init().message, fastmcp_context=object())
    await mw.on_initialize(context, _call_next)

    maison["org"] = 999  # bascule de la maison EN COURS DE VOL, comme le 22/09
    await mw.on_initialize(context, _call_next)

    assert poses == [226, 226], (
        f"la boîte de la flotte a suivi la maison au lieu de l'org du run : {poses}")
