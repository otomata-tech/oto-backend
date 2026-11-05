"""Connecteur Nextmotion — le périmètre administratif étendu le 2026-09-17.

Ce que ce fichier verrouille :
- le routage des ressources ajoutées (agenda, catalogue, ventes, leads, réglages) vers
  la bonne méthode du client, avec les filtres de la spec ;
- les refus : filtre d'un autre `kind`, op sans endpoint, argument qu'une op n'utilise
  pas — `False` et `0` comptent (`is not None`) ;
- **la liste blanche sur CHAQUE outil et CHAQUE kind** : une réponse amont empoisonnée
  (identité d'un patient ou d'une personne, texte libre clinique, fichier, en-têtes,
  objet libre, champ inconnu) ne laisse sortir aucune valeur sentinelle, et un patient
  imbriqué sort réduit à `{"id"}` ;
- ce qui DOIT sortir : lignes de devis/facture complètes côté commercial, pipeline d'un
  lead, totaux d'un patient, graphiques.

Toutes les valeurs sont factices.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

C = "00000000-0000-4000-8000-00000000000c"
X = "00000000-0000-4000-8000-00000000000d"
S = "SENTINELLE-ADMIN"


def _mcp():
    import importlib

    from fastmcp import FastMCP
    from oto_mcp.providers.nextmotion import CONNECTOR

    m = FastMCP("t")
    for mod in CONNECTOR.modules:
        importlib.import_module(f"oto_mcp.tools.{mod}").register(m)
    return m


def _tool(name):
    return asyncio.run(_mcp().get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    return inst


# --- routage -----------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,method,args,sent", [
    ("nextmotion_calendar", {"kind": "room", "clinic_id": C}, "list_appointment_rooms",
     (C,), {"limit": 50, "offset": 0}),
    ("nextmotion_calendar", {"kind": "device", "op": "get", "item_id": X},
     "get_appointment_device", (X,), {}),
    ("nextmotion_calendar", {"kind": "opening_hour", "clinic_id": C, "show_all": False},
     "list_calendar_opening_hours", (C,), {"show_all": False, "limit": 50, "offset": 0}),
    ("nextmotion_calendar", {"kind": "absence", "clinic_id": C, "start_date": "2026-01-01",
                             "offset": 0}, "list_calendar_absences", (C,),
     {"start_date": "2026-01-01", "end_date": None, "show_all": None, "limit": 50,
      "offset": 0}),
    ("nextmotion_calendar", {"kind": "appointment_request", "clinic_id": C,
                             "request_status": "new"}, "list_appointment_requests", (C,),
     {"status": "new", "limit": 50, "offset": 0}),
    ("nextmotion_calendar", {"kind": "appointment_request", "op": "get", "item_id": X},
     "get_appointment_request", (X,), {}),
    ("nextmotion_journey", {"clinic_id": C, "doctor_ids": [X], "include_ongoing": False,
                            "order": "-start_time"}, "list_calendar_journeys", (C,),
     {"start_date": None, "end_date": None, "include_ongoing": False, "status": None,
      "doctor_ids": [X], "visit_type_ids": None, "sub_visit_type_ids": None,
      "patient_id": None, "order": "-start_time", "limit": 50, "offset": 0}),
    ("nextmotion_catalog", {"kind": "treatment_package", "clinic_id": C, "search": "z"},
     "list_treatment_packages", (C,), {"search": "z", "limit": 50, "offset": 0}),
    ("nextmotion_catalog", {"kind": "accounting_distribution", "op": "get", "item_id": X},
     "get_accounting_distribution", (X,), {}),
    ("nextmotion_catalog", {"kind": "global_product", "clinic_id": C},
     "list_global_products", (C,), {"search": None, "limit": 50, "offset": 0}),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "items", "item_id": X,
                            "limit": 5}, "list_treatment_package_items", (X,),
     {"limit": 5, "offset": 0}),
    ("nextmotion_catalog", {"kind": "treatment_pricing", "op": "distributions",
                            "item_id": X}, "list_treatment_pricing_distributions", (X,), {}),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "distributions",
                            "item_id": X}, "list_treatment_package_distributions", (X,), {}),
    ("nextmotion_payment", {"clinic_id": C, "invoice_id": X}, "list_payments", (C,),
     {"invoice_id": X, "limit": 50, "offset": 0}),
    ("nextmotion_payment", {"op": "get", "payment_id": X}, "get_payment", (X,), {}),
    ("nextmotion_statistics", {"kind": "appointment_income", "clinic_id": C,
                               "period_type": "week"},
     "get_appointment_income_statistics", (C,),
     {"start_date": None, "end_date": None, "period_type": "week"}),
    ("nextmotion_statistics", {"kind": "treatment_types_income", "clinic_id": C,
                               "end_date": "2026-06-30"},
     "list_treatment_type_income_statistics", (C,),
     {"start_date": None, "end_date": "2026-06-30", "period_type": None}),
    ("nextmotion_patient_stats", {"patient_id": X}, "get_patient_stats", (X,), {}),
    ("nextmotion_lead", {"clinic_id": C}, "list_leads", (C,), {"limit": 50, "offset": 0}),
    ("nextmotion_lead", {"op": "get", "lead_id": X}, "get_lead", (X,), {}),
    ("nextmotion_setting", {"kind": "feature", "clinic_id": C}, "list_clinic_features",
     (C,), {"limit": 50, "offset": 0}),
    ("nextmotion_setting", {"kind": "object_label", "clinic_id": C,
                            "label_types": ["lead_source"]}, "list_object_labels", (C,),
     {"types": ["lead_source"], "limit": 50, "offset": 0}),
    ("nextmotion_setting", {"kind": "document_template", "clinic_id": C,
                            "document_type": 0}, "list_document_templates", (C,),
     {"type": 0, "master_id": None, "limit": 50, "offset": 0}),
    ("nextmotion_setting", {"kind": "communication_template", "clinic_id": C,
                            "template_kind": "sms"}, "list_communication_templates", (C,),
     {"kind": "sms", "limit": 50, "offset": 0}),
    ("nextmotion_setting", {"kind": "webhook", "op": "get", "item_id": X}, "get_webhook",
     (X,), {}),
    ("nextmotion_setting", {"kind": "payment_medium", "op": "get", "item_id": X},
     "get_payment_medium", (X,), {}),
])
def test_routage_et_filtres(client, tool, kwargs, method, args, sent):
    _tool(tool)(**kwargs)
    getattr(client, method).assert_called_once_with(*args, **sent)


# --- refus -------------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,match", [
    ("nextmotion_calendar", {"kind": "room", "clinic_id": C, "show_all": False},
     "does not use `show_all`"),
    ("nextmotion_calendar", {"kind": "absence", "op": "get", "item_id": X,
                             "show_all": False}, "does not use `show_all`"),
    ("nextmotion_calendar", {"kind": "opening_hour", "clinic_id": C,
                             "request_status": "new"}, "does not use `request_status`"),
    ("nextmotion_calendar", {"kind": "room", "op": "get", "item_id": X, "offset": 0},
     "does not use `offset`"),
    ("nextmotion_calendar", {"kind": "room"}, "requires `clinic_id`"),
    ("nextmotion_catalog", {"kind": "global_product", "op": "get", "item_id": X},
     "no single read"),
    ("nextmotion_catalog", {"kind": "visit_type", "op": "items", "item_id": X},
     "treatment_package"),
    ("nextmotion_catalog", {"kind": "visit_type", "op": "distributions", "item_id": X},
     "treatment_pricing"),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "items", "item_id": X,
                            "clinic_id": C}, "does not use `clinic_id`"),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "items", "item_id": X,
                            "search": ""}, "does not use `search`"),
    ("nextmotion_catalog", {"kind": "treatment_pricing", "op": "distributions",
                            "item_id": X, "offset": 0}, "does not use `offset`"),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "items"}, "requires `item_id`"),
    ("nextmotion_payment", {"op": "get", "payment_id": X, "invoice_id": X},
     "does not use `invoice_id`"),
    ("nextmotion_payment", {"clinic_id": C, "payment_id": X}, "does not use `payment_id`"),
    ("nextmotion_lead", {"op": "get", "lead_id": X, "limit": 10}, "does not use `limit`"),
    ("nextmotion_setting", {"kind": "feature", "op": "get", "item_id": X},
     "no single read"),
    ("nextmotion_setting", {"kind": "object_label", "op": "get", "item_id": X},
     "no single read"),
    ("nextmotion_setting", {"kind": "webhook", "clinic_id": C, "document_type": 0},
     "does not use `document_type`"),
    ("nextmotion_patient_stats", {"patient_id": ""}, "requires `patient_id`"),
])
def test_refus_avant_tout_appel(client, tool, kwargs, match):
    with pytest.raises(McpError, match=match):
        _tool(tool)(**kwargs)
    assert not client.method_calls


def test_aucun_filtre_par_nom_de_personne_n_est_expose(client):
    for name in ("nextmotion_lead", "nextmotion_journey"):
        props = asyncio.run(_mcp().get_tool(name)).parameters["properties"]
        assert "search" not in props, name
    order = asyncio.run(_mcp().get_tool("nextmotion_journey")).parameters["properties"]
    assert "patient_name" not in json.dumps(order["order"])


# --- liste blanche : une réponse empoisonnée, sur chaque outil et chaque kind ----------

def _patient():
    return {"id": X, "first_name": S, "last_name": S, "email": S, "phone_number": S,
            "birth_date": S, "age": S, "gender": S, "doctor_comments": S,
            "patient_number": S, "postal_address": S, "city": S,
            "photograph": {"media_file": S}}


def _poison():
    """Une ligne amont qui porte TOUT ce qui ne doit pas sortir, aux clés où l'API le met."""
    ligne = {"id": C, "name": "Ligne-Test", "details": S, "treatment": X,
             "subpricing": [{"kind": "k", "details": S, "accounting_code": "706"}]}
    return {
        "id": X, "patient": _patient(), "first_name": S, "last_name": S, "email": S,
        "phone_number": S, "birth_date": S, "age": S, "gender": S, "notes": S,
        "free_text": S, "template_text": S, "details": S, "title": S, "subject": S,
        "rebate_details": S, "external_reference": S, "pre_payment_url": S,
        "pre_payment_message": S, "headers": {"Authorization": S}, "template": {"html": S},
        "meta": {"k": S}, "consultation": {"id": X, "name": S}, "visit": X,
        "document": {"media_file": S}, "info": {"media_file": S},
        "photograph": {"media_file": S}, "bolt_note": {"name": S},
        "survey_form": {"name": S}, "subpayment": {"k": S}, "last_opened_time": S,
        "media_event_count": S, "last_media_event_time": S,
        "status": {"libre": S}, "statuses": [{"libre": S}, "confirmed"],
        "required_steps": [{"libre": S}],
        "champ_de_demain": S,
        "calendar_event": {"id": C, "title": S, "subtitle": S, "notes": S,
                           "treatment_session_status": S, "appointment_reminder_sms": S,
                           "appointment_canceled_whatsapp_msg": S,
                           "doctors": [{"id": C, "prefixed_name": "Dr Test",
                                        "photograph": {"media_file": S}}]},
        "invoice": {"id": C, "patient": _patient(), "title": S, "free_text": S,
                    "document": {"media_file": S}, "invoiced_treatments": [ligne]},
        "invoiced_treatments": [ligne], "quoted_treatments": [ligne],
    }


