"""Testabilité des outils depuis le dashboard + override d'identité REST.

- `is_testable` borne le bouton « tester » aux connecteurs open-data en lecture
  seule (FOD & co) — jamais un outil à effet de bord (email, write, messagerie).
- `sub_override` fait résoudre la bonne identité côté REST le temps d'un appel.
"""
from oto_mcp import auth_hooks
from oto_mcp.tool_visibility import TESTABLE_NAMESPACES, is_testable, namespace_of


def test_testable_covers_fod_readonly():
    for name in ("fr_get", "fr_search", "fr_siret", "fr_stock_search",
                 "foncier_geocode", "foncier_parcelle", "urba_zonage",
                 "sante_finess", "frenchtech_membres", "culture_spectacle_search",
                 "infosec_dns"):
        assert is_testable(name), name


def test_not_testable_side_effect_and_apps():
    # Effet de bord / mutation / coût — jamais testables via un simple bouton.
    for name in ("email_send", "data_write", "data_delete_row", "folk_create_person",
                 "whatsapp_send_message", "pennylane_create_credit_note",
                 "serper_web_search", "oto_use_org"):
        assert not is_testable(name), name
    # Les MCP Apps renvoient un composant d'UI, pas du JSON → exclues.
    assert not is_testable("foncier_site_app")
    assert not is_testable("foncier_comparables_app")


def test_testable_namespaces_are_readonly_only():
    # Garde-fou : aucun namespace à effet de bord ne se glisse dans l'allowlist.
    for ns in ("data", "email", "folk", "whatsapp", "pennylane", "slack", "gmail"):
        assert ns not in TESTABLE_NAMESPACES


def test_sub_override_sets_identity():
    assert auth_hooks.current_user_sub_from_token() is None
    with auth_hooks.sub_override("user-42"):
        assert auth_hooks.current_user_sub_from_token() == "user-42"
        assert namespace_of("fr_get") == "fr"
    assert auth_hooks.current_user_sub_from_token() is None
