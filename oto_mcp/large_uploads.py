"""Gros fichiers de projet (vidéos de plusieurs Go) : dépôt multipart DIRECT au stockage.

Le lien d'upload signé (`upload_tokens`, #105) lit le corps dans le backend, borné à
25 Mo : au-delà, un fichier passerait des minutes dans un handler, et tiendrait la
mémoire d'un serveur mono-boucle. Ici, les octets ne passent JAMAIS par le backend :

1. **Ouverture** (`oto_upload_url target=project_file large=true`) : le backend ouvre un
   dépôt multipart sur une clé qu'il choisit (`project-files/<projet>/<aléa>/<nom>`) et
   SCELLE la clé et l'`UploadId` dans un jeton signé (`typ=upload_large`). Le client ne
   fournit ni l'une ni l'autre : il ne peut viser que ce dépôt-là.
2. **Parties** (`GET …/part/<n>`) : une URL PUT présignée par partie, à la demande (une
   heure). Le client y envoie `PART_SIZE` octets bruts, la dernière partie est plus
   courte. Rien ne se compte côté backend : le stockage tient l'état.
3. **Clôture** (`POST …/complete`) : le backend LISTE lui-même les parties reçues (le
   client ne renvoie aucun ETag), vérifie qu'elles se suivent depuis 1, que toutes sauf
   la dernière font `PART_SIZE`, que le total tient sous le plafond, clôt, relit le
   début du fichier (type, #562) et range la ligne `project_files`. Le jeton est
   CONSOMMÉ ici (usage unique), pas à la première partie.
4. **Abandon** (`DELETE …`) : le dépôt est abandonné, ses parties libérées. Un dépôt
   jamais clos est abandonné par la règle de cycle de vie du seau à 2 jours
   (`infra/scaleway/media-versioning.sh`).

5. **Échéance** : un gros fichier est un TRANSIT (le montrer, le relayer vers YouTube ou
   une autre plateforme), pas une archive. Il est rangé avec une échéance
   (`project_files.expires_at`, `keep_days()` : 14 jours par défaut), dite à chaque
   surface — la réponse d'`oto_upload_url`, la page d'upload, l'accusé, les listes de
   fichiers. Passée, `fichiers_echus` retire l'objet et la ligne.

À chaque étape, l'identité scellée repasse par la garde (`garde_identite`) et l'écriture
du projet est REJUGÉE (`upload_tokens.check_target_access`) : un jeton de 24 h ne survit
pas à un accès retiré.

Limites Scaleway : 1 000 parties, 5 Mo à 5 Go chacune. `PART_SIZE` fixe (100 Mio par
défaut) : la page d'upload d'un humain ne connaît la taille qu'une fois le fichier
choisi, donc la taille n'est pas déclarée à l'ouverture — le plafond se juge à la
clôture, et à chaque partie demandée (`n × PART_SIZE`).
"""
from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta, timezone
import os
import secrets
from typing import Optional
from urllib.parse import quote

from . import media_store, upload_tokens

TYP = "upload_large"
#: Le plafond du dépôt scaleway en parties.
MAX_PARTS = 1000
_MIB = 1024 * 1024


