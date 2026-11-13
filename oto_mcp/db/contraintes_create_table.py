"""Les contraintes d'un `CREATE TABLE` qu'une base déjà servie n'a jamais reçues (#786).

Une contrainte déclarée INLINE dans un `CREATE TABLE IF NOT EXISTS` n'arrive que sur une
base neuve : sur une base qui existe déjà, le `CREATE TABLE` est sauté, l'`ALTER` du
démarrage rend la colonne mais pas la contrainte. Quatre contraintes manquaient ainsi à la
base de l'instance principale, alors qu'une instance née après elles les porte :

- `projects_mcp_slug_key` UNIQUE (mcp_slug) — la base servie n'a que l'index PARTIEL
  `uq_projects_mcp_slug` (… WHERE mcp_slug IS NOT NULL), qui ne se promeut pas ;
- `org_instructions_owner_pkey` PRIMARY KEY (owner_type, owner_id, slug) ;
- `org_instruction_revisions_owner_pkey` PRIMARY KEY (owner_type, owner_id, slug, version) ;
- `resource_grants_role_check` CHECK (role IN ('viewer', 'editor', 'manager')).

**Les noms et les formes sont ceux du `CREATE TABLE`** (`db/schema/`) : la base remise à
niveau devient identique à une base neuve, au catalogue près. Une base neuve porte AUSSI les
index uniques du démarrage (`uq_org_instructions_owner_slug`, `uq_org_instruction_revisions_
owner`, `uq_projects_mcp_slug`) à côté de l'index de la contrainte : on ne les consomme donc
pas — promouvoir `uq_…` le RENOMMERAIT, et le démarrage suivant le reconstruirait, non
concurrent, dans sa transaction. L'index de la clé se construit à part, CONCURRENTLY
(`index_concurrent`), puis se promeut (`ADD CONSTRAINT … USING INDEX`) : un ordre de
catalogue, sans parcours de la table.

Le constat se lit au catalogue (`pg_constraint`, `pg_index`, `pg_attribute`) avant tout
ordre — sans verrou de table, sauf l'`AccessShareLock` (celui d'un SELECT) que prend la
relecture de l'expression d'un CHECK : une contrainte déjà posée sous la forme attendue ne fait rien ; posée sous le même
nom avec une AUTRE forme, elle lève (`ContrainteDivergente`) — jamais tenue pour faite.

Le verdict prend `scalaire(sql) -> valeur | None`, comme `index_concurrent` : la révision
passe par SQLAlchemy, le banc du rejeu du démarrage par psycopg.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .index_concurrent import IndexConcurrent

REVISION = "0057_contraintes_create_table"

Scalaire = Callable[[str], Optional[Any]]

#: Attente maximale d'un verrou EXCLUSIF bref (promotion, pose du CHECK `NOT VALID`).
#: Courte exprès : un `AccessExclusiveLock` EN ATTENTE bloque toutes les lectures qui
#: arrivent derrière lui. Hors transaction, un `SET` vaut pour la session : remis à zéro.
ATTENTE_BREVE = "SET lock_timeout = '5s'"


@dataclass(frozen=True)
class Cle:
    """Une clé primaire ou une unicité du `CREATE TABLE`, portée par son index."""

    index: IndexConcurrent
    colonnes: tuple[str, ...]
    primaire: bool

    @property
    def nom(self) -> str:
        return self.index.nom

    @property
    def table(self) -> str:
        return self.index.table

    @property
    def definition(self) -> str:
        """`pg_get_constraintdef` de la contrainte posée."""
        genre = "PRIMARY KEY" if self.primaire else "UNIQUE"
        return f"{genre} ({', '.join(self.colonnes)})"

    @property
    def indexdef(self) -> str:
        """`pg_get_indexdef` de l'index qu'on promeut : exactement ces colonnes, sans
        prédicat — sinon la promotion poserait une autre clé que celle du `CREATE TABLE`."""
        return (f"CREATE UNIQUE INDEX {self.nom} ON public.{self.table} "
                f"USING btree ({', '.join(self.colonnes)})")

    @property
    def ddl_promotion(self) -> str:
        genre = "PRIMARY KEY" if self.primaire else "UNIQUE"
        return (f"ALTER TABLE {self.table} ADD CONSTRAINT {self.nom} {genre} "
                f"USING INDEX {self.nom}")

    @property
    def ddl_retrait(self) -> str:
        """Le retour arrière manuel : la contrainte ET son index."""
        return f"ALTER TABLE {self.table} DROP CONSTRAINT {self.nom}"


@dataclass(frozen=True)
class Verification:
    """Un CHECK du `CREATE TABLE`, posé `NOT VALID` puis validé à part."""

    nom: str
    table: str
    expression: str
    #: `pg_get_constraintdef` une fois validée.
    definition: str

    @property
    def ddl_pose(self) -> str:
        return (f"ALTER TABLE {self.table} ADD CONSTRAINT {self.nom} "
                f"CHECK ({self.expression}) NOT VALID")

    @property
    def ddl_validation(self) -> str:
        return f"ALTER TABLE {self.table} VALIDATE CONSTRAINT {self.nom}"

    @property
    def ddl_retrait(self) -> str:
        return f"ALTER TABLE {self.table} DROP CONSTRAINT {self.nom}"


PROJECTS_MCP_SLUG = Cle(
    index=IndexConcurrent(
        nom="projects_mcp_slug_key",
        table="projects",
        forme="(mcp_slug)",
        revision=REVISION,
        unique=True,
    ),
    colonnes=("mcp_slug",),
    primaire=False,
)
ORG_INSTRUCTIONS_OWNER = Cle(
    index=IndexConcurrent(
        nom="org_instructions_owner_pkey",
        table="org_instructions",
        forme="(owner_type, owner_id, slug)",
        revision=REVISION,
        unique=True,
    ),
    colonnes=("owner_type", "owner_id", "slug"),
    primaire=True,
)
ORG_INSTRUCTION_REVISIONS_OWNER = Cle(
    index=IndexConcurrent(
        nom="org_instruction_revisions_owner_pkey",
        table="org_instruction_revisions",
        forme="(owner_type, owner_id, slug, version)",
        revision=REVISION,
        unique=True,
    ),
    colonnes=("owner_type", "owner_id", "slug", "version"),
    primaire=True,
)
RESOURCE_GRANTS_ROLE = Verification(
    nom="resource_grants_role_check",
    table="resource_grants",
    # La forme du `CREATE TABLE` d'AUJOURD'HUI (`db/schema/grants.py`), `contributor`
    # compris depuis la révision 20261113_1900_app_use_role : une base neuve la porte,
    # une base versionnée l'a reçue de cette révision ; 0057 ne s'y rejoue pas.
    expression="role IN ('viewer', 'contributor', 'editor', 'manager')",
    definition=("CHECK ((role = ANY (ARRAY['viewer'::text, 'contributor'::text, "
                "'editor'::text, 'manager'::text])))"),
)

CLES: tuple[Cle, ...] = (PROJECTS_MCP_SLUG, ORG_INSTRUCTIONS_OWNER,
                         ORG_INSTRUCTION_REVISIONS_OWNER)
VERIFICATIONS: tuple[Verification, ...] = (RESOURCE_GRANTS_ROLE,)


class ContrainteDivergente(RuntimeError):
    """Un objet porte déjà le nom attendu, sous une autre forme : ni le tenir pour fait,
    ni le remplacer en douce — l'écart se regarde à la main."""


