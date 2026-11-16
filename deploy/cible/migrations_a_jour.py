#!/usr/bin/env python3
"""La base d'un rôle est-elle à la tête des migrations du code qu'on démarre ?
(oto-backend#1163, #1195)

Le défaut qu'il ferme : une instance cible servait le code d'un tag dont la tête Alembic
était 0037, sur une base restée en 0031 — la chaîne de déploiement montait le code sans
jouer les migrations, et rien ne le disait.

Exécuté par `deploy/cible/deployer.sh`, sous le lanceur (qui tire `DATABASE_URL`), dans
l'arbre de la couleur INACTIVE, AVANT son démarrage :

    <arbre>/.venv/bin/python <arbre>/deploy/lanceur_secrets.py --script deploy/cible/migrations_a_jour.py [--migrer|--retour]

- la tête ATTENDUE est celle du registre de CET arbre — le tag qu'on monte, ou la version
  précédente qu'un retour arrière redémarre ; jamais le code qui sert ;
- la révision COURANTE est lue dans la base du rôle, en lecture seule, avec le même
  constat que le démarrage (`db._version_alembic.constater`).

Trois modes :

- (aucun) — CONSTAT : ne touche à rien. Sortie 0 si la base est à la tête du tag, ou NEUVE
  (aucune table : le démarrage crée le schéma et y pose la tête) ; 3 si elle est EN RETARD
  sur une chaîne linéaire du registre du tag (`--migrer` la rattrape) ; 1 sinon.
- `--migrer` — MONTÉE (#1195) : une cible suit le tronc sans geste humain, donc une base EN
  RETARD sur une chaîne LINÉAIRE est migrée ici (`oto-mcp maintenance copies-du-coffre`,
  puis `oto-mcp migrer upgrade head`, depuis cet arbre), puis relue : elle doit être à la
  tête, sinon refus. Le journal dit « de → vers ». Sortie 0 (à jour, neuve, ou migrée) ou 1.
- `--retour` — RETOUR ARRIÈRE (#1195) : l'arbre est celui de la couleur précédente. Sa base
  doit être EXACTEMENT à la tête de son registre ; une révision qu'il ne connaît pas (une
  montée l'a migrée depuis) refuse — on corrige vers l'avant, par un nouveau tag. Sortie
  0 ou 1.

Refus communs, qu'aucun mode ne contourne : registre à plusieurs têtes ou vide, base
injoignable, base sans version, plusieurs révisions en base, révision inconnue du registre,
chaîne non linéaire entre la base et la tête (une fusion de files s'y glisse), révision
retirée par un squash (antérieure à la référence du registre : le refus nomme le tag
d'avant le squash qui la monte d'abord, docs/migrations-versionnees.md §5.4). Aucun cas ne
passe par défaut.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

ARBRE = Path(__file__).resolve().parents[2]
if str(ARBRE) not in sys.path:
    sys.path.insert(0, str(ARBRE))

import psycopg  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.db import index_concurrent  # noqa: E402
from oto_mcp.db._version_alembic import (_REGISTRE, BaseAnterieureALaReference,  # noqa: E402
                                         EtatRegistre, constater, versions_de)

A_JOUR = 0
ILLISIBLE = 1
EN_RETARD = 3
DELAI_S = 15
# Une migration peut réécrire une grosse table : large, mais borné — une montée suspendue
# tient le verrou de la porte et toutes les montées suivantes derrière elle.
DELAI_MIGRATION_S = 1200
# Jusqu'où une révision construit elle-même un index CONCURRENTLY pendant CETTE migration
# (`index_concurrent.PLAFOND_ENV`, lu par le seul chemin révision). Le plafond commun
# (100 000 lignes) protège la fenêtre de démarrage, où la construction bloque les écritures ;
# ici elle est concurrente (rien de bloqué) et le seul risque est la durée. Calibré sur la
# mesure, pas deviné : 172 s par index pour ~12 M lignes en production (04/10/2026) ; la
# révision 0049 en pose trois, soit ~9 min pour 12 M lignes, sous `DELAI_MIGRATION_S`
# (1 200 s). 10 M lignes gardent la marge. Au-delà : `ConstructionManuelleRequise`, et le
# geste est `oto-mcp maintenance index-concurrents <révision>` sur le rôle. Un index
# INVALIDE reste un geste manuel, ici comme partout (décision du 09/10/2026).
PLAFOND_INDEX_LIGNES = 10_000_000


class Refus(Exception):
    """Rien ne se conclut : le message nomme pourquoi."""


def _registre(registre: Path) -> tuple[ScriptDirectory, str, set[str]]:
    script = ScriptDirectory(str(registre))
    tetes = sorted(script.get_heads())
    if not tetes:
        raise Refus(f"registre de migrations du tag vide ({registre})")
    if len(tetes) > 1:
        raise Refus(f"le registre du tag a {len(tetes)} têtes ({', '.join(tetes)}) : "
                    "aucune n'est « la » tête — fusionner les files (alembic merge) dans "
                    "le tronc, puis monter un tag qui n'en a qu'une")
    return script, tetes[0], {r.revision for r in script.walk_revisions()}


def tete_du_tag(registre: Path) -> tuple[str, set[str]]:
    """La tête UNIQUE du registre de l'arbre, et toutes ses révisions."""
    _, tete, connues = _registre(registre)
    return tete, connues


