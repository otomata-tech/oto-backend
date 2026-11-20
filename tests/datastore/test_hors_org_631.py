"""#631 — le nom d'un tableau, résolu par `data_claim_next`, refusé « datastore inconnu »
par `data_write` dans le MÊME travail, 78 secondes plus tard.

Mesuré en production le 29/08/2026 (un run de l'org 226) : réservation ok à 21:10:05,
écriture refusée à 21:11:23, écriture par le pronom `@claimed` ok à 21:11:35 (pronom
retiré le 07/09/2026, sans rien changer au diagnostic) — 103 refus de cette
famille sur la soirée, 82 sur sept jours, tous du même geste. La cause n'est pas dans le
datastore : l'écriture refusée est le seul des trois appels SANS axe `_org`, donc résolue
dans l'org MAISON de l'appelant, où le tableau n'existe pas. Le journal ne montre pas
l'axe (le middleware le retire des arguments avant le sink) ; la preuve est la colonne
`org_id` stampée — 226 sur les deux appels ok, 2 sur le refus.

Deux gestes, prouvés sur le chemin SERVI (middleware + outil monté, `_run_id` lu des
arguments bruts, vrai PostgreSQL) :

① **le run sait où il travaille** : quand sa réservation active porte un tableau de ce
   nom, le nom se résout par elle — et le bail LOCALISE sans donner un droit qu'on n'a pas ;
② **sinon le refus le dit** : où le tableau existe, sous quelle org l'appel a été résolu,
   et l'axe à passer — la face REST le disait déjà (#316), la face MCP répondait
   « inconnu » nu.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-campagne-631"
AUTRE = "sub-etranger-631"


@pytest.fixture(scope="module")
def orgs(live) -> dict:
    """Deux orgs RÉELLES du même sub : `maison` (active, comme l'org 2 en prod) et
    `travail` (celle du tableau, comme l'org 226). Un tiers, membre de nulle part."""
    from oto_mcp import org_store
    maison = org_store.create_org("Maison 631", created_by=SUB)
    travail = org_store.create_org("Travail 631", created_by=SUB)
    org_store.add_org_member(travail, SUB)
    org_store.add_org_member(maison, SUB)
    assert org_store.set_active_org(SUB, maison)
    assert org_store.get_active_org(SUB) == maison
    return {"maison": maison, "travail": travail}


@pytest.fixture
def surface(orgs, monkeypatch):
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    # L'axe `_org=` est GARDÉ sur le sub du jeton : l'identité, pas le seam sous test.
    from oto_mcp import call_axes
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    return orgs


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if not _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-631")
        register_all(m)
        for n in ("data_write",):
            _OUTILS[n] = asyncio.run(m.get_tool(n))
    return _OUTILS[nom]


def _table(org_id: int) -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("org", str(org_id), "copie-eval-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    db.set_datastore_schema(ns_id, {"fields": [{"key": "siren"}, {"key": "statut"}]})
    for i in range(2):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}", "statut": "a_faire"})
    return ns, ns_id


def _reserver(ns: str, run: str, org_id: int) -> dict:
    """La réservation comme elle est arrivée en prod : SOUS l'org du tableau
    (`_org=` posé), rattachée au run. Le geste sous test est l'ÉCRITURE qui suit."""
    from oto_mcp import session_org
    from oto_mcp.datastore.core import make_store
    tok_o = session_org.set_call_org(org_id)
    tok_r = session_org.set_call_run(run)
    try:
        ligne = make_store(SUB).claim_next(ns, worker=run, lease_s=600)
    finally:
        session_org.reset_call_run(tok_r)
        session_org.reset_call_org(tok_o)
    assert ligne, "une ligne était libre : la réservation la rend"
    return ligne


def _ecrire(arguments: dict):
    """`data_write` comme il arrive en production : `_run_id=` (et l'éventuel `_org=`)
    lus des arguments BRUTS par le middleware, posés, retirés, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_write")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = "data_write"
    msg.arguments = dict(arguments)
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware(frozenset()).on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


def _refus(arguments: dict) -> str:
    with pytest.raises(Exception) as e:
        _ecrire(arguments)
    return str(e.value)


def _valeur(ns_id: int, row_id: str, champ: str):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return (row or {}).get("data", {}).get(champ)


# ── ① le run sait où il travaille ─────────────────────────────────────────────

def test_le_nom_reserve_par_le_run_s_ecrit_sans_axe_org(surface):
    """Le geste exact du 29/08 21:11:23 : `data_write(datastore=<nom>, id=<ligne>)`
    avec `_run_id` et sans `_org`, l'org maison n'étant pas celle du tableau."""
    ns, ns_id = _table(surface["travail"])
    run = uuid.uuid4().hex
    ligne = _reserver(ns, run, surface["travail"])

    out = _ecrire({"datastore": ns, "id": ligne["_id"],
                   "row": {"statut": "fait"}, "_run_id": run})
    assert out["_id"] == ligne["_id"], out
    assert _valeur(ns_id, ligne["_id"], "statut") == "fait"


def test_le_bail_localise_mais_ne_donne_aucun_droit(orgs, monkeypatch):
    """Un jeton de run n'est pas un axe de droits : un tiers qui le connaît ne lit ni
    n'écrit le tableau de l'org dont il n'est pas membre — même refus qu'avant."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    ns, ns_id = _table(orgs["travail"])
    run = uuid.uuid4().hex
    ligne = _reserver(ns, run, orgs["travail"])

    monkeypatch.setattr(T, "_acting_store", lambda: make_store(AUTRE))
    monkeypatch.setattr(T, "_ns", lambda n: n)
    monkeypatch.setattr(T, "_project_hint", lambda n: None)
    msg = _refus({"datastore": ns, "id": ligne["_id"],
                  "row": {"statut": "vole"}, "_run_id": run})
    assert f"datastore `{ns}` unknown" in msg, msg
    assert _valeur(ns_id, ligne["_id"], "statut") == "a_faire"


# ── ② sinon, le refus dit où est le tableau et sous quelle org l'appel a été résolu ──

def test_sans_reservation_le_refus_nomme_les_deux_orgs_et_l_axe(surface):
    """Pas de bail sur ce tableau : le nom ne se résout pas dans l'org maison. Le refus
    dit dans quelle org il existe, dans laquelle l'appel a été résolu, et quoi passer.
    Le run est INCONNU de `runs` (jamais ouvert) : l'org du run (#639) n'a rien à poser
    — c'est le cas du run mal posé que l'indice continue de couvrir."""
    ns, _ = _table(surface["travail"])
    run = uuid.uuid4().hex

    msg = _refus({"datastore": ns, "row": {"siren": "999"}, "_run_id": run})
    assert f"datastore `{ns}` unknown" in msg, msg
    assert f"org {surface['travail']}" in msg and "Travail 631" in msg, msg
    assert f"org {surface['maison']}" in msg and "Maison 631" in msg, msg
    assert f"`_org={surface['travail']}`" in msg, msg


def test_un_nom_qui_n_existe_nulle_part_reste_un_refus_nu(surface):
    """On ne suggère que le tableau DEMANDÉ — pas une org au hasard."""
    msg = _refus({"datastore": "999999999", "row": {"siren": "999"}})
    assert "unknown" in msg and "_org=" not in msg, msg


def test_avec_l_axe_org_rien_ne_change(surface):
    """L'appel bien formé (`_org=` posé) écrit comme avant — la résolution par bail et
    l'indice ne s'ajoutent qu'au chemin qui échouait."""
    ns, ns_id = _table(surface["travail"])
    out = _ecrire({"datastore": ns, "row": {"siren": "5511100X", "statut": "neuf"},
                   "_org": surface["travail"]})
    assert out.get("_id") and _valeur(ns_id, out["_id"], "statut") == "neuf"
