"""`lemlist_push_rows` — des lignes poussées en leads PAR RÉFÉRENCE, sur une vraie base.

Le vrai store (`make_store`, DDL servi) et un client lemlist simulé à la CLASSE : ce
qu'on éprouve est ce que la ligne porte après le geste, et ce que le reçu dit —
jamais une valeur de la personne.

1. Le geste nominal : un lead par ligne, l'id et l'état écrits en retour.
2. L'échec partiel : doublon, erreur amont, identité manquante — chaque ligne a son
   code, les autres passent.
3. **Aucune donnée personnelle dans la réponse**, quel que soit le chemin.
4. Les règles de la file : une ligne tenue par un autre run est écartée AVANT
   l'appel ; le filtre reprend là où le lot s'est arrêté.
5. Ce qui refuse avant tout envoi : colonne inconnue, `dry_run`.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import MagicMock

import pytest

SUB = "sub-push-lemlist"
_GENS = [
    {"email": "ada@acme.test", "prenom": "Ada", "nom": "Lovelace"},
    {"email": "alan@acme.test", "prenom": "Alan", "nom": "Turing"},
    {"email": "grace@acme.test", "prenom": "Grace", "nom": "Hopper"},
]
_MAPPING = {"email": "email", "first_name": "prenom", "last_name": "nom"}
#: Tout ce qu'aucune réponse ne doit jamais contenir.
_PERSONNEL = [v for p in _GENS for v in p.values()]
#: Les colonnes du tableau, déclarées : une colonne non déclarée est refusée (oto#124).
_COLONNES = ["email", "prenom", "nom", "poste", "lemlist_lead_id", "lemlist_status"]


@pytest.fixture
def banc(live, monkeypatch):
    """Le compte, un client lemlist simulé, et l'outil tel que le serveur le monte."""
    import oto.tools.lemlist as lemlist_pkg
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import lemlist_lignes
    client = MagicMock()
    client.create_lead.side_effect = lambda cam, lead, **kw: {
        "_id": "lea_" + lead["email"].split("@")[0]}
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(access, "resolve_api_key", lambda *a, **k: ("k", False))
    monkeypatch.setattr(lemlist_pkg, "LemlistClient", lambda **kw: client)
    m = FastMCP("t")
    lemlist_lignes.register(m)
    return asyncio.run(m.get_tool("lemlist_push_rows")).fn, client


def _table(rows=_GENS) -> tuple[str, list[str]]:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", SUB, "leads-" + uuid.uuid4().hex[:6]))
    st = make_store(SUB)
    st.set_schema(ns, {"fields": [{"key": c, "type": "text"} for c in _COLONNES]})
    return ns, [str(st.append_row(ns, dict(r))["_id"]) for r in rows]


def _ligne(ns, rid) -> dict:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).get_row(ns, rid)


def _sans_donnee_personnelle(out: dict) -> None:
    texte = json.dumps(out, ensure_ascii=False)
    for v in _PERSONNEL:
        assert v not in texte, f"la réponse porte une donnée de la personne : {v!r}"


# ── 1. le geste nominal ──────────────────────────────────────────────────────

def test_chaque_ligne_devient_un_lead_et_le_sait(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids)

    assert out["pushed"] == 3 and out["errors"] == [] and out["remaining"] == 0
    assert out["stopped"] is None
    envoyes = [c.args[1] for c in client.create_lead.call_args_list]
    assert envoyes[0] == {"email": "ada@acme.test", "firstName": "Ada",
                          "lastName": "Lovelace"}, "le lead est lu CÔTÉ SERVEUR"
    ligne = _ligne(ns, ids[0])
    assert ligne["lemlist_lead_id"] == "lea_ada"
    assert ligne["lemlist_status"] == "pushed"
    _sans_donnee_personnelle(out)


def test_une_cle_hors_table_devient_une_variable_personnalisee(banc):
    fn, client = banc
    ns, ids = _table()
    fn(datastore=ns, campaign_id="cam_X", row_ids=ids[:1],
       field_mapping={"email": "email", "prenomCourt": "prenom"})
    assert client.create_lead.call_args.args[1] == {"email": "ada@acme.test",
                                                    "prenomCourt": "Ada"}


# ── 2. l'échec partiel ──────────────────────────────────────────────────────

def test_doublon_erreur_et_identite_manquante_ont_chacun_leur_code(banc):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client = banc
    ns, ids = _table([*_GENS, {"prenom": "Sans", "nom": "Adresse"}])

    def create(cam, lead, **kw):
        if lead["email"].startswith("alan"):
            raise UpstreamHTTPError(400, "Lead already in the campaign", service="lemlist")
        if lead["email"].startswith("grace"):
            raise UpstreamHTTPError(500, "boom", service="lemlist")
        return {"_id": "lea_1"}
    client.create_lead.side_effect = create

    out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids)
    assert out["pushed"] == 1 and out["duplicates"] == 1 and out["failed"] == 1
    assert out["skipped_missing_identity"] == 1
    codes = {e["row_id"]: e["code"] for e in out["errors"]}
    assert codes == {ids[2]: "lemlist_http_500", ids[3]: "missing_identity"}
    assert client.create_lead.call_count == 3, "la ligne sans identité ne part pas"
    assert _ligne(ns, ids[1])["lemlist_status"] == "duplicate"
    assert _ligne(ns, ids[1])["lemlist_status.comment"] == "already_in_campaign"
    assert _ligne(ns, ids[2])["lemlist_status"] == "failed"
    assert _ligne(ns, ids[3]).get("lemlist_status") is None
    assert "left untouched" in out["existing_left_untouched"], (
        "la réponse dit qu'un lead déjà pris n'a pas été touché")
    _sans_donnee_personnelle(out)


