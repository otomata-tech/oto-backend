"""Cleaning auto des champs credential à la pose (ADR 0011).

Régression Zoho (03/07) : un refresh token collé avec un espace/retour-ligne au
milieu (`strip()` ne touche que les bords) → token corrompu → `invalid_code`.
`clean_field_value` retire tout whitespace des champs clés/tokens/ids, mais préserve
l'espace interne d'un mot de passe (whitespace_significant).
"""
from oto_mcp.credentials_store import clean_field_value
from oto_mcp.providers import CredentialField


def test_strips_internal_whitespace_for_tokens():
    f = CredentialField("refresh_token", "Refresh Token", secret=True)
    assert clean_field_value(f, "1000.ab cd\n ef") == "1000.abcdef"


def test_strips_all_whitespace_kinds_and_edges():
    f = CredentialField("key", "API key")
    assert clean_field_value(f, "  a\tb\r\nc ") == "abc"


def test_password_keeps_internal_space_strips_edges():
    f = CredentialField("password", "Mot de passe", secret=True,
                        whitespace_significant=True)
    assert clean_field_value(f, "  hunter 2  ") == "hunter 2"


def test_non_string_passthrough():
    f = CredentialField("key", "API key")
    assert clean_field_value(f, None) is None


def test_zoho_password_field_flagged_significant():
    # le password dérivé de basic_auth doit être whitespace-significatif
    from oto_mcp.providers import REGISTRY
    for c in REGISTRY.values():
        if c.secret_kind == "basic_auth":
            pwd = [f for f in c.secret_fields if f.name == "password"]
            assert pwd and pwd[0].whitespace_significant is True
            break
