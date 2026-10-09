"""Un index posé CONCURRENTLY sur une table servie : quand le construire soi-même, quand
le laisser à la main.

Né pour `idx_tool_calls_org_tool_ok` (oto-backend#1145), généralisé pour les index de la
recherche dans les valeurs servies (#307) : chaque index de ce régime naît par deux
chemins — sa révision Alembic (CONCURRENTLY, hors transaction) ou le geste manuel sur une
base servie, et le démarrage d'une base neuve (`_init.py`, non concurrent, dans sa
transaction) — et les deux appliquent le MÊME verdict, écrit ici une fois. Un index posé
avant la référence du registre (squash, docs/migrations-versionnees.md §5.4) n'a plus de
révision : toute base vivante le porte, et seul le démarrage d'une base neuve le pose.

- l'index existe et il est valide → rien à faire ;
- il existe et il est INVALIDE (construction CONCURRENTLY interrompue) → `IndexInvalide` :
  `IF NOT EXISTS` le prendrait pour fait et il ne servirait jamais ;
- il est absent et sa table est PETITE (base neuve, vide, de test) → construire ;
- il est absent et sa table est GROSSE → `ConstructionManuelleRequise`. Mesuré en
  production le 04/10/2026 : 172 s pour environ 12 M lignes de `tool_calls`, au-delà des
  120 s de la fenêtre de démarrage — et, au démarrage, non concurrent, la construction
  bloquerait les écritures de la table pendant tout ce temps.

Les deux refus renvoient à la procédure manuelle (`docs/migrations-versionnees.md`
§5.1). La taille se lit dans `pg_class.reltuples` (l'estimation de l'ANALYZE, sans
parcours) ; une table jamais analysée (`reltuples = -1`) est comptée, au plus jusqu'au
seuil plus une ligne.

Le verdict prend `scalaire(sql) -> valeur | None` : la révision passe par SQLAlchemy,
le démarrage par psycopg, le banc par un faux — une seule logique pour les trois.

**Verrous de la révision.** `ShareUpdateExclusiveLock` sur la table seulement : ni
lectures ni écritures bloquées. La phase concurrente ATTEND la fin de toute transaction
ouverte avant elle, par des attentes de verrou sur leur `virtualxid`, et `lock_timeout`
coupe CES attentes aussi : à 2 s, une construction a échoué en production
(`LockNotAvailable`, index laissé invalide). Il vaut donc 5 min.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Au-delà, la construction sort de la fenêtre de démarrage ou de migration : elle se
#: fait à la main. 100 000 lignes de `tool_calls` se construisent en une fraction de
#: seconde ; un index d'expression coûteux déclare un seuil plus bas.
CONSTRUCTION_MAX_LIGNES = 100_000

#: Hors transaction, un `SET` vaut pour la SESSION : il est remis à zéro en sortie.
ATTENTE_MAX = "SET lock_timeout = '5min'"
ATTENTE_RENDUE = "RESET lock_timeout"


@dataclass(frozen=True)
class IndexConcurrent:
    """Un index de ce régime : son nom, sa table, sa forme et la révision qui le pose."""

    nom: str
    table: str
    #: Tout ce qui suit `ON <table>` : colonnes ou `USING GIN (…)`, prédicat partiel.
    forme: str
    #: L'identifiant de la révision qui le pose — cité par la procédure manuelle. `None`
    #: pour un index antérieur à la référence du registre : aucune révision à rejouer.
    revision: Optional[str] = None
    max_lignes: int = CONSTRUCTION_MAX_LIGNES

    @property
    def ddl_concurrent(self) -> str:
        """La construction de la révision : concurrente, hors transaction."""
        return f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {self.nom} ON {self.table} {self.forme}"

    @property
    def ddl_demarrage(self) -> str:
        """La construction du démarrage d'une base neuve : dans sa transaction."""
        return f"CREATE INDEX IF NOT EXISTS {self.nom} ON {self.table} {self.forme}"

    @property
    def sql_validite(self) -> str:
        """`None` si l'index est absent, sinon sa validité."""
        return ("SELECT i.indisvalid FROM pg_index i "
                f"WHERE i.indexrelid = to_regclass('{self.nom}')")

    def sql_table_trop_grosse(self, max_lignes: int) -> str:
        """`true` si la table compte plus de `max_lignes` lignes (estimation)."""
        n = int(max_lignes)
        return (
            "SELECT CASE WHEN c.reltuples >= 0 THEN c.reltuples > " f"{n} "
            f"ELSE (SELECT count(*) FROM (SELECT 1 FROM {self.table} "
            f"LIMIT {n + 1}) t) > {n} END "
            f"FROM pg_class c WHERE c.oid = '{self.table}'::regclass"
        )

    @property
    def ddl_retrait(self) -> str:
        """Le retrait d'un index INVALIDE avant de le reconstruire — un geste manuel."""
        return f"DROP INDEX CONCURRENTLY IF EXISTS {self.nom}"

    @property
    def procedure(self) -> str:
        if self.revision:
            # La commande joue le `CREATE` exact tiré du code : rien à retaper.
            return (
                "À la main, hors fenêtre de démarrage ou de migration "
                "(docs/migrations-versionnees.md §5.1) : "
                f"oto-mcp maintenance index-concurrents {self.revision}, puis "
                "oto-mcp migrer upgrade head. Un index INVALIDE se retire d'abord, à la "
                f"main : {self.ddl_retrait}; (CONCURRENTLY, hors transaction)."
            )
        return (
            "À la main, hors fenêtre de démarrage ou de migration "
            "(docs/migrations-versionnees.md §5.1) : SET statement_timeout = 0; "
            "SET lock_timeout = '5min'; "
            f"{self.ddl_retrait}; {self.ddl_concurrent}; "
            "puis vérifier `indisvalid`."
        )


