"""`apollo_reveal_phone` servait sa fiche ENTIÈRE, et c'était le seul des trois.

Mesuré sur un appel RÉEL en production le 2026-09-11 (clé payante, une personne) :
**65 374 caractères**, dont `person.organization` 59 244 et `employment_history`
4 396. Le client MCP a REFUSÉ la réponse — `exceeds maximum allowed tokens`. Donc :
les crédits dépensés, les numéros bel et bien commandés, et l'agent incapable de
lire quoi que ce soit. C'est exactement la panne que la projection existe pour
empêcher, sur le seul outil qui l'avait manquée — et c'est l'outil du signalement
client.

Second piège vu au même appel : Apollo rend DEUX identifiants. `request_id` au
premier niveau (entier signé 64 bits, celui qui sonde) et
`phone_enrichment.request_id` (interne, qui ne sonde rien). Le message d'Apollo
dit lui-même d'employer « the top-level `request_id` ». Deux identifiants dont un
seul marche, dans la même réponse, c'est un piège : on retire celui qui ne sert
pas et on le NOMME.

⚠️ Les fiches de ce fichier sont SYNTHÉTIQUES, à la forme du vrai payload : le
relevé de production porte une personne réelle, et ce dépôt est public.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

_WEBHOOK = "https://hooks.acme.test/apollo"


def _fiche(bavarde: bool = True) -> dict:
    """Une fiche à la FORME du relevé de production (volumes comparables)."""
    org = {"name": "Acme", "primary_domain": "acme.test", "phone": "+33100000000",
           "industry": "software", "estimated_num_employees": 8000}
    if bavarde:
        org |= {"current_technologies": [{"uid": f"t{i}"} for i in range(400)],
                "technology_names": [f"n{i}" for i in range(200)],
                "funding_events": [{"amount": i} for i in range(40)],
                "suborganizations": [{"id": f"s{i}"} for i in range(30)],
                "keywords": [f"k{i}" for i in range(300)]}
    fiche = {"id": "p1", "first_name": "Jane", "last_name": "Doe",
             "email": "jane@acme.test", "organization": org}
    if bavarde:
        fiche |= {"employment_history": [{"org": f"o{i}"} for i in range(40)],
                  "account": {"id": "a1", "name": "Acme", "raw": "x" * 1500}}
    return fiche


def _mount(monkeypatch, *, match_return=None):
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.match_person.return_value = match_return if match_return is not None else {
        "person": _fiche(),
        "request_id": -4131798613199158083,
        "phone_enrichment": {
            "request_id": "6aa46cfc7483f60018032fef",
            "status": "pending",
            "message": "…poll with the top-level `request_id` field…"},
    }
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: MagicMock(key="k-byo", is_platform=False))
    monkeypatch.setattr(access, "resolve_api_key", lambda *a, **k: ("k", False))
    monkeypatch.setattr(access, "platform_quota_hint", lambda p: None)
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)
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
    return asyncio.run(m.get_tool("apollo_reveal_phone")).fn


def test_a_reveal_no_longer_serves_a_payload_the_client_refuses(monkeypatch):
    fn = _mount(monkeypatch)
    out = fn(webhook_url=_WEBHOOK, person_id="p1")
    brut = len(json.dumps({"person": _fiche(), "request_id": 1}))
    servi = len(json.dumps(out))
    assert servi * 4 < brut, f"allègement insuffisant : {brut} -> {servi}"
    org = out["person"]["organization"]
    for lourd in ("current_technologies", "technology_names", "funding_events",
                  "suborganizations", "keywords"):
        assert lourd not in org
    for lourd in ("employment_history", "account"):
        assert lourd not in out["person"]
    # ce qu'on vient chercher RESTE
    assert out["person"]["email"] == "jane@acme.test"
    assert out["person"]["last_name"] == "Doe"
    assert org["name"] == "Acme" and org["phone"] == "+33100000000"


def test_the_id_that_does_not_poll_is_removed_and_named(monkeypatch):
    """Deux identifiants dont un seul sonde, c'est un piège — Apollo le dit
    lui-même dans le message qu'on garde."""
    fn = _mount(monkeypatch)
    out = fn(webhook_url=_WEBHOOK, person_id="p1")
    assert "request_id" not in out["phone_enrichment"]
    assert "phone_enrichment.request_id" in out["projection"]["dropped"]
    # le message d'Apollo, qui EXPLIQUE lequel vaut, reste
    assert "top-level" in out["phone_enrichment"]["message"]
    # et l'identifiant qui sonde est bien celui du premier niveau, en chaîne
    assert out["request_id"] == "-4131798613199158083"


def test_full_true_returns_everything_including_the_trap(monkeypatch):
    fn = _mount(monkeypatch)
    out = fn(webhook_url=_WEBHOOK, person_id="p1", full=True)
    assert "current_technologies" in out["person"]["organization"]
    assert "employment_history" in out["person"]
    assert out["phone_enrichment"]["request_id"] == "6aa46cfc7483f60018032fef"
    assert "projection" not in out


def test_a_lean_payload_announces_no_projection(monkeypatch):
    """Ne pas annoncer une coupe qui n'a rien coupé."""
    fn = _mount(monkeypatch, match_return={"person": _fiche(bavarde=False),
                                           "request_id": 7})
    out = fn(webhook_url=_WEBHOOK, person_id="p1")
    assert "projection" not in out


def test_a_no_match_is_untouched_by_the_projection(monkeypatch):
    fn = _mount(monkeypatch, match_return={"person": None})
    out = fn(webhook_url=_WEBHOOK, person_id="p1")
    assert out["matched"] is False
    assert "projection" not in out


# --------------------------------------------------------------------------- #
# Le sondage ré-échote l'identifiant — en nombre, donc abîmé
# --------------------------------------------------------------------------- #

def test_the_poll_does_not_echo_a_damaged_id(monkeypatch):
    """Mesuré en production le 2026-09-12 : sondé avec `-8351464734221602674`,
    l'enveloppe d'Apollo rendait `-8351464734221603000` — 326 d'écart, signature
    du float64. Un agent qui relit `result.request_id` pour re-sonder plus tard
    range un identifiant qui ne sonde rien."""
    import asyncio
    from unittest.mock import MagicMock
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.poll_webhook_result.return_value = {
        "done": True,
        "result": {"request_id": -8351464734221602674,
                   "webhook_status": "success",
                   "webhook_result": {"people": [{"phone_numbers": [{}]}]}},
    }
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: MagicMock(key="k", is_platform=False))
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)
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
    out = fn(request_id="-8351464734221602674")

    rid = out["result"]["request_id"]
    assert isinstance(rid, str), "l'écho doit sortir en CHAÎNE"
    assert rid == "-8351464734221602674", "et identique à ce qu'on a sondé"
    # le reste de l'enveloppe est intact
    assert out["result"]["webhook_status"] == "success"
    assert out["result"]["webhook_result"]["people"]