def chemin_lineaire(script: ScriptDirectory, courante: str, tete: str) -> list[str]:
    """Les révisions que `upgrade head` jouerait de `courante` (exclue) à `tete`, dans
    l'ordre — à condition que ce soit une simple chaîne. Une révision de fusion sur le
    chemin refuse : migrer en aveugle à travers deux files mêlées n'est pas un geste
    qu'une montée automatique prend seule."""
    chemin: list[str] = []
    rev = script.get_revision(tete)
    while rev.revision != courante:
        parents = rev.down_revision
        if isinstance(parents, (tuple, list)):
            raise Refus(f"chaîne non linéaire entre {courante} et {tete} : {rev.revision} "
                        f"fusionne {', '.join(parents)} — migrer à la main, en root, "
                        "après lecture de `migrer history`")
        if parents is None:
            raise Refus(f"{courante} n'est pas sur la chaîne qui mène à {tete}")
        chemin.append(rev.revision)
        rev = script.get_revision(parents)
    return list(reversed(chemin))


def lire_base(dsn: str) -> tuple[EtatRegistre, list[str]]:
    """L'état du registre de la base, et ses révisions — en lecture seule.

    Une erreur de connexion ou de lecture refuse en nommant sa classe seulement : son
    message peut porter l'hôte ou l'utilisateur de la base, et ce journal remonte jusqu'au
    run du workflow. Une base antérieure à la référence refuse avec le message du constat
    (la révision, la référence, le tag d'avant le squash) — rien de la connexion."""
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=DELAI_S) as conn:
            conn.read_only = True
            conn.execute(f"SET statement_timeout = '{DELAI_S}s'")
            etat = constater(conn)
            versions = versions_de(conn) if etat is EtatRegistre.VERSIONNEE else []
    except BaseAnterieureALaReference as refus:
        raise Refus(str(refus)) from refus
    except psycopg.Error as erreur:
        etat_sql = f", SQLSTATE {erreur.sqlstate}" if erreur.sqlstate else ""
        raise Refus(f"base du rôle illisible ({type(erreur).__name__}{etat_sql})") from erreur
    return etat, versions


#: Ce que la montée joue depuis cet arbre, dans l'ordre, sur une base EN RETARD.
#: D'abord les copies du coffre des connecteurs renommés (`oto_mcp.maintenance.
#: copies_declarees`) : notre instance les fait au démarrage, qui précède sa migration ;
#: ici la migration précède le démarrage, et une révision de renommage repointe vers ces
#: copies — sans elles, elle lève (`20261112_1200_connecteur_api`). Additives et
#: idempotentes : l'ancien code, qui sert encore, n'en voit rien.
COPIES_DU_COFFRE = ("maintenance", "copies-du-coffre")
MIGRATION = ("migrer", "upgrade", "head")