class _Empoisonne:
    """Faux client : toute liste rend une page d'une ligne empoisonnée, toute lecture la
    ligne seule. Les statistiques par type rendent une liste, comme la spec."""

    def __init__(self, sans=()):
        self._sans = set(sans)
        self.appels = []

    def _ligne(self):
        return {k: v for k, v in _poison().items() if k not in self._sans}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.appels.append(name)
            if name.startswith(("list_", "reorder_", "set_")) or name == "search_time_slots":
                return {"count": 1, "next": None, "data": [self._ligne()]}
            return {"data": self._ligne()}
        return call


#: L'identité que `nextmotion_patient` SERT : retirée du poison pour cet outil seul.
_IDENTITE = ("first_name", "last_name", "email", "phone_number", "birth_date", "age",
             "gender")
_CONTACT = ("first_name", "last_name", "email", "phone_number")
_EVENT = {"start_time": "t0", "end_time": "t1"}

_APPELS = [
    ("nextmotion_appointment", {"clinic_id": C}, ()),
    # Les écritures, aperçu (`dry_run` par défaut, qui relit l'objet) et réponse.
    ("nextmotion_appointment", {"op": "update", "appointment_id": X,
                                "data": {"calendar_event": _EVENT}}, ()),
    ("nextmotion_appointment", {"op": "update", "appointment_id": X, "dry_run": False,
                                "data": {"calendar_event": _EVENT}}, ()),
    ("nextmotion_quote", {"op": "delete", "quote_id": X}, ()),
    ("nextmotion_quote", {"op": "validate", "quote_id": X, "dry_run": False}, ()),
    ("nextmotion_invoice", {"op": "pay", "invoice_id": X, "dry_run": False,
                            "data": {"card": "1.00"}}, ()),
    ("nextmotion_invoice", {"op": "credit_note", "clinic_id": C, "dry_run": False,
                            "data": {"patient": X, "items": [{"amount": "1.00"}]}}, ()),
    ("nextmotion_payment", {"op": "update", "payment_id": X, "dry_run": False,
                            "data": {"cash": "1.00"}}, ()),
    ("nextmotion_lead", {"op": "update", "lead_id": X,
                         "data": {"first_name": "f", "last_name": "l"}}, _CONTACT),
    ("nextmotion_lead", {"op": "create", "clinic_id": C, "dry_run": False,
                         "data": {"first_name": "f", "last_name": "l"}}, _CONTACT),
    ("nextmotion_lead", {"op": "convert", "lead_id": X, "dry_run": False}, ()),
    ("nextmotion_product", {"op": "delete", "product_id": X, "dry_run": False}, ()),
    ("nextmotion_calendar", {"kind": "appointment_request", "op": "create",
                             "dry_run": False,
                             "data": {"visit_type_opening_hour": X, "time_slot": "t",
                                      "email": "e", "first_name": "f", "last_name": "l",
                                      "birth_date": "b", "phone_number": "p"}}, ()),
    ("nextmotion_calendar", {"kind": "absence", "op": "update", "item_id": X,
                             "dry_run": False, "data": {"calendar_event": _EVENT}}, ()),
    ("nextmotion_communication", {"kind": "call", "clinic_id": C, "dry_run": False,
                                  "data": {"patient": X, "notes": "n"}}, ()),
    ("nextmotion_communication", {"kind": "message", "clinic_id": C, "dry_run": False,
                                  "data": {"communication_template_kind": "sms",
                                           "communication_template_type": "quote",
                                           "object": X}}, ()),
    ("nextmotion_setting", {"kind": "webhook", "op": "create", "clinic_id": C,
                            "dry_run": False,
                            "data": {"action_type": "a", "url": "u",
                                     "headers": {"Authorization": S}}}, ()),
    ("nextmotion_setting", {"kind": "survey_form", "op": "placeholders",
                            "survey_type": "bolt_note"}, ()),
    ("nextmotion_setting", {"kind": "document_template", "op": "duplicate", "item_id": X,
                            "dry_run": False}, ()),
    ("nextmotion_catalog", {"kind": "treatment_type", "op": "post_treatment",
                            "item_id": X}, ()),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "set_distributions",
                            "item_id": X, "dry_run": False,
                            "data": [{"user": C, "accounting_distribution": X}]}, ()),
    # L'outil patient sert l'identité ; tout le reste du poison doit rester dehors.
    ("nextmotion_patient", {"clinic_id": C}, _IDENTITE),
    ("nextmotion_patient", {"op": "get", "patient_id": X}, _IDENTITE),
    ("nextmotion_patient", {"op": "update", "patient_id": X, "dry_run": False,
                            "data": {"email": "e", "first_name": "f", "last_name": "l",
                                     "gender": "0"}}, _IDENTITE),
    ("nextmotion_appointment", {"op": "get", "appointment_id": X}, ()),
    ("nextmotion_quote", {"op": "get", "quote_id": X}, ()),
    ("nextmotion_invoice", {"clinic_id": C}, ()),
    ("nextmotion_product", {"clinic_id": C}, ()),
    # Le téléphone d'une CLINIQUE est le sien, pas celui d'un patient.
    ("nextmotion_clinic", {}, ("phone_number",)),
    ("nextmotion_availability", {"clinic_id": C}, ()),
    ("nextmotion_journey", {"clinic_id": C}, ()),
    ("nextmotion_payment", {"clinic_id": C}, ()),
    ("nextmotion_payment", {"op": "get", "payment_id": X}, ()),
    ("nextmotion_patient_stats", {"patient_id": X}, ()),
    # Un lead sert son identité de contact ; ses notes et sa référence externe non.
    ("nextmotion_lead", {"clinic_id": C}, _CONTACT),
    ("nextmotion_lead", {"op": "get", "lead_id": X}, _CONTACT),
    # Un graphique a un titre (celui du graphique, pas d'un patient).
    ("nextmotion_statistics", {"kind": "appointment_income", "clinic_id": C}, ("title",)),
    ("nextmotion_statistics", {"kind": "treatment_types", "clinic_id": C}, ("title",)),
    ("nextmotion_catalog", {"kind": "treatment_package", "op": "items", "item_id": X},
     ("details",)),
    ("nextmotion_catalog", {"kind": "treatment_pricing", "op": "distributions",
                            "item_id": X}, ()),
] + [
    ("nextmotion_calendar", {"kind": k, **o}, ())
    for k in ("room", "device", "opening_hour", "absence", "appointment_request")
    for o in ({"clinic_id": C}, {"op": "get", "item_id": X})
] + [
    ("nextmotion_setting", {"kind": k, **o}, ())
    for k, ops in (("feature", 1), ("object_label", 1), ("payment_medium", 2),
                   ("communication_template", 2), ("document_template", 2),
                   ("survey_form", 2), ("webhook", 2))
    for o in ({"clinic_id": C}, {"op": "get", "item_id": X})[:ops]
] + [
    # `details` d'un tarif et `subject` d'un type de visite du CATALOGUE sont des libellés
    # de la clinique, pas un texte sur un patient.
    ("nextmotion_catalog", {"kind": k, **o}, ("details", "subject"))
    for k, ops in (("visit_type", 2), ("visit_type_category", 2), ("sub_visit_type", 2),
                   ("treatment_type", 2), ("treatment_pricing", 2), ("treatment_package", 2),
                   ("accounting_distribution", 2), ("global_product", 1))
    for o in ({"clinic_id": C}, {"op": "get", "item_id": X})[:ops]
]


