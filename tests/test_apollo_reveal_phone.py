"""`apollo_reveal_phone` — le seul geste d'Apollo qui ne rend pas son résultat.

Le client demandait `reveal_phone_number` et `webhook_url` : Apollo ne rend PAS
les mobiles dans la réponse, il les POSTe à une URL quelques minutes plus tard.
Ce que ces tests figent :

1. **Le reveal part sur la clé propre, SINON sur la clé plateforme** — depuis
   qu'Apollo DÉCLARE ce qu'il facture (`credits_consumed` de l'enveloppe livrée :
   8 pour une personne dont le numéro est trouvé, 0 sinon, plus 1 pour le match ;
   mesuré le 2026-11-22). Sur la clé commune, la commande vérifie et débite le pire
   cas (9 unités) du quota, est refusée au-delà du plafond quotidien de l'org, et seule
   l'org qui a commandé peut sonder Apollo pour ce `request_id`. La relève trace le
   chiffre déclaré (`quantity`) sous l'id de la commande et marque la commande relevée ;
   un coût non déclaré est un refus nommé, jamais un zéro.
2. **`request_id` est servi en CHAÎNE.** C'est un entier signé 64 bits (~7,2e17),
   au-delà de la précision d'un nombre JavaScript : rendu en nombre il revient
   faux d'une unité ou deux, sans que rien ne le dise, et un id faux ne sonde
   rien.
3. **L'URL de livraison est la NÔTRE** — générée par `apollo_receiver`, jamais
   fournie par l'appelant ; une `webhook_url` héritée est ignorée
   (`tests/test_apollo_receveur.py` éprouve le receveur lui-même).
4. **Pas de `next_step` qui promette un outil inutilisable** : si Apollo accepte
   sans rendre d'id, la réponse le DIT au lieu d'annoncer un sondage impossible.

Mock la CLASSE client (jamais `requests`) — cf. `tests/test_apollo_location_filters.py`.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock

import pytest


_WEBHOOK = "https://hooks.acme.test/apollo"
#: L'URL que `apollo_receiver.commander` rend dans ce montage — la seule qu'Apollo
#: doit jamais recevoir.
_RECEVEUR = "https://mcp.acme.test/api/receivers/apollo/phones/jeton"

# `None` est une VALEUR DE RETOUR à part entière ici (le client oto-core traduit
# le 404 d'Apollo en None) : le défaut du montage ne peut donc pas s'écrire
# `if x is None`, sinon le cas le plus intéressant devient intestable.
_DEFAUT = object()


def _mount(monkeypatch, *, byo: bool = True, is_platform=None, quota=None,
           match_return=_DEFAUT, poll_return=_DEFAUT, recu=None, connue=True,
           du_jour=0):
    """Monte apollo.py sur un FastMCP nu. `byo=False` simule l'absence de
    credential propre (donc le palier plateforme) en faisant lever
    `resolve_credential` comme le fait la vraie résolution.

    ⚠️ `is_platform` est DÉCOUPLÉ de `byo`, et sans ça un test ne prouve rien :
    avec le défaut (`is_platform = not byo`), un org qui a sa propre clé voit
    `resolve_api_key` rendre « pas plateforme », donc « aucun crédit plateforme
    débité » serait vrai même si l'outil ne basculait PAS sur la clé propre. Le
    cas qui mord est celui où les deux sont disponibles : c'est là que le choix
    de la clé se lit."""
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.match_person.return_value = (
        {"person": {"id": "p1"}, "request_id": 718432950164203900}
        if match_return is _DEFAUT else match_return)
    client.poll_webhook_result.return_value = (
        {"done": False, "retry_after_seconds": 10}
        if poll_return is _DEFAUT else poll_return)
    usage: list[str] = []

    client.resolutions = []

    def _resolve(provider, want="auto", *a, **k):
        client.resolutions.append((want, k))
        if byo:
            return MagicMock(key="k-byo", is_platform=False)
        if want == "byo":
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="No `apollo` credential configured for you"))
        # La cascade complète (`want="auto"`) descend jusqu'au palier plateforme.
        return MagicMock(key="k-plateforme", is_platform=True)

    monkeypatch.setattr(access, "resolve_credential", _resolve)
    plateforme = (not byo) if is_platform is None else is_platform
    monkeypatch.setattr(access, "resolve_api_key", lambda *a, **k: ("k", plateforme))
    from oto_mcp import session_org
    client.trace = {}
    monkeypatch.setattr(session_org, "note_call_trace",
                        lambda **v: client.trace.update(v))
    from _releve import capter_units
    capter_units(monkeypatch, "apollo", lambda p, n: usage.append(p))
    monkeypatch.setattr(access, "platform_quota_hint", lambda p: quota)
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)

    # Le receveur : sa base est éprouvée ailleurs (`test_apollo_receveur.py`) ; ici
    # on relève ce que l'outil lui DEMANDE. `recu` = ce qu'Apollo nous aurait livré
    # (None : rien d'arrivé, le sondage d'Apollo prend le relais).
    from oto_mcp import apollo_receiver
    client.receveur = []
    # Le plafond se compte DANS la commande (`commander(cle, plafond=…)`, sous verrou) :
    # le double le rejoue sur `du_jour`, les commandes déjà passées de l'org.
    client.plafonds = []

    def _commander(cle, plafond=None):
        client.plafonds.append(plafond)
        if plafond is not None and du_jour >= plafond:
            raise apollo_receiver.PlafondDuJour(du_jour, plafond)
        client.receveur.append(("commande",))
        return "jeton", _RECEVEUR
    monkeypatch.setattr(apollo_receiver, "commander", _commander)
    monkeypatch.setattr(apollo_receiver, "lier",
                        lambda j, rid: client.receveur.append(("lier", j, str(rid))))
    monkeypatch.setattr(apollo_receiver, "abandonner",
                        lambda j: client.receveur.append(("abandon", j)))
    monkeypatch.setattr(apollo_receiver, "resultat_recu", lambda rid, cle: recu)
    monkeypatch.setattr(apollo_receiver, "commande_connue", lambda rid, cle: connue)
    client.releves = []
    monkeypatch.setattr(apollo_receiver, "marquer_releve",
                        lambda rid, cle: client.releves.append(str(rid)))

    m = FastMCP("t")
    apollo_tool.register(m)
    return m, client, usage


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --------------------------------------------------------------------------- #
# 1. La clé : la sienne, sinon la clé plateforme — au juste compte
# --------------------------------------------------------------------------- #

def test_without_your_own_key_the_reveal_runs_on_the_platform_key(monkeypatch):
    m, client, usage = _mount(monkeypatch, byo=False)
    out = _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.match_person.called
    assert client.match_person.call_args.kwargs["reveal_phone_number"] is True
    assert out["request_id"] == "718432950164203900"
    (want, k), = client.resolutions
    assert want == "auto" and k.get("units") == 9, (
        "le quota commun est VÉRIFIÉ pour le pire cas d'un reveal, pas pour 1")
    assert usage == ["apollo"], "et DÉBITÉ une fois, sur la clé commune"


def test_a_platform_reveal_debits_the_worst_case_and_bills_the_match(monkeypatch):
    from _releve import capter_units  # noqa: F401 — le montage l'a posé
    m, client, _ = _mount(monkeypatch, byo=False)
    debits: list[int] = []
    from oto_mcp import session_org
    suivant = session_org.note_call_trace

    def _note(**v):
        if v.get("units"):
            debits.append(v["units"])
        suivant(**v)
    monkeypatch.setattr(session_org, "note_call_trace", _note)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert debits == [9], "pire cas : le match (1) + un numéro trouvé (8)"
    assert client.trace.get("quantity") == 1, "le match se facture à la commande"


def test_a_platform_reveal_that_matches_nobody_debits_the_match_only(monkeypatch):
    m, client, _ = _mount(monkeypatch, byo=False, match_return=None)
    debits: list[int] = []
    from oto_mcp import session_org
    suivant = session_org.note_call_trace

    def _note(**v):
        if v.get("units"):
            debits.append(v["units"])
        suivant(**v)
    monkeypatch.setattr(session_org, "note_call_trace", _note)
    out = _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert out["matched"] is False
    assert debits == [1]
    assert client.trace.get("quantity") == 0, "personne : rien à facturer"


def test_your_own_key_wins_and_debits_nothing_shared(monkeypatch):
    m, client, usage = _mount(monkeypatch, byo=True)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.match_person.called
    assert usage == [], "clé propre : rien à débiter du pot commun"


def test_on_the_platform_key_only_YOUR_org_orders_can_be_polled(monkeypatch):
    """Le sondage d'Apollo répond à qui tient la clé : sur la clé commune, un
    `request_id` deviné rendrait le lead d'une AUTRE org."""
    from oto_mcp.mcp_errors import McpError

    m, client, _ = _mount(monkeypatch, byo=False, connue=False)
    with pytest.raises(McpError) as e:
        _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert not client.poll_webhook_result.called
    assert "your organization" in (e.value.error.message or "")


