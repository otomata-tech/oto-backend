"""oto#25 lot b2 — `connectors/health.py`, l'aide PARTAGÉE de marquage.

Extrait de `capabilities/connectors/verify.py` (`_FLAGGABLE` / `_record_health`) pour
que d'autres modules (atlassian, folk, salesforce, zoho) marquent une ligne rejetée
sans redéfinir chacun leur propre garde de portée. Ce fichier couvre le module en
isolation — la non-régression de `verify.py` lui-même est couverte par
`tests/test_credential_rejete_541.py` et `tests/connectors/test_connector_verify.py`
(déjà verts après le refactor, sans modification).
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import health


@pytest.fixture
def vault(monkeypatch):
    """Coffre en mémoire : {(entity_type, entity_id, connector, account): meta}."""
    rows: dict = {("member", "2:sub-x", "linear", ""): {}}

    def _update_meta(entity_type, entity_id, connector, account, patch, conn=None):
        key = (entity_type, entity_id, connector, account)
        if key not in rows:
            return False
        rows[key] = {**rows[key], **patch}
        return True

    monkeypatch.setattr(credentials_store, "update_meta", _update_meta)
    return rows


# --- record_health : ce qu'utilise verify.py, inchangé --------------------------

def test_record_health_marque_le_rejet(vault):
    health.record_health("linear", ("member", "2:sub-x", ""), False, "invalid_grant")
    assert vault[("member", "2:sub-x", "linear", "")] == {
        "health_ko": True, "health_reason": "invalid_grant"}


def test_record_health_demarque_sur_succes(vault):
    """`ok=True` efface `health_ko`/`health_reason` — le DÉMARQUAGE (b3) reste
    intact : ce lot (b2) ne le touche pas."""
    vault[("member", "2:sub-x", "linear", "")] = {
        "health_ko": True, "health_reason": "invalid_grant"}
    health.record_health("linear", ("member", "2:sub-x", ""), True, None)
    assert vault[("member", "2:sub-x", "linear", "")] == {
        "health_ko": False, "health_reason": None}


def test_record_health_scope_none_est_un_noop(vault):
    health.record_health("linear", None, False, "peu importe")
    assert vault[("member", "2:sub-x", "linear", "")] == {}, "aucun scope ⇒ rien n'est écrit"


def test_record_health_est_best_effort(monkeypatch):
    """Une base indisponible ne doit jamais faire échouer l'appelant — la santé
    est un bonus, jamais bloquant (même contrat que l'ancien `_record_health`)."""
    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(credentials_store, "update_meta", _boom)
    health.record_health("linear", ("member", "2:sub-x", ""), False, "x")  # ne lève pas


# --- mark_rejected : la façade neuve du lot b2 -----------------------------------

def test_mark_rejected_ecrit_sur_un_scope_flaggable(vault):
    health.mark_rejected("member", "2:sub-x", "linear", "", "invalid_grant: token revoked")
    meta = vault[("member", "2:sub-x", "linear", "")]
    assert meta["health_ko"] is True
    assert meta["health_reason"] == "invalid_grant: token revoked"


@pytest.mark.parametrize("entity_type", ["group", "org"])
def test_mark_rejected_ecrit_sur_les_trois_paliers_geres_par_verify(vault, entity_type, monkeypatch):
    rows = vault
    rows[(entity_type, "7", "linear", "")] = {}
    health.mark_rejected(entity_type, "7", "linear", "", "invalid_grant")
    assert rows[(entity_type, "7", "linear", "")]["health_ko"] is True


def test_mark_rejected_accepte_le_scope_user_legacy(vault):
    """Le scope LEGACY `("user", sub)` (atlassian/folkmcp/google) est aussi ÉTROIT
    qu'un scope membre — un seul utilisateur — et n'atteint jamais `verify.py` (sa
    cascade ne produit que member/group/org, cf. docstring du module). L'accepter
    ici est ce qui permet à atlassian/folk d'utiliser la MÊME garde."""
    vault[("user", "sub-1", "atlassian", "")] = {}
    health.mark_rejected(credentials_store.USER, "sub-1", "atlassian", "", "invalid_grant")
    assert vault[("user", "sub-1", "atlassian", "")]["health_ko"] is True


@pytest.mark.parametrize("entity_type", ["tenant", "platform", None])
def test_mark_rejected_refuse_tenant_et_plateforme(monkeypatch, entity_type):
    """Le cœur de la garde : jamais tenant/plateforme, quel que soit l'appelant —
    un hoquet chez un seul membre ne doit pas peindre en rouge une clé PARTAGÉE."""
    calls = []
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda *a, **k: calls.append(a) or True)
    health.mark_rejected(entity_type, "peu-importe", "linear", "", "invalid_grant")
    assert calls == [], f"un scope {entity_type!r} ne doit RIEN écrire"


def test_mark_rejected_refuse_un_entity_id_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda *a, **k: calls.append(a) or True)
    health.mark_rejected("member", None, "linear", "", "invalid_grant")
    assert calls == []


def test_mark_rejected_ne_demarque_jamais(vault):
    """`mark_rejected` n'a qu'un sens : signaler un rejet RÉEL — il ne prend même
    pas de paramètre `ok`, contrairement à `record_health`."""
    import inspect
    assert "ok" not in inspect.signature(health.mark_rejected).parameters
