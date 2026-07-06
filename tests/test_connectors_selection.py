"""Sélection marketplace (`connectors.*`) — projection compacte (#109) + guidage
d'activation (#111). Seams de domaine monkeypatchés (pas de DB)."""
import oto_mcp.capabilities.connectors_selection as CS
from oto_mcp.capabilities._types import ResolvedCtx


def _catalog(monkeypatch, entries):
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: list(entries))
    monkeypatch.setattr(CS.connector_selection, "list_selection", lambda sub, org: {})
    monkeypatch.setattr(CS.org_store, "get_org_default_connectors", lambda org: [])
    monkeypatch.setattr(CS, "_doctrine_refs_by_ns", lambda org: {})


# ── #109 : projection compacte par défaut, plein sur verbose ──

_FAT = {"name": "serper", "label": "Serper", "help": "recherche web", "family": "api",
        "category": "Prospection", "availability": "self_serve", "logo_url": None,
        "namespaces": ["serper"], "doc_sections": [{"body_md": "x" * 5000}],
        "credential_fields": [{"name": "api_key"}], "auth": {"method": "secret"}}


def test_me_compact_by_default_drops_heavy_fields(monkeypatch):
    _catalog(monkeypatch, [_FAT])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput())
    c = out["connectors"][0]
    assert out["verbose"] is False
    assert c["name"] == "serper" and c["state"] == "not_selected"
    # Les gros champs ne sont PAS dans la vue compacte.
    for heavy in ("doc_sections", "credential_fields", "auth"):
        assert heavy not in c


def test_me_verbose_keeps_full_card(monkeypatch):
    _catalog(monkeypatch, [_FAT])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(verbose=True))
    c = out["connectors"][0]
    assert out["verbose"] is True and c["doc_sections"] and c["auth"]["method"] == "secret"


def test_me_state_filter(monkeypatch):
    _catalog(monkeypatch, [_FAT, {**_FAT, "name": "hunter"}])
    monkeypatch.setattr(CS.connector_selection, "list_selection",
                        lambda sub, org: {"hunter": "active"})
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(state="active"))
    assert [c["name"] for c in out["connectors"]] == ["hunter"]


# ── #111 : guidage d'activation (oto_call comme pont) ──

def test_select_returns_activation_hint(monkeypatch):
    monkeypatch.setattr(CS.connector_activation, "exposed_connectors", lambda org: {"unipile"})
    calls = []
    monkeypatch.setattr(CS.connector_selection, "set_state",
                        lambda sub, name, state, org: calls.append((sub, name, state, org)))
    # #186 : la réponse DONNE les noms d'outils (registre boot, immunisé visibilité).
    import oto_mcp.tool_registry as tr
    monkeypatch.setattr(tr, "boot_tool_names",
                        lambda: ["unipile_me", "unipile_search", "zoho_get"])
    out = CS._select(ResolvedCtx(sub="u1", org_id=42), CS.ConnectorActionInput(name="unipile"))
    assert out["connector"] == "unipile" and out["state"] == "active"
    assert out["tools"] == ["unipile_me", "unipile_search"]   # les siens, pas zoho
    assert "oto_call" in out["hint"] and "unipile_me" in out["hint"]
    assert calls and calls[0][1] == "unipile"