class IndexInvalide(RuntimeError):
    """L'index existe mais n'est pas valide : il ne sert aucune lecture."""

    def __init__(self, index: IndexConcurrent) -> None:
        self.index = index
        super().__init__(
            f"{index.nom} existe mais est INVALIDE (construction CONCURRENTLY "
            "interrompue). " + index.procedure)


class ConstructionManuelleRequise(RuntimeError):
    """L'index est absent d'une table trop grosse pour le construire ici."""

    def __init__(self, index: IndexConcurrent, max_lignes: int) -> None:
        self.index = index
        super().__init__(
            f"{index.nom} est absent et {index.table} dépasse {max_lignes} lignes : sa "
            "construction sortirait de la fenêtre de démarrage ou de migration. "
            + index.procedure)


def scalaire_de(conn) -> Callable[[str], Optional[Any]]:
    """Le `scalaire` d'une connexion psycopg à lignes en dict (`_connect`) : la première
    colonne de la première ligne, `None` sans ligne."""
    def scalaire(sql: str) -> Optional[Any]:
        ligne = conn.execute(sql).fetchone()
        return None if ligne is None else next(iter(ligne.values()))
    return scalaire


def a_construire(index: IndexConcurrent, scalaire: Callable[[str], Optional[Any]], *,
                 max_lignes: Optional[int] = None) -> bool:
    """`True` s'il faut construire ICI, `False` si l'index est déjà là et valide.

    Lève `IndexInvalide` ou `ConstructionManuelleRequise` — jamais une construction
    longue en douce, jamais un index invalide pris pour fait."""
    seuil = index.max_lignes if max_lignes is None else max_lignes
    valide = scalaire(index.sql_validite)
    if valide is True:
        return False
    if valide is False:
        raise IndexInvalide(index)
    if scalaire(index.sql_table_trop_grosse(seuil)):
        raise ConstructionManuelleRequise(index, seuil)
    return True


def poser_au_demarrage(conn, index: IndexConcurrent) -> None:
    """La forme d'une base NEUVE, dans la transaction du démarrage. Une base servie
    le reçoit à la main ou de sa révision, CONCURRENTLY. Index invalide ou table trop
    grosse : le démarrage continue et le DIT, le geste est manuel (§5.1)."""
    try:
        if a_construire(index, scalaire_de(conn)):
            conn.execute(index.ddl_demarrage)
    except (IndexInvalide, ConstructionManuelleRequise) as e:
        logger.error("démarrage : %s", e)


