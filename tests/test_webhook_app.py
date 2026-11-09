"""`oto_webhook_app` — la carte d'un agent webhook : son état, le type de jeton, un
bouton qui OUVRE l'écran où la personne tire le porteur. Vrai prefab_ui, base stubbée.

Prouvé : le bouton porte le lien `webhook` (rien d'autre, aucun outil appelé, aucun
porteur nulle part), la pastille suit le journal des livraisons (lu SANS corps), le type
de jeton suit le préréglage, un refus est une carte en mots fixes, et un agent programmé
n'est pas un webhook.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import FastMCP

pytest.importorskip("prefab_ui")

from oto_mcp import access, db, links  # noqa: E402
from oto_mcp.capabilities import _acces_agent  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied  # noqa: E402
from oto_mcp.tools import webhook_app  # noqa: E402

ORG = 77


@pytest.fixture()
def banc(monkeypatch):
    etat = {"agent": {"id": 5, "kind": "webhook", "label": "Signups", "hook_auth": None},
            "livraisons": [], "corps_demande": []}
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u-carte")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(db, "get_trigger",
                        lambda i, o: etat["agent"] if i == 5 and o == ORG else None)

    def _livraisons(i, o, limit=50, en_attente=False, avec_corps=False):
        etat["corps_demande"].append(avec_corps)
        return etat["livraisons"]
    monkeypatch.setattr(db, "livraisons", _livraisons)
    monkeypatch.setattr(_acces_agent, "exiger", lambda ctx, t, requis: "owner")
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p:
                        f"https://app.example.test/org/{p['org']}/webhook/{p['id']}"
                        if kind == "webhook" else None)
    m = FastMCP("t")
    webhook_app.register(m)
    etat["fn"] = asyncio.run(m.get_tool("oto_webhook_app")).fn
    return etat


def _carte(res) -> str:
    sc = res.structured_content
    return json.dumps(sc if isinstance(sc, dict) else sc.to_json(), default=str)


def test_en_attente_du_jeton_le_bouton_ouvre_l_ecran(banc):
    res = banc["fn"](trigger_id=5)
    carte = _carte(res)
    url = f"https://app.example.test/org/{ORG}/webhook/5"
    assert "openLink" in carte and url in carte
    assert "Signups" in carte and "Waiting for its token" in carte
    assert "Get the token" in carte and "Authorization header" in carte
    assert "otoh_" not in carte and "toolCall" not in carte
    assert url in res.content[0].text and "never see the token" in res.content[0].text
    # Le journal se lit SANS les corps (données de tiers).
    assert banc["corps_demande"] == [False]


def test_qui_recoit_deja_dit_receiving(banc):
    banc["livraisons"] = [{"outcome": "queued", "received_at": "2026-11-06 10:42"}]
    carte = _carte(banc["fn"](trigger_id=5))
    assert "Receiving events" in carte and "Open webhook setup" in carte


def test_un_refus_ne_compte_pas_comme_recu(banc):
    banc["livraisons"] = [{"outcome": "refused_rate", "received_at": "2026-11-06 10:42"}]
    assert "Waiting for its token" in _carte(banc["fn"](trigger_id=5))


def test_le_type_de_jeton_suit_le_prereglage(banc):
    banc["agent"]["hook_auth"] = "body_secret"
    assert "`secret` field" in _carte(banc["fn"](trigger_id=5))
    banc["agent"]["hook_auth"] = "standard_webhooks"
    carte = _carte(banc["fn"](trigger_id=5))
    assert "Set up on its page" in carte and "Get the token" not in carte


def test_un_agent_programme_n_est_pas_un_webhook(banc):
    banc["agent"]["kind"] = "schedule"
    carte = _carte(banc["fn"](trigger_id=5))
    assert "Cannot show this webhook" in carte and "schedule" in carte
    assert "openLink" not in carte


def test_inconnu_ou_sans_droit_mots_fixes(banc, monkeypatch):
    carte = _carte(banc["fn"](trigger_id=999))
    assert "No webhook agent with that id" in carte

    def _refus(ctx, t, requis):
        raise AuthzDenied(403, "trigger_forbidden", "x <script>")
    monkeypatch.setattr(_acces_agent, "exiger", _refus)
    carte = _carte(banc["fn"](trigger_id=5))
    assert "can't be shown here" in carte and "<script>" not in carte


def test_sans_ecran_declare_pas_de_bouton(banc, monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: None)
    res = banc["fn"](trigger_id=5)
    assert "openLink" not in _carte(res) and "No link" in res.content[0].text
