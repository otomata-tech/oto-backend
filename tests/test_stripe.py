"""Connecteur Stripe — paiements & facturation (api.stripe.com v1).

Verrouille : l'entrée de registre (credential à 3 champs, byo-only, catégorie
Finance, éditeur Stripe), la doc how-to, la surface MCP (9 tools, chacun avec
une description — régression du piège f-string-docstring), la sonde « tester la
connexion », la jointure tool↔client oto-core (garde version-skew), le dispatch
`op=` (required manquant refusé, arg non pertinent refusé), et surtout les deux
propriétés qui font la sûreté de ce connecteur : **rien ne déplace d'argent**,
et **l'agrégat dit quand il est incomplet**.
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import stripe

EXPECTED_TOOLS = {
    "stripe_search", "stripe_customer", "stripe_subscription", "stripe_invoice",
    "stripe_payment", "stripe_balance", "stripe_catalog", "stripe_checkout",
    "stripe_event",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_credential(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {"api_key": "rk_test_x", "api_version": "",
                                        "stripe_account": ""})


def _fn_with_mock_client():
    """Enregistre le module avec `StripeClient` mocké, DANS le patch (sinon le
    `from … import StripeClient` de `register()` capture la vraie classe avant
    que le patch ne s'applique)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.stripe.client.StripeClient")
    cls = patcher.start()
    m = FastMCP("t")
    stripe.register(m)
    return m, cls, patcher


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registre -----------------------------------------------------------------

def test_stripe_is_a_byo_only_multi_field_connector():
    c = providers.REGISTRY["stripe"]
    assert c.kind == "tools"
    assert c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes, "jamais de clé Stripe partagée entre orgs"
    assert c.default_active is False
    assert c.default_quota == 0
    # secret_kind="fields" ⇒ résolu par resolve_credential_fields, pas resolve_api_key
    assert "stripe" in providers.CREDENTIAL_PROVIDERS
    assert "stripe" not in providers.KEY_PROVIDERS


def test_stripe_credential_fields_separate_the_secret_from_what_it_reads():
    c = providers.REGISTRY["stripe"]
    assert [f.name for f in c.secret_fields] == ["api_key", "api_version", "stripe_account"]
    # api_version et stripe_account sont NON secrets ⇒ ils deviennent des config_fields
    # et voyagent avec la clé gagnante via resolve_credential().config.
    assert [f.name for f in c.config_fields] == ["api_version", "stripe_account"]
    assert c.secret_fields[0].secret is True
    assert all(not f.required for f in c.config_fields), \
        "les deux satellites sont facultatifs"


def test_stripe_connect_account_is_a_credential_field_not_a_tool_parameter(all_tools):
    """Avec Connect, la MÊME question rend le CA d'une autre société selon cet
    en-tête. Le poser au credential le rend impossible à basculer en cours de
    conversation — s'il devenait un paramètre d'outil, cette propriété tombe."""
    assert "stripe_account" in {f.name for f in providers.REGISTRY["stripe"].config_fields}
    for name in EXPECTED_TOOLS:
        params = set(all_tools[name].parameters.get("properties", {}))
        assert "stripe_account" not in params, f"{name} expose stripe_account en paramètre"


def test_stripe_catalogue_overlays_are_curated():
    c = providers.REGISTRY["stripe"]
    assert c.category == "Finance"
    # Sans entrée d'éditeur, le catalogue annoncerait « Otomata » comme auteur de
    # l'API Stripe — chez les clients d'un tenant tiers, c'est notre marque qui
    # revendiquerait le travail d'un autre.
    assert c.publisher_name == "Stripe"
    assert c.label == "Stripe"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["stripe"] == "stripe.com"
    assert "stripe" not in providers._SANS_LOGO_DE_MARQUE


def test_stripe_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["stripe"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_stripe_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "stripe" for t in all_tools if t.startswith("stripe_"))


def test_stripe_exposes_exactly_the_expected_tools(all_tools):
    assert {t for t in all_tools if t.startswith("stripe_")} == EXPECTED_TOOLS


def test_stripe_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    # ⚠️ le patch DOIT être arrêté : sans ça la classe cliente reste un
    # MagicMock pour les tests suivants, et `hasattr(mock, "…")` étant
    # toujours vrai, les tests d'absence ne prouveraient plus rien.
    m, cls, patcher = _fn_with_mock_client()
    try:
        assert connector_verify.supports("stripe")
    finally:
        patcher.stop()


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.stripe.client import StripeClient
    for meth in ("search", "balance", "list_balance_transactions",
                 "get_balance_transaction", "list_payouts", "get_payout",
                 "list_customers", "get_customer", "create_customer",
                 "update_customer", "list_customer_payment_methods",
                 "list_subscriptions", "get_subscription", "list_subscription_items",
                 "list_invoices", "get_invoice", "get_invoice_lines",
                 "create_invoice", "update_invoice", "list_invoice_items",
                 "create_invoice_item", "list_payment_intents", "get_payment_intent",
                 "list_charges", "get_charge", "list_refunds", "get_refund",
                 "list_disputes", "get_dispute", "list_products", "get_product",
                 "create_product", "update_product", "list_prices", "get_price",
                 "create_price", "update_price", "list_coupons", "get_coupon",
                 "create_coupon", "update_coupon", "list_promotion_codes",
                 "get_promotion_code", "create_promotion_code", "update_promotion_code",
                 "list_payment_links", "get_payment_link",
                 "get_payment_link_line_items", "create_payment_link",
                 "update_payment_link", "list_checkout_sessions",
                 "get_checkout_session", "get_checkout_session_line_items",
                 "list_events", "get_event", "list_webhook_endpoints"):
        assert callable(getattr(StripeClient, meth, None)), f"StripeClient.{meth} manquant"


def test_no_money_moving_verb_is_reachable_from_this_module():
    """La propriété centrale du connecteur : ce n'est pas une politique de ce
    fichier — les méthodes n'existent pas sur le client, donc les rebrancher
    demande une PR oto-core, pas une ligne ici."""
    from oto.tools.stripe.client import StripeClient
    for absent in ("create_refund", "create_payout", "cancel_payout",
                   "reverse_payout", "close_dispute", "cancel_subscription",
                   "finalize_invoice", "pay_invoice", "void_invoice",
                   "delete_customer", "capture_charge", "create_charge"):
        assert not hasattr(StripeClient, absent), f"StripeClient.{absent} est réapparu"


# --- dispatch op= (comportement) ----------------------------------------------

def test_limit_defaults_to_100_because_stripe_silently_truncates_to_10():
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_customer")(op="list")
        assert cls.return_value.list_customers.call_args.kwargs["limit"] == 100
    finally:
        patcher.stop()


def test_limit_out_of_range_is_refused():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="between 1 and 100"):
            _tool(m, "stripe_customer")(op="list", limit=500)
    finally:
        patcher.stop()


