"""Rechargement à chaud du registre d'émetteurs (ADR 0052 B4, moitié « prise d'effet »).

Le provisionnement d'un tenant reste un runbook (instance d'annuaire, client OAuth,
hosts, ligne `tenants`) — mais sa prise d'effet exigeait un REDÉMARRAGE : le registre
est construit au boot, d'où le verdict `pending_restart` de l'écran de suivi.
`server.reload_tenant_registry()` fait relire les déclarations au process qui tourne,
par DEUX swaps de référence (atomiques — aucun lecteur ne voit un état intermédiaire) :
le registre installé (`tenancy.install`) et les émetteurs acceptés du verifier vivant
(`_VERIFIER._by_issuer`).

Ce que ces tests garantissent, dans l'ordre de ce qui coûterait cher :

1. **Un échec ne laisse RIEN à moitié posé.** Base illisible ⟹ l'exception remonte
   et le process garde le registre d'avant, entier — jamais un registre vide qui
   couperait l'authentification de tous les tenants tiers d'un coup.
2. **Le reload prend effet aux deux étages** : la classification (`tenancy.current`)
   ET la vérification (`_by_issuer`) — c'est le second qui éteint `pending_restart`.
3. **Sans serveur construit** (script, test), le reload ne plante pas : le registre
   bouge, le rapport dit `verifier_updated=False`.
"""
from __future__ import annotations

import pytest

from oto_mcp import server, tenancy


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.delenv("LOGTO_ENDPOINT_ALT", raising=False)
    # Le préfixe d'outils du primaire n'est pas l'objet ici : sans base, il lèverait
    # (`PrefixeOutilsIndetermine`), ce que `test_tool_alias_par_tenant` couvre.
    monkeypatch.setattr(tenancy, "load_primary_tool_prefix", lambda: "")


@pytest.fixture
def registre_installe():
    avant = tenancy.current()
    yield
    tenancy.install(avant)


def _tenant_row(slug="acme"):
    return {"slug": slug, "name": slug.title(),
            "issuer": f"https://auth.{slug}.test/oidc"}


def test_reload_swaps_registry_and_live_verifier(env, registre_installe, monkeypatch):
    monkeypatch.setattr(tenancy, "load_tenants", lambda: [_tenant_row("acme")])
    faux = type("V", (), {"_by_issuer": {}})()
    monkeypatch.setattr(server, "_VERIFIER", faux)

    rapport = server.reload_tenant_registry()

    assert rapport["verifier_updated"] is True
    assert "acme" in rapport["tenants"] and "oto" in rapport["tenants"]
    # Étage 1 : la classification du process voit le tenant neuf.
    assert tenancy.current().tenant_of("acme:u-1") == "acme"
    # Étage 2 : le verifier vivant accepte son émetteur — c'est le swap qui éteint
    # `pending_restart` (avant lui, les jetons du tenant étaient encore rejetés).
    slugs = {slug for slug, _ in faux._by_issuer.values()}
    assert "acme" in slugs and "oto" in slugs
    tiers = faux._by_issuer["https://auth.acme.test/oidc"]
    assert tiers[0] == "acme" and tiers[1] is not None, \
        "un tenant tiers reçoit SON verifier (le primaire, c'est nous → None)"


def test_reload_failure_keeps_the_previous_registry_whole(env, registre_installe,
                                                          monkeypatch):
    monkeypatch.setattr(tenancy, "load_tenants", lambda: [_tenant_row("acme")])
    server.reload_tenant_registry()
    assert tenancy.current().tenant_of("acme:u-1") == "acme"

    def boom():
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(tenancy, "load_tenants", boom)
    with pytest.raises(RuntimeError, match="base injoignable"):
        server.reload_tenant_registry()
    # Le registre d'AVANT l'échec est intact — acme authentifie toujours.
    assert tenancy.current().tenant_of("acme:u-1") == "acme"


def test_reload_without_a_built_server_reports_it(env, registre_installe, monkeypatch):
    monkeypatch.setattr(tenancy, "load_tenants", lambda: [])
    monkeypatch.setattr(server, "_VERIFIER", None)
    rapport = server.reload_tenant_registry()
    assert rapport["verifier_updated"] is False
    assert rapport["tenants"] == ["oto"]


def test_console_op_reload_routes_to_the_server(monkeypatch):
    """La face capacité : `oto_admin_tenant op=reload` (et POST
    /api/admin/tenants/reload) appellent LE geste du serveur, et rendent son rapport
    sous l'enveloppe op-aware."""
    from oto_mcp.capabilities import tenants_admin as ta
    from oto_mcp.capabilities._types import ResolvedCtx

    appels = []
    monkeypatch.setattr(
        server, "reload_tenant_registry",
        lambda: appels.append(1) or {"tenants": ["oto"], "issuers": 1,
                                     "verifier_updated": True})
    out = ta._console(ResolvedCtx(sub="op", role="super_admin"),
                      ta.TenantConsoleInput(op="reload"))
    assert appels == [1]
    assert out["reload"]["reloaded"] is True
    assert out["reload"]["verifier_updated"] is True