def test_une_campagne_introuvable_arrete_le_lot_sans_marquer_la_ligne(banc):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client = banc
    ns, ids = _table()
    client.create_lead.side_effect = UpstreamHTTPError(404, "Campaign not found",
                                                       service="lemlist")
    out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids)
    assert out["stopped"] == "lemlist_campaign_not_found"
    assert client.create_lead.call_count == 1
    assert out["remaining"] == 3, "rien n'a été traité : tout reste à pousser"
    assert _ligne(ns, ids[0]).get("lemlist_status") is None


def test_une_ligne_deja_poussee_n_est_pas_repoussee(banc):
    fn, client = banc
    ns, ids = _table()
    fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids[:1])
    client.create_lead.reset_mock()
    out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids[:1])
    assert out["skipped_already_pushed"] == 1 and not client.create_lead.called


# ── 4. la file ──────────────────────────────────────────────────────────────

def test_une_ligne_tenue_par_un_autre_run_est_ecartee_avant_l_appel(banc):
    from oto_mcp import session_org
    from oto_mcp.datastore.core import make_store
    fn, client = banc
    ns, ids = _table()
    tok = session_org.set_call_run("run-autre")
    try:
        make_store(SUB).claim_row(ns, ids[0], worker="w-autre")
    finally:
        session_org.reset_call_run(tok)

    tok = session_org.set_call_run("run-moi")
    try:
        out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING,
                 row_ids=ids)
    finally:
        session_org.reset_call_run(tok)
    assert {e["row_id"]: e["code"] for e in out["errors"]} == {ids[0]: "row_locked"}
    assert client.create_lead.call_count == 2, "aucun lead pour la ligne tenue"
    assert _ligne(ns, ids[0]).get("lemlist_status") is None


def test_le_run_qui_tient_la_ligne_la_pousse(banc):
    from oto_mcp import session_org
    from oto_mcp.datastore.core import make_store
    fn, _ = banc
    ns, ids = _table()
    tok = session_org.set_call_run("run-moi")
    try:
        make_store(SUB).claim_row(ns, ids[0], worker="w")
        out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING,
                 row_ids=ids[:1])
    finally:
        session_org.reset_call_run(tok)
    assert out["pushed"] == 1 and out["errors"] == []


def test_par_filtre_le_lot_reprend_la_ou_il_s_est_arrete(banc):
    fn, client = banc
    ns, ids = _table()
    a = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, filter={}, batch_size=2)
    assert a["pushed"] == 2 and a["remaining"] == 1
    b = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, filter={}, batch_size=2)
    assert b["pushed"] == 1 and b["remaining"] == 0
    assert client.create_lead.call_count == 3, "aucune ligne poussée deux fois"


# ── 5. ce qui refuse avant tout envoi ───────────────────────────────────────

def test_une_colonne_inconnue_refuse_le_lot_entier(banc):
    from oto_mcp.mcp_errors import McpError
    fn, client = banc
    ns, ids = _table()
    with pytest.raises(McpError) as e:
        fn(datastore=ns, campaign_id="cam_X", row_ids=ids,
           field_mapping={"email": "courriel"})
    assert e.value.error.data["code"] == "push_rows_unknown_columns"
    assert not client.create_lead.called


def test_une_colonne_libre_vide_sur_tout_le_lot_n_est_pas_inconnue(banc):
    """Une colonne vide sur les lignes de ce lot existe quand même dans le tableau."""
    fn, client = banc
    ns, ids = _table([*_GENS, {"email": "x@acme.test", "poste": "CTO"}])
    out = fn(datastore=ns, campaign_id="cam_X", row_ids=ids[:2],
             field_mapping={**_MAPPING, "job_title": "poste"})
    assert out["pushed"] == 2
    assert "jobTitle" not in client.create_lead.call_args.args[1]


def test_dry_run_n_envoie_ni_n_ecrit_rien(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, campaign_id="cam_X", field_mapping=_MAPPING, row_ids=ids,
             dry_run=True)
    assert out["would_push"] == 3 and out["dry_run"] is True
    assert not client.create_lead.called
    assert _ligne(ns, ids[0]).get("lemlist_status") is None
    _sans_donnee_personnelle(out)


def test_un_tableau_inconnu_est_un_refus_nomme(banc):
    from oto_mcp.mcp_errors import McpError
    fn, client = banc
    with pytest.raises(McpError) as e:
        fn(datastore="999999999", campaign_id="cam_X", field_mapping=_MAPPING,
           row_ids=["1"])
    assert e.value.error.data["code"] == "datastore_not_found"
