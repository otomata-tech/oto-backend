"""B3 : unipile_client tire le DSN de la config du credential résolu (apparié à
la clé gagnante de la cascade). Clé plateforme → DSN None (instance env/défaut)."""
from oto_mcp import access
from oto_mcp.access import ResolvedCredential
from oto_mcp.tools import unipile as unipile_tool


class _FakeClient:
    def __init__(self, api_key=None, account_id=None, dsn=None):
        self.api_key, self.account_id, self.dsn = api_key, account_id, dsn


def test_unipile_client_byo_passes_dsn(monkeypatch):
    rc = ResolvedCredential("unipile", "KEY", False, "org", "org", "39")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(
        access.credentials_store, "get_credential_with_meta",
        lambda et, eid, prov, account="": {
            "meta": {"dsn": "api6.unipile.com:13616"}, "secret": "KEY", "set_at": None})
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(unipile_tool.db, "get_unipile_account_id", lambda sub, prov: "ACC")
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)

    cli = unipile_tool.unipile_client("LINKEDIN")
    assert cli.dsn == "api6.unipile.com:13616"
    assert cli.api_key == "KEY" and cli.account_id == "ACC"


def test_unipile_client_platform_dsn_none(monkeypatch):
    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(unipile_tool.db, "get_unipile_account_id", lambda sub, prov: "ACC")
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)

    cli = unipile_tool.unipile_client("LINKEDIN")
    assert cli.dsn is None and cli.api_key == "PK"
