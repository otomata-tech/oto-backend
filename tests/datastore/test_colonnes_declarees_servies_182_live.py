"""Toute colonne déclarée est servie à `null` — rejoué sur PostgreSQL (oto#182).

Le critère de la mission qui a porté la demande : une colonne jamais écrite est PRÉSENTE
à `null` dans la ligne que rend la réservation (`claim_next`) comme dans la lecture, et
rien n'est écrit en base pour autant.
"""
from __future__ import annotations

import uuid

from oto_mcp.datastore.core import make_store


def _table():
    from oto_mcp import db
    ns = "t182-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    st = make_store("sub-test")
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison", "type": "text"},
        {"key": "site_web", "type": "text"}]})
    return st, ns, ns_id


def _stockee(ns_id: int, row_id: str) -> dict:
    from oto_mcp import db
    return db.datastore_get_row(ns_id, row_id)["data"]


def test_la_lecture_et_la_reservation_servent_la_colonne_jamais_ecrite(live):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "1", "raison": "ACME"})["_id"]

    assert st.get_row(ns, rid)["site_web"] is None
    (ligne,) = st.list_rows(ns)
    assert (ligne["raison"], ligne["site_web"]) == ("ACME", None)

    reservee = st.claim_next(ns, worker="w-182")
    assert reservee is not None and reservee["_id"] == rid
    assert "site_web" in reservee and reservee["site_web"] is None, (
        "la ligne réservée ne porte pas la colonne déclarée jamais écrite")
    assert "site_web" not in _stockee(ns_id, rid), "la complétion a écrit en base"


def test_une_colonne_ajoutee_au_schema_est_servie_sans_rien_ecrire(live):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "2", "raison": "BETA"})["_id"]
    avant = _stockee(ns_id, rid)

    st.patch_schema(ns, fields=[{"key": "telephone", "type": "text"}])

    assert st.get_row(ns, rid)["telephone"] is None
    assert _stockee(ns_id, rid) == avant, "le stockage a bougé"


def test_une_valeur_ecrite_nest_pas_remplacee_par_null(live):
    st, ns, _ = _table()
    rid = st.append_row(ns, {"siren": "3", "raison": "GAMMA", "site_web": "gamma.fr"})["_id"]
    assert st.get_row(ns, rid)["site_web"] == "gamma.fr"
