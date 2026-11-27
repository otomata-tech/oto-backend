"""Tools `twilio` / `twilio_act` et sonde du connecteur Twilio (clé propre de l'org).

Ce que ce fichier verrouille :
- la paire `<ns>` / `<ns>_act` et le routage vers la bonne méthode du client, avec le
  credential à deux champs passé tel quel ;
- l'appel MIS EN RELATION : le commercial sonne d'abord, le TwiML compose le contact
  avec le numéro de l'org en présentation, enregistrement des deux côtés par défaut ;
- l'achat et la libération réservés à un admin de l'org ;
- `dry_run` : aucune méthode qui agit n'est appelée ;
- l'envoi de SMS en lot : un refus par destinataire ne coupe pas le lot, un 401 si ;
- l'audio d'un enregistrement déposé en transit ;
- les refus d'intention et la sonde.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError

_CREDS = {"account_sid": "AC" + "0" * 32, "auth_token": "jeton-de-test"}


@pytest.fixture
def client(monkeypatch):
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.twilio.TwilioClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(_CREDS))
    monkeypatch.setattr("oto_mcp.access.resolve.resolve_credential",
                        lambda provider, **kw: MagicMock(fields=dict(_CREDS)))
    inst.built = built
    return inst


@pytest.fixture
def admin(monkeypatch):
    etat = {"admin": True}
    monkeypatch.setattr("oto_mcp.tools.twilio._caller_is_org_admin", lambda: etat["admin"])
    return etat


def _fn(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import twilio as T

    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool(name)).fn


def test_lecture_des_numeros_resserree(client):
    client.list_numbers.return_value = {"incoming_phone_numbers": [
        {"sid": "PN1", "phone_number": "+33939030001", "uri": "/x", "subresource_uris": {}}],
        "next_page_uri": None}
    out = _fn("twilio")(op="numbers")
    assert client.built == [_CREDS]
    assert out["incoming_phone_numbers"] == [{"sid": "PN1", "phone_number": "+33939030001"}]
    assert "next_page_uri" in out


def test_un_appel_par_sid(client):
    client.get_call.return_value = {"sid": "CA1", "status": "completed"}
    assert _fn("twilio")(op="calls", sid="CA1")["status"] == "completed"
    client.list_calls.assert_not_called()


def test_audio_en_transit(client, monkeypatch):
    client.recording_media.return_value = b"ID3"
    vu = {}
    monkeypatch.setattr("oto_mcp.transit.deposer",
                        lambda prefix, data, mime, name: vu.update(p=prefix, d=data) or {"source": 1})
    assert _fn("twilio")(op="recording_audio", sid="RE1") == {"source": 1}
    assert vu == {"p": "twilio-recordings", "d": b"ID3"}


def test_refus_d_intention(client):
    with pytest.raises(McpError):
        _fn("twilio")(op="numbers", country="FR")
    with pytest.raises(McpError):
        _fn("twilio")(op="available_numbers")


def test_appel_mis_en_relation(client, admin):
    client.create_call.return_value = {"sid": "CA9", "status": "queued", "uri": "/x"}
    out = _fn("twilio_act")(op="call", rep="+33600000001", to=["+33600000002"],
                            from_="+33939030001", announce="Appel vers le prospect")
    args, kw = client.create_call.call_args
    assert args == ("+33600000001", "+33939030001")
    assert "<Number>+33600000002</Number>" in kw["twiml"]
    assert 'callerId="+33939030001"' in kw["twiml"]
    assert "record-from-answer-dual" in kw["twiml"]
    assert out == {"sid": "CA9", "status": "queued"}


def test_appel_un_seul_contact_et_sans_enregistrement(client, admin):
    with pytest.raises(McpError):
        _fn("twilio_act")(op="call", rep="+33600000001", to=["+33600000002", "+33600000003"],
                          from_="+33939030001")
    _fn("twilio_act")(op="call", rep="+33600000001", to=["+33600000002"],
                      from_="+33939030001", record=False)
    assert "record=" not in client.create_call.call_args.kwargs["twiml"]


def test_achat_et_liberation_reserves_aux_admins(client, admin):
    admin["admin"] = False
    for op, kw in (("add_number", {"phone_number": "+33939030001"}),
                   ("remove_number", {"sid": "PN1"})):
        with pytest.raises(McpError) as e:
            _fn("twilio_act")(op=op, **kw)
        assert "admin" in str(e.value)
    client.buy_number.assert_not_called()
    client.release_number.assert_not_called()
    admin["admin"] = True
    _fn("twilio_act")(op="add_number", phone_number="+33939030001", bundle_sid="BU1")
    client.buy_number.assert_called_once()


def test_dry_run_n_agit_jamais(client, admin):
    client.get_number.return_value = {"phone_number": "+33939030001", "sms_url": "https://a"}
    act = _fn("twilio_act")
    act(op="add_number", phone_number="+33939030001", dry_run=True)
    out = act(op="update_number", sid="PN1", sms_url="https://b", dry_run=True)
    assert out["changes"] == {"sms_url": {"from": "https://a", "to": "https://b"}}
    act(op="remove_number", sid="PN1", dry_run=True)
    act(op="call", rep="+33600000001", to=["+33600000002"], from_="+33939030001", dry_run=True)
    out = act(op="send_sms", from_="MYSHOP", to=["+33600000002"], body="Salut", dry_run=True)
    assert out["to"] == ["+33600000002"]
    for m in ("buy_number", "update_number", "release_number", "create_call", "send_message"):
        getattr(client, m).assert_not_called()


def test_sms_en_lot_continue_apres_un_refus(client, admin):
    from oto.tools.common.errors import UpstreamHTTPError

    client.send_message.side_effect = [
        {"sid": "SM1", "status": "queued", "num_segments": "1"},
        UpstreamHTTPError(400, {"code": 21211, "message": "Invalid 'To'"}),
    ]
    out = _fn("twilio_act")(op="send_sms", from_="+33939030001",
                            to=["+33600000001", "+33600000002"], body="Bonjour")
    assert out["total"] == 2 and out["sent"][0]["sid"] == "SM1"
    assert out["failed"][0]["index"] == 1 and "21211" in out["failed"][0]["error"]


def test_sms_un_401_coupe_le_lot(client, admin):
    from oto.tools.common.errors import UpstreamHTTPError

    client.send_message.side_effect = UpstreamHTTPError(401, {"code": 20003})
    with pytest.raises(McpError):
        _fn("twilio_act")(op="send_sms", from_="+33939030001",
                          to=["+33600000001", "+33600000002"], body="x")
    assert client.send_message.call_count == 1


def test_sms_borne_du_lot(client, admin):
    with pytest.raises(McpError):
        _fn("twilio_act")(op="send_sms", from_="+33939030001",
                          to=["+336000000%02d" % i for i in range(51)], body="x")


def test_sonde(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.tools import twilio as T

    inst = MagicMock()
    monkeypatch.setattr("oto.tools.twilio.TwilioClient", lambda **kw: inst)
    inst.get_account.side_effect = UpstreamHTTPError(401, {"code": 20003})
    with pytest.raises(cv.NonAutorise):
        T._verify(dict(_CREDS))
    inst.get_account.side_effect = None
    inst.get_account.return_value = {"status": "active"}
    T._verify(dict(_CREDS))


def test_sms_le_lot_s_arrete_avant_le_delai(client, admin, monkeypatch):
    from oto_mcp.tools import twilio as T

    horloge = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(T, "time", MagicMock(monotonic=lambda: next(horloge, 99.0)))
    client.send_message.return_value = {"sid": "SM1", "status": "queued"}
    out = _fn("twilio_act")(op="send_sms", from_="+33939030001",
                            to=["+33600000001", "+33600000002", "+33600000003"], body="x")
    assert len(out["sent"]) == 1
    assert [r["index"] for r in out["not_attempted"]] == [1, 2]
    assert T.SMS_TIME_BUDGET_S == 30.0


# ── sans clé propre : les numéros de la plateforme, par le relais du commerce ──────────

@pytest.fixture
def courtier(monkeypatch):
    """Aucune clé Twilio pour l'org : le courtier répond à la place de Twilio."""
    from oto_mcp.access.resolve import CredentialUnavailable
    from mcp.types import ErrorData
    from oto_mcp.tools import twilio_courtier as C

    def _absent(provider, account=None):
        raise CredentialUnavailable(ErrorData(code=-32602, message="no key"))

    class Plateforme:
        is_platform, fields = True, {"account_sid": "AC-plateforme", "auth_token": "relay-t"}

    def _resolve(provider, want="auto", **kw):
        if want == "byo":
            _absent(provider)
        return Plateforme()

    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields", _absent)
    monkeypatch.setattr("oto_mcp.access.resolve.resolve_credential", _resolve)
    vu = {"get": [], "post": [], "token": []}

    class Faux:
        def get(self, tail="", params=None):
            vu["get"].append((tail, params))
            if tail == "":
                return {"numbers": [{"id": 7, "e164": "+33939030001"}]}
            return {"threads": []}

        def post(self, tail, body):
            vu["post"].append((tail, body))
            if body.get("to") == "+33600000009":
                raise C.CourtierRefus(400, "destination_not_allowed")
            return {"id": 1, "status": "queued", "segments": 1}

    monkeypatch.setattr(C, "pour_l_appelant", lambda token: vu["token"].append(token) or Faux())
    return vu


