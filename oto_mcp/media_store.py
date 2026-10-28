"""Stockage d'images publiques (avatars user, logos d'org, images de tête d'email)
sur Scaleway Object Storage (S3-compatible).

Couche backend-core (ADR 0004) : possède le client S3, la validation et la
construction d'URL publique. N'importe jamais l'adaptateur REST. Les URLs
produites sont **publiques** (pas des secrets) → seule l'URL est persistée en
DB (colonnes `users.avatar_url` / `orgs.logo_url`), jamais dans le coffre chiffré.

Config 100% par env de process :
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
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

# Type sniffé → extension stockée. On ne fait JAMAIS confiance au Content-Type
# déclaré par le client : l'extension dérive des magic bytes.
_ALLOWED = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp"}
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 Mo

# La règle d'écriture de chaque préfixe, source UNIQUE : l'écriture native la pose
# (`upload_image`, `upload_object`), l'import d'un périmètre la repose sur chaque objet
# versé (`entetes_d_objet`). Un préfixe absent de ces deux ensembles n'a pas de règle.
PREFIXES_PUBLICS = frozenset({"avatars", "org-logos", "images"})
PREFIXES_DURABLES = frozenset({"project-files", "transcription-jobs", "app-files"})
# Les préfixes de TRANSIT (`upload_transit`) : la liste FERMÉE de ce qu'une référence de
# transit peut ouvrir (`oto_mcp/transit.py`), sous `tmp/` comme `upload_private`.
PREFIXES_TRANSIT = frozenset({"threecx-recordings", "aircall-recordings",
                              "allo-recordings"})
_ENTETES_PUBLICS = {"ACL": "public-read",
                    "CacheControl": "public, max-age=31536000, immutable"}

_client = None  # singleton boto3, gardé comme db._pool


class MediaError(Exception):
    """Échec de validation/upload, traduit en réponse HTTP par l'adaptateur REST."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code


def max_image_bytes() -> int:
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
    """Détecte le type réel par magic bytes (PNG / JPEG / GIF / WEBP). None sinon."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --- Le type SERVI d'un blob durable (oto-backend#562) -------------------------
# Un fichier de projet arrive avec un type DÉCLARÉ (par le client, le jeton d'upload
# ou une fonction) : rien ne l'obligeait à dire vrai, et il repartait tel quel dans
# le `ContentType` de l'objet — donc dans ce que sert son URL présignée, puis son URL
# publique une fois le fichier partagé. Un HTML déclaré `text/plain` ou `image/png`,
# ou un `text/html` avoué, devenait une page active servie depuis notre stockage.
#
# Le type servi se décide sur le CONTENU, jamais sur la déclaration seule, et rien
# n'est refusé : ce qu'on ne reconnaît pas, ou ce qui est actif, est écrit en
# `application/octet-stream` avec `Content-Disposition: attachment` — le navigateur
# le télécharge, il ne l'interprète jamais.

_NEUTRE = "application/octet-stream"

# Conteneurs dont la signature ne dit pas le format précis : le type déclaré est
# retenu s'il appartient à la famille reconnue, sinon le type générique du conteneur.
_FAMILLE_ZIP = frozenset({
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/epub+zip",
})
_FAMILLE_OLE = frozenset({"application/msword", "application/vnd.ms-excel",
                          "application/vnd.ms-powerpoint", "application/vnd.ms-outlook"})
_FAMILLE_ISO = frozenset({"audio/mp4", "audio/x-m4a", "audio/m4a", "video/mp4",
                          "video/quicktime", "image/heic", "image/avif"})

# Texte PASSIF : un navigateur l'affiche, il ne l'exécute pas. Retenu seulement si le
# contenu est bien du texte UTF-8 et ne ressemble pas à du balisage actif.
_TEXTE_PASSIF = frozenset({"text/plain", "text/csv", "text/markdown", "text/x-markdown",
                           "text/tab-separated-values", "application/json",
                           "application/x-ndjson"})

# Débuts de balisage qu'un navigateur interprète (HTML, SVG, XML/XSLT) : un texte qui
# en porte dans son premier kilo-octet est servi comme actif, quel que soit son type
# déclaré.
_BALISES_ACTIVES = (b"<html", b"<!doctype", b"<script", b"<svg", b"<?xml", b"<iframe",
                    b"<body", b"<head", b"<object", b"<embed", b"<xsl")


@dataclass(frozen=True)
class TypeServi:
    """Ce que le stockage SERVIRA pour un blob : `content_type`, et `attachment` =
    télécharger plutôt qu'afficher (vrai exactement quand le type est neutralisé)."""
    content_type: str
    attachment: bool


