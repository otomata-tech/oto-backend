#!/usr/bin/env python3
"""Archive puis purge le journal d'appels au-delà de la fenêtre de rétention.

**Pourquoi ce script existe.** `tool_calls` n'avait AUCUNE rétention : 47 % de la base
le 2026-08-27, et une croissance passée de 9 600 à ~90 000 lignes/jour en deux semaines
(×10, tiré par une campagne de runner). La lecture, elle, a été réparée le même jour par
un index — mais une table qui ne fait que croître finit toujours par coûter ailleurs
(sauvegardes, vacuum, restauration). D'où : on garde 90 jours en ligne, le reste part en
froid sur l'Object Storage avant d'être effacé.

**Pourquoi ce n'est PAS une simple purge de logs.** Cette table est à double emploi :
c'est le journal d'observabilité, ET la source de vérité des exécutions — un run n'est
pas stocké, il est RECONSTRUIT depuis ses faits, et ces faits sont deux lignes d'ici
(`run_start` / `run_finish`, cf. `db/usage.py::_runs_from_journal`). Les effacer
effacerait l'historique des runs, pas seulement du log. Ils sont donc **exemptés** :
ils pèsent ~3 % du volume, les garder indéfiniment est bon marché.
⚠️ Conséquence assumée : un run dont les appels ordinaires ont été archivés garde son
ouverture, sa clôture et son issue, mais son « dernier signe de vie » retombe sur sa date
d'ouverture (`last_seen_at` se dérive du dernier appel rattaché). Sans effet sur un run
clos ; un run resté ouvert et vieux de 90 jours est de toute façon lu comme silencieux.

**Une page de run archivé le DIT** (#665, arbitrage du 23/09/2026, option B). Le run
reste listé avec ses bornes ; son corps, lui, est parti. Pour que sa page ne retombe
pas sur la page vide de #289, chaque mois archivé s'inscrit dans `journal_archives`
(mois, objet S3, lignes relues, date) — APRÈS la relecture qui autorise la
suppression, AVANT la suppression elle-même : la page qui lit ce registre dit alors
« contenu archivé le … » à la place du contenu. ⚠️ Si l'inscription échoue (table
absente : backend pas encore déployé), le script s'arrête AVANT de supprimer — un
corps effacé sans registre, c'est exactement la page vide qu'on ferme.

**Où il tourne, et pourquoi pas dans le backend.** Sur la box, en travail planifié — pas
dans le processus MCP. Celui-ci est mono-boucle : y loger un export de plusieurs
centaines de Mo et une suppression par lots reviendrait à réinstaller la panne que ce
même journal a causée (cf. `docs/event-loop-perf.md`). Un verrou consultatif protège de
toute façon contre deux exécutions simultanées — prod et preprod partagent la base.

**Un passage est reprenable, et ne réécrit jamais une archive** (oto-backend#1197). Le
travail tourne chaque jour ; un jour sans mois éligible ne fait rien. Pour chaque mois
éligible, l'état se LIT avant d'agir :
- ni inscription ni objet : export, relecture, inscription, suppression (le cas nominal) ;
- inscrit, objet présent : c'est la reprise d'une suppression interrompue (délai du
  service, crash). L'objet est relu, recompté contre l'inscription, et CHAQUE ligne encore
  en base doit y figurer (par son `id`) — alors on finit la suppression, sans réexporter ;
- objet présent sans inscription (passage coupé entre dépôt et inscription, ou
  `--export-only`) : adopté seulement s'il porte EXACTEMENT les lignes encore en base
  (même compte, chaque `id` couvert, aucun doublon), puis inscrit ;
- tout le reste lève `ArchiveIncoherente`, qui dit quoi vérifier, et rien n'est supprimé.
⚠️ L'objet S3 n'est JAMAIS écrasé : le versionnage du bucket n'est pas une garantie
(suspendu), et réécrire un mois avec le reste d'une suppression interrompue effaçait
pour de bon les lignes déjà supprimées.

**Ce que coûte la suppression.** Elle avance par PLAGE de `created_at` (index
`idx_tool_calls_created_at`) avec un curseur : chaque lot lit les 20 000 lignes suivantes
du mois, jamais la table ni ce qui est déjà purgé. Le prédicat d'avant
(`to_char(date_trunc(...)) = mois`) ne pouvait servir aucun index : chaque lot relisait
toute la table. La pause entre deux lots (`--pause`) laisse au WAL et à l'autovacuum le
temps de suivre (`docs/monitoring.md` §Rétention, mesures).

Usage :
    archive_tool_calls.py [--dry-run] [--export-only] [--retention-days N] [--pause S]

L'environnement est celui du backend (`/opt/oto-mcp/.env`) : `DATABASE_URL` et
`OTO_MCP_S3_{ENDPOINT,REGION,BUCKET,ACCESS_KEY,SECRET_KEY}`. Dès que la box est passée au
lanceur générique (#967 lot 5), les secrets n'y sont plus : le script se lance par
`deploy/lanceur_secrets.py --script deploy/archive_tool_calls.py [options]`
(`deploy/oto-journal-archive.service`).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import os
import shutil
import sys
import tempfile
import re
import time
from datetime import datetime, timezone

import boto3
import psycopg
from botocore.exceptions import ClientError
from psycopg.rows import dict_row

# Les faits qui RECONSTRUISENT un run : jamais archivés, jamais supprimés. Toucher à
# cette liste, c'est décider d'oublier des exécutions — pas d'alléger un log.
RUN_FACTS = ("run_start", "run_finish")

# Préfixe S3. Déposé SANS ACL publique (le bucket porte les deux régimes : `upload_image`
# pose `public-read`, les blobs durables non — cf. `media_store.py`). Un journal porte des
# identifiants de compte et des arguments d'appel : il suit le régime privé.
S3_PREFIX = "journal/tool_calls"

# Suppression par lots : un DELETE de plusieurs millions de lignes tiendrait un lock long
# et gonflerait la table de tuples morts. La pause (réglable : `--pause`) laisse respirer
# l'autovacuum et étale le WAL : la base de prod checkpointe toutes les 30 s, et chaque
# page de tas touchée pour la première fois après un checkpoint s'écrit ENTIÈRE au WAL.
DELETE_BATCH = 20_000
DELETE_PAUSE_S = 1.0

# Verrou consultatif : deux exécutions concurrentes exporteraient le même mois deux fois,
# et la seconde supprimerait ce que la première est en train de lire.
LOCK_KEY = 0x07C11  # constante arbitraire, stable — ne la change pas sans raison

log = logging.getLogger("archive_tool_calls")


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if not val:
        raise SystemExit(f"variable d'environnement requise absente : {name}")
    return val


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_env("OTO_MCP_S3_ENDPOINT"),
        region_name=os.environ.get("OTO_MCP_S3_REGION", "fr-par"),
        aws_access_key_id=_env("OTO_MCP_S3_ACCESS_KEY"),
        aws_secret_access_key=_env("OTO_MCP_S3_SECRET_KEY"),
    )


class ArchiveIncoherente(RuntimeError):
    """L'état d'un mois ne permet ni d'exporter ni de reprendre sans risquer de perdre des
    lignes. Le message dit quoi vérifier ; RIEN n'a été supprimé pour ce mois."""


