"""Résolveur « fichier côté oto » — désigner un fichier déjà accessible par oto et
en récupérer les OCTETS côté serveur (oto-backend#60).

Un agent MCP n'a pas de système de fichiers : il ne peut pas désigner un PDF par un
chemin disque. Ce module résout une **référence typée** vers le contenu binaire d'un
fichier qu'oto sait atteindre — pièce Gmail, fichier Drive, URL, fichier d'un projet,
fichier déposé en transit par un outil —
pour qu'un tool (upload Pennylane, etc.) l'ingère sans passer par le disque.

Couche backend-core (ADR 0004) : imports lazy des clients connecteurs, résolution
des credentials Google via `google_oauth`. Pas de fallback : une source illisible
ou inconnue lève `FileSourceError` (traduite en erreur actionnable par l'appelant).
"""
from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import access, url_perimeter
from .auth import google as google_oauth

# Plafond par défaut : Pennylane accepte 100 Mo, mais on borne pour ne pas charger
# un fichier géant en RAM par mégarde. Override par appel via `max_bytes`.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 Mo

#: Plafond d'un contenu passé EN LIGNE (`{kind: "content"}`, oto-backend#1149) : un
#: argument d'outil généré par l'agent casse dès quelques centaines de Ko (constat
#: porté par `oto_upload_url`). Au-delà, le fichier passe par le PUT signé ou par une
#: source que le serveur atteint.
CONTENT_MAX_BYTES = 256 * 1024


class FileSourceError(RuntimeError):
    """Référence de fichier invalide, source illisible, ou dépassement de taille."""


@dataclass
class ResolvedFile:
    data: bytes
    filename: str
    mime: str


def _google_creds(account: Optional[str], service: str):
    """`service` : le service Google que la source lit — vérifié comme un appel
    d'outil (un compte qui ne l'a pas autorisé reçoit le refus nommé, pas un 403 muet).
    `account` est le compte de la SOURCE : il peut différer du `_account=` de l'appel
    (envoyer depuis une boîte un fichier du Drive d'un autre compte) ; omis, c'est le
    compte de l'appel qui lit (oto-backend#1160)."""
    sub = access.current_user_sub_or_raise()
    return google_oauth.credentials_for(sub, account=account, service=service,
                                        source_account=True)


def _from_drive(src: dict) -> ResolvedFile:
    file_id = src.get("file_id")
    if not file_id:
        raise FileSourceError("source drive : `file_id` requis.")
    from oto.tools.google.drive.lib.drive_client import DriveClient
    client = DriveClient(credentials=_google_creds(src.get("account"), "drive"))
    if src.get("export_sheets"):
        meta = client.get_file_metadata(str(file_id))
        if meta.get("mimeType") == "application/vnd.google-apps.spreadsheet":
            # A native Sheet has no bytes to download: export its first sheet as CSV.
            att = client.export_file_bytes(str(file_id), "text/csv")
            return ResolvedFile(att["data"], (att.get("filename") or str(file_id)) + ".csv",
                                "text/csv")
    att = client.get_file_bytes(str(file_id))
    return ResolvedFile(att["data"], att.get("filename") or str(file_id),
                        att.get("mimeType") or "application/octet-stream")


def _from_gmail(src: dict) -> ResolvedFile:
    message_id, filename = src.get("message_id"), src.get("filename")
    if not message_id or not filename:
        raise FileSourceError("source gmail : `message_id` et `filename` requis.")
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    client = GmailClient(credentials=_google_creds(src.get("account"), "gmail"))
    att = client.get_attachment(message_id, filename, int(src.get("index", 0)))
    return ResolvedFile(att["data"], att.get("filename") or filename,
                        att.get("mimeType") or "application/octet-stream")