def _signature(data: bytes, declare: str) -> str | None:
    """Le type que les OCTETS attestent (signatures de format). None sinon."""
    image = _sniff_content_type(data)
    if image:
        return image
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return declare if declare in _FAMILLE_ZIP else "application/zip"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return declare if declare in _FAMILLE_OLE else None
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return declare if declare in _FAMILLE_ISO else "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm" if declare == "audio/webm" else "video/webm"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:2] == b"\x1f\x8b":
        return "application/gzip"
    return None


def _balisage_actif(data: bytes) -> bool:
    tete = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return any(b in tete for b in _BALISES_ACTIVES)


def _texte_utf8(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def type_servi(data: bytes, declare: str | None) -> TypeServi:
    """Le type sous lequel un blob durable sera servi, décidé sur son CONTENU (#562).

    1. une signature de format reconnue fait foi (le déclaré ne départage qu'une
       famille de conteneurs : docx/xlsx dans un zip, m4a/mp4 dans un `ftyp`…) ;
    2. sinon, un texte UTF-8 déclaré dans un type PASSIF garde ce type, s'il ne porte
       pas de balisage actif ;
    3. tout le reste — non reconnu, ou actif (HTML, SVG, XML, JavaScript, quel que
       soit le type déclaré) — est servi NEUTRE et en téléchargement.

    Rien n'est refusé : un fichier légitime qu'on ne sait pas nommer reste déposé,
    il est seulement téléchargé au lieu d'être affiché."""
    base = (declare or "").split(";")[0].strip().lower()
    signe = _signature(data, base)
    if signe:
        return TypeServi(signe, attachment=False)
    if base in _TEXTE_PASSIF and _texte_utf8(data) and not _balisage_actif(data):
        return TypeServi(base, attachment=False)
    return TypeServi(_NEUTRE, attachment=True)


def public_base() -> str:
    """La base publique de CETTE instance, sans `/` final : celle sous laquelle
    `public_url` sert chaque clé. L'export par périmètre la relit pour réécrire, à
    l'import, les URL d'une instance vers celles d'une autre (#1088)."""
    base = os.environ.get("OTO_MCP_S3_PUBLIC_BASE_URL")
    if base:
        return base.rstrip("/")
    # Style virtual-hosted Scaleway : https://<bucket>.s3.fr-par.scw.cloud/<key>
    from .config import require_env
    parts = urlsplit(require_env("OTO_MCP_S3_ENDPOINT"))
    return f"{parts.scheme}://{_bucket()}.{parts.netloc}"


def public_url(key: str) -> str:
    return f"{public_base()}/{key}"


# NB : les logos de connecteurs ne transitent plus par S3 — ils sont servis par
# le CDN logo.dev (cf. `providers.Connector.logo_url_for`). Plus de seed ni d'assets.


def _entetes_publics(data: bytes) -> dict:
    """Les en-têtes d'une image publique : son type lu sur ses octets (jamais le
    déclaré), lisible de tous, en cache immuable — sa clé change avec son contenu."""
    sniffed = _sniff_content_type(data)
    if sniffed is None or sniffed not in _ALLOWED:
        raise MediaError(400, "unsupported_type", "Formats acceptés : png, jpeg, gif, webp.")
    return {"ContentType": sniffed, **_ENTETES_PUBLICS}


def _entetes_durables(data: bytes, declare: str, name: str) -> dict:
    """Les en-têtes d'un blob durable : le type SERVI (`type_servi`, #562), et le
    téléchargement forcé quand il est neutralisé. Privé : aucune ACL."""
    servi = type_servi(data, declare)
    entetes = {"ContentType": servi.content_type}
    if servi.attachment:
        entetes["ContentDisposition"] = f"attachment; filename*=UTF-8''{name}"
    return entetes


def entetes_d_objet(key: str, data: bytes, declare: str | None, public: bool) -> dict:
    """Les en-têtes qu'écrit l'écriture native pour l'objet `key`, d'après son préfixe :
    ce que l'import d'un périmètre repose sur chaque objet versé (#1088).

    - préfixe public (`upload_image`) : type lu sur les octets, `public-read`, cache
      immuable — quel que soit l'état de l'objet à la source ;
    - préfixe durable (`upload_object`) : type servi d'après `declare` (le type de
      l'objet source, que `type_servi` redécide sur le contenu), privé, sauf un document
      partagé (`make_public`), dont `public` dit qu'il l'était à la source.

    Un autre préfixe n'a pas de règle : `MediaError`, jamais une ACL devinée."""
    prefix = key.split("/", 1)[0]
    if prefix in PREFIXES_PUBLICS:
        return _entetes_publics(data)
    if prefix in PREFIXES_DURABLES:
        entetes = _entetes_durables(data, declare or "", key.rsplit("/", 1)[1])
        if public:
            entetes["ACL"] = "public-read"
        return entetes
    raise MediaError(500, "unknown_prefix", f"Aucune règle d'écriture pour le préfixe "
                                            f"{prefix!r} (objet {key!r}).")


def upload_image(prefix: str, owner_id: str, data: bytes, content_type: str) -> str:
    """Valide une image et l'uploade en public-read. Retourne son URL publique.

    `prefix` = "avatars" | "org-logos" | "images" (image publique déposée par un agent,
    ex. la tête d'un `email_send`) ; `owner_id` = sub | org_id (str).
    Clé par hash de contenu → ré-upload identique idempotent + cache-busting
    naturel (un nouveau contenu = une nouvelle URL) — et **non devinable** : 128 bits
    de SHA-256, qu'on ne retrouve qu'en possédant l'image.
    """
    if prefix not in PREFIXES_PUBLICS:
        raise MediaError(500, "unknown_prefix", f"Préfixe d'image publique inconnu : {prefix!r}.")
    if not data:
        raise MediaError(400, "missing_file", "Fichier vide.")
    if len(data) > max_image_bytes():
        raise MediaError(413, "image_too_large", f"Image > {max_image_bytes()} octets.")
    entetes = _entetes_publics(data)
    digest = hashlib.sha256(data).hexdigest()[:32]
    key = f"{prefix}/{quote(owner_id, safe='')}/{digest}.{_ALLOWED[entetes['ContentType']]}"
    try:
        _get_client().put_object(Bucket=_bucket(), Key=key, Body=data, **entetes)
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "upload_failed", str(e))
    return public_url(key)


