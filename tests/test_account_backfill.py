"""R1c — renommage de compte du coffre (backfill '' -> label nommé au passage
multi-compte). Re-chiffrement obligatoire : l'AAD lie le ciphertext à son `account`.
"""
from oto_mcp import credentials_store as cs


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_rename_account_reencrypts(monkeypatch):
    monkeypatch.setattr(cs, "get_credential_with_meta",
                        lambda et, eid, con, account="":
                        {"secret": "S", "meta": {"is_default": True}, "set_at": None})
    monkeypatch.setattr(cs, "_connect", lambda: _Conn())
    ups, dels = [], []
    monkeypatch.setattr(cs, "_upsert",
                        lambda c, et, eid, con, acct, sec, sb, meta: ups.append((acct, sec, meta)))
    monkeypatch.setattr(cs, "_delete", lambda c, et, eid, con, acct: dels.append(acct))
    assert cs.rename_account("member", "1:u", "zoho", "", "principal") is True
    assert ups == [("principal", "S", {"is_default": True})]  # re-posé sous le nouveau nom
    assert dels == [""]                                       # ancienne ligne supprimée


def test_rename_account_missing_returns_false(monkeypatch):
    monkeypatch.setattr(cs, "get_credential_with_meta", lambda *a, **k: None)
    assert cs.rename_account("member", "1:u", "zoho", "", "principal") is False
