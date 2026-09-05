"""Chaque sonde `verify` appelée avec ce que le coffre RENDRAIT réellement —
jamais un dict écrit à la main.

Régression du 05/09/2026 (oto#69) : `folk`/`hunter`/`pennylane` lisaient
`fields["api_key"]` alors que `credentials_store.unpack_secret` range la clé
sous `key` pour un `secret_kind="api_key"`. Leurs trois sondes répondaient donc
TOUJOURS `{ok:false, error:"'api_key'"}` — pire que l'absence de sonde, qui au
moins ne mentait pas. Le piège était déjà nommé le 2026-07-08 sur `unipile`
(`tests/test_unipile_verify.py`), et n'a protégé personne d'autre : une leçon
écrite à côté du geste ne protège pas le geste suivant.

Ce fichier ferme la CLASSE, pas les trois cas : il marche le registre RÉEL des
sondes et appelle chacune avec les champs que `unpack_secret` produirait pour
son `secret_kind` (round-trip par `pack_secret`, jamais deviné). Le réseau est
coupé au point bas commun (`requests.Session.request` — tous les clients
oto-core l'utilisent) : la sonde peut échouer pour toute autre raison (compte
introuvable, quota, refus d'auth…), seul un accès de champ qui lève
`KeyError`/`AttributeError` compte comme un défaut de CE banc.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
import requests
from fastmcp import FastMCP

from oto_mcp import credentials_store, providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tools import register_all


class _FakeResponse:
    """Un `requests.Response` assez complet pour qu'AUCUN client ne trébuche sur
    un attribut absent avant même d'atteindre la ligne qui nous intéresse — sinon
    ce banc accuserait sa propre sonde d'un défaut qu'elle n'a pas."""
    status_code = 200
    ok = True
    reason = "OK"
    text = "{}"
    content = b"{}"
    headers: dict = {}

    def json(self):
        # `access_token`/`expires_in` : forme d'un mint de token OAuth2 réussi
        # (client-credentials ou refresh) — un client comme Silae qui indexe
        # `token_data["access_token"]` SANS `.get()` trébucherait sinon sur un
        # KeyError qui n'a rien à voir avec la lecture de champs que ce banc
        # vérifie. Purement additif : aucune sonde existante ne lit ces clés,
        # donc rien d'autre ne change de comportement.
        return {"access_token": "faux-jeton-de-banc", "expires_in": 3600}

    def raise_for_status(self):
        return None


def _fake_fields(connector: str) -> dict:
    """Ce que `unpack_secret` rendrait pour une vraie clé de CE connecteur —
    une valeur factice par champ déclaré (`Connector.secret_fields`), packée
    puis dépaquée par les VRAIES fonctions du coffre, jamais un dict deviné."""
    c = providers.REGISTRY.get(connector)
    schema = c.secret_fields if c else ()
    fake = {f.name: f"fake-{f.name}" for f in schema}
    secret = credentials_store.pack_secret(connector, fake)
    return credentials_store.unpack_secret(connector, secret)


#: Sondes qui ne peuvent PAS passer ce banc pour une raison légitime — déclarée,
#: jamais silencieuse. Une entrée ici est une exemption ASSUMÉE : elle doit
#: nommer pourquoi le banc générique ne peut pas la juger.
EXEMPTS: dict[str, str] = {}


def _registre() -> dict:
    register_all(FastMCP("t"))
    return dict(connector_verify._REGISTRY)


def test_toutes_les_sondes_survivent_a_leurs_vrais_champs(monkeypatch):
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, *a, **k: _FakeResponse())
    echecs = []
    for nom, probe in _registre().items():
        if nom in EXEMPTS:
            continue
        fields = _fake_fields(nom)
        kwargs = {}
        if "instance" in inspect.signature(probe).parameters:
            kwargs["instance"] = ("member", "0:test-bench", "")
        try:
            if inspect.iscoroutinefunction(probe):
                asyncio.run(probe(fields, {}, **kwargs))
            else:
                probe(fields, {}, **kwargs)
        except (KeyError, AttributeError) as e:
            echecs.append(
                f"{nom}: {type(e).__name__} {e!r} — la sonde lit un champ que "
                f"le coffre ne rend pas pour ce connecteur (champs réels : "
                f"{sorted(fields)})")
        except Exception:
            # Toute AUTRE exception est la sonde qui fait son travail normal
            # (compte introuvable, quota, refus d'auth, réseau simulé mal
            # formé pour SA lecture précise…) — ce n'est pas ce banc qui la juge.
            pass
    assert not echecs, (
        "sonde(s) aveugle(s) à leurs propres champs :\n  " + "\n  ".join(echecs))


def test_les_exemptions_restent_justifiees():
    """Une exemption sans sonde enregistrée ne protège plus rien — elle doit
    disparaître avec la sonde qu'elle visait, pas traîner indéfiniment."""
    fantomes = sorted(n for n in EXEMPTS if n not in _registre())
    assert not fantomes, f"exemption(s) sans sonde correspondante : {fantomes}"


def test_le_controle_MORD__une_sonde_qui_lit_le_mauvais_champ_est_detectee():
    """Contre-épreuve : sans cette protection, une sonde `fields["api_key"]`
    sur un connecteur `secret_kind="api_key"` (qui range sous `key`) doit être
    vue — exactement la régression du 05/09."""
    connector_verify.register("_ut_champ_faux", lambda f, c: f["api_key"])
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(requests.Session, "request",
                            lambda self, *a, **k: _FakeResponse())
        fields = _fake_fields("hunter")  # {"key": "fake-key"} — même forme partout
        with pytest.raises(KeyError):
            connector_verify.probe_for("_ut_champ_faux")(fields, {})
    finally:
        monkeypatch.undo()
        del connector_verify._REGISTRY["_ut_champ_faux"]
        del connector_verify._COUVERTURE["_ut_champ_faux"]