def _assert_public_host(host: str) -> None:
    """Anti-SSRF : refuse une cible qui résout vers une adresse non publique
    (boucle locale, privée, lien-local — dont les métadonnées cloud
    169.254.169.254 —, réservée, multicast). Sans ce garde-fou, un agent pourrait
    faire lire au serveur ses services internes ou l'IMDS.

    C'est la garde d'egress de la plateforme (`oto_mcp/egress.py`) sous la
    politique « URL choisie par l'agent » (aucune exception déclarée) — la même
    couture que `web_read` et que les connecteurs à hôte libre (oto#180). Elle
    portait sa propre règle jusqu'au 12/09/2026."""
    from . import egress
    if not host:
        raise FileSourceError("source url : hôte manquant.")
    hote = f"[{host}]" if ":" in host else host
    try:
        egress.check_url(f"http://{hote}/", connector="source url", field="url",
                         exceptions_declarees=False)
    except egress.EgressRefused as e:
        raise FileSourceError(str(e)) from None


#: Hops allowed when a caller opts into redirects (`oto_import`); each is re-checked.
MAX_REDIRECTS = 5


def _check_url(url: str) -> None:
    """Perimeter, scheme and anti-SSRF — run on the first URL and on every hop."""
    url_perimeter.refuse_if_excluded(url, url_perimeter.perimeter_of_call())
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise FileSourceError("source url : `url` http(s) requise.")
    from urllib.parse import urlsplit
    _assert_public_host(urlsplit(str(url)).hostname or "")


def _reste(deadline: Optional[float]) -> Optional[float]:
    """Seconds left before `deadline` (monotonic), or None without a deadline.

    Refuses once it is past: the per-phase timeout of httpx bounds each read, not
    the whole download — a server that drips one byte per read would otherwise
    hold a threadpool thread for as long as it likes (same class as the freeze
    of 21/09)."""
    if deadline is None:
        return None
    reste = deadline - time.monotonic()
    if reste <= 0:
        raise FileSourceError(
            "source url: the download did not finish within the call's budget — "
            "the server is too slow; use a faster link or upload the file.")
    return reste


def _refus_http(status: int) -> str:
    """The refusal of a URL answered with an error. A signed link (a storage URL with
    its signature and token in the query) answers 400 or 403 once ALTERED — one
    character lost or changed when copied from one call to another — or EXPIRED: the
    message says so, and that the fix is to fetch the link again, never to retry it."""
    if status in (400, 403):
        return (f"source url: the server refused the link (HTTP {status}) — link altered "
                "or expired: re-read the recording / re-fetch the URL from the tool that "
                "served it, and pass it unchanged; prefer a short `source` when that tool "
                "returns one.")
    if status == 404:
        return "source url: nothing at this URL (HTTP 404)."
    return f"source url: the server answered HTTP {status}."


def _from_url(src: dict, max_bytes: int, follow_redirects: bool = False,
              deadline: Optional[float] = None) -> ResolvedFile:
    # Périmètre du projet (#605) : un fichier lu à une URL est une page lue. Et le
    # refus du périmètre parle en PREMIER (#632) — avant la forme http(s), avant
    # l'anti-SSRF (qui résout l'hôte : sans réseau, il parlait à sa place).
    url_perimeter.refuse_if_excluded(src.get("url"), url_perimeter.perimeter_of_call())
    url = src.get("url")
    _check_url(url)
    explicite = src.get("filename")
    if explicite is not None:
        if not isinstance(explicite, str) or not os.path.basename(explicite.strip()):
            raise FileSourceError("source url : `filename` doit être un nom de fichier.")
        explicite = os.path.basename(explicite.strip())
    from urllib.parse import unquote, urljoin, urlsplit

    import httpx
    # Redirections DÉSACTIVÉES par défaut : un 3xx pourrait pointer une IP interne.
    # Opt-in (`follow_redirects`) : suivies à la main, chaque saut re-vérifié, jamais
    # de https vers http.
    with httpx.Client(follow_redirects=False, timeout=60.0) as c:
        for _ in range(MAX_REDIRECTS + 1):
            reste = _reste(deadline)
            with c.stream("GET", url,
                          timeout=60.0 if reste is None else min(60.0, reste)) as r:
                if 300 <= r.status_code < 400:
                    if not follow_redirects:
                        raise FileSourceError(
                            "source url : redirection non suivie (anti-SSRF) — fournir "
                            "l'URL finale directe.")
                    nxt = urljoin(str(url), r.headers.get("location") or "")
                    if (str(url).lower().startswith("https://")
                            and not nxt.lower().startswith("https://")):
                        raise FileSourceError(
                            "source url: redirect from https to http refused.")
                    _check_url(nxt)
                    url = nxt
                    continue
                if r.status_code >= 400:
                    raise FileSourceError(_refus_http(r.status_code))
                declared = r.headers.get("content-length")
                if declared and int(declared) > max_bytes:
                    raise FileSourceError(
                        f"fichier distant trop volumineux ({declared} > {max_bytes} octets).")
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    _reste(deadline)
                    total += len(chunk)
                    if total > max_bytes:
                        raise FileSourceError(f"fichier distant > {max_bytes} octets.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = (r.headers.get("content-type")
                        or "application/octet-stream").split(";")[0].strip()
                servi = _nom_de_disposition(r.headers.get("content-disposition"))
                break
        else:
            raise FileSourceError(f"source url: more than {MAX_REDIRECTS} redirects.")
    # #1346: the name the caller gives, else the one the server serves
    # (`Content-Disposition`, e.g. a presigned storage URL with an opaque key), else
    # the path. A generic type is refined from that name's extension.
    name = explicite or servi \
        or os.path.basename(unquote(urlsplit(str(url)).path)) or "file"
    if mime == "application/octet-stream":
        mime = mimetypes.guess_type(name)[0] or mime
    return ResolvedFile(data, name, mime)


