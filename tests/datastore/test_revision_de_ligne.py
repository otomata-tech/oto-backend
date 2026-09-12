"""La RÉVISION d'une ligne : ce qui la fait avancer, ce qui ne la fait pas, où elle se
lit, et ce qu'une écriture d'un AUTRE code en fait (12/09/2026).

`datastore_rows.rev` est avancée par le déclencheur `datastore_rows_20_revision`
(`db/revision.py`), jamais par le code : pendant la bascule bleu/vert, deux versions
servent 120 s la même base, et une écriture de l'ancienne doit être vue par la
précondition `expected_revision` de la nouvelle. Tout ici se juge sur ce que porte la
BASE, par une connexion fraîche — jamais sur l'écho d'un appel.

Ce que ces bancs tiennent, et la chute jouée en mémoire qui les a fait rougir :

1. **la couverture** — `data` sous toutes ses formes, chaque colonne du bail à elle
   seule, la réservation, son renouvellement, sa libération ; et ce qui ne compte pas
   (écriture sans effet, JSON réordonné, `updated_at`, `claims`, `embed_dirty`,
   `search_vec`). Chute : `claimed_by` retiré de la condition du déclencheur ;
2. **la base partagée** — l'UPDATE de l'ancien patch fait avancer `rev`. Chute : `rev+1`
   posé par le code au lieu du déclencheur ;
3. **l'échec fermé** — `?expected_revision=` est un paramètre CONNU du serveur. Chute :
   le champ retiré de l'entrée REST, comme sur l'ancien serveur (`400 unknown_fields`) ;
4. **la lecture** — `_revision`, une chaîne, sur tous les chemins qui rendent une ligne,
   et la garde mécanique qui empêche un SELECT d'oublier `rev`.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
import uuid

import pytest

from _datastore_rest import call, stub_authz

SUB = "sub-revision"
_OU = " WHERE ns_id = %s AND row_id = 'r1'"


def _table(ligne: dict | None = None) -> tuple[str, int]:
    from oto_mcp import db
    ns = "rev-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    db.datastore_insert_row(ns_id, "r1", dict(ligne or {"statut": "a_faire"}))
    return ns, ns_id


def _etat(ns_id: int) -> dict:
    """Ce que porte la BASE : donnée, révision, horodatage, compteur, bail. Les dates en
    texte PostgreSQL : le formateur de lignes les coupe à la seconde."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT data, rev, updated_at::text AS updated_at, claims, claimed_by, "
            "       claimed_until::text AS claimed_until, claimed_run "
            "FROM datastore_rows" + _OU, (ns_id,)).fetchone())


def _sql(requete: str, ns_id: int) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(requete + _OU, (ns_id,))


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-revision")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


@pytest.fixture
def mcp(live, monkeypatch):
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", _store)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return lambda nom, **arguments: asyncio.run(
        _outil(nom).run(arguments)).structured_content


# ── la garde mécanique ─────────────────────────────────────────────────────────

_ENTETE_LIGNE = "row_id, created_at, updated_at, data"


def test_toute_projection_de_ligne_selectionne_rev():
    """`_row_to_dict` sert `_revision` quand la ligne porte `rev` : un SELECT qui
    l'oublierait servirait une ligne SANS révision, et l'agent écrirait sans la
    précondition qu'il croit disponible. Par l'AST, comme la garde de `claimed_run`."""
    racine = pathlib.Path(__file__).resolve().parents[2] / "oto_mcp" / "db"
    requetes = [(f.name, n.value) for f in sorted(racine.glob("*.py"))
                for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and _ENTETE_LIGNE in n.value]
    assert len(requetes) >= 8, (
        f"la garde ne voit que {len(requetes)} requêtes : la signature "
        f"{_ENTETE_LIGNE!r} a changé, elle ne garde plus rien")
    manquantes = [(f, s[:100]) for f, s in requetes if not re.search(r"\brev\b", s)]
    assert not manquantes, f"projection(s) de ligne sans `rev` : {manquantes}"


def test_la_revision_est_une_colonne_de_plateforme_declaree_au_contrat():
    from oto_mcp.capabilities.datastore.rows import Row
    from oto_mcp.datastore.columns import _META_COLS
    assert "_revision" in _META_COLS
    assert Row.model_fields["revision"].serialization_alias == "_revision"


