"""Upload out-of-bande de contenu volumineux (issue oto-backend#105).

`oto_upload_url(target=…)` rend une URL signée à usage unique + TTL court sur laquelle
l'agent PUT le contenu depuis le disque (`curl --data-binary @fichier`), au lieu de le
faire transiter INLINE par le contexte du LLM (coût tokens + troncature sur du verbatim).
Le backend matérialise dans la cible (`PUT /api/upload/<token>`, `api.routes`) en
réappliquant l'autz.

⚠️ **Deux faces, pas une** (oto#106). La frappe du jeton a longtemps été MCP-only (« une
amorce d'action agent ») : un client REST pur voyait `/api/upload/{token}` publié au
contrat sans pouvoir obtenir le jeton, ni savoir quel corps envoyer, ni ce que l'accusé
contient — et chargeait 8 910 lignes une à une. `POST /api/me/upload-url` frappe le même
jeton, avec les mêmes paramètres ; la réception déclare son propre contrat
(`api.uploads.CONTRAT_RECEPTION`). Hors portée d'un jeton PORTÉ (`token_scopes`) : la
cible vit dans le corps, et ce qu'un jeton porté atteint se lit dans le chemin.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .. import config, file_source, upload_tokens
from ..datastore import schema as dsv2
from ..datastore import upsert_implicite as upi
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

#: The inline bound of a file the agent produced (`file_source` `{kind: "content"}`), as
#: the descriptions say it.
_EN_LIGNE_KO = file_source.CONTENT_MAX_BYTES // 1024


class UploadUrlInput(BaseModel):
    target: Literal["doc", "project_file", "datastore", "image"]
    op: Literal["create", "update"] = "create"        # doc : create sous projet, ou update d'un doc
    project_id: Optional[int] = None                  # doc create / project_file
    parent_id: Optional[int] = None                   # doc create (None = 1er niveau)
    doc_id: Optional[int] = None                      # doc update
    title: Optional[str] = None                       # doc create (requis) / project_file (optionnel)
    kind: Optional[Literal["doc", "note", "source"]] = None  # doc create (défaut source)
    filename: Optional[str] = None                    # project_file (requis)
    description: Optional[str] = None                 # project_file (optionnel)
    content_type: Optional[str] = None                # project_file (sinon déduit à la réception)
    datastore: Optional[str] = None                   # le tableau visé (requis)
    format: Optional[Literal["ndjson", "csv"]] = None  # datastore (défaut ndjson)
    #: datastore : la clé qui DÉSIGNE les lignes (oto#141) — nommée, une valeur en place
    #: modifie sa ligne ; omise, le fichier se rapproche sur `schema.key` mais AJOUTE.
    key: Optional[str] = Field(default=None, description=(
        "datastore: the column that DESIGNATES rows — an existing key value modifies "
        "its row, a new one creates it, no `upsert` needed. Omitted: the file matches "
        "on the table's declared key but ADDS (an existing value follows `upsert`)."))
    #: datastore — l'upload est LA porte de l'import, donc celle où poser la couche
    #: `origine` est le plus légitime (oto#70 lot 2). Le PUT, lui, ne porte aucun
    #: paramètre : c'est une URL signée qu'un socle appelle sans rien décider. La
    #: déclaration se fait donc ICI, au mint, par celui qui prépare l'import — et
    #: elle est SCELLÉE dans le jeton, comme la cible : à la réception, personne ne
    #: peut se la donner.
    #:
    #: ⚠️ DÉCRIT, pas seulement typé : un booléen sans phrase se lit « je ne sais pas à
    #: quoi il sert », et un agent ne coche pas ce qu'il ne comprend pas. Le texte vient
    #: de la même source que les deux autres faces.
    origine_override: bool = Field(
        default=False, description=dsv2.description_parametre_origine(en=True))
    #: datastore — oto#140. Déclaré ICI comme le précédent, et pour la même raison :
    #: le PUT ne porte aucun paramètre, donc celui qui livre les octets ne peut pas
    #: s'accorder lui-même le droit de poser de la donnée d'origine. C'est celui qui
    #: PRÉPARE l'import qui le déclare, et c'est scellé dans le jeton.
    donnees_d_origine: bool = Field(
        default=False, description=dsv2.description_donnees_d_origine(en=True))
    #: datastore — oto#141. Même raison que les deux précédents : le PUT ne porte aucun
    #: paramètre, donc la fusion sur la clé se DEMANDE ici, au mint, et se scelle.
    upsert: bool = Field(default=False, description=upi.description_parametre())
    large: bool = Field(default=False, description=(
        "project_file only: a BIG file (a video of several GB, up to the `max_bytes` the "
        "answer gives). The bytes go straight to storage in parts, never through oto: "
        "the answer gives `part_size`, `part_url` (GET `…/part/<n>` → the URL to PUT part "
        "n on) and `complete_url` (POST once every part is sent). Valid 24 h, the file "
        "lands in the project at completion. A large file is TEMPORARY: it is deleted "
        "automatically after `kept_days` (14 by default; `notice` says it) — tell the user."))


class UploadUrlOutput(BaseModel):
    """Le lien frappé — et le mode d'emploi du PUT qu'il ouvre."""
    url: str = Field(description=(
        "l'adresse signée, à usage unique : y envoyer le corps (`PUT`, octets bruts), "
        "ou l'ouvrir dans un navigateur (formulaire de dépôt). Sans `Authorization` : "
        "le jeton de l'adresse fait foi. L'accusé et les refus de la réception sont "
        "ceux de `PUT /api/upload/{token}`. Un jeton API PORTÉ ne frappe pas de lien "
        "(la cible vit dans le corps) : `403 token_scope_forbidden`."))
    method: Literal["PUT"]
    expires_at: int = Field(description="expiration du lien, en secondes epoch (TTL court)")
    max_bytes: int = Field(description="plafond du corps, en octets : au-delà, `413`")
    headers: dict[str, str] = Field(description=(
        "en-têtes à poser sur le PUT — le `Content-Type` attendu : NDJSON "
        "(`application/x-ndjson`, un objet JSON par ligne) ou CSV (`text/csv`, "
        "en-tête requis) pour un tableau, Markdown pour une page"))
    hint: str = Field(description="la conduite, en une phrase (shell, navigateur, ou inline)")
    target: dict = Field(description=(
        "la cible SCELLÉE dans le jeton (lisible, signée, non chiffrée) — le PUT ne "
        "porte aucun paramètre"))
    part_size: Optional[int] = Field(None, description=(
        "`large` : la taille EXACTE de chaque partie, sauf la dernière (plus courte)"))
    max_parts: Optional[int] = Field(None, description="`large` : parties au plus")
    part_url: Optional[str] = Field(None, description=(
        "`large` : `GET` en remplaçant `{n}` (1, 2…) rend, en texte, l'URL où `PUT` la "
        "partie n (présignée, une heure, sans `Content-Type`)"))
    complete_url: Optional[str] = Field(None, description=(
        "`large` : `POST` une fois toutes les parties envoyées → l'accusé ; une partie "
        "manquante est nommée, et le même lien la reprend"))
    abort_url: Optional[str] = Field(None, description="`large` : `DELETE` abandonne le dépôt")
    kept_days: Optional[int] = Field(None, description=(
        "`large` : jours de garde du fichier après le dépôt ; ensuite il est supprimé "
        "automatiquement (`expires_at` de l'accusé et des listes de fichiers)"))
    notice: Optional[str] = Field(None, description=(
        "`large` : la durée de garde en une phrase, à dire à l'utilisateur"))


# Les refus de la frappe. La cible est RE-jugée à la réception (`check_target_access`) :
# les mêmes 403/404 peuvent donc aussi sortir du PUT, cf. `api.uploads.CONTRAT_RECEPTION`.
_REFUS_DU_MINT = (
    DeclaredError(400, "missing_doc", "`target=doc`, `op=update` sans `doc_id`"),
    DeclaredError(400, "missing_project",
                  "`target=doc` (`op=create`) ou `project_file` sans `project_id`"),
    DeclaredError(400, "missing_title", "`target=doc`, `op=create` sans `title`"),
    DeclaredError(400, "missing_filename", "`target=project_file` sans `filename`"),
    DeclaredError(400, "missing_datastore", "`target=datastore` sans `datastore`"),
    DeclaredError(404, "unknown_namespace",
                  "le tableau nommé ne se voit pas depuis l'org de l'appel"),
    DeclaredError(403, "read_only", "le tableau est partagé en LECTURE seule"),
    DeclaredError(404, "unknown_doc", "la page visée (`doc_id`) n'existe pas"),
    DeclaredError(404, "unknown_project", "le projet visé (`project_id`) n'existe pas"),
    DeclaredError(403, "forbidden", "l'appelant n'a pas l'écriture sur la cible"),
    DeclaredError(400, "bad_parent",
                  "`target=doc`, `op=create` : `parent_id` n'existe pas ou n'est pas une "
                  "page du projet visé (même garde qu'`oto_doc op=create`)"),
    DeclaredError(400, "large_needs_project_file",
                  "`large=true` ne vaut que pour `target=project_file`"),
    DeclaredError(400, "upsert_without_key",
                  "`upsert=true` sur un tableau sans clé métier, et sans `key` : il n'y "
                  "aurait rien sur quoi fusionner"),
)


def _project_file_target(inp, filename: str) -> dict:
    if inp.project_id is None:
        raise AuthzDenied(400, "missing_project", "`project_id` requis.")
    return {"kind": "project_file", "project_id": int(inp.project_id),
            "filename": filename.strip(),
            "title": (inp.title.strip() if inp.title else None),
            "description": (inp.description.strip() if inp.description else None),
            "content_type": getattr(inp, "content_type", None)}


def _datastore_target(sub: str, inp, fmt: str) -> dict:
    if not (inp.datastore and inp.datastore.strip()):
        raise AuthzDenied(400, "missing_datastore", "`datastore` requis.")
    ns = inp.datastore.strip()
    from ..datastore.errors import est_un_nom_de_tableau
    if est_un_nom_de_tableau(ns):
        from .datastore.common import refus_nom_de_tableau
        raise refus_nom_de_tableau(ns)
    from ..datastore import core as ds  # lazy : évite tout cycle d'import au boot
    store = ds.make_store(sub)
    try:
        ns_id = store.resolve_ns_id_for_write(ns)  # org active présente au mint
    except ds.DatastoreNotFound:
        raise AuthzDenied(404, "unknown_namespace", f"Tableau `{ns}` inconnu.")
    except ds.DatastoreReadOnly:
        raise AuthzDenied(403, "read_only", f"Tableau `{ns}` partagé en lecture seule.")
    # Clé effective figée au mint (param explicite, sinon clé déclarée au schéma).
    eff_key = inp.key or store.declared_key(ns)
    # oto#141 : refusé ICI, au mint, et pas à la réception — celui qui livre les octets
    # ne peut plus rien corriger au jeton.
    try:
        upi.refuser_upsert_sans_cle(bool(inp.upsert), eff_key, ns, lot=True)
    except ValueError as e:
        raise AuthzDenied(400, "upsert_without_key", str(e))
    return {"kind": "datastore", "ns_id": ns_id, "namespace": ns,
            "format": fmt, "key": eff_key,
            "origine_override": bool(inp.origine_override),
            "donnees_d_origine": bool(inp.donnees_d_origine),
            "upsert": bool(inp.upsert),
            # oto#141 : la clé NOMMÉE à la frappe désigne ; scellé comme le reste.
            "cle_passee": bool(inp.key)}


def _upload_url(ctx: ResolvedCtx, inp: UploadUrlInput) -> dict:
    sub = ctx.sub
    # Descripteur de cible SCELLÉ dans le jeton — jamais accepté d'un param client à la
    # réception (verrou IDOR : sub/org/cible figés au mint).
    if inp.target == "doc":
        if inp.op == "update":
            if inp.doc_id is None:
                raise AuthzDenied(400, "missing_doc", "`doc_id` requis (op=update).")
            target = {"kind": "doc", "op": "update", "doc_id": int(inp.doc_id)}
        else:
            if inp.project_id is None:
                raise AuthzDenied(400, "missing_project", "`project_id` requis (op=create).")
            if not (inp.title and inp.title.strip()):
                raise AuthzDenied(400, "missing_title", "`title` requis (op=create).")
            target = {"kind": "doc", "op": "create", "project_id": int(inp.project_id),
                      "parent_id": int(inp.parent_id) if inp.parent_id is not None else None,
                      "title": inp.title.strip(), "doc_kind": inp.kind or "source"}
    elif inp.target == "project_file":
        if not (inp.filename and inp.filename.strip()):
            if inp.project_id is None:
                raise AuthzDenied(400, "missing_project", "`project_id` requis.")
            raise AuthzDenied(400, "missing_filename", "`filename` requis.")
        target = _project_file_target(inp, inp.filename)
    elif inp.target == "image":
        # Aucun paramètre : ni projet ni nom — la clé dérive du contenu (hash), le type
        # des magic bytes. L'URL publique arrive dans l'accusé de réception.
        target = {"kind": "image"}
    else:  # datastore
        target = _datastore_target(sub, inp, inp.format or "ndjson")

    # Fail-fast : refuse tout de suite sans l'écriture sur la cible (l'autz est
    # RÉAPPLIQUÉE à la réception — le jeton ne fait pas foi seul). Pour datastore
    # l'accès a déjà été vérifié par resolve_ns_id_for_write ; re-checké au receive.
    try:
        upload_tokens.check_target_access(sub, target)
    except upload_tokens.UploadError as e:
        raise AuthzDenied(e.status, e.code, e.message)

    if inp.large:
        if target["kind"] != "project_file":
            raise AuthzDenied(400, "large_needs_project_file",
                              "`large=true` is for `target=project_file` only.")
        return _large_upload_url(ctx, target)

    token, exp = upload_tokens.sign(sub, ctx.org_id, target)
    url = f"{config.public_base_url()}/api/upload/{token}"
    _CT_BY_KIND = {"doc": "text/markdown; charset=utf-8",
                   "datastore": ("text/csv" if target.get("format") == "csv"
                                 else "application/x-ndjson")}
    ct = target.get("content_type") or _CT_BY_KIND.get(target["kind"], "application/octet-stream")
    # La borne annoncée est celle qui MORD : une image est refusée à 2 Mo par
    # `upload_image`, bien avant le plafond générique de 25 Mo.
    if target["kind"] == "image":
        from .. import media_store  # lazy : boto3 n'est chargé qu'à l'upload
        limite = media_store.max_image_bytes()
    else:
        limite = upload_tokens.max_bytes()
    # Sans PUT ni humain, le repli dépend de la cible : un texte (page, tableau) peut
    # passer en ligne ; une image n'a pas de voie en ligne, un fichier de projet une voie
    # BORNÉE (`file_source` `content`, oto-backend#1149) — le base64 dans un argument
    # casse dès quelques centaines de Ko.
    if target["kind"] in ("doc", "datastore"):
        repli = ("send the content INLINE instead: `data_write(rows=[…], key=…)` in "
                 "slices for a table, `oto_doc op=create|update|patch` for a page.")
    elif target["kind"] == "image":
        repli = ("a FILE has no inline path — never base64 it into an argument. If it "
                 "already lives somewhere reachable (a link, a Drive file, a Gmail "
                 "attachment), `oto_import` makes the server fetch it; otherwise report "
                 "the URL to a human and stop.")
    else:
        repli = ("if the file already lives somewhere reachable (a link, a Drive file, a "
                 "Gmail or messaging attachment), `oto_import` makes the server fetch it; "
                 f"a file you produced, up to {_EN_LIGNE_KO} KB, goes through "
                 "`oto_import(source={kind:\"content\", filename, base64}, "
                 "target=\"project_file\")` — never base64 a bigger one into an "
                 "argument; otherwise report the URL to a human and stop.")
    return {
        "url": url,
        "method": "PUT",
        "expires_at": exp,
        "max_bytes": limite,
        "headers": {"Content-Type": ct},
        # Deux voies pour le MÊME lien : agent avec shell (curl PUT) OU, sans shell
        # (claude.ai), transmettre l'URL à l'humain qui l'ouvre → page d'upload.
        "hint": (f"If you have a shell: `curl -X PUT -H 'Content-Type: {ct}' "
                 f"--data-binary @FILE '{url}'`. If you DON'T (no shell), hand this URL to "
                 "the user — opening it in a browser shows an upload form. With neither "
                 "(unattended scheduled run, or the PUT blocked by your sandbox's egress "
                 f"policy), {repli} Single-use, expires soon; the body never returns through "
                 "you (only a light receipt)."),
        "target": target,
    }


def _large_upload_url(ctx: ResolvedCtx, target: dict) -> dict:
    """Un gros fichier de projet : dépôt multipart direct au stockage (`large_uploads`)."""
    from .. import large_uploads, media_store
    try:
        token, exp, scelle = large_uploads.ouvrir(ctx.sub, ctx.org_id, target)
    except media_store.MediaError as e:
        raise AuthzDenied(e.status, e.code, str(e))
    base = f"{config.public_base_url()}/api/upload/large/{token}"
    taille = scelle["part_size"]
    mio = taille // (1024 * 1024)
    return {
        "url": base,
        "method": "PUT",
        "expires_at": exp,
        "max_bytes": scelle["max_bytes"],
        "headers": {},
        "part_size": taille,
        "max_parts": large_uploads.MAX_PARTS,
        "part_url": f"{base}/part/{{n}}",
        "complete_url": f"{base}/complete",
        "abort_url": base,
        "kept_days": large_uploads.keep_days(),
        "notice": large_uploads.retention_notice(),
        "hint": (
            f"Split the file in parts of exactly {taille} bytes ({mio} MiB; the last one "
            "shorter), PUT each one on its own URL, then POST complete. With a shell: "
            f"`B='{base}'; F=FILE; P=$(( ($(wc -c < \"$F\") + {taille} - 1) / {taille} )); "
            "for n in $(seq 1 $P); do dd if=\"$F\" bs=1M skip=$(( (n-1)*"
            f"{mio} )) count={mio} 2>/dev/null | curl -sf -X PUT -H 'Content-Type:' "
            "--data-binary @- "
            "\"$(curl -sf \"$B/part/$n\")\" -o /dev/null || echo \"part $n failed\"; done; "
            "curl -s -X POST \"$B/complete\"`. A failed part is sent again the same way; "
            "complete names any missing part. Without a shell, hand the URL to the user: "
            "opening it in a browser shows an upload form that does all of it. Valid 24 h, "
            "single completion; the bytes never pass through oto. "
            f"{large_uploads.retention_notice()} Tell the user."),
        "target": {k: v for k, v in scelle.items() if k not in ("key", "upload_id")},
    }


CAPABILITIES += [
    Capability(
        key="me.upload_url", handler=_upload_url, Input=UploadUrlInput, authz=SUB_ONLY,
        Output=UploadUrlOutput, errors=_REFUS_DU_MINT,
        description=(
            "Get a signed, single-use URL to push a big file into oto without passing it "
            "through the conversation. With a shell: `curl -X PUT --data-binary @FILE "
            "'<url>'`. Without one, give the URL to the user: it opens an upload form. If "
            "the file is already reachable (a link, a Drive file, a project file, a Gmail "
            "attachment), use `oto_import` instead: the server fetches it — as for a "
            f"file you produced up to {_EN_LIGNE_KO} KB (`{{kind:\"content\"}}`). Targets: "
            "`datastore` (CSV or NDJSON rows; `key` designates rows by that column), `doc`, "
            "`project_file`, "
            "`image` (public permanent URL, 2 MB). A big file (a video of several GB): "
            "`target=project_file, large=true` sends it to storage in parts, and it is "
            "deleted automatically after 14 days by default. With neither a shell nor a user, send "
            "rows inline with `data_write`. The URL is signed, not encrypted: keep "
            "confidential names out of it."
        ),
        mcp="oto_upload_url",
        rest=RestBinding("POST", "/api/me/upload-url"),
    ),
]


# ── oto_import: the server fetches the file (no content through the conversation) ──

#: Whole-call budget, fetch included; the write stops between 500-row slices.
IMPORT_BUDGET_S = 40.0

_GSHEET = re.compile(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID = re.compile(r"[#&?]gid=(\d+)")
_EXT_FORMAT = {".csv": "csv", ".tsv": "csv", ".txt": "csv",
               ".ndjson": "ndjson", ".jsonl": "ndjson"}
_MIME_FORMAT = {"text/csv": "csv", "text/tab-separated-values": "csv",
                "application/vnd.ms-excel": "csv", "text/plain": "csv",
                "application/x-ndjson": "ndjson", "application/jsonl": "ndjson",
                "application/x-jsonlines": "ndjson"}
_SEPARATOR = {",": ",", ";": ";", "tab": "\t", "|": "|"}


class ImportInput(BaseModel):
    source: dict = Field(description=(
        "Where the file is: `{kind:\"url\", url}` (public link; a Google Sheets share "
        "link works if the sheet is public, `gid` picks the tab), `{kind:\"drive\", "
        "file_id}` (your Google account; a native Sheet exports its first tab as CSV), "
        "`{kind:\"project_file\", project_id, file_id}`, `{kind:\"gmail\", "
        "message_id, filename}`, a messaging attachment's `source` as read (`{kind:"
        "\"unipile\", channel, url}`), a recording's `source` as read (`{kind:"
        "\"transit\", ref}`), or a file YOU produced: `{kind:\"content\", "
        f"filename, base64}}` (or `text` instead of `base64`), {_EN_LIGNE_KO} KB at most. With "
        "`target=\"project_file\"`, the file is filed in the project as is."))
    target: Literal["datastore", "project_file"] = "datastore"
    datastore: Optional[str] = None
    format: Optional[Literal["csv", "ndjson"]] = Field(
        default=None, description="Default: from the file name or type.")
    separator: Optional[Literal[",", ";", "tab", "|"]] = Field(
        default=None, description="CSV only. Default: detected.")
    key: Optional[str] = Field(default=None, description=(
        "Column that DESIGNATES a row: a re-run then updates instead of appending, no "
        "`upsert` needed. Omitted, the file matches on the declared key but ADDS. Default: the table's declared key."))
    declare_columns: bool = Field(default=True, description=(
        "Declare headers that match no column as text columns (label = header); for "
        "NDJSON, keys no column declares, typed from their values. Existing columns are "
        "never changed. Off, a column the schema does not declare is refused."))
    resume_from: Optional[int] = Field(default=None, description=(
        "From a previous receipt, when the file did not fit one call. Needs "
        "`source_sha256`."))
    source_sha256: Optional[str] = Field(default=None, description=(
        "From the previous receipt: the resume is refused if the file changed."))
    project_id: Optional[int] = None
    filename: Optional[str] = Field(default=None, description=(
        "project_file only. Default: the source's file name."))
    title: Optional[str] = None
    description: Optional[str] = None
    origine_override: bool = Field(
        default=False, description=dsv2.description_parametre_origine(en=True))
    donnees_d_origine: bool = Field(
        default=False, description=dsv2.description_donnees_d_origine(en=True))
    upsert: bool = Field(default=False, description=upi.description_parametre())


class ImportOutput(BaseModel):
    """The receipt — never the content."""
    model_config = ConfigDict(extra="allow")
    ok: bool
    kind: Literal["datastore", "project_file"]
    source: dict = Field(description=(
        "`{kind, name, mime, bytes, sha256, url?}` — `url` without its query string"))
    datastore: Optional[str] = None
    file_id: Optional[int] = Field(default=None, description=(
        "project_file only: the file created, as `oto_project_files` lists it"))
    project_id: Optional[int] = Field(default=None, description="project_file only")
    inserted: Optional[int] = None
    updated: Optional[int] = None
    count: Optional[int] = Field(default=None, description="rows written by this call")
    total_rows: Optional[int] = None
    done: Optional[bool] = None
    resume_from: Optional[int] = Field(default=None, description=(
        "set when the file did not fit the call: call again with it and "
        "`source.sha256` as `source_sha256`"))
    format: Optional[str] = None
    encoding: Optional[str] = None
    separator: Optional[str] = None
    matched_by_label: Optional[dict] = Field(default=None, description="header → column")
    unmatched_headers: Optional[list] = None
    created_columns: Optional[list] = None
    hint: Optional[str] = None


def _source_for_fetch(source: dict) -> dict:
    """A public Google Sheets link becomes its CSV export; a Drive Sheet exports."""
    src = dict(source or {})
    if src.get("kind") == "drive":
        src["export_sheets"] = True
    url = str(src.get("url") or "")
    m = _GSHEET.match(url)
    if src.get("kind") == "url" and m and "/export" not in url:
        gid = _GID.search(url)
        src["url"] = (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export"
                      f"?format=csv" + (f"&gid={gid.group(1)}" if gid else ""))
    return src


def _public_url(url: str) -> str:
    """Scheme, host and path only: a signed link's query string is a credential."""
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname or ''}{p.path}"