def _cle(mois: str) -> str:
    return f"{S3_PREFIX}/{mois}.csv.gz"


def _bornes(conn: psycopg.Connection, mois: str) -> tuple[datetime, datetime]:
    """[début, fin) du mois calendaire, calculés PAR LA BASE dans le fuseau de la session :
    le même que celui de `date_trunc('month', created_at)` dans `_months_to_archive`. Le
    mois compté là et la plage purgée ici sont donc le même ensemble de lignes.

    ⚠️ C'est ce qui rend la suppression indexable : une PLAGE sur `created_at` se sert de
    `idx_tool_calls_created_at`, un prédicat sur `to_char(date_trunc(...))` d'aucun index."""
    if not re.fullmatch(r"\d{4}-\d{2}", mois):
        raise ValueError(f"mois invalide : {mois!r} (attendu AAAA-MM)")
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT (%(m)s || '-01')::timestamptz AS debut, "
        "       (%(m)s || '-01')::timestamptz + interval '1 month' AS fin",
        {"m": mois},
    ).fetchone()
    return row["debut"], row["fin"]


# Les lignes archivables d'un mois : la plage, moins les faits de run. Une seule écriture,
# partagée par l'export, la vérification de reprise et la suppression.
_DU_MOIS = ("created_at >= %(debut)s AND created_at < %(fin)s "
            "AND coalesce(tool, '') <> ALL(%(facts)s)")

