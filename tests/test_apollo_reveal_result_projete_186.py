"""`apollo_reveal_phone_result` projette `people[]` comme ses voisins (otomata-tech/oto#186).

Le sondage rendait l'enveloppe du webhook BRUTE : chaque élément de
`webhook_result.people[]` portait la fiche personne ET l'organisation complète,
pile technique comprise (~15 000 c. par personne) — le seul des quatre verbes de la
famille à ne pas projeter, et sans l'échappatoire `full`.

⚠️ Le piège : la projection existante lit `payload["person"]`. Une réutilisation
littérale ne mordrait sur rien, et un test qui ne vérifierait que « la clé
`projection` existe » ou « la réponse est un dict » resterait vert. On vérifie donc
sur `people[]` LUI-MÊME que les blocs de masse ont disparu, et que les numéros sont
restés. Doublure du client : aucun appel réel à Apollo."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from fastmcp import FastMCP

from oto_mcp import access


def _personne(pid: str) -> dict:
    return {
        "id": pid, "name": "Prénom Nom", "title": "CEO",
        "phone_numbers": [{"sanitized_number": "+33600000000", "type_cd": "mobile",
                           "dnc_status_cd": None}],
        "employment_history": [{"organization_name": "X"}] * 20,
        "account": {"id": "acc", "name": "Employeur", "blob": "y" * 1500},
        "organization": {
            "name": "Employeur", "primary_domain": "employeur.example",
            "current_technologies": [{"name": f"tech{i}"} for i in range(300)],
            "technology_names": [f"tech{i}" for i in range(300)],
            "keywords": ["k"] * 100, "funding_events": [], "suborganizations": [],
        },
    }


def _sonder(monkeypatch, **kw):
    import oto.tools.apollo.client as apollo_client
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.poll_webhook_result.return_value = {
        "done": True,
        "result": {"request_id": 718432950164203900, "webhook_status": "delivered",
                   "failure_reason": None,
                   "webhook_result": {"people": [_personne("p1"), _personne("p2")]}},
    }
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: MagicMock(key="k", is_platform=False))
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **k: client)
    # Le receveur d'oto (éprouvé dans `test_apollo_receveur.py`) : rien de livré,
    # donc le sondage d'Apollo répond — le chemin que ce banc décrit.
    from oto_mcp import apollo_receiver
    monkeypatch.setattr(apollo_receiver, "commander",
                        lambda cle, plafond=None: ("jeton", "https://mcp.acme.test/api/receivers/apollo/phones/jeton"))
    monkeypatch.setattr(apollo_receiver, "lier", lambda j, rid: None)
    monkeypatch.setattr(apollo_receiver, "abandonner", lambda j: None)
    monkeypatch.setattr(apollo_receiver, "resultat_recu", lambda rid, cle: None)
    m = FastMCP("t")
    apollo_tool.register(m)
    fn = asyncio.run(m.get_tool("apollo_reveal_phone_result")).fn
    return fn(request_id="718432950164203900", **kw)


def test_le_defaut_projette_chaque_element_de_people(monkeypatch):
    out = _sonder(monkeypatch)
    people = out["result"]["webhook_result"]["people"]
    assert len(people) == 2
    for p in people:
        assert "employment_history" not in p and "account" not in p, (
            "la fiche personne sort brute : la projection n'a pas mordu sur people[]")
        assert "current_technologies" not in p["organization"]
        assert "technology_names" not in p["organization"]
        # ce qu'on vient chercher reste
        assert p["phone_numbers"][0]["sanitized_number"] == "+33600000000"
        assert p["id"] and p["name"] and p["organization"]["name"] == "Employeur"
    # la projection se DIT, sur le chemin réel, avec l'échappatoire
    bloc = out["projection"]
    assert bloc["how_to_get_everything"] == "full=True"
    assert "result.webhook_result.people[].employment_history" in bloc["dropped"]
    # l'enveloppe et l'écho d'identifiant restent
    assert out["result"]["request_id"] == "718432950164203900"
    assert out["result"]["webhook_status"] == "delivered"


def test_le_defaut_est_nettement_plus_leger(monkeypatch):
    leger = len(json.dumps(_sonder(monkeypatch)))
    entier = len(json.dumps(_sonder(monkeypatch, full=True)))
    assert leger * 4 < entier, (leger, entier)


def test_full_rend_l_enveloppe_entiere(monkeypatch):
    out = _sonder(monkeypatch, full=True)
    assert "projection" not in out
    p = out["result"]["webhook_result"]["people"][0]
    assert p == _personne("p1")
