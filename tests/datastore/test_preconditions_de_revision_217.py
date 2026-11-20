"""Les gestes qui partaient SANS précondition : suppression et libération (oto#217).

Le patch par `id` a sa précondition depuis le 12/09/2026 (`?expected_revision=`) ; la
SUPPRESSION et la LIBÉRATION n'en avaient aucune, ni côté serveur ni côté contrat. Une
ligne modifiée entre la lecture et le clic disparaissait avec la modification ; un bail
repris par un second worker était retiré par une libération forcée décidée sur l'état
d'avant — d'où une double attribution possible.

Ce banc REJOUE les scénarios de l'énoncé sur une base jetable, par les routes servies
(l'adaptateur REST réel) et par la face agent, en relevant à chaque étape ce que porte
la BASE : la donnée, la révision, le bail et le compteur de reprises. Ce qui s'y juge :

1. la précondition est ACCEPTÉE là où elle manquait (suppression, libération), sur les
   deux régimes de la libération — un paramètre inerte serait pire que son absence ;
2. elle REFUSE ce qui a changé depuis la lecture (`409 revision_conflict`,
   `details.current_revision`), et ZÉRO ligne n'est touchée — ni donnée, ni révision,
   ni bail, ni compteur, ni entrée au journal ;
3. la suppression est jumelle de l'écriture par `id` : verrou, bail, révision, puis
   suppression. Une ligne sous bail d'un autre rend `409 row_locked` — elle sortait en
   500 muet, l'exception traversant l'adaptateur REST ;
4. les refus que ces routes émettent sont DÉCLARÉS au contrat, et chacun est rejoué ici.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from _datastore_rest import call, cap, stub_authz

SUB = "sub-217"

SCHEMA = {"fields": [
    {"key": "titre", "type": "text"},
    {"key": "statut", "role": "status",
     "lifecycle": {"states": ["ouvert", "en_revue", "clos"],
                   "transitions": {"ouvert": ["en_revue", "clos"],
                                   "en_revue": ["clos", "ouvert"],
                                   "clos": ["ouvert"]}}},
]}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def _table(ligne: dict | None = None) -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "p217-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    _store().set_schema(ns, SCHEMA)
    db.datastore_insert_row(ns_id, "r1", dict(ligne or {"titre": "t1",
                                                        "statut": "ouvert"}))
    return ns, ns_id


def _etat(ns_id: int, row_id: str = "r1") -> dict | None:
    """Ce que porte la BASE — jamais l'écho d'un appel."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT data, rev, updated_at::text AS updated_at, claims, abandon_reason, "
            "       claimed_by, claimed_until::text AS claimed_until, claimed_run "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
        return dict(row) if row else None


def _rev(ns_id: int, row_id: str = "r1") -> str:
    return str(_etat(ns_id, row_id)["rev"])


@pytest.fixture
def route(live, monkeypatch):
    """Une table, une ligne, et les params de chemin des routes de ligne."""
    stub_authz(monkeypatch)
    ns, ns_id = _table()
    return ns, ns_id, {"datastore": ns, "row_id": "r1"}


@pytest.fixture
def journal(monkeypatch):
    """Les entrées de journal posées par les routes — vides sur un refus."""
    from oto_mcp.datastore import journal as dsj
    poses: list = []
    monkeypatch.setattr(dsj, "record",
                        lambda tool, **kw: poses.append((tool, kw.get("row_id"))))
    return poses


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t217")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


@pytest.fixture
def mcp(live, monkeypatch):
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", _store)
    monkeypatch.setattr(T, "_store_for", lambda sub: _store())
    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return lambda nom, **arguments: asyncio.run(
        _outil(nom).run(arguments)).structured_content


# ── 1 & 2. la transition, telle qu'elle se joue aujourd'hui ────────────────────

def test_une_transition_portant_la_revision_lue_passe(route):
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    code, corps = call("me.datastore.update_row", path_params=params,
                       body={"statut": "en_revue"},
                       query=f"expected_revision={r0}".encode(), sub=SUB)
    assert code == 200, corps
    assert corps["_revision"] == str(int(r0) + 1) == _rev(ns_id)


def test_une_transition_aveugle_ecrase_la_decision_dun_autre(route):
    """Le défaut, mesuré : A lit `ouvert` (r0), B passe `en_revue` (r1), A écrit `clos`
    SANS révision — la décision de B disparaît sans un mot."""
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    _store().update_row(ns, "r1", {"statut": "en_revue"})          # B
    assert _rev(ns_id) == str(int(r0) + 1)
    code, corps = call("me.datastore.update_row", path_params=params,
                       body={"statut": "clos"}, sub=SUB)           # A, à l'aveugle
    assert code == 200, corps
    assert _etat(ns_id)["data"]["statut"] == "clos", "l'écrasement est le fait à couvrir"