def _format_of(rf, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    import os
    ext = os.path.splitext(rf.filename or "")[1].lower()
    fmt = _EXT_FORMAT.get(ext) or _MIME_FORMAT.get((rf.mime or "").lower())
    if fmt is None:
        raise AuthzDenied(400, "unknown_format",
                          f"Can't tell the format of `{rf.filename}` ({rf.mime}): pass "
                          f"`format` (csv or ndjson).")
    return fmt


def _refuse_html(rf, source: dict) -> None:
    head = rf.data[:512].lstrip().lower()
    if rf.mime == "text/html" or head.startswith((b"<!doctype html", b"<html")):
        hint = ("The Google Sheet is not public: share it with \"anyone with the link\", "
                "or use `{kind: \"drive\", file_id}`." if _GSHEET.match(
                    str((source or {}).get("url") or "")) else
                "The link returned a web page, not a file: use a direct download link.")
        raise AuthzDenied(400, "not_a_data_file", hint)


def _import(ctx: ResolvedCtx, inp: ImportInput) -> dict:
    sub = ctx.sub
    deadline = time.monotonic() + IMPORT_BUDGET_S
    if inp.resume_from and not inp.source_sha256:
        raise AuthzDenied(400, "missing_source_sha256",
                          "`resume_from` needs the `source_sha256` of the first receipt.")
    # Authz before the fetch: nothing is downloaded for a target the caller can't write.
    if inp.target == "project_file":
        target = _project_file_target(inp, inp.filename or "file")
    else:
        target = _datastore_target(sub, inp, inp.format or "csv")
    try:
        upload_tokens.check_target_access(sub, target)
    except upload_tokens.UploadError as e:
        raise AuthzDenied(e.status, e.code, e.message)

    source = _source_for_fetch(inp.source)
    try:
        rf = file_source.resolve(source, max_bytes=upload_tokens.max_bytes(),
                                 follow_redirects=True, deadline=deadline)
    except file_source.FileSourceError as e:
        raise AuthzDenied(400, "source_unreadable", str(e))
    sha = hashlib.sha256(rf.data).hexdigest()
    if inp.source_sha256 and inp.source_sha256 != sha:
        raise AuthzDenied(409, "source_changed",
                          "The file changed since the first call: start again without "
                          "`resume_from`.")
    src_info = {"kind": source.get("kind"), "name": rf.filename, "mime": rf.mime,
                "bytes": len(rf.data), "sha256": sha}
    if source.get("kind") == "url":
        src_info["url"] = _public_url(str(source.get("url")))

    try:
        if inp.target == "project_file":
            if not inp.filename:
                target["filename"] = rf.filename
            row = upload_tokens.deposer_fichier_projet(sub, target, rf.data, rf.mime)
            return {"ok": True, "kind": "project_file", "file_id": row["id"],
                    "project_id": row["project_id"], "filename": row["filename"],
                    "bytes": len(rf.data), "source": src_info}
        fmt = _format_of(rf, inp.format)
        _refuse_html(rf, inp.source)
        sep = _SEPARATOR.get(inp.separator or "") or (
            "\t" if (rf.filename or "").lower().endswith(".tsv")
            or rf.mime == "text/tab-separated-values" else None)
        from ..datastore import core as ds  # lazy : évite tout cycle d'import au boot
        store = ds.make_store(sub)
        ns_id = int(target["ns_id"])
        # oto#124 : le NDJSON déclare aussi ses clés neuves (typées d'après leurs
        # valeurs) — une colonne non déclarée est refusée à l'écriture.
        parsed = upload_tokens.parse_import(
            rf.data, fmt, store._schema_of(ns_id),
            separator=sep, declare_columns=inp.declare_columns)
        created = parsed["new_columns"]
        if created:
            store.patch_schema(target["namespace"], fields=created)
        if target.get("key"):
            target["key"] = parsed["header_map"].get(target["key"], target["key"])
        out = upload_tokens.import_rows(sub, target, parsed["rows"], deadline=deadline,
                                        resume_from=inp.resume_from or 0)
    except upload_tokens.UploadError as e:
        raise AuthzDenied(e.status, e.code, e.message, details=e.details)
    receipt = {"ok": True, "kind": "datastore", "datastore": target["namespace"], **out,
               **parsed["info"], "source": src_info}
    if created:
        receipt["created_columns"] = [c["key"] for c in created]
    if parsed["traduits"]:
        receipt["entetes_traduits"] = parsed["traduits"]
    if not out["done"]:
        receipt["hint"] = (f"Not finished: call again with resume_from={out['resume_from']} "
                           f"and source_sha256 (same source).")
    if not target.get("key"):
        receipt["no_key"] = "Rows were appended: pass `key` so a re-run updates instead."
    return receipt


CAPABILITIES += [
    Capability(
        key="me.import", handler=_import, Input=ImportInput, Output=ImportOutput,
        authz=SUB_ONLY,
        errors=(
            DeclaredError(400, "missing_datastore", "`target=datastore` without `datastore`"),
            DeclaredError(400, "missing_project", "`target=project_file` without `project_id`"),
            DeclaredError(404, "unknown_namespace", "the table is not visible from this org"),
            DeclaredError(403, "read_only", "the table is shared read-only"),
            DeclaredError(404, "unknown_project", "the project does not exist"),
            DeclaredError(403, "forbidden", "no write access on the target"),
            DeclaredError(400, "missing_source_sha256", "`resume_from` without `source_sha256`"),
            DeclaredError(400, "source_unreadable",
                          "the source can't be read (link, redirect, size, private host)"),
            DeclaredError(409, "source_changed", "the file changed between two calls"),
            DeclaredError(400, "unknown_format", "the format can't be told: pass `format`"),
            DeclaredError(400, "not_a_data_file",
                          "the link returned a web page (a private Google Sheet, a login)"),
            DeclaredError(400, "not_utf8", "not UTF-8, UTF-16 or cp1252"),
            DeclaredError(400, "binary_content", "binary file (an .xlsx?)"),
            DeclaredError(400, "entete_en_collision", "two headers map to one column"),
            DeclaredError(400, "empty_dataset", "no rows"),
            DeclaredError(400, "bad_ndjson", "an NDJSON line is not an object"),
            DeclaredError(400, "bad_row",
                          "a row was refused: `details` gives the row and `resume_from`"),
            DeclaredError(400, "upsert_without_key",
                          "`upsert=true` on a table with no business key and no `key`"),
            DeclaredError(409, "business_key_exists",
                          "without `upsert=true`, rows of the file share a key value, or "
                          "the file ADDS (no `key`) rows whose key the table already holds: `details` names them "
                          "(`doublons`, `existantes`) — nothing written when judged "
                          "before the first slice, else `resume_from`"),
            DeclaredError(400, "unknown_column",
                          "(oto#124) the file carries a column the "
                          "table's schema does not declare and `declare_columns` did "
                          "not declare it: `details.colonnes` names it, nothing written"),
        ),
        description=(
            "Load a file the server can reach into a table or a project — the content "
            "never goes through the conversation. Use it instead of `data_write` rows for "
            "any file (CSV export, Google Sheet, Clay export, attachment). CSV: `,` `;` or "
            "tab, UTF-8 / UTF-16 / cp1252, headers matched to column keys or labels; new "
            "headers become text columns. Pass `key` so a re-run updates instead of "
            "appending. A big file stops after ~40 s with `resume_from`: call again with "
            "it and `source_sha256`."),
        mcp="oto_import",
        rest=RestBinding("POST", "/api/me/import"),
    ),
]
