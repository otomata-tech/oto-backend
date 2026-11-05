"""web_image (signaux 1389, 1394) — voir une image publique depuis son URL.

Ce qui se teste ici : l'image revient en contenu image MCP, après une ligne de texte
qui dit l'URL servie ; le format se lit dans les OCTETS (une page d'erreur étiquetée
`image/png` est refusée) ; une image au-delà du plafond est refusée, jamais coupée ;
la garde SSRF et le périmètre du projet mordent comme sur `web_read`, redirection
comprise ; et la sortie traverse la rédaction sans perdre son bloc image.
"""
from __future__ import annotations

import asyncio
import base64

import pytest
from mcp.types import ImageContent, TextContent

from oto_mcp import file_content, redaction
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import web as W

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):
            return deco(a[0])
        return deco


def _web_image(monkeypatch, fetch=None):
    reg = _Reg()
    if fetch is not None:
        monkeypatch.setattr(W, "_fetch_http", lambda url: fetch)
    W.register(reg)
    fn = reg.tools["web_image"]
    return lambda **kw: asyncio.run(fn(**kw))


def _lu(body: bytes, *, content_type="image/png", final_url="https://acme.fr/a.png",
        capped=False) -> dict:
    return {"ok": True, "status": 200, "html": "", "body": body,
            "content_type": content_type, "capped": capped,
            "final_url": final_url, "verdict": "read"}


@pytest.mark.parametrize("octets,fmt", [
    (_PNG, "png"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "jpeg"),
    (b"GIF89a" + b"\x00" * 16, "gif"),
    (b"GIF87a" + b"\x00" * 16, "gif"),
    (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16, "webp"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", None),
    (b"<!doctype html><html>404</html>", None),
    (b"", None),
])
def test_le_format_se_lit_dans_les_octets(octets, fmt):
    assert file_content.image_format(octets) == fmt


def test_l_image_revient_en_contenu_image_apres_une_ligne_de_texte(monkeypatch):
    voir = _web_image(monkeypatch, fetch=_lu(_PNG))
    out = voir(url="https://acme.fr/a.png")
    texte, image = out.content
    assert isinstance(texte, TextContent) and "https://acme.fr/a.png" in texte.text
    assert "png" in texte.text and str(len(_PNG)) in texte.text
    assert isinstance(image, ImageContent) and image.mimeType == "image/png"
    assert base64.b64decode(image.data) == _PNG
    assert out.structured_content is None, \
        "pas de canal structuré : un client qui le préfère au contenu perdrait l'image"


def test_une_redirection_est_dite(monkeypatch):
    voir = _web_image(monkeypatch, fetch=_lu(_PNG, final_url="https://cdn.acme.fr/a.png"))
    ligne = voir(url="https://acme.fr/a.png").content[0].text
    assert "https://cdn.acme.fr/a.png" in ligne and "redirect" in ligne


def test_une_page_etiquetee_image_est_refusee(monkeypatch):
    """Le Content-Type ne fait pas foi : une page d'erreur servie 200 en `image/png`
    n'atteint pas le modèle comme une image."""
    voir = _web_image(monkeypatch, fetch=_lu(b"<html>not found</html>"))
    with pytest.raises(McpError) as e:
        voir(url="https://acme.fr/a.png")
    assert "not a png/jpeg/gif/webp image" in str(e.value)
    assert "image/png" in str(e.value) and "web_read" in str(e.value)


def test_une_image_au_plafond_est_refusee_jamais_coupee(monkeypatch):
    voir = _web_image(monkeypatch, fetch=_lu(_PNG, capped=True))
    with pytest.raises(McpError) as e:
        voir(url="https://acme.fr/enorme.png")
    assert "over 3 MB" in str(e.value) and "never cut" in str(e.value)


def test_un_fetch_en_echec_dit_son_verdict(monkeypatch):
    voir = _web_image(monkeypatch, fetch={"ok": False, "verdict": "HTTP 403", "status": 403})
    with pytest.raises(McpError) as e:
        voir(url="https://acme.fr/a.png")
    assert "HTTP 403" in str(e.value)


def test_une_adresse_interne_est_refusee_avant_toute_connexion(monkeypatch):
    """Le vrai `_fetch_http` : la garde SSRF de `web_read` mord ici aussi."""
    from oto_mcp import egress
    monkeypatch.setattr(egress, "resolved_addresses", lambda h, p: {"169.254.169.254"})
    monkeypatch.setattr(W.requests, "get",
                        lambda *a, **k: pytest.fail("aucune connexion vers l'interne"))
    voir = _web_image(monkeypatch)
    with pytest.raises(McpError) as e:
        voir(url="https://metadata.example.com/a.png")
    assert "adresse interne" in str(e.value)


def test_le_perimetre_mord_sur_l_url_finale(monkeypatch):
    vues = []

    def _refuse(url, per):
        vues.append(url)
        if url and "exclu" in str(url):
            raise McpError(W.ErrorData(code=W.INVALID_REQUEST, message="hors périmètre"))

    monkeypatch.setattr(W.url_perimeter, "refuse_if_excluded", _refuse)
    voir = _web_image(monkeypatch, fetch=_lu(_PNG, final_url="https://exclu.fr/a.png"))
    with pytest.raises(McpError):
        voir(url="https://acme.fr/a.png")
    assert vues == ["https://acme.fr/a.png", "https://exclu.fr/a.png"]


def test_la_redaction_laisse_passer_le_bloc_image(monkeypatch):
    """Le texte n'est pas du JSON : la rédaction n'a rien à réémettre, et ne
    reconstruit donc jamais une sortie texte seule qui aurait perdu l'image."""
    voir = _web_image(monkeypatch, fetch=_lu(_PNG))
    out = voir(url="https://acme.fr/a.png")
    assert redaction.extract_payload(out) is None
    assert redaction.redact_payload("web", None) is redaction.PASSTHROUGH
    assert not redaction.sert_du_vide(out)