def test_created_window_becomes_stripes_operator_dict():
    """La fenêtre de Stripe est `created[gte]`/`created[lt]`, pas deux champs."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_invoice")(op="list", created_after=1704067200,
                                   created_before=1706745600)
        assert cls.return_value.list_invoices.call_args.kwargs["created"] == {
            "gte": 1704067200, "lt": 1706745600}
    finally:
        patcher.stop()


def test_customer_get_requires_id_and_refuses_list_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_customer")
        with pytest.raises(McpError, match="requires .customer_id."):
            fn(op="get")
        with pytest.raises(McpError, match="op='get' does not use"):
            fn(op="get", customer_id="cus_1", created_after=1)
        cls.return_value.get_customer.assert_not_called()

        cls.return_value.get_customer.return_value = {"id": "cus_1"}
        assert fn(op="get", customer_id="cus_1") == {"id": "cus_1"}
    finally:
        patcher.stop()


def test_customer_create_requires_something_findable():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_customer")
        with pytest.raises(McpError, match="at least `email` or `name`"):
            fn(op="create")
        fn(op="create", email="a@b.co")
        cls.return_value.create_customer.assert_called_once()
    finally:
        patcher.stop()


def test_subscription_list_refuses_a_subscription_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="op='list' does not use"):
            _tool(m, "stripe_subscription")(op="list", subscription_id="sub_1")
        cls.return_value.list_subscriptions.assert_not_called()
    finally:
        patcher.stop()


def test_draft_invoice_pulls_pending_items_by_default():
    """Vérifié en live : sans « include », la facture revient à total=0 avec zéro
    ligne alors que des lignes viennent d'être posées — un « facture créée »
    faux, et le montant reste en suspens."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_invoice")(op="create_draft", customer_id="cus_1")
        kwargs = cls.return_value.create_invoice.call_args.kwargs
        assert kwargs["pending_invoice_items_behavior"] == "include"
        assert kwargs["auto_advance"] is False, "un brouillon ne se finalise pas tout seul"
    finally:
        patcher.stop()