def _jouer(arguments: tuple[str, ...], environnement: dict, apres_echec: str) -> None:
    """`oto-mcp <arguments>` DEPUIS CET ARBRE, borné à `DELAI_MIGRATION_S` ; sa sortie va
    au journal du run. Un échec ou un dépassement refuse, en disant quoi lire."""
    nom = " ".join(arguments)
    commande = [str(ARBRE / ".venv" / "bin" / "oto-mcp"), *arguments]
    try:
        fini = subprocess.run(commande, timeout=DELAI_MIGRATION_S, env=environnement)
    except subprocess.TimeoutExpired as depasse:
        raise Refus(f"`{nom}` n'a pas fini en {DELAI_MIGRATION_S} s") from depasse
    if fini.returncode != 0:
        raise Refus(f"`{nom}` a échoué (code {fini.returncode}, sortie ci-dessus) — "
                    f"{apres_echec}")


def migrer_la_base() -> None:
    """Les copies du coffre, puis `oto-mcp migrer upgrade head`, DEPUIS CET ARBRE — le
    registre du tag qu'on monte.

    Elles héritent de l'environnement du lanceur (`DATABASE_URL` et la clé maîtresse
    comprises) ; la migration reçoit en plus le plafond de construction des index
    (`PLAFOND_INDEX_LIGNES`) — posé sur cette commande seulement, jamais dans
    l'environnement de la porte. Un échec refuse : la couleur ne démarre pas."""
    _jouer(COPIES_DU_COFFRE, dict(os.environ),
           "rien n'est migré ; les copies déjà faites restent (additives, idempotentes)")
    _jouer(MIGRATION, {**os.environ, index_concurrent.PLAFOND_ENV: str(PLAFOND_INDEX_LIGNES)},
           "la base peut s'être arrêtée entre deux révisions : lire `migrer current` "
           "avant toute relance")


def _une_revision(etat: EtatRegistre, versions: list[str]) -> str:
    """La révision unique d'une base versionnée — sinon refus, nommé."""
    if etat is EtatRegistre.SANS_VERSION:
        raise Refus("la base a des tables mais pas d'`alembic_version` — on ignore quelles "
                    "révisions elle a reçues. Établir la dernière présente dans son schéma, "
                    "puis `migrer stamp <révision>` (docs/migrations-versionnees.md §5.2)")
    if len(versions) != 1:
        raise Refus(f"`alembic_version` porte {len(versions)} révision(s) "
                    f"({', '.join(versions) or 'aucune'}) — une seule est attendue")
    return versions[0]


def verdict(tete: str, connues: set[str], etat: EtatRegistre,
            versions: list[str]) -> tuple[int, str]:
    """Le code de sortie du CONSTAT et ce qu'on en dit (sans le contrôle de linéarité,
    qui lit le registre : `main` l'ajoute)."""
    if etat is EtatRegistre.NEUVE:
        return A_JOUR, (f"migrations : base neuve — le démarrage y créera le schéma et "
                        f"posera la tête du tag ({tete})")
    try:
        courante = _une_revision(etat, versions)
    except Refus as refus:
        return ILLISIBLE, f"migrations : {refus}"
    if courante == tete:
        return A_JOUR, f"migrations : base à la tête du tag ({tete})"
    if courante not in connues:
        return ILLISIBLE, (f"migrations : la base est en {courante}, révision inconnue du "
                           f"registre du tag (tête {tete}) — base plus récente que le tag, ou "
                           "issue d'une autre file. `upgrade head` n'y changerait rien")
    return EN_RETARD, (f"migrations : la base est en {courante}, le tag attend {tete} — "
                       "base EN RETARD sur le code qu'on monte")


