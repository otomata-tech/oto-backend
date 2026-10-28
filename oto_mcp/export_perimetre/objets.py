"""Les objets du stockage objet d'un périmètre : une ARCHIVE chiffrée pour la cible (#1088).

Décision d'Alexis du 28/09/2026 : les objets voyagent par une archive, et par elle
seule. L'export (chez nous) lit chaque objet du périmètre dans notre stockage et
l'écrit dans l'archive, chiffré sous la clé de l'instance CIBLE, comme les secrets ;
l'import (chez elle) la verse dans son stockage, avec ses propres identifiants. Aucune
copie directe d'un seau à l'autre, aucune URL signée.

**Quels objets.** Ceux que désigne une ligne du périmètre :
- par une CLÉ (`COLONNES_DE_CLES`) : `project_files.s3_key`, `transcription_jobs.audio_key`,
  `app_files.s3_key` ;
- par une URL de notre stockage public, où qu'elle soit : dans une colonne d'URL
  (`COLONNES_D_URL`) comme dans un contenu (le corps d'une page, un JSON). Une image
  qu'un agent a déposée (`images/<sub>/…`) n'a d'autre trace que son URL, collée
  n'importe où : on la trouve en cherchant `<base publique>/<chemin>` dans chaque ligne.
  Le chemin n'est pas la clé : les URL stockées la citent encodée d'un niveau, et la
  clé s'en tire en décodant le chemin une fois (`cle_du_chemin`).
Chaque objet garde sa CLÉ sur la cible ; les URL, elles, sont réécrites vers la base
publique de la cible à l'import (`transformation`) : seule la base change, le chemin
reste tel quel.

**L'archive** : un `tar` dont chaque membre est nommé par la clé de l'objet et porte
l'objet scellé (`crypto.seal`, AES-256-GCM) sous la clé cible, l'AAD liant le chiffré à
SA clé d'objet. Le manifeste inscrit l'archive (son empreinte SHA-256) et, par objet,
sa taille et l'empreinte SHA-256 du clair. L'import vérifie l'archive avant toute
écriture, puis chaque objet déchiffré. Un objet déjà présent dans le stockage cible
avec la même empreinte ET les mêmes en-têtes est sauté : un import interrompu se reprend.

**Les en-têtes** (ACL, `Content-Type`, `Cache-Control`, `Content-Disposition`) : l'import
repose sur chaque objet ceux de l'écriture native, d'après son préfixe
(`media_store.entetes_d_objet`, la source unique) — une image publique (`avatars/`,
`org-logos/`, `images/`) redevient lisible de tous et en cache immuable, un blob durable
(`project-files/`, `transcription-jobs/`) reste privé, sauf un document partagé. Ce que
la règle ne lit pas sur les octets, l'export le relève à la source et le manifeste le
porte, par objet : le type de l'objet (`content_type`) et s'il était public (`public`).
Un préfixe sans règle refuse.

Les archives froides du journal mêlent tous les propriétaires : aucune de leurs clés
n'entre ici.
"""
from __future__ import annotations

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol
from urllib.parse import unquote

from .. import media_store
from ..crypto import seal, unseal

COLONNES_DE_CLES = frozenset({"project_files.s3_key", "transcription_jobs.audio_key",
                              "app_files.s3_key"})
COLONNES_D_URL = frozenset({"users.avatar_url", "orgs.logo_url", "project_files.public_url"})

# Ce qui peut suivre `<base>/` dans une clé : tout sauf ce qui termine une URL dans un
# texte (blanc, guillemet, chevron, parenthèse, crochet, `?`, `#`, antislash).
_CARACTERES_DE_CLE = r"[^\s\"'<>()\[\]?#\\]+"


class ObjetsRefuses(RuntimeError):
    """Les objets du périmètre ne partent pas, ou n'arrivent pas, tels qu'ils sont."""


class Stockage(Protocol):
    def lire(self, cle: str) -> bytes | None: ...
    def ecrire(self, cle: str, donnees: bytes, empreinte: str, entetes: dict) -> None: ...
    def decrire(self, cle: str) -> tuple[int, str] | None: ...
    def entetes(self, cle: str) -> dict: ...


_TOUT_LE_MONDE = "http://acs.amazonaws.com/groups/global/AllUsers"
_ENTETES_RELUS = ("ContentType", "CacheControl", "ContentDisposition")


