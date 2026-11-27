"""Apollo — ce qu'une CONSTRUCTION DE LISTE exige, et que la surface ne rendait pas.

Trois défauts mesurés en production le 2026-09-11, sur le chemin exact d'un client
qui « source des contacts et veut leurs téléphones » :

1. **Aucun match en LOT.** `people/bulk_match` (≤10 par appel) est ce qu'emploie
   toute construction de liste : un search rend des centaines de personnes aux noms
   obfusqués, qu'il faut révéler. En unitaire, 300 personnes = 300 allers-retours.
   ⚠️ Le lot n'économise AUCUN crédit (Apollo facture à la personne) — il économise
   les appels. Le compteur plateforme doit donc débiter PAR ENTRÉE, jamais 1 pour
   l'appel, sinon le pot commun paie dix fois moins que ce qu'il consomme.
2. **Un match rendait 60 701 caractères pour UNE personne** — dont 55 404 de fiche
   entreprise, et 32 321 pour la seule clé `current_technologies`. L'appel DÉPASSAIT
   la limite de sortie du client MCP : sourcer en boucle était impossible. Denylist
   NOMMÉE (leçon `fr_get`), `full=True` rend le brut.
3. **`request_id` partait en NOMBRE.** Entier signé 64 bits (~7,2e17) : au-delà de la
   précision d'un float JavaScript. Relevé en prod : `-4604290848231370000`, quatre
   zéros de queue — la valeur était déjà réécrite. Un agent qui la repasse à
   `apollo_reveal_phone_result` sonde un identifiant qui n'existe pas.

Mock la CLASSE client (jamais `requests`) — cf. `tests/test_apollo_location_filters.py`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

_DEFAUT = object()


def _mount(monkeypatch, *, byo: bool = True, match_return=_DEFAUT, bulk_return=_DEFAUT):
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import apollo as apollo_tool

    grosse_org = {"name": "Acme", "primary_domain": "acme.test", "phone": "+33100000000",
                  "current_technologies": ["t"] * 500, "technology_names": ["n"] * 200,
                  "funding_events": [{"x": 1}] * 50, "suborganizations": [{"y": 2}] * 40,
                  "keywords": ["k"] * 300}
    client = MagicMock()
    client.match_person.return_value = (
        {"person": {"id": "p1", "organization": dict(grosse_org)},
         "request_id": -4604290848231370000}
        if match_return is _DEFAUT else match_return)
    client.bulk_match_people.return_value = (
        {"matches": [{"id": "p1", "organization": dict(grosse_org)},
                     {"id": "p2", "organization": dict(grosse_org)}]}
        if bulk_return is _DEFAUT else bulk_return)
    usage: list[tuple] = []

    def _resolve(provider, want="auto", *a, **k):
        if not byo:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message="No `apollo` credential configured for you"))
        return MagicMock(key="k-byo")

    monkeypatch.setattr(access, "resolve_credential", _resolve)
    monkeypatch.setattr(access, "resolve_api_key", lambda *a, **k: ("k", not byo))
    from _releve import capter_units
    capter_units(monkeypatch, "apollo", lambda p, n: usage.append((p, n)))
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
    return m, client, usage


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --------------------------------------------------------------------------- #
# 1. Le lot
# --------------------------------------------------------------------------- #

def test_a_lot_debits_one_unit_PER_PERSON_not_one_for_the_call(monkeypatch):
    """Apollo facture à la personne. Débiter 1 pour un lot de 10 ferait payer au
    pot commun le dixième de ce qu'il consomme."""
    m, _, usage = _mount(monkeypatch, byo=False)
    _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(10)])
    assert usage == [("apollo", 10)]


def test_a_plain_lot_works_on_the_shared_key(monkeypatch):
    m, client, _ = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_bulk_match")(people=[{"id": "p1"}])
    assert client.bulk_match_people.called
    assert "matches" in out


