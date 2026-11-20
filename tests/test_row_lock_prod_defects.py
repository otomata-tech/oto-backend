"""Les deux défauts constatés EN PRODUCTION sur le premier essai réel du verrou (#317).

Campagne de 8 910 lignes bloquée, org 226, le 15/08. Trois symptômes, **une racine et
demie** :

**① `run_finish` ne libérait rien.** `_current_run()` lisait `session_org.current_call_run()`,
qui ne renvoie un run que si le jeton `_run_id=` a été passé EXPLICITEMENT. Un agent qui
fait `run_start` empile dans l'état de session — donc `claimed_run` n'était jamais posé,
et la libération par run ne trouvait rien. Le calllog, lui, lisait déjà les DEUX sources
(`server.py`) : le geste juste existait à trois lignes.

**② L'écriture unitaire rendait un 500.** Même racine : le titulaire n'étant reconnu par
aucune des deux voies, `RowLocked` était levée **contre lui-même** — et n'était traduite
sur aucune surface.

**③ L'écriture par lot « marchait » — parce que sa protection était INERTE.** Un
fail-open ÉCRIT (« date en chaîne ⇒ comparaison impossible ⇒ ne bloque pas ») contre un
row factory qui normalise TOUT `datetime` en chaîne. Le cas cru marginal était le cas
normal. Les deux chemins avaient donc des comportements **opposés** : l'un refusait tout
le monde, l'autre n'a jamais refusé personne.

⚠️ Ces tests manquaient parce que les précédents exerçaient les **helpers** et posaient le
run à la main (`set_call_run`) — ils validaient donc la moitié du chemin qui marchait.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def table(live):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-agent", "camp-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)

    db.set_datastore_schema(ns_id, {"fields": [{"key": "siren"}, {"key": "statut"}]})
    for i in range(3):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}", "statut": "a_faire"})
    return ns, ns_id


def _store(sub="sub-agent"):
    from oto_mcp.datastore.core import make_store
    return make_store(sub)


def _bail(ns_id, row_id) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


# ── ③ le fail-open : LE test qui aurait attrapé le défaut ────────────────────

def test_a_lease_read_as_a_string_still_protects(table):
    """⚠️ **Le test manquant.** Le row factory du dépôt normalise tout `datetime` en
    CHAÎNE — c'est le cas NORMAL, pas un cas limite. La première version du garde
    retournait « comparaison impossible ⇒ ne bloque pas » : la protection du chemin de
    fusion n'a donc jamais rien protégé, et personne ne l'a vu parce que les tests
    passaient par un chemin qui, lui, filtrait la date en SQL.

    On vérifie donc la protection SUR LA FORME RÉELLE de la donnée, pas sur une forme
    de laboratoire."""
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    _store().claim_next(ns, worker="agent-1")     # bail posé, date rendue en chaîne

    # Un AUTRE worker du même compte — la flotte de sous-agents, le cas réel. Il ne
    # déclare ni run ni worker : il n'est donc pas le titulaire.
    with pytest.raises(RowLocked):
        _store().update_row(ns, "r0", {"statut": "écrasé"})


def test_an_unreadable_lease_refuses_rather_than_opens(table):
    """Parti pris maison : pas de fallback, on lève. Un bail dont on ne sait pas s'il
    court protège encore quelqu'un — l'ignorance ne doit pas se résoudre en faveur de
    l'écrivain, qui est précisément ce que faisait le fail-open."""
    from oto_mcp.datastore.core import DatastorePg, RowLocked

    guard = DatastorePg._lease_guard("r0")
    with pytest.raises(RowLocked):
        guard({"claimed_by": "agent-1", "claimed_until": "pas une date",
               "claimed_run": None})


def test_an_expired_lease_read_as_a_string_does_not_block(table):
    """Le pendant : la chaîne est PARSÉE, donc un bail expiré rendu en texte cesse
    bien de protéger. Sans le parse, on refuserait des écritures parfaitement
    légitimes — l'erreur symétrique de celle qu'on corrige."""
    from oto_mcp.datastore.core import DatastorePg

    passe = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    DatastorePg._lease_guard("r0")({"claimed_by": "mort", "claimed_until": passe,
                                    "claimed_run": None})   # ne lève pas


def test_a_batch_no_longer_writes_over_someone_elses_row(table):
    """Le chemin par lot passe par la fusion : sa protection était inerte, donc un
    lot écrivait sur les lignes d'autrui **sans un mot**. C'était le contournement
    utilisé en production."""
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    _store().claim_next(ns, worker="agent-1")

    with pytest.raises(RowLocked):
        _store().write_rows(ns, [{"siren": "55111000", "statut": "écrasé par un lot"}],
                            key="siren")


# ── ① la séquence réelle de l'agent ──────────────────────────────────────────

def test_the_run_stack_is_enough_to_stamp_a_claim(table, monkeypatch):
    """⚠️ **La racine de ①.** L'agent n'a passé AUCUN jeton `_run_id=` : il a fait
    `run_start`, qui empile dans l'état de session. Le stamp doit marcher avec ça
    seul — c'est ce que le middleware pose désormais, en lisant les deux sources
    comme le calllog."""
    from oto_mcp import session_org
    ns, ns_id = table

    # Ce que le middleware pose quand la pile porte un run et qu'aucun jeton n'est donné.
    token = session_org.set_call_run("run-de-la-pile")
    try:
        row = _store().claim_next(ns, worker="agent-1")
    finally:
        session_org.reset_call_run(token)

    assert _bail(ns_id, row["_id"])["claimed_run"] == "run-de-la-pile"


