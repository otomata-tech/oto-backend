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
