"""`db.google_grant_holders` sur le vrai coffre : toutes les lignes qui portent un compte
Google, quelle que soit l'entité — c'est la liste que `auth/google._grant_held_elsewhere`
consulte avant de révoquer chez Google. Un simulacre rendrait ce qu'on lui a appris ;
ce qu'on vérifie ici, c'est que la requête voit le membre, l'équipe et l'org à la fois,
et qu'elle lit l'émetteur sans déchiffrer.
"""
from __future__ import annotations

import os

import pytest

from oto_mcp import credentials_store as cs
from oto_mcp import db

ORG, AUTRE_ORG, EQUIPE = 7401, 7402, 7403


def _poser(entity_type, entity_id, account, client_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cs._upsert(conn, entity_type, str(entity_id), "google", account, "RT-FICTIF",
                   "usr_banc", {"client_id": client_id} if client_id else {})


@pytest.fixture(scope="module")
def coffre(live):
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ.setdefault("OTO_MCP_MASTER_KEY", "0" * 64)
    _poser(cs.MEMBER, cs.member_id(ORG, "usr_a"), "boite@x.test", "cid-env")
    _poser("org", ORG, "boite@x.test", "cid-env")
    _poser(cs.MEMBER, cs.member_id(AUTRE_ORG, "usr_a"), "boite@x.test", None)
    _poser("group", EQUIPE, "autre@x.test", "cid-env")
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)


def test_toutes_les_entites_qui_portent_le_compte(coffre):
    lignes = sorted((h["entity_type"], h["entity_id"], h["client_id"])
                    for h in db.google_grant_holders("boite@x.test"))
    assert lignes == sorted([
        ("member", f"{ORG}:usr_a", "cid-env"),
        ("org", str(ORG), "cid-env"),
        ("member", f"{AUTRE_ORG}:usr_a", None),
    ])
    assert db.google_grant_holders("personne@x.test") == []
