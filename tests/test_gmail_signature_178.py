"""`gmail(op="draft")` et `gmail_act(op="send")` apposent la signature Gmail du compte émetteur (otomata-tech/oto#178).

L'API Gmail n'appose JAMAIS la signature : c'est le client web qui l'ajoute à la
composition. La CLI la posait (`--sign`, défaut), le tool non — retours 794 et 795
(07/09/2026) : des mails partis au nom de l'utilisateur, sans sa signature.

Doublure de `GmailClient` : aucun appel réel à Google.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.mcp_errors import McpError

_SIG = '<div>Alexis — <a href="https://oto.cx">oto</a></div>'


class _FauxClient:
    def __init__(self, signature=_SIG, boom=None):
        self._signature, self._boom = signature, boom
        self.appels: list[tuple[str, dict]] = []

    def get_signature(self):
        if self._boom:
            raise self._boom
        return self._signature

    def _note(self, nom, kw):
        self.appels.append((nom, kw))
        return {"id": "m1", "threadId": "t1"}

    def send(self, **kw):
        return self._note("send", kw)

    def create_draft(self, **kw):
        return self._note("create_draft", kw)

    def reply(self, **kw):
        return self._note("reply", kw)

    def create_draft_reply(self, **kw):
        return self._note("create_draft_reply", kw)


def _compose(monkeypatch, client, **kw):
    from fastmcp import FastMCP
    from oto_mcp.tools import gmail as G

    monkeypatch.setattr(G, "_client_for_user", lambda account=None: client)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    m = FastMCP("t")
    G.register(m)
    mode = kw.pop("mode", "draft")
    if mode == "send":
        fn = asyncio.run(m.get_tool("gmail_act")).fn
        return asyncio.run(fn(op="send", **kw))
    fn = asyncio.run(m.get_tool("gmail")).fn
    return asyncio.run(fn(op="draft", **kw))


@pytest.mark.parametrize("mode,reply_to,methode", [
    ("send", None, "send"), ("draft", None, "create_draft"),
    ("send", "orig-1", "reply"), ("draft", "orig-1", "create_draft_reply"),
])
def test_la_signature_du_compte_est_apposee_par_defaut(monkeypatch, mode, reply_to,
                                                       methode):
    cli = _FauxClient()
    out = _compose(monkeypatch, cli, body="Bonjour **Jeanne**", mode=mode,
                   to="j@x.fr", subject="s", reply_to=reply_to)
    (nom, kw), = cli.appels
    assert nom == methode
    # le markdown est rendu AVANT la signature : le corps reste mis en forme
    assert "<strong>Jeanne</strong>" in kw["html"]
    assert kw["html"].endswith("<br>--<br>" + _SIG)
    assert out["signature"] == "appended"


def test_un_html_explicite_garde_son_contenu_et_recoit_la_signature(monkeypatch):
    cli = _FauxClient()
    _compose(monkeypatch, cli, body="x", html="<p>Corps</p>", mode="send", to="j@x.fr")
    assert cli.appels[0][1]["html"] == "<p>Corps</p><br>--<br>" + _SIG


def test_markdown_False_donne_un_corps_texte_echappe_pas_du_markdown(monkeypatch):
    cli = _FauxClient()
    _compose(monkeypatch, cli, body="a < b\n**pas gras**", markdown=False,
             mode="send", to="j@x.fr")
    html = cli.appels[0][1]["html"]
    assert html.startswith('<div dir="ltr">a &lt; b<br>**pas gras**</div>')
    assert html.endswith(_SIG)


def test_sign_False_n_appose_rien_et_le_dit(monkeypatch):
    cli = _FauxClient(boom=AssertionError("la signature ne doit pas être lue"))
    out = _compose(monkeypatch, cli, body="x", mode="send", to="j@x.fr", sign=False)
    # sans signature, le markdown est rendu (assaini) ici, pas par GmailClient
    assert cli.appels[0][1]["html"] == "<p>x</p>"
    assert out["signature"] == "disabled"


def test_un_compte_sans_signature_le_dit_au_lieu_de_se_taire(monkeypatch):
    cli = _FauxClient(signature="")
    out = _compose(monkeypatch, cli, body="x", mode="send", to="j@x.fr")
    # sans signature, le markdown est rendu (assaini) ici, pas par GmailClient
    assert cli.appels[0][1]["html"] == "<p>x</p>"
    assert out["signature"] == "none_configured"


def test_une_signature_illisible_refuse_et_n_envoie_rien(monkeypatch):
    cli = _FauxClient(boom=RuntimeError("403 insufficient scope"))
    with pytest.raises(McpError) as e:
        _compose(monkeypatch, cli, body="x", mode="send", to="j@x.fr")
    assert "sign=False" in e.value.error.message
    assert "nothing was written" in e.value.error.message
    assert cli.appels == []


def test_le_texte_servi_annonce_la_signature():
    from fastmcp import FastMCP
    from oto_mcp.tools import gmail as G

    m = FastMCP("t")
    G.register(m)
    for nom in ("gmail", "gmail_act"):
        t = asyncio.run(m.get_tool(nom))
        assert "signature" in t.description and "none_configured" in t.description, nom
        assert t.parameters["properties"]["sign"]["default"] is True, nom
        assert "signature" in t.parameters["properties"]["sign"]["description"], nom


@pytest.mark.parametrize("signature", [
    "-- <br>Alexis",
    '<div dir="ltr">--&nbsp;<br>Alexis</div>',
    "<div>\n  <span>--</span><br>Alexis</div>",
])
def test_une_signature_qui_porte_son_separateur_n_en_recoit_pas_un_second(
        monkeypatch, signature):
    """#1345 : la signature Gmail du compte commence déjà par `--` → un seul séparateur."""
    cli = _FauxClient(signature=signature)
    _compose(monkeypatch, cli, body="Bonjour", to="j@x.fr", subject="s")
    (_, kw), = cli.appels
    assert kw["html"].endswith("<br>" + signature)
    assert "<br>--<br>" not in kw["html"]


def test_un_brouillon_rend_son_lien_web_sous_le_compte_resolu(monkeypatch):
    """#1345 : un brouillon rend un lien cliquable, sous le compte qui l'a écrit
    (celui que la résolution de l'appel a noté)."""
    from oto_mcp import session_org

    class _Cli(_FauxClient):
        def create_draft(self, **kw):
            self.appels.append(("create_draft", kw))
            return {"id": "r-1", "message_id": "18f0a1b2c3", "threadId": "t1"}

    monkeypatch.setattr(session_org, "current_call_trace",
                        lambda: {"resolved_account": "moi@exemple.fr"})
    out = _compose(monkeypatch, _Cli(), body="Bonjour", to="j@x.fr", subject="s")
    assert out["web_link"] == (
        "https://mail.google.com/mail/u/moi@exemple.fr/#drafts?compose=18f0a1b2c3")


def test_un_envoi_ne_rend_pas_de_lien_de_brouillon(monkeypatch):
    from oto_mcp import session_org

    monkeypatch.setattr(session_org, "current_call_trace",
                        lambda: {"resolved_account": "moi@exemple.fr"})
    out = _compose(monkeypatch, _FauxClient(), body="Bonjour", to="j@x.fr",
                   subject="s", mode="send")
    assert "web_link" not in out