_DEFAULT_PRESIGN_EXPIRY = 3600  # 1 h


def presign_expiry() -> int:
    raw = os.environ.get("OTO_MCP_S3_PRESIGN_EXPIRY")
    return int(raw) if raw else _DEFAULT_PRESIGN_EXPIRY


_CARACTERE_NON_SUR = re.compile(r"[^A-Za-z0-9._-]+")
_NOM_MAX = 120


def nom_sur(filename: str | None) -> str:
    """Le dernier segment d'une CLÉ tiré d'un nom de fichier : caractères sûrs seuls
    (`A-Z a-z 0-9 . _ -`, le reste devient `_`), sans `/` ni point de tête (ni `..`),
    borné à `_NOM_MAX` en gardant l'extension. Jamais `quote` : un `%` littéral dans la
    clé, boto le ré-encode (`%5B` → `%255B`) dans l'URL qu'il signe."""
    nom = _CARACTERE_NON_SUR.sub("_", filename or "").lstrip("._")
    if len(nom) > _NOM_MAX:
        racine, ext = os.path.splitext(nom)
        ext = ext[:16]
        nom = racine[:_NOM_MAX - len(ext)] + ext
    return nom or "file"


def cle_transit(prefix: str, owner_id: str, digest: str) -> str:
    """`tmp/<prefix>/<owner>/<digest>` : la racine d'un dépôt sous `tmp/`. Le seul
    endroit qui l'écrit, pour le dépôt (`upload_private`, `upload_transit`) comme pour
    la relecture d'une référence de transit."""
    return f"tmp/{prefix}/{quote(owner_id, safe='')}/{digest}"


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
    key = f"{cle_transit(prefix, owner_id, digest)}/{nom_sur(filename)}"
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


