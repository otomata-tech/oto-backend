"""Un `feedback` qui ne signale RIEN est refusé, sur les deux faces (13/11/2026).

Des agents hébergés déposaient des signaux qui n'en sont pas — cible ou texte valant
« noop », « placeholder », « test », « n/a », « accidental call »… — et chacun devait
être refusé à la main dans la pile d'arbitrage. Le serveur les refuse désormais avant
toute écriture : 400 `feedback_placeholder` (REST), `McpError` portant le même message
(MCP).

⚠️ **Le refus est conservateur, et c'est l'autre moitié de ce banc** : la liste est
courte et se compare à la valeur ENTIÈRE. Un vrai retour qui contient « test » ou
« ignore » dans une phrase passe ; un texte vide avec une vraie cible passe aussi
(`text` est optionnel depuis toujours).
"""
from __future__ import annotations

import asyncio

import pytest

from _datastore_rest import call, cap, stub_authz

from oto_mcp.capabilities import usage as U
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.db.usage import DepotSignal

# (target, text) qui ne portent aucun retour.
PLACEHOLDERS = [
    ("noop", None),
    ("no-op", ""),
    ("placeholder", "placeholder"),
    ("serper_search", "noop"),
    ("serper_search", "  No-Op.  "),
    ("serper_search", "test"),
    ("serper_search", "IGNORE"),
    ("serper_search", "n/a"),
    ("serper_search", "accidental call"),
    ("internal-note-skip", None),
    ("internal_note_skip", "   "),
    ("noop-wait", None),
    ("data_write", "[placeholder]"),
    ("", None),
    ("  ", "   "),
]

# Des retours réels — y compris ceux qui CONTIENNENT les mots de la liste.
REELS = [
    ("serper_search", "returned 0 results for a query that has hits on google"),
    ("data_write", "the test row was refused with unknown_column although declared"),
    ("lemlist_campaign", "had to ignore the `status` field: it is always null"),
    ("test", "the tool named in the guide does not exist in my toolbox"),
    ("noop", "run_finish answered ok but the run stays open in the timeline"),
    ("send a whatsapp message to a group", None),
    ("fr_search", "n/a values everywhere in the officers block"),
    # Les signalements COURTS, mais réels : la liste ne les attrape pas, ni en texte
    # avec une cible, ni en cible seule (un `gap` dont la cible EST le retour).
    ("serper_search", "le tool serper_search renvoie 500"),
    ("data_write", "champ email vide"),
    ("serper_scrape", "timeout"),
    ("data_write", "doublon"),
    ("le tool serper_search renvoie 500", None),
    ("champ email vide", None),
    ("timeout", None),
    ("doublon", ""),
    ("", "timeout"),
    ("", "doublon"),
    ("serper_search", "ignoré"),
    ("serper_search", "500"),
]

SUB = "u-1"


@pytest.fixture()
def depots(monkeypatch):
    """L'écriture remplacée par un enregistreur : on lit ce qui serait écrit, sans base."""
    vus: list[dict] = []

    def _insert(**kw):
        vus.append(kw)
        return DepotSignal(id=len(vus))

    monkeypatch.setattr(U.db, "insert_usage_signal", _insert)
    monkeypatch.setattr(U, "_correlation", lambda: ("agent", "s1"))
    stub_authz(monkeypatch)
    return vus


def _corps(target, text):
    body = {"signal": "tool_feedback", "kind": "other", "target": target}
    if text is not None:
        body["text"] = text
    return body


# --- La règle -------------------------------------------------------------

@pytest.mark.parametrize("target,text", PLACEHOLDERS)
def test_un_placeholder_est_refuse_avant_toute_ecriture(depots, target, text):
    inp = U.FeedbackInput(**_corps(target, text))

    class _Ctx:
        sub, org_id = SUB, 35

    with pytest.raises(AuthzDenied) as e:
        U._feedback(_Ctx(), inp)
    assert (e.value.status, e.value.code) == (400, "feedback_placeholder")
    assert "do not call feedback" in e.value.message
    assert depots == []


@pytest.mark.parametrize("target,text", REELS)
def test_un_vrai_retour_passe_meme_s_il_contient_ces_mots(depots, target, text):
    inp = U.FeedbackInput(**_corps(target, text))

    class _Ctx:
        sub, org_id = SUB, 35

    out = U._feedback(_Ctx(), inp)
    assert out["ok"] is True
    assert [d["target"] for d in depots] == [target]


# --- La face REST ---------------------------------------------------------

@pytest.mark.parametrize("target,text", PLACEHOLDERS)
def test_rest_refuse_un_placeholder(depots, target, text):
    code, out = call("usage.feedback", body=_corps(target, text), sub=SUB)
    assert code == 400, out
    assert out["error"] == "feedback_placeholder"
    assert "if there is nothing to report, do not call feedback" in out["detail"]
    assert depots == []


@pytest.mark.parametrize("target,text", REELS)
def test_rest_accepte_un_vrai_retour(depots, target, text):
    code, out = call("usage.feedback", body=_corps(target, text), sub=SUB)
    assert code == 200, out
    assert out["ok"] is True and out["id"] == 1
    assert len(depots) == 1 and depots[0]["body"] == text


# --- La face MCP ----------------------------------------------------------

def _appel_mcp(monkeypatch, arguments):
    from fastmcp import Client, FastMCP

    from oto_mcp import org_suspension
    from oto_mcp.capabilities import _mcp_adapter

    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(org_suspension, "garde_capacite", lambda key, ctx: None)
    mcp = FastMCP("sonde")
    _mcp_adapter.register(mcp, [cap("usage.feedback")])

    async def _go():
        async with Client(mcp) as c:
            return await c.call_tool("feedback", arguments, raise_on_error=False)

    return asyncio.run(_go())


def _texte(res) -> str:
    return " ".join(getattr(b, "text", "") for b in (res.content or []))


@pytest.mark.parametrize("target,text", [
    ("noop", None), ("serper_search", "test"), ("accidental call", "n/a")])
def test_mcp_refuse_un_placeholder(depots, monkeypatch, target, text):
    res = _appel_mcp(monkeypatch, _corps(target, text))
    assert res.is_error
    assert "if there is nothing to report, do not call feedback" in _texte(res)
    assert depots == []


def test_mcp_accepte_un_vrai_retour(depots, monkeypatch):
    res = _appel_mcp(monkeypatch, _corps(
        "data_write", "the test row was refused with unknown_column although declared"))
    assert not res.is_error, _texte(res)
    assert len(depots) == 1


def test_la_description_servie_dit_de_ne_pas_l_appeler_a_vide():
    assert "Never call it as a no-op" in cap("usage.feedback").description