def test_an_order_of_your_org_on_the_platform_key_is_polled(monkeypatch):
    m, client, _ = _mount(monkeypatch, byo=False, connue=True)
    out = _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert client.poll_webhook_result.called
    assert out["done"] is False
    (want, k), = client.resolutions
    assert k.get("check_usage") is False, (
        "une relève ne consomme rien : elle ne vérifie aucun quota (#943)")


@pytest.mark.parametrize("byo", [True, False])
def test_the_collected_reveal_traces_what_apollo_declares_under_its_id(monkeypatch,
                                                                         byo):
    """Le chiffre facturé est CELUI QU'APOLLO DÉCLARE dans l'enveloppe livrée, tracé
    sous l'id de la commande — chaque relève le retrace, le facturant le compte
    une fois par id."""
    corps = {"credits_consumed": 8, "webhook_result": {"people": [
        {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000",
                                        "type_cd": "mobile"}]}]}}
    m, client, _ = _mount(monkeypatch, byo=byo, recu=corps)
    _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert client.trace == {"apollo_reveal_request_id": "718432950164203900",
                            "quantity": 8}


def test_a_reveal_that_found_nothing_traces_zero(monkeypatch):
    corps = {"credits_consumed": 0, "webhook_result": {"people": [
        {"id": "p1", "status": "failure", "phone_numbers": []}]}}
    m, client, _ = _mount(monkeypatch, byo=False, recu=corps)
    _tool(m, "apollo_reveal_phone_result")(request_id="42")
    assert client.trace["quantity"] == 0


