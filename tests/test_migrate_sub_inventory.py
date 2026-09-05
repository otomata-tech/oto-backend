"""Garde-fou de l'inventaire `_SUB_COLUMNS` (migrate_sub, bascule de tenant #35/#56).

La boucle de `migrate_sub` fait des UPDATE nus dans UNE transaction : une entrée
pointant une table/colonne ABSENTE fait échouer tout le merge — silencieusement,
puisque rien ne l'exerce en CI. Vécu (Phase H B1, 10/07) : `user_grants` droppée
par 0044 §F mais restée listée → migrate_sub cassé pendant deux jours.

Ce test fige le contrat : chaque `(table, col)` de l'inventaire doit exister dans
le DDL — colonne déclarée dans le bloc CREATE TABLE de `_schema.py`, OU ajoutée
par un `ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col>` de `_init.py`. Dropper
une table/colonne sans retirer son entrée casse ce test au lieu de casser le
merge en prod.
"""
import pathlib
import re

from oto_mcp.db._schema import _SCHEMA
from oto_mcp.db.users import _SUB_COLUMNS

_INIT_SRC = (pathlib.Path(__file__).resolve().parent.parent
             / "oto_mcp" / "db" / "_init.py").read_text(encoding="utf-8")


def _create_blocks(schema_sql: str) -> dict[str, str]:
    """{table: corps du CREATE} — parse suffisant pour vérifier la présence d'un
    nom de colonne (les DDL du repo sont réguliers : un bloc par table)."""
    blocks = {}
    for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", schema_sql, re.S):
        blocks[m.group(1)] = m.group(2)
    return blocks


def test_sub_columns_inventory_matches_ddl():
    blocks = _create_blocks(_SCHEMA)
    problems = []
    for table, col in _SUB_COLUMNS:
        body = blocks.get(table)
        if body is None:
            problems.append(f"{table}.{col} : table absente de _schema.py")
            continue
        in_create = re.search(rf"^\s*{col}\b", body, re.M) is not None
        in_alter = re.search(
            rf"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}\b",
            _INIT_SRC) is not None
        if not (in_create or in_alter):
            problems.append(f"{table}.{col} : colonne introuvable (CREATE et ALTER)")
    assert not problems, (
        "entrées _SUB_COLUMNS mortes (migrate_sub échouerait en prod) :\n  "
        + "\n  ".join(problems)
        + "\nRetirer l'entrée de l'inventaire (ou restaurer la colonne)."
    )


def test_active_membership_tables_are_pre_treated():
    """Une table `(scope, sub)` avec un `is_active` UNIQUE par sub ne peut pas passer
    par l'UPDATE nu de `_SUB_COLUMNS`.

    Vécu prod 2026-07-28 (un compte réel) : `UPDATE org_members SET sub=new` a fait
    porter DEUX appartenances actives au même sub → `UniqueViolation
    org_members_one_active`. Le merge échouait à CHAQUE requête de l'utilisateur (donc
    jamais fusionné, plus un round-trip Logto et un traceback par appel). Ces tables
    doivent être listées dans `_MEMBERSHIP_TABLES` et traitées AVANT la boucle.
    """
    from oto_mcp.db.users import _MEMBERSHIP_TABLES
    declared = {t for t, _ in _MEMBERSHIP_TABLES}
    # Source de vérité = le DDL : tout index unique partiel `ON <table>(sub) WHERE is_active`.
    in_ddl = set(re.findall(
        r"CREATE UNIQUE INDEX IF NOT EXISTS \w+\s*\n?\s*ON (\w+)\(sub\) WHERE is_active",
        _SCHEMA))
    assert in_ddl, "le parse du DDL ne trouve plus les index `one_active` — test à réparer"
    manquantes = in_ddl - declared
    assert not manquantes, (
        f"tables à `is_active` unique non pré-traitées par migrate_sub : {sorted(manquantes)}. "
        "Les ajouter à `_MEMBERSHIP_TABLES` (sinon le merge de comptes lève "
        "UniqueViolation en prod, en silence côté CI).")