@pytest.mark.parametrize("kwargs", [
    {"reveal_personal_emails": True},
    {"reveal_phone_number": True, "webhook_url": "https://hooks.acme.test/a"},
])
def test_a_lot_that_REVEALS_needs_your_own_key(monkeypatch, kwargs):
    from oto_mcp.mcp_errors import McpError

    m, client, usage = _mount(monkeypatch, byo=False)
    with pytest.raises(McpError):
        _tool(m, "apollo_bulk_match")(people=[{"id": "p1"}], **kwargs)
    assert not client.bulk_match_people.called
    assert usage == [], "rien ne doit être débité sur un refus"


def test_the_lot_forwards_people_and_flags(monkeypatch):
    m, client, _ = _mount(monkeypatch)
    _tool(m, "apollo_bulk_match")(
        people=[{"id": "p1"}, {"id": "p2"}], reveal_personal_emails=True)
    kw = client.bulk_match_people.call_args
    assert kw.args[0] == [{"id": "p1"}, {"id": "p2"}]
    assert kw.kwargs["reveal_personal_emails"] is True
    assert kw.kwargs["reveal_phone_number"] is None


def test_a_phone_lot_hands_back_a_string_id_and_where_to_collect(monkeypatch):
    m, _, _ = _mount(monkeypatch, bulk_return={
        "matches": [{"id": "p1"}], "request_id": -4604290848231370000})
    out = _tool(m, "apollo_bulk_match")(
        people=[{"id": "p1"}], reveal_phone_number=True,
        webhook_url="https://hooks.acme.test/a")
    assert out["request_id"] == "-4604290848231370000"
    assert isinstance(out["request_id"], str)
    assert "apollo_reveal_phone_result" in out["next_step"]


# --------------------------------------------------------------------------- #
# 2. Le payload
# --------------------------------------------------------------------------- #

def test_a_single_match_sheds_the_company_mass_and_NAMES_it(monkeypatch):
    m, _, _ = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_match_person")(person_id="p1")
    org = out["person"]["organization"]
    for lourd in ("current_technologies", "technology_names", "funding_events",
                  "suborganizations", "keywords"):
        assert lourd not in org
    # ce qui sert à sourcer RESTE
    assert org["name"] == "Acme" and org["primary_domain"] == "acme.test"
    assert org["phone"] == "+33100000000"
    assert out["projection"]["how_to_get_everything"] == "full=True"
    assert any("current_technologies" in d for d in out["projection"]["dropped"])


def test_full_true_gives_the_raw_payload_back(monkeypatch):
    m, _, _ = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_match_person")(person_id="p1", full=True)
    assert "current_technologies" in out["person"]["organization"]
    assert "projection" not in out


def test_the_lot_sheds_the_mass_on_EVERY_match(monkeypatch):
    """À 10 personnes la fiche entreprise est recopiée 10 fois — c'est là que le
    payload devient illisible, pas sur l'unitaire."""
    m, _, _ = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_bulk_match")(people=[{"id": "p1"}, {"id": "p2"}])
    assert len(out["matches"]) == 2
    for match in out["matches"]:
        assert "current_technologies" not in match["organization"]
        assert match["organization"]["name"] == "Acme"
    assert "projection" in out


def test_a_lot_survives_a_null_match(monkeypatch):
    """Apollo rend `null` là où rien n'a matché — la projection ne doit pas y
    trébucher."""
    m, _, _ = _mount(monkeypatch, byo=False, bulk_return={
        "matches": [None, {"id": "p2", "organization": {"name": "Acme",
                                                        "keywords": ["k"] * 10}}]})
    out = _tool(m, "apollo_bulk_match")(people=[{"id": "p1"}, {"id": "p2"}])
    assert out["matches"][0] is None
    assert "keywords" not in out["matches"][1]["organization"]


# --------------------------------------------------------------------------- #
# 3. request_id
# --------------------------------------------------------------------------- #

def test_a_plain_match_no_longer_leaks_a_float_damaged_id(monkeypatch):
    """Apollo rend un `request_id` à CHAQUE match, reveal ou pas. Relevé en prod :
    `-4604290848231370000`, déjà réécrit par un float64 en aval."""
    m, _, _ = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_match_person")(person_id="p1")
    assert out["request_id"] == "-4604290848231370000"
    assert isinstance(out["request_id"], str)


