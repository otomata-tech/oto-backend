"""#547 — la description de `_run_id` disait au modèle qu'il pouvait l'omettre.

Mesuré le 29/08/2026 sur une campagne : le jeton était passé sur **140/140**
réservations (consigne fraîche, premier appel) puis **omis à l'écriture**, et
**31 écritures sur 100 étaient refusées** — 31/31 sur une ligne que l'appelant
tenait lui-même, aucun run déclaré, identifiant exact, contenu intact. Ni
saturation de contexte, ni reconnexion : le texte de l'outil, relu à chaque
appel, pèse plus que la consigne lue une fois au handshake, et il disait
« le run ACTIF s'applique déjà ».

Deux corrections, testées ici :
- **la description sert le contraire** — obligatoire, ce que coûte l'omission,
  l'exception nommée ;
- **le refus enseigne** — quand la ligne est tenue par le propre run de
  l'appelant et que l'appel ne porte aucun run, le refus nomme la faute.

⚠️ **Une phrase UNIQUE, jamais choisie à l'appel.** Beaucoup de clients
récupèrent `tools/list` une fois à la poignée de main et le figent pour toute la
session : une description conditionnelle n'atteindrait jamais un modèle en
session longue, et ne « marcherait » que là où chaque appel est sa propre
session. Un correctif qui ne corrige que la moitié qu'on mesure est pire qu'un
bug ouvert.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp import call_axes


def _desc() -> str:
    return call_axes.RUN.schema["description"]


# ── ① la description dit l'inverse de ce qu'elle disait ──────────────────────

def test_la_description_dit_obligatoire_et_ce_que_coute_l_omission():
    d = _desc()
    assert "OBLIGATOIRE" in d, f"le jeton doit être annoncé obligatoire : {d!r}"
    assert "refusée" in d, (
        f"la CONSÉQUENCE de l'omission doit être écrite, pas déduite : {d!r}")
    for déclencheur in ("run_start", "data_claim_next"):
        assert déclencheur in d, f"`{déclencheur}` doit nommer le moment où le jeton devient dû : {d!r}"


def test_la_description_ne_dit_plus_que_le_run_actif_s_applique():
    """La phrase exacte qui a coûté 31 lignes sur 100. Elle était VRAIE sur un serveur
    qui tient une session avec un run actif, et fausse partout ailleurs — donc lue
    comme une permission d'omettre."""
    d = _desc()
    assert "s'applique déjà" not in d
    assert "n'y compte pas" in d, (
        f"l'héritage doit être présenté comme une exception dont on ne dépend pas : {d!r}")


# ── ② la même phrase pour tout le monde — pas de génération conditionnelle ───

def _outils_servis(ctx) -> dict[str, dict]:
    """Ce que le MIDDLEWARE sert réellement pour `tools/list`, pas ce que le schéma nu
    contient : c'est `inject_schema` qui recopie la description dans chaque outil."""
    from fastmcp import FastMCP

    from oto_mcp.middleware.call_context import CallContextMiddleware

    m = FastMCP("t")

    @m.tool()
    def data_write(x: int) -> dict:      # datastore `data` ⇒ porte l'axe `_run_id`
        return {}

    tools = asyncio.run(m.list_tools(run_middleware=False))

    async def _call_next(_):
        return tools

    servis = asyncio.run(CallContextMiddleware().on_list_tools(ctx, _call_next))
    return {t.name: t.parameters for t in servis}


class _Ctx:
    """L'état de session, comme FastMCP le tient (cf. test_row_lock_prod_defects)."""

    def __init__(self):
        self._state = {}

    async def get_state(self, k):
        return self._state.get(k)

    async def set_state(self, k, v):
        self._state[k] = v


def test_la_description_servie_est_la_meme_avec_ou_sans_run_actif():
    """Le cœur du garde-fou : **aucune branche**. Une session longue qui tient un run
    et un appel isolé sans session doivent recevoir la MÊME description — sinon le
    correctif n'atteint que la flotte (chaque appel = une session) et rate exactement
    les clients où le schéma est figé au handshake."""
    from oto_mcp import guide_run

    longue = _Ctx()
    asyncio.run(guide_run.push_run(longue, "run-en-cours", "campagne"))
    isolee = _Ctx()

    avec = _outils_servis(longue)["data_write"]["properties"]["_run_id"]
    sans = _outils_servis(isolee)["data_write"]["properties"]["_run_id"]

    assert avec == sans, (
        "la description de `_run_id` dépend de l'état de session — un client qui fige "
        "`tools/list` au handshake ne verrait jamais la bonne version.")
    assert avec["description"] == _desc()