def test_migrate_sub_sub_bearing_columns_are_triaged():
    """Le tripwire INVERSE — celui dont l'absence a coûté `orgs.personal_of` (14
    espaces personnels en double, 14/08) puis les neuf colonnes du dossier du 23/08
    (déroulés, activité, CGU, déclencheurs, réservations, options comp invisibles au
    compte fusionné) : `test_sub_columns_inventory_matches_ddl` vérifie que les
    entrées LISTÉES existent, jamais qu'une colonne porteuse d'un identifiant de
    compte SOIT listée.

    Ici : toute colonne du DDL dont le nom appartient à la famille « porte un sub »
    doit être TRIAGÉE — repointée (`_SUB_COLUMNS`), pré-traitée (`_PK_SUB_TABLES`,
    `_MEMBERSHIP_TABLES`), ou dans l'allowlist ci-dessous AVEC sa raison. Une
    colonne neuve de cette famille arrive donc ROUGE : le triage (repointer ou
    abandonner, et pourquoi) devient un acte explicite, plus un oubli.
    """
    from oto_mcp.db.users import _MEMBERSHIP_TABLES, _PK_SUB_TABLES

    NAMES = ("sub|old_sub|new_sub|effective_sub|owner_sub|grantee_sub|accepted_sub|"
             "personal_of|requested_by|resolved_by|granted_by|created_by|set_by|"
             "invited_by|published_by|suspended_by|principal_id|entity_id|grantee_id|owner_id")
    porteurs: set[tuple[str, str]] = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                         _SCHEMA, re.S):
        table, body = m.group(1), m.group(2)
        for lm in re.finditer(rf"^\s*({NAMES})\s+TEXT", body, re.M):
            porteurs.add((table, lm.group(1)))
    for am in re.finditer(
            rf"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS ({NAMES})\s+TEXT",
            _INIT_SRC):
        porteurs.add((am.group(1), am.group(2)))
    assert porteurs, "le parse du DDL ne trouve plus de colonne porteuse — test à réparer"

    # Hors inventaire, chacune pour une raison STRUCTURELLE (pas un oubli) :
    allow = {
        # oto#70 lot 2 — le relevé des écritures de couche `origine`. Trois raisons,
        # et la première est structurelle : **son `sub` est NULLABLE par conception**,
        # parce qu'un appel par jeton d'API n'en porte pas (oto-backend#882) et que
        # cette population-là est justement celle qu'on cherche à joindre. Une colonne
        # nullable ne peut pas entrer dans une clé primaire, donc `_PK_SUB_TABLES` ne
        # s'applique pas ; et son index unique porte le sub, donc l'UPDATE nu de
        # `_SUB_COLUMNS` y lèverait `UniqueViolation` dès que les deux comptes d'une
        # personne ont écrit la même colonne — le cas nominal après une bascule.
        #
        # Ce que l'abandon coûte, mesuré et assumé : la ligne survit au merge avec un
        # sub périmé. Cette table compte une POPULATION pour dimensionner un préavis,
        # pas un historique — un écrivain de plus dans le compte est sans conséquence,
        # et la table disparaît avec le préavis. C'est le seul endroit où cet argument
        # vaut : ailleurs, une ligne abandonnée est une donnée perdue.
        ("origine_ecritures", "sub"),
        # L'ENTITÉ du coffre entre dans l'AAD : une ligne repointée sans rechiffrement
        # est indéchiffrable — pire qu'absente (0052 §Migrer : l'utilisateur repose
        # ses clés, jamais d'UPDATE ici).
        ("connector_credentials", "entity_id"),
        # L'instance (lot L6) SUIT la ligne de coffre : son `owner_id` EST
        # l'`entity_id` juste au-dessus, et le lien entre les deux est ce quadruplet.
        # Repointer l'instance seule la DÉTACHERAIT de sa ligne de coffre — un objet
        # qui désigne une clé qui n'existe pas, strictement pire que rien. Elle ne
        # peut donc pas être repointée tant que la ligne du coffre ne l'est pas, et
        # la ligne du coffre ne l'est jamais (l'AAD). Le compte migré repose ses clés,
        # le boot suivant nomme les lignes neuves.
        # ⚠️ CORRIGÉ le 2026-08-28 (L6 pièce 2), qui EST « le lot qui fait suivre
        # l'instance aux déplacements du coffre » : il n'y a rien à archiver ici. La
        # ligne du coffre n'est pas SUPPRIMÉE par une bascule de compte, seulement
        # abandonnée en place — instance et ligne restent donc appariées, et
        # l'invariant (chaque ligne vivante ↔ une instance vivante, dans les deux
        # sens) tient. Archiver l'instance seule le CASSERAIT, à l'endroit précis où
        # la version précédente de ce commentaire proposait de le faire. Le compte
        # migré repose ses clés : ce sont des instances neuves, nommées à la pose.
        ("connector_instances", "owner_id"),
        # Repointée par l'étape 3 bis de migrate_sub, FILTRÉE sur grantee_kind='user'
        # (la colonne porte aussi des ids d'org) — pas un UPDATE nu d'inventaire.
        ("grants", "grantee_id"),
        # owner_type ∈ {org, group} : ces owner_id sont des ids numériques, jamais un
        # sub (les procédures user n'existent pas dans cette table).
        ("org_instructions", "owner_id"), ("org_instruction_revisions", "owner_id"),
        # La marque d'espace personnel : étape 2 quater (index unique ⟹ démarquage
        # conditionnel, pas un UPDATE nu). Vécu 14/08.
        ("orgs", "personal_of"),
        # La table d'alias EST le produit du merge — la repointer se mordrait la queue.
        ("sub_aliases", "old_sub"), ("sub_aliases", "new_sub"),
        # Étape 2 : PK sub ⟹ DELETE du frais puis repointage, pas un UPDATE nu.
        ("user_account_profile", "sub"),
        # Le sujet même du merge (étapes 1 et 4).
        ("users", "sub"),
    }
    couvertes = (set(_SUB_COLUMNS)
                 | {(t, c) for t, c, _ in _PK_SUB_TABLES}
                 | {(t, "sub") for t, _ in _MEMBERSHIP_TABLES}
                 | allow)
    manquantes = porteurs - couvertes
    assert not manquantes, (
        "colonnes porteuses d'un sub NON triagées par migrate_sub :\n  "
        + "\n  ".join(f"{t}.{c}" for t, c in sorted(manquantes))
        + "\nLes repointer (_SUB_COLUMNS / _PK_SUB_TABLES), les traiter à part, ou "
          "les ajouter à l'allowlist de ce test AVEC leur raison.")
    mortes = allow - porteurs
    assert not mortes, (
        f"entrées d'allowlist sans colonne DDL correspondante : {sorted(mortes)} — "
        "retirer l'entrée (la colonne a disparu) ou réparer le parse.")