def test_draft_invoice_can_still_be_left_empty_on_request():
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_invoice")(op="create_draft", customer_id="cus_1",
                                   pending_items="exclude")
        assert cls.return_value.create_invoice.call_args.kwargs[
            "pending_invoice_items_behavior"] == "exclude"
    finally:
        patcher.stop()


def test_add_item_requires_amount_and_currency():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_invoice")
        with pytest.raises(McpError, match="requires `customer_id`"):
            fn(op="add_item", amount=1000, currency="eur")
        with pytest.raises(McpError, match="`amount`.*`currency`"):
            fn(op="add_item", customer_id="cus_1")
        fn(op="add_item", customer_id="cus_1", amount=-2000, currency="eur")
        assert cls.return_value.create_invoice_item.call_args.kwargs["amount"] == -2000
    finally:
        patcher.stop()


# --- l'agrégat : la seule réponse honnête à « combien a-t-on facturé » ---------

def _invoice(amount, currency="eur", status="paid", id_="in_x"):
    return {"id": id_, "currency": currency, "total": amount,
            "amount_paid": amount, "amount_due": 0, "status": status}


def test_totals_sums_per_currency_and_never_across_them():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.list_invoices.return_value = {
            "data": [_invoice(1000, "eur", id_="in_1"),
                     _invoice(2000, "eur", id_="in_2"),
                     _invoice(500, "usd", id_="in_3")],
            "has_more": False}
        out = _tool(m, "stripe_invoice")(op="totals")
        assert out["count"] == 3
        assert out["complete"] is True
        assert out["by_currency"]["eur"]["total"] == 3000
        assert out["by_currency"]["usd"]["total"] == 500
        assert "total" not in out, "aucune somme toutes devises confondues"
        assert out["count_by_status"] == {"paid": 3}
    finally:
        patcher.stop()


def test_totals_follows_the_cursor_across_pages():
    m, cls, patcher = _fn_with_mock_client()
    try:
        pages = [
            {"data": [_invoice(100, id_="in_1")], "has_more": True},
            {"data": [_invoice(200, id_="in_2")], "has_more": False},
        ]
        cls.return_value.list_invoices.side_effect = pages
        out = _tool(m, "stripe_invoice")(op="totals")
        assert out["count"] == 2
        assert out["by_currency"]["eur"]["total"] == 300
        # la 2e page part APRÈS le dernier id de la 1re
        assert cls.return_value.list_invoices.call_args_list[1].kwargs[
            "starting_after"] == "in_1"
    finally:
        patcher.stop()


