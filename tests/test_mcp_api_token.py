"""La face MCP accepte un jeton d'API `oto_` — et refuse un jeton PORTÉ.

Pourquoi ce chemin existe : un runtime non interactif (Claude Tag dans Slack, une
CI) ne peut pas faire l'OAuth dance. Sans lui, il faut une application machine
dédiée par intégration, donc un compte orphelin sans email ni dashboard, que son
propriétaire ne peut ni voir ni configurer. Un jeton d'API, lui, EST un vrai compte.

Pourquoi le cran sur les jetons portés : leur gate (`token_scopes.authorize`)
raisonne sur méthode + chemin HTTP, notions absentes d'un appel MCP. Les accepter
ici ignorerait leur portée en silence — c'est-à-dire l'élargirait. Ce test est le
garde-fou : il tombe si quelqu'un retire le fail-closed.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import server


class _Verifier(server._IatGatedVerifier):
    """Instance nue : on n'exerce que le chemin jeton d'API, jamais le JWT."""

    def __init__(self):
        pass  # le parent exigerait un JWKS joignable

    async def verify_token(self, token):  # noqa: D102 — le JWT n'est pas le sujet
        return await server._IatGatedVerifier.verify_token(self, token)


def _verify(token: str, row, monkeypatch):
    monkeypatch.setattr(server.db, "verify_api_token", lambda t: row)
    return asyncio.run(_Verifier()._verify_api_token(token))


def test_jeton_non_porte_resout_son_sub(monkeypatch):
    tok = _verify("oto_abc", {"sub": "u-42", "scopes": None}, monkeypatch)
    assert tok is not None
    assert tok.claims["sub"] == "u-42"
    assert tok.subject == "u-42"


@pytest.mark.parametrize("kind", ["user", "delegation"])
def test_le_genre_du_jeton_voyage_avec_lui(kind, monkeypatch):
    """`token_kind` est ce qui dit, au bord du protocole, qu'un appel est un travail du
    runner : sans lui, le renommage par tenant servait `acme_*` au worker, dont
    l'allowlist est canonique — et l'agent tournait sans outils."""
    tok = _verify("oto_abc", {"sub": "acme:u-1", "scopes": None, "token_kind": kind},
                  monkeypatch)
    assert tok.claims["token_kind"] == kind


def test_jeton_porte_refuse(monkeypatch):
    assert _verify("oto_abc", {"sub": "u-42", "scopes": {"namespaces": ["crm"]}},
                   monkeypatch) is None


def test_jeton_inconnu_refuse(monkeypatch):
    assert _verify("oto_abc", None, monkeypatch) is None


@pytest.mark.parametrize("token", ["", "eyJhbGciOi.jwt.like", "Bearer oto_abc"])
def test_ce_qui_nest_pas_un_jeton_api_passe_au_jwt(token, monkeypatch):
    """Pas de préfixe `oto_` ⟹ on ne touche pas la DB et on rend la main au JWT."""
    def _boom(_):
        raise AssertionError("la DB ne doit pas être interrogée pour un non-jeton")

    monkeypatch.setattr(server.db, "verify_api_token", _boom)
    assert asyncio.run(_Verifier()._verify_api_token(token)) is None


def test_le_jwt_reste_le_chemin_par_defaut(monkeypatch):
    """`verify_token` ne court-circuite le JWT que pour un jeton `oto_` reconnu."""
    monkeypatch.setattr(server.db, "verify_api_token", lambda t: None)
    calls = []

    async def _super(self, token):
        calls.append(token)
        return None

    monkeypatch.setattr(server.JWTVerifier, "verify_token", _super)
    # Registre d'émetteurs vide (défaut de classe) : un jeton sans `iss` connu route
    # sur le verifier PRIMAIRE — donc `super().verify_token`, ci-dessus.
    v = _Verifier()
    asyncio.run(v.verify_token("oto_inconnu"))
    assert calls == ["oto_inconnu"], "un jeton oto_ rejeté doit retomber sur le JWT"