def test_pk_sub_tables_reste_matches_the_real_primary_key():
    """`_PK_SUB_TABLES` n'était vérifiée par RIEN — ni contre le DDL, ni contre la PK.

    Son 3ᵉ champ (`reste`) sert à bâtir le prédicat « la même ligne » du DELETE de
    l'étape 2 ter. Un `reste` incomplet ne casse pas : il ÉLARGIT le DELETE. Oublier
    `grantee_group_id`, par exemple, ferait supprimer TOUS les prêts de l'ancien
    compte sur ce canal dès que le nouveau en porte un seul — une perte silencieuse,
    au milieu de la transaction censée les sauver. Un `reste` en trop, à l'inverse,
    rend le DELETE inopérant et laisse l'`UPDATE` suivant lever `UniqueViolation`,
    donc échouer tout le merge (le mode d'échec vécu le 28/07 sur `org_members`).

    On dérive donc la PK du DDL : `{col} | reste` doit être EXACTEMENT la clé
    primaire de la table, et `col` doit en faire partie. Une entrée dont la table a
    disparu rougit ici plutôt qu'en fenêtre de bascule.
    """
    from oto_mcp.db.users import _PK_SUB_TABLES

    pks: dict[str, tuple[str, ...]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                         _SCHEMA, re.S):
        table, body = m.group(1), m.group(2)
        declaree = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", body)
        if declaree:
            pks[table] = tuple(c.strip() for c in declaree.group(1).split(","))
            continue
        inline = re.findall(r"^\s*(\w+)\s+[^,\n]*\bPRIMARY KEY\b", body, re.M)
        if inline:
            pks[table] = tuple(inline)
    assert pks, "le parse du DDL ne trouve plus aucune PRIMARY KEY — test à réparer"

    problems = []
    for table, col, reste in _PK_SUB_TABLES:
        reelle = pks.get(table)
        if reelle is None:
            problems.append(f"{table}.{col} : table sans PRIMARY KEY dans _schema.py")
            continue
        if col not in reelle:
            problems.append(
                f"{table}.{col} : la colonne de sub n'est PAS dans la PK {reelle} — "
                "elle relève alors de _SUB_COLUMNS (UPDATE nu), pas d'ici")
            continue
        if set(reste) | {col} != set(reelle):
            problems.append(
                f"{table}.{col} : reste={reste} ⟹ clé {sorted(set(reste) | {col})}, "
                f"or la PK du DDL est {sorted(reelle)}")
    assert not problems, (
        "entrées _PK_SUB_TABLES dont le `reste` ne décrit pas la vraie PK :\n  "
        + "\n  ".join(problems)
        + "\nLe DELETE de l'étape 2 ter supprimerait trop (reste incomplet) ou rien "
          "(reste en trop, puis UniqueViolation à l'UPDATE).")


