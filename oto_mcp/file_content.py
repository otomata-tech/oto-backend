"""Décision de rendu d'un contenu de fichier pour un agent MCP (texte vs binaire).

Partagé par les tools qui ramènent le contenu d'un fichier (PJ Gmail, fichier
Drive…) : un petit contenu textuel est renvoyé INLINE (l'agent le lit), un binaire
ou un gros fichier passe par une URL signée (`media_store.upload_private`). Le seuil
inline évite d'injecter trop de tokens dans le contexte de l'agent.

Un TABLEUR `.xlsx` est un binaire qu'on sait lire : il est rendu INLINE en CSV par
feuille (`file_extract.render_xlsx_csv`, oto#181), borné au même seuil inline.

Un PDF aussi : son TEXTE est rendu INLINE (`file_extract.extract`), le PDF original
joint en URL signée. Servi seulement en URL, il n'était lisible par aucun agent dont
le bac à sable ne joint pas notre stockage — or c'est le cas par défaut d'un client
qui filtre sa sortie réseau.

Une PIÈCE JOINTE d'e-mail (outlook, gmail) passe par `render_attachment_for_agent` :
une image, ou un PDF scanné dont les pages sont des JPEG, revient en BLOC IMAGE MCP
(lu par le client sans réseau, rien n'est déposé) ; le reste suit `render_for_agent`.
"""
from __future__ import annotations

import io
from typing import Optional, Union

from . import file_extract
from .file_extract import (DEFAULT_SHEET_ROWS, NOT_A_SPREADSHEET, SpreadsheetError,
                           is_spreadsheet, render_xlsx_csv)

# Au-delà de cette taille, même un contenu textuel part en URL signée plutôt que
# d'être injecté dans le contexte de l'agent (texte = tokens).
INLINE_TEXT_CAP = 256 * 1024  # 256 Ko

_TEXTUAL_MIME = {
    "application/json", "application/ld+json", "application/xml",
    "application/csv", "application/x-ndjson", "application/markdown",
    "application/x-yaml", "application/yaml",
}


def as_text(data: bytes, mime: str) -> Optional[str]:
    """Renvoie le contenu décodé en UTF-8 si le fichier est textuel, sinon None.

    Un type `text/*` ou JSON/CSV/XML/YAML est traité comme texte ; pour un type
    inconnu, on décode quand même et on accepte si c'est de l'UTF-8 propre sans
    octet NUL (heuristique : un binaire — PDF, image — échoue le decode ou
    contient des NUL → part en URL signée)."""
    m = (mime or "").split(";")[0].strip().lower()
    looks_text = m.startswith("text/") or m in _TEXTUAL_MIME
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if looks_text or "\x00" not in s:
        return s
    return None


# Plafond d'une image servie EN CONTENU (bloc image MCP) : celui de `web_image`. Au-delà,
# le bloc base64 pèse trop dans le contexte du client ; l'image part en URL signée.
INLINE_IMAGE_CAP = 3_000_000
# Pages d'un PDF scanné rendues en image, au plus. Au-delà, le scan part en URL signée.
SCAN_PAGES_CAP = 5


