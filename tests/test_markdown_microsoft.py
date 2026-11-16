"""Le corps markdown des services Microsoft 365 passe par le rendu commun des messages.

`outlook` (brouillon, réponse ; l'envoi passe par le même brouillon), `teams_act`, `onenote_act` et `outlook_calendar_event`
rendent `body` par `_microsoft_graph.markdown_html`, qui délègue à
`markdown_assaini.rendu_markdown_assaini` — le rendu de `gmail` et d'`email_send`.

Ce que ces tests tiennent :
- sur chaque service, le HTML qui part vers Graph est le rendu commun : rien de ce que
  `test_markdown_assaini` écarte n'arrive au client ;
- un message ordinaire sort comme le rendu d'oto-core, au `rel` des liens et à
  l'écriture du style d'alignement près, image `http(s)` comprise ;
- l'image `cid:` (pièce jointe en ligne) n'est gardée que par `outlook`, le seul service
  qui porte des pièces en ligne (`content_id`).

Doublures des clients d'oto-core (autospec) : aucun appel réel à Microsoft Graph.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import create_autospec

import pytest

from oto_mcp.tools._microsoft_graph import markdown_html

_PIEGES = [
    ("<script>alert(1)</script>", "<script"),
    ('<img src="x" onerror="alert(1)">', "onerror"),
    ('<b onclick="x()">y</b>', "onclick"),
    ('paragraphe\n{: onclick="x()" }', "onclick"),
    ("[clic](javascript:alert(1))", "javascript:"),
    ("[clic](data:text/html;base64,PHNjcmlwdD4=)", "data:"),
    ("![p](data:image/png;base64,AAAA)", "data:"),
    ('<iframe src="https://x.test"></iframe>', "<iframe"),
]

_ORDINAIRE = (
    "# Titre\n\n## Sous-titre\n\nBonjour **Camille**, voici *le* point :\n\n"
    "- un\n- deux\n    - deux.a\n\n1. a\n2. b\n\n> cité\n\n"
    "[le lien](https://exemple.test/x?a=1&b=2) et `code`\n\n"
    "![logo](https://exemple.test/l.png)\n\n"
    "| a | b |\n|:-|-:|\n| 1 | 2 |\n\n---\n\nfin  \nligne\n\n```\nbloc\n```\n"
)


def _service(monkeypatch, module, classe):
    """Le faux client d'oto-core du service, le jeton et le compte : rien ne sort."""
    import oto.tools.microsoft as coeur

    inst = create_autospec(getattr(coeur, classe), instance=True)
    monkeypatch.setattr(coeur, classe, lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for",
                        lambda sub, svc, renew=False: f"AT-{svc}")
    monkeypatch.setattr("oto_mcp.auth.microsoft.resolve_account",
                        lambda sub, svc, account=None: (object(), {"scopes": ""}))
    import importlib

    from fastmcp import FastMCP

    m = FastMCP("t")
    importlib.import_module(f"oto_mcp.tools.{module}").register(m)
    return inst, lambda nom: asyncio.run(m.get_tool(nom)).fn


def _outlook(monkeypatch, body):
    c, outil = _service(monkeypatch, "outlook", "MailClient")
    c.create_draft.return_value = {"id": "d1"}
    outil("outlook")(op="draft", body=body, to=["m@f.example"], subject="s")
    return c.create_draft.call_args.kwargs["body_html"]


def _outlook_reponse(monkeypatch, body):
    c, outil = _service(monkeypatch, "outlook", "MailClient")
    c.create_reply_draft.return_value = {"id": "d2"}
    outil("outlook")(op="draft", body=body, reply_to="m1")
    return c.create_reply_draft.call_args.kwargs["body_html"]


def _teams(monkeypatch, body):
    c, outil = _service(monkeypatch, "teams", "TeamsClient")
    c.post_chat_message.return_value = {"id": "m1"}
    outil("teams_act")(op="post", chat_id="c1", body=body)
    return c.post_chat_message.call_args.kwargs["html"]


def _onenote(monkeypatch, body):
    c, outil = _service(monkeypatch, "onenote", "OneNoteClient")
    c.create_page.return_value = {"id": "p1"}
    outil("onenote_act")(op="create_page", section_id="s1", title="t", body=body)
    return c.create_page.call_args.kwargs["body_html"]


def _calendrier(monkeypatch, body):
    c, outil = _service(monkeypatch, "outlook_calendar", "CalendarClient")
    c.create_event.return_value = {"id": "e1"}
    outil("outlook_calendar_event")(op="create", subject="s", start="2026-10-15T14:00:00",
                                    end="2026-10-15T15:00:00", body=body)
    return c.create_event.call_args.kwargs["body_html"]


_CANAUX = {"outlook": _outlook, "outlook_reponse": _outlook_reponse, "teams_act": _teams,
           "onenote_act": _onenote, "outlook_calendar_event": _calendrier}


@pytest.mark.parametrize("canal", sorted(_CANAUX))
@pytest.mark.parametrize("piege, interdit", _PIEGES)
def test_le_corps_parti_est_le_rendu_commun(monkeypatch, canal, piege, interdit):
    assert interdit not in _CANAUX[canal](monkeypatch, piege)


@pytest.mark.parametrize("canal", sorted(_CANAUX))
def test_un_message_ordinaire_sort_comme_avant(monkeypatch, canal):
    from oto.tools.google.gmail.lib.gmail_client import _markdown_to_html_fragment

    avant = _markdown_to_html_fragment(_ORDINAIRE)
    normalise = re.sub(r'style="text-align: (\w+);"', r'style="text-align:\1"', avant)
    parti = _CANAUX[canal](monkeypatch, _ORDINAIRE)
    assert parti.replace(' rel="noopener noreferrer"', "") == normalise
    assert '<img alt="logo" src="https://exemple.test/l.png">' in parti


@pytest.mark.parametrize("canal", sorted(_CANAUX))
def test_l_image_en_ligne_cid_n_est_gardee_que_par_outlook(monkeypatch, canal):
    parti = _CANAUX[canal](monkeypatch, "![logo](cid:logo)")
    attendu = canal in ("outlook", "outlook_reponse")
    assert ('src="cid:logo"' in parti) is attendu


def test_cid_sans_image_refuse():
    from oto_mcp.markdown_assaini import rendu_markdown_assaini

    with pytest.raises(ValueError, match="cid=True"):
        rendu_markdown_assaini("x", cid=True)
    assert 'src="cid:logo"' in markdown_html("![l](cid:logo)", cid=True)
    assert "cid:" not in markdown_html("![l](cid:logo)")