def upload_transit(prefix: str, owner_id: str, data: bytes, content_type: str,
                   filename: str | None = None) -> str:
    """Dépose des octets EN TRANSIT et rend leur empreinte (`digest`), pas une URL :
    l'objet se désigne ensuite par une référence courte, relue côté serveur
    (`oto_mcp/transit.py`), qu'un agent recopie sans l'altérer.

    Clé `cle_transit(prefix, owner_id, digest)` (sous `tmp/`, purgée en 1 jour comme
    `upload_private`), le nom d'origine en métadonnée (`filename`, encodé) et en
    téléchargement forcé si l'objet est un jour signé. `prefix` ∈ `PREFIXES_TRANSIT`."""
    if prefix not in PREFIXES_TRANSIT:
        raise MediaError(500, "unknown_prefix", f"Préfixe de transit inconnu : {prefix!r}.")
    if not data:
        raise MediaError(400, "missing_file", "Contenu vide.")
    digest = hashlib.sha256(data).hexdigest()[:32]
    nom = quote(os.path.basename(filename or "") or "file", safe="")[:400]
    try:
        _get_client().put_object(
            Bucket=_bucket(), Key=cle_transit(prefix, owner_id, digest), Body=data,
            ContentType=content_type or "application/octet-stream",
            ContentDisposition=f"attachment; filename*=UTF-8''{nom}",
            Metadata={"filename": nom})
    except MediaError:
        raise
    except Exception as e:  # boto / réseau
        raise MediaError(500, "upload_failed", str(e))
    return digest


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
    # noqa: SILENT — best-effort : un orphelin S3 ne casse jamais la requête user
    except Exception:
        pass  # best-effort : un orphelin ne doit jamais casser la requête user


# --- Blobs applicatifs DURABLES (documents de projet, ADR 0032 §3) -----------
# Ni `upload_image` (ACL public) ni `upload_private` (préfixe `tmp/` purgé en 1 j)
# ne conviennent à un document de projet : on veut du **durable + privé**. On
# persiste la CLÉ (pas une URL signée qui expire) et on signe à la demande.

def upload_object(prefix: str, owner_id: str, data: bytes, content_type: str,
                  filename: str | None = None, *, max_bytes: int | None = None,
                  propre_au_depot: bool = False) -> str:
    """Stocke un blob DURABLE (hors `tmp/`, pas d'ACL public) et renvoie sa **clé**
    S3 (à persister). L'URL d'accès se génère à la lecture via `presign_get`. Clé
    par hash de contenu → ré-upload identique idempotent. `max_bytes` (optionnel)
    surcharge le plafond image par défaut (2 Mo) — un document brut peut être plus
    gros (upload out-of-bande d'un agent, issue #105).

    `propre_au_depot=True` : la clé porte en plus un aléa, deux dépôts du même contenu
    ne partagent pas l'objet. Pour un objet que son dépôt PURGE à sa fin (l'audio d'un
    travail de transcription) : avec la clé par contenu, la purge d'un travail
    supprimait l'audio d'un autre travail sur le même fichier (oto-backend#1301).

    ⚠️ `content_type` est un type DÉCLARÉ : l'objet est écrit sous `type_servi`
    (#562), décidé sur le contenu — neutre et en téléchargement s'il est actif ou
    non reconnu. C'est ce que serviront son URL présignée et son URL publique."""
    if prefix not in PREFIXES_DURABLES:
        raise MediaError(500, "unknown_prefix", f"Préfixe de blob durable inconnu : {prefix!r}.")
    if not data:
        raise MediaError(400, "missing_file", "Contenu vide.")
    limit = max_bytes if max_bytes is not None else max_image_bytes()
    if len(data) > limit:
        raise MediaError(413, "file_too_large", f"Fichier > {limit} octets.")
    digest = hashlib.sha256(data).hexdigest()[:32]
    if propre_au_depot:
        digest = f"{digest}-{secrets.token_hex(8)}"
    key = f"{prefix}/{quote(owner_id, safe='')}/{digest}/{nom_sur(filename)}"
    # Le nom ENCODÉ va dans l'en-tête (RFC 5987, c'est sa place), jamais dans la clé.
    entetes = _entetes_durables(data, content_type, quote(filename or "file", safe=""))
    try:
        _get_client().put_object(Bucket=_bucket(), Key=key, Body=data, **entetes)
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


