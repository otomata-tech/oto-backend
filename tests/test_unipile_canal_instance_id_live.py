"""`channels[].instance_id` (#1499) : le propriétaire d'un compte de messagerie hébergée
lit l'id de SA sous-instance (`unipile`, `canal:<PROVIDER>`) dans son état Unipile — c'est
lui qu'il passe aux règles pour ouvrir son compte à d'autres. Un canal non connecté rend
null. Sur une vraie base : la sous-instance est celle que la connexion a posée.
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def compte(live):
    from oto_mcp import db
    from oto_mcp.org_store import members, orgs
    u = uuid.uuid4().hex[:6]
    sub = f"canal_{u}"
    db.upsert_user(sub, email=f"{sub}@x.test", name=sub)
    org = orgs.create_org(f"Canal {u}", created_by=sub)
    members.add_org_member(org, sub, "org_member")
    db.set_unipile_account(sub, f"ACC-{u}", None, org_id=org, provider="LINKEDIN")
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        inst = conn.execute(
            "SELECT id FROM connector_instances WHERE connector = 'unipile' "
            "AND owner_type = 'member' AND owner_id = %s AND account = 'canal:LINKEDIN' "
            "AND revoked_at IS NULL", (f"{org}:{sub}",)).fetchone()["id"]
    return {"sub": sub, "org": org, "inst": inst}


def test_le_canal_connecte_porte_sa_sous_instance_l_autre_null(compte):
    from oto_mcp import db
    from oto_mcp.tools import unipile
    accts = {a["provider"]: a for a in db.list_unipile_accounts(compte["sub"])
             if a.get("org_id") == compte["org"]}
    canaux = unipile._channels_from(accts, db.unipile_channel_instance_ids(compte["sub"]))
    assert canaux["linkedin"]["connected"] is True
    assert canaux["linkedin"]["instance_id"] == compte["inst"]
    assert canaux["whatsapp"]["connected"] is False
    assert canaux["whatsapp"]["instance_id"] is None


def test_une_seule_lecture_pour_tous_les_canaux(compte):
    """Les ids se lisent en UNE requête par appel, jamais une par canal."""
    from unittest import mock

    from oto_mcp import db
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        ids = db.unipile_channel_instance_ids(compte["sub"])
    assert ids == {(compte["org"], "LINKEDIN"): compte["inst"]}
    assert len(vues) == 1