def test_the_holder_writes_without_declaring_anything(table):
    """②. Le titulaire écrivait sous son propre run et se faisait refuser — puis le
    refus ressortait en 500. Sous le run qui tient la ligne, il écrit sans rien
    déclarer."""
    from oto_mcp import db, session_org
    ns, ns_id = table

    token = session_org.set_call_run("run-agent")
    try:
        row = _store().claim_next(ns, worker="agent-1")
        _store().update_row(ns, row["_id"], {"statut": "traité"})
    finally:
        session_org.reset_call_run(token)

    assert db.datastore_get_row(ns_id, row["_id"])["data"]["statut"] == "traité"


def test_closing_the_run_frees_the_campaign(table):
    """Le geste complet : trois lignes prises sous un run, la fermeture les rend."""
    from oto_mcp import db, session_org
    ns, ns_id = table

    token = session_org.set_call_run("run-campagne")
    try:
        pris = [_store().claim_next(ns, worker="agent-1") for _ in range(3)]
    finally:
        session_org.reset_call_run(token)
    assert all(p for p in pris)

    assert db.datastore_release_by_run("run-campagne") == 3
    for p in pris:
        assert _bail(ns_id, p["_id"])["claimed_by"] is None


# ── ② le refus est nommé, jamais un 500 ──────────────────────────────────────

def test_the_refusal_carries_the_way_out(table):
    """Un 500 ne dit rien. Le refus doit porter la faute probable (`_run_id` omis, #515),
    JUSQU'À QUAND et COMMENT lever — c'est ce que l'agent bloqué n'avait pas. Il ne
    nomme pas le titulaire : l'échéance suffit."""
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    _store().claim_next(ns, worker="agent-1")
    with pytest.raises(RowLocked) as e:
        _store().update_row(ns, "r0", {"statut": "x"})

    msg = str(e.value)
    assert "_run_id" in msg                    # la faute la plus fréquente, d'abord
    assert "agent-1" not in msg                # le titulaire d'un tiers n'est pas nommé
    assert "until" in msg                      # jusqu'à quand
    assert "data_release" in msg               # comment lever


def test_the_write_surface_translates_the_refusal():
    """⚠️ La traduction, sur la SURFACE — c'est elle qui manquait, et sans elle le
    refus le mieux rédigé ressort en « Erreur interne du serveur »."""
    from pathlib import Path

    from oto_mcp.tools import datastore as tools_ds
    src = Path(tools_ds.__file__).read_text()

    assert "except RowLocked" in src, "le refus doit être traduit, pas propagé"
    # Et dans les MÊMES blocs que les autres refus métier, pas dans un coin.
    assert src.count("except RowLocked") >= 1


# ── le middleware lui-même : le chemin qui manquait ──────────────────────────

@pytest.mark.asyncio
async def test_the_middleware_pins_the_active_run_from_the_stack():
    """⚠️ **Le test qui manquait vraiment.** Celui d'au-dessus pose le run à la main
    (`set_call_run`) : il valide la moitié qui marchait déjà. C'est exactement le
    défaut qui a laissé passer le bug en production — des tests qui exercent les
    helpers au lieu du chemin réel.

    Ici on exerce le MIDDLEWARE : une pile de run, aucun jeton `_run_id=`, et on
    vérifie que le run est posé pendant le handler.

    ⚠️ Ce banc passait l'état de session comme contexte du MIDDLEWARE lui-même ; or le
    vrai `MiddlewareContext` n'a pas `get_state` — l'état est sur son
    `fastmcp_context`. Il est resté vert pendant que le repli était mort en production
    (28/08 → 27/10/2026, #830). Le contexte a désormais la forme réelle, et le run de
    la pile est posé en run de SESSION (lu par le datastore), pas en run de l'appel
    (réservé au `_run_id` explicite : org du run, gardes de flotte)."""
    from oto_mcp import guide_run, session_org
    from oto_mcp.datastore.outils import _current_run
    from oto_mcp.middleware.call_context import CallContextMiddleware

    vu = {}

    class _Session:                  # l'état de session, comme FastMCP le tient
        def __init__(self):
            self._state = {}

        async def get_state(self, k):
            return self._state.get(k)

        async def set_state(self, k, v):
            self._state[k] = v

    class _MiddlewareCtx:            # comme le vrai : PAS de get_state
        pass

    session = _Session()
    await guide_run.push_run(session, "run-empile", "campagne")

    class _Msg:
        name, arguments = "data_claim_next", {"datastore": "t", "worker": "w"}

    ctx = _MiddlewareCtx()
    ctx.message = _Msg()
    ctx.fastmcp_context = session

    async def _call_next(_):
        vu["run"] = _current_run()                    # ce que voit le datastore
        vu["explicite"] = session_org.current_call_run()
        return "ok"

    await CallContextMiddleware(frozenset()).on_call_tool(ctx, _call_next)

    assert vu["run"] == "run-empile", (
        "sans jeton explicite, le run ACTIF de la pile doit être vu du datastore — "
        "c'est la racine du défaut de production")
    assert vu["explicite"] is None, "le run de session n'est pas le run NOMMÉ (#830)"
    assert session_org.current_session_run() is None, "et retiré après l'appel"