# ── 1. la couverture ───────────────────────────────────────────────────────────

def test_la_revision_avance_sur_chaque_forme_de_la_donnee(live):
    """absent → `null` → `[]` sont trois états ; une clé retirée en est un quatrième."""
    _, ns_id = _table()
    for nom, requete in [
        ("absent → null", "UPDATE datastore_rows SET data = data || '{\"x\": null}'::jsonb"),
        ("null → []", "UPDATE datastore_rows SET data = jsonb_set(data, '{x}', '[]'::jsonb)"),
        ("[] → [1]", "UPDATE datastore_rows SET data = jsonb_set(data, '{x}', '[1]'::jsonb)"),
        ("clé retirée", "UPDATE datastore_rows SET data = data - 'x'"),
    ]:
        avant = _etat(ns_id)["rev"]
        _sql(requete, ns_id)
        assert _etat(ns_id)["rev"] == avant + 1, f"{nom} : la révision n'a pas avancé"


@pytest.mark.parametrize("colonne, valeur", [
    ("claimed_by", "'poste-2'"),
    ("claimed_run", "'run-seul'"),
    ("claimed_until", "NOW() + interval '5 minutes'"),
])
def test_chaque_colonne_du_bail_fait_avancer_la_revision_A_ELLE_SEULE(live, colonne, valeur):
    """Colonne par colonne : une réservation change les trois d'un coup, elle ne dirait
    donc pas laquelle manque à la condition du déclencheur."""
    _, ns_id = _table()
    avant = _etat(ns_id)["rev"]
    _sql(f"UPDATE datastore_rows SET {colonne} = {valeur}", ns_id)
    assert _etat(ns_id)["rev"] == avant + 1, f"`{colonne}` seul n'avance pas la révision"


def test_reserver_renouveler_liberer_font_avancer_la_revision(live):
    ns, ns_id = _table()
    st = _store()
    r0 = _etat(ns_id)["rev"]
    pris = st.claim_row(ns, "r1", worker="poste-1", lease_s=600)
    assert _etat(ns_id)["rev"] == r0 + 1, "réserver"
    assert pris["_revision"] == str(r0 + 1), "la réservation rend la révision posée"
    avant = _etat(ns_id)
    renouvele = st.claim_row(ns, "r1", worker="poste-1", lease_s=900)
    apres = _etat(ns_id)
    assert apres["claimed_until"] != avant["claimed_until"], "témoin : bail repoussé"
    assert apres["rev"] == r0 + 2, "renouveler"
    assert renouvele["_revision"] == str(r0 + 2)
    st.release_claim(ns, "r1", worker="poste-1")
    assert _etat(ns_id)["rev"] == r0 + 3, "libérer"


@pytest.mark.parametrize("nom, prealable, requete", [
    ("écriture sans effet", None, "UPDATE datastore_rows SET data = data"),
    ("JSON réordonné", None,
     "UPDATE datastore_rows SET data = '{\"b\": 1, \"a\": 2}'::jsonb"),
    ("updated_at seul", None,
     "UPDATE datastore_rows SET updated_at = NOW() + interval '1 second'"),
    ("compteur de reprises", None, "UPDATE datastore_rows SET claims = claims + 3"),
    ("remise de claims",
     "UPDATE datastore_rows SET claims = 3, abandon_reason = 'plafond'",
     "UPDATE datastore_rows SET claims = 0, abandon_reason = NULL"),
    ("embed_dirty", None, "UPDATE datastore_rows SET embed_dirty = NOT embed_dirty"),
    ("search_vec vidé", None, "UPDATE datastore_rows SET search_vec = NULL"),
])
def test_ce_qui_ne_fait_PAS_avancer_la_revision(live, nom, prealable, requete):
    _, ns_id = _table({"a": 2, "b": 1})
    if prealable:
        _sql(prealable, ns_id)
    avant = _etat(ns_id)["rev"]
    _sql(requete, ns_id)
    assert _etat(ns_id)["rev"] == avant, f"{nom} a fait avancer la révision"


