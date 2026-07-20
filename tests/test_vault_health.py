"""Scan de santé du coffre (#72) — `credentials_store.classify_vault_rows` + capacité.

On exerce le VRAI chemin AES-GCM (comme `test_crypto`) : une ligne chiffrée avec la
master key COURANTE est saine, une ligne chiffrée avec une clé PÉRIMÉE ressort
`undecryptable`. Invariant de confidentialité : la sortie ne contient JAMAIS de
plaintext. La capacité est testée par stub (garde `PLATFORM_ADMIN` + traduction de
l'erreur master-key-absente). Pas de DB : le SELECT de `scan_vault_health` est
vérifié au déploiement (cf. CLAUDE.md).
"""
from __future__ import annotations

import base64
import json

import pytest

from oto_mcp import credentials_store as cs

_KEY_A = base64.b64encode(b"\xaa" * 32).decode()   # master key courante
_KEY_B = base64.b64encode(b"\xbb" * 32).decode()   # ancienne (périmée)


def _row(monkeypatch, key, et, eid, connector, account, secret):
    """Fabrique une ligne de coffre chiffrée avec `key` (mime une ligne PG)."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", key)
    from oto_mcp import crypto
    blob = crypto.encrypt(secret, cs._aad(et, eid, connector, account))
    return {"entity_type": et, "entity_id": eid, "connector": connector,
            "account": account, "secret_enc": blob, "set_at": "2026-07-16 00:00:00"}


def test_classify_detects_stale_key_row(monkeypatch):
    rows = [
        _row(monkeypatch, _KEY_A, "org", "1", "attio", "", "good-org"),
        _row(monkeypatch, _KEY_B, "member", "1:sub-x", "unipile", "", "STALE-SECRET"),
        _row(monkeypatch, _KEY_A, "platform", "otomata", "scaleway", "", "good-pf"),
    ]
    # scan avec la clé COURANTE = A → seule la ligne chiffrée en B est indéchiffrable.
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_A)
    res = cs.classify_vault_rows(rows)

    assert res["total"] == 3
    assert res["ok"] == 2
    assert res["undecryptable"] == 1
    assert res["by_connector"]["unipile"] == {"total": 1, "undecryptable": 1}
    assert res["by_connector"]["attio"] == {"total": 1, "undecryptable": 0}
    assert res["by_entity_type"]["member"] == {"total": 1, "undecryptable": 1}
    ko = res["undecryptable_rows"]
    assert len(ko) == 1
    assert (ko[0]["entity_type"], ko[0]["connector"], ko[0]["entity_id"]) == \
        ("member", "unipile", "1:sub-x")


def test_scan_output_never_leaks_plaintext(monkeypatch):
    rows = [
        _row(monkeypatch, _KEY_A, "org", "1", "attio", "", "TOP-SECRET-VALUE"),
        _row(monkeypatch, _KEY_B, "org", "2", "attio", "", "ANOTHER-SECRET"),
    ]
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_A)
    res = cs.classify_vault_rows(rows)
    blob = json.dumps(res, default=str)
    assert "TOP-SECRET-VALUE" not in blob
    assert "ANOTHER-SECRET" not in blob
    # ni le ciphertext (secret_enc) ne fuit dans la sortie
    assert "secret_enc" not in blob


def test_empty_vault(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_A)
    res = cs.classify_vault_rows([])
    assert res == {"total": 0, "ok": 0, "undecryptable": 0,
                   "by_connector": {}, "by_entity_type": {}, "undecryptable_rows": []}


# ── capacité ──────────────────────────────────────────────────────────────

def test_capability_returns_scan(monkeypatch):
    from oto_mcp.capabilities import vault_health as vh
    from oto_mcp.capabilities._types import ResolvedCtx
    sentinel = {"total": 5, "undecryptable": 2}
    monkeypatch.setattr(cs, "scan_vault_health", lambda: sentinel)
    out = vh._vault_health(ResolvedCtx(sub="admin", org_id=None), vh.VaultHealthInput())
    assert out is sentinel


def test_capability_translates_missing_key(monkeypatch):
    from oto_mcp.capabilities import vault_health as vh
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

    def _boom():
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — scan de santé impossible")
    monkeypatch.setattr(cs, "scan_vault_health", _boom)
    with pytest.raises(AuthzDenied) as e:
        vh._vault_health(ResolvedCtx(sub="admin", org_id=None), vh.VaultHealthInput())
    assert e.value.code == "vault_scan_unavailable"
    assert e.value.status == 503