def verdict_retour(tete: str, connues: set[str], etat: EtatRegistre,
                   versions: list[str]) -> tuple[int, str]:
    """Le retour arrière redémarre le code de la couleur précédente : sa base doit être à
    la tête de SON registre, ni plus récente, ni en retard."""
    if etat is EtatRegistre.NEUVE:
        return ILLISIBLE, ("retour : la base est neuve (aucune table) — rien n'y a jamais "
                           "servi, on ne rebascule pas dessus")
    try:
        courante = _une_revision(etat, versions)
    except Refus as refus:
        return ILLISIBLE, f"retour : {refus}"
    if courante == tete:
        return A_JOUR, (f"retour : base en {courante}, la tête que connaît le code de la "
                        "couleur précédente")
    if courante not in connues:
        return ILLISIBLE, (f"retour : la base est en {courante}, révision INCONNUE du code "
                           f"de la couleur précédente (sa tête : {tete}) — une montée l'a "
                           "migrée depuis, ce code ne connaît pas ce schéma. On ne rebascule "
                           "pas : corriger vers l'avant, par un nouveau tag")
    return ILLISIBLE, (f"retour : la base est en {courante}, EN RETARD sur le code de la "
                       f"couleur précédente (sa tête : {tete}) — ce code n'a pas pu y servir "
                       "après sa garde de montée ; lire `migrer current` avant tout geste")


def _constat(tete, connues, script, etat, versions) -> tuple[int, str]:
    code, message = verdict(tete, connues, etat, versions)
    if code == EN_RETARD:
        chemin = chemin_lineaire(script, versions[0], tete)
        message += f" (chaîne linéaire : {' → '.join(chemin)})"
    return code, message


def main(argv: Sequence[str] = (),
         lire: Callable[[str], tuple[EtatRegistre, list[str]]] = lire_base,
         registre: Path = _REGISTRE,
         migrer: Callable[[], None] = migrer_la_base) -> int:
    argv = list(argv)
    if argv not in ([], ["--migrer"], ["--retour"]):
        print(f"migrations : usage — [--migrer | --retour], reçu {argv}", file=sys.stderr)
        return ILLISIBLE
    mode = argv[0] if argv else ""
    try:
        script, tete, connues = _registre(registre)
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise Refus("DATABASE_URL absente de l'environnement (le lanceur la tire)")
        etat, versions = lire(dsn)
        if mode == "--retour":
            code, message = verdict_retour(tete, connues, etat, versions)
        else:
            code, message = _constat(tete, connues, script, etat, versions)
        if mode == "--migrer" and code == EN_RETARD:
            depuis = versions[0]
            print(message, file=sys.stderr)
            print(f"migrations : on joue `migrer upgrade head` depuis l'arbre du tag "
                  f"({depuis} → {tete}), après `maintenance copies-du-coffre` (le coffre "
                  "des connecteurs renommés, que le démarrage copierait trop tard)")
            print(f"migrations : un index CONCURRENTLY se construit jusqu'à "
                  f"{PLAFOND_INDEX_LIGNES:_} lignes (défaut "
                  f"{index_concurrent.CONSTRUCTION_MAX_LIGNES:_}) ; au-delà, "
                  f"`oto-mcp maintenance index-concurrents <révision>`")
            sys.stdout.flush()
            migrer()
            apres = verdict(tete, connues, *lire(dsn))
            if apres[0] != A_JOUR:
                raise Refus(f"après `migrer upgrade head`, la base n'est pas à la tête du "
                            f"tag : {apres[1]}")
            code, message = A_JOUR, f"migrations : base migrée de {depuis} vers {tete}"
    except Refus as refus:
        prefixe = "retour" if mode == "--retour" else "migrations"
        print(f"{prefixe} : REFUS — {refus}", file=sys.stderr)
        return ILLISIBLE
    print(message, file=sys.stdout if code == A_JOUR else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