def _patients(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "patient":
                yield v
            yield from _patients(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _patients(v)


@pytest.mark.parametrize("tool,kwargs,sans", _APPELS,
                         ids=[f"{t}-{json.dumps(k, sort_keys=True)}" for t, k, _ in _APPELS])
def test_aucune_sentinelle_ne_sort(monkeypatch, tool, kwargs, sans):
    faux = _Empoisonne(sans)
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: faux)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    out = _tool(tool)(**kwargs)
    assert faux.appels, "l'outil n'a pas appelé le client"
    assert S not in json.dumps(out), json.dumps(out)[:600]
    assert all(p == {"id": X} for p in _patients(out))


def test_chaque_outil_est_couvert_par_le_poison():
    outils = {t.name for t in asyncio.run(_mcp().list_tools())}
    exemptes = {"nextmotion_practitioner",  # un praticien a un nom : il est servi
                # agrégats : leur poison vit dans test_nextmotion_analyse.py
                "nextmotion_patient_demographics", "nextmotion_device_usage",
                # ses lignes de soin arrivent RÉDUITES par la lib : poison dédié plus bas
                "nextmotion_package_usage"}
    assert outils - exemptes - {t for t, _, _ in _APPELS} == set()


# --- ce qui DOIT sortir --------------------------------------------------------------

def test_lignes_de_facture_completes_cote_commercial(client):
    sous = {"kind": "product", "name": "Sous-Test", "price": "1.00", "rebate": "0.00",
            "markup": "0.50", "vat_rate": "20.000", "vat_rate_frac": "0.2",
            "details": S, "distributed_to": 1, "accounting_code": "706100"}
    ligne = {"id": C, "created_time": "2026-01-01T00:00:00Z", "position": 1,
             "name": "Ligne-Test", "price": "10.00", "quantity": 2, "rebate": "0.00",
             "rebate_percent": "0", "markup": "1.00", "vat_rate": "20.000",
             "vat_price": "2.00", "vat_excl_price": "8.00", "details": S, "treatment": X,
             "subpricing": [sous]}
    client.get_invoice.return_value = {"data": {"id": X, "invoiced_treatments": [ligne]}}
    out = _tool("nextmotion_invoice")(op="get", invoice_id=X)["invoice"]
    attendu = {k: v for k, v in ligne.items() if k not in ("details", "treatment")}
    attendu["subpricing"] = [{k: v for k, v in sous.items() if k != "details"}]
    assert out["invoiced_treatments"] == [attendu]


def test_paiement_rend_ses_montants_et_sa_facture_projetee(client):
    client.get_payment.return_value = {"data": {
        "id": X, "card": "50.00", "cash": "0.00", "deferred": None,
        "custom_medium_list": [{"id": C, "name": "Chèque-cadeau", "amount": "5.00"}],
        "invoice": {"id": C, "number_id": "F-0001", "total_price": "55.00",
                    "patient": _patient()}}}
    out = _tool("nextmotion_payment")(op="get", payment_id=X)
    assert out["payment"]["card"] == "50.00" and out["payment"]["deferred"] is None
    assert out["payment"]["custom_medium_list"][0]["amount"] == "5.00"
    assert out["payment"]["invoice"] == {"id": C, "number_id": "F-0001",
                                         "total_price": "55.00", "patient": {"id": X}}
    assert "withheld" in out


def test_lead_garde_le_pipeline(client):
    label = {"id": C, "type": "lead_source", "name": "Source-Test", "color": "#000"}
    client.get_lead.return_value = {"data": {
        "id": X, "notes": S, "source": label, "desired_treatment": label,
        "follow_up_count": 3, "assigned_doctor": {"id": C, "prefixed_name": "Dr Test"}}}
    out = _tool("nextmotion_lead")(op="get", lead_id=X)
    assert out["lead"] == {"id": X, "source": label, "desired_treatment": label,
                           "follow_up_count": 3,
                           "assigned_doctor": {"id": C, "prefixed_name": "Dr Test"}}
    assert "withheld" in out


def test_statistiques_un_graphique_ou_plusieurs_toujours_une_liste(client):
    chart = {"title": "CA", "labels": ["2026-01"], "meta": {"k": S},
             "datasets": [{"data": [12], "color": "#111", "label": S}]}
    client.get_appointment_income_statistics.return_value = {"data": chart}
    client.list_treatment_type_statistics.return_value = {"data": [chart, chart]}
    one = _tool("nextmotion_statistics")(kind="appointment_income", clinic_id=C)
    many = _tool("nextmotion_statistics")(kind="treatment_types", clinic_id=C)
    propre = {"title": "CA", "labels": ["2026-01"], "datasets": [{"data": [12],
                                                                   "color": "#111"}]}
    assert one == {"kind": "appointment_income", "charts": [propre]}
    assert many["charts"] == [propre, propre]


def test_totaux_patient_sans_la_vie_du_dossier(client):
    client.get_patient_stats.return_value = {"data": {
        "invoiced_total": "100.00", "paid_total": "80.00", "last_visit_time": None,
        "media_event_count": 7, "last_opened_time": "2026-01-01T00:00:00Z"}}
    out = _tool("nextmotion_patient_stats")(patient_id=X)
    assert out["patient_stats"] == {"invoiced_total": "100.00", "paid_total": "80.00",
                                    "last_visit_time": None}


def test_une_feuille_ne_transporte_jamais_d_objet(client):
    client.list_appointment_rooms.return_value = {"count": 1, "next": None, "data": [
        {"id": X, "name": {"libre": S}, "visit_types": [C, {"libre": S}], "color": None}]}
    out = _tool("nextmotion_calendar")(kind="room", clinic_id=C)
    assert out["rooms"] == [{"id": X, "visit_types": [C], "color": None}]


def test_fields_sur_une_ressource_ajoutee(client):
    client.list_payments.return_value = {"count": 1, "next": "n", "data": [
        {"id": X, "card": "1.00", "cash": "2.00", "invoice": {"id": C, "patient": _patient()}}]}
    out = _tool("nextmotion_payment")(clinic_id=C, fields=["card"])
    assert out["payments"] == [{"id": X, "card": "1.00"}] and out["has_more"] is True



# --- le décompte d'un forfait (#148) : compteurs administratifs seulement -----------

F1 = "00000000-0000-4000-8000-0000000000f1"
F2 = "00000000-0000-4000-8000-0000000000f2"
T1 = "00000000-0000-4000-8000-0000000000a1"
P1 = "00000000-0000-4000-8000-0000000000b1"


def _forfait(client):
    client.list_appointments.return_value = {"count": 3, "next": None, "data": [
        {"id": "r1", "patient": {"id": X, "last_name": S}},
        {"id": "r2"}, {"id": "r3"}]}
    lignes = {"r1": [{"id": "s1", "invoice": F1, "quote": None, "treatment_type": T1,
                      "treatment_pricing": P1, "quantity": 1,
                      "treatment_time": "2026-09-01T09:00:00Z"}],
              "r2": [{"id": "s2", "invoice": F1, "quote": None, "treatment_type": T1,
                      "treatment_pricing": P1, "quantity": 1,
                      "treatment_time": "2026-09-15T09:00:00Z"}],
              "r3": [{"id": "s3", "invoice": F2, "quote": None, "treatment_type": T1,
                      "treatment_pricing": P1, "quantity": 1,
                      "treatment_time": "2026-09-20T09:00:00Z"}]}
    client.list_appointment_billed_counts.side_effect = (
        lambda i, limit, offset: {"count": 1, "next": None, "data": lignes[i]})
    client.get_treatment_type.return_value = {"data": {"id": T1, "name": "Laser",
                                                       "notes": S}}
    client.get_invoice.side_effect = lambda i: {"data": {
        "id": i, "number": f"FA-{i[-2:]}", "status": "paid", "issued_time": "2026-08-30",
        "patient": {"id": X, "first_name": S},
        "invoiced_treatments": [{"name": "Forfait laser 6 séances", "quantity": 6,
                                 "details": S}]}}


def test_le_decompte_d_un_forfait_par_facture(client):
    from oto.tools.nextmotion import consumed_by_billing  # noqa: F401 — exige le pin

    _forfait(client)
    out = _tool("nextmotion_package_usage")(clinic_id=C, patient_id=X)
    client.list_appointments.assert_called_once_with(C, patient_id=X, limit=100, offset=0)
    assert out["appointments_read"] == 3 and out["truncated"] is False
    assert [(g["invoice"], g["consumed"], g["treatment_type_name"])
            for g in out["consumed"]] == [(F1, 2, "Laser"), (F2, 1, "Laser")]
    assert out["sold"][F1] == {"kind": "invoice", "number": "FA-f1", "status": "paid",
                               "issued_time": "2026-08-30",
                               "lines": [{"name": "Forfait laser 6 séances",
                                          "quantity": 6}]}
    assert S not in json.dumps(out)
    client.get_quote.assert_not_called()


def test_le_decompte_dit_quand_il_s_arrete(client, monkeypatch):
    from oto_mcp.tools import nextmotion_ventes as V

    monkeypatch.setattr(V, "_USAGE_MAX_APPOINTMENTS", 2)
    _forfait(client)
    out = _tool("nextmotion_package_usage")(clinic_id=C, patient_id=X)
    assert out["appointments_read"] == 2 and out["truncated"] is True


@pytest.mark.parametrize("kwargs,motif", [
    ({"clinic_id": C, "patient_id": ""}, "patient_id"),
    ({"clinic_id": "pas-un-uuid", "patient_id": X}, "UUID"),
])
def test_le_decompte_refuse_avant_tout_appel(client, kwargs, motif):
    with pytest.raises(McpError, match=motif):
        _tool("nextmotion_package_usage")(**kwargs)
    client.list_appointments.assert_not_called()



class _EmpoisonneSaufDecompte(_Empoisonne):
    """Le poison partout, sauf les lignes de soin : la lib les rend réduites à ses ids et
    compteurs (`BILLED_COUNT_FIELDS`), et c'est ce contrat que l'outil consomme."""

    def list_appointment_billed_counts(self, appointment_id, limit, offset):
        self.appels.append("list_appointment_billed_counts")
        return {"count": 1, "next": None, "data": [
            {"id": C, "invoice": X, "quote": None, "treatment_type": C,
             "treatment_pricing": C, "quantity": 1, "treatment_time": "t",
             "notes": S, "zone": S, "text": S}]}


def test_le_decompte_ne_laisse_sortir_aucune_sentinelle(monkeypatch):
    faux = _EmpoisonneSaufDecompte()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: faux)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    out = _tool("nextmotion_package_usage")(clinic_id=C, patient_id=X)
    assert {"list_appointments", "list_appointment_billed_counts", "get_invoice"} <= set(
        faux.appels)
    assert S not in json.dumps(out), json.dumps(out)[:600]
    assert out["consumed"][0]["consumed"] == 1
