"""Lot L1 (ADR 0052) — le tenant est NOMMÉ en base, rien ne le lit encore.

Deux modes de panne, et tous deux sont des **ordres** — donc vérifiables sans base,
là où un test SQL exigerait un PostgreSQL et ne dirait rien de plus :

1. `tenants` créée APRÈS `orgs` dans `_SCHEMA` → sur une base VIERGE, la FK
   `orgs.tenant_id → tenants(id)` échoue (`relation "tenants" does not exist`).
   C'est exactement le #151 déjà vécu sur `orgs`, dont le commentaire de `_schema`
   garde la trace.
2. La colonne ajoutée AVANT le seed du tenant 1 → `ALTER TABLE orgs … NOT NULL
   DEFAULT 1 REFERENCES tenants(id)` viole la FK dès qu'il existe une seule org.
   Sur une base vide le boot passerait, et casserait chez le premier qui a des
   données : le pire moment pour l'apprendre.

Le troisième test garde l'intention du lot : L1 **nomme** l'existant, il ne le
déplace pas. Le jour où un call-site lit `tenant_id`, ce n'est plus L1 — c'est un
autre lot, avec sa propre revue.

> **Première lecture admise, et bornée (suivi des tenants).** Le garde-fou refusait
> TOUTE lecture de `orgs.tenant_id` et demandait, le jour venu, qu'on le retire —
> le retirer rendrait alors muet ce qu'il protège vraiment : qu'aucun chemin de
> **résolution** (identité, credential, visibilité, autz) ne se mette à dépendre du
> rattachement d'org. L'écran de suivi plateforme lit la colonne pour la COMPTER, et
> ne décide de rien avec. Le garde-fou passe donc d'une interdiction totale à une
> **allowlist nommée** (patron `test_org_seam_tripwire.py`) : les deux fichiers du
> suivi, et eux seuls. Un lecteur ailleurs casse toujours — c'est le cas qu'on veut
> voir en revue.
"""
from __future__ import annotations

import pathlib
import re

from oto_mcp.db import _schema

_DB = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp" / "db"
# Le DDL n'est plus un fichier mais un ASSEMBLAGE (`db/schema/<domaine>.py`
# concaténés dans un ordre figé) : on lit la chaîne SERVIE, seule chose dont les
# ordres et les formes ci-dessous soient des propriétés.
_SCHEMA_SRC = _schema._SCHEMA
_INIT_SRC = (_DB / "_init.py").read_text(encoding="utf-8")
# Le seed du tenant 1 vit à part depuis qu'il suit la déclaration de l'instance (#969).
_SEED_SRC = (_DB / "_tenant_primaire.py").read_text(encoding="utf-8")


def test_tenants_table_is_created_before_orgs():
    tenants = _SCHEMA_SRC.index("CREATE TABLE IF NOT EXISTS tenants")
    orgs = _SCHEMA_SRC.index("CREATE TABLE IF NOT EXISTS orgs")
    assert tenants < orgs, (
        "`tenants` doit être déclarée AVANT `orgs` dans _SCHEMA : PostgreSQL crée "
        "les tables dans l'ordre du DDL, et la FK `orgs.tenant_id` échouerait sur "
        "une base vierge (même panne que #151 sur `orgs`).")


def test_tenant_one_is_seeded_before_the_column_references_it():
    seed = _INIT_SRC.index("_tenant_primaire.semer(conn)")
    alter = _INIT_SRC.index("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS tenant_id")
    assert seed < alter, (
        "le tenant 1 doit être semé AVANT l'ajout de `orgs.tenant_id` : la colonne "
        "naît `NOT NULL DEFAULT 1 REFERENCES tenants(id)`, donc la FK est violée "
        "par la première org existante si le tenant n'est pas là.")
    insert = _SEED_SRC.index("INSERT INTO tenants")
    setval = _SEED_SRC.index("pg_get_serial_sequence('tenants','id')")
    assert insert < setval, (
        "le recalage de séquence doit suivre le seed : un INSERT à id explicite ne "
        "fait pas avancer la BIGSERIAL, et le prochain tenant naîtrait sur l'id 1.")