def part_size() -> int:
    raw = os.environ.get("OTO_MCP_LARGE_UPLOAD_PART_BYTES")
    taille = int(raw) if raw else 100 * _MIB
    # En Mio entiers : le découpage côté client (`dd bs=1M`) compte en Mio.
    return max(5, min(taille // _MIB, 5 * 1024)) * _MIB


def max_bytes() -> int:
    """Le plafond d'un gros fichier (50 Gio par défaut), jamais au-delà de ce que
    1 000 parties de `part_size()` peuvent porter."""
    raw = os.environ.get("OTO_MCP_LARGE_UPLOAD_MAX_BYTES")
    plafond = int(raw) if raw else 50 * 1024 * _MIB
    return min(plafond, MAX_PARTS * part_size())


def ttl() -> int:
    """Durée de vie du lien : 24 h par défaut (un fichier de 50 Go sur une ligne lente),
    jamais au-delà — la table d'usage unique oublie un jeton consommé après un jour."""
    raw = os.environ.get("OTO_MCP_LARGE_UPLOAD_TTL_S")
    return max(600, min(int(raw) if raw else 86400, 86400))


def keep_days() -> int:
    """Combien de jours un gros fichier reste dans le projet après son dépôt : 14 par
    défaut. Passé ce délai, `fichiers_echus` le retire (objet et ligne) — un gros fichier
    est un TRANSIT (montrer, relayer vers une plateforme), pas une archive."""
    raw = os.environ.get("OTO_MCP_LARGE_UPLOAD_KEEP_DAYS")
    return max(1, min(int(raw) if raw else 14, 365))


def retention_notice(lang: str = "en") -> str:
    """La phrase dite à l'utilisateur partout où le gros fichier se dépose."""
    j = keep_days()
    if lang == "fr":
        return (f"Les gros fichiers sont gardés {j} jours après le dépôt, puis supprimés "
                "automatiquement. Télécharge-le ou envoie-le ailleurs avant.")
    return (f"Large files are kept for {j} days after the upload, then deleted "
            "automatically. Download it or send it on before then.")


#: Une URL de partie vit une heure : une partie de 100 Mio passe largement.
PART_URL_TTL = 3600


def _type_declare(target: dict) -> str:
    declare = target.get("content_type")
    if not declare:
        declare = mimetypes.guess_type(target.get("filename") or "")[0]
    return declare or "application/octet-stream"


def ouvrir(sub: str, org_id: Optional[int], target: dict) -> tuple[str, int, dict]:
    """Ouvre le dépôt multipart d'un fichier de projet et rend `(jeton, exp, cible)`.
    Suppose l'autz déjà jugée (`check_target_access`)."""
    pid = int(target["project_id"])
    filename = target.get("filename") or "file"
    servi = media_store.type_servi_gros(_type_declare(target))
    key = (f"project-files/{quote(str(pid), safe='')}/{secrets.token_hex(16)}/"
           f"{media_store.nom_sur(filename)}")
    upload_id = media_store.ouvrir_multipart(
        key, servi.content_type, filename if servi.attachment else None)
    scelle = {**target, "kind": "project_file_large", "key": key, "upload_id": upload_id,
              "served_type": servi.content_type, "part_size": part_size(),
              "max_bytes": max_bytes()}
    jeton, exp = upload_tokens.sign(sub, org_id, scelle, ttl=ttl(), typ=TYP)
    return jeton, exp, scelle


def verifier(jeton: str) -> Optional[dict]:
    return upload_tokens.verify(jeton, typ=TYP)


def juger(payload: dict) -> None:
    """Rejuge l'identité et l'écriture du projet — à CHAQUE étape. Lève `UploadError`."""
    from . import garde_identite
    sub, target = payload["sub"], payload["target"]
    if (coupe := garde_identite.refus(sub)):
        raise upload_tokens.UploadError(403, *coupe)
    upload_tokens.check_target_access(sub, {**target, "kind": "project_file"})


def url_de_partie(payload: dict, numero: int) -> str:
    target = payload["target"]
    if not 1 <= numero <= MAX_PARTS:
        raise upload_tokens.UploadError(400, "bad_part_number",
                                        f"Part numbers go from 1 to {MAX_PARTS}.")
    if (numero - 1) * int(target["part_size"]) >= int(target["max_bytes"]):
        raise upload_tokens.UploadError(
            413, "content_too_large",
            f"Part {numero} would start beyond the {target['max_bytes']} bytes ceiling.")
    juger(payload)
    try:
        return media_store.presign_partie(target["key"], target["upload_id"], numero,
                                          expiry=PART_URL_TTL)
    except media_store.MediaError as e:
        raise upload_tokens.UploadError(e.status, e.code, str(e))


def _juger_parties(parties: list[dict], taille_partie: int, plafond: int) -> int:
    """Le total des parties reçues, ou un refus nommé : 1..n sans trou, toutes de
    `taille_partie` sauf la dernière, sous le plafond."""
    if not parties:
        raise upload_tokens.UploadError(400, "empty_body", "No part was received.")
    manquantes = sorted(set(range(1, parties[-1]["n"] + 1)) - {p["n"] for p in parties})
    if manquantes:
        raise upload_tokens.UploadError(
            400, "missing_parts", f"Parts missing: {manquantes[:20]} — send them, then "
                                  "complete again.")
    for p in parties[:-1]:
        if p["size"] != taille_partie:
            raise upload_tokens.UploadError(
                400, "bad_part_size",
                f"Part {p['n']} is {p['size']} bytes: every part but the last is exactly "
                f"{taille_partie} bytes. Send it again, then complete.")
    total = sum(p["size"] for p in parties)
    if total > plafond:
        raise upload_tokens.UploadError(413, "content_too_large",
                                        f"{total} bytes, ceiling {plafond}.")
    return total


def terminer(payload: dict) -> dict:
    """Clôt le dépôt et range le fichier dans le projet. Rend l'accusé LÉGER du lien
    signé (oto#86) : le nom et la taille, jamais un identifiant interne."""
    from . import db, redaction
    sub, target = payload["sub"], payload["target"]
    juger(payload)
    key, upload_id = target["key"], target["upload_id"]
    nb_parties = None
    try:
        parties = media_store.parties_recues(key, upload_id)
    except media_store.MediaError as e:
        if e.code != "upload_not_found":
            raise upload_tokens.UploadError(e.status, e.code, str(e))
        # Le dépôt n'existe plus : déjà clos (une clôture précédente a échoué APRÈS la
        # fermeture — reprise ici), ou abandonné (rien à ranger). L'objet le dit.
        try:
            total = media_store.taille_objet(key)
        except media_store.MediaError:
            raise upload_tokens.UploadError(409, "token_already_used") from None
    else:
        total = _juger_parties(parties, int(target["part_size"]), int(target["max_bytes"]))
        nb_parties = len(parties)
        # FERMER d'abord, consommer ensuite : une panne passagère entre les deux laisse
        # un objet clos que la clôture suivante range (branche ci-dessus) — l'inverse
        # laissait un dépôt de 50 Go que plus aucun lien ne pouvait clore.
        try:
            media_store.clore_multipart(key, upload_id, parties)
        except media_store.MediaError as e:
            raise upload_tokens.UploadError(e.status, e.code, str(e))
    # Usage unique : deux clôtures concurrentes ne rangent qu'une ligne.
    if not db.consume_upload_token(payload["jti"]):
        raise upload_tokens.UploadError(409, "token_already_used")
    servi = target["served_type"]
    if servi != "application/octet-stream":
        # #562 : un type passif déclaré ne couvre pas un contenu actif. Une signature
        # de format reconnue fait foi (un zip ou un EPUB porte du XML) ; sans elle, un
        # début de page web est RETIRÉ — rien ne se renomme sur un objet de plusieurs Go.
        try:
            debut = media_store.lire_debut(key)
        except media_store.MediaError as e:
            media_store.delete_by_key(key)
            raise upload_tokens.UploadError(e.status, e.code, str(e))
        if media_store.debut_actif(debut, servi):
            media_store.delete_by_key(key)
            raise upload_tokens.UploadError(
                400, "active_content",
                f"The file starts like a web page, not a {servi} file: it was removed. "
                "Ask for a new link with content_type=application/octet-stream to "
                "store it as a download.")
    pid = int(target["project_id"])
    filename = target.get("filename") or "file"
    echeance = datetime.now(timezone.utc) + timedelta(days=keep_days())
    row = db.add_project_file(pid, key, filename, mime=servi, size_bytes=total,
                              title=target.get("title"),
                              description=target.get("description"), created_by=sub,
                              expires_at=echeance)
    db.log_project_activity(pid, sub, "project.file_add", target.get("title") or filename)
    return {"ok": True, "kind": "project_file",
            **redaction.champs_autorises(row, "filename"), "bytes": total,
            "expires_at": echeance.replace(microsecond=0).isoformat(),
            "notice": retention_notice(),
            **({"parts": nb_parties} if nb_parties is not None else {})}


def abandonner(payload: dict) -> dict:
    target = payload["target"]
    juger(payload)
    from . import db
    if not db.consume_upload_token(payload["jti"]):
        raise upload_tokens.UploadError(409, "token_already_used")
    media_store.abandonner_multipart(target["key"], target["upload_id"])
    return {"ok": True, "kind": "project_file", "aborted": True}


def libelle(target: dict, lang: str = "en") -> str:
    """Libellé de la page d'upload (servie sans authentification) : le nom seul."""
    gio = max_bytes() / (1024 * _MIB)
    j = keep_days()
    if lang == "fr":
        return (f"fichier « {target.get('filename')} » (jusqu’à {gio:.0f} Go, gardé "
                f"{j} jours)")
    return f"file “{target.get('filename')}” (up to {gio:.0f} GB, kept {j} days)"