class ColonnesNullables(RuntimeError):
    """Une clé primaire sur des colonnes encore nullables : `ADD PRIMARY KEY USING INDEX`
    poserait le NOT NULL en parcourant la table sous `AccessExclusiveLock`."""


def _litteral(texte: str) -> str:
    return "'" + texte.replace("'", "''") + "'"


def _definition_posee(table: str, nom: str, scalaire: Scalaire) -> Optional[str]:
    return scalaire(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        f"WHERE c.conrelid = {_litteral(table)}::regclass AND c.conname = {_litteral(nom)}")


def cle_posee(cle: Cle, scalaire: Scalaire) -> bool:
    """`True` si la contrainte est déjà là sous la forme du `CREATE TABLE`, `False` s'il
    reste à la poser. Lève si le nom est pris par une autre forme, ou si une clé primaire
    porterait sur des colonnes nullables."""
    posee = _definition_posee(cle.table, cle.nom, scalaire)
    if posee is not None:
        if posee != cle.definition:
            raise ContrainteDivergente(
                f"{cle.table}.{cle.nom} est posée comme « {posee} », le CREATE TABLE "
                f"déclare « {cle.definition} ».")
        return True
    if cle.primaire:
        nullables = scalaire(
            "SELECT string_agg(attname, ', ' ORDER BY attnum) FROM pg_attribute "
            f"WHERE attrelid = {_litteral(cle.table)}::regclass AND NOT attisdropped "
            f"AND attname IN ({', '.join(_litteral(c) for c in cle.colonnes)}) "
            "AND NOT attnotnull")
        if nullables:
            raise ColonnesNullables(
                f"{cle.table} : {nullables} encore nullable(s) — la clé primaire "
                f"{cle.nom} les passerait NOT NULL en parcourant la table sous verrou "
                "exclusif. Poser le NOT NULL d'abord (CHECK … NOT VALID, VALIDATE, "
                "SET NOT NULL), puis rejouer.")
    return False


def verifier_index(cle: Cle, scalaire: Scalaire) -> None:
    """L'index à promouvoir a exactement la forme de la clé — un index homonyme posé à
    la main sous une autre forme ne devient pas la clé primaire."""
    posee = scalaire(f"SELECT pg_get_indexdef(to_regclass({_litteral(cle.nom)}))")
    if posee != cle.indexdef:
        raise ContrainteDivergente(
            f"l'index {cle.nom} est « {posee} », la clé attend « {cle.indexdef} ».")


def etat_verification(verif: Verification, scalaire: Scalaire) -> str:
    """`absente`, `a_valider` (posée `NOT VALID`, une reprise) ou `posee`."""
    posee = _definition_posee(verif.table, verif.nom, scalaire)
    if posee is None:
        return "absente"
    if posee == verif.definition:
        return "posee"
    if posee == verif.definition + " NOT VALID":
        return "a_valider"
    raise ContrainteDivergente(
        f"{verif.table}.{verif.nom} est posée comme « {posee} », le CREATE TABLE "
        f"déclare « {verif.definition} ».")


def poser_dans_la_transaction(conn, scalaire: Scalaire) -> list[str]:
    """La même remise à niveau, DANS une transaction, sans CONCURRENTLY : le chemin du
    banc qui rejoue le démarrage sur une base d'avant (`tests/test_boot_order_replay.py`),
    jamais celui d'une base servie. Rend les ordres joués."""
    joues: list[str] = []

    def jouer(sql: str) -> None:
        conn.execute(sql)
        joues.append(sql)

    for cle in CLES:
        if cle_posee(cle, scalaire):
            continue
        if scalaire(cle.index.sql_validite) is None:
            jouer(cle.index.ddl_demarrage)
        verifier_index(cle, scalaire)
        jouer(cle.ddl_promotion)
    for verif in VERIFICATIONS:
        etat = etat_verification(verif, scalaire)
        if etat == "absente":
            jouer(verif.ddl_pose)
        if etat != "posee":
            jouer(verif.ddl_validation)
    return joues
