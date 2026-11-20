"""Un tenant DÉCLARÉ et NON CHARGÉ doit réveiller quelqu'un (préprod, 2026-09-07).

Ce soir-là, « `…/oidc` réclamé par le tenant 'X' alors qu'il est déjà tenu par 'oto'
— ligne ignorée » est parti dans le journal système, **sans destinataire** : personne
ne l'a vu, et le tenant est resté non chargé toute la journée (ses jetons routent vers
le verifier primaire, qui les rejette — le partenaire est injoignable sans qu'aucune
requête n'échoue chez nous). Le refus est bon ; ce qui manquait est un destinataire.

Ce que ces tests garantissent, dans l'ordre de ce qui coûterait cher :

1. **C'est le chemin de BOOT qui alerte** — `server._build_verifier()`, celui qui a
   tourné en préprod, pas `tenancy.build` convoqué directement.
2. **L'alerte ARRIVE.** Le banc pose un client Sentry réel dont le transport est une
   liste, et le `before_send` de production (`sentry_setup._before_send`, qui jette
   99 % des events construits). Une alerte émise puis droppée deux gardes plus loin
   ne réveillerait personne, et un banc qui n'assert que l'appel ne le verrait pas.
3. **Le nominal reste MUET** : deux tenants distincts, zéro event — sans quoi le
   premier test serait vert quoi qu'il arrive.
"""
from __future__ import annotations

import logging

import pytest
import sentry_sdk

from oto_mcp import sentry_setup, server, tenancy

_PRIMAIRE = "https://auth.oto.ninja/oidc"
_TIERS = "https://auth.acme.test/oidc"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.setenv("MCP_AUDIENCE", "https://mcp.oto.ninja/mcp")
    monkeypatch.delenv("LOGTO_ENDPOINT_ALT", raising=False)
    monkeypatch.delenv("MCP_AUDIENCE_ALT", raising=False)
    # Le boot pose le verifier vivant du process : monkeypatch le rend au sortir.
    monkeypatch.setattr(server, "_VERIFIER", None)
    # Le préfixe d'outils du primaire n'est pas l'objet ici : sans base, il lèverait
    # (`PrefixeOutilsIndetermine`), ce que `test_tool_alias_par_tenant` couvre.
    monkeypatch.setattr(tenancy, "load_primary_tool_prefix", lambda: "")


@pytest.fixture
def registre_installe():
    """`_build_verifier` installe le registre construit — on rend celui d'avant."""
    avant = tenancy.current()
    yield
    tenancy.install(avant)


@pytest.fixture
def alertes():
    """Sentry RÉEL, transport = une liste, `before_send` de production.

    Pas de réseau (transport fonction), pas d'intégration installée : ce qu'on
    éprouve, c'est la chaîne `capture_message` → `_before_send` → transport, la seule
    qui décide si quelqu'un est réveillé.
    """
    recu: list = []
    client = sentry_sdk.Client(dsn="https://cle@sentry.invalid/1", transport=recu.append,
                               before_send=sentry_setup._before_send,
                               default_integrations=False)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        yield recu


def _ligne(slug, issuer):
    return {"slug": slug, "name": slug.title(), "issuer": issuer}


def test_un_emetteur_deja_tenu_alerte_au_boot(env, registre_installe, alertes,
                                              monkeypatch, caplog):
    """Le cas de la préprod : une ligne réclame l'émetteur primaire (tenu par `oto`)."""
    monkeypatch.setattr(tenancy, "load_tenants",
                        lambda: [_ligne("acme", _PRIMAIRE)])
    with caplog.at_level(logging.WARNING, logger="oto_mcp.tenancy"):
        server._build_verifier()

    # Le tenant n'est PAS chargé — c'est ce que l'alerte doit dire.
    assert tenancy.current().get(_PRIMAIRE).slug == tenancy.primary_slug()

    assert len(alertes) == 1, [e.get("message") for e in alertes]
    event = alertes[0]
    assert event["level"] == "error"
    assert event["tags"]["oto.registre_tenants"] == "refus"
    # Le texte nomme le tenant et l'émetteur : sans eux, l'alerte ne dit pas QUI est
    # resté à quai, et il faut retourner au journal système — le détour qu'on ferme.
    assert "acme" in event["message"] and _PRIMAIRE in event["message"]
    # Le journal garde sa ligne : on ajoute un destinataire, on ne déplace rien.
    assert any("acme" in r.getMessage() for r in caplog.records)


def test_un_slug_refuse_alerte_aussi(env, registre_installe, alertes, monkeypatch):
    """Même classe, autre refus : un slug qui entrerait dans le sub est écarté."""
    monkeypatch.setattr(tenancy, "load_tenants",
                        lambda: [_ligne("Acme Corp", _TIERS)])
    server._build_verifier()

    assert tenancy.current().get(_TIERS) is None
    assert len(alertes) == 1, [e.get("message") for e in alertes]
    assert "Acme Corp" in alertes[0]["message"]


def test_deux_tenants_distincts_ne_reveillent_personne(env, registre_installe,
                                                       alertes, monkeypatch):
    """Le contrôle qui empêche le banc d'être vert par construction."""
    monkeypatch.setattr(tenancy, "load_tenants", lambda: [
        _ligne("acme", _TIERS),
        _ligne("beta", "https://auth.beta.test/oidc"),
    ])
    server._build_verifier()

    assert tenancy.current().get(_TIERS).slug == "acme"
    assert alertes == []