# Un lot de suppression : les `lot` lignes suivantes du mois à partir du curseur, dans
# l'ordre de l'index `idx_tool_calls_created_at`. Rend le nombre supprimé et la date de
# la dernière, qui devient le curseur suivant : un lot ne relit jamais ce qu'un lot
# précédent a purgé. `>=` (et non `>`) : deux appels peuvent partager un instant.
_SQL_LOT = f"""
WITH lot AS (
    SELECT id FROM tool_calls
     WHERE {_DU_MOIS.replace('%(debut)s', '%(curseur)s')}
     ORDER BY created_at
     LIMIT %(lot)s),
supprimees AS (
    DELETE FROM tool_calls WHERE id IN (SELECT id FROM lot) RETURNING created_at)
SELECT count(*) AS n, max(created_at) AS dernier FROM supprimees
"""


def _months_to_archive(conn: psycopg.Connection, retention_days: int) -> list[tuple[str, int]]:
    """Les mois CALENDAIRES entièrement sortis de la fenêtre, qui portent encore des
    lignes archivables.

    Le mois entier doit être derrière la borne : archiver un mois à cheval déposerait un
    fichier incomplet, que la prochaine passe ne saurait pas compléter (l'objet existe
    déjà) — on aurait effacé des lignes qui ne sont dans aucune archive.

    « Le mois finit avant `now() - rétention` » s'écrit `created_at < date_trunc('month',
    now() - rétention)` : une fin de mois est un début de mois, et le plus grand début de
    mois inférieur ou égal à la borne est sa troncature. Écrit ainsi, le prédicat se sert
    de l'index sur `created_at` — un passage quotidien sans mois éligible ne lit que les
    faits de run des mois déjà purgés."""
    rows = conn.cursor(row_factory=dict_row).execute(
        """
        SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS mois,
               count(*) AS lignes
          FROM tool_calls
         WHERE created_at < date_trunc('month', now() - %(retention)s * interval '1 day')
           AND coalesce(tool, '') <> ALL(%(facts)s)
         GROUP BY 1 ORDER BY 1
        """,
        {"facts": list(RUN_FACTS), "retention": retention_days},
    ).fetchall()
    for r in rows:
        log.info("mois archivable : %s (%s lignes)", r["mois"],
                 f"{r['lignes']:_}".replace("_", " "))
    return [(r["mois"], r["lignes"]) for r in rows]


def _inscription(conn: psycopg.Connection, mois: str) -> dict | None:
    """L'inscription du mois au registre (`cle`, `lignes`), ou None."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT cle, lignes FROM journal_archives WHERE mois = %s", (mois,)).fetchone()


def _objet_present(s3, bucket: str, key: str) -> int | None:
    """Taille de l'objet s'il existe, None s'il n'existe pas.

    ⚠️ Seul un « introuvable » vaut absence. Un refus d'accès ou une panne réseau n'est
    PAS une absence : le prendre pour tel ferait réexporter, donc écraser."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _export_month(conn: psycopg.Connection, s3, bucket: str, mois: str) -> str:
    """Exporte un mois en CSV gzip et le dépose sur l'Object Storage.

    Passe par un fichier temporaire plutôt qu'un flux en mémoire : un mois pèse ~1 Go
    brut. ⚠️ La box a un `/` étroit (incident disque du 2026-07-24) — l'espace est
    vérifié avant, et le temporaire est retiré quoi qu'il arrive.

    ⚠️ N'ÉCRASE JAMAIS un objet existant (#1197) : c'est ici, au point d'écriture, que la
    garde est posée, quel que soit le chemin qui y mène. Les lignes sortent dans l'ordre
    où la base les lit (un seul parcours, sans tri) : chacune porte son `id` et sa date."""
    key = _cle(mois)
    if _objet_present(s3, bucket, key) is not None:
        raise ArchiveIncoherente(
            f"s3://{bucket}/{key} existe déjà : export refusé, une archive n'est jamais "
            "écrasée. Lire l'état du mois (registre `journal_archives`, objet) avant tout.")
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < 2 * 1024**3:
        raise SystemExit(
            f"moins de 2 Go libres sur {tempfile.gettempdir()} ({free // 1024**2} Mo) — "
            "export refusé plutôt que remplir le disque de la box")
    debut, fin = _bornes(conn, mois)

    fd, path = tempfile.mkstemp(prefix=f"tool_calls-{mois}-", suffix=".csv.gz")
    os.close(fd)
    try:
        # ⚠️ Ne PAS compter les enregistrements en comptant les sauts de ligne du flux :
        # `args` et `error` en contiennent, et le CSV les porte entre guillemets. Mesuré
        # le 2026-08-27 : 12 830 « lignes » annoncées pour 12 459 enregistrements réels.
        # Le seul compte juste est celui de la base — un compteur qui ment dans un log
        # d'archivage est pire qu'absent, c'est lui qu'on interrogera pour vérifier.
        with gzip.open(path, "wb") as gz:
            with conn.cursor().copy(
                f"COPY (SELECT * FROM tool_calls WHERE {_DU_MOIS}) "
                "TO STDOUT WITH (FORMAT csv, HEADER true)",
                {"facts": list(RUN_FACTS), "debut": debut, "fin": fin},
            ) as copy:
                for chunk in copy:
                    gz.write(chunk)
        taille = os.path.getsize(path)
        log.info("export %s : %s Mo compressés", mois, round(taille / 1024**2, 1))
        s3.upload_file(path, bucket, key)  # pas d'ACL : objet privé
    finally:
        os.unlink(path)

    meta = s3.head_object(Bucket=bucket, Key=key)
    if meta["ContentLength"] != taille:
        raise SystemExit(
            f"archive {key} déposée incomplète ({meta['ContentLength']} != {taille} "
            "octets) — AUCUNE suppression effectuée")
    log.info("archive déposée : s3://%s/%s (%s octets)", bucket, key, meta["ContentLength"])
    return key


