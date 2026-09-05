"""Six trous d'oto#69 qui n'en étaient pas un : la délégation de credential.

`linkedin_unipile`, `whatsapp`, `telegram`, `instagram`, `messenger`, `twitter`
n'ont pas de credential à eux — ils empruntent celui d'`unipile`
(`Connector.credential_of`, cf. `access/cascade.py::walk_cascade`, qui normalise
DÉJÀ pour la résolution). `unipile` a une sonde `verify` depuis #133. Sans lecture
normalisée côté `connectors/verify.py`, chaque canal répondait `verify_unavailable`
malgré une sonde qui teste EXACTEMENT sa clé — le même bug que celui que la cascade
corrige pour la résolution, côté verify.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.capabilities.connectors import verify as cv

CANAUX = ("linkedin_unipile", "whatsapp", "telegram", "instagram", "messenger", "twitter")


@pytest.fixture(autouse=True)
def _registre_intact():
    probes, couv = dict(connector_verify._REGISTRY), dict(connector_verify._COUVERTURE)
    yield
    connector_verify._REGISTRY.clear(), connector_verify._REGISTRY.update(probes)
    connector_verify._COUVERTURE.clear(), connector_verify._COUVERTURE.update(couv)


def test_les_six_canaux_delegue_vers_unipile_dans_le_registre():
    """Prérequis du test : si le registre changeait de forme, ce banc doit le dire
    plutôt que de laisser les tests suivants passer pour une mauvaise raison."""
    for canal in CANAUX:
        assert providers.credential_provider(canal) == "unipile", canal


def test_chaque_canal_trouve_la_sonde_du_porteur():
    connector_verify._REGISTRY.clear()
    connector_verify._COUVERTURE.clear()
    connector_verify.register("unipile", lambda f, c: None, couvre=connector_verify.AUTH_QUOTA)
    for canal in CANAUX:
        assert connector_verify.supports(canal), canal
        assert connector_verify.probe_for(canal) is connector_verify.probe_for("unipile"), canal
        assert connector_verify.couverture(canal) == connector_verify.AUTH_QUOTA, canal


def test_un_connecteur_sans_delegation_n_est_pas_affecte():
    """Le porteur d'un connecteur NU est lui-même — la normalisation ne doit rien
    changer pour les ~50 autres connecteurs de l'issue."""
    connector_verify._REGISTRY.clear()
    connector_verify._COUVERTURE.clear()
    connector_verify.register("hunter", lambda f, c: {"quota": {}})
    assert connector_verify.probe_for("hunter") is not None
    assert connector_verify.probe_for("linkedin_unipile") is None, (
        "hunter n'a aucun rapport avec unipile — aucune fuite de sonde entre "
        "connecteurs non-délégués")


def test_un_canal_sans_sonde_reste_indisponible():
    """Contre-épreuve : si `unipile` lui-même n'a pas de sonde, ses canaux non plus —
    la normalisation ne doit pas fabriquer une sonde qui n'existe pas."""
    connector_verify._REGISTRY.clear()
    connector_verify._COUVERTURE.clear()
    assert connector_verify.probe_for("linkedin_unipile") is None
    assert connector_verify.couverture("linkedin_unipile") is None
    assert not connector_verify.supports("linkedin_unipile")


# --- bout en bout : la capacité, pas seulement le registre --------------------

def test_la_capacite_verify_marche_pour_un_canal_unipile(monkeypatch):
    """`connectors.verify(provider="linkedin_unipile")` ne doit PLUS lever
    `verify_unavailable` : la sonde d'`unipile` la couvre."""
    connector_verify._REGISTRY.clear()
    connector_verify._COUVERTURE.clear()
    connector_verify.register("unipile", lambda f, c: None)
    monkeypatch.setattr(cv.access, "resolve_credential",
                        lambda *a, **k: types.SimpleNamespace(
                            fields={"key": "K"}, config={}, mode="user",
                            entity_type="member", entity_id="42:user-1", account=""))
    monkeypatch.setattr(cv.credentials_store, "update_meta", lambda *a, **k: True)
    res = asyncio.run(cv._verify(cv.ResolvedCtx(sub="user-1", org_id=42),
                                 cv.VerifyInput(provider="linkedin_unipile")))
    assert res["ok"] is True
    assert res["coverage"] == connector_verify.AUTH


def test_la_sante_s_ecrit_sous_le_porteur_pas_sous_le_canal(monkeypatch):
    """Sans normalisation, `update_meta` viserait `connector='linkedin_unipile'` —
    une ligne qui n'existe pas — et échouerait EN SILENCE (0 ligne, pas d'exception).
    La marque doit atterrir sur `unipile`, où vit réellement le credential."""
    connector_verify._REGISTRY.clear()
    connector_verify._COUVERTURE.clear()
    connector_verify.register("unipile", lambda f, c: (_ for _ in ()).throw(
        ValueError("session expirée")))
    monkeypatch.setattr(cv.access, "resolve_credential",
                        lambda *a, **k: types.SimpleNamespace(
                            fields={"key": "K"}, config={}, mode="user",
                            entity_type="member", entity_id="42:user-1", account=""))
    vu = {}
    monkeypatch.setattr(cv.credentials_store, "update_meta",
                        lambda et, eid, conn, acct, patch: vu.update(
                            connector=conn, patch=patch) or True)
    res = asyncio.run(cv._verify(cv.ResolvedCtx(sub="user-1", org_id=42),
                                 cv.VerifyInput(provider="linkedin_unipile")))
    assert res["ok"] is False
    assert vu["connector"] == "unipile", (
        f"la santé a été écrite sous {vu.get('connector')!r} au lieu de 'unipile' — "
        "elle n'atteindra jamais la ligne réelle du coffre")
    assert vu["patch"]["health_ko"] is True
