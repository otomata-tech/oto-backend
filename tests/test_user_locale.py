"""Capacité `me.locale.set` (préférence de langue UI) : validation de l'énum
'en'|'fr' (Input pydantic), écriture set→get via le seam db monkeypatché."""
import pytest
from pydantic import ValidationError

from oto_mcp.capabilities import user_locale as UL
from oto_mcp.capabilities._types import ResolvedCtx


def _ctx(sub="u1"):
    return ResolvedCtx(sub=sub)


def test_set_writes_and_returns_locale(monkeypatch):
    store = {}
    monkeypatch.setattr(UL.db, "set_user_locale",
                        lambda sub, locale: store.__setitem__(sub, locale))
    out = UL._set_locale(_ctx(sub="u1"), UL.SetLocaleInput(locale="fr"))
    assert out == {"locale": "fr"}
    assert store == {"u1": "fr"}                    # écrit sous le bon sub


def test_get_reflects_set(monkeypatch):
    # set→get end-to-end contre un faux users store (dict par sub).
    users = {"u1": {"locale": None}}
    monkeypatch.setattr(UL.db, "set_user_locale",
                        lambda sub, locale: users[sub].__setitem__("locale", locale))
    UL._set_locale(_ctx(sub="u1"), UL.SetLocaleInput(locale="en"))
    assert users["u1"]["locale"] == "en"            # ce que GET /api/me relira


@pytest.mark.parametrize("bad", ["de", "EN", "", "français", "en-US"])
def test_invalid_locale_rejected(bad):
    with pytest.raises(ValidationError):
        UL.SetLocaleInput(locale=bad)


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    assert "me.locale.set" in by_key
    cap = by_key["me.locale.set"]
    assert cap.mcp is None                          # REST-only (préférence UI)
    assert cap.rest.verb == "PUT" and cap.rest.path == "/api/me/locale"