def test_totals_declares_itself_incomplete_rather_than_lying():
    """Une somme partielle présentée comme le chiffre d'affaires est le pire
    résultat possible : le plafond doit être VISIBLE dans la réponse."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.list_invoices.return_value = {
            "data": [_invoice(100, id_="in_1")], "has_more": True}
        out = _tool(m, "stripe_invoice")(op="totals")
        assert out["complete"] is False
        assert "INCOMPLETE" in out["note"]
        assert cls.return_value.list_invoices.call_count == stripe._AGGREGATE_MAX_PAGES
    finally:
        patcher.stop()


def test_totals_refuses_write_and_targeting_fields():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="op='totals' does not use"):
            _tool(m, "stripe_invoice")(op="totals", invoice_id="in_1")
    finally:
        patcher.stop()


# --- recherche ----------------------------------------------------------------

def test_search_passes_the_query_through():
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_search")(resource="customers", query='email~"acme.com"')
        cls.return_value.search.assert_called_once_with(
            "customers", 'email~"acme.com"', limit=100, page=None)
    finally:
        patcher.stop()


# --- catalogue : l'immuabilité du montant d'un prix ----------------------------

def test_update_price_refuses_a_new_amount_and_says_what_to_do():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="immutable"):
            _tool(m, "stripe_catalog")(op="update_price", price_id="price_1",
                                       unit_amount=2500)
        cls.return_value.update_price.assert_not_called()
    finally:
        patcher.stop()


def test_create_price_requires_the_three_fields_stripe_needs():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `product_id`"):
            _tool(m, "stripe_catalog")(op="create_price", unit_amount=1900)
        _tool(m, "stripe_catalog")(op="create_price", product_id="prod_1",
                                   unit_amount=1900, currency="eur",
                                   recurring_interval="month")
        assert cls.return_value.create_price.call_args.kwargs["recurring"] == {
            "interval": "month"}
    finally:
        patcher.stop()


# --- catalogue : coupons & promotion codes -------------------------------------

def test_create_coupon_requires_duration_and_exactly_one_discount_kind():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="requires `duration`"):
            fn(op="create_coupon", percent_off=20)
        with pytest.raises(McpError, match="`percent_off` OR `amount_off`"):
            fn(op="create_coupon", duration="once")
        with pytest.raises(McpError, match="`percent_off` OR `amount_off`"):
            fn(op="create_coupon", duration="once", percent_off=20, amount_off=500,
               currency="eur")
        cls.return_value.create_coupon.assert_not_called()

        fn(op="create_coupon", duration="once", percent_off=20, name="LAUNCH20")
        assert cls.return_value.create_coupon.call_args.kwargs == {
            "percent_off": 20, "duration": "once", "name": "LAUNCH20"}
    finally:
        patcher.stop()


def test_create_coupon_amount_off_requires_currency():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="also requires `currency`"):
            fn(op="create_coupon", duration="once", amount_off=500)
        fn(op="create_coupon", duration="once", amount_off=500, currency="eur")
        assert cls.return_value.create_coupon.call_args.kwargs["currency"] == "eur"
    finally:
        patcher.stop()


def test_create_coupon_repeating_requires_duration_in_months():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="duration_in_months"):
            fn(op="create_coupon", duration="repeating", percent_off=10)
        fn(op="create_coupon", duration="repeating", percent_off=10, duration_in_months=3)
        cls.return_value.create_coupon.assert_called_once()
    finally:
        patcher.stop()


def test_update_coupon_only_accepts_name_and_metadata():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="requires `coupon_id`"):
            fn(op="update_coupon", name="x")
        with pytest.raises(McpError, match="nothing else that can be modified"):
            fn(op="update_coupon", coupon_id="cp_1")
        fn(op="update_coupon", coupon_id="cp_1", name="renamed")
        cls.return_value.update_coupon.assert_called_once_with("cp_1", name="renamed")
    finally:
        patcher.stop()


def test_create_promotion_code_requires_a_coupon():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="requires `coupon_id`"):
            fn(op="create_promotion_code", code="LAUNCH20")
        fn(op="create_promotion_code", coupon_id="cp_1", code="LAUNCH20",
           restrictions={"first_time_transaction": True})
        assert cls.return_value.create_promotion_code.call_args.kwargs == {
            "coupon": "cp_1", "code": "LAUNCH20",
            "restrictions": {"first_time_transaction": True}}
    finally:
        patcher.stop()


def test_create_coupon_refuses_a_stray_promotion_code_text():
    """`code` est le texte d'une PROMOTION CODE, pas d'un coupon — le confondre
    ferait croire à tort que le coupon lui-même porte ce texte."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="op='create_coupon' does not use"):
            _tool(m, "stripe_catalog")(op="create_coupon", duration="once",
                                       percent_off=20, code="LAUNCH20")
        cls.return_value.create_coupon.assert_not_called()
    finally:
        patcher.stop()


