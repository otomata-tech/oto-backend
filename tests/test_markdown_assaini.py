"""Le rendu markdown → HTML d'un message composé est ASSAINI, sur tous les chemins.

`gmail(op="draft")`, `gmail_act(op="send")` (et leurs réponses) et `email_send` rendent
le markdown par UNE fonction (`oto_mcp.markdown_assaini`) : le rendu d'oto-core laisse
passer le HTML brut, les attributs `attr_list` et les liens `javascript:`/`data:`.

Ce que ces tests tiennent :
- **assaini** : balise brute, gestionnaire d'événement, attribut `attr_list`, lien
  `javascript:`/`data:` — rien de cela ne sort, avec ou sans signature ;
- **un message ordinaire ne change pas** : titres, listes, liens, gras, tableau, bloc
  de code sortent comme le rendu d'oto-core, au `rel` des liens et à l'écriture du
  style d'alignement près ;
- `gmail` ne laisse plus GmailClient rendre lui-même le markdown (son rendu n'est pas
  assaini) : le HTML qu'il reçoit est déjà rendu.

Doublure de `GmailClient` : aucun appel réel à Google.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from oto_mcp.markdown_assaini import rendu_markdown_assaini

_PIEGES = [
    ("<script>alert(1)</script>", "<script"),
    ('<img src="x" onerror="alert(1)">', "onerror"),
    ('<b onclick="x()">y</b>', "onclick"),
    ('paragraphe\n{: onclick="x()" }', "onclick"),
    ('# titre {: onmouseover="x()" }', "onmouseover"),
    ("[clic](javascript:alert(1))", "javascript:"),
    ("[clic](data:text/html;base64,PHNjcmlwdD4=)", "data:"),
    ("![p](data:image/png;base64,AAAA)", "data:"),
    ('<iframe src="https://x.test"></iframe>', "<iframe"),
    ('<p style="display:none">caché</p>', "display:none"),
    ('| a |\n|---|\n| <span style="position:fixed">x</span> |', "position"),
]

_ORDINAIRE = (
    "# Titre\n\n## Sous-titre\n\nBonjour **Camille**, voici *le* point :\n\n"
    "- un\n- deux\n    - deux.a\n\n1. a\n2. b\n\n> cité\n\n"
    "[le lien](https://exemple.test/x?a=1&b=2) et `code`\n\n"
    "| a | b |\n|:-|-:|\n| 1 | 2 |\n\n---\n\nfin  \nligne\n\n```\nbloc\n```\n"
)


@pytest.mark.parametrize("images", [False, True])
@pytest.mark.parametrize("piege, interdit", _PIEGES)
def test_le_rendu_est_assaini(piege, interdit, images):
    assert interdit not in rendu_markdown_assaini(piege, images=images)


def test_un_message_ordinaire_sort_comme_le_rendu_d_oto_core():
    from oto.tools.google.gmail.lib.gmail_client import _markdown_to_html_fragment

    brut = _markdown_to_html_fragment(_ORDINAIRE)
    sur = rendu_markdown_assaini(_ORDINAIRE, images=True)
    normalise = re.sub(r'style="text-align: (\w+);"', r'style="text-align:\1"', brut)
    assert sur.replace(' rel="noopener noreferrer"', "") == normalise
    assert 'style="text-align:right"' in sur


def test_l_image_https_ne_passe_que_sur_demande():
    md = "![logo](https://exemple.test/l.png)"
    assert '<img alt="logo" src="https://exemple.test/l.png">' in rendu_markdown_assaini(
        md, images=True)
    assert "<img" not in rendu_markdown_assaini(md, images=False)


# ── gmail : le HTML parti est le HTML assaini ──────────────────────────────

class _FauxClient:
    def __init__(self, signature):
        self._signature = signature
        self.appels: list[tuple[str, dict]] = []

    def get_signature(self):
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


def _compose(monkeypatch, client, mode, **kw):
    from fastmcp import FastMCP
    from oto_mcp.tools import gmail as G

    monkeypatch.setattr(G, "_client_for_user", lambda account=None: client)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    m = FastMCP("t")
    G.register(m)
    if mode == "send":
        return asyncio.run(asyncio.run(m.get_tool("gmail_act")).fn(op="send", **kw))
    return asyncio.run(asyncio.run(m.get_tool("gmail")).fn(op="draft", **kw))


_PIEGE_GMAIL = ('Bonjour **Jeanne**\n\n<script>x()</script>\n\n<img src="x" onerror="y()">'
                '\n\npara\n{: onclick="z()" }\n\n[a](javascript:alert(1)) [b](data:text/html,x)')


@pytest.mark.parametrize("signature", ["<div>sig</div>", ""])
@pytest.mark.parametrize("mode,reply_to", [
    ("send", None), ("draft", None), ("send", "orig-1"), ("draft", "orig-1"),
])
def test_gmail_envoie_le_html_assaini(monkeypatch, signature, mode, reply_to):
    cli = _FauxClient(signature)
    _compose(monkeypatch, cli, mode, body=_PIEGE_GMAIL, to="j@x.test", subject="s",
             reply_to=reply_to)
    (_, kw), = cli.appels
    html = kw["html"]
    assert "<strong>Jeanne</strong>" in html
    for interdit in ("<script", "onerror", "onclick", "javascript:", "data:"):
        assert interdit not in html
    assert kw["body"] == _PIEGE_GMAIL  # la partie texte reste la source


def test_gmail_sans_signature_ni_markdown_reste_en_texte(monkeypatch):
    cli = _FauxClient("")
    _compose(monkeypatch, cli, "send", body="<b>x</b>", to="j@x.test", subject="s",
             markdown=False)
    (_, kw), = cli.appels
    assert kw["html"] is None and kw["markdown"] is False


def test_email_send_rend_par_la_meme_fonction(monkeypatch):
    from oto_mcp import email as E
    import oto_mcp.markdown_assaini as M

    vus = []
    vrai = M.rendu_markdown_assaini
    monkeypatch.setattr(M, "rendu_markdown_assaini",
                        lambda body, *, images: vus.append(images) or vrai(body, images=images))
    E.corps_markdown("**x**")
    assert vus == [False]
