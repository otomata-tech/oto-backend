"""Chaque email de plateforme part avec une partie `text/plain` tirée de son HTML.

Un email en HTML seul est un signal anti-spam classique : des invitations arrivaient
en spam chez des destinataires extérieurs (06/11/2026, en-têtes relus : SPF, DKIM et
DMARC passent, mais `Content-Type: text/html` seul). `email.version_texte` rend le
texte ; les deux transports de l'instance (relais mailer, API TEM) l'envoient.
"""
from __future__ import annotations

import sys
import types

from oto_mcp import email as E

_GABARIT = (
    '<!DOCTYPE html><html lang="en"><head><title>Marque</title><style>p{color:red}</style></head>'
    '<body><div style="display:none;max-height:0">aperçu caché&#8203;&#847;&#8203;&#847;'
    '</div><table><tr><td>Marque</td></tr><tr><td>'
    '<p style="margin:0">alice@x.fr invites you to join <strong>Projet A</strong>.</p>'
    '<table><tr><td><a href="https://app.test/invitation/inv_1?otl=A&amp;b=1">join</a>'
    '</td></tr></table><p>https://app.test/invitation/inv_1?otl=A&amp;b=1</p>'
    '<p>Marque &middot; marque.test<br>reply if it looks wrong.</p></td></tr></table>'
    '</body></html>')


def test_le_texte_garde_les_mots_et_le_lien_en_clair():
    t = E.version_texte(_GABARIT)
    assert "alice@x.fr invites you to join Projet A." in t
    assert "join: https://app.test/invitation/inv_1?otl=A&b=1" in t
    assert t.count("https://app.test/invitation/inv_1") == 1   # pas répété en clair
    assert "Marque · marque.test\nreply if it looks wrong." in t


def test_le_preheader_cache_le_head_et_les_invisibles_ne_passent_pas():
    t = E.version_texte(_GABARIT)
    assert "aperçu caché" not in t
    assert "color:red" not in t
    assert "​" not in t and "͏" not in t
    assert "\n\n\n" not in t


def test_un_lien_dont_le_libelle_est_l_url_n_est_pas_double():
    t = E.version_texte('<p><a href="https://x.test/a">https://x.test/a</a></p>')
    assert t == "https://x.test/a"


def _fake_httpx(monkeypatch) -> list[dict]:
    envois: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

    def post(url, headers=None, json=None, timeout=None):
        envois.append(json)
        return _Resp()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(post=post))
    return envois


def test_le_relais_recoit_le_texte(monkeypatch):
    for n in ("OTO_TEM_SECRET_KEY", "OTO_TEM_PROJECT_ID", "OTO_TEM_REGION"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("OTO_MAILER_URL", "https://relais.test/api/send")
    monkeypatch.setenv("OTO_MAILER_SEND_BEARER", "b")
    monkeypatch.setenv("OTO_MAIL_FROM", "Oto <oto@otomata.tech>")
    envois = _fake_httpx(monkeypatch)
    assert E._send("a@x.fr", "s", _GABARIT) is True
    assert envois[0]["html"] == _GABARIT
    assert "join: https://app.test/" in envois[0]["text"]


def test_tem_recoit_le_texte(monkeypatch):
    monkeypatch.delenv("OTO_MAILER_URL", raising=False)
    monkeypatch.setenv("OTO_TEM_SECRET_KEY", "k")
    monkeypatch.setenv("OTO_TEM_PROJECT_ID", "p")
    monkeypatch.setenv("OTO_TEM_REGION", "fr-par")
    monkeypatch.setenv("OTO_MAIL_FROM", "Oto <oto@otomata.tech>")
    envois = _fake_httpx(monkeypatch)
    assert E._send("a@x.fr", "s", _GABARIT) is True
    assert "Projet A" in envois[0]["text"] and envois[0]["html"] == _GABARIT