def test_create_promotion_code_refuses_a_stray_active_flag():
    """Un code neuf est actif par défaut chez Stripe — `active` n'existe qu'en
    modification (`update_promotion_code`), pas à la création."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="op='create_promotion_code' does not use"):
            _tool(m, "stripe_catalog")(op="create_promotion_code", coupon_id="cp_1",
                                       active=False)
        cls.return_value.create_promotion_code.assert_not_called()
    finally:
        patcher.stop()


def test_list_promotion_codes_filters_by_coupon():
    """Un `coupon_id` fourni mais silencieusement ignoré rendrait TOUS les
    codes du compte en laissant croire qu'ils appartiennent à ce coupon —
    même piège que `stripe_invoice(op='list', invoice_id=…)`."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        _tool(m, "stripe_catalog")(op="list_promotion_codes", coupon_id="cp_1")
        assert cls.return_value.list_promotion_codes.call_args.kwargs["coupon"] == "cp_1"
    finally:
        patcher.stop()


def test_update_promotion_code_can_deactivate():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_catalog")
        with pytest.raises(McpError, match="requires `promotion_code_id`"):
            fn(op="update_promotion_code", active=False)
        with pytest.raises(McpError, match="requires .active. or .metadata."):
            fn(op="update_promotion_code", promotion_code_id="promo_1")
        fn(op="update_promotion_code", promotion_code_id="promo_1", active=False)
        cls.return_value.update_promotion_code.assert_called_once_with(
            "promo_1", active=False)
    finally:
        patcher.stop()


def test_checkout_create_link_requires_a_price():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `price_id`"):
            _tool(m, "stripe_checkout")(op="create_link")
        _tool(m, "stripe_checkout")(op="create_link", price_id="price_1", quantity=3)
        cls.return_value.create_payment_link.assert_called_once()
        items = cls.return_value.create_payment_link.call_args[0][0]
        assert items == [{"price": "price_1", "quantity": 3}]
    finally:
        patcher.stop()


def _link_body(ptype="one_time", **kw):
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.get_price.return_value = {"type": ptype}
        _tool(m, "stripe_checkout")(op="create_link", price_id="price_1", **kw)
        return cls.return_value.create_payment_link.call_args[1], cls.return_value
    finally:
        patcher.stop()


def test_create_link_recurring_puts_created_metadata_on_subscription():
    body, client = _link_body(ptype="recurring", metadata={"campagne": "rentree"},
                              created_metadata={"account_id": "7"}, max_uses=1)
    assert body["metadata"] == {"campagne": "rentree"}
    assert body["subscription_data"] == {"metadata": {"account_id": "7"}}
    assert body["restrictions"] == {"completed_sessions": {"limit": 1}}
    assert "payment_intent_data" not in body
    client.get_price.assert_called_once_with("price_1")


def test_create_link_one_time_uses_payment_intent_data():
    body, _ = _link_body(ptype="one_time", created_metadata={"account_id": "7"})
    assert body["payment_intent_data"] == {"metadata": {"account_id": "7"}}
    assert "subscription_data" not in body


def test_create_link_recurring_price_needs_no_metadata():
    """Un lien d'abonnement sans métadonnée reste permis, et ne lit pas le prix
    (la clé n'a peut-être pas Prices: Read)."""
    body, client = _link_body(ptype="recurring")
    assert body == {}
    client.get_price.assert_not_called()


