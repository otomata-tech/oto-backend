"""Décision de rendu d'un contenu de fichier pour un agent MCP (texte vs binaire).

Partagé par les tools qui ramènent le contenu d'un fichier (PJ Gmail, fichier
Drive…) : un petit contenu textuel est renvoyé INLINE (l'agent le lit), un binaire
ou un gros fichier passe par une URL signée (`media_store.upload_private`). Le seuil
inline évite d'injecter trop de tokens dans le contexte de l'agent.
"""
from __future__ import annotations

from typing import Optional

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


class MediaUnavailable(RuntimeError):
    """Le stockage temporaire (S3) requis pour servir un binaire/gros fichier en
    URL signée est indisponible — l'appelant traduit en erreur de tool."""


def render_for_agent(data: bytes, filename: str, mime: str, *, sub: str, prefix: str) -> dict:
    """Rendu d'un contenu de fichier pour un agent MCP — **home unique** de la
    règle inline-vs-URL (ex-duplication gmail/drive/slack).

    - petit contenu textuel (≤ `INLINE_TEXT_CAP`) → INLINE
      `{encoding: "text", content}` (l'agent le lit) ;
    - binaire ou volumineux → dépôt privé S3 + **URL signée temporaire**
      `{encoding: "url", url, expires_in}` (`media_store.upload_private`).

    `prefix` = préfixe de clé S3 (`gmail-attachments`, `drive-files`,
    `slack-files`…) ; `sub` = propriétaire du dépôt. **Appel BLOQUANT** (I/O S3) :
    invoquer depuis un handler sync (threadpool) ou via `asyncio.to_thread`.
    Lève `MediaUnavailable` si le stockage est absent (S3 non configuré).
    """
    out = {"filename": filename, "mimeType": mime, "size": len(data)}
    text = as_text(data, mime)
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
