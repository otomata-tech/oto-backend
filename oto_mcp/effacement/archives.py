"""Les archives FROIDES du journal d'appels, réécrites sans l'org effacée.

Le journal (`tool_calls`) garde trente jours en ligne ; le reste part au froid, un
objet CSV gzip par mois, qui MÊLE tous les propriétaires (`deploy/archive_tool_calls.py`,
registre `journal_archives`). Les arguments d'un appel y portent des données de l'org :
l'effacement ne serait pas complet s'il les y laissait.

Pour chaque mois archivé depuis la création de l'org :
1. relire l'archive en flux et écrire, dans un temporaire, ses lignes SAUF celles de
   l'org (`org_id`) et celles, sans org, des comptes qui partent (`sub`) ;
2. rien à retirer : le mois n'est pas touché ;
3. sinon déposer le temporaire sous une clé NEUVE (`<mois>.effacement-<id>.csv.gz`) —
   une archive n'est JAMAIS écrasée, règle de l'archivage (#1197) —, relire et
   recompter l'objet déposé, repointer `journal_archives` (même transaction, sous
   condition de l'ancienne clé), puis seulement retirer l'ancien objet.

Rejouable : un objet neuf déjà déposé (passage coupé) est relu et adopté s'il porte le
compte attendu, sinon le mois lève sans rien retirer. Le tout tient le verrou
consultatif de l'archivage (`LOCK_KEY`) : les deux ne réécrivent jamais le même mois en
même temps — prod et préprod partagent la base.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import shutil
import tempfile
from typing import Iterator

_log = logging.getLogger("oto_mcp.effacement")

# Le verrou consultatif de `deploy/archive_tool_calls.py` — la MÊME constante.
LOCK_KEY = 0x07C11
_ESPACE_MIN = 2 * 1024**3


class ArchiveNonReecrite(RuntimeError):
    pass


def _lignes(corps) -> Iterator[list[str]]:
    limite = csv.field_size_limit(10_000_000)
    try:
        with gzip.GzipFile(fileobj=corps) as gz:
            yield from csv.reader(io.TextIOWrapper(gz, encoding="utf-8", newline=""))
    finally:
        csv.field_size_limit(limite)


def _compter(s3, seau: str, cle: str) -> int:
    lignes = _lignes(s3.get_object(Bucket=seau, Key=cle)["Body"])
    next(lignes, None)
    return sum(1 for _ in lignes)


def _present(s3, seau: str, cle: str) -> bool:
    try:
        s3.head_object(Bucket=seau, Key=cle)
        return True
    except Exception as e:  # noqa: BLE001 — 404 attendu ; le reste est relevé à l'écriture
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def cle_neuve(cle: str, id_effacement: int) -> str:
    base = cle[:-len(".csv.gz")] if cle.endswith(".csv.gz") else cle
    return f"{base}.effacement-{id_effacement}.csv.gz"


def _filtrer(s3, seau: str, cle: str, org_id: int, partants: set[str],
             chemin: str) -> tuple[int, int]:
    """Écrit dans `chemin` l'archive sans l'org ; rend (gardées, retirées).

    Les lignes GARDÉES sont recopiées OCTET POUR OCTET, jamais re-sérialisées : le CSV
    de PostgreSQL distingue NULL (champ vide) de la chaîne vide (`""`), ce que le
    module `csv` confond — réécrire un enregistrement d'une autre org l'altérerait. Le
    lecteur ne sert qu'à DÉCIDER ; le texte brut de chaque enregistrement (qui peut
    tenir sur plusieurs lignes) est capté au passage."""
    limite = csv.field_size_limit(10_000_000)
    brut: list[str] = []

    def lignes_brutes(texte):
        for ligne in texte:
            brut.append(ligne)
            yield ligne

    try:
        corps = s3.get_object(Bucket=seau, Key=cle)["Body"]
        with gzip.GzipFile(fileobj=corps) as gz, \
                gzip.open(chemin, "wt", encoding="utf-8", newline="") as sortie:
            lecteur = csv.reader(lignes_brutes(io.TextIOWrapper(gz, encoding="utf-8",
                                                                newline="")))
            entete = next(lecteur, None)
            if not entete:
                raise ArchiveNonReecrite(f"archive {cle} vide ou illisible")
            sortie.write("".join(brut))
            brut.clear()
            i_org, i_sub = entete.index("org_id"), entete.index("sub")
            org = str(org_id)
            gardees = retirees = 0
            for rec in lecteur:
                texte = "".join(brut)
                brut.clear()
                if rec[i_org] == org or (rec[i_org] == "" and rec[i_sub] in partants):
                    retirees += 1
                    continue
                sortie.write(texte)
                gardees += 1
    finally:
        csv.field_size_limit(limite)
    return gardees, retirees


def reecrire(conn, s3, seau: str, *, id_effacement: int, org_id: int,
             partants: tuple[str, ...], depuis_mois: str) -> dict:
    """Réécrit chaque mois archivé ≥ `depuis_mois` ('YYYY-MM'). `conn` est une connexion
    AUTOCOMMIT qui tient le verrou de l'archivage le temps de l'appel."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
        raise ArchiveNonReecrite("l'archivage du journal tourne : réécriture remise au "
                                 "passage suivant")
    try:
        mois = conn.execute(
            "SELECT mois, cle, lignes FROM journal_archives WHERE mois >= %s ORDER BY mois",
            (depuis_mois,)).fetchall()
        reecrits, retirees_total = 0, 0
        for m in mois:
            n = _un_mois(conn, s3, seau, m, id_effacement, org_id, set(partants))
            if n:
                reecrits += 1
                retirees_total += n
        return {"mois_lus": len(mois), "mois_reecrits": reecrits, "lignes_retirees": retirees_total}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _un_mois(conn, s3, seau, m: dict, id_effacement: int, org_id: int,
             partants: set[str]) -> int:
    ancienne, neuve = m["cle"], cle_neuve(m["cle"], id_effacement)
    if ancienne == neuve:
        return 0       # déjà réécrit par CET effacement
    libre = shutil.disk_usage(tempfile.gettempdir()).free
    if libre < _ESPACE_MIN:
        raise ArchiveNonReecrite(f"moins de 2 Go libres sur {tempfile.gettempdir()} : "
                                 "réécriture refusée plutôt que remplir le disque")
    fd, chemin = tempfile.mkstemp(prefix=f"effacement-{m['mois']}-", suffix=".csv.gz")
    os.close(fd)
    try:
        gardees, retirees = _filtrer(s3, seau, ancienne, org_id, partants, chemin)
        if retirees == 0:
            return 0
        if gardees + retirees != int(m["lignes"]):
            raise ArchiveNonReecrite(
                f"archive {m['mois']} : {gardees + retirees} lignes relues pour "
                f"{m['lignes']} inscrites — rien n'est réécrit")
        if _present(s3, seau, neuve):
            if _compter(s3, seau, neuve) != gardees:
                raise ArchiveNonReecrite(f"{neuve} existe déjà avec un autre compte : "
                                         "rien n'est réécrit")
        else:
            s3.upload_file(chemin, seau, neuve)  # pas d'ACL : objet privé
            if _compter(s3, seau, neuve) != gardees:
                raise ArchiveNonReecrite(f"{neuve} relu avec un autre compte que {gardees}")
    finally:
        os.unlink(chemin)
    n = conn.execute("UPDATE journal_archives SET cle = %s, lignes = %s "
                     "WHERE mois = %s AND cle = %s",
                     (neuve, gardees, m["mois"], ancienne)).rowcount
    if n != 1:
        raise ArchiveNonReecrite(f"journal_archives {m['mois']} a changé pendant la "
                                 "réécriture : l'ancien objet est gardé")
    s3.delete_object(Bucket=seau, Key=ancienne)
    _log.info("effacement %s : archive %s réécrite (%s ligne(s) retirée(s))",
              id_effacement, m["mois"], retirees)
    return retirees