def test_sans_cle_les_lectures_passent_par_le_courtier(client, courtier):
    out = _fn("twilio")(op="numbers")
    assert out["numbers"][0]["e164"] == "+33939030001"
    _fn("twilio")(op="messages", from_="+33600000001")
    assert courtier["get"][-1] == ("/messages", {"peer": "+33600000001"})
    with pytest.raises(McpError) as e:
        _fn("twilio")(op="available_numbers", country="FR")
    assert "Phone page" in str(e.value)
    client.list_numbers.assert_not_called()


def test_sans_cle_sms_et_appel_par_le_courtier(client, courtier, admin):
    out = _fn("twilio_act")(op="send_sms", from_="+33939030001",
                            to=["+33600000001", "+33600000009"], body="Bonjour")
    assert out["via"] == "platform" and [r["index"] for r in out["sent"]] == [0]
    assert "not open" in out["failed"][0]["error"]
    assert courtier["post"][0] == ("/messages", {"number_id": 7, "to": "+33600000001",
                                                 "body": "Bonjour"})
    _fn("twilio_act")(op="call", from_="+33939030001", rep="+33600000002", to=["+33600000003"])
    assert courtier["post"][-1] == ("/calls", {"number_id": 7, "rep": "+33600000002",
                                               "to": "+33600000003"})
    with pytest.raises(McpError):
        _fn("twilio_act")(op="send_sms", from_="+33100000000", to=["+33600000001"], body="x")
    with pytest.raises(McpError):
        _fn("twilio_act")(op="add_number", phone_number="+33939030002")
    client.send_message.assert_not_called()
    client.create_call.assert_not_called()


