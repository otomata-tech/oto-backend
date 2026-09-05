"""oto#25 lot (a) — le CALLER (`access_token_for`) d'un mount OAuth fédéré.

Jusqu'au 2026-09-04, un grant mort (`*ReauthRequired`, cf.
`test_oauth_refresh_does_not_purge_on_config_error.py` pour la règle qui décide
QUAND cette exception se lève) faisait PURGER la ligne du coffre
(`clear_credential`) : le fait « ça a été révoqué » redevenait indiscernable de
« jamais posé » — un repli qui masque un problème plutôt que de le nommer, que ce
dépôt interdit par principe. Depuis oto#25 lot (a), la ligne reste et se fait
MARQUER rejetée (`meta.health_ko` + `meta.health_reason`), même mécanisme que la
sonde `verify` d'un connecteur keyé (`capabilities/connectors/verify._record_health`),
motif fournisseur BRUT en valeur de champ.

Second volet : `_link_state` (le seam `connectors/link.py` que lit `access.status_for`
pour `/api/me`, seul lecteur qui atteigne ce scope LEGACY `("user", sub)`) doit
relayer cette santé — sinon la marque n'est observable nulle part pour cette
famille (`test_link_state_surfaces_health` ci-dessous, et le pendant côté
`access/status.py` : `test_la_sante_legacy_est_relayee` /
`test_la_sante_absente_ne_pose_rien` dans
`tests/connectors/test_connector_link_status.py`).
"""
from __future__ import annotations

import importlib

import pytest

from oto_mcp import credentials_store


class _Resp:
    """Réponse HTTP minimale, même patron que le fichier voisin."""

    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_post(monkeypatch, mod, resp) -> None:
    """Neutralise le réseau ET la résolution de client (même garde que le fichier
    voisin : `_client_id()` déclenche une DCR réelle si non patché)."""
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)
    if hasattr(mod, "_client_id"):
        monkeypatch.setattr(mod, "_client_id", lambda: "client-de-test")


class _FakeVault:
    """Coffre minimal, keyé comme `connector_credentials` : {(entity_type,
    entity_id, connector, account): {secret, meta}}. Assez pour observer si une
    ligne SURVIT, sans dépendre d'une vraie PG (pas de DATABASE_URL en local)."""

    def __init__(self, sub: str, connector: str, refresh_token: str):
        self.rows: dict = {("user", sub, connector, ""):
                           {"secret": refresh_token, "meta": {}}}

    def get_credential_with_meta(self, entity_type, entity_id, connector, account=""):
        row = self.rows.get((entity_type, entity_id, connector, account))
        if not row:
            return None
        return {"secret": row["secret"], "meta": row["meta"],
                "set_at": "2026-01-01T00:00:00Z", "set_by": entity_id}

    def update_meta(self, entity_type, entity_id, connector, account, patch, conn=None):
        row = self.rows.get((entity_type, entity_id, connector, account))
        if row is None:
            return False
        row["meta"] = {**row["meta"], **patch}
        return True

    def clear_credential(self, entity_type, entity_id, connector, conn=None, account=""):
        return self.rows.pop((entity_type, entity_id, connector, account), None) is not None

    def set_credential(self, entity_type, entity_id, connector, secret, set_by=None,
                       meta=None, conn=None, account="", expected_version=None):
        self.rows[(entity_type, entity_id, connector, account)] = {
            "secret": secret, "meta": meta or {}}


_CASES = [
    ("atlassian", "AtlassianReauthRequired", "atlassian"),
    ("folk", "FolkReauthRequired", "folkmcp"),
]


@pytest.mark.parametrize("modname,excname,connector", _CASES)
def test_dead_grant_marks_rejected_row_survives(monkeypatch, modname, excname, connector):
    """LE cœur du correctif : la ligne existe TOUJOURS après un grant mort, et
    porte le motif fournisseur brut — pas la purge d'avant."""
    mod = importlib.import_module(f"oto_mcp.auth.{modname}")
    vault = _FakeVault("sub-1", connector, "REFRESH-DEAD")

    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        vault.get_credential_with_meta)
    monkeypatch.setattr(credentials_store, "update_meta", vault.update_meta)
    monkeypatch.setattr(credentials_store, "clear_credential", vault.clear_credential)
    monkeypatch.setattr(credentials_store, "set_credential", vault.set_credential)
    _patch_post(monkeypatch, mod,
               _Resp(400, '{"error":"invalid_grant","error_description":"token revoked"}'))

    token = mod.access_token_for("sub-1")

    assert token is None
    key = ("user", "sub-1", connector, "")
    assert key in vault.rows, (
        "la ligne du coffre a été PURGÉE — régression du comportement d'avant "
        "oto#25 lot (a) : « révoqué » redevient indiscernable de « jamais posé »")
    meta = vault.rows[key]["meta"]
    assert meta["health_ko"] is True
    assert meta["health_reason"] is not None and "invalid_grant" in meta["health_reason"], (
        f"le motif fournisseur BRUT doit être en valeur de champ, pas une "
        f"catégorie opaque : {meta['health_reason']!r}")