def _from_project_file(src: dict, max_bytes: int) -> ResolvedFile:
    """Un fichier DÉPOSÉ sur un projet (« Autre document », `oto_project_files`), lu
    dans le stockage objet — sans URL signée ni aller-retour HTTP (ADR 0074).

    La garde est celle d'une lecture par-id d'un projet : visible dans l'org ACTIVE
    (`ownership.visible_in_org`, ADR 0023) ET lisible (`can_access(read)`). Un refus
    répond EXACTEMENT comme un fichier inconnu : distinguer les deux dirait à qui n'a
    pas le droit que le fichier existe."""
    try:
        pid, fid = int(src.get("project_id")), int(src.get("file_id"))
    except (TypeError, ValueError):
        raise FileSourceError(
            "source project_file : `project_id` et `file_id` entiers requis "
            "(ids rendus par oto_project_files op=list).") from None
    from . import db, media_store, ownership
    introuvable = FileSourceError(
        f"source project_file : fichier #{fid} introuvable sur le projet #{pid}.")
    sub = access.current_user_sub_or_raise()
    rid = str(pid)
    if not (ownership.visible_in_org(sub, access.current_org(sub), "project", rid)
            and ownership.can_access(sub, "project", rid, "read")):
        raise introuvable
    row = db.get_project_file(fid)
    if not row or int(row["project_id"]) != pid:
        raise introuvable
    try:
        data = media_store.fetch_object(row["s3_key"], max_bytes=max_bytes)
    except media_store.MediaError as e:
        raise FileSourceError(
            f"source project_file : lecture impossible ({e.code} : {e}).") from None
    return ResolvedFile(data, row.get("filename") or f"fichier-{fid}",
                        row.get("mime") or "application/octet-stream")


