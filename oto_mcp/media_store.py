"""Stockage d'images publiques (avatars user, logos d'org) sur Scaleway Object
Storage (S3-compatible).

Couche backend-core (ADR 0004) : possède le client S3, la validation et la
construction d'URL publique. N'importe jamais l'adaptateur REST. Les URLs
produites sont **publiques** (pas des secrets) → seule l'URL est persistée en
DB (colonnes `users.avatar_url` / `orgs.logo_url`), jamais dans le coffre chiffré.

Config 100% par env de process (cohérent `OTO_CONFIG_DISABLE_SOPS=1`) :
- `OTO_MCP_S3_ENDPOINT`         ex. https://s3.fr-par.scw.cloud
- `OTO_MCP_S3_REGION`           défaut "fr-par"
- `OTO_MCP_S3_BUCKET`           ex. oto-media
- `OTO_MCP_S3_ACCESS_KEY` / `OTO_MCP_S3_SECRET_KEY`  clé API Scaleway (Object Storage)
- `OTO_MCP_S3_PUBLIC_BASE_URL`  (optionnel) base publique/CDN ; sinon dérivée virtual-hosted
- `OTO_MCP_S3_MAX_IMAGE_BYTES`  (optionnel) défaut 2 Mo

Import paresseux de boto3 + client mis en cache au 1er usage : le module se
charge proprement même si le stockage n'est pas configuré (l'erreur ne tombe
qu'à l'upload, jamais au boot ni sur `/api/me`).
"""
from __future__ import annotations

import hashlib
import os
from urllib.parse import quote, urlsplit

# Type sniffé → extension stockée. On ne fait JAMAIS confiance au Content-Type
# déclaré par le client : l'extension dérive des magic bytes.
_ALLOWED = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 Mo

_client = None  # singleton boto3, gardé comme db._pool


class MediaError(Exception):
    """Échec de validation/upload, traduit en réponse HTTP par l'adaptateur REST."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code


def _max_bytes() -> int:
    raw = os.environ.get("OTO_MCP_S3_MAX_IMAGE_BYTES")
    return int(raw) if raw else _DEFAULT_MAX_BYTES


def _get_client():
    global _client
    if _client is None:
        try:
            import boto3  # import paresseux : pas de dép dure au boot
        except ImportError as e:  # pragma: no cover
            raise MediaError(500, "storage_unavailable", f"boto3 manquant: {e}")
        from .config import require_env
        _client = boto3.client(
            "s3",
            endpoint_url=require_env("OTO_MCP_S3_ENDPOINT"),
            region_name=os.environ.get("OTO_MCP_S3_REGION", "fr-par"),
            aws_access_key_id=require_env("OTO_MCP_S3_ACCESS_KEY"),
            aws_secret_access_key=require_env("OTO_MCP_S3_SECRET_KEY"),
        )
    return _client


def _bucket() -> str:
    from .config import require_env
    return require_env("OTO_MCP_S3_BUCKET")


def _sniff_content_type(data: bytes) -> str | None:
    """Détecte le type réel par magic bytes (PNG / JPEG / WEBP). None sinon."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def public_url(key: str) -> str:
    base = os.environ.get("OTO_MCP_S3_PUBLIC_BASE_URL")
    if base:
        return f"{base.rstrip('/')}/{key}"
    # Style virtual-hosted Scaleway : https://<bucket>.s3.fr-par.scw.cloud/<key>
    from .config import require_env
    parts = urlsplit(require_env("OTO_MCP_S3_ENDPOINT"))
    return f"{parts.scheme}://{_bucket()}.{parts.netloc}/{key}"


# NB : les logos de connecteurs ne transitent plus par S3 — ils sont servis par
# le CDN logo.dev (cf. `providers.Connector.logo_url_for`). Plus de seed ni d'assets.


def upload_image(prefix: str, owner_id: str, data: bytes, content_type: str) -> str:
    """Valide une image et l'uploade en public-read. Retourne son URL publique.

    `prefix` = "avatars" | "org-logos" ; `owner_id` = sub | org_id (str).
    Clé par hash de contenu → ré-upload identique idempotent + cache-busting
    naturel (un nouveau contenu = une nouvelle URL).
    """
    if not data:
        raise MediaError(400, "missing_file", "Fichier vide.")
    if len(data) > _max_bytes():
        raise MediaError(413, "image_too_large", f"Image > {_max_bytes()} octets.")
    sniffed = _sniff_content_type(data)
    if sniffed is None or sniffed not in _ALLOWED:
        raise MediaError(400, "unsupported_type", "Formats acceptés : png, jpeg, webp.")
    ext = _ALLOWED[sniffed]
    digest = hashlib.sha256(data).hexdigest()[:32]
    key = f"{prefix}/{quote(owner_id, safe='')}/{digest}.{ext}"
    try:
        _get_client().put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType=sniffed,
            ACL="public-read",
            CacheControl="public, max-age=31536000, immutable",
        )
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "upload_failed", str(e))
    return public_url(key)


_DEFAULT_PRESIGN_EXPIRY = 3600  # 1 h


def presign_expiry() -> int:
    raw = os.environ.get("OTO_MCP_S3_PRESIGN_EXPIRY")
    return int(raw) if raw else _DEFAULT_PRESIGN_EXPIRY