class _Ids:
    """Ensemble d'`id` en bitmap par blocs de 65 536 (8 Ko le bloc) : un mois de 9 M
    lignes aux `id` contigus tient en ~1 Mo, là où un `set` Python en prendrait 500."""

    def __init__(self) -> None:
        self._blocs: dict[int, bytearray] = {}
        self.distincts = 0

    def ajouter(self, n: int) -> None:
        bloc = self._blocs.setdefault(n >> 16, bytearray(8192))
        octet, bit = (n & 0xFFFF) >> 3, 1 << (n & 7)
        if not bloc[octet] & bit:
            bloc[octet] |= bit
            self.distincts += 1

    def __contains__(self, n: int) -> bool:
        bloc = self._blocs.get(n >> 16)
        return bloc is not None and bool(bloc[(n & 0xFFFF) >> 3] & (1 << (n & 7)))


def _relire_archive(s3, bucket: str, key: str, *, avec_ids: bool = False):
    """Relit l'archive DEPUIS l'Object Storage, en flux : (enregistrements, colonnes, ids).

    Lu en flux : une archive pèse plusieurs centaines de Mo, la charger entière en
    mémoire sur la box la mettrait en difficulté."""
    # Un `args` peut être gros ; le défaut de `csv` coupe à 128 ko. ⚠️ La limite est
    # GLOBALE au processus : relevée sans être rendue, elle désarmait toute autre lecture
    # CSV du même processus — vécu le 09/10/2026 (#1111) : relu par un banc, ce script
    # laissait la limite à 10 Mo et la garde « cellule trop grosse » de
    # `oto_mcp/csv_tolerant.py` ne se déclenchait plus. Relevée le temps de la relecture,
    # rendue à la sortie, quoi qu'il arrive.
    limite_avant = csv.field_size_limit(10_000_000)
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        ids = _Ids() if avec_ids else None
        with gzip.GzipFile(fileobj=body) as gz:
            lecteur = csv.reader(io.TextIOWrapper(gz, encoding="utf-8"))
            entete = next(lecteur, None)
            if not entete:
                raise SystemExit(f"archive {key} vide ou illisible — AUCUNE suppression effectuée")
            if ids is None:
                relus = sum(1 for _ in lecteur)
            else:
                col, relus = entete.index("id"), 0
                for rec in lecteur:
                    ids.ajouter(int(rec[col]))
                    relus += 1
    finally:
        csv.field_size_limit(limite_avant)
    return relus, len(entete), ids


def _verify_archive(s3, bucket: str, key: str, attendu: int) -> None:
    """Relit l'archive DEPUIS l'Object Storage et recompte ses enregistrements.

    ⚠️ C'est la garantie qui autorise la suppression, et elle ne se déduit d'aucune autre.
    Comparer la taille du dépôt à celle du fichier local prouve que l'upload n'a rien
    perdu — pas que l'export contenait tout, ni qu'il se relit. La seule preuve est de
    refaire le chemin du jour où on en aura besoin : télécharger, décompresser, parser,
    compter. Le surcoût d'une relecture par mois archivé est sans commune mesure avec une
    suppression définitive fondée sur une supposition."""
    relus, colonnes, _ = _relire_archive(s3, bucket, key)
    if relus != attendu:
        raise SystemExit(
            f"archive {key} : {relus} enregistrements relus pour {attendu} attendus — "
            "AUCUNE suppression effectuée")
    log.info("archive vérifiée à la relecture : %s enregistrements, %s colonnes",
             relus, colonnes)