def _from_api(src: dict, max_bytes: int) -> ResolvedFile:
    """Un fichier SERVI par le connecteur `api` de l'appelant (un export CSV d'un
    pont, un PDF d'une API) : GET seulement, avec EXACTEMENT la résolution
    d'instance et de clé de `api_get` (`_project` épinglé, `_instance`, équipe) et
    ses gardes d'egress — c'est sa fabrique qui sert. L'authentification est
    injectée côté serveur et ne sort jamais ; une redirection est refusée (elle
    l'emporterait) ; le corps est lu en flux, borné par `max_bytes`."""
    path, params = src.get("path"), src.get("params")
    if not isinstance(path, str) or not path.startswith("/"):
        raise FileSourceError(
            "source api : `path` requis, relatif à la base_url du connecteur "
            "(commence par /).")
    if params is not None and not isinstance(params, dict):
        raise FileSourceError("source api : `params` doit être un objet.")
    import requests

    from .mcp_errors import McpError
    from .tools import api as connecteur_api
    try:
        client = connecteur_api._client()
    except McpError as e:
        raise FileSourceError(f"source api : {e.error.message}") from None
    try:
        data, ctype = client.get_raw(path, params, max_bytes=max_bytes)
    except ValueError as e:
        # Même traduction que `api_get` : une redirection refusée ne rend que
        # schéma://hôte/chemin, jamais la requête qui peut porter la clé.
        raise FileSourceError(
            f"source api : {connecteur_api._value_error(e).error.message}") from None
    except requests.HTTPError as e:
        raise FileSourceError(
            f"source api : {connecteur_api._upstream_error(e).error.message}") from None
    except requests.RequestException as e:
        # Le type seul : le texte d'une erreur réseau porte l'URL, et l'URL porte la
        # clé quand le connecteur l'injecte en paramètre de requête.
        raise FileSourceError(
            f"source api : l'API cible n'a pas répondu ({type(e).__name__}).") from None
    nom = path.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "api"
    return ResolvedFile(data, nom, ctype.split(";")[0].strip() or "application/octet-stream")


#: Whole-download budget of an attachment (each read is also bounded by its timeout):
#: a server that drips one byte per read must not hold a thread.
_UNIPILE_BUDGET_S = 90.0


def _nom_de_disposition(valeur: Optional[str]) -> Optional[str]:
    """The file name a `Content-Disposition` header carries (`filename*` RFC 2231
    included), without any path."""
    if not valeur:
        return None
    from email.message import Message
    m = Message()
    m["content-disposition"] = valeur
    nom = m.get_filename()
    return os.path.basename(nom) if nom else None


def _from_unipile(src: dict, max_bytes: int) -> ResolvedFile:
    """Une pièce jointe d'un message reçu sur une messagerie hébergée (LinkedIn,
    WhatsApp, Telegram, Instagram), désignée par la `source` que le lecteur pose sur
    chaque pièce (`attachments[].source`, `{channel, url}`).

    L'URL est celle qu'Unipile sert dans le message ; elle exige la clé. Le client est
    celui du COMPTE OPÉRÉ du canal (`unipile_client` : la même résolution que la
    lecture, même refus sans compte), et l'URL doit désigner CE compte sur CETTE
    instance : `https://<dsn>/v2/<account_id>/…` (le chemin v2 porte le compte). Sans
    cette garde, une clé partagée lirait la pièce d'un autre compte. La clé ne part
    que vers l'instance qui la porte : redirection refusée (elle l'emporterait)."""
    from urllib.parse import quote, urlsplit

    from .mcp_errors import McpError
    from .tools import unipile as messagerie
    canal, url = src.get("channel"), src.get("url")
    provider = messagerie.UNIPILE_CHANNELS.get(str(canal or "").lower())
    if provider is None:
        raise FileSourceError(
            "source unipile : `channel` attendu parmi "
            f"{', '.join(messagerie.UNIPILE_CHANNELS)}.")
    if not isinstance(url, str) or not url:
        raise FileSourceError(
            "source unipile : `url` requise — passer telle quelle la `source` d'une "
            "pièce jointe lue (`attachments[].source`).")
    try:
        client = messagerie.unipile_client(provider)
    except McpError as e:
        raise FileSourceError(f"source unipile : {e.error.message}") from None
    p = urlsplit(url)
    compte = quote(client.account_id(), safe="")
    if (p.scheme != "https" or p.netloc != client.dsn
            or not p.path.startswith(f"/v2/{compte}/")):
        raise FileSourceError(
            f"source unipile : cette URL ne désigne pas une pièce du compte {canal} "
            "opéré par cet appel — relire le message avec ce compte et passer la "
            "`source` rendue.")
    import requests
    deadline = time.monotonic() + _UNIPILE_BUDGET_S
    lent = FileSourceError(
        "source unipile : le téléchargement n'a pas fini dans le budget de l'appel.")
    try:
        with client.session.get(url, headers={"accept": "*/*"}, stream=True,
                                allow_redirects=False, timeout=(10, 60)) as r:
            if 300 <= r.status_code < 400:
                raise FileSourceError(
                    "source unipile : redirection refusée (elle emporterait la clé).")
            if r.status_code >= 400:
                raise FileSourceError(
                    f"source unipile : Unipile a répondu {r.status_code} sur la pièce "
                    "jointe (message supprimé, pièce expirée ou compte déconnecté).")
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FileSourceError(
                    f"pièce jointe trop volumineuse ({declared} > {max_bytes} octets).")
            chunks, total = [], 0
            for chunk in r.iter_content(64 * 1024):
                if time.monotonic() > deadline:
                    raise lent
                total += len(chunk)
                if total > max_bytes:
                    raise FileSourceError(f"pièce jointe > {max_bytes} octets.")
                chunks.append(chunk)
            mime = (r.headers.get("content-type")
                    or "application/octet-stream").split(";")[0].strip()
            nom = _nom_de_disposition(r.headers.get("content-disposition"))
            if nom:
                # Unipile builds the name from the full MIME type, parameters included
                # (`ACF0….ogg; codecs=opus`): they are not part of a file name.
                nom = nom.split(";", 1)[0].strip() or None
    except requests.RequestException as e:
        # The type only: the text of a network error carries the URL, and the URL the
        # operated account.
        raise FileSourceError(
            f"source unipile : Unipile n'a pas répondu ({type(e).__name__}).") from None
    if not nom:
        dernier = p.path.rstrip("/").rsplit("/", 1)[-1]
        nom = f"{str(canal).lower()}-{dernier}" + (mimetypes.guess_extension(mime) or "")
    return ResolvedFile(b"".join(chunks), nom, mime or "application/octet-stream")