def test_on_the_platform_key_an_undeclared_cost_is_a_named_refusal(monkeypatch):
    """Un coût illisible ne devient jamais zéro : la relève est REFUSÉE, nommément, et
    la commande reste non relevée — le relevé serveur la facture au pire cas."""
    from oto_mcp.mcp_errors import McpError

    corps = {"webhook_result": {"people": [
        {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000"}]}]}}
    m, client, _ = _mount(monkeypatch, byo=False, recu=corps)
    with pytest.raises(McpError) as e:
        _tool(m, "apollo_reveal_phone_result")(request_id="42")
    assert e.value.error.data["code"] == "apollo_cost_undeclared"
    assert client.releves == [], "non relevée : le relevé serveur la facturera"
    assert client.trace == {"quantity": 0}


def test_on_your_own_key_an_undeclared_cost_is_served_unbilled(monkeypatch):
    corps = {"webhook_result": {"people": [
        {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000"}]}]}}
    m, client, _ = _mount(monkeypatch, byo=True, recu=corps)
    out = _tool(m, "apollo_reveal_phone_result")(request_id="42")
    assert out["done"] is True
    assert "quantity" not in client.trace
    assert client.trace["apollo_reveal_request_id"] == "42"


def test_a_platform_read_with_its_cost_marks_the_order_settled(monkeypatch):
    corps = {"credits_consumed": 8, "webhook_result": {"people": [
        {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000"}]}]}}
    m, client, _ = _mount(monkeypatch, byo=False, recu=corps)
    _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert client.releves == ["718432950164203900"]


def test_an_own_key_read_marks_nothing(monkeypatch):
    corps = {"credits_consumed": 8, "webhook_result": {"people": []}}
    m, client, _ = _mount(monkeypatch, byo=True, recu=corps)
    _tool(m, "apollo_reveal_phone_result")(request_id="9")
    assert client.releves == []


def test_the_org_daily_cap_refuses_before_anything_is_ordered(monkeypatch):
    from oto_mcp.mcp_errors import McpError

    monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", "5")
    m, client, usage = _mount(monkeypatch, byo=False, du_jour=5)
    with pytest.raises(McpError) as e:
        _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert e.value.error.data == {"code": "apollo_phone_daily_cap", "used": 5,
                                  "limit": 5, "retryable": False}
    assert not client.match_person.called and client.receveur == []
    assert usage == []


def test_under_the_cap_the_order_goes(monkeypatch):
    monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", "5")
    m, client, _ = _mount(monkeypatch, byo=False, du_jour=4)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.match_person.called


def test_your_own_key_has_no_org_cap(monkeypatch):
    monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", "0")
    m, client, _ = _mount(monkeypatch, byo=True, du_jour=999)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.match_person.called


def test_your_own_key_orders_without_a_cap(monkeypatch):
    m, client, _ = _mount(monkeypatch, byo=True, du_jour=0)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.plafonds == [None]


def test_the_platform_order_carries_the_configured_cap(monkeypatch):
    monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", "7")
    m, client, _ = _mount(monkeypatch, byo=False, du_jour=0)
    _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert client.plafonds == [7]


@pytest.mark.parametrize("lu", ["beaucoup", "-1", "1.5"])
def test_an_unreadable_cap_refuses_to_start(monkeypatch, lu):
    """Comme `access/platform_rate.verifier_reglages` : un réglage illisible empêche
    le démarrage, jamais un défaut silencieux."""
    from oto_mcp.tools import apollo as apollo_tool

    monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", lu)
    with pytest.raises(ValueError, match="OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY"):
        apollo_tool.verifier_reglages()


@pytest.mark.parametrize("lu,attendu", [(None, 100), ("", 100), (" 0 ", 0), ("25", 25)])
def test_a_readable_cap_is_read(monkeypatch, lu, attendu):
    from oto_mcp.tools import apollo as apollo_tool

    if lu is None:
        monkeypatch.delenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", raising=False)
    else:
        monkeypatch.setenv("OTO_MCP_APOLLO_PHONE_REVEALS_PER_ORG_DAILY", lu)
    apollo_tool.verifier_reglages()
    assert apollo_tool.plafond_reveals_org() == attendu


def test_the_startup_runs_the_cap_guard():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "oto_mcp" / "server.py").read_text()
    assert "apollo_outils.verifier_reglages()" in src

def test_a_pending_check_traces_a_zero_not_an_absence(monkeypatch):
    m, client, _ = _mount(monkeypatch, byo=False)
    _tool(m, "apollo_reveal_phone_result")(request_id="42")
    assert client.trace == {"quantity": 0}


# --------------------------------------------------------------------------- #
# 2. Le geste nominal
# --------------------------------------------------------------------------- #

def test_reveal_forwards_the_flag_and_OUR_receiver(monkeypatch):
    """Apollo reçoit l'URL générée par oto — même quand un appelant d'avant ce lot
    passe encore la sienne."""
    m, client, usage = _mount(monkeypatch)
    _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    kw = client.match_person.call_args.kwargs
    assert kw["reveal_phone_number"] is True
    assert kw["webhook_url"] == _RECEVEUR
    assert kw["person_id"] == "p1"
    assert usage == [], "clé BYO : rien à débiter du pot plateforme"


def test_request_id_is_served_as_a_string(monkeypatch):
    """718432950164203900 > 2^53 : rendu en nombre, il revient faux."""
    m, _, _ = _mount(monkeypatch)
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert out["request_id"] == "718432950164203900"
    assert isinstance(out["request_id"], str)
    assert "apollo_reveal_phone_result" in out["next_step"]


def test_no_request_id_says_so_instead_of_promising_a_poll(monkeypatch):
    """Personne TROUVÉE, mais pas d'id : le reveal est parti, il n'est
    simplement pas sondable."""
    m, _, _ = _mount(monkeypatch, match_return={"person": {"id": "p1"}})
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert "request_id" not in out
    assert "Nothing to poll" in out["next_step"]
    assert out.get("matched") is not False, "une personne EST revenue"


@pytest.mark.parametrize("reponse, cas", [
    (None, "404 Apollo → le client rend None"),
    ({}, "corps vide"),
    ({"person": None}, "200 sans personne"),
])
def test_apollo_finding_nobody_is_not_an_accepted_reveal(monkeypatch, reponse, cas):
    """Le mensonge le plus cher est celui qui RASSURE : annoncer un reveal en vol
    quand Apollo n'a rien trouvé fait attendre à l'agent un POST qui ne partira
    jamais, et lui fait rendre le lead pour traité sans réessayer."""
    m, _, _ = _mount(monkeypatch, match_return=reponse)
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert out["matched"] is False, cas
    assert "request_id" not in out
    assert "No Apollo match" in out["next_step"]
    assert "accepted" not in out["next_step"], (
        "rien n'a été accepté : ni commande, ni crédit, ni webhook")


# --------------------------------------------------------------------------- #
# 4. Le sondage
# --------------------------------------------------------------------------- #

def test_pending_poll_is_not_a_failure(monkeypatch):
    m, _, _ = _mount(monkeypatch, poll_return={"done": False,
                                               "retry_after_seconds": 10})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert out["done"] is False
    assert out["retry_after_seconds"] == 10
    assert "10s" in out["next_step"]


def test_a_finished_poll_hands_back_the_numbers(monkeypatch):
    """⚠️ L'enveloppe est celle qu'Apollo PUBLIE, pas une forme commode : les
    numéros vivent sous `webhook_result`, un cran plus bas que le raccourci
    qu'on aurait écrit spontanément. Un fixture inventé ici rendrait ce test
    vert sur une description fausse — c'est exactement ce qui était arrivé."""
    payload = {
        "request_id": 718432950164203900,
        "webhook_status": "delivered",
        "failure_reason": None,
        "webhook_result": {"people": [
            {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000",
                                            "type_cd": "mobile"}]}]},
    }
    m, _, usage = _mount(monkeypatch, poll_return={"done": True, "result": payload})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    # ⚠️ L'enveloppe ressort TELLE QUELLE à une exception près, et c'en est une
    # volontaire : l'identifiant ré-échoté par Apollo sort en CHAÎNE (cf.
    # `test_the_poll_does_not_echo_a_damaged_id`). Ce test figeait l'égalité
    # stricte avec le payload d'Apollo et a donc rougi quand la sérialisation est
    # descendue d'un niveau — c'est ce qu'on lui demande : dire qu'un contrat
    # servi a changé, plutôt que de suivre en silence.
    assert out == {"done": True,
                   "result": {**payload, "request_id": "718432950164203900"}}
    assert usage == [], "un sondage coûte 0 crédit — rien à débiter"


def test_the_poll_description_names_the_path_the_numbers_ACTUALLY_take(monkeypatch):
    """Le chemin annoncé se lit sur le corps qu'Apollo publie, pas sur celui
    qu'on aurait aimé. Servir `result.people[]` là où il y a
    `result.webhook_result.people[]` fait lire « aucun numéro » à chaque sondage
    réussi — et une description est relue comme une instruction."""
    corps = {"webhook_result": {"people": [
        {"phone_numbers": [{"sanitized_number": "+33600000000"}]}]}}
    m, _, _ = _mount(monkeypatch, poll_return={"done": True, "result": corps})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="1")
    doc = asyncio.run(m.get_tool("apollo_reveal_phone_result")).description or ""

    # On PARCOURT le chemin que la description dicte, sur la réponse servie :
    # relire les deux et les trouver d'accord ne prouve rien, les suivre si.
    chemin = doc.split("the numbers are at ")[1].split("(")[0].strip().strip("`")
    noeud = out
    for cle in chemin.split("."):
        noeud = noeud[cle.removesuffix("[]")]
        if cle.endswith("[]"):
            noeud = noeud[0]
    assert noeud, f"le chemin servi (`{chemin}`) ne mène nulle part"


# --------------------------------------------------------------------------- #
# 5. Ce que les descriptions promettent existe vraiment
# --------------------------------------------------------------------------- #

def test_the_reveal_only_names_tools_that_exist(monkeypatch):
    """Une description d'outil est relue comme une instruction : un agent qui a
    une intention et pas de destination s'en fabrique une (#613/#632)."""
    m, _, _ = _mount(monkeypatch)
    doc = asyncio.run(m.get_tool("apollo_reveal_phone")).description or ""
    for cite in ("apollo_reveal_phone_result", "apollo_search_people",
                 "apollo_match_person"):
        assert cite in doc
        assert asyncio.run(m.get_tool(cite)) is not None


def test_match_person_says_the_phone_lives_elsewhere(monkeypatch):
    """Sans ça, un agent qui cherche un mobile le demande ici, ne le trouve pas,
    et conclut que la plateforme ne sait pas faire — le signal du client."""
    m, _, _ = _mount(monkeypatch)
    doc = asyncio.run(m.get_tool("apollo_match_person")).description or ""
    assert "apollo_reveal_phone" in doc


def test_match_person_forwards_reveal_personal_emails(monkeypatch):
    m, client, _ = _mount(monkeypatch)
    _tool(m, "apollo_match_person")(person_id="p1", reveal_personal_emails=True)
    assert client.match_person.call_args.kwargs["reveal_personal_emails"] is True


# --------------------------------------------------------------------------- #
# 6. L'autre reveal : les emails personnels, MÊME règle
# --------------------------------------------------------------------------- #

def test_personal_emails_reveal_is_refused_without_your_own_key(monkeypatch):
    """Le reveal d'emails personnels est un reveal : Apollo le facture EN PLUS du
    match pendant que le quota (`units`) débite une unité — le prix d'un
    match nu. Le laisser partir sur la clé commune fait mentir `platform_quota`,
    exactement comme le téléphone ; que l'écart soit mesuré (~9×) ou seulement
    inconnu ne change pas le sens du défaut."""
    from oto_mcp.mcp_errors import McpError

    m, client, usage = _mount(monkeypatch, byo=False, is_platform=True)
    with pytest.raises(McpError):
        _tool(m, "apollo_match_person")(person_id="p1", reveal_personal_emails=True)
    assert not client.match_person.called, "aucun appel ne doit partir"
    assert usage == [], "aucun crédit plateforme ne doit être débité"


def test_the_personal_emails_refusal_names_the_cost_not_the_data_boundary(monkeypatch):
    """Même exigence que pour le téléphone : servir « tes propres données » à qui
    bute sur un mur de COÛT l'envoie chercher au mauvais endroit."""
    from oto_mcp.mcp_errors import McpError

    m, _, _ = _mount(monkeypatch, byo=False, is_platform=True)
    with pytest.raises(McpError) as e:
        _tool(m, "apollo_match_person")(person_id="p1", reveal_personal_emails=True)
    msg = e.value.error.message or ""
    assert "reveal_personal_emails" in msg
    assert re.search(r"bills|credit", msg), "le motif est le coût, il doit se dire"
    assert "sequences" not in msg, "le motif « tes propres données » ne vaut pas ici"


def test_a_personal_emails_reveal_never_rides_the_shared_meter(monkeypatch):
    """Le geste, pas l'outil. Sur un org qui a les DEUX (sa clé ET le palier
    plateforme ouvert), demander les emails personnels bascule sur la clé propre :
    rien n'est débité du pot commun, et la réponse ne porte pas un `platform_quota`
    qui décrirait une clé qu'on n'a pas utilisée."""
    m, client, usage = _mount(
        monkeypatch, byo=True, is_platform=True, quota={"remaining": 42},
        match_return={"person": {"id": "p1"}})
    out = _tool(m, "apollo_match_person")(person_id="p1",
                                          reveal_personal_emails=True)
    assert client.match_person.call_args.kwargs["reveal_personal_emails"] is True
    assert usage == [], "clé propre : rien à débiter du pot plateforme"
    assert "platform_quota" not in out


def test_a_plain_match_still_rides_the_shared_meter(monkeypatch):
    """Contrôle positif du test ci-dessus — sans lui, « rien n'a été débité » ne
    distingue pas « la bascule a eu lieu » de « le montage ne débite jamais »."""
    m, _, usage = _mount(
        monkeypatch, byo=True, is_platform=True, quota={"remaining": 42},
        match_return={"person": {"id": "p1"}})
    out = _tool(m, "apollo_match_person")(person_id="p1")
    assert usage == ["apollo"], "un match nu reste sur le palier plateforme"
    assert out["platform_quota"] == {"remaining": 42}


# --------------------------------------------------------------------------- #
# 7. Le refus « pas de match » n'affirme rien sur le coût
# --------------------------------------------------------------------------- #

def _sans_match(monkeypatch) -> str:
    m, _, _ = _mount(monkeypatch, match_return=None)
    return _tool(m, "apollo_reveal_phone")(
        webhook_url=_WEBHOOK, person_id="p1")["next_step"]


@pytest.mark.parametrize("formule", [
    "no credit spent", "nothing was ordered", "no credit", "no charge",
    "free of charge", "at no cost", "nothing was spent", "nothing was billed",
])
def test_the_no_match_message_never_claims_the_call_was_free(monkeypatch, formule):
    """⚠️ Le code ne sait PAS ce que l'amont a facturé, et Apollo facture l'appel,
    pas le résultat : il facture même la coquille vide qu'il fabrique (~12 crédits
    pour zéro donnée, oto-core `44acc08`). Une branche d'échec qui affirme la
    gratuité est une MESURE qu'on n'a pas prise — et elle est servie à un agent
    qui la relit comme une instruction.

    La classe refusée est l'absolu non mesurable sur la dépense, pas un mot : les
    formules ci-dessous en sont les formes rencontrées."""
    assert formule not in _sans_match(monkeypatch).lower()


def test_the_no_match_message_prices_the_second_attempt(monkeypatch):
    """Le symétrique, et il compte autant : bannir « gratuit » sans dire que ce
    n'est pas gratuit laisserait un message muet sur le coût, qui enchaîne quand
    même sur « réessaie ». Un second appel présenté comme offert est le défaut
    qu'on corrige ici — il doit donc être CHIFFRÉ."""
    msg = _sans_match(monkeypatch)
    assert re.search(r"\bbill|\bcharge|\bpaid\b|\bcost", msg, re.I), (
        "le refus doit dire que l'appel n'était pas gratuit")
    assert re.search(r"free", msg, re.I), (
        "et le dire en visant la lecture fausse (« do NOT read that as free »)")


# --------------------------------------------------------------------------- #
# 8. Toute capacité de reveal ANNONCE sa réserve de clé, dans le texte servi
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("outil, marqueur", [
    ("apollo_reveal_phone", "own Apollo key"),
    ("apollo_match_person", "reveal_personal_emails"),
])
def test_a_reveal_says_in_its_served_text_that_it_needs_your_own_key(
        monkeypatch, outil, marqueur):
    """Un outil offert sera appelé : la réserve de clé doit se lire AVANT l'appel,
    dans ce que le serveur sert — pas seulement dans le refus qui arrive après.
    Et elle doit porter sa RAISON : « ta propre clé » sans le coût se lit comme une
    tracasserie d'installation, et un agent qui n'en comprend pas le motif cherche
    à la contourner (#613/#632).

    On ne fige aucune phrase : on exige la classe (réserve de clé + motif de coût)
    dans le voisinage de la capacité nommée."""
    m, _, _ = _mount(monkeypatch)
    doc = asyncio.run(m.get_tool(outil)).description or ""
    assert marqueur in doc, f"{outil} ne nomme même pas la capacité"
    i = doc.index(marqueur)
    voisinage = doc[max(0, i - 250):i + 400]
    assert re.search(r"your own Apollo key", voisinage, re.I), (
        f"{outil} : la réserve de clé ne se lit pas près de « {marqueur} »")
    assert re.search(r"credit|bill|charge|cost", voisinage, re.I), (
        f"{outil} : la réserve est annoncée sans son motif (le coût)")



# --------------------------------------------------------------------------- #
# 6. Compte à sec (signaux #1026, #1114, #1283, #1348)
# --------------------------------------------------------------------------- #

def test_an_order_closed_for_lack_of_credits_says_so_at_the_top(monkeypatch):
    """« done: true » sans numéro, la raison enfouie sous `result` : l'agent lisait
    « fini » et recommandait. La réponse dit qu'il n'y aura rien, et pourquoi."""
    payload = {"request_id": 1, "webhook_status": "failed",
               "failure_reason": "You ran out of mobile number credits",
               "webhook_result": {"people": []}}
    m, _, _ = _mount(monkeypatch, poll_return={"done": True, "result": payload})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="1")
    assert out["done"] is True and out["delivered"] is False
    assert out["reason"] == "out_of_credits"
    assert "out of credits" in out["next_step"] and "do not order again" in out["next_step"]


def test_another_failure_is_named_without_blaming_credits(monkeypatch):
    payload = {"failure_reason": "webhook expired", "webhook_result": {}}
    m, _, _ = _mount(monkeypatch, poll_return={"done": True, "result": payload})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="1")
    assert out["delivered"] is False and out["reason"] == "not_delivered"
    assert out["failure_reason"] == "webhook expired"


def test_an_empty_account_at_order_time_is_quota_exhausted(monkeypatch):
    """Le client (oto-core ≥ ApolloCreditsExhausted) lève une erreur qui porte
    `status_code = 402` pour le 422 « insufficient credits » : la taxonomie la lit
    `quota_exhausted`, et la commande née avant l'appel est abandonnée."""
    from oto_mcp import error_taxonomy

    class _ASec(RuntimeError):
        status_code = 402

    m, client, _ = _mount(monkeypatch)
    client.match_person.side_effect = _ASec(
        "Apollo 422 on people/match: You have insufficient credits")
    with pytest.raises(_ASec) as e:
        _tool(m, "apollo_reveal_phone")(person_id="p1")
    assert ("abandon", "jeton") in client.receveur
    assert error_taxonomy.classify(e.value).code == "quota_exhausted"
