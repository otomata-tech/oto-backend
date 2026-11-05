"""Connecteur Nextmotion — dispatch `op=`, projection des données de santé, sonde.

Ce que ce fichier verrouille :
- la SURFACE (19 tools, sur les sept modules du connecteur) et le routage de chaque op
  vers la bonne méthode du client — les ressources ajoutées le 2026-09-17 ont leur
  propre fichier, `test_nextmotion_administratif.py`, les écritures et l'identité du
  patient (2026-10-01) le leur, `test_nextmotion_ecritures.py` ;
- un argument requis manquant nommé, un argument non pertinent REFUSÉ — « fourni » se
  lit `is not None`, donc `dry_run=False` et `offset=0` comptent ;
- `dry_run` vaut True par défaut sur reschedule/delete et n'atteint JAMAIS la méthode
  mutante (toutes les autres écritures : `test_nextmotion_ecritures.py`) ;
- la projection en liste blanche : un rendez-vous, un devis, une facture ne rendent
  aucun champ de santé ni texte libre, même quand l'API les envoie ;
- le filtre de période des factures : parcours de toutes les pages sans supposer un
  ordre, plafond de pages, `complet` faux et `offset_suivant` quand le plafond coupe ;
- le stock produits en liste blanche, sans rattachement aux factures ;
- la sonde : une clé vide est refusée avant le client, un 401/403 se classe
  `NonAutorise` sur `status_code`.

Toutes les valeurs sont factices.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

C = "00000000-0000-4000-8000-00000000000c"
X = "00000000-0000-4000-8000-00000000000d"
SENTINEL = "SENTINELLE-CLINIQUE"


@pytest.fixture
def client(monkeypatch):
    """Faux `NextmotionClient` + clé résolue. `register()` importe la classe À
    L'INTÉRIEUR de la fonction : patcher l'attribut du package suffit."""
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    return inst


def _mcp():
    """Les outils de TOUS les modules que le registre monte pour ce connecteur : le
    cliquet et la surface se jugent sur le montage réel, pas sur un module."""
    import importlib

    from fastmcp import FastMCP
    from oto_mcp.providers.nextmotion import CONNECTOR

    m = FastMCP("t")
    for mod in CONNECTOR.modules:
        importlib.import_module(f"oto_mcp.tools.{mod}").register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def test_the_surface_is_exactly_twenty_tools(client):
    assert sorted(t.name for t in asyncio.run(_mcp().list_tools())) == [
        "nextmotion_appointment", "nextmotion_availability", "nextmotion_calendar",
        "nextmotion_catalog", "nextmotion_clinic", "nextmotion_communication",
        "nextmotion_device_usage", "nextmotion_invoice", "nextmotion_journey",
        "nextmotion_lead", "nextmotion_package_usage", "nextmotion_patient",
        "nextmotion_patient_demographics", "nextmotion_patient_stats", "nextmotion_payment",
        "nextmotion_practitioner",
        "nextmotion_product", "nextmotion_quote", "nextmotion_setting",
        "nextmotion_statistics",
    ]


# --- routage -----------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,method", [
    ("nextmotion_clinic", {}, "list_clinics"),
    ("nextmotion_practitioner", {"clinic_id": C}, "list_doctors"),
    ("nextmotion_practitioner", {"op": "get", "doctor_id": X}, "get_doctor"),
    ("nextmotion_appointment", {"clinic_id": C}, "list_appointments"),
    ("nextmotion_appointment", {"op": "get", "appointment_id": X}, "get_appointment"),
    ("nextmotion_availability", {"clinic_id": C}, "search_time_slots"),
    ("nextmotion_catalog", {"kind": "visit_type", "clinic_id": C}, "list_visit_types"),
    ("nextmotion_catalog", {"kind": "treatment_pricing", "op": "get", "item_id": X},
     "get_treatment_pricing"),
    ("nextmotion_quote", {"clinic_id": C}, "list_quotes"),
    ("nextmotion_quote", {"op": "get", "quote_id": X}, "get_quote"),
    ("nextmotion_invoice", {"clinic_id": C}, "list_invoices"),
    ("nextmotion_invoice", {"op": "get", "invoice_id": X}, "get_invoice"),
    ("nextmotion_product", {"clinic_id": C}, "list_products"),
    ("nextmotion_product", {"op": "get", "product_id": X}, "get_product"),
])
def test_ops_route_to_the_right_client_method(client, tool, kwargs, method):
    _tool(tool)(**kwargs)
    getattr(client, method).assert_called_once()


def test_list_defaults_are_sent_as_the_api_defaults(client):
    _tool("nextmotion_appointment")(clinic_id=C, date="2026-01-02")
    client.list_appointments.assert_called_once_with(
        C, date="2026-01-02", patient_id=None, limit=50, offset=0)


def test_catalog_forwards_only_its_kinds_filter(client):
    _tool("nextmotion_catalog")(kind="sub_visit_type", clinic_id=C, visit_type_id=X)
    client.list_sub_visit_types.assert_called_once_with(
        C, visit_type_id=X, limit=50, offset=0)


# --- refus -------------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,match", [
    ("nextmotion_practitioner", {}, "requires `clinic_id`"),
    ("nextmotion_practitioner", {"op": "get"}, "requires `doctor_id`"),
    ("nextmotion_appointment", {"op": "delete"}, "requires `appointment_id`"),
    ("nextmotion_appointment", {"op": "reschedule", "appointment_id": X},
     "requires `visit_type_opening_hour_id`, `time_slot`"),
    ("nextmotion_catalog", {"kind": "treatment_type", "op": "get"}, "requires `item_id`"),
])
def test_missing_required_argument_is_named(client, tool, kwargs, match):
    with pytest.raises(McpError, match=match):
        _tool(tool)(**kwargs)
    assert not client.method_calls


@pytest.mark.parametrize("tool,kwargs,match", [
    # Des valeurs « fausses » restent des valeurs fournies.
    ("nextmotion_appointment", {"clinic_id": C, "dry_run": False}, "`dry_run`"),
    ("nextmotion_appointment", {"op": "get", "appointment_id": X, "dry_run": False},
     "`dry_run`"),
    ("nextmotion_practitioner", {"op": "get", "doctor_id": X, "offset": 0}, "`offset`"),
    ("nextmotion_appointment", {"op": "delete", "appointment_id": X, "clinic_id": C},
     "`clinic_id`"),
    ("nextmotion_appointment", {"op": "delete", "appointment_id": X, "time_slot": "t"},
     "`time_slot`"),
    ("nextmotion_quote", {"op": "get", "quote_id": X, "patient_id": X}, "`patient_id`"),
    ("nextmotion_catalog", {"kind": "visit_type", "clinic_id": C, "search": "zzz"},
     "`search`"),
    ("nextmotion_catalog", {"kind": "visit_type_category", "clinic_id": C,
                            "visit_type_id": X}, "`visit_type_id`"),
    ("nextmotion_invoice", {"op": "get", "invoice_id": X, "invoiced_from": "2026-01-01"},
     "`invoiced_from`"),
    ("nextmotion_invoice", {"clinic_id": C, "max_pages": 1}, "`max_pages`"),
    ("nextmotion_product", {"clinic_id": C, "product_id": X}, "`product_id`"),
    ("nextmotion_product", {"op": "get", "product_id": X, "stock_state": "low"},
     "`stock_state`"),
    ("nextmotion_product", {"op": "get", "product_id": X, "offset": 0}, "`offset`"),
])
def test_an_argument_the_op_does_not_use_is_refused(client, tool, kwargs, match):
    with pytest.raises(McpError, match=f"does not use {match}"):
        _tool(tool)(**kwargs)
    assert not client.method_calls


def test_unknown_op_is_refused(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("nextmotion_appointment")(op="cancel", appointment_id=X)


# --- écritures : dry_run par défaut ------------------------------------------------

@pytest.mark.parametrize("op,extra,mutating", [
    ("delete", {}, "delete_appointment"),
    ("reschedule", {"visit_type_opening_hour_id": X, "time_slot": "2026-01-02T10:00:00Z"},
     "reschedule_appointment"),
])
def test_writes_default_to_dry_run_and_never_reach_the_mutating_method(
        client, op, extra, mutating):
    client.get_appointment.return_value = {"data": {"id": X, "status": "confirmed"}}
    out = _tool("nextmotion_appointment")(op=op, appointment_id=X, **extra)
    assert out["dry_run"] is True and out["would"] == op
    assert out["appointment"] == {"id": X, "status": "confirmed"}
    getattr(client, mutating).assert_not_called()


def test_dry_run_true_explicit_does_not_write_either(client):
    client.get_appointment.return_value = {"data": {"id": X}}
    _tool("nextmotion_appointment")(op="delete", appointment_id=X, dry_run=True)
    client.delete_appointment.assert_not_called()


def test_dry_run_false_writes(client):
    _tool("nextmotion_appointment")(
        op="reschedule", appointment_id=X, visit_type_opening_hour_id=C,
        time_slot="2026-01-02T10:00:00Z", dry_run=False)
    client.reschedule_appointment.assert_called_once_with(
        X, visit_type_opening_hour_id=C, time_slot="2026-01-02T10:00:00Z")

    out = _tool("nextmotion_appointment")(op="delete", appointment_id=X, dry_run=False)
    client.delete_appointment.assert_called_once_with(X)
    assert out == {"deleted": True, "appointment_id": X}


def test_no_read_op_reaches_a_mutating_method(client):
    for kwargs in ({"clinic_id": C}, {"op": "get", "appointment_id": X}):
        _tool("nextmotion_appointment")(**kwargs)
    client.delete_appointment.assert_not_called()
    client.reschedule_appointment.assert_not_called()


# --- projection des données de santé -----------------------------------------------

def _patient():
    return {"id": X, "first_name": SENTINEL, "last_name": SENTINEL,
            "email": SENTINEL, "phone_number": SENTINEL,
            "birth_date": "1900-01-01", "age": 126, "gender": 9,
            "doctor_comments": SENTINEL, "patient_number": SENTINEL,
            "postal_address": SENTINEL, "photograph": {"media_file": SENTINEL}}


_MEDICAL_KEYS = {"first_name", "last_name", "email", "phone_number", "patient_number",
                 "title", "birth_date", "age", "gender", "doctor_comments", "photograph",
                 "preview_photograph", "notes", "free_text", "template_text", "details",
                 "rebate_details", "treatment", "bolt_note", "visit", "subtitle",
                 "treatment_session_status", "document", "postal_address"}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _assert_clean(out):
    assert SENTINEL not in json.dumps(out)
    assert not _MEDICAL_KEYS & set(_keys(out))


def test_appointment_withholds_health_data(client):
    client.list_appointments.return_value = {"count": 1, "next": None, "data": [{
        "id": X, "status": "confirmed", "subject": SENTINEL, "visit": X,
        "patient": _patient(),
        "visit_type": {"id": C, "subject": "Type-Test", "price": "0.00",
                       "bolt_note": {"name": SENTINEL}, "category": {"id": C, "name": "Cat"}},
        "calendar_event": {"id": C, "start_time": "2026-01-02T10:00:00Z",
                           "notes": SENTINEL, "subtitle": SENTINEL, "title": SENTINEL,
                           "treatment_session_status": SENTINEL,
                           "appointment_reminder_sms": SENTINEL,
                           "doctors": [{"id": C, "prefixed_name": "Dr Test",
                                        "photograph": {"media_file": SENTINEL}}]},
        "field_added_tomorrow": SENTINEL,
    }]}
    out = _tool("nextmotion_appointment")(clinic_id=C)
    _assert_clean(out)
    row = out["appointments"][0]
    assert row["patient"] == {"id": X}
    assert row["calendar_event"]["doctors"] == [{"id": C, "prefixed_name": "Dr Test"}]
    assert row["visit_type"]["category"] == {"id": C, "name": "Cat"}
    assert out["has_more"] is False and "withheld" in out


@pytest.mark.parametrize("op,extra", [
    ("delete", {}),
    ("reschedule", {"visit_type_opening_hour_id": C, "time_slot": "2026-01-02T10:00:00Z"}),
])
def test_appointment_dry_run_preview_is_projected_too(client, op, extra):
    client.get_appointment.return_value = {"data": {"id": X, "patient": _patient()}}
    out = _tool("nextmotion_appointment")(op=op, appointment_id=X, **extra)
    _assert_clean(out)
    assert out["appointment"]["patient"] == {"id": X}


@pytest.mark.parametrize("tool,method,lines", [
    ("nextmotion_quote", "get_quote", "quoted_treatments"),
    ("nextmotion_invoice", "get_invoice", "invoiced_treatments"),
])
def test_billing_documents_withhold_health_data(client, tool, method, lines):
    getattr(client, method).return_value = {"data": {
        "id": X, "number_id": "T-0001", "status": 3, "total_price": "0.00",
        "title": SENTINEL,
        "notes": SENTINEL, "free_text": SENTINEL, "template_text": SENTINEL,
        "rebate_details": SENTINEL, "document": {"media_file": SENTINEL},
        "patient": _patient(),
        lines: [{"id": C, "name": "Ligne-Test", "price": "0.00", "quantity": 1,
                 "details": SENTINEL, "treatment": X}],
    }}
    arg = {"nextmotion_quote": "quote_id", "nextmotion_invoice": "invoice_id"}[tool]
    out = _tool(tool)(op="get", **{arg: X})
    _assert_clean(out)
    doc = out[tool.split("_")[1]]
    assert doc[lines] == [{"id": C, "name": "Ligne-Test", "price": "0.00", "quantity": 1}]
    assert doc["patient"] == {"id": X}
    assert doc["number_id"] == "T-0001" and doc["total_price"] == "0.00"


# --- erreurs amont & clé -----------------------------------------------------------

def test_non_employee_403_is_read_on_status_code_and_error_code(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.list_doctors.side_effect = UpstreamHTTPError(
        403, {"errors": [{"code": "non_employee_access_denied", "message": "x"}]},
        service="nextmotion")
    with pytest.raises(McpError, match="is not an employee of this clinic"):
        _tool("nextmotion_practitioner")(clinic_id=C)


def test_a_plain_403_says_the_clinic_is_outside_the_key_user_rights(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.list_doctors.side_effect = UpstreamHTTPError(
        403, {"errors": [{"code": "permission_denied", "message": "x"}]}, service="nextmotion")
    with pytest.raises(McpError) as exc:
        _tool("nextmotion_practitioner")(clinic_id=C)
    msg = str(exc.value)
    assert "the API key is valid" in msg and "`clinic_id`" in msg
    assert "Nextmotion code: permission_denied." in msg


def test_client_value_error_becomes_invalid_params(client):
    client.get_doctor.side_effect = ValueError("doctor_id must be a UUID")
    with pytest.raises(McpError, match="UUID"):
        _tool("nextmotion_practitioner")(op="get", doctor_id="42")


def test_an_empty_resolved_key_never_builds_a_client(monkeypatch):
    built = []
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient",
                        lambda **kw: built.append(kw))
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("  ", False))
    with pytest.raises(McpError, match="no API key"):
        _tool("nextmotion_clinic")()
    assert not built


@pytest.mark.parametrize("fields", [{}, {"key": ""}, {"key": "   "}, {"key": None}])
def test_probe_refuses_an_empty_key_before_the_client(monkeypatch, fields):
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import nextmotion as N

    built = []
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient",
                        lambda **kw: built.append(kw))
    with pytest.raises(connector_verify.NonAutorise):
        N._verify(fields)
    assert not built


@pytest.mark.parametrize("status,expected", [(401, "NonAutorise"), (403, "NonAutorise"),
                                             (500, "UpstreamHTTPError")])
def test_probe_classifies_on_status_code(monkeypatch, status, expected):
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.tools import nextmotion as N

    inst = MagicMock()
    inst.get_me.side_effect = UpstreamHTTPError(status, {"errors": []}, service="nextmotion")
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: inst)
    with pytest.raises(Exception) as exc:
        N._verify({"key": "nm-test"})
    assert type(exc.value).__name__ == expected


def test_probe_calls_get_me_with_the_posed_key(monkeypatch):
    from oto_mcp.tools import nextmotion as N

    seen = {}
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient",
                        lambda **kw: seen.update(kw) or inst)
    N._verify({"key": "nm-test"})
    assert seen == {"api_key": "nm-test"}
    inst.get_me.assert_called_once_with()


# --- projection des listes (cliquet `test_sorties_listes_projetees`) ----------------

def test_fields_ne_garde_que_les_colonnes_demandees_et_l_id(client):
    client.list_clinics.return_value = {"count": 1, "next": None, "data": [
        {"id": C, "name": SENTINEL, "address": "adresse factice", "phone": "0"}]}
    out = _tool("nextmotion_clinic")(fields=["name"])
    assert out["clinics"] == [{"id": C, "name": SENTINEL}]
    assert out["count"] == 1 and out["has_more"] is False


def test_fields_inconnu_est_nomme_avec_les_cles_servies(client):
    # Le libellé d'un type de visite est `subject`, sa durée `duration_minutes` :
    # `name` et `duration` demandés disparaissaient sans un mot.
    client.list_visit_types.return_value = {"count": 1, "next": None, "data": [
        {"id": X, "subject": "Consultation", "duration_minutes": 30, "price": "50.00"}]}
    out = _tool("nextmotion_catalog")(kind="visit_type", clinic_id=C,
                                      fields=["id", "name", "duration", "price"])
    assert out["visit_types"] == [{"id": X, "price": "50.00"}]
    assert out["missing_fields"] == ["name", "duration"]
    assert "subject" in out["missing_fields_hint"]
    assert "duration_minutes" in out["missing_fields_hint"]


def test_fields_tous_connus_ne_dit_rien(client):
    client.list_clinics.return_value = {"count": 1, "next": None, "data": [
        {"id": C, "name": SENTINEL}]}
    assert "missing_fields" not in _tool("nextmotion_clinic")(fields=["name"])


def test_fields_etoile_rend_la_liste_blanche_jamais_le_brut(client):
    client.list_appointments.return_value = {"count": 1, "next": None, "data": [
        {"id": X, "status": 1, "patient": {"id": X, "last_name": "SENTINELLE-NOM",
                                           "birth_date": "1900-01-01"}}]}
    brut = json.dumps(_tool("nextmotion_appointment")(clinic_id=C, fields=["*"]))
    assert "SENTINELLE-NOM" not in brut and "birth_date" not in brut


def test_fields_sur_un_get_est_refuse(client):
    with pytest.raises(McpError):
        _tool("nextmotion_invoice")(op="get", invoice_id=X, fields=["id"])


def test_chaque_outil_de_liste_satisfait_le_cliquet_de_projection(client):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_sorties_listes_projetees import _pagine, _projette
    outils = asyncio.run(_mcp().list_tools())
    assert [t.name for t in outils if _pagine(t) and not _projette(t)] == []


# --- factures : filtre de période côté outil ----------------------------------------

def _factures(*dates, suite=True):
    """Une page amont : une facture par date (`None` = pas de `invoiced_time`)."""
    rows = [{"id": f"00000000-0000-4000-8000-{i:012d}", "status": 3,
             "patient": _patient(), **({"invoiced_time": d} if d else {})}
            for i, d in enumerate(dates)]
    return {"count": 9999, "next": "page-suivante" if suite else None, "data": rows}


def test_periode_parcourt_toutes_les_pages_sans_supposer_un_ordre(client):
    client.list_invoices.side_effect = [
        # Une page ENTIÈRE avant la période : un parcours qui supposerait un tri
        # décroissant s'arrêterait là et perdrait la page suivante.
        _factures("2025-12-31T23:59:00+01:00", "2025-11-02T09:00:00+01:00"),
        _factures("2026-01-01T00:00:00+01:00", "2026-01-31T23:30:00+01:00",
                  "2026-02-01T00:00:00+01:00", suite=False),
    ]
    out = _tool("nextmotion_invoice")(clinic_id=C, invoiced_from="2026-01-01",
                                      invoiced_to="2026-01-31")
    assert [c.kwargs["offset"] for c in client.list_invoices.call_args_list] == [0, 2]
    assert {c.kwargs["limit"] for c in client.list_invoices.call_args_list} == {100}
    assert [f["invoiced_time"] for f in out["invoices"]] == [
        "2026-01-01T00:00:00+01:00", "2026-01-31T23:30:00+01:00"]
    assert out["pages_lues"] == 2 and out["factures_parcourues"] == 5
    assert out["complet"] is True and out["offset_suivant"] is None
    _assert_clean(out)


def test_periode_coupee_par_le_plafond_le_dit_et_donne_la_reprise(client):
    client.list_invoices.side_effect = [_factures("2026-01-05T10:00:00Z"),
                                        _factures("2026-01-06T10:00:00Z")]
    out = _tool("nextmotion_invoice")(clinic_id=C, invoiced_from="2026-01-01",
                                      max_pages=2, offset=40)
    assert [c.kwargs["offset"] for c in client.list_invoices.call_args_list] == [40, 41]
    assert out["complet"] is False and out["offset_suivant"] == 42
    assert out["pages_lues"] == 2 and out["factures_parcourues"] == 2
    assert len(out["invoices"]) == 2


def test_periode_applique_fields_et_la_liste_blanche(client):
    client.list_invoices.return_value = _factures("2026-01-05T10:00:00Z", suite=False)
    out = _tool("nextmotion_invoice")(clinic_id=C, invoiced_to="2026-01-05",
                                      fields=["invoiced_time"])
    assert set(out["invoices"][0]) == {"id", "invoiced_time"}
    assert out["complet"] is True and "withheld" in out
    _assert_clean(out)


def test_periode_sans_invoiced_time_lisible_leve(client):
    client.list_invoices.return_value = _factures(None, suite=False)
    with pytest.raises(McpError, match="invoiced_time"):
        _tool("nextmotion_invoice")(clinic_id=C, invoiced_from="2026-01-01")


@pytest.mark.parametrize("kwargs,match", [
    ({"invoiced_from": "2026-01-01", "limit": 10}, "`limit`"),
    ({"invoiced_from": "01/01/2026"}, "YYYY-MM-DD"),
    ({"invoiced_from": "2026-02-01", "invoiced_to": "2026-01-01"}, "is after"),
    ({"invoiced_from": "2026-01-01", "max_pages": 0}, "max_pages"),
    ({"invoiced_from": "2026-01-01", "max_pages": 101}, "max_pages"),
])
def test_periode_refuse_ce_qui_n_a_pas_de_sens(client, kwargs, match):
    with pytest.raises(McpError, match=match):
        _tool("nextmotion_invoice")(clinic_id=C, **kwargs)
    assert not client.method_calls


def test_sans_periode_la_liste_reste_une_page(client):
    client.list_invoices.return_value = _factures("2026-01-05T10:00:00Z")
    out = _tool("nextmotion_invoice")(clinic_id=C, limit=10)
    client.list_invoices.assert_called_once_with(C, limit=10, offset=0)
    assert "complet" not in out and out["has_more"] is True


# --- stock produits -----------------------------------------------------------------

def test_product_list_forwards_the_spec_filters(client):
    _tool("nextmotion_product")(clinic_id=C, search="zzz", stock_state="out",
                                expiring_within_days=30, order="-stock_level")
    client.list_products.assert_called_once_with(
        C, search="zzz", stock_state="out", expiring_within_days=30,
        order="-stock_level", limit=50, offset=0)


def test_product_is_a_whitelist_without_patient_note(client):
    produit = {"id": X, "lot_number": "LOT-TEST", "expiration_date": "2030-01-01",
               "stock_level": "1.00", "warning_level": 0, "physical_stock_level": "1.00",
               "physical_stock_diff": "0", "unit_price": "0.00",
               "created_time": "2026-01-01T00:00:00Z", "modified_time": "2026-01-01T00:00:00Z",
               "global_product": {"id": C, "name": "Produit-Test", "brand": "Marque-Test",
                                  "image": None, "champ_de_demain": SENTINEL},
               "champ_de_demain": SENTINEL}
    client.list_products.return_value = {"count": 1, "next": None, "data": [produit]}
    client.get_product.return_value = {"data": produit}
    page = _tool("nextmotion_product")(clinic_id=C)
    one = _tool("nextmotion_product")(op="get", product_id=X)
    for row in (page["products"][0], one["product"]):
        assert SENTINEL not in json.dumps(row)
        assert row["lot_number"] == "LOT-TEST"
        assert row["global_product"] == {"id": C, "name": "Produit-Test",
                                         "brand": "Marque-Test", "image": None}
    assert "withheld" not in page and "withheld" not in one


def test_product_description_says_stock_is_not_linked_to_invoices(client):
    tool = asyncio.run(_mcp().get_tool("nextmotion_product"))
    assert "not linked to invoices" in tool.description
