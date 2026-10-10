"""Tools `luma_*` et sonde du connecteur Luma (lu.ma).

Ce que ce fichier verrouille — les tripwires génériques couvrent déjà registre,
éditeur, logo, dérivation des modules, prose servie et jointure au client :
- la SURFACE (10 tools sur deux modules) et ce qui est déclaré LECTURE : seuls
  `luma_events` et `luma_guests`, dont TOUTES les ops lisent ;
- la clé passée telle quelle, et `calendar_id` (clé d'ORGANISATION) transmis
  au client ;
- les trois gestes qui sortent de l'organisation sont en dry-run par défaut,
  et le dry-run ne touche à rien d'autre que la demande de jeton d'annulation ;
- l'annulation d'un événement payant exige `should_refund` explicite ;
- la vue resserrée des listes : clés nommées retirées à toute profondeur,
  enveloppe intacte, bloc `omitted` qui NOMME ce qui manque, `full=True` brut —
  et le secret de signature d'un webhook absent d'une liste ;
- la traduction des refus amont et la sonde.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    """Le faux client, et les kwargs de chaque construction."""
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.luma.client.LumaClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, *a, **k: ("cle-de-test", False))
    inst.built = built
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import luma as L, luma_calendar as LC

    m = FastMCP("t")
    L.register(m)
    LC.register(m)
    return m


def _tools():
    return {t.name: t for t in asyncio.run(_mcp().list_tools())}


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"message": "nope"}, service="luma")


# --- surface ------------------------------------------------------------------

def test_surface():
    assert set(_tools()) == {
        "luma_events", "luma_event_admin", "luma_guests", "luma_guest_admin",
        "luma_blasts", "luma_tickets", "luma_contacts", "luma_calendar",
        "luma_memberships", "luma_webhooks"}


def test_seules_les_lectures_pures_sont_declarees_lecture():
    from oto_mcp.tools.lecture import en_lecture
    lecture = {n for n, t in _tools().items() if en_lecture(t)}
    assert lecture == {"luma_events", "luma_guests"}


def test_le_registre_declare_une_cle_byo_sur_deux_modules():
    from oto_mcp import providers
    c = providers.REGISTRY["luma"]
    assert c.kind == "tools" and c.secret_kind == "api_key" and c.keyed
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert c.modules == ("luma", "luma_calendar")


def test_la_cle_et_le_calendrier_passent_au_client(client):
    client.get_calendar.return_value = {"id": "cal-1"}
    assert _tool("luma_calendar")(op="get", calendar_id="cal-9") == {"id": "cal-1"}
    assert client.built == [{"api_key": "cle-de-test", "calendar_id": "cal-9"}]


# --- lectures ----------------------------------------------------------------

def test_liste_des_evenements_traduit_les_drapeaux(client):
    _tool("luma_events")(include_viewed=True, include_external=True,
                         sort_direction="asc", after="2026-10-01T00:00:00Z")
    kw = client.list_events.call_args.kwargs
    assert kw["access"] == ["manage", "view"]
    assert kw["platforms"] == ["luma", "external"]
    assert kw["sort_column"] == "start_at" and kw["after"] == "2026-10-01T00:00:00Z"


def test_resolve_d_un_lien(client):
    _tool("luma_events")(op="resolve", url="https://lu.ma/abc")
    client.lookup_entity.assert_called_once_with("https://lu.ma/abc")


def test_invites_par_email(client):
    _tool("luma_guests")(event_id="evt-1", op="get", guest="a@example.test")
    client.get_guest.assert_called_once_with("evt-1", "a@example.test")


def test_coupons_evenement_ou_calendrier(client):
    _tool("luma_tickets")(op="coupons", event_id="evt-1")
    _tool("luma_tickets")(op="coupons")
    client.list_event_coupons.assert_called_once()
    client.list_calendar_coupons.assert_called_once()


# --- vue resserrée -----------------------------------------------------------

_GUESTS = {"entries": [{"id": "gst-1", "user_email": "a@example.test",
                        "approval_status": "approved", "phone_number": "+33…",
                        "registration_answers": [{"q": "a"}],
                        "event_tickets": [{"id": "t", "check_in_qr_code": "x"}]}],
           "has_more": True, "next_cursor": "c2"}


def test_liste_d_invites_resserree_et_nommee(client):
    client.list_guests.return_value = _GUESTS
    out = _tool("luma_guests")(event_id="evt-1")
    row = out["entries"][0]
    assert row["user_email"] == "a@example.test" and "phone_number" not in row
    assert "check_in_qr_code" not in row["event_tickets"][0]      # à toute profondeur
    assert out["has_more"] is True and out["next_cursor"] == "c2"
    assert out["omitted"]["keys"] == ["check_in_qr_code", "phone_number",
                                      "registration_answers"]
    assert _GUESTS["entries"][0]["phone_number"] == "+33…"         # non destructif
    assert _tool("luma_guests")(event_id="evt-1", full=True) is _GUESTS


def test_rien_retire_pas_de_bloc_omitted(client):
    client.list_events.return_value = {"entries": [{"event": {"name": "N"}}],
                                       "has_more": False}
    assert "omitted" not in _tool("luma_events")()


def test_le_secret_d_un_webhook_ne_sort_pas_d_une_liste(client):
    client.list_webhooks.return_value = {"entries": [
        {"id": "wh-1", "url": "https://x.test", "secret": "whsec"}],
        "has_more": False}
    out = _tool("luma_webhooks")()
    assert "whsec" not in str(out) and out["omitted"]["keys"] == ["secret"]


# --- ce qui sort de l'organisation : dry-run ---------------------------------

def test_invitation_en_dry_run_par_defaut(client):
    out = _tool("luma_guest_admin")(event_id="evt-1", op="invite",
                                    guests=[{"email": "a@example.test"}])
    assert out["dry_run"] is True and out["recipients"] == ["a@example.test"]
    client.send_invites.assert_not_called()
    _tool("luma_guest_admin")(event_id="evt-1", op="invite", dry_run=False,
                              guests=[{"email": "a@example.test"}])
    client.send_invites.assert_called_once()


def test_blast_en_dry_run_par_defaut(client):
    out = _tool("luma_blasts")(op="send", event_id="evt-1", content_md="Hi")
    assert out["dry_run"] is True
    assert out["recipient_groups"] == [{"status": "approved"}]
    client.create_blast.assert_not_called()
    _tool("luma_blasts")(op="send", event_id="evt-1", content_md="Hi", dry_run=False)
    client.create_blast.assert_called_once()


def test_annulation_en_dry_run_dit_si_des_invites_ont_paye(client):
    client.request_event_cancellation.return_value = {
        "cancellation_token": "tok", "is_paid": True}
    out = _tool("luma_event_admin")(op="cancel", event_id="evt-1")
    assert out["dry_run"] is True and out["is_paid"] is True
    assert "tok" not in str(out)
    client.cancel_event.assert_not_called()


def test_annulation_payante_exige_should_refund(client):
    client.request_event_cancellation.return_value = {
        "cancellation_token": "tok", "is_paid": True}
    with pytest.raises(McpError, match="should_refund"):
        _tool("luma_event_admin")(op="cancel", event_id="evt-1", dry_run=False)
    client.cancel_event.assert_not_called()
    _tool("luma_event_admin")(op="cancel", event_id="evt-1", dry_run=False,
                              should_refund=True)
    client.cancel_event.assert_called_once_with("evt-1", "tok", should_refund=True)


def test_annulation_gratuite_passe_sans_should_refund(client):
    client.request_event_cancellation.return_value = {
        "cancellation_token": "tok", "is_paid": False}
    _tool("luma_event_admin")(op="cancel", event_id="evt-1", dry_run=False)
    client.cancel_event.assert_called_once_with("evt-1", "tok", should_refund=None)


# --- écritures directes -------------------------------------------------------

def test_changement_de_statut_transmet_send_email(client):
    _tool("luma_guest_admin")(event_id="evt-1", op="set_status",
                              guest="gst-1", status="approved", send_email=False)
    client.update_guest_status.assert_called_once_with(
        "evt-1", "gst-1", "approved", should_refund=None, send_email=False,
        message=None)


def test_ajout_refuse_le_statut_declined(client):
    with pytest.raises(McpError, match="declined"):
        _tool("luma_guest_admin")(event_id="evt-1", op="add", status="declined",
                                  guests=[{"email": "a@example.test"}])
    client.add_guests.assert_not_called()


@pytest.mark.parametrize("tool,kw,needle", [
    ("luma_events", {"op": "get"}, "event_id"),
    ("luma_event_admin", {"op": "update", "event_id": "evt-1"}, "fields"),
    ("luma_guest_admin", {"event_id": "evt-1", "op": "set_status", "guest": "g"},
     "status"),
    ("luma_tickets", {"op": "create_coupon", "code": "X"}, "discount"),
    ("luma_calendar", {"op": "update", "fields": {"name": "N"}}, "calendar_id"),
    ("luma_webhooks", {"op": "create", "url": "https://x.test"}, "event_types"),
])
def test_arguments_requis(client, tool, kw, needle):
    with pytest.raises(McpError, match=needle):
        _tool(tool)(**kw)


def test_refus_local_du_client_nomme(client):
    client.list_contacts.side_effect = ValueError("`limit` must be an integer >= 1")
    with pytest.raises(McpError, match="limit"):
        _tool("luma_contacts")(limit=0)


# --- refus amont --------------------------------------------------------------

@pytest.mark.parametrize("status,needle", [
    (401, "rejected the key"), (403, "Luma Plus"), (404, "not found"),
    (422, "HTTP 422"), (429, "minute"), (503, "unavailable"),
])
def test_traduction_des_refus_amont(client, status, needle):
    client.list_webhooks.side_effect = _upstream(status)
    with pytest.raises(McpError, match=needle):
        _tool("luma_webhooks")()


# --- sonde --------------------------------------------------------------------

def test_sonde_appelle_get_self(client):
    from oto_mcp.tools import luma_socle as S
    S._verify({"key": "cle"})
    client.get_self.assert_called_once_with()
    assert client.built == [{"api_key": "cle"}]


@pytest.mark.parametrize("status,classe", [(401, cv.UNAUTHORIZED), (500, cv.UNKNOWN)])
def test_sonde_classe_par_code(client, status, classe):
    from oto_mcp.tools import luma_socle as S
    client.get_self.side_effect = _upstream(status)
    with pytest.raises(Exception) as e:
        S._verify({"key": "cle"})
    assert cv.classer(e.value) == classe
