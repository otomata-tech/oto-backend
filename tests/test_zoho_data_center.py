"""Résolution de région Zoho (`data_center`) — plus de repli silencieux sur `com`.

Régression : un self-client `.eu` sans `data_center` (ou avec une valeur libre non
reconnue) retombait sur `accounts.zoho.com` → `invalid_client` opaque. Désormais on
lève une erreur actionnable ; `com` reste une région valide, aucune n'est forcée.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp.tools.zoho import _resolve_dc_domains, _DC_DOMAINS


@pytest.mark.parametrize("dc", list(_DC_DOMAINS))
def test_each_region_maps_to_its_domains(dc):
    api_domain, accounts_url = _resolve_dc_domains(dc)
    assert (api_domain, accounts_url) == _DC_DOMAINS[dc]


def test_com_is_valid_not_forced_to_eu():
    assert _resolve_dc_domains("com") == (
        "https://www.zohoapis.com", "https://accounts.zoho.com")


def test_normalizes_case_and_whitespace():
    assert _resolve_dc_domains("  EU ") == _DC_DOMAINS["eu"]


@pytest.mark.parametrize("bad", [None, "", "   ", "europe", "eu.zoho.com", "us"])
def test_missing_or_unknown_region_raises_not_falls_back_to_com(bad):
    with pytest.raises(McpError):
        _resolve_dc_domains(bad)