def test_an_id_already_a_string_is_left_alone(monkeypatch):
    m, _, _ = _mount(monkeypatch, byo=False,
                     match_return={"person": {"id": "p1"}, "request_id": "42"})
    assert _tool(m, "apollo_match_person")(person_id="p1")["request_id"] == "42"


def test_no_request_id_invents_none(monkeypatch):
    m, _, _ = _mount(monkeypatch, byo=False, match_return={"person": {"id": "p1"}})
    assert "request_id" not in _tool(m, "apollo_match_person")(person_id="p1")


# --------------------------------------------------------------------------- #
# 4. La ligne FACTURÉE — un autre compteur que le quota (oto-backend#935)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("byo,kwargs", [(False, {}),
                                        (True, {"reveal_personal_emails": True})])
def test_a_lot_writes_its_BILLED_quantity_on_any_key(monkeypatch, byo, kwargs):
    """`units` tient le QUOTA de la clé commune ; la ligne FACTURÉE
    (`tool_calls.quantity`) est écrite par `note_call_trace`. Sans elle la quantité
    reste NULL, que le consommateur lit 1 : un lot de 10 se facturait 1.
    Inconditionnelle (clé commune comme clé propre), comme `fullenrich`."""
    from oto_mcp import session_org
    traces = []
    monkeypatch.setattr(session_org, "note_call_trace", lambda **kw: traces.append(kw))
    m, _, _ = _mount(monkeypatch, byo=byo)
    _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(10)], **kwargs)
    assert [t["quantity"] for t in traces if "quantity" in t] == [10]


# --------------------------------------------------------------------------- #
# 5. Ce que pèse un lot SERVI (oto-backend#935)
# --------------------------------------------------------------------------- #

def _fiche_de_lot(i: int) -> dict:
    """Une fiche de lot à la FORME de l'exemple que documente Apollo pour
    `people/bulk_match` : l'historique d'emploi et la fiche société du CRM y font
    l'essentiel du poids. Contenu fictif ; seules les proportions comptent."""
    return {
        "id": f"p{i}", "first_name": "Ada", "last_name": "Lovelace",
        "name": "Ada Lovelace", "title": "Head of Growth", "email": f"ada{i}@acme.test",
        "linkedin_url": f"https://www.linkedin.com/in/ada-{i}", "city": "Paris",
        "employment_history": [
            {"organization_name": f"Org {j}", "title": "t" * 60, "id": "e" * 24,
             "start_date": "2019-01-01", "end_date": None, "description": "d" * 230}
            for j in range(9)],
        "account": {"id": "a" * 24, "name": "Acme", "domain": "acme.test",
                    "label_ids": ["l" * 24] * 10, "notes": "n" * 2000},
        "organization": {"name": "Acme", "primary_domain": "acme.test",
                         "phone": "+33100000000", "industry": "software",
                         "short_description": "s" * 320, "keywords": ["k" * 10] * 25},
    }


def _lot_de_dix() -> dict:
    return {"status": "success", "matches": [_fiche_de_lot(i) for i in range(10)]}


def test_a_default_lot_of_ten_fits_where_the_raw_one_did_not(monkeypatch):
    """Sur la forme documentée par Apollo, un lot de 10 servait 85 941 c. — et
    60 693 c. débordaient déjà un client MCP pour UNE personne. Tronqué par le
    client, un lot est perdu ET payé : le défaut doit tenir loin dessous. Le premier
    assert est le contrôle positif — sans lui, un banc qui ne déborderait plus
    jamais rendrait ce test vert pour rien."""
    from fastmcp.tools.base import default_serializer
    m, _, _ = _mount(monkeypatch, byo=False, bulk_return=_lot_de_dix())
    people = [{"id": f"p{i}"} for i in range(10)]
    brut = len(default_serializer(_tool(m, "apollo_bulk_match")(people=people, full=True)))
    defaut = len(default_serializer(_tool(m, "apollo_bulk_match")(people=people)))
    assert brut > 60_000, f"le banc ne reproduit plus un lot qui déborde ({brut} c.)"
    assert defaut < 30_000, f"un lot de 10 servi par défaut pèse {defaut} c."