def upload_private(prefix: str, owner_id: str, data: bytes, content_type: str,
                   filename: str | None = None, *, expiry: int | None = None) -> str:
    """Uploade des octets en PRIVÉ (pas d'ACL public) et renvoie une URL GET
    **signée et expirante**. Pour les contenus retournés par un connecteur que
    l'agent doit récupérer hors-bande (binaires/gros fichiers) — PJ Gmail
    aujourd'hui, et besoin transverse Drive/Pennylane à venir (signal #64).

    Clé sous `tmp/<prefix>/<owner>/<hash>/<filename>` : déduplication par contenu
    + objet jetable. **Le bucket a une règle de lifecycle `expire-tmp-1d`** (préfixe
    `tmp/`, expiration 1 jour) qui purge ces objets — l'URL signée n'expire qu'à
    `presign_expiry()` (déf. 1 h), bien avant. Pas de fallback : stockage non
    configuré ⟹ `MediaError`.
    """
    if not data:
        raise MediaError(400, "missing_file", "Contenu vide.")
    digest = hashlib.sha256(data).hexdigest()[:32]
    name = quote(filename or "file", safe="")
    key = f"tmp/{prefix}/{quote(owner_id, safe='')}/{digest}/{name}"
    try:
        client = _get_client()
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expiry or presign_expiry(),
        )
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "upload_failed", str(e))


def delete_by_url(url: str) -> None:
    """Supprime l'objet pointé par `url` (best-effort — n'échoue jamais).

    Ne supprime que si l'URL appartient bien à notre base publique/bucket.
    """
    if not url:
        return
    try:
        base = os.environ.get("OTO_MCP_S3_PUBLIC_BASE_URL")
        if base and url.startswith(base.rstrip("/") + "/"):
            key = url[len(base.rstrip("/")) + 1:]
        else:
            # virtual-hosted : tout après le 1er "/" du path
            key = urlsplit(url).path.lstrip("/")
        if not key:
            return
        _get_client().delete_object(Bucket=_bucket(), Key=key)
    except Exception:
        pass  # best-effort : un orphelin ne doit jamais casser la requête user


# --- Blobs applicatifs DURABLES (documents de projet, ADR 0032 §3) -----------
# Ni `upload_image` (ACL public) ni `upload_private` (préfixe `tmp/` purgé en 1 j)
# ne conviennent à un document de projet : on veut du **durable + privé**. On
# persiste la CLÉ (pas une URL signée qui expire) et on signe à la demande.

def upload_object(prefix: str, owner_id: str, data: bytes, content_type: str,
                  filename: str | None = None) -> str:
    """Stocke un blob DURABLE (hors `tmp/`, pas d'ACL public) et renvoie sa **clé**
    S3 (à persister). L'URL d'accès se génère à la lecture via `presign_get`. Clé
    par hash de contenu → ré-upload identique idempotent."""
    if not data:
        raise MediaError(400, "missing_file", "Contenu vide.")
    if len(data) > _max_bytes():
        raise MediaError(413, "file_too_large", f"Fichier > {_max_bytes()} octets.")
    digest = hashlib.sha256(data).hexdigest()[:32]
    name = quote(filename or "file", safe="")
    key = f"{prefix}/{quote(owner_id, safe='')}/{digest}/{name}"
    try:
        _get_client().put_object(
            Bucket=_bucket(), Key=key, Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return key
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "upload_failed", str(e))


def copy_object(src_key: str, prefix: str, owner_id: str) -> str:
    """Copie un blob DURABLE privé vers le namespace `(prefix, owner_id)` et renvoie
    la clé neuve (copie server-side, sans télécharger ni ré-uploader). Le contenu
    étant identique, on réutilise le `digest`/`filename` portés par la clé source
    (`{prefix}/{owner}/{digest}/{name}`) → copie idempotente. Pas d'ACL public (la
    copie repart privée, même si l'original était partagé)."""
    parts = src_key.split("/")
    if len(parts) < 2:
        raise MediaError(400, "bad_src_key", f"Clé source invalide : {src_key!r}.")
    digest, name = parts[-2], parts[-1]
    dest_key = f"{prefix}/{quote(owner_id, safe='')}/{digest}/{name}"
    if dest_key == src_key:
        return src_key
    try:
        _get_client().copy_object(
            Bucket=_bucket(),
            CopySource={"Bucket": _bucket(), "Key": src_key},
            Key=dest_key,
        )
        return dest_key
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "copy_failed", str(e))


def presign_get(key: str, *, expiry: int | None = None) -> str:
    """URL GET signée et expirante pour une clé privée (lecture à la demande)."""
    try:
        return _get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expiry or presign_expiry(),
        )
    except Exception as e:
        raise MediaError(500, "presign_failed", str(e))


def delete_by_key(key: str) -> None:
    """Supprime un objet par sa clé (best-effort — n'échoue jamais)."""
    if not key:
        return
    try:
        _get_client().delete_object(Bucket=_bucket(), Key=key)
    except Exception:
        pass


def make_public(key: str) -> str:
    """Bascule un objet DURABLE existant en `public-read` et renvoie son URL
    publique permanente (ADR 0032 §3 — un « Autre document » partagé publiquement)."""
    try:
        _get_client().put_object_acl(Bucket=_bucket(), Key=key, ACL="public-read")
    except Exception as e:
        raise MediaError(500, "acl_failed", str(e))
    return public_url(key)


def make_private(key: str) -> None:
    """Repasse un objet en privé (best-effort)."""
    if not key:
        return
    try:
        _get_client().put_object_acl(Bucket=_bucket(), Key=key, ACL="private")
    except Exception:
        pass