# Les SEULS lecteurs admis de `orgs.tenant_id` : le suivi (il COMPTE le
# rattachement et n'en dérive aucune décision). Tout ajout à cette liste est un
# changement de nature — la colonne cesserait d'être un nom pour devenir une entrée
# de résolution — et doit être argumenté en revue, pas glissé dans un diff.
_LECTEURS_ADMIS = {
    "db/tenants.py",              # les compteurs de suivi (lecture seule)
    "capabilities/tenants_admin.py",  # la capacité qui les sert (PLATFORM_ADMIN)
    # L'audience d'une relance de plateforme (2026-09-02). Entrée DÉLIBÉRÉE, et deux
    # précisions qui la bornent :
    #  - ce module ne lit pas `orgs.tenant_id` lui-même — il réutilise
    #    `tenants._ORG_TENANT_EXPR`, déjà admise ci-dessus ; ce que le grep attrape est
    #    le join sur le tenant du SUB (`tenants.id`), plus la prose qui l'explique ;
    #  - le rattachement n'y sert qu'à REFUSER (écarter les comptes d'un partenaire
    #    d'un envoi), jamais à accorder quoi que ce soit. Aucune identité, aucun
    #    credential, aucune visibilité n'en dépend — ce que ce garde-fou protège.
    "db/outreach.py",
    # L'audience des emails d'activation d'un tenant (2026-10-03). Même nature que la
    # relance : le grep attrape le join sur le tenant du SUB (`tenants.id`), jamais
    # `orgs.tenant_id`. Le rattachement n'y sert qu'à CHOISIR à qui un tenant écrit
    # (ses propres comptes) — aucune identité, aucun credential, aucune visibilité.
    "db/activation.py",
    # Le CONTRÔLE DE CONFORMITÉ du rattachement (2026-09-03) vit dans `db/tenants.py`,
    # déjà admis ci-dessus — rien à ajouter pour lui.
    #
    # La DÉSACTIVATION d'un tenant (2026-10-06, oto-backend#1165) lit aussi le
    # rattachement, dans `db/tenants.py`, déjà admis — et c'est un changement de NATURE,
    # décidé et non glissé : désactiver un tenant suspend les orgs qui lui sont
    # rattachées (`desactiver_tenant`), et la levée d'une de ses orgs par le geste d'org
    # est refusée tant qu'il l'est (`tenant_desactive_de_l_org`, appelée par
    # `org_store.resume_org`). Le rattachement n'y sert qu'à COUPER (suspendre, refuser
    # une levée), jamais à accorder : aucune identité, aucun credential, aucune
    # visibilité n'en dépend. Les deux lectures restent dans ce fichier : un nouveau
    # lecteur ailleurs tombe ici. (`orgs.suspended_tenant_id`, l'ORIGINE d'une
    # suspension, est une autre colonne : le mot entier ne l'attrape pas.)
    #
    # L'effacement d'une org (`docs/effacement-d-org.md`) NOMME la colonne parmi celles
    # que la pierre tombale de la ligne `orgs` GARDE (`PIERRE_TOMBALE_GARDE`) : il ne la
    # lit pas et n'en dérive rien — le tenant n'est jamais du périmètre effacé.
    "effacement/disposition.py",
}

# L'AXE gardé : la colonne `tenant_id`, en mot ENTIER — `suspended_tenant_id` (l'origine
# d'une suspension d'org, #1165) n'est ni une lecture ni une écriture du rattachement.
# `o.tenant_id`, `tenant_id = %s`, `(… tenant_id)` restent attrapés.
_COLONNE = re.compile(r"\btenant_id\b")

# ── L'ÉCRIVAIN (2026-09-03) ──────────────────────────────────────────────────
#
# L1 laissait la colonne au DEFAULT : 165 orgs sur 165 portaient le tenant primaire,
# dont les 65 qui vivent chez un partenaire. Le provisioning l'écrit désormais à la
# naissance de l'org — sinon on la remplit à la main tous les six mois.
#
# ⚠️ **Écrire n'est pas résoudre**, et c'est ce qui rend l'ajout compatible avec ce
# que ce fichier protège : `create_org` POSE le rattachement dérivé de l'émetteur du
# jeton de son créateur ; aucune identité, aucun credential, aucune visibilité, aucune
# autz ne le LIT pour décider. La cascade de credentials le dit à ses deux points
# d'entrée (`access/cascade.py`, `access/chain_resolution.py`) : le tenant servi est
# celui du SUB appelant, « jamais sur le rattachement de l'org ».
#
# Deux fichiers, deux natures :
_ECRIVAIN_UNIQUE = "org_store/orgs.py"      # le SEUL SQL qui pose la colonne
_DERIVATION_ADMISE = "config.py"            # `tenant_slug_for`, qui la dérive du sub
_ECRIVAINS_ADMIS = {_ECRIVAIN_UNIQUE, _DERIVATION_ADMISE}


def test_only_the_tracking_read_touches_tenant_id():
    """L1 nomme l'existant, il ne le déplace pas.

    Ce que le garde-fou protège n'est pas « personne ne lit la colonne » mais
    « aucune RÉSOLUTION n'en dépend » : identité, credential, visibilité, autz
    continuent d'ignorer le rattachement d'org. Un lecteur hors allowlist tombe.
    """
    root = _DB.parent
    readers = []
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            if not _COLONNE.search(line):
                continue
            # Ni la DDL (`_schema` et les fragments de `db/schema/`), ni la
            # migration (`_init`), ni un commentaire — Python ou SQL — ne sont des
            # LECTEURS de la colonne.
            if path.name in ("_init.py", "_schema.py"):
                continue
            if path.parent.name == "schema" and path.parent.parent.name == "db":
                continue
            if line.lstrip().startswith(("#", "--")):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in _LECTEURS_ADMIS or rel in _ECRIVAINS_ADMIS:
                continue
            readers.append(f"{path.relative_to(root.parent)}: {line.strip()}")
    assert not readers, (
        "Quelqu'un lit `orgs.tenant_id` hors du suivi, ce qui déborde du lot L1 "
        f"(« l'existant est nommé, pas déplacé ») : {readers}. Un chemin de "
        "résolution qui dépend du rattachement d'org est un LOT, avec sa revue : "
        f"l'ajouter à _LECTEURS_ADMIS doit être un acte délibéré.")