def poser_par_revision(op, index: IndexConcurrent) -> None:
    """Le corps d'`upgrade()` d'une révision de ce régime, sous `autocommit_block` :
    construire si le verdict le permet, puis VÉRIFIER la validité — un index laissé
    invalide lève au lieu d'être estampillé fait."""
    with op.get_context().autocommit_block():
        bind = op.get_bind()

        def scalaire(sql: str):
            return bind.exec_driver_sql(sql).scalar()

        if not a_construire(index, scalaire):
            return
        op.execute(ATTENTE_MAX)
        try:
            op.execute(index.ddl_concurrent)
        finally:
            op.execute(ATTENTE_RENDUE)
        if scalaire(index.sql_validite) is not True:
            raise IndexInvalide(index)


def retirer_par_revision(op, index: IndexConcurrent) -> None:
    """Le retour arrière : `DROP INDEX CONCURRENTLY`, même attente bornée."""
    with op.get_context().autocommit_block():
        op.execute(ATTENTE_MAX)
        try:
            op.execute(index.ddl_retrait)
        finally:
            op.execute(ATTENTE_RENDUE)


# ── Le geste manuel, versionné (`oto-mcp maintenance index-concurrents <révision>`) ──


def declares() -> tuple[IndexConcurrent, ...]:
    """Tous les index de ce régime, là où ils sont déclarés — le registre unique que lit
    le geste manuel. Un index déclaré ailleurs et absent d'ici, la commande ne saurait
    pas le poser : un banc compare cette liste aux déclarations du code.

    Import tardif : les modules déclarants importent celui-ci."""
    from . import index_releve, search
    return (index_releve.RELEVE, *index_releve.OUVERTURES, *search.INDEX_VALEURS)


class RevisionSansIndex(ValueError):
    """La révision demandée ne pose aucun index de ce régime (inconnue, ou d'une autre
    nature) : rien à construire, et le dire plutôt que sortir vert sans rien faire."""

    def __init__(self, demandee: str, connues: list[str]) -> None:
        self.demandee = demandee
        super().__init__(
            f"aucun index CONCURRENTLY n'est posé par la révision {demandee!r}. "
            "Révisions qui en posent : " + (", ".join(connues) or "aucune") + ".")


def index_de_revision(demandee: str) -> tuple[IndexConcurrent, ...]:
    """Les index d'une révision, dans leur ordre de déclaration. `demandee` est
    l'identifiant complet ou son numéro (`0049`)."""
    tous = declares()
    connues = sorted({i.revision for i in tous if i.revision})
    retenue = [r for r in connues if r == demandee or r.split("_", 1)[0] == demandee]
    if len(retenue) != 1:
        raise RevisionSansIndex(demandee, connues)
    return tuple(i for i in tous if i.revision == retenue[0])


def poser_a_la_main(conn, demandee: str,
                    dire: Callable[[str], None] = logger.info) -> list[str]:
    """Le geste manuel du §5.1 : construire, un par un, les index d'une révision que sa
    migration refuse de construire (`ConstructionManuelleRequise`), sur une connexion en
    AUTOCOMMIT (CONCURRENTLY est refusé dans une transaction).

    Sans plafond de taille — c'est tout l'objet du geste — mais jamais sans contrôle :
    - déjà valide → rien ;
    - INVALIDE → `IndexInvalide`, avec le `DROP` à jouer, qui NE se joue PAS ici
      (décision d'Alexis, 09/10/2026 : un index cassé reste un geste humain) ; les
      suivants ne sont pas construits ;
    - absent → `ddl_concurrent`, puis `indisvalid` exigé.

    Rend une ligne par index. Révision inconnue : `RevisionSansIndex`."""
    index = index_de_revision(demandee)
    scalaire = scalaire_de(conn)
    # La construction lit deux fois toute la table ; la connexion est dédiée et fermée
    # après, donc ces réglages de session ne fuient vers personne.
    conn.execute("SET statement_timeout = 0")
    conn.execute(ATTENTE_MAX)
    faits: list[str] = []
    for i in index:
        valide = scalaire(i.sql_validite)
        if valide is True:
            faits.append(f"{i.nom} : déjà posé, valide")
            dire(faits[-1])
            continue
        if valide is False:
            raise IndexInvalide(i)
        dire(f"{i.nom} : construction CONCURRENTLY sur {i.table}…")
        debut = time.monotonic()
        conn.execute(i.ddl_concurrent)
        duree = time.monotonic() - debut
        if scalaire(i.sql_validite) is not True:
            raise IndexInvalide(i)
        faits.append(f"{i.nom} : construit et valide en {duree:.1f} s")
        dire(faits[-1])
    return faits