def test_create_link_managed_payments_is_sent_only_when_asked():
    """Omitted = the account's own default (nothing sent); `false` refuses
    Stripe as merchant of record explicitly, `true` asks for it."""
    body, _ = _link_body(ptype="recurring")
    assert "managed_payments" not in body
    body, _ = _link_body(ptype="recurring", managed_payments=False)
    assert body["managed_payments"] == {"enabled": False}
    body, _ = _link_body(ptype="recurring", managed_payments=True)
    assert body["managed_payments"] == {"enabled": True}


def test_managed_payments_is_refused_outside_create_link():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="managed_payments"):
            _tool(m, "stripe_checkout")(op="list_links", managed_payments=False)
    finally:
        patcher.stop()


def test_create_link_refuses_an_unreadable_price_type():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "stripe_checkout")
        for price in ({"id": "price_1"}, ["price_1"], None):
            cls.return_value.get_price.return_value = price
            with pytest.raises(McpError, match="unreadable type"):
                fn(op="create_link", price_id="price_1", created_metadata={"a": "1"})
        cls.return_value.create_payment_link.assert_not_called()
    finally:
        patcher.stop()


def test_create_link_surfaces_a_failed_price_read():
    m, cls, patcher = _fn_with_mock_client()
    try:
        from oto.tools.common.errors import UpstreamHTTPError
        cls.return_value.get_price.side_effect = UpstreamHTTPError(
            403, {"error": {"message": "no perms"}}, service="stripe")
        with pytest.raises(McpError, match="RESTRICTED"):
            _tool(m, "stripe_checkout")(op="create_link", price_id="price_1",
                                        created_metadata={"a": "1"})
        cls.return_value.create_payment_link.assert_not_called()
    finally:
        patcher.stop()


@pytest.mark.parametrize("max_uses", [0, -1, True])
def test_create_link_refuses_a_bad_max_uses(max_uses):
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="max_uses"):
            _tool(m, "stripe_checkout")(op="create_link", price_id="price_1",
                                        max_uses=max_uses)
        cls.return_value.create_payment_link.assert_not_called()
    finally:
        patcher.stop()


@pytest.mark.parametrize("kw", [{"max_uses": 1}, {"created_metadata": {"a": "1"}},
                                {"price_id": "price_1"}, {"quantity": 2}])
def test_create_link_fields_are_refused_on_other_ops(kw):
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="only apply to op='create_link'"):
            _tool(m, "stripe_checkout")(op="update_link", payment_link_id="plink_1",
                                        active=False, **kw)
        cls.return_value.update_payment_link.assert_not_called()
    finally:
        patcher.stop()


# --- messages d'erreur : actionnables, pas génériques --------------------------

def test_missing_tax_code_error_tells_the_agent_how_to_fix_it():
    """Vécu en live : `create_link` rend un 400 « the product tax code is
    missing » sur un compte éligible aux paiements gérés. Le message doit porter
    le correctif, sinon l'agent abandonne."""
    from oto.tools.common.errors import UpstreamHTTPError
    msg = stripe._upstream_message(UpstreamHTTPError(400, {
        "error": {"message": "Invalid line_items[0]: the product tax code is missing.",
                  "code": None}}, service="stripe"))
    assert "tax_code" in msg and "update_product" in msg


def test_auth_error_mentions_restricted_key_permissions():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = stripe._upstream_message(UpstreamHTTPError(403, {
        "error": {"message": "no perms"}, "request_id": "req_1"}, service="stripe"))
    assert "RESTRICTED" in msg
    assert "req_1" in msg, "le request_id est ce que le support Stripe demande en premier"


def test_not_found_error_mentions_the_test_live_split():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = stripe._upstream_message(UpstreamHTTPError(404, {
        "error": {"message": "No such customer"}}, service="stripe"))
    assert "MODE" in msg