def fetch_object(key: str, *, max_bytes: int | None = None) -> bytes:
    """Les OCTETS d'un objet privé — lecture serveur, pour l'extraction de texte (#298).

    ⚠️ **La taille est vérifiée AVANT de matérialiser**, par un `head_object` qui ne
    transfère rien. Lire puis mesurer serait un contrôle victime de ce qu'il contrôle :
    un objet de 200 Mo serait déjà en mémoire au moment d'être refusé, sur un serveur
    mono-loop où un dépassement tue le process entier.

    L'upload borne déjà ce qui entre — mais un objet écrit par un autre chemin, ou une
    borne abaissée après coup, ne doivent pas pouvoir contourner celle-ci. Un contrôle
    à l'entrée ne dispense pas d'un contrôle à la lecture : ils ne protègent pas des
    mêmes erreurs.

    ≠ `presign_get`, qui rend une URL pour que le CLIENT lise. Ici c'est le serveur qui
    lit, et repasser par une URL signée serait un aller-retour HTTP vers notre propre
    stockage.
    """
    return lire_objet(key, max_bytes=max_bytes).data


@dataclass(frozen=True)
class ObjetLu:
    data: bytes
    content_type: str
    filename: str | None  # le nom posé en métadonnée (`upload_transit`), décodé


def _absent(e: Exception) -> bool:
    """L'objet n'existe pas (jamais posé, ou purgé) : le code d'erreur S3 de boto."""
    code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
    return code in ("404", "NoSuchKey", "NotFound")


def lire_objet(key: str, *, max_bytes: int | None = None) -> ObjetLu:
    """`fetch_object`, avec le type et le nom posés au dépôt. Un objet ABSENT lève
    `MediaError(404, "not_found")`, distinct d'un stockage injoignable (`fetch_failed`)."""
    limite = max_bytes or max_image_bytes()
    try:
        client = _get_client()
        meta = client.head_object(Bucket=_bucket(), Key=key)
        taille = int(meta.get("ContentLength") or 0)
        if taille > limite:
            raise MediaError(413, "object_too_large",
                             f"{taille} octets (borne {limite})")
        body = client.get_object(Bucket=_bucket(), Key=key)["Body"]
        # `amt` borné même après le head : la taille annoncée pourrait mentir, et on
        # lit un octet de plus pour distinguer « pile la borne » de « tronqué ».
        data = body.read(limite + 1)
    except MediaError:
        raise
    except Exception as e:  # boto / réseau / clé absente
        if _absent(e):
            raise MediaError(404, "not_found", "objet absent") from None
        raise MediaError(500, "fetch_failed", str(e))
    if len(data) > limite:
        raise MediaError(413, "object_too_large", f"dépasse {limite} octets à la lecture")
    nom = (meta.get("Metadata") or {}).get("filename")
    return ObjetLu(data, meta.get("ContentType") or "application/octet-stream",
                   unquote(nom) if nom else None)


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
    # noqa: SILENT — best-effort : un orphelin S3 ne casse jamais la requête user
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
    """Repasse un objet en `private`. **Lève** `MediaError` si l'ACL n'a pas bougé.

    Symétrique de `make_public` — et ce n'est pas cosmétique. Tant que cette bascule
    était best-effort, une ACL refusée laissait l'objet `public-read` avec son URL
    permanente pendant que l'appelant persistait « privé » et rendait `{"ok": true}` :
    l'écran disait privé, le fichier ne l'était pas (inventaire des silences du
    2026-08-27, site B1). L'asymétrie ÉTAIT le bug ; la symétrie est la correction.
    """
    if not key:
        return
    try:
        _get_client().put_object_acl(Bucket=_bucket(), Key=key, ACL="private")
    except Exception as e:
        raise MediaError(500, "acl_failed", str(e))