def _hors_archive(conn: psycopg.Connection, mois: str, ids: _Ids) -> tuple[int, int, list[int]]:
    """Lignes archivables du mois encore en base : (combien, combien absentes de
    l'archive, quelques `id` absents). Lu en flux, par la plage indexée."""
    debut, fin = _bornes(conn, mois)
    restantes, absentes, exemples = 0, 0, []
    with conn.cursor().copy(
        f"COPY (SELECT id FROM tool_calls WHERE {_DU_MOIS}) TO STDOUT",
        {"facts": list(RUN_FACTS), "debut": debut, "fin": fin},
    ) as copy:
        copy.set_types(["int8"])
        for (id_,) in copy.rows():
            restantes += 1
            if id_ not in ids:
                absentes += 1
                if len(exemples) < 5:
                    exemples.append(id_)
    return restantes, absentes, exemples


def _reprendre(conn: psycopg.Connection, s3, bucket: str, mois: str,
               inscription: dict | None, taille: int | None) -> int:
    """Un mois qui a déjà un objet ou une inscription : prouve que la suppression peut
    (re)partir SANS réexporter, et rend le compte de l'archive. Lève sinon.

    La preuve : l'objet se relit ; son compte est celui de l'inscription ; chaque ligne
    du mois encore en base y figure par son `id`. Sans inscription (passage coupé entre
    dépôt et inscription, ou `--export-only`), l'objet n'est adopté que s'il porte
    EXACTEMENT les lignes en base — même compte, aucun doublon, chaque `id` couvert."""
    key = _cle(mois)
    if inscription is not None and inscription["cle"] != key:
        raise ArchiveIncoherente(
            f"{mois} : le registre désigne {inscription['cle']}, ce script écrit {key}. "
            "Vérifier lequel porte le mois avant toute suppression.")
    if taille is None:
        raise ArchiveIncoherente(
            f"{mois} : inscrit au registre ({inscription['lignes']} lignes) mais "
            f"s3://{bucket}/{key} est ABSENT. Les lignes déjà supprimées ne sont peut-être "
            "plus nulle part : retrouver l'objet (versions, autre bucket) avant tout ; "
            "ne PAS retirer l'inscription pour réexporter.")
    relus, _, ids = _relire_archive(s3, bucket, key, avec_ids=True)
    if ids.distincts != relus:
        raise ArchiveIncoherente(
            f"{mois} : s3://{bucket}/{key} porte {relus} enregistrements pour "
            f"{ids.distincts} id distincts — archive à examiner, rien supprimé.")
    if inscription is not None and relus != inscription["lignes"]:
        raise ArchiveIncoherente(
            f"{mois} : s3://{bucket}/{key} relu à {relus} enregistrements, le registre en "
            f"inscrit {inscription['lignes']}. L'objet a été remplacé ou tronqué : comparer "
            "aux versions de l'objet avant toute suppression.")
    restantes, absentes, exemples = _hors_archive(conn, mois, ids)
    if inscription is None and (absentes or relus != restantes):
        raise ArchiveIncoherente(
            f"{mois} : s3://{bucket}/{key} existe sans inscription et ne porte pas exactement "
            f"les {restantes} lignes en base ({relus} enregistrements, {absentes} lignes en "
            "base absentes de l'objet). Ce n'est pas l'export de ce qui reste : l'examiner "
            "(dépôt d'un autre passage, autre base ?) ; il ne sera ni écrasé ni adopté, "
            "rien supprimé.")
    if absentes:
        raise ArchiveIncoherente(
            f"{mois} : {absentes} des {restantes} lignes encore en base ne sont PAS dans "
            f"s3://{bucket}/{key} (ex. id {exemples}). Des lignes sont entrées dans ce mois "
            "après l'export : les examiner ; rien supprimé.")
    log.info("%s : s3://%s/%s relu, %s enregistrements, couvre les %s lignes encore en base "
             "— %s, sans réexport", mois, bucket, key, relus, restantes,
             "reprise de la suppression" if inscription else "objet adopté")
    return relus


