"""L'empreinte d'un run : ce qu'il a réellement exécuté (chantier du run, lot J2).

Un run n'enregistrait que le SLUG de sa procédure. Or les procédures sont versionnées
(`org_instructions.version`, un snapshot par version dans `org_instruction_revisions`) :
rejouer « la même procédure » trois semaines plus tard, c'est en jouer une autre sans
que rien ne le dise. ADR 0055-D10 / 0058-D1 : *le gel des versions EST l'empreinte du
run*, et chaque jour de runs sans empreinte est un jour non reproductible.

Deux moitiés, une seule livrée ici :
- la **version de procédure**, résolue par `run_start` et versée dans le JOURNAL (le
  domicile cohérent avec le verdict du 12/08 : le run est ses faits, pas sa ligne
  d'index) — testée de bout en bout ci-dessous ;
- la **clé d'instance** (quel credential a réellement servi), dont l'allowlist du relevé
  est ouverte mais dont le point d'écriture est `access.resolve_credential`.

Le chemin est exercé POUR DE VRAI : `run_start` réel + `CallContextMiddleware`, et le
relevé relu comme le fait le sink du calllog. Ne sont stubbés que la DB et l'identité.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp import access, call_axes, db, org_store, server, session_org
from oto_mcp.auth import hooks as auth_hooks
from oto_mcp.db import usage
from oto_mcp import calllog
from oto_mcp.middleware.call_context import CallContextMiddleware
from oto_mcp.tools import guide_run as drt


class _SessionCtx:
    def __init__(self):
        self._state: dict = {}

    async def get_state(self, key):
        return self._state.get(key)

    async def set_state(self, key, value):
        self._state[key] = value


class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, msg):
        self.message = msg


@pytest.fixture(scope="module")
def run_start_fn():
    mcp = FastMCP("test")
    drt.register(mcp)
    return asyncio.run(mcp.get_tool("run_start")).fn


def _wire(monkeypatch, *, sub="u1", org=35, group=None,
          org_procedures=None, group_procedures=None, user_procedures=None) -> dict:
    """Rend le relevé de ce que les doublures ont VU. Un test qui affirme une absence
    (« pas de version ») le lit : la résolution de version est best-effort, donc une
    doublure que rien n'atteint — base absente, refus d'identité — rend la même
    absence, avalée en silence, et le test passerait sans avoir rien parcouru (#896)."""
    lus: dict = {"org": 0, "procedures": []}

    def _org(s):
        lus["org"] += 1
        return org

    def _procedure(otype, oid, slug):
        lus["procedures"].append((otype, oid, slug))
        par_palier = {"user": user_procedures, "org": org_procedures,
                      "group": group_procedures}
        return (par_palier[otype] or {}).get(slug)

    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(org_store, "get_active_org", _org)
    monkeypatch.setattr(access, "current_group", lambda s=None, **kw: group)
    # UN seul store depuis #681 : le palier est le PREMIER argument, plus un module
    # par palier. Un stub qui ignorerait `otype` rendrait la procédure d'org pour une
    # lecture d'équipe — et le test « l'org prime sur l'équipe » passerait à vide.
    monkeypatch.setattr(org_store, "get_instruction", _procedure)
    monkeypatch.setattr(db, "insert_run", lambda run_id, **kw: None)
    return lus


async def _start(run_start_fn, **args) -> tuple[dict, dict]:
    """Ouvre un run par le chemin réel. Rend (résultat, relevé d'appel).

    ⚠️ Le relevé est posé par `CallContextMiddleware` lui-même (holder neuf par appel) :
    on le relit DEPUIS L'INTÉRIEUR de l'appel, exactement là où le sink du calllog le
    relit pour le verser dans les args de la ligne. Le poser depuis le test donnerait un
    holder que le middleware remplace — et un test qui n'observerait rien."""
    seen: dict = {}
    mw = CallContextMiddleware()

    async def _next(ctx):
        out = await run_start_fn(_SessionCtx(), **ctx.message.arguments)
        seen.update(session_org.current_call_trace() or {})
        return out

    out = await mw.on_call_tool(_Ctx(_Msg("run_start", dict(args))), _next)
    return out, seen


# ── La version de procédure atterrit dans le journal ────────────────────────

@pytest.mark.asyncio
async def test_le_run_enregistre_la_version_de_la_procedure(monkeypatch, run_start_fn):
    _wire(monkeypatch, org_procedures={"prospection": {"version": 7}})
    out, trace = await _start(run_start_fn, label="prospection Q3", doctrine="prospection")
    # Dans la ligne de journal (via le relevé) …
    assert trace["doctrine_version"] == 7 and trace["guide_scope"] == "org"
    # … et rendue à l'agent, qui sait donc ce qu'il déroule.
    assert out["doctrine"] == "prospection" and out["doctrine_version"] == 7


@pytest.mark.asyncio
async def test_la_procedure_dequipe_compte_comme_celle_de_lorg(monkeypatch, run_start_fn):
    """`oto_procedure(op='get')` sert l'org PUIS l'équipe active : l'empreinte doit
    suivre le même ordre, sinon une procédure d'équipe se déroule sans version."""
    _wire(monkeypatch, group=12, group_procedures={"relance": {"version": 2}})
    _, trace = await _start(run_start_fn, label="relances", doctrine="relance")
    assert trace["doctrine_version"] == 2 and trace["guide_scope"] == "group"


@pytest.mark.asyncio
async def test_lorg_prime_sur_lequipe(monkeypatch, run_start_fn):
    _wire(monkeypatch, group=12, org_procedures={"relance": {"version": 9}},
          group_procedures={"relance": {"version": 2}})
    _, trace = await _start(run_start_fn, label="relances", doctrine="relance")
    assert trace["doctrine_version"] == 9


@pytest.mark.asyncio
async def test_un_run_ad_hoc_ne_prétend_à_aucune_version(monkeypatch, run_start_fn):
    lus = _wire(monkeypatch, org_procedures={"prospection": {"version": 7}})
    out, trace = await _start(run_start_fn, label="un truc vite fait")
    assert out["doctrine_version"] is None and "doctrine_version" not in trace
    assert "guide_scope" not in trace
    assert lus["org"], "l'org du run n'a pas été lue : l'absence ne prouve rien"
    assert lus["procedures"] == [], "un run ad hoc ne cherche aucune procédure"


@pytest.mark.asyncio
async def test_un_slug_inconnu_ouvre_le_run_sans_version(monkeypatch, run_start_fn):
    """Un slug qui ne désigne rien de lisible ici (guide d'un autre foyer, faute de
    frappe) : pas de version, mais le run s'ouvre — l'empreinte n'est pas un gate."""
    lus = _wire(monkeypatch)
    out, trace = await _start(run_start_fn, label="?", doctrine="ce-slug-nexiste-pas")
    assert out["run_id"] and out["doctrine_version"] is None
    assert "doctrine_version" not in trace
    # Le slug a été CHERCHÉ dans l'org du run, et rien n'y répondait.
    assert ("org", 35, "ce-slug-nexiste-pas") in lus["procedures"]


@pytest.mark.asyncio
async def test_une_lecture_de_version_en_panne_nempeche_pas_le_run(monkeypatch,
                                                                   run_start_fn):
    """Best-effort, comme tout ce qui entoure un run : la base indisponible au moment
    de résoudre la version ne doit pas faire échouer l'ouverture du déroulé."""
    pannes: list = []

    def _boom(*a, **kw):
        pannes.append(a)
        raise RuntimeError("pool épuisé")

    _wire(monkeypatch)
    monkeypatch.setattr(org_store, "get_instruction", _boom)
    out, trace = await _start(run_start_fn, label="prospection", doctrine="prospection")
    assert out["run_id"] and out["doctrine_version"] is None
    # Seul le slug passé est relevé (#1340) : aucune moitié d'empreinte inventée.
    assert trace == {usage._ARG_PROCEDURE: "prospection"}
    # La panne avalée est CELLE-CI : une autre (base absente plus haut) donnerait le
    # même run sans version, et le test passerait sans avoir éprouvé la sienne.
    assert pannes, "la lecture de version n'a jamais été tentée"


# ── #1156 : l'empreinte suit la lecture servie, copie perso comprise ─────────

@pytest.mark.asyncio
async def test_la_copie_perso_qui_masque_lorg_est_celle_que_le_run_journalise(
        monkeypatch, run_start_fn):
    """Le bug : l'empreinte lisait l'org puis l'équipe, jamais la copie perso. Un run
    qui exécutait la copie perso masquant celle de l'org journalisait la version de
    l'ORG — l'empreinte d'un texte que le run n'avait pas parcouru."""
    _wire(monkeypatch, org_procedures={"campagne": {"version": 9, "archived_at": None}},
          user_procedures={"campagne": {"version": 2, "archived_at": None}})
    out, trace = await _start(run_start_fn, label="campagne", guide="campagne")
    assert trace["doctrine_version"] == 2 and trace["guide_scope"] == "user"
    assert out["guide_version"] == 2


@pytest.mark.asyncio
async def test_une_copie_perso_archivee_laisse_lempreinte_a_lorg(monkeypatch,
                                                                 run_start_fn):
    """Même règle que la lecture par nom (décision du 14/10/2026) : une copie perso
    archivée ne masque plus, le run exécute — et journalise — celle de l'org."""
    _wire(monkeypatch, org_procedures={"campagne": {"version": 9, "archived_at": None}},
          user_procedures={"campagne": {"version": 2,
                                        "archived_at": "2026-10-14 09:00:00+00"}})
    _, trace = await _start(run_start_fn, label="campagne", guide="campagne")
    assert trace["doctrine_version"] == 9 and trace["guide_scope"] == "org"


def test_la_face_rest_journalise_le_meme_palier(monkeypatch):
    """`POST /api/me/runs` écrit son fait `run_start` elle-même : même résolution, même
    clé que la face MCP."""
    from oto_mcp.capabilities import run_thread
    from oto_mcp.capabilities._types import ResolvedCtx
    _wire(monkeypatch, org_procedures={"campagne": {"version": 9, "archived_at": None}},
          user_procedures={"campagne": {"version": 2, "archived_at": None}})
    faits: list = []
    monkeypatch.setattr(db, "insert_tool_call", faits.append)
    out = run_thread._ouvrir(ResolvedCtx(sub="u1", org_id=35),
                             run_thread.RunOuvertureInput(label="campagne",
                                                          guide="campagne"))
    assert out["guide_version"] == 2
    [fait] = faits
    assert fait["tool"] == "run_start"
    assert fait["args"]["doctrine_version"] == 2
    assert fait["args"]["guide_scope"] == "user"


# ── Le relevé d'appel, et ce qu'il laisse passer ────────────────────────────

def test_lempreinte_passe_lallowlist_du_journal():
    """Le relevé est filtré à l'écriture (`server._TRACED_ARGS`) : une clé absente de
    l'allowlist est silencieusement jetée. Les deux moitiés de l'empreinte doivent y
    être — sinon `run_start` calcule une version que personne n'écrit."""
    assert "doctrine_version" in server._TRACED_ARGS
    assert "guide_scope" in server._TRACED_ARGS
    assert "instance" in server._TRACED_ARGS


def test_seule_lallowlist_atteint_les_args_de_la_ligne():
    """Reproduit le filtre du sink : ce qui est déclaré passe, le reste tombe."""
    trace = {"doctrine_version": 7, "guide_scope": "user",
             "instance": "org:35:pennylane", "ns_id": 160,
             "secret_interne": "à ne jamais journaliser"}
    args = {**{"label": "prospection Q3"},
            **{k: v for k, v in trace.items() if k in server._TRACED_ARGS}}
    assert args == {"label": "prospection Q3", "doctrine_version": 7,
                    "guide_scope": "user", "instance": "org:35:pennylane", "ns_id": 160}


# ── #1340 : un run ouvert avec `guide=` est un run qui DÉROULE la procédure ──

@pytest.mark.asyncio
async def test_le_slug_passe_par_guide_atteint_la_cle_que_lit_laudit(monkeypatch,
                                                                     run_start_fn):
    """Le bug : le journal garde les arguments tels que passés ; un agent qui suit la
    description (`guide=`) n'écrivait jamais la clé que lisent la reconstruction des
    runs et l'inertie de l'audit de projet — la procédure passait pour jamais déroulée.
    Ici, le chemin du sink : relevé réel, filtré par l'allowlist, versé dans les args."""
    _wire(monkeypatch, org_procedures={"campagne": {"version": 9, "archived_at": None}})
    _, trace = await _start(run_start_fn, label="campagne", guide="campagne")
    ligne = calllog.apply_call_trace(
        {"tool": "run_start", "args": {"label": "campagne", "guide": "campagne"}},
        trace, server._TRACED_ARGS)
    assert ligne["args"][usage._ARG_PROCEDURE] == "campagne"


@pytest.mark.asyncio
async def test_un_run_ad_hoc_ne_nomme_aucune_procedure(monkeypatch, run_start_fn):
    _wire(monkeypatch)
    _, trace = await _start(run_start_fn, label="un truc vite fait")
    assert usage._ARG_PROCEDURE not in trace