def test_le_vecteur_de_rang_recalcule_ne_fait_pas_avancer_la_revision(live):
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.search import stamp_rank_vector
    _, ns_id = _table({"nom": "durand"})
    _sql("UPDATE datastore_rows SET search_vec = NULL", ns_id)
    avant = _etat(ns_id)["rev"]
    with _connect() as conn:
        stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s", (ns_id, "r1"))
        pose = conn.execute("SELECT search_vec IS NOT NULL AS pose FROM datastore_rows" + _OU,
                            (ns_id,)).fetchone()["pose"]
    assert pose, "témoin : le vecteur a bien été recalculé"
    assert _etat(ns_id)["rev"] == avant


def test_un_patch_qui_reecrit_la_meme_valeur_ne_fait_pas_avancer_la_revision(live):
    ns, ns_id = _table({"statut": "a_faire"})
    _sql("UPDATE datastore_rows SET claims = 2", ns_id)
    avant = _etat(ns_id)
    out = _store().update_row(ns, "r1", {"statut": "a_faire"})
    apres = _etat(ns_id)
    # Témoin : l'UPDATE du patch remet `claims` à zéro — il a bien eu lieu.
    assert (avant["claims"], apres["claims"]) == (2, 0), "témoin : l'UPDATE a eu lieu"
    assert apres["rev"] == avant["rev"]
    assert out["_revision"] == str(avant["rev"])


def test_le_declencheur_n_est_pose_qu_une_fois(live):
    from oto_mcp.db import revision
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        assert revision.poser_revision_de_ligne(conn) is False, "déjà là : rien n'est reposé"
        n = conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE tgname = %s",
                         (revision.NOM_DECLENCHEUR,)).fetchone()["n"]
    assert n == 1


# ── 2. la base partagée ────────────────────────────────────────────────────────

# L'UPDATE du patch par `id` tel que le tag précédent l'émet (feu `datastore_update_row`,
# f1d03c80), mot pour mot : pendant la bascule bleu/vert, la couleur encore servie écrit
# ainsi sur la même base, sans rien savoir de `rev`.
_UPDATE_DE_L_ANCIEN_CODE = (
    "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz, "
    "       claims = 0, abandon_reason = NULL "
    "WHERE ns_id = %s AND row_id = %s "
    "RETURNING row_id, created_at, updated_at, data")


def test_une_ecriture_de_l_ANCIEN_code_fait_avancer_la_revision(live):
    from oto_mcp.datastore.errors import RevisionConflict
    from oto_mcp.db._conn import _connect
    ns, ns_id = _table({"statut": "a_faire"})
    lue = _store().get_row(ns, "r1")["_revision"]
    with _connect() as conn:
        conn.execute(_UPDATE_DE_L_ANCIEN_CODE,
                     (json.dumps({"statut": "fait"}), "2026-09-12T22:00:00Z", ns_id, "r1"))
    assert _etat(ns_id)["rev"] == int(lue) + 1, (
        "l'écriture de l'ancien code n'a pas fait avancer la révision : une précondition "
        "de la nouvelle passerait par-dessus")
    with pytest.raises(RevisionConflict):
        _store().update_row(ns, "r1", {"statut": "rate"}, expected_revision=lue)
    assert _etat(ns_id)["data"] == {"statut": "fait"}


# ── 3. l'échec fermé ───────────────────────────────────────────────────────────

def test_la_precondition_est_un_parametre_de_REQUETE_connu_du_serveur(live, monkeypatch):
    """L'ancien serveur rend `400 unknown_fields` sur ce paramètre — rien n'est écrit sans
    la protection demandée. Le nouveau doit l'accepter, et l'appliquer."""
    stub_authz(monkeypatch)
    ns, ns_id = _table({"statut": "a_faire"})
    route = {"datastore": ns, "row_id": "r1"}
    code, corps = call("me.datastore.update_row", path_params=route,
                       body={"statut": "fait"}, query=b"expected_revision=0", sub=SUB)
    assert code == 200, f"paramètre refusé par le serveur : {code} {corps}"
    assert corps["_revision"] == "1" and _etat(ns_id)["data"] == {"statut": "fait"}

    code, corps = call("me.datastore.update_row", path_params=route,
                       body={"statut": "rate"}, query=b"expected_revision=0", sub=SUB)
    assert (code, corps["error"]) == (409, "revision_conflict"), corps
    assert corps["details"] == {"current_revision": "1"}
    assert _etat(ns_id)["data"] == {"statut": "fait"}

    code, corps = call("me.datastore.update_row", path_params=route,
                       body={"statut": "rate"}, query=b"expected_revision=abc", sub=SUB)
    assert (code, corps["error"]) == (400, "invalid_row_input"), corps
    assert _etat(ns_id)["data"] == {"statut": "fait"}