# `CREATE UNIQUE INDEX [CONCURRENTLY] [IF NOT EXISTS] nom ON table(cols) [WHERE pred]`.
# Deux sources, et c'est LE point : le DDL déclaratif de `_schema.py`, ET les
# `conn.execute(...)` de `_init.py`. Aucune garde ne regardait la seconde — or DIX des
# QUATORZE index uniques du schéma y sont créés (dont quatre des huit qui couvrent une
# colonne porteuse de sub), `uq_user_datastores_owner_ns` compris. C'est là que le trou
# est resté.
_UNIQUE_INDEX = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"\w+\s+ON\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)


def _index_uniques(source: str) -> set[tuple[str, str]]:
    """{(table, colonne)} couvertes par un index unique. Les DDL de `_init.py` sont
    des littéraux Python parfois concaténés implicitement sur deux lignes : on les
    recolle avant de lire, sinon `ON <table>(...)` tombe hors de la chaîne trouvée."""
    recolle = re.sub(r'"\s*\n\s*"', "", source)
    couvertes = set()
    for m in _UNIQUE_INDEX.finditer(recolle):
        table = m.group(1)
        for col in m.group(2).split(","):
            couvertes.add((table, col.strip()))
    return couvertes


def test_toute_colonne_sub_sous_index_unique_est_pre_traitee():
    """Le VRAI critère du pré-traitement : « un UPDATE nu peut-il lever une violation
    d'unicité ? » — et pas « la colonne est-elle dans la PK ? ».

    `test_pk_sub_tables_reste_matches_the_real_primary_key` juge le bac de la clé
    PRIMAIRE, et il a raison de le faire : son `reste` sert à bâtir un DELETE, un
    `reste` incomplet l'ÉLARGIT. Mais il ne répond qu'à une moitié de la question.
    L'autre moitié n'était gardée par personne : une colonne repointée par l'UPDATE
    nu de `_SUB_COLUMNS` alors qu'un index UNIQUE la couvre. L'UPDATE lève alors
    `UniqueViolation`, et cette exception fait échouer **tout** le merge — mode
    d'échec vécu en prod le 2026-07-28 sur `org_members` (merge en échec à CHAQUE
    requête de l'utilisateur, donc jamais fusionné).

    `test_active_membership_tables_are_pre_treated` ne ferme que la forme
    `ON <table>(sub) WHERE is_active`, écrite en dur, et seulement dans `_SCHEMA`.
    Ici on ferme la CLASSE : n'importe quel index unique, partiel ou non, sur
    n'importe quelle colonne porteuse de sub, déclaré dans `_schema.py` OU créé par
    `_init.py`.

    Le critère est volontairement conservateur : un index unique PARTIEL est retenu
    sans regarder son prédicat. Deux lignes peuvent ne jamais y tomber ensemble —
    mais le démontrer se fait à la main, dans l'allowlist ci-dessous, jamais par
    omission silencieuse.

    Portée : seules les colonnes de `_SUB_COLUMNS` sont concernées. Une colonne qui
    n'est PAS repointée (`connector_instances.owner_id`, `orgs.personal_of`) ne subit
    aucun UPDATE nu, donc aucune violation — elle sort d'elle-même, sans exception à
    écrire.
    """
    from oto_mcp.db.users import (_MEMBERSHIP_TABLES, _PK_SUB_TABLES,
                                  _SUB_COLUMNS, _UNIQUE_INDEX_SUB_TABLES)

    du_schema = _index_uniques(_SCHEMA)
    du_init = _index_uniques(_INIT_SRC)
    assert du_schema, "le parse ne trouve plus d'index unique dans _schema.py"
    assert du_init, (
        "le parse ne trouve plus d'index unique dans _init.py — or c'est la moitié "
        "que les gardes précédentes ne regardaient pas ; sans elle ce test redevient "
        "la décoration qu'il remplace.")
    sous_index = du_schema | du_init

    pre_traitees = ({(t, c) for t, c, _r in _PK_SUB_TABLES}
                    | {(t, "sub") for t, _k in _MEMBERSHIP_TABLES}
                    | {(t, c) for t, c, _a, _p in _UNIQUE_INDEX_SUB_TABLES})

    # Sous index unique, repointée en UPDATE nu, et pourtant NON pré-traitée — chaque
    # entrée avec sa raison, et la raison ne peut pas être « on verra ».
    allow = {
        # ⚠️ TROU RÉEL, PAS UNE EXCEPTION DE CONFORT — ouvert, daté du 2026-09-02.
        # `uq_user_datastores_owner_ns (owner_type, owner_id, namespace)` n'est PAS
        # partiel : deux comptes d'une même personne qui ont chacun un namespace du
        # même nom (« prospects »…) font lever `UniqueViolation` à l'étape 3 et
        # échouer TOUT le merge. Reproduit sur base réelle le 2026-09-02.
        #
        # Il n'est pas corrigé ICI parce que le geste mécanique des autres familles —
        # DELETE de la ligne en trop — serait PIRE que la panne : `datastore_rows` est
        # en `ON DELETE CASCADE` sur `user_datastores(id)`, donc supprimer le
        # namespace de l'ancien compte détruit ses LIGNES. Un merge qui échoue est
        # bruyant et rejouable ; des lignes effacées en silence ne se rattrapent pas.
        # La résolution correcte est probablement de RENOMMER le namespace repris,
        # ce qui suppose une décision de produit (le nom que verra l'utilisateur),
        # pas un choix de tuyauterie. → à trancher, puis retirer cette entrée.
        ("user_datastores", "owner_id"),
    }

    manquantes = sorted(
        (t, c) for t, c in set(_SUB_COLUMNS)
        if (t, c) in sous_index and (t, c) not in pre_traitees and (t, c) not in allow)
    assert not manquantes, (
        "colonnes repointées par un UPDATE NU alors qu'un index UNIQUE les couvre :\n  "
        + "\n  ".join(f"{t}.{c}" for t, c in manquantes)
        + "\nL'UPDATE de l'étape 3 y lèvera `UniqueViolation` dès que les deux comptes "
          "portent la même ligne, et fera échouer TOUT le merge (vécu le 2026-07-28 "
          "sur `org_members`). Les pré-traiter (`_PK_SUB_TABLES` si la clé est la PK, "
          "`_UNIQUE_INDEX_SUB_TABLES` si c'est un autre index unique, "
          "`_MEMBERSHIP_TABLES` pour une appartenance), ou les allowlister ICI avec "
          "la DÉMONSTRATION que les deux lignes ne peuvent pas coexister.")

    # Une allowlist ne se périme jamais toute seule : c'est le mode de panne de ce
    # genre de liste. Trois façons de devenir sans objet — la colonne quitte
    # `_SUB_COLUMNS`, son index unique disparaît, ou le trou est enfin BOUCHÉ — et
    # la troisième est celle qu'on oublierait, puisqu'elle ne casse rien.
    mortes = sorted(e for e in allow
                    if e not in sous_index
                    or e not in set(_SUB_COLUMNS)
                    or e in pre_traitees)
    assert not mortes, (
        f"entrées d'allowlist devenues sans objet : {mortes} — la colonne a quitté "
        "`_SUB_COLUMNS`, son index unique a disparu, ou elle est désormais "
        "PRÉ-TRAITÉE. Retirer l'entrée : une exception qui survit à sa raison finit "
        "par couvrir un trou qu'on croit fermé.")