def _record_archive(conn: psycopg.Connection, mois: str, key: str, lignes: int) -> None:
    """Inscrit le mois au registre des archives (#665) — la source de « contenu
    archivé le … » sur la page d'un run.

    Une inscription ne se réécrit JAMAIS (#1197) : la réécrire avec le compte d'une
    reprise effaçait la trace des lignes déjà supprimées. Un mois déjà inscrit ici est
    une incohérence (le passage l'a lu non inscrit). Aucune exception n'est rattrapée :
    sans inscription, pas de suppression."""
    inseres = conn.execute(
        """
        INSERT INTO journal_archives (mois, cle, lignes) VALUES (%(mois)s, %(cle)s, %(lignes)s)
        ON CONFLICT (mois) DO NOTHING
        """,
        {"mois": mois, "cle": key, "lignes": lignes},
    ).rowcount
    if inseres != 1:
        raise ArchiveIncoherente(
            f"{mois} : déjà inscrit au registre alors que ce passage l'a lu non inscrit "
            "(autre exécution hors verrou ?). L'inscription existante est gardée ; rien "
            "supprimé.")
    log.info("registre : %s inscrit comme archivé (%s)", mois, key)


def _delete_month(conn: psycopg.Connection, mois: str, pause_s: float = DELETE_PAUSE_S) -> int:
    """Supprime les lignes archivées, par lots bornés qui avancent sur l'index de
    `created_at` (#1197). Chaque lot est sa propre transaction : interrompu, le passage
    suivant reprend là où la base en est."""
    debut, fin = _bornes(conn, mois)
    curseur, total = debut, 0
    while True:
        lot = conn.cursor(row_factory=dict_row).execute(
            _SQL_LOT,
            {"facts": list(RUN_FACTS), "curseur": curseur, "fin": fin, "lot": DELETE_BATCH},
        ).fetchone()
        if lot["n"] == 0:
            break
        total += lot["n"]
        curseur = lot["dernier"]
        log.info("  supprimé %s lignes (cumul %s)", lot["n"], total)
        time.sleep(pause_s)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="n'exporte rien et ne supprime rien : dit seulement ce qui partirait")
    parser.add_argument("--export-only", action="store_true",
                        help="dépose et vérifie l'archive, mais NE SUPPRIME RIEN. C'est ce qui "
                             "permet d'exercer le chemin réel — export, upload, vérification — "
                             "sans engager la moitié irréversible ; à blanc on ne prouve rien.")
    parser.add_argument("--retention-days", type=int,
                        default=int(os.environ.get("OTO_JOURNAL_RETENTION_DAYS", "90")),
                        help="fenêtre gardée en ligne (défaut 90, ou OTO_JOURNAL_RETENTION_DAYS)")
    parser.add_argument("--pause", type=float, default=DELETE_PAUSE_S,
                        help=f"secondes entre deux lots de suppression (défaut {DELETE_PAUSE_S}) : "
                             "étale le WAL et laisse suivre l'autovacuum")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("=== archivage du journal d'appels — rétention %s j%s ===",
             args.retention_days, " (À BLANC)" if args.dry_run else "")

    bucket = _env("OTO_MCP_S3_BUCKET")
    with psycopg.connect(_env("DATABASE_URL"), autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
            log.warning("une autre exécution tient le verrou — abandon (rien n'a été touché)")
            return 0
        try:
            mois_list = _months_to_archive(conn, args.retention_days)
            if not mois_list:
                log.info("rien à archiver : aucun mois entièrement sorti de la fenêtre.")
                return 0
            if args.dry_run:
                log.info("à blanc : %s mois partiraient (%s)", len(mois_list),
                         ", ".join(m for m, _ in mois_list))
                return 0

            s3 = _s3_client()
            for mois, restantes in mois_list:
                key = _cle(mois)
                inscription = _inscription(conn, mois)
                taille = _objet_present(s3, bucket, key)
                if inscription is None and taille is None:
                    _export_month(conn, s3, bucket, mois)
                    _verify_archive(s3, bucket, key, restantes)
                    lignes = restantes
                else:
                    lignes = _reprendre(conn, s3, bucket, mois, inscription, taille)
                if args.export_only:
                    log.info("%s : %s lignes archivées dans %s — suppression NON demandée",
                             mois, lignes, key)
                    continue
                if inscription is None:
                    _record_archive(conn, mois, key, lignes)
                supprimees = _delete_month(conn, mois, args.pause)
                log.info("%s : %s lignes archivées dans %s, %s supprimées de la base",
                         mois, lignes, key, supprimees)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))

    log.info("=== terminé à %s ===", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