def test_la_face_MCP_porte_la_precondition_et_la_refuse_sans_id(mcp):
    ns, ns_id = _table({"statut": "a_faire"})
    out = mcp("data_write", datastore=ns, id="r1", row={"statut": "fait"},
              expected_revision="0")
    assert out["_revision"] == "1"
    with pytest.raises(Exception, match="revision_conflict"):
        mcp("data_write", datastore=ns, id="r1", row={"statut": "rate"},
            expected_revision="0")
    with pytest.raises(Exception, match="expected_revision"):
        mcp("data_write", datastore=ns, row={"statut": "neuf"}, expected_revision="1")
    assert _etat(ns_id)["data"] == {"statut": "fait"}


# ── 4. la lecture ──────────────────────────────────────────────────────────────

def test_la_revision_est_servie_en_CHAINE_sur_tous_les_chemins_de_lecture(mcp, monkeypatch):
    from oto_mcp import db
    stub_authz(monkeypatch)
    ns, ns_id = _table({"statut": "a_faire"})
    st = _store()
    ecrit = st.update_row(ns, "r1", {"statut": "fait"})
    attendu = str(_etat(ns_id)["rev"])
    assert attendu == "1", "témoin : le patch a fait avancer la révision"
    route = {"datastore": ns, "row_id": "r1"}
    # Une projection ne rend `_revision` que NOMMÉE — et la nommer n'est pas une faute.
    projete = mcp("data_rows", datastore=ns, fields=["statut", "_revision"])
    assert "warning" not in projete, projete.get("warning")
    lectures = {
        "réponse du patch": ecrit,
        "par id": st.get_row(ns, "r1"),
        "liste": st.list_rows(ns)[0],
        "curseur": st.cursor_rows(ns, limit=10)["rows"][0],
        "curseur trié": st.cursor_rows(ns, limit=10, order_by="_created_at")["rows"][0],
        "rows_by_ids": db.datastore_rows_by_ids(ns_id, ["r1"])["r1"],
        "REST par id": call("me.datastore.get_row", path_params=route, sub=SUB)[1],
        "REST liste": call("me.datastore.list_rows", path_params={"datastore": ns},
                           sub=SUB)[1]["rows"][0],
        "MCP par id": mcp("data_rows", datastore=ns, id="r1"),
        "MCP projeté": projete["rows"][0],
    }
    fautes = {nom: ligne.get("_revision") for nom, ligne in lectures.items()
              if ligne.get("_revision") != attendu}
    assert not fautes, f"révision absente ou fausse (attendu {attendu!r}) : {fautes}"

    pris = st.claim_row(ns, "r1", worker="poste-1", lease_s=600)
    attendu = str(_etat(ns_id)["rev"])
    assert pris["_revision"] == attendu, "réservation"
    assert st.queue(ns)[0]["_revision"] == attendu, "file de supervision"
    st.release_claim(ns, "r1", worker="poste-1")
    suivante = st.claim_next(ns, worker="poste-2", filter={"statut": "fait"}, lease_s=600)
    assert suivante["_revision"] == str(_etat(ns_id)["rev"]), "réservation de la suivante"
    st.release_claim(ns, "r1", worker="poste-2")
    code, corps = call("me.datastore.claim_row", path_params=route,
                       body={"worker": "poste-3"}, sub=SUB)
    assert code == 200, corps
    assert corps["row"]["_revision"] == str(_etat(ns_id)["rev"]), "réservation REST"


def test_une_colonne_nommee__revision_ne_s_ecrit_jamais(live):
    """Une ligne relue puis republiée telle quelle porte `_revision` : la clé est une
    colonne de plateforme, elle ne devient jamais une donnée."""
    ns, ns_id = _table()
    _store().update_row(ns, "r1", {"_revision": "99", "statut": "fait"})
    assert _etat(ns_id)["data"] == {"statut": "fait"}
