"""Jetons d'upload signés — push out-of-bande de contenu volumineux (issue oto-backend#105).

Un agent avec un shell qui doit écrire un GROS contenu (transcript ~95 Ko, dataset,
PDF) dans oto ne doit PAS le faire transiter par le contexte du LLM (coût tokens +
risque de troncature/paraphrase sur du verbatim). `oto_upload_url(target)` (capacité
`me.upload_url`) rend une URL SIGNÉE + scellée sur laquelle l'agent PUT le contenu
hors-bande (`curl --data-binary @fichier`) ; le backend matérialise dans la ressource
cible en RÉAPPLIQUANT son autz. Le body ne repasse jamais par le LLM (ni en entrée ni
en sortie — la réponse est un accusé léger : id + longueur).

Jeton = `<b64url(payload)>.<b64url(sig)>` (même famille que les states OAuth). payload
= {typ:"upload", jti, sub, org, target, exp}. HMAC-SHA256, secret partagé
`OTO_MCP_OAUTH_STATE_SECRET` (déjà provisionné en prod). TTL court (`_TTL`) + **usage
unique** (`db.consume_upload_token`, table `upload_tokens_used`). Le champ `typ` évite
qu'un state OAuth soit rejoué comme jeton d'upload.

⚠️ **Le payload est SIGNÉ, pas chiffré — et c'est accepté** (#562, décision du 23/09) :
qui détient le lien lit `sub`, `org` et la cible. Rien n'y est un secret (la signature
seule ouvre, la cible est réautorisée à la réception), le jeton vit `_TTL` et sert une
fois. Ne pas y sceller un titre ou un nom de fichier confidentiel.

Deux consommateurs pour la MÊME URL signée : un **agent avec shell** (curl PUT du corps
brut) OU, à défaut (claude.ai sans shell), un **humain** à qui l'agent transmet le lien —
l'endpoint sert alors une page d'upload (GET) qui POST le fichier en multipart.

Cibles supportées (`target["kind"]`) :
- `doc`          : page Documents d'un projet — op=create (sous project_id/parent_id)
                   ou op=update d'un doc existant ; body = texte de l'upload (utf-8).
- `project_file` : fichier brut (« Autre document ») — blob durable en Object Storage,
                   comble le gap upload multipart dashboard-only (un agent peut déposer
                   un PDF/CSV).
- `datastore`    : lot de lignes dans un tableau (NDJSON ou CSV) → batch upsert/dedup sur
                   la clé (`target.key`, sinon `schema.key`). ns_id scellé au mint (org
                   active présente) ; autz réappliquée org-agnostiquement au receive.
- `image`        : UNE image publiée à une URL **publique et permanente**
                   (`media_store.upload_image`, préfixe `images/<sub>/`) — la tête d'un
                   `email_send`, réutilisée d'envoi en envoi. Aucune ressource cible :
                   le porteur authentifié est la garde (comme l'avatar). Bornes de
                   `upload_image` : png/jpeg/gif/webp par magic bytes, 2 Mo, clé par
                   hash de contenu (non devinable, ré-upload idempotent).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

# Module-level : `media_store` n'importe que la stdlib (boto3 y est chargé à l'upload),
# et le parcours des refus déclarés (oto#106) ne suit que ce que le module VOIT.
from . import media_store

_TTL = 900  # 15 min — assez pour un curl, assez court pour borner la fenêtre.
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 Mo — plafond dur du contenu poussé.

# Content-Type que curl pose par défaut avec --data-binary : trompeur pour un PDF/CSV,
# on ne s'y fie jamais (on préfère celui déclaré au mint).
_CURL_DEFAULT_CT = "application/x-www-form-urlencoded"


class UploadError(Exception):
    """Échec de validation/autz/matérialisation, traduit en réponse par l'appelant."""

    def __init__(self, status: int, code: str, message: str = "",
                 details: Optional[dict] = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def max_bytes() -> int:
    raw = os.environ.get("OTO_MCP_UPLOAD_MAX_BYTES")
    return int(raw) if raw else _DEFAULT_MAX_BYTES


def _secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise UploadError(500, "no_state_secret", "OTO_MCP_OAUTH_STATE_SECRET manquant.")
    return v.encode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(sub: str, org_id: Optional[int], target: dict, *, ttl: int = _TTL,
         typ: str = "upload") -> tuple[str, int]:
    """Signe un jeton d'upload scellant (sub, org, cible). Renvoie (token, exp_epoch).
    `typ` sépare les familles de jetons (`relay` : le relais Pennylane cabinet) — un
    jeton d'une famille ne s'ouvre jamais sur la route d'une autre."""
    exp = int(time.time()) + ttl
    payload = json.dumps(
        {"typ": typ, "jti": secrets.token_urlsafe(12), "sub": sub,
         "org": org_id, "target": target, "exp": exp},
        separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}", exp


def verify(token: str, typ: str = "upload") -> Optional[dict]:
    """Renvoie le payload si signature valide, `typ` attendu et non expiré ; None sinon.
    NE consomme PAS le jeton (l'usage unique est appliqué à la matérialisation)."""
    if not token or "." not in token:
        return None
    p_b64, sig_b64 = token.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    # noqa: SILENT — fail-closed : toute erreur de vérification ⇒ jeton refusé
    except Exception:
        return None
    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    # noqa: SILENT — fail-closed : toute erreur de vérification ⇒ jeton refusé
    except Exception:
        return None
    if data.get("typ") != typ:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def why_refused(token: str, typ: str = "upload") -> str:
    """Why `verify` refused `token`, in one sentence for the caller: EXPIRED (signed,
    right family, past `exp`) or ALTERED (anything else). Both stay
    `invalid_or_expired_token`; the sentence tells a caller who minted the link seconds
    ago that the TTL is not the problem — the URL it sent is not the one it was given
    (an agent retyping a long signed link into a shell command is the usual cause)."""
    try:
        p_b64, sig_b64 = token.split(".", 1)
        payload = _b64url_decode(p_b64)
        signed = hmac.compare_digest(
            _b64url_decode(sig_b64), hmac.new(_secret(), payload, hashlib.sha256).digest())
        data = json.loads(payload) if signed else None
    # noqa: SILENT — an unreadable token is the "altered" answer
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("typ") == typ:
        return ("This link has expired (it lives 15 minutes and serves once): mint "
                "a new one.")
    return ("This link is not the one that was issued: its signature does not match. "
            "Send the URL exactly as `oto_upload_url` returned it (copy it, never "
            "retype it), or mint a new one.")


def target_label(target: dict, lang: str = "en") -> str:
    """Libellé humain de la cible (page d'upload GET, servie SANS authentification).
    Pas de secret, pas de contenu — et depuis oto#86, plus aucun IDENTIFIANT
    INTERNE (`doc_id`/`project_id`) : la docstring promettait déjà « pas de secret,
    pas de contenu », mais interpolait ces deux id bruts dans le HTML servi à
    n'importe qui porte le lien. Choix délibéré : on n'ajoute PAS de résolution en
    base (nom de projet/titre du doc) pour les remplacer — un aller-retour DB de
    plus sur un chemin anonyme pour un gain d'ergonomie seul ne le vaut pas ici ;
    le libellé s'appuie donc uniquement sur ce que le jeton porte déjà (titre,
    nom de fichier), sans jamais l'id brut. `lang` : `en` (défaut) ou `fr`."""
    fr = lang == "fr"
    k = target.get("kind")
    if k == "doc":
        if target.get("op") == "update":
            return "mise à jour d'une page Documents" if fr else "update of a Documents page"
        return (f"nouvelle page « {target.get('title')} »" if fr
                else f"new page “{target.get('title')}”")
    if k == "project_file":
        return (f"fichier « {target.get('filename')} »" if fr
                else f"file “{target.get('filename')}”")
    if k == "datastore":
        # ⚠️ `namespace` et non `datastore` : cette cible est SCELLÉE dans un jeton
        # d'upload signé. Les jetons déjà émis portent l'ancienne clé, et un jeton ne se
        # réécrit pas — le renommage du 08/09/2026 s'arrête donc à cette frontière, comme
        # il s'arrête au bord du stockage. Lire `datastore` ici rendrait « tableau
        # « None » » sur tout jeton en circulation, sans lever d'erreur.
        fmt = target.get("format", "ndjson")
        return (f"tableau « {target.get('namespace')} » (lot {fmt})" if fr
                else f"table “{target.get('namespace')}” ({fmt} batch)")
    if k == "image":
        return ("image publique (png, jpeg, gif ou webp, 2 Mo max)" if fr
                else "public image (png, jpeg, gif or webp, 2 MB max)")
    return k or "?"


def _parse_rows(data: bytes, fmt: str,
                schema: Optional[dict] = None) -> tuple[list, dict]:
    """Décode le corps d'un upload datastore en lignes. NDJSON (défaut, une ligne = un
    objet JSON) ou CSV (1re ligne = en-tête). Lève `UploadError`.

    Rend `(lignes, en-têtes traduits)`. Le second est **toujours vide hors CSV**, et
    c'est un choix : le NDJSON porte des CLÉS, pas des étiquettes. Une clé pointée y
    est une adresse — le store la range si elle en désigne une, la refuse sinon.
    Traduire ici masquerait un bug d'appelant au lieu de le lui dire."""
    parsed = parse_import(data, fmt, schema)
    return parsed["rows"], parsed["traduits"]


def parse_import(data: bytes, fmt: str, schema: Optional[dict] = None, *,
                 separator: Optional[str] = None,
                 declare_columns: bool = False) -> dict:
    """Rows of an import body, plus how the file was read.

    CSV is read tolerantly (`csv_tolerant`): separator, encoding and headers matched to
    column keys or labels. With `declare_columns`, headers matching no column get a slug
    key and are listed in `new_columns` for the caller to declare. NDJSON carries keys:
    with `declare_columns`, its keys that no column declares are listed in
    `new_columns`, typed from their values (`types_inferes`, oto#124)."""
    from . import csv_tolerant as ct
    if fmt != "csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadError(400, "not_utf8", "Le contenu doit être de l'UTF-8.")
        rows = _ndjson_rows(text)
        return {"rows": rows, "traduits": {}, "info": {},
                "new_columns": _cles_a_declarer(schema, rows) if declare_columns else [],
                "header_map": {}}
    try:
        decoded = ct.decode(data)
    except ct.CsvError as e:
        if e.code == "binary_content":
            raise UploadError(400, "binary_content", str(e))
        raise UploadError(400, "not_utf8", str(e))
    try:
        sep = separator or ct.detect_separator(decoded.text)
        headers, raw = ct.read_rows(decoded.text, sep)
    except ct.CsvError as e:
        raise UploadError(400, e.code, str(e))

    from .datastore.errors import RowValidationError
    from .datastore.points import traduire_les_entetes
    try:
        mapped = ct.map_headers(schema, headers)
        # Legacy dotted translation, signed PUT only: oto_import slugs every new key.
        traduits = {} if declare_columns else traduire_les_entetes(schema, headers)
    except ct.CsvError as e:
        raise UploadError(400, "entete_en_collision", str(e))
    except RowValidationError as e:
        # Une collision d'en-têtes est un refus d'INGESTION, pas de schéma : elle
        # sort en 400 nommé, au même titre que « pas de l'UTF-8 ».
        raise UploadError(400, "entete_en_collision", str(e))
    if declare_columns:
        target, new_columns = _declared_targets(schema, headers, mapped)
        unmatched = []
    else:
        target = {h: traduits.get(h, mapped.target.get(h, h)) for h in headers if h}
        new_columns = []
        unmatched = [h for h in mapped.unmatched if h not in traduits]
    rows = [{target.get(k, k): v for k, v in r.items() if k} for r in raw]
    if not rows:
        raise UploadError(400, "empty_dataset", "Aucune ligne CSV (en-tête requis).")
    info = {"format": "csv", "encoding": decoded.encoding,
            "separator": {"\t": "tab"}.get(sep, sep)}
    if mapped.by_label:
        info["matched_by_label"] = mapped.by_label
    if unmatched:
        info["unmatched_headers"] = unmatched
    empty = [i for i, h in enumerate(headers, 1) if not h]
    if empty:
        info["dropped_empty_headers"] = empty  # 1-based column positions
    return {"rows": rows, "traduits": traduits, "info": info, "new_columns": new_columns,
            "header_map": target}


def _cles_a_declarer(schema: Optional[dict], rows: list) -> list:
    """The NDJSON keys no column declares, typed from the values they carry — the same
    rule as the column freeze (`types_inferes`). A dotted or technical key is not a
    column: the store files or refuses it."""
    from .datastore import colonnes_non_declarees as cnd
    from .datastore import types_inferes as ti
    valeurs: dict = {}
    for r in rows:
        if isinstance(r, dict):
            for k, v in r.items():
                valeurs.setdefault(k, []).append(v)
    return ti.colonnes_a_declarer(valeurs, cnd.declarees(schema))


def _declared_targets(schema: Optional[dict], headers: list, mapped) -> tuple[dict, list]:
    """Every header lands on a DECLARED column: a matched one, an annotation of a
    column in the file, or a new text column keyed by its slug (label = header).
    Dotted headers included: `acme.com` becomes `acme_com`, never an undeclared key."""
    from . import csv_tolerant as ct
    from .datastore import schema as dsv2
    from .datastore.declaration import _fields
    declared = {f.get("key") for f in _fields(schema) if f.get("key")}
    target: dict = {}
    for h in headers:
        if h and (h in declared or h in mapped.by_label):
            target[h] = mapped.target[h]
    bases = declared | {target.get(h, ct.key_for(h)) for h in headers if h and "." not in h}
    used = set(target.values())  # keys already taken by a header of THIS file
    new_columns = []
    for i, h in enumerate(headers, 1):
        if not h or h in target:
            continue
        adresse = dsv2.layer_address(h) if "." in h else None
        if adresse is not None and ct.key_for(adresse[0]) in bases:
            target[h] = f"{ct.key_for(adresse[0])}.{adresse[1]}" \
                if adresse[0] not in declared else h
            continue
        k = ct.key_for(h) or f"column_{i}"
        if k in used:
            raise UploadError(400, "entete_en_collision",
                              f"Header `{h}` would become column `{k}`, already taken by "
                              f"another header. Nothing was imported: rename one of them.")
        used.add(k)
        target[h] = k
        if k not in declared:  # a re-import finds the column it declared last time
            new_columns.append({"key": k, "label": h, "type": "text"})
    return target, new_columns


def _ndjson_rows(text: str) -> list:
    rows = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            raise UploadError(400, "bad_ndjson", f"Ligne NDJSON {i} invalide (objet JSON attendu).")
        if not isinstance(obj, dict):
            raise UploadError(400, "bad_ndjson", f"Ligne NDJSON {i} n'est pas un objet.")
        rows.append(obj)
    if not rows:
        raise UploadError(400, "empty_dataset", "Aucune ligne NDJSON.")
    return rows


#: One `_write_rows_to_ns` call per slice: a budget stops between slices, never inside.
IMPORT_SLICE = 500


def import_rows(sub: str, target: dict, rows: list, *, deadline: float,
                resume_from: int = 0) -> dict:
    """Write `rows[resume_from:]` into a table in slices, until done or `deadline`
    (`time.monotonic()`). A refusal names the absolute row and what is already written.
    Assumes authz checked (`check_target_access`) and columns already declared."""
    from . import db  # lazy : évite tout cycle d'import au boot
    from .datastore import colonnes_non_declarees as cnd
    from .datastore import core as ds
    from .datastore import mots_deprecies as mdp
    from .datastore import upsert_implicite as upi
    store = ds.make_store(sub)
    ns_id = int(target["ns_id"])
    # Retired-word refusals are judged on the whole file before the first slice.
    mdp.controler(*(r for r in rows[resume_from:] if isinstance(r, dict)))
    # oto#124: so are undeclared columns — nothing written.
    try:
        cnd.juger_le_lot(store._schema_of(ns_id), rows[resume_from:])
    except ds.ColonneNonDeclaree as e:
        raise UploadError(400, "unknown_column", str(e), details={
            **(e.details or {}), "row": resume_from + 1, "written": 0,
            "resume_from": resume_from})
    # oto#141: so is the business key (without `upsert`): duplicates
    # inside the file — and, when the file ADDS (no `key` named at the call), keys
    # already in the table — refuse the WHOLE file before the first slice. One record
    # for all slices, so that `fusions` carries the file's ranks and `dans_rang`
    # crosses slices.
    key, upsert = target.get("key"), bool(target.get("upsert"))
    cle_passee = bool(target.get("cle_passee"))
    fusions = upi.Fusions()
    fusions.decalage = resume_from
    if key and not upsert:
        try:
            upi.juger_le_lot(
                rows[resume_from:], key=key, decalage=resume_from,
                designation=upi.designe(store._schema_of(ns_id), cle_passee),
                datastore=target.get("namespace") or f"#{ns_id}",
                textes=db.datastore_textes_de_cle,
                chercher=lambda kv: db.datastore_find_row_id_by_key(ns_id, key, kv))
        except ds.BusinessKeyExists as e:
            raise UploadError(409, "business_key_exists", str(e), details={
                **(e.details or {}), "row": resume_from + 1, "written": 0,
                "resume_from": resume_from})
        fusions.juge = True
    inserted = updated = 0
    fusionnees: list = []
    i, last = resume_from, 0.0
    while i < len(rows):
        if i > resume_from and time.monotonic() + last > deadline:
            break
        start = time.monotonic()
        store._lot_rang = 0
        fusions.decalage = i
        try:
            out = store._write_rows_to_ns(
                ns_id, rows[i:i + IMPORT_SLICE], key=key,
                origine_override=bool(target.get("origine_override")),
                donnees_d_origine=bool(target.get("donnees_d_origine")),
                upsert=upsert, cle_passee=cle_passee, fusions=fusions)
        except ds.BusinessKeyExists as e:
            # A race lost after the whole-file judgement: the row is named, what is
            # already written stays, and the resume point is said — like `bad_row`.
            rang = getattr(store, "_lot_rang", 0) or 1
            done = i + rang - 1
            raise UploadError(409, "business_key_exists", str(e), details={
                **(e.details or {}), "row": done + 1,
                "written": inserted + updated + rang - 1, "resume_from": done + 1})
        except ds.ColonneNonDeclaree as e:
            # oto#124: a column that appeared after the whole-file judgement (a race
            # on the schema). Named like `bad_row`, with its own code.
            rang = getattr(store, "_lot_rang", 0) or 1
            done = i + rang - 1
            raise UploadError(400, "unknown_column", str(e), details={
                **(e.details or {}), "row": done + 1,
                "written": inserted + updated + rang - 1, "resume_from": done + 1})
        except ValueError as e:
            rang = getattr(store, "_lot_rang", 0) or 1
            done = i + rang - 1
            raise UploadError(400, "bad_row", str(e), details={
                "row": done + 1, "written": inserted + updated + rang - 1,
                "resume_from": done + 1})
        inserted, updated = inserted + out["inserted"], updated + out["updated"]
        fusionnees += out.get("fusions") or []
        i += IMPORT_SLICE
        last = time.monotonic() - start
    done = min(i, len(rows))
    return {"inserted": inserted, "updated": updated, "count": inserted + updated,
            "total_rows": len(rows), "done": done >= len(rows),
            **({} if done >= len(rows) else {"resume_from": done}),
            **({"fusions": fusionnees} if fusionnees else {}),
            **store.off_schema_report()}


def check_target_access(sub: str, target: dict) -> None:
    """Vérifie l'accès ÉCRITURE à la cible. Lève `UploadError` sinon. Appelée au mint
    (fail-fast) ET à la réception — le jeton ne fait pas foi seul, l'autz de la cible
    est réappliquée à l'écriture (verrou IDOR : ADR 0009/0030)."""
    from . import db, ownership  # lazy : évite tout cycle d'import au boot
    kind = target.get("kind")
    if kind == "image":
        # Pas de ressource cible : l'image n'appartient à rien d'autre qu'à son
        # déposant. La garde est le sub scellé dans le jeton (même régime que l'avatar).
        return
    if kind == "datastore":
        ns_id = target.get("ns_id")
        if ns_id is None:
            raise UploadError(400, "missing_namespace", "Tableau requis.")
        if not ownership.can_access(sub, "datastore_namespace", str(ns_id), "write"):
            raise UploadError(403, "forbidden", "Écriture refusée sur ce tableau.")
        return
    pid = target.get("project_id")
    if kind == "doc" and target.get("op") == "update":
        row = db.get_doc_by_id(int(target["doc_id"]))
        if row is None:
            raise UploadError(404, "unknown_doc", f"Doc #{target.get('doc_id')} inconnu.")
        pid = row["project_id"]
    if pid is None:
        raise UploadError(400, "missing_project", "Projet requis.")
    if db.get_project_by_id(int(pid)) is None:
        raise UploadError(404, "unknown_project", f"Projet #{pid} inconnu.")
    if not ownership.can_access(sub, "project", str(pid), "write"):
        raise UploadError(403, "forbidden", "Écriture refusée sur ce projet.")
    # Le parent d'une page créée appartient au MÊME projet — la garde d'`oto_doc
    # op=create` (`capabilities/docs/writes.create`) : sans elle, un parent d'un autre
    # projet rattachait la page à un arbre que l'appelant ne peut pas écrire. Jugée au
    # mint ET à la réception, comme le reste de cette fonction.
    if kind == "doc" and target.get("op") != "update" and target.get("parent_id") is not None:
        parent = db.get_doc_by_id(int(target["parent_id"]))
        if not parent or parent["project_id"] != int(pid):
            raise UploadError(400, "bad_parent", "Parent invalide (autre projet ou inexistant).")


def _resolve_content_type(target: dict, request_ct: Optional[str]) -> str:
    """Type déclaré au mint > type de la requête (sauf défaut curl trompeur) > octet-stream."""
    if target.get("content_type"):
        return target["content_type"]
    if request_ct and request_ct.split(";")[0].strip() != _CURL_DEFAULT_CT:
        return request_ct
    return "application/octet-stream"


def deposer_fichier_projet(sub: str, target: dict, data: bytes,
                           request_ct: Optional[str]) -> dict:
    """Range `data` en fichier du projet de `target` et rend la LIGNE créée (son id
    compris). Suppose l'autz déjà vérifiée (`check_target_access`).

    Deux appelants, deux accusés : le lien signé (`materialize`) répond à un porteur
    ANONYME et n'en recopie que le nom (oto#86) ; `oto_import`, appelé par un compte
    authentifié qui a le droit d'écrire le projet, rend l'id du fichier créé."""
    from . import db  # lazy : évite tout cycle d'import au boot
    pid = int(target["project_id"])
    filename = target.get("filename") or "file"
    # Le type DÉCLARÉ ne fait que départager ; ce qui est écrit et enregistré est
    # le type SERVI, décidé sur le contenu (#562).
    ctype = media_store.type_servi(data, _resolve_content_type(target, request_ct)).content_type
    try:
        key = media_store.upload_object("project-files", str(pid), data, ctype,
                                        filename, max_bytes=max_bytes())
    except media_store.MediaError as e:
        raise UploadError(e.status, e.code, str(e))
    row = db.add_project_file(pid, key, filename, mime=ctype, size_bytes=len(data),
                              title=target.get("title"),
                              description=target.get("description"), created_by=sub)
    db.log_project_activity(pid, sub, "project.file_add", target.get("title") or filename)
    return row


def materialize(sub: str, target: dict, data: bytes, request_ct: Optional[str]) -> dict:
    """Écrit le contenu poussé dans la ressource cible. Renvoie un accusé léger
    (jamais le body). Suppose l'autz déjà vérifiée (`check_target_access`)."""
    from . import db  # lazy : évite tout cycle d'import au boot
    kind = target.get("kind")

    if kind == "doc":
        try:
            body_md = data.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadError(400, "not_utf8", "Une page Documents doit être du texte UTF-8.")
        # oto#86 : même verdict que la branche project_file, trouvé incomplet par
        # fleet — cette branche rendait `doc_id` (et `project_id` sur `create`) à
        # l'anonyme porteur du lien, alors que le libellé HTML de la MÊME route
        # (`target_label`) a été corrigé pour ne JAMAIS les porter. Les deux
        # verdicts ne peuvent pas coexister : l'accusé ne porte que ce qui suit.
        if target.get("op") == "update":
            did = int(target["doc_id"])
            db.update_doc(did, body_md=body_md, edited_by=sub)
            row = db.get_doc_by_id(did)
            db.log_project_activity(row["project_id"], sub, "doc.update", row.get("title"))
            return {"ok": True, "kind": "doc", "op": "update",
                    "bytes": len(data), "chars": len(body_md)}
        pid = int(target["project_id"])
        did = db.create_doc(pid, target["title"], parent_id=target.get("parent_id"),
                            body_md=body_md, kind=target.get("doc_kind") or "source",
                            created_by=sub)
        db.log_project_activity(pid, sub, "doc.create", target["title"])
        return {"ok": True, "kind": "doc", "op": "create",
                "bytes": len(data), "chars": len(body_md)}

    if kind == "project_file":
        row = deposer_fichier_projet(sub, target, data, request_ct)
        # Allow-list, pas retrait (oto#86) : la ligne de base porte, entre autres,
        # l'identifiant du compte déposant (`created_by`), l'id interne de la ligne
        # et du projet, l'état de publication — un `row.pop("s3_key")` n'en retirait
        # qu'UN. Ce lien est CONÇU pour être transmis à un tiers sans shell
        # (docstring du module) : il n'a besoin de savoir que c'est passé, la
        # taille, et le nom du fichier.
        from . import redaction
        return {"ok": True, "kind": "project_file",
                **redaction.champs_autorises(row, "filename"), "bytes": len(data)}

    if kind == "datastore":
        from .datastore import core as ds  # lazy : évite tout cycle d'import au boot
        from .datastore import upsert_implicite as upi
        store = ds.make_store(sub)
        # Le schéma n'entre en jeu que pour un CSV, seul format qui porte des
        # EN-TÊTES : une colonne déclarée rend `site_web.comment` lisible comme une
        # annotation (le store la rangera), et une cible de traduction déclarée est la
        # colonne que vise chaque rechargement. Le NDJSON porte des clés — il n'a rien
        # à traduire.
        fmt = target.get("format") or "ndjson"
        parsed = parse_import(
            data, fmt,
            store._schema_of(int(target["ns_id"])) if fmt == "csv" else None)
        rows, entetes_traduits = parsed["rows"], parsed["traduits"]
        try:
            out = store._write_rows_to_ns(
                int(target["ns_id"]), rows, key=target.get("key"),
                # oto#70 lot 2 : la déclaration a été faite au MINT et scellée dans
                # le jeton. Un import qui pose la couche `origine` la déclare donc
                # comme n'importe quel autre écrivain — mais celui qui appelle le
                # PUT ne peut pas se l'accorder lui-même, il ne fait que livrer des
                # octets à une URL signée.
                origine_override=bool(target.get("origine_override")),
                # oto#140 : déclaré au MINT lui aussi. Un fichier qui EST la donnée de
                # la cliente fige la version d'origine de chaque case au moment où
                # elle entre — plus d'ordre de gestes à respecter, plus de cran à
                # déclarer avant, donc plus de « (origine inconnue) » à découvrir
                # trois semaines plus tard.
                donnees_d_origine=bool(target.get("donnees_d_origine")),
                # oto#141 : déclaré au MINT et scellé comme les deux précédents — celui
                # qui livre les octets ne décide pas qu'ils fusionnent. Un jeton frappé
                # sans lui (ou avant lui) vaut `false`. L'accusé est lu par un porteur
                # de lien ANONYME : ni `fusions` ni le refus n'y nomment une ligne par
                # son identifiant interne (oto#86).
                upsert=bool(target.get("upsert")),
                # oto#141 : la clé NOMMÉE au mint (scellée) DÉSIGNE ; sinon le fichier
                # AJOUTE. Un jeton d'avant ce champ ajoute.
                cle_passee=bool(target.get("cle_passee")),
                fusions=upi.Fusions(nommer=False))
        except ds.BusinessKeyExists as e:
            # 409 et non `bad_row` : la requête est bien formée, c'est l'ÉTAT du tableau
            # qui s'y oppose — le refus nomme les lignes et les deux gestes.
            raise UploadError(409, "business_key_exists", str(e), details=e.details)
        except ds.ColonneNonDeclaree as e:
            # oto#124 : son code, comme la face REST — le geste est de déclarer.
            raise UploadError(400, "unknown_column", str(e), details=e.details)
        except ValueError as e:
            raise UploadError(400, "bad_row", str(e))
        # Le chemin de bulk load est celui où le silence coûte le plus cher (#294) :
        # un format renommé + un lot de 500 lignes, et tout atterrit hors schéma.
        # ⚠️ Cette réponse servait encore `namespace` SEUL : elle n'avait jamais
        # basculé, alors que la liste l'avait fait à sec et les réponses unitaires en
        # doublon. Trois politiques dans un même produit — c'est l'incohérence qui a
        # décidé la bascule sèche du 10/09/2026, pas la rupture elle-même.
        rendu = {"ok": True, "kind": "datastore", "datastore": target.get("namespace"),
                 "inserted": out["inserted"], "updated": out["updated"],
                 "count": out["count"], "bytes": len(data),
                 **({"fusions": out["fusions"]} if out.get("fusions") else {}),
                 **parsed["info"], **store.off_schema_report()}
        if entetes_traduits:
            # DITE, jamais silencieuse : sans cette ligne, le client reçoit une colonne
            # qu'il ne peut plus retrouver par le nom qu'il lui a donné — exactement le
            # défaut qu'on ferme côté écriture, rouvert côté import.
            rendu["entetes_traduits"] = entetes_traduits
            rendu["entetes_traduits_hint"] = (
                "Un point ne peut pas figurer dans un nom de colonne (il y désigne une "
                "annotation de la colonne qui le précède). Ces en-têtes ont été "
                "traduits : "
                + ", ".join(f"`{a}` → `{b}`"
                            for a, b in sorted(entetes_traduits.items()))
                + ". Pour choisir toi-même le nom, renomme la colonne dans le fichier "
                  "source et recharge.")
        return rendu

    if kind == "image":
        # Le type déclaré (curl, formulaire) n'est jamais cru : `upload_image` le
        # dérive des magic bytes et refuse tout ce qui n'est pas une image.
        try:
            url = media_store.upload_image("images", sub, data, "")
        except media_store.MediaError as e:
            raise UploadError(e.status, e.code, str(e))
        return {"ok": True, "kind": "image", "url": url, "bytes": len(data)}

    raise UploadError(400, "unknown_target", f"Cible inconnue : {kind!r}.")
