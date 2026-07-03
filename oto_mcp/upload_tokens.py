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

_TTL = 900  # 15 min — assez pour un curl, assez court pour borner la fenêtre.
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 Mo — plafond dur du contenu poussé.

# Content-Type que curl pose par défaut avec --data-binary : trompeur pour un PDF/CSV,
# on ne s'y fie jamais (on préfère celui déclaré au mint).
_CURL_DEFAULT_CT = "application/x-www-form-urlencoded"


class UploadError(Exception):
    """Échec de validation/autz/matérialisation, traduit en réponse par l'appelant."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message


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


def sign(sub: str, org_id: Optional[int], target: dict, *, ttl: int = _TTL) -> tuple[str, int]:
    """Signe un jeton d'upload scellant (sub, org, cible). Renvoie (token, exp_epoch)."""
    exp = int(time.time()) + ttl
    payload = json.dumps(
        {"typ": "upload", "jti": secrets.token_urlsafe(12), "sub": sub,
         "org": org_id, "target": target, "exp": exp},
        separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}", exp


def verify(token: str) -> Optional[dict]:
    """Renvoie le payload si signature valide, typ==upload et non expiré ; None sinon.
    NE consomme PAS le jeton (l'usage unique est appliqué à la matérialisation)."""
    if not token or "." not in token:
        return None
    p_b64, sig_b64 = token.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if data.get("typ") != "upload":
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def target_label(target: dict) -> str:
    """Libellé humain de la cible (page d'upload GET). Pas de secret, pas de contenu."""
    k = target.get("kind")
    if k == "doc":
        if target.get("op") == "update":
            return f"mise à jour de la page Documents #{target.get('doc_id')}"
        return f"nouvelle page « {target.get('title')} » (projet #{target.get('project_id')})"
    if k == "project_file":
        return f"fichier « {target.get('filename')} » (projet #{target.get('project_id')})"
    if k == "datastore":
        return f"tableau « {target.get('namespace')} » (lot {target.get('format', 'ndjson')})"
    return k or "?"


def _parse_rows(data: bytes, fmt: str) -> list:
    """Décode le corps d'un upload datastore en lignes (list[dict]). NDJSON (défaut,
    une ligne = un objet JSON) ou CSV (1re ligne = en-tête). Lève `UploadError`."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError(400, "not_utf8", "Le contenu doit être de l'UTF-8.")
    if fmt == "csv":
        import csv
        import io
        rows = [dict(r) for r in csv.DictReader(io.StringIO(text))]
        if not rows:
            raise UploadError(400, "empty_dataset", "Aucune ligne CSV (en-tête requis).")
        return rows
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


def check_target_access(sub: str, target: dict) -> None:
    """Vérifie l'accès ÉCRITURE à la cible. Lève `UploadError` sinon. Appelée au mint
    (fail-fast) ET à la réception — le jeton ne fait pas foi seul, l'autz de la cible
    est réappliquée à l'écriture (verrou IDOR : ADR 0009/0030)."""
    from . import db, ownership  # lazy : évite tout cycle d'import au boot
    kind = target.get("kind")
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


def _resolve_content_type(target: dict, request_ct: Optional[str]) -> str:
    """Type déclaré au mint > type de la requête (sauf défaut curl trompeur) > octet-stream."""
    if target.get("content_type"):
        return target["content_type"]
    if request_ct and request_ct.split(";")[0].strip() != _CURL_DEFAULT_CT:
        return request_ct
    return "application/octet-stream"


def materialize(sub: str, target: dict, data: bytes, request_ct: Optional[str]) -> dict:
    """Écrit le contenu poussé dans la ressource cible. Renvoie un accusé léger
    (jamais le body). Suppose l'autz déjà vérifiée (`check_target_access`)."""
    from . import db, media_store  # lazy : évite tout cycle d'import au boot
    kind = target.get("kind")

    if kind == "doc":
        try:
            body_md = data.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadError(400, "not_utf8", "Une page Documents doit être du texte UTF-8.")
        if target.get("op") == "update":
            did = int(target["doc_id"])
            db.update_doc(did, body_md=body_md, edited_by=sub)
            row = db.get_doc_by_id(did)
            db.log_project_activity(row["project_id"], sub, "doc.update", row.get("title"))
            return {"ok": True, "kind": "doc", "op": "update", "doc_id": did,
                    "bytes": len(data), "chars": len(body_md)}
        pid = int(target["project_id"])
        did = db.create_doc(pid, target["title"], parent_id=target.get("parent_id"),
                            body_md=body_md, kind=target.get("doc_kind") or "source",
                            created_by=sub)
        db.log_project_activity(pid, sub, "doc.create", target["title"])
        return {"ok": True, "kind": "doc", "op": "create", "doc_id": did,
                "project_id": pid, "bytes": len(data), "chars": len(body_md)}

    if kind == "project_file":
        pid = int(target["project_id"])
        filename = target.get("filename") or "file"
        ctype = _resolve_content_type(target, request_ct)
        try:
            key = media_store.upload_object("project-files", str(pid), data, ctype,
                                            filename, max_bytes=max_bytes())
        except media_store.MediaError as e:
            raise UploadError(e.status, e.code, str(e))
        row = db.add_project_file(pid, key, filename, mime=ctype, size_bytes=len(data),
                                  title=target.get("title"),
                                  description=target.get("description"), created_by=sub)
        db.log_project_activity(pid, sub, "project.file_add", target.get("title") or filename)
        row.pop("s3_key", None)
        return {"ok": True, "kind": "project_file", "file": row, "bytes": len(data)}

    if kind == "datastore":
        from . import datastore as ds  # lazy : évite tout cycle d'import au boot
        rows = _parse_rows(data, target.get("format") or "ndjson")
        try:
            out = ds.make_store(sub)._write_rows_to_ns(
                int(target["ns_id"]), rows, key=target.get("key"))
        except ValueError as e:
            raise UploadError(400, "bad_row", str(e))
        return {"ok": True, "kind": "datastore", "namespace": target.get("namespace"),
                "inserted": out["inserted"], "updated": out["updated"],
                "count": out["count"], "bytes": len(data)}

    raise UploadError(400, "unknown_target", f"Cible inconnue : {kind!r}.")
