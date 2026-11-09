"""Le réglage Slack d'une org, « mention seule » (`db/slack_reglage.py`,
`oto_org_settings domain=slack`).

1. Défaut FALSE : une org qui ne pose rien garde le comportement du 29/10/2026.
2. La console : `get` rend le réglage, `set` exige `mention_only`, une org inconnue est
   un 404 nommé — jamais un « faux » deviné.
3. En base : la colonne se pose et se relit ; une org absente se dit (None / False).
"""
from __future__ import annotations

import psycopg
import pytest

from oto_mcp import db
from oto_mcp.capabilities import org_console as OC
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def stockage(monkeypatch):
    etat = {7: False}
    monkeypatch.setattr(db.slack_reglage, "mention_seule", lambda org: etat.get(org))

    def _poser(org, v):
        if org not in etat:
            return False
        etat[org] = v
        return True

    monkeypatch.setattr(db.slack_reglage, "poser_mention_seule", _poser)
    return etat


def _reglage(org_id=7, **kw):
    return OC._org_settings(ResolvedCtx(sub="admin", org_id=org_id),
                            OC.OrgSettingsInput(domain="slack", org_id=org_id, **kw))


def test_get_rend_le_defaut_sans_rien_poser(stockage):
    assert _reglage(op="get") == {"org_id": 7, "mention_only": False}
    assert stockage == {7: False}


def test_set_pose_puis_retire(stockage):
    assert _reglage(op="set", mention_only=True) == {"org_id": 7, "mention_only": True}
    assert stockage[7] is True
    assert _reglage(op="set", mention_only=False)["mention_only"] is False


@pytest.mark.parametrize("kw, statut, code", [
    ({"op": "set"}, 400, "missing_mention_only"),
    ({"op": "preview"}, 400, "unsupported_op"),
    ({"op": "get", "org_id": 8}, 404, "unknown_org"),
    ({"op": "set", "org_id": 8, "mention_only": True}, 404, "unknown_org"),
])
def test_les_refus_sont_nommes_et_n_ecrivent_rien(stockage, kw, statut, code):
    with pytest.raises(AuthzDenied) as e:
        _reglage(**kw)
    assert (e.value.status, e.value.code) == (statut, code)
    assert stockage == {7: False}


def test_la_description_servie_nomme_le_domaine():
    cap = next(c for c in OC.CAPABILITIES if c.key == "org.settings.console")
    assert "slack (the org's Slack app" in cap.description
    assert "mention_only" in cap.description


def test_la_colonne_se_pose_et_se_relit(live, pg_module_dsn):
    from oto_mcp.db import slack_reglage
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        org = c.execute("INSERT INTO orgs (name) VALUES ('Slack') RETURNING id"
                        ).fetchone()[0]
    assert slack_reglage.mention_seule(org) is False, "une org d'avant : le défaut"
    assert slack_reglage.poser_mention_seule(org, True)
    assert slack_reglage.mention_seule(org) is True
    assert slack_reglage.poser_mention_seule(org, False)
    assert slack_reglage.mention_seule(org) is False
    assert slack_reglage.mention_seule(10**12) is None
    assert slack_reglage.poser_mention_seule(10**12, True) is False