def test_seule_la_description_de_run_id_change_sur_un_outil_qui_le_porte():
    """L'empreinte : les autres axes servis par le même outil sont intacts."""
    servi = _outils_servis(_Ctx())["data_write"]["properties"]
    for axe in call_axes.AXES:
        if axe.param in servi:
            assert servi[axe.param] == dict(axe.schema)


# ── ③ le refus enseigne : `_run_id` omis, nommé quand c'est prouvé ───────────


@pytest.fixture
def table(live):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-agent", "camp-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)

    for i in range(3):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}", "statut": "a_faire"})
    return ns, ns_id


def _refus(ns: str, row_id: str = "r0"):
    """Le refus RÉEL, levé par le store sur le chemin d'écriture."""
    from oto_mcp.datastore.core import RowLocked, make_store
    with pytest.raises(RowLocked) as e:
        make_store("sub-agent").update_row(ns, row_id, {"statut": "écrasé"})
    return e.value


def _claim_sous_run(ns: str, run_id: str, sub: str) -> None:
    """Réserve une ligne SOUS un run — comme un agent qui vient de faire `run_start`
    puis `data_claim_next` en portant son `_run_id`."""
    from oto_mcp import db, session_org
    from oto_mcp.datastore.core import make_store
    db.insert_run(run_id, sub=sub, org_id=None, label="campagne")
    tok = session_org.set_call_run(run_id)
    try:
        make_store("sub-agent").claim_next(ns, worker="agent-1")
    finally:
        session_org.reset_call_run(tok)


def test_le_refus_nomme_l_omission_quand_la_ligne_est_tenue_par_ton_run(table, monkeypatch):
    """Le cas mesuré, 31 fois sur 100 : la ligne est tenue par le propre run de
    l'appelant, et l'appel qui écrit ne porte aucun `_run_id`."""
    from oto_mcp.tools import datastore as surface
    ns, _ = table
    run = "run-" + uuid.uuid4().hex[:8]
    _claim_sous_run(ns, run, "sub-agent")
    monkeypatch.setattr(surface.access, "current_user_sub_from_token", lambda: "sub-agent")

    msg = surface._row_locked_message(_refus(ns))

    assert "_run_id" in msg and "probably omitted" in msg, msg
    assert run in msg, "le run de l'appelant lui est rendu — c'est le sien"
    assert "data_release" in msg, "le refus garde sa sortie explicite (#317)"


def test_le_refus_ne_nomme_pas_le_run_d_un_TIERS(table, monkeypatch):
    """⚠️ Le garde-fou du garde-fou. `_run_id` n'autorise rien, il NOMME : imprimer le
    run d'un tiers dans un refus lui donnerait le jeton qui lève le verrou, et ferait
    du verrou une étiquette."""
    from oto_mcp.tools import datastore as surface
    ns, _ = table
    run = "run-" + uuid.uuid4().hex[:8]
    _claim_sous_run(ns, run, "quelqu-un-d-autre")
    monkeypatch.setattr(surface.access, "current_user_sub_from_token", lambda: "sub-agent")

    msg = surface._row_locked_message(_refus(ns))

    assert run not in msg, "un refus ne publie pas le run d'un tiers"
    assert "probably omitted" not in msg


def test_pas_d_indice_quand_l_appel_porte_deja_un_run(table, monkeypatch):
    """Si l'appelant porte un run et se fait quand même refuser, la cause est ailleurs
    — un indice faux est pire qu'un refus nu."""
    from oto_mcp import session_org
    from oto_mcp.tools import datastore as surface
    ns, _ = table
    run = "run-" + uuid.uuid4().hex[:8]
    _claim_sous_run(ns, run, "sub-agent")
    monkeypatch.setattr(surface.access, "current_user_sub_from_token", lambda: "sub-agent")

    autre = "run-" + uuid.uuid4().hex[:8]
    refus = _refus(ns)
    tok = session_org.set_call_run(autre)
    try:
        msg = surface._row_locked_message(refus)
    finally:
        session_org.reset_call_run(tok)

    assert "probably omitted" not in msg


def test_les_deux_traductions_du_refus_passent_par_le_meme_seam():
    """Un indice posé sur UNE surface et pas l'autre se remarque en production, pas en
    revue : chaque site qui traite un `RowLocked` doit servir le même texte. Un site, c'est
    un `except RowLocked` ou un tri `isinstance(e, RowLocked)` (la suppression par lot
    trie ses refus ligne à ligne) ; compter seulement les `except` laissait le second
    hors du compte, et la garde rougissait sur un code juste."""
    import inspect

    from oto_mcp.tools import datastore as surface
    src = inspect.getsource(surface)
    sites = src.count("except RowLocked") + src.count("isinstance(e, RowLocked)")
    assert sites == src.count("_row_locked_message(e)") >= 2