def image_format(octets: bytes) -> Optional[str]:
    """`png` | `jpeg` | `gif` | `webp` read from the MAGIC BYTES, else None.

    The `Content-Type` header is not trusted: a server can label anything
    `image/png`, and an HTML error page served 200 must not reach the model as an
    image. SVG is deliberately absent: it is XML (scripts, external references),
    and the MCP clients render only raster formats."""
    if octets.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if octets.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if octets[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if octets[:4] == b"RIFF" and octets[8:12] == b"WEBP":
        return "webp"
    return None


class MediaUnavailable(RuntimeError):
    """Le stockage temporaire (S3) requis pour servir un binaire/gros fichier en
    URL signée est indisponible — l'appelant traduit en erreur de tool."""


def render_for_agent(data: bytes, filename: str, mime: str, *, sub: str, prefix: str,
                     sheet: Optional[Union[int, str]] = None,
                     max_rows: Optional[int] = None) -> dict:
    """Rendu d'un contenu de fichier pour un agent MCP — **home unique** de la
    règle inline-vs-URL (ex-duplication gmail/drive/slack).

    - petit contenu textuel (≤ `INLINE_TEXT_CAP`) → INLINE
      `{encoding: "text", content}` (l'agent le lit) ;
    - binaire ou volumineux → dépôt privé S3 + **URL signée temporaire**
      `{encoding: "url", url, expires_in}` (`media_store.upload_private`) ;
    - tableur `.xlsx` → INLINE en CSV par feuille : `{encoding: "text",
      format: "csv", content, sheets, sheet_names, truncated}`, restreint à la
      feuille `sheet` (nom ou index) et borné à `max_rows` lignes par feuille et à
      `INLINE_TEXT_CAP` caractères. `sheet`/`max_rows` sur un autre format lèvent
      `SpreadsheetError` (`not_a_spreadsheet`) : jamais ignorés en silence.
      TOUJOURS, comme pour un PDF, le fichier BRUT est AUSSI déposé en privé :
      `raw_url` + `raw_expires_in` (signal #1152 : un tableur tronqué n'avait aucun
      chemin vers ses données ; #1347 : même entier, le CSV perd formats, largeurs et
      types d'un classeur à modifier). Le CSV inline reste : c'est lui que
      l'agent lit. Stockage indisponible → `raw_unavailable` le DIT, le rendu inline
      n'échoue pas pour autant ;
    - PDF → INLINE son texte extrait : `{encoding: "text", format: "pdf-text",
      content, pages, truncated}` + TOUJOURS `raw_url` vers l'original (la mise en
      page et les images ne passent pas dans le texte). Sans texte extractible
      (scanné, protégé, illisible) → URL signée comme un binaire, et
      `text_unavailable` dit pourquoi.

    `prefix` = préfixe de clé S3 (`gmail-attachments`, `drive-files`,
    `slack-files`…) ; `sub` = propriétaire du dépôt. **Appel BLOQUANT** (I/O S3) :
    invoquer depuis un handler sync (threadpool) ou via `asyncio.to_thread`.
    Lève `MediaUnavailable` si le stockage est absent (S3 non configuré), et
    `SpreadsheetError` (statut nommé) sur un tableur trop gros, forgé ou illisible.
    """
    out = {"filename": filename, "mimeType": mime, "size": len(data)}
    if is_spreadsheet(filename, mime):
        r = render_xlsx_csv(data, sheet=sheet,
                            max_rows=DEFAULT_SHEET_ROWS if max_rows is None else max_rows,
                            max_chars=INLINE_TEXT_CAP)
        out.update(encoding="text", format="csv", content=r.text,
                   sheets=[{"index": s.index, "name": s.name, "rows_total": s.rows_total,
                            "rows_rendered": s.rows_rendered, "truncated": s.truncated}
                           for s in r.sheets],
                   sheet_names=list(r.sheet_names), truncated=r.truncated)
        # ALWAYS the original, truncated or not (#1347): the CSV loses formats,
        # widths and types — a workbook to edit and send back needs its bytes.
        _joindre_le_brut(out, data, filename, mime, sub=sub, prefix=prefix)
        return out
    if sheet is not None or max_rows is not None:
        raise SpreadsheetError(
            NOT_A_SPREADSHEET,
            f"`sheet`/`max_rows` ne valent que pour un tableur .xlsx — « {filename} » "
            f"({mime or 'type inconnu'}) n'en est pas un : relance sans ces paramètres.")
    pdf = file_extract.is_pdf(filename, mime)
    if pdf:
        ex = file_extract.extract(data, filename, mime)
        if ex.ok:
            out.update(encoding="text", format="pdf-text",
                       content=ex.text[:INLINE_TEXT_CAP], pages=ex.pages,
                       truncated=len(ex.text) > INLINE_TEXT_CAP)
            _joindre_le_brut(out, data, filename, mime, sub=sub, prefix=prefix)
            return out
        out["text_unavailable"] = f"{ex.status} : {ex.detail}"
    # Un PDF sans texte extractible part en URL : ses octets, même décodables, ne
    # sont pas un texte à lire.
    text = None if pdf else as_text(data, mime)
    if text is not None and len(data) <= INLINE_TEXT_CAP:
        out.update(encoding="text", content=text)
        return out
    from . import media_store
    try:
        url = media_store.upload_private(prefix, sub, data, mime, filename)
    except media_store.MediaError as e:
        raise MediaUnavailable(
            f"Fichier binaire/volumineux ({len(data)} octets) : stockage temporaire "
            f"indisponible pour produire une URL ({e}). Configurer OTO_MCP_S3_*."
        )
    out.update(encoding="url", url=url, expires_in=media_store.presign_expiry())
    return out


def _joindre_le_brut(out: dict, data: bytes, filename: str, mime: str, *, sub: str,
                     prefix: str) -> None:
    """Dépose le fichier ORIGINAL (tableur rendu en CSV, PDF rendu en texte) et en rend
    l'URL signée.

    Un stockage absent ne fait pas échouer le rendu : le texte inline est juste et
    utile. Mais l'absence se DIT (`raw_unavailable`) — jamais un `raw_url` manquant
    en silence, qu'un agent lirait « pas de fichier complet »."""
    from . import media_store
    try:
        out["raw_url"] = media_store.upload_private(prefix, sub, data, mime, filename)
    except media_store.MediaError as e:
        suite = ("relance avec `sheet` et un `max_rows` plus haut pour lire plus de "
                 "lignes." if out.get("format") == "csv" else
                 "le texte ci-dessus est celui du document.")
        out["raw_unavailable"] = (
            f"fichier brut non déposé : stockage temporaire indisponible ({e}) ; {suite}")
        return
    out["raw_expires_in"] = media_store.presign_expiry()


def render_attachment_for_agent(data: bytes, filename: str, mime: str, *, sub: str,
                                prefix: str, sheet: Optional[Union[int, str]] = None,
                                max_rows: Optional[int] = None):
    """Rendu d'une PIÈCE JOINTE d'e-mail (outlook, gmail) : `render_for_agent`, sauf
    pour ce qu'un modèle lit en image.

    Une URL signée sur notre stockage est injoignable d'un bac à sable qui filtre sa
    sortie réseau (le cas par défaut d'un client) : une image, ou un scan sans texte,
    servie ainsi n'était lisible par personne. D'où, SANS dépôt (rien n'est conservé) :

    - une image png/jpeg/gif/webp (format lu dans les OCTETS) ≤ `INLINE_IMAGE_CAP` →
      `ToolResult` : une ligne de texte (nom, type, taille) + le bloc image ;
    - un PDF sans texte extractible (scan) dont chaque image de page est un JPEG
      (`/DCTDecode` : le flux brut EST le fichier JPEG, aucun décodage), au plus
      `SCAN_PAGES_CAP` pages et `INLINE_IMAGE_CAP` octets d'images → `ToolResult` :
      une ligne + un bloc image par image de page.

    Le reste suit `render_for_agent` (dict). Une image ou un scan qui n'a pas pu être
    rendu en image le DIT (`image_unavailable` : trop gros, trop de pages, encodage
    non convertible sans moteur de rendu) — jamais une URL seule sans raison.

    La ligne de texte est du texte libre, pas du JSON : comme `web_image`, aucun
    middleware ne réémet alors la réponse depuis un JSON qui perdrait le bloc image."""
    raison = None
    if sheet is None and max_rows is None and not is_spreadsheet(filename, mime):
        fmt = image_format(data)
        if fmt is not None:
            if len(data) <= INLINE_IMAGE_CAP:
                return _en_images(filename, mime, len(data), f"{fmt} image",
                                  [(fmt, data)])
            raison = (f"image of {len(data)} bytes, over the "
                      f"{INLINE_IMAGE_CAP // 1_000_000} MB served as image content")
        elif file_extract.is_pdf(filename, mime):
            ex = file_extract.extract(data, filename, mime)
            if ex.status == file_extract.EMPTY:
                images, raison = _jpeg_des_pages(data)
                if images is not None:
                    return _en_images(
                        filename, mime, len(data),
                        f"scanned PDF without text, {ex.pages} page(s), "
                        f"{len(images)} page image(s) in JPEG", images)
    out = render_for_agent(data, filename, mime, sub=sub, prefix=prefix, sheet=sheet,
                           max_rows=max_rows)
    if raison is not None:
        out["image_unavailable"] = f"{raison} — served as a signed URL instead."
    return out


def _en_images(filename: str, mime: str, size: int, quoi: str, images: list):
    from fastmcp.tools.tool import ToolResult
    from fastmcp.utilities.types import Image
    from mcp.types import TextContent
    ligne = (f"Attachment « {filename} » ({mime or 'unknown type'}, {size} bytes): "
             f"{quoi}, below as image content. Nothing was stored.")
    return ToolResult(content=[TextContent(type="text", text=ligne),
                               *(Image(data=octets, format=fmt).to_image_content()
                                 for fmt, octets in images)])


def _filtres(x) -> list[str]:
    f = x.get("/Filter")
    if f is None:
        return []
    f = f.get_object()
    return [str(v.get_object()) for v in f] if isinstance(f, list) else [str(f)]


def _jpeg_des_pages(data: bytes) -> tuple[Optional[list], Optional[str]]:
    """Les images de page d'un PDF scanné, en JPEG tels que le PDF les porte : `(images,
    None)`, ou `(None, raison)` dès qu'une page ne se rend pas ainsi.

    Pur pypdf, sans Pillow : seul un flux `/Filter /DCTDecode` (sans autre filtre) est
    un fichier JPEG tel quel — on en lit les octets ENCODÉS (`_data`), jamais décodés.
    CCITTFax, JBIG2, JPX ou des pixels bruts (Flate) demanderaient un moteur de rendu
    que le paquet n'embarque pas : le scan reste alors en URL, raison dite."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            reader.decrypt("")
        n = len(reader.pages)
        if n > SCAN_PAGES_CAP:
            return None, (f"scanned PDF of {n} pages, over the {SCAN_PAGES_CAP} pages "
                          "served as images")
        images, total = [], 0
        for i, page in enumerate(reader.pages, start=1):
            ressources = (page.get("/Resources") or {})
            ressources = ressources.get_object() if ressources else {}
            xobjets = ressources.get("/XObject")
            xobjets = xobjets.get_object() if xobjets is not None else {}
            for ref in xobjets.values():
                x = ref.get_object()
                if x.get("/Subtype") != "/Image":
                    continue
                filtres = _filtres(x)
                if filtres != ["/DCTDecode"]:
                    return None, (f"page {i} image is encoded "
                                  f"{'+'.join(filtres) or 'raw'}, not JPEG: it cannot "
                                  "be rendered without an imaging engine")
                octets = bytes(x._data)
                if image_format(octets) != "jpeg":
                    return None, f"page {i} image is not a readable JPEG stream"
                total += len(octets)
                if total > INLINE_IMAGE_CAP:
                    return None, (f"page images over the "
                                  f"{INLINE_IMAGE_CAP // 1_000_000} MB served as image "
                                  "content")
                images.append(("jpeg", octets))
    # noqa: SILENT — PDF illisible : la raison nommée est rendue à l'appelant
    except Exception as e:
        return None, f"page images unreadable ({type(e).__name__})"
    if not images:
        return None, "no page image found in this PDF"
    return images, None