def test_sans_cle_ni_courtier_le_refus_dit_quoi_faire(monkeypatch):
    """No own key and the platform key does not resolve for the org (no key, or the org
    cut from it, out of credits): refused, and the relay is never reached."""
    from oto_mcp.access.resolve import CredentialUnavailable
    from mcp.types import ErrorData
    from oto_mcp.tools import twilio_courtier as C

    def _absent(provider, account=None, **kw):
        raise CredentialUnavailable(ErrorData(code=-32602, message="no key"))

    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields", _absent)
    monkeypatch.setattr("oto_mcp.access.resolve.resolve_credential", _absent)
    reached = []
    monkeypatch.setattr(C, "pour_l_appelant", lambda token: reached.append(token))
    with pytest.raises(McpError) as e:
        _fn("twilio")(op="numbers")
    assert "out of credits" in str(e.value) and reached == []


def test_le_relais_est_appele_avec_la_cle_plateforme(courtier):
    _fn("twilio")(op="numbers")
    assert courtier["token"] == ["relay-t"]


def test_twilio_est_un_connecteur_plateforme_ouvert():
    """Open to every org like Unipile, cut per org by the commerce (share_denied)."""
    from oto_mcp import providers
    c = providers.connector_for_provider("twilio")
    assert "platform" in c.auth_modes and "byo_org" in c.auth_modes and c.platform_key_open


def test_les_outils_twilio_ne_debitent_rien_cote_oto():
    from oto_mcp.access.reglement import SANS_DEBIT
    assert {"twilio", "twilio_act"} <= SANS_DEBIT



def test_le_courtier_reste_au_tenant_primaire(monkeypatch):
    from oto_mcp.tools import twilio_courtier as C
    monkeypatch.setenv("OTO_COMMERCE_RELAY_URL", "https://commerce/service/oto")
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "sub-a")
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: 42)
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(C, "_du_tenant_primaire", lambda org: False)
    assert C.pour_l_appelant("t") is None
    monkeypatch.setattr(C, "_du_tenant_primaire", lambda org: True)
    c = C.pour_l_appelant("t")
    assert (c.org_id, c.sub, c.role, c.token) == (42, "sub-a", "org_member", "t")
    assert C.pour_l_appelant("") is None



def test_un_type_non_vendu_dit_les_types_vendus(client):
    from oto.tools.common.errors import UpstreamHTTPError
    client.search_available_numbers.side_effect = UpstreamHTTPError(404, {"code": 20404})
    client.available_number_types.return_value = ["local"]
    with pytest.raises(McpError) as e:
        _fn("twilio")(op="available_numbers", country="FR", number_type="mobile")
    assert "sells no mobile numbers in FR" in str(e.value) and "local" in str(e.value)



def test_sans_cle_un_nom_d_expediteur(client, courtier, admin):
    out = _fn("twilio_act")(op="send_sms", from_="EXEMPLE", to=["+33600000001"], body="RDV demain")
    assert out["sent"] and courtier["post"][-1] == (
        "/messages", {"sender_id": "EXEMPLE", "to": "+33600000001", "body": "RDV demain"})