def _from_transit(src: dict, max_bytes: int) -> ResolvedFile:
    """Un fichier qu'un outil a déposé EN TRANSIT pour l'appelant (`oto_mcp/transit.py`),
    désigné par la référence courte que cet outil a rendue — relu dans le stockage
    objet, jamais par une URL recopiée. La garde (owner = appelant, forme stricte,
    préfixes fermés) et le refus d'une référence expirée sont ceux de `transit.lire`."""
    from . import transit
    try:
        data, nom, mime = transit.lire(src.get("ref"), max_bytes=max_bytes)
    except transit.TransitError as e:
        raise FileSourceError(str(e)) from None
    nom = nom or "file" + (mimetypes.guess_extension(mime) or "")
    return ResolvedFile(data, nom, mime)


def _from_content(src: dict, max_bytes: int) -> ResolvedFile:
    """Un fichier PRODUIT par l'agent (un .xlsx d'un run, un CSV), passé dans l'appel :
    `base64` pour du binaire, `text` pour de l'UTF-8 — exactement l'un des deux.
    Borné à `CONTENT_MAX_BYTES` (et au plafond de l'appelant), vérifié AVANT de décoder
    sur la longueur encodée. `filename` est requis : son extension type le fichier."""
    nom = src.get("filename")
    if not isinstance(nom, str) or not os.path.basename(nom.strip()):
        raise FileSourceError(
            "source content : `filename` requis (son extension type le fichier, "
            "ex. rapport.xlsx).")
    b64, texte = src.get("base64"), src.get("text")
    if (b64 is None) == (texte is None):
        raise FileSourceError(
            "source content : exactement un de `base64` (binaire) ou `text` (UTF-8).")
    plafond = min(max_bytes, CONTENT_MAX_BYTES)
    trop = (f"source content : au-delà de {plafond} octets, un fichier ne passe pas "
            "en ligne — oto_upload_url (PUT), ou une source que le serveur atteint.")
    if texte is not None:
        if not isinstance(texte, str):
            raise FileSourceError("source content : `text` doit être une chaîne.")
        data = texte.encode("utf-8")
    else:
        if not isinstance(b64, str):
            raise FileSourceError("source content : `base64` doit être une chaîne.")
        b64 = "".join(b64.split())
        if len(b64) > 4 * ((plafond + 2) // 3):
            raise FileSourceError(trop)
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise FileSourceError(
                "source content : `base64` invalide (base64 standard, sans préfixe "
                "`data:`).") from None
    if len(data) > plafond:
        raise FileSourceError(trop)
    nom = os.path.basename(nom.strip())
    mime = (src.get("mime") or mimetypes.guess_type(nom)[0]
            or ("text/plain" if texte is not None else "application/octet-stream"))
    return ResolvedFile(data, nom, str(mime))


_RESOLVERS = {"drive": _from_drive, "gmail": _from_gmail}
# Les résolveurs qui bornent la taille EUX-MÊMES, avant de matérialiser les octets.
_RESOLVERS_BORNES = {"url": _from_url, "project_file": _from_project_file,
                     "api": _from_api, "content": _from_content,
                     "unipile": _from_unipile, "transit": _from_transit}


#: Les `kind` renommés, refusés en nommant le nouveau — aucun alias : le connecteur
#: Custom API s'appelle `api` depuis le 12/11/2026 (`providers.CONNECTEURS_RENOMMES`).
_KINDS_RENOMMES = {"http": " `kind=\"http\"` est devenu `kind=\"api\"` (le connecteur "
                           "Custom API s'appelle `api`) : rejoue avec `\"kind\": \"api\"`, "
                           "mêmes `path` et `params`."}


def resolve(source: Any, *, max_bytes: int = DEFAULT_MAX_BYTES,
            follow_redirects: bool = False,
            deadline: Optional[float] = None) -> ResolvedFile:
    """Résout une référence de fichier côté oto vers ses octets + métadonnées.

    `source` = dict `{"kind": "drive"|"gmail"|"url"|"project_file"|"api"|"content"|
    "unipile"|"transit", …}` :
      - drive        : `{file_id, account?}`
      - gmail        : `{message_id, filename, index?, account?}`
      - url          : `{url, filename?}` (http/https, redirections refusées ; nom :
                       `filename`, sinon `Content-Disposition`, sinon le chemin)
      - project_file : `{project_id, file_id}` (ids d'`oto_project_files op=list`)
      - api          : `{path, params?}` (GET via le connecteur `api` de l'appelant)
      - content      : `{filename, base64 | text, mime?}` (fichier produit par l'agent,
                       ≤ `CONTENT_MAX_BYTES`)
      - unipile      : `{channel, url}` (pièce jointe d'un message, telle que le lecteur
                       de la messagerie la rend dans `attachments[].source`)
      - transit      : `{ref}` (fichier déposé en transit par un outil pour l'appelant,
                       la `source` que cet outil rend ; purgé en un jour)
    Lève `FileSourceError` si `kind` manque/inconnu, source illisible, ou taille
    dépassée. Borne la taille à `max_bytes` (charge en RAM). `deadline` (horloge
    monotone) borne le téléchargement d'une URL de bout en bout, sauts compris."""
    if not isinstance(source, dict):
        raise FileSourceError("source : objet attendu `{kind, …}`.")
    kind = source.get("kind")
    if kind == "url":
        rf = _from_url(source, max_bytes, follow_redirects, deadline)
    elif kind in _RESOLVERS_BORNES:
        rf = _RESOLVERS_BORNES[kind](source, max_bytes)
    else:
        fn = _RESOLVERS.get(kind)
        if fn is None:
            raise FileSourceError(
                f"source `kind`={kind!r} inconnu (attendu : drive, gmail, url, project_file, "
                f"api, content, unipile, transit).{_KINDS_RENOMMES.get(kind, '')}")
        try:
            rf = fn(source)
        except FileSourceError:
            raise
        except Exception as e:  # erreur client connecteur (Drive/Gmail) → actionnable
            raise FileSourceError(str(e))
    if len(rf.data) > max_bytes:
        raise FileSourceError(f"fichier de {len(rf.data)} octets > plafond {max_bytes}.")
    return rf