def test_la_meme_transition_portant_la_revision_lue_est_refusee(route, journal):
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    _store().update_row(ns, "r1", {"statut": "en_revue"})          # B
    avant = _etat(ns_id)
    code, corps = call("me.datastore.update_row", path_params=params,
                       body={"statut": "clos"},
                       query=f"expected_revision={r0}".encode(), sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    assert corps["details"] == {"current_revision": str(avant["rev"])}
    assert _etat(ns_id) == avant, "rien n'a bougé : ni donnée, ni révision, ni bail"
    assert journal == [], "un refus ne se journalise pas comme un geste"


def test_une_ligne_abandonnee_ne_revient_pas_dans_la_file_par_un_refus(route):
    """L'effet de bord de l'énoncé : toute écriture remet `claims = 0` et
    `abandon_reason = NULL`. Une transition REFUSÉE ne doit pas la remettre en file."""
    from oto_mcp.db._conn import _connect
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claims = 3, abandon_reason = 'plafond' "
                     "WHERE ns_id = %s AND row_id = 'r1'", (ns_id,))
    _store().update_row(ns, "r1", {"titre": "t2"})                 # un autre écrit
    code, corps = call("me.datastore.update_row", path_params=params,
                       body={"statut": "clos"},
                       query=f"expected_revision={r0}".encode(), sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    apres = _etat(ns_id)
    assert (apres["claims"], apres["abandon_reason"]) == (0, None), (
        "témoin : c'est l'écriture du voisin qui a rouvert la ligne, pas le refus")


def test_une_reservation_puis_une_liberation_intercalees_font_refuser(route):
    """Scénario 3 : A lit (r0), un worker réserve, écrit, libère — la révision a
    avancé trois fois. L'écriture aveugle de A passe ; celle qui porte r0 est refusée."""
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    st = _store()
    st.claim_row(ns, "r1", worker="w-1", lease_s=600)
    st.release_claim(ns, "r1", worker="w-1")
    assert int(_rev(ns_id)) > int(r0)
    code, corps = call("me.datastore.update_row", path_params=params,
                       body={"statut": "en_revue"},
                       query=f"expected_revision={r0}".encode(), sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps


# ── 3. la SUPPRESSION : verrou, bail, révision, puis suppression ───────────────

def test_supprimer_avec_la_revision_lue(route):
    ns, ns_id, params = route
    code, corps = call("me.datastore.delete_row", path_params=params,
                       query=f"expected_revision={_rev(ns_id)}".encode(), sub=SUB)
    assert (code, corps) == (200, {"ok": True, "id": "r1"})
    assert _etat(ns_id) is None


def test_supprimer_apres_une_modification_concurrente_est_refuse(route, journal):
    """Le défaut : la ligne disparaissait AVEC la modification qu'un autre venait d'y
    poser, et rien dans le contrat ne permettait de s'en prémunir."""
    ns, ns_id, params = route
    r0 = _rev(ns_id)
    _store().update_row(ns, "r1", {"titre": "corrigé par un autre"})
    avant = _etat(ns_id)
    code, corps = call("me.datastore.delete_row", path_params=params,
                       query=f"expected_revision={r0}".encode(), sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    assert corps["details"] == {"current_revision": str(avant["rev"])}
    assert _etat(ns_id) == avant, "zéro ligne touchée"
    assert journal == []


def test_supprimer_une_ligne_sous_le_bail_dun_autre_rend_409_row_locked(route):
    """`RowLocked` n'était traduite par aucune route : l'exception traversait
    l'adaptateur REST et la suppression d'une ligne réservée sortait en 500 muet."""
    ns, ns_id, params = route
    _store().claim_row(ns, "r1", worker="w-1", lease_s=600)
    code, corps = call("me.datastore.delete_row", path_params=params, sub=SUB)
    assert (code, corps["error"]) == (409, "row_locked"), corps
    assert _etat(ns_id) is not None, "la ligne réservée est toujours là"


def test_supprimer_une_ligne_absente_rend_404(route):
    ns, ns_id, params = route
    code, corps = call("me.datastore.delete_row",
                       path_params={"datastore": ns, "row_id": "fantome"}, sub=SUB)
    assert (code, corps["error"]) == (404, "row_not_found"), corps


def test_une_revision_illisible_est_refusee_avant_toute_suppression(route):
    ns, ns_id, params = route
    code, corps = call("me.datastore.delete_row", path_params=params,
                       query=b"expected_revision=abc", sub=SUB)
    assert (code, corps["error"]) == (400, "invalid_row_input"), corps
    assert _etat(ns_id) is not None


def test_la_face_agent_porte_la_precondition_de_suppression(mcp, live):
    ns, ns_id = _table()
    r0 = _rev(ns_id)
    _store().update_row(ns, "r1", {"titre": "t2"})
    with pytest.raises(Exception, match="revision_conflict"):
        mcp("data_delete_row", datastore=ns, id="r1", expected_revision=r0)
    assert _etat(ns_id) is not None
    assert mcp("data_delete_row", datastore=ns, id="r1",
               expected_revision=_rev(ns_id)) == {"ok": True, "id": "r1"}
    assert _etat(ns_id) is None


# ── 4. la LIBÉRATION : les deux régimes, jamais un paramètre inerte ────────────

def _bail(ns: str, worker: str = "w-1") -> None:
    _store().claim_row(ns, "r1", worker=worker, lease_s=600)


def test_liberer_de_force_avec_la_revision_lue(route):
    ns, ns_id, params = route
    _bail(ns)
    code, corps = call("me.datastore.release_claim", path_params=params, no_body=True,
                       query=f"expected_revision={_rev(ns_id)}".encode(), sub=SUB)
    assert (code, corps["released"]) == (200, True), corps
    assert _etat(ns_id)["claimed_by"] is None


def test_une_liberation_forcee_ne_retire_pas_le_bail_repris_par_un_autre(route):
    """Le scénario de la double attribution : la supervision décide de libérer le bail
    de `w-1`, `w-1` rend la main, `w-2` prend la ligne — et la libération forcée
    partait quand même, retirant le bail de `w-2`."""
    ns, ns_id, params = route
    st = _store()
    _bail(ns, "w-1")
    lue = _rev(ns_id)                       # l'état PRÉSENTÉ à la supervision
    st.release_claim(ns, "r1", worker="w-1")
    st.claim_row(ns, "r1", worker="w-2", lease_s=600)
    code, corps = call("me.datastore.release_claim", path_params=params, no_body=True,
                       query=f"expected_revision={lue}".encode(), sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    assert _etat(ns_id)["claimed_by"] == "w-2", "le bail du second est intact"


def test_une_liberation_gardee_porte_la_meme_precondition(route):
    """Les DEUX régimes, sans quoi le paramètre serait inerte sur l'un d'eux."""
    ns, ns_id, params = route
    _bail(ns, "w-1")
    perimee = str(int(_rev(ns_id)) - 1)
    code, corps = call("me.datastore.release_claim", path_params=params,
                       body={"worker": "w-1", "expected_revision": perimee}, sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    assert _etat(ns_id)["claimed_by"] == "w-1"
    code, corps = call("me.datastore.release_claim", path_params=params,
                       body={"worker": "w-1", "expected_revision": _rev(ns_id)}, sub=SUB)
    assert (code, corps["released"]) == (200, True), corps


def test_liberer_une_ligne_absente_avec_precondition_rend_404(route):
    ns, ns_id, params = route
    code, corps = call("me.datastore.release_claim",
                       path_params={"datastore": ns, "row_id": "fantome"},
                       no_body=True, query=b"expected_revision=0", sub=SUB)
    assert (code, corps["error"]) == (404, "row_not_found"), corps


def test_liberer_une_ligne_sans_bail_reste_un_no_lease(route):
    """La précondition ne change pas le verdict bénin : rien à rendre reste `no_lease`,
    et surtout pas une erreur — une flotte a branché sa borne d'arrêt dessus (#517)."""
    ns, ns_id, params = route
    code, corps = call("me.datastore.release_claim", path_params=params,
                       body={"worker": "w-1", "expected_revision": _rev(ns_id)}, sub=SUB)
    assert code == 200, corps
    assert (corps["released"], corps["reason"]) == (False, "no_lease")
    # Le régime FORCÉ n'a jamais rendu de motif (la supervision agit sans garde) :
    # la précondition ne lui en invente pas un.
    code, corps = call("me.datastore.release_claim", path_params=params, no_body=True,
                       query=f"expected_revision={_rev(ns_id)}".encode(), sub=SUB)
    assert (code, corps["released"], corps["reason"]) == (200, False, None), corps


def test_une_liberation_sans_precondition_garde_son_fil(route):
    """Additif : un appelant qui ne la passe pas garde EXACTEMENT le geste d'avant."""
    ns, ns_id, params = route
    _bail(ns)
    code, corps = call("me.datastore.release_claim", path_params=params, no_body=True,
                       sub=SUB)
    assert (code, corps["released"]) == (200, True), corps


# ── 5. les refus DÉCLARÉS au contrat, rejoués sur les routes servies ──────────

def _codes(cle: str) -> set[tuple[int, str]]:
    return {(e.status, e.code) for e in cap(cle).errors}


@pytest.mark.parametrize("cle,attendus", [
    ("me.datastore.update_row",
     {(409, "revision_conflict"), (409, "row_locked"), (400, "row_invalid"),
      (400, "invalid_row_input"), (404, "row_not_found"), (404, "datastore_not_found"),
      (403, "datastore_read_only"), (400, "jeton_mal_place")}),
    ("me.datastore.delete_row",
     {(409, "revision_conflict"), (409, "row_locked"), (400, "invalid_row_input"),
      (404, "row_not_found"), (404, "datastore_not_found"),
      (403, "datastore_read_only"), (400, "jeton_mal_place")}),
    ("me.datastore.release_claim",
     {(409, "revision_conflict"), (404, "row_not_found"), (400, "worker_required"),
      (400, "invalid_row_input"), (404, "datastore_not_found"),
      (403, "datastore_read_only"), (400, "jeton_mal_place")}),
    ("me.datastore.claim_next",
     {(400, "worker_required"), (400, "invalid_claim"), (404, "datastore_not_found"),
      (403, "datastore_read_only")}),
    ("me.datastore.claim_row",
     {(400, "worker_required"), (400, "invalid_claim"), (404, "row_not_found"),
      (409, "row_claimed"), (409, "row_outside_claimable"),
      (404, "datastore_not_found"), (403, "datastore_read_only")}),
])
def test_les_refus_de_ces_routes_sont_declares(cle, attendus):
    """Un refus émis et non déclaré est un refus qu'un client généré ne connaît pas :
    il le traite en panne. L'atteignabilité et la publication au document sont gardées
    par `tests/test_capability_declared_errors.py` ; ici on fige la LISTE."""
    assert attendus <= _codes(cle), f"{cle} : refus non déclarés {attendus - _codes(cle)}"


def test_reserver_sans_libelle_rend_worker_required(route):
    ns, _, params = route
    code, corps = call("me.datastore.claim_row", path_params=params, body={}, sub=SUB)
    assert (code, corps["error"]) == (400, "worker_required"), corps
    code, corps = call("me.datastore.claim_next", path_params={"datastore": ns},
                       body={}, sub=SUB)
    assert (code, corps["error"]) == (400, "worker_required"), corps


def test_liberer_sans_libelle_depuis_un_jeton_porte_rend_worker_required(route):
    """Un jeton PORTÉ est le vecteur d'une intégration multi-utilisateurs : y laisser
    la libération forcée, c'est laisser chacun retirer la ligne de son collègue."""
    from oto_mcp.auth import token_scopes
    ns, _, params = route
    token_scopes.set_current({"datastores": {ns: "write"}})
    try:
        code, corps = call("me.datastore.release_claim", path_params=params,
                           no_body=True, sub=SUB)
    finally:
        token_scopes.set_current(None)
    assert (code, corps["error"]) == (400, "worker_required"), corps


def test_reserver_une_ligne_deja_tenue_rend_row_claimed(route):
    ns, _, params = route
    _bail(ns, "w-1")
    code, corps = call("me.datastore.claim_row", path_params=params,
                       body={"worker": "w-2"}, sub=SUB)
    assert (code, corps["error"]) == (409, "row_claimed"), corps


def test_reserver_hors_du_perimetre_declare_rend_row_outside_claimable(route):
    ns, ns_id, params = route
    schema = dict(SCHEMA)
    schema["fields"] = [dict(f) for f in SCHEMA["fields"]]
    schema["fields"][1]["lifecycle"] = dict(schema["fields"][1]["lifecycle"],
                                            claimable={"statut": "en_revue"})
    _store().set_schema(ns, schema)
    code, corps = call("me.datastore.claim_row", path_params=params,
                       body={"worker": "w-1"}, sub=SUB)
    assert (code, corps["error"]) == (409, "row_outside_claimable"), corps


def test_un_tableau_en_lecture_seule_refuse_la_liberation(monkeypatch):
    """Libérer est une écriture : le régime du tableau la refuse comme les autres."""
    from _datastore_rest import Boom
    from oto_mcp.capabilities.datastore import rows as R
    from oto_mcp.datastore.core import DatastoreReadOnly
    stub_authz(monkeypatch)
    monkeypatch.setattr(R, "make_store", lambda sub: Boom(DatastoreReadOnly("v")))
    code, corps = call("me.datastore.release_claim",
                       path_params={"datastore": "1", "row_id": "r1"},
                       body={"worker": "w-1"}, sub=SUB)
    assert (code, corps["error"]) == (403, "datastore_read_only"), corps