def test_a_lot_drops_the_two_heavy_blocks_on_EVERY_match_and_NAMES_them(monkeypatch):
    m, _, _ = _mount(monkeypatch, byo=False, bulk_return=_lot_de_dix())
    out = _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(10)])
    for match in out["matches"]:
        assert "employment_history" not in match and "account" not in match
        assert match["name"] == "Ada Lovelace"
        assert match["organization"]["name"] == "Acme"
    assert {"employment_history", "account"} <= set(out["projection"]["dropped"])
    assert out["projection"]["how_to_get_everything"] == "full=True"
    brut = _tool(m, "apollo_bulk_match")(people=[{"id": "p0"}], full=True)
    assert "employment_history" in brut["matches"][0] and "account" in brut["matches"][0]


# --------------------------------------------------------------------------- #
# Quota : taille du lot déclarée à la résolution, débit = ce qu'Apollo facture (oto#168)
# --------------------------------------------------------------------------- #

def test_the_lot_size_is_declared_when_the_key_is_resolved(monkeypatch):
    from oto_mcp import access

    m, _, _ = _mount(monkeypatch, byo=False)
    vus: list[dict] = []
    monkeypatch.setattr(access, "resolve_api_key",
                        lambda p, *a, **k: (vus.append(k), ("k", True))[1])
    _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(7)])
    assert vus == [{"units": 7}]


def test_the_debit_is_what_apollo_billed_not_what_was_submitted(monkeypatch):
    """10 soumises, 3 facturées : les 7 sans correspondance ne coûtent rien à Apollo."""
    m, _, usage = _mount(monkeypatch, byo=False, bulk_return={
        "matches": [], "credits_consumed": 3})
    _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(10)])
    assert usage == [("apollo", 3)]


def test_a_lot_apollo_billed_nothing_for_debits_nothing(monkeypatch):
    m, _, usage = _mount(monkeypatch, byo=False, bulk_return={
        "matches": [], "credits_consumed": 0})
    _tool(m, "apollo_bulk_match")(people=[{"id": "p1"}, {"id": "p2"}])
    assert usage == []


# --------------------------------------------------------------------------- #
# La ligne FACTURÉE porte les crédits qu'Apollo a facturés (oto#168)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("byo,kwargs", [(False, {}),
                                        (True, {"reveal_personal_emails": True})])
@pytest.mark.parametrize("factures", [3, 0])
def test_the_BILLED_quantity_is_what_apollo_billed(monkeypatch, byo, kwargs, factures):
    """10 soumises, `factures` crédits rendus par Apollo : la ligne facturée porte le
    chiffre de l'amont, 0 compris (un 0 tracé = rien de facturé), sur toute clé."""
    from oto_mcp import session_org
    traces = []
    monkeypatch.setattr(session_org, "note_call_trace", lambda **kw: traces.append(kw))
    m, _, _ = _mount(monkeypatch, byo=byo, bulk_return={
        "matches": [], "credits_consumed": factures})
    _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(10)], **kwargs)
    assert [t["quantity"] for t in traces if "quantity" in t] == [factures]


def test_a_silent_apollo_answer_counts_what_was_submitted_AND_SAYS_SO(monkeypatch, caplog):
    """Réponse sans `credits_consumed` : on compte les personnes soumises (le plus sûr
    pour le quota), mais le journal le dit — jamais un repli silencieux."""
    import logging
    from oto_mcp import session_org
    traces = []
    monkeypatch.setattr(session_org, "note_call_trace", lambda **kw: traces.append(kw))
    m, _, usage = _mount(monkeypatch, byo=False, bulk_return={"matches": []})
    with caplog.at_level(logging.WARNING, logger="oto_mcp.tools.apollo"):
        _tool(m, "apollo_bulk_match")(people=[{"id": f"p{i}"} for i in range(4)])
    assert usage == [("apollo", 4)]
    assert [t["quantity"] for t in traces if "quantity" in t] == [4]
    assert any("credits_consumed" in r.getMessage() for r in caplog.records)
