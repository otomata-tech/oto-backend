"""Résolveur « fichier côté oto » — désigner un fichier déjà accessible par oto et
en récupérer les OCTETS côté serveur (oto-backend#60).

Un agent MCP n'a pas de système de fichiers : il ne peut pas désigner un PDF par un
chemin disque. Ce module résout une **référence typée** vers le contenu binaire d'un
fichier qu'oto sait atteindre — pièce Gmail, fichier Drive, URL — pour qu'un tool
(upload Pennylane, etc.) l'ingère sans passer par le disque.

Couche backend-core (ADR 0004) : imports lazy des clients connecteurs, résolution
des credentials Google via `google_oauth`. Pas de fallback : une source illisible
ou inconnue lève `FileSourceError` (traduite en erreur actionnable par l'appelant).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import access, google_oauth

# Plafond par défaut : Pennylane accepte 100 Mo, mais on borne pour ne pas charger
# un fichier géant en RAM par mégarde. Override par appel via `max_bytes`.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 Mo


class FileSourceError(RuntimeError):
    """Référence de fichier invalide, source illisible, ou dépassement de taille."""


@dataclass
class ResolvedFile:
    data: bytes
    filename: str
    mime: str


def _google_creds(account: Optional[str]):
    sub = access.current_user_sub_or_raise()
    return google_oauth.credentials_for(sub, account=account)


def _from_drive(src: dict) -> ResolvedFile:
    file_id = src.get("file_id")
    if not file_id:
        raise FileSourceError("source drive : `file_id` requis.")
    from oto.tools.google.drive.lib.drive_client import DriveClient
    client = DriveClient(credentials=_google_creds(src.get("account")))
    att = client.get_file_bytes(str(file_id))
    return ResolvedFile(att["data"], att.get("filename") or str(file_id),
                        att.get("mimeType") or "application/octet-stream")


def _from_gmail(src: dict) -> ResolvedFile:
    message_id, filename = src.get("message_id"), src.get("filename")
    if not message_id or not filename:
        raise FileSourceError("source gmail : `message_id` et `filename` requis.")
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    client = GmailClient(credentials=_google_creds(src.get("account")))
    att = client.get_attachment(message_id, filename, int(src.get("index", 0)))
    return ResolvedFile(att["data"], att.get("filename") or filename,
                        att.get("mimeType") or "application/octet-stream")


def _assert_public_host(host: str) -> None:
    """Anti-SSRF : refuse une cible qui résout vers une IP non-publique (loopback,
    privée, link-local — dont les métadonnées cloud 169.254.169.254 —, reserved,
    multicast). Sans ce garde-fou, un agent pourrait faire lire au serveur ses
    services internes (`localhost:9103`) ou l'IMDS. Toutes les IP résolues du host
    doivent être globales."""
    import ipaddress
    import socket
    if not host:
        raise FileSourceError("source url : hôte manquant.")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise FileSourceError(f"source url : hôte non résolu ({e}).")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise FileSourceError(
                f"source url : cible non autorisée ({ip}) — adresse interne/réservée.")


def _from_url(src: dict, max_bytes: int) -> ResolvedFile:
    url = src.get("url")
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise FileSourceError("source url : `url` http(s) requise.")
    import os
    from urllib.parse import unquote, urlsplit

    import httpx
    host = urlsplit(str(url)).hostname
    _assert_public_host(host or "")
    # Redirections DÉSACTIVÉES : un 3xx pourrait pointer une IP interne (le garde-fou
    # ci-dessus ne valide que l'hôte initial). Nos sources légitimes (URLs signées S3,
    # gmail_get_attachment) sont directes → pas de redirect attendu.
    with httpx.Client(follow_redirects=False, timeout=60.0) as c:
        with c.stream("GET", url) as r:
            if 300 <= r.status_code < 400:
                raise FileSourceError(
                    "source url : redirection non suivie (anti-SSRF) — fournir l'URL finale directe.")
            r.raise_for_status()
            declared = r.headers.get("content-length")
            if declared and int(declared) > max_bytes:
                raise FileSourceError(
                    f"fichier distant trop volumineux ({declared} > {max_bytes} octets).")
            chunks, total = [], 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FileSourceError(f"fichier distant > {max_bytes} octets.")
                chunks.append(chunk)
            data = b"".join(chunks)
            mime = (r.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    name = os.path.basename(unquote(urlsplit(str(url)).path)) or "file"
    return ResolvedFile(data, name, mime)


_RESOLVERS = {"drive": _from_drive, "gmail": _from_gmail}


def resolve(source: Any, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ResolvedFile:
    """Résout une référence de fichier côté oto vers ses octets + métadonnées.

    `source` = dict `{"kind": "drive"|"gmail"|"url", …}` :
      - drive : `{file_id, account?}`
      - gmail : `{message_id, filename, index?, account?}`
      - url   : `{url}` (http/https, suit les redirections)
    Lève `FileSourceError` si `kind` manque/inconnu, source illisible, ou taille
    dépassée. Borne la taille à `max_bytes` (charge en RAM)."""
    if not isinstance(source, dict):
        raise FileSourceError("source : objet attendu `{kind, …}`.")
    kind = source.get("kind")
    if kind == "url":
        rf = _from_url(source, max_bytes)
    else:
        fn = _RESOLVERS.get(kind)
        if fn is None:
            raise FileSourceError(
                f"source `kind`={kind!r} inconnu (attendu : drive, gmail, url).")
        try:
            rf = fn(source)
        except FileSourceError:
            raise
        except Exception as e:  # erreur client connecteur (Drive/Gmail) → actionnable
            raise FileSourceError(str(e))
    if len(rf.data) > max_bytes:
        raise FileSourceError(f"fichier de {len(rf.data)} octets > plafond {max_bytes}.")
    return rf
