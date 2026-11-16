"""`email_send(markdown=True)` : le corps est du markdown, rendu comme `gmail` le rend,
puis ASSAINI — le mail part sous la charte d'une marque.

Ce que ces tests tiennent :
- **par défaut, rien ne change** : sans `markdown`, le corps est échappé comme avant
  (le rendu est identique, octet pour octet, à celui d'un appel sans le paramètre) ;
- **le même rendu que gmail** : gras, liens, listes, titres, tableaux ;
- **assaini** : balise brute, gestionnaire d'événement, attribut `attr_list`, image,
  lien `javascript:` — rien de cela ne sort ;
- les trois chemins de l'outil (rendu servi, mailer commun qui re-rend, file différée)
  reçoivent le paramètre.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import email as E

_MD = "bonjour **Camille**,\n\n- point un\n- point deux\n\n[le lien](https://exemple.test/x)"


def test_par_defaut_le_corps_reste_du_texte_echappe():
    corps = "**pas du gras**\n\n<b>balise</b>"
    assert E.render_composed_email(corps) == E.render_composed_email(corps, markdown=False)
    html = E.render_composed_email(corps)
    assert "**pas du gras**" in html and "&lt;b&gt;balise&lt;/b&gt;" in html
    assert "<strong>" not in html


def test_le_markdown_est_rendu_comme_gmail():
    html = E.render_composed_email(_MD, markdown=True)
    assert "<strong>Camille</strong>" in html
    assert "<li>point un</li>" in html
    assert 'href="https://exemple.test/x"' in html
    assert "**" not in html


def test_tableau_et_titre_passent():
    html = E.corps_markdown("# Titre\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<h1>Titre</h1>" in html and "<td>1</td>" in html


@pytest.mark.parametrize("piege, interdit", [
    ("<script>alert(1)</script>", "<script"),
    ("<b onclick=\"x()\">y</b>", "onclick"),
    ("paragraphe\n{: onmouseover=\"x()\" }", "onmouseover=\"x"),
    ("[clic](javascript:alert(1))", "javascript:"),
    ("![pixel](https://traceur.test/p.png)", "<img"),
    ("<iframe src=\"https://x.test\"></iframe>", "<iframe"),
    ("<p style=\"display:none\">caché</p>", "display:none"),
])
def test_le_html_produit_est_assaini(piege, interdit):
    assert interdit not in E.corps_markdown(piege)


def test_les_paragraphes_portent_la_marge_de_la_charte():
    html = E.corps_markdown("un\n\ndeux")
    assert html.count(f'<p style="{E._charte.PARA}">') == 2


def test_l_apercu_est_le_texte_sans_syntaxe():
    html = E.render_composed_email("**bonjour** Camille,\n\nsuite", markdown=True)
    assert "bonjour Camille," in html.split("</div>", 1)[0]


def test_l_envoi_par_le_mailer_rend_le_markdown(monkeypatch):
    parti = {}
    monkeypatch.setattr(E, "_send", lambda to, subject, html, **kw: parti.update(html=html) or True)
    assert E.send_composed_email("a@x.test", "objet", _MD, reply_to="r@x.test", markdown=True)
    assert "<strong>Camille</strong>" in parti["html"]


# ── l'outil ────────────────────────────────────────────────────────────────

@pytest.fixture
def outil():
    from fastmcp import FastMCP
    from oto_mcp.tools import email as T
    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool("email_send"))


_ROUTE_COMMUNE = {"org_id": None, "connector": None, "transport": "mailer",
                  "from_email": None, "from_name": None, "reply_to": None,
                  "quiet_hours": None, "footer": None}


@pytest.fixture
def commun(monkeypatch):
    from oto_mcp.tools import email as T
    monkeypatch.setattr(T, "_resolve_route", lambda f: ("logto:sa", dict(_ROUTE_COMMUNE)))
    monkeypatch.setattr(T.config, "front_for", lambda sub: (None, None))
    monkeypatch.setattr(T, "_garder_plafond_commun", lambda sub, n: None)
    monkeypatch.setattr(T, "_metrer_commun", lambda n: None)
    monkeypatch.setattr(T.mailer, "_mail_from", lambda: "oto@instance.test")
    return T


def _envoyer(outil, **kw):
    base = dict(ctx=None, to="prospect@ailleurs.test", subject="objet", body=_MD)
    base.update(kw)
    return outil.fn(**base)


def test_le_parametre_est_au_schema_et_faux_par_defaut(outil):
    prop = outil.parameters["properties"]["markdown"]
    assert prop["type"] == "boolean" and prop["default"] is False


def test_dry_run_rend_le_markdown(outil, commun):
    out = _envoyer(outil, dry_run=True, markdown=True)
    assert "<strong>Camille</strong>" in out["html"]
    assert "**Camille**" in _envoyer(outil, dry_run=True)["html"]


def test_l_envoi_immediat_commun_passe_le_parametre(outil, commun, monkeypatch):
    vu = {}
    monkeypatch.setattr(commun.mailer, "send_composed_email",
                        lambda to, subject, body, **kw: vu.update(kw) or True)
    assert _envoyer(outil, force_now=True, markdown=True)["sent"]
    assert vu["markdown"] is True


def test_l_envoi_differe_stocke_le_html_rendu(outil, commun, monkeypatch):
    vu = {}
    monkeypatch.setattr(commun.db, "enqueue_scheduled_email",
                        lambda **kw: vu.update(kw) or 7)
    assert _envoyer(outil, send_at="2099-01-01T08:00", markdown=True)["scheduled"]
    assert "<strong>Camille</strong>" in vu["body_html"]