class StockageS3:
    """Un seau S3 (client boto3 ou compatible). L'empreinte SHA-256 est posée en
    métadonnée à l'écriture : c'est elle qu'on relit, pas l'ETag (un MD5, et pas même
    un MD5 pour un envoi en plusieurs parties)."""

    def __init__(self, client, seau: str):
        self.client, self.seau = client, seau

    def lire(self, cle: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.seau, Key=cle)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def ecrire(self, cle: str, donnees: bytes, empreinte: str, entetes: dict) -> None:
        self.client.put_object(Bucket=self.seau, Key=cle, Body=donnees,
                               Metadata={"sha256": empreinte}, **entetes)

    def entetes(self, cle: str) -> dict:
        """Les en-têtes de l'objet, sous la forme où `ecrire` les pose : `ACL` vaut
        `public-read` si tout le monde peut le lire, et n'y est pas sinon."""
        tete = self.client.head_object(Bucket=self.seau, Key=cle)
        entetes = {k: tete[k] for k in _ENTETES_RELUS if tete.get(k)}
        acl = self.client.get_object_acl(Bucket=self.seau, Key=cle)
        if any(g.get("Grantee", {}).get("URI") == _TOUT_LE_MONDE
               and g.get("Permission") in ("READ", "FULL_CONTROL")
               for g in acl.get("Grants", [])):
            entetes["ACL"] = "public-read"
        return entetes

    def decrire(self, cle: str) -> tuple[int, str] | None:
        try:
            tete = self.client.head_object(Bucket=self.seau, Key=cle)
        except self.client.exceptions.ClientError as e:
            if str(e.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(tete["ContentLength"]), (tete.get("Metadata") or {}).get("sha256", "")


def _empreinte(donnees: bytes) -> str:
    return hashlib.sha256(donnees).hexdigest()


def _empreinte_fichier(chemin: Path) -> str:
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def _aad(cle: str) -> str:
    return f"oto-export-perimetre/objet:{cle}"


def cle_du_chemin(chemin: str) -> str:
    """La clé que désigne le chemin d'une URL de notre stockage public : le chemin
    décodé d'UN niveau, et d'un seul. Les URL stockées encodent la clé d'un niveau —
    constaté sur une vraie copie : la clé `images/<slug>%3A<id>/…` (le sub
    `<slug>:<id>` encodé à l'écriture) est citée par `…/images/<slug>%253A<id>/…`. Pris
    tel quel, le chemin désignait un objet absent ; décodé deux fois, un autre."""
    return unquote(chemin)


def cles_dans(texte: str, base_publique: str) -> set[str]:
    """Les clés des objets dont une URL `<base_publique>/<chemin>` figure dans `texte`,
    chaque chemin ramené à sa clé par `cle_du_chemin`."""
    return {cle_du_chemin(chemin) for chemin in
            re.findall(re.escape(f"{base_publique}/") + f"({_CARACTERES_DE_CLE})", texte)}


def archiver(cles: Iterable[str], source: Stockage, chemin: Path,
             cle_cible: bytes) -> tuple[dict[str, dict], str]:
    """Écrit l'archive des objets `cles`, scellés sous `cle_cible` ; rend, par clé, la
    taille et l'empreinte du clair, son type et s'il est public à la source, et
    l'empreinte de l'archive. Un objet absent de la source refuse, tous nommés, avant
    d'écrire quoi que ce soit."""
    cles = sorted(set(cles))
    _refuser_sans_regle(cles)
    manquantes = [c for c in cles if source.decrire(c) is None]
    if manquantes:
        raise ObjetsRefuses(f"{len(manquantes)} objet(s) du périmètre absent(s) du stockage "
                            f"source : {manquantes[:20]}")
    liste: dict[str, dict] = {}
    provisoire = chemin.with_name(chemin.name + ".partiel")
    with tarfile.open(provisoire, "x", format=tarfile.PAX_FORMAT) as archive:
        for cle in cles:
            donnees = source.lire(cle)
            entetes = source.entetes(cle)
            liste[cle] = {"taille": len(donnees), "sha256": _empreinte(donnees),
                          "content_type": entetes.get("ContentType", ""),
                          "public": entetes.get("ACL") == "public-read"}
            scelle = seal(cle_cible, donnees, _aad(cle))
            membre = tarfile.TarInfo(cle)
            membre.size = len(scelle)
            archive.addfile(membre, io.BytesIO(scelle))
    provisoire.replace(chemin)
    return liste, _empreinte_fichier(chemin)


@dataclass
class Rapport:
    copies: list[str] = field(default_factory=list)
    deja_la: list[str] = field(default_factory=list)
    octets: int = 0


def controler_archive(chemin: Path, empreinte: str) -> None:
    if not chemin.is_file():
        raise ObjetsRefuses(f"archive des objets introuvable : {chemin}")
    if _empreinte_fichier(chemin) != empreinte:
        raise ObjetsRefuses("l'empreinte de l'archive des objets n'est pas celle du "
                            "manifeste : archive tronquée ou modifiée")


def _refuser_sans_regle(cles: Iterable[str]) -> None:
    """Un objet dont le préfixe n'a pas de règle d'écriture (`media_store`) refuse, tous
    nommés : l'import ne saurait ni son ACL ni ses en-têtes."""
    connus = media_store.PREFIXES_PUBLICS | media_store.PREFIXES_DURABLES
    sans_regle = sorted(c for c in cles if c.split("/", 1)[0] not in connus)
    if sans_regle:
        raise ObjetsRefuses(f"{len(sans_regle)} objet(s) sous un préfixe sans règle "
                            f"d'écriture (`media_store`) : {sans_regle[:20]}")


def _controler_regles(liste: dict[str, dict]) -> None:
    """Avant toute écriture : chaque objet a une règle d'écriture (son préfixe), et le
    manifeste porte ce que la règle ne lit pas sur les octets. Un manifeste qui ne les
    relève pas (export antérieur) refuse : on ne devine ni un type, ni une ACL."""
    _refuser_sans_regle(liste)
    muets = sorted(n for n, porte in liste.items()
                   if "content_type" not in porte or "public" not in porte)
    if muets:
        raise ObjetsRefuses(f"le manifeste ne dit ni le type ni l'ACL de {len(muets)} "
                            f"objet(s) (export antérieur à leur relevé) : refaire l'export "
                            f"— {muets[:20]}")


def _entetes_attendus(nom: str, donnees: bytes, porte: dict) -> dict:
    """Les en-têtes de l'écriture native pour l'objet `nom` (`media_store.entetes_d_objet`),
    d'après ce que le manifeste a relevé à la source."""
    try:
        return media_store.entetes_d_objet(nom, donnees, porte["content_type"],
                                           porte["public"])
    except media_store.MediaError as e:
        raise ObjetsRefuses(f"{nom} : {e}") from e


def verser(chemin: Path, liste: dict[str, dict], cible: Stockage, cle: bytes) -> Rapport:
    """Verse chaque objet de l'archive dans `cible` : déchiffré sous `cle` (celle de
    l'instance), vérifié contre le manifeste, écrit avec les en-têtes de l'écriture
    native, puis RELU (octets et en-têtes). Un objet déjà présent avec la même empreinte
    et les mêmes en-têtes est sauté ; présent avec d'autres en-têtes, il est réécrit."""
    rapport = Rapport()
    with tarfile.open(chemin, "r") as archive:
        membres = {m.name: m for m in archive.getmembers()}
        if set(membres) != set(liste):
            raise ObjetsRefuses("l'archive et le manifeste ne listent pas les mêmes objets")
        _controler_regles(liste)
        for nom in sorted(liste):
            attendu = (liste[nom]["taille"], liste[nom]["sha256"])
            try:
                donnees = unseal(cle, archive.extractfile(membres[nom]).read(), _aad(nom))
            except RuntimeError as e:
                raise ObjetsRefuses(f"{nom} : ne se déchiffre pas sous la clé de cette "
                                    "instance") from e
            if (len(donnees), _empreinte(donnees)) != attendu:
                raise ObjetsRefuses(f"{nom} : déchiffré, il n'est pas celui du manifeste")
            entetes = _entetes_attendus(nom, donnees, liste[nom])
            if cible.decrire(nom) == attendu and cible.entetes(nom) == entetes:
                rapport.deja_la.append(nom)
                continue
            cible.ecrire(nom, donnees, attendu[1], entetes)
            if cible.decrire(nom) != attendu or cible.entetes(nom) != entetes:
                raise ObjetsRefuses(f"{nom} : le stockage cible ne rend pas ce qu'on y a écrit")
            rapport.copies.append(nom)
            rapport.octets += attendu[0]
    return rapport