class _RespOK:
    """Réponse HTTP 200 avec un corps JSON réel — le cas RÉUSSI, absent de `_Resp`
    (toujours `{}`, pensé pour les cas d'échec où seul le texte brut compte)."""

    def __init__(self, body: dict):
        self.status_code, self._body = 200, body

    @property
    def text(self) -> str:
        import json
        return json.dumps(self._body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        pass


@pytest.mark.parametrize("modname,connector", [("atlassian", "atlassian"), ("folk", "folkmcp")])
def test_un_refresh_reussi_demarque_une_ligne_precedemment_rejetee(monkeypatch, modname, connector):
    """oto#25 lot (b3) — le démarquage sur « refresh réussi ». Ce n'est PAS un geste
    neuf : `set_credential` REMPLACE tout le `meta` (jamais un merge, cf. son
    docstring) — le chemin nominal de `access_token_for` écrit `meta={access_token,
    expires_at}` sans jamais reporter `health_ko`. Ce test fige ce comportement
    (aujourd'hui accidentel, demain une garantie) : une régression de `set_credential`
    vers un merge le ferait rougir."""
    mod = importlib.import_module(f"oto_mcp.auth.{modname}")
    vault = _FakeVault("sub-1", connector, "REFRESH-1")
    key = ("user", "sub-1", connector, "")
    vault.rows[key]["meta"] = {"health_ko": True, "health_reason": "invalid_grant: dead"}

    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        vault.get_credential_with_meta)
    monkeypatch.setattr(credentials_store, "update_meta", vault.update_meta)
    monkeypatch.setattr(credentials_store, "clear_credential", vault.clear_credential)
    monkeypatch.setattr(credentials_store, "set_credential", vault.set_credential)
    _patch_post(monkeypatch, mod, _RespOK({"access_token": "AT-NEW", "expires_in": 3600}))

    token = mod.access_token_for("sub-1")

    assert token == "AT-NEW"
    meta = vault.rows[key]["meta"]
    assert "health_ko" not in meta and "health_reason" not in meta, (
        f"le refresh a réussi, la marque de rejet aurait dû disparaître : {meta!r}")


@pytest.mark.parametrize("modname,excname,connector", _CASES)
def test_config_error_still_does_not_touch_the_row(monkeypatch, modname, excname, connector):
    """Contre-épreuve : un incident de CONFIG (`invalid_client`) ne lève pas
    `*ReauthRequired` (verrouillé côté `_refresh` par le fichier voisin) — il doit
    donc remonter tel quel, sans que `access_token_for` marque ni purge quoi que
    ce soit sur une ligne par ailleurs valide."""
    mod = importlib.import_module(f"oto_mcp.auth.{modname}")
    vault = _FakeVault("sub-1", connector, "REFRESH-OK")

    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        vault.get_credential_with_meta)
    monkeypatch.setattr(credentials_store, "update_meta", vault.update_meta)
    monkeypatch.setattr(credentials_store, "clear_credential", vault.clear_credential)
    monkeypatch.setattr(credentials_store, "set_credential", vault.set_credential)
    _patch_post(monkeypatch, mod, _Resp(400, '{"error":"invalid_client"}'))

    with pytest.raises(Exception):
        mod.access_token_for("sub-1")

    key = ("user", "sub-1", connector, "")
    assert key in vault.rows
    assert vault.rows[key]["meta"] == {}, "un incident de config ne doit rien écrire sur la ligne"


@pytest.mark.parametrize("modname,connector", [("atlassian", "atlassian"), ("folk", "folkmcp")])
def test_link_state_surfaces_health(monkeypatch, modname, connector):
    """`_link_state` — seul lecteur qui atteigne ce scope LEGACY — doit relayer
    `health_ko`/`health_reason` : sans lui, la marque posée ci-dessus ne serait
    observable nulle part pour cette famille (cf. `connectors/link.py`,
    `access/status.py`)."""
    mod = importlib.import_module(f"oto_mcp.auth.{modname}")
    monkeypatch.setattr(
        credentials_store, "get_credential_with_meta",
        lambda et, eid, con, account="": {"secret": "x", "meta": {}, "set_at": "t"})
    monkeypatch.setattr(
        credentials_store, "credential_health",
        lambda et, eid, con, account="": "invalid_grant (motif brut de test)")

    st = mod._link_state("sub-1")

    assert st.linked is True
    assert st.health_ko is True
    assert st.health_reason == "invalid_grant (motif brut de test)"


@pytest.mark.parametrize("modname,connector", [("atlassian", "atlassian"), ("folk", "folkmcp")])
def test_link_state_healthy_reports_no_ko(monkeypatch, modname, connector):
    """Contrepartie : tant que rien n'a été constaté, `health_ko` reste `None`
    (jamais `False`) — même convention que `ProviderStatus.health_ko`."""
    mod = importlib.import_module(f"oto_mcp.auth.{modname}")
    monkeypatch.setattr(
        credentials_store, "get_credential_with_meta",
        lambda et, eid, con, account="": {"secret": "x", "meta": {}, "set_at": "t"})
    monkeypatch.setattr(
        credentials_store, "credential_health", lambda et, eid, con, account="": None)

    st = mod._link_state("sub-1")

    assert st.health_ko is None
    assert st.health_reason is None