def _ecrit_la_colonne(bloc: str) -> bool:
    """Ce qui suit `INSERT INTO orgs` / `UPDATE orgs` ÉCRIT-il la colonne ?

    La portée d'un ordre SQL, généreusement : 400 caractères, assez pour attraper la
    colonne dans la liste d'un INSERT comme dans un SET. Coupée au premier `WHERE` : ce
    qui le suit FILTRE, n'écrit pas — un `UPDATE orgs … WHERE tenant_id = …` LIT le
    rattachement (le garde des lecteurs le voit) sans le poser. Une écriture de la
    colonne est toujours AVANT : dans la liste d'un INSERT, dans un SET, ou dans le
    SET d'un `ON CONFLICT DO UPDATE`."""
    return bool(_COLONNE.search(bloc[:400].split("WHERE", 1)[0]))


def test_le_garde_des_ecrivains_vise_la_colonne_et_rien_d_autre():
    """Le contrefactuel du garde : sans lui, un garde affaibli serait vert."""
    assert _ecrit_la_colonne(" (name, tenant_id) VALUES (%s, %s)")
    assert _ecrit_la_colonne(" SET tenant_id = %s WHERE id = %s")
    assert _ecrit_la_colonne(" o SET name = %s, tenant_id = t.id FROM tenants t WHERE t.slug = %s")
    assert _ecrit_la_colonne(" (id, tenant_id) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE "
                             "SET tenant_id = EXCLUDED.tenant_id WHERE orgs.id > 0")
    # L'origine d'une suspension n'est pas le rattachement ; un filtre n'est pas une pose.
    assert not _ecrit_la_colonne(" SET suspended_at = NOW(), suspended_tenant_id = %s "
                                 "WHERE tenant_id = %s AND suspended_at IS NULL")
    assert not _ecrit_la_colonne(" SET suspended_tenant_id = NULL WHERE suspended_tenant_id = %s")
    # Les lecteurs : le mot entier, préfixe d'alias compris.
    assert _COLONNE.search("ON t.id = o.tenant_id") and _COLONNE.search("WHERE tenant_id = %s")
    assert not _COLONNE.search("o.suspended_tenant_id IS NOT NULL")


def test_le_rattachement_a_un_ecrivain_et_un_seul():
    """`orgs.tenant_id` s'écrit à UN endroit — sinon la dérivation ne prouve rien.

    ⚠️ **Cette unicité compte PLUS depuis le 03/09/2026, pas moins.** Le contrôle de
    conformité qui rapportait les écarts a été retiré ce jour-là — il surveillait une
    divergence sans conséquence, puisque `db.org_tenant_slug` double ce rattachement
    par deux dérivations lues du jeton. Conséquence : plus rien ne rapporte un second
    écrivain qui poserait la colonne selon sa propre règle. Ce test est donc devenu la
    SEULE chose qui s'y oppose, et il est mécanique là où le contrôle était humain.

    ⚠️ Le test vérifie les DEUX sens, et le second est celui qui compte le jour où
    quelqu'un « simplifie » : que le rattachement soit écrit *quelque part*. Un
    inventaire qui ne trouve rien rendrait 0 écrivain illégitime et passerait au vert
    — exactement l'état inerte d'avant ce lot, en silence.
    """
    root = _DB.parent
    ecrivains = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        if rel in ("db/_init.py", "db/_schema.py") or path.parent.name == "schema":
            continue  # DDL et migration : ils CRÉENT la colonne, ils ne la peuplent pas
        for ordre in ("INSERT INTO orgs", "UPDATE orgs"):
            for bloc in src.split(ordre)[1:]:
                if _ecrit_la_colonne(bloc):
                    ecrivains.append(rel)
    assert ecrivains, (
        "PERSONNE n'écrit `orgs.tenant_id` : le provisioning est retombé au DEFAULT "
        "de la colonne, et toute org naîtra sur le tenant primaire quel que soit son "
        "émetteur — l'état inerte que ce lot ferme (65 orgs sur 165 au 2026-09-03). "
        "Ce contrôle rougit AUSSI quand il ne trouve rien : un zéro n'est pas un vert.")
    assert set(ecrivains) == {_ECRIVAIN_UNIQUE}, (
        f"`orgs.tenant_id` est écrit hors de l'écrivain unique : {sorted(set(ecrivains))}. "
        f"Le rattachement se pose dans `{_ECRIVAIN_UNIQUE}` et nulle part ailleurs — "
        "un second écrivain avec sa propre règle le ferait diverger en silence, et "
        "plus rien ne le rapporte depuis que le contrôle de conformité a été retiré "
        "(03/09/2026).")
