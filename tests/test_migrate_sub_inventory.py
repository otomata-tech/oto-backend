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
        # Une colonne ajoutée par un fragment du schéma (`ALTER TABLE … ADD COLUMN IF NOT
        # EXISTS` dans `_schema.py`, ex. `apps.owner_id`) compte comme celle de `_init.py`.
        in_alter = any(re.search(
            rf"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}\b", src) is not None
            for src in (_INIT_SRC, _SCHEMA))
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

    # La FORME, pas une liste de noms (#439) : la liste d'avant (`created_by`,
    # `set_by`…) laissait passer `updated_by`, `edited_by`, `disabled_by`,
    # `view_as_sub`, `acteur_sub` — neuf colonnes abandonnées par chaque fusion sans
    # qu'aucune garde ne rougisse. Toute colonne `*sub` ou `*_by` est désormais vue.
    NAMES = (r"\w*sub|\w+_by|personal_of|principal_id|entity_id|grantee_id|owner_id")
    porteurs: set[tuple[str, str]] = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                         _SCHEMA, re.S):
        table, body = m.group(1), m.group(2)
        for lm in re.finditer(rf"^\s*({NAMES})\s+TEXT", body, re.M):
            porteurs.add((table, lm.group(1)))
    for src in (_INIT_SRC, _SCHEMA):
        for am in re.finditer(
                rf"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS ({NAMES})\s+TEXT", src):
            porteurs.add((am.group(1), am.group(2)))
    assert porteurs, "le parse du DDL ne trouve plus de colonne porteuse — test à réparer"

    # Hors inventaire, chacune pour une raison STRUCTURELLE (pas un oubli) :
    allow = {
        # The in-app agent's conversations and undo entries are SHORT-LIVED (an idle
        # thread closes after 30 min, an undo expires after an hour) and bound to the
        # sub that opened them: after a merge the old ones read "not found" and the
        # person starts a new one. Nothing to carry over. Its daily spend is a counter
        # of the current UTC day under the sub that spent it: after a merge, the new sub
        # starts its day at zero — at most one day's ceiling more, never a lost record.
        ("copilot_threads", "sub"), ("copilot_undos", "sub"), ("copilot_spend", "sub"),
        # Le COMPTE DE SERVICE d'une org (clés d'org, #1188) n'est pas une identité Logto :
        # aucune bascule de tenant ni fusion de comptes ne le vise, il n'a rien à suivre.
        ("org_service_accounts", "sub"),
        # L'ENTITÉ du coffre entre dans l'AAD : jamais d'UPDATE nu (la ligne serait
        # indéchiffrable). Elle SUIT pourtant la personne depuis #439 — rechiffrée,
        # pas repointée : `credentials_store.rekey_personal_credentials`, appelé par
        # `repointer_patrimoine` (étape 3 ter), garde `tests/test_migrate_sub_vault.py`.
        ("connector_credentials", "entity_id"),
        # L'instance (lot L6) SUIT la ligne de coffre : son `owner_id` EST
        # l'`entity_id` juste au-dessus, et le lien entre les deux est ce quadruplet.
        # ⚠️ Depuis #439, elle suit le RECHIFFREMENT d'une clé personnelle, avec son
        # id (`connector_instances.move_instance_to_owner`) — jamais un UPDATE nu
        # d'inventaire, qui la détacherait de sa ligne. L'historique ci-dessous
        # décrit l'état d'avant, où la ligne de coffre restait en place.
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
        # Le journal des prêts nomme sa ligne du coffre par son entité : il la SUIT quand
        # elle se rechiffre (`loan_events.suivre_la_ligne`, appelé par
        # `rekey_personal_credentials`) et reste avec elle quand elle reste en place —
        # un UPDATE nu d'inventaire le détacherait de sa ligne, comme l'instance.
        ("connector_loan_events", "entity_id"),
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
        # Identités de MACHINE, pas de personne : le worker du runner s'authentifie
        # par un secret de machine déclaré en base (jamais un compte `users`), et
        # l'ordonnanceur d'une flotte DÉCLARE son identifiant à la prise. Une fusion
        # de comptes de personne n'a rien à y repointer.
        ("runner_jobs", "claimed_by"), ("runner_fleets", "taken_by"),
        ("runner_workers", "worker_sub"), ("runner_platform_workers", "worker_sub"),
        ("runner_platform_depots", "worker_sub"),
        # Le BAIL d'une ligne de file de travail (`data_claim_next`) : un verrou
        # éphémère, borné par `claimed_until`, qui expire seul. Le repointer
        # prolongerait sous un autre nom un bail que son run a pris sous l'ancien —
        # et `claimed_run`, qui le justifie, ne suit pas.
        ("datastore_rows", "claimed_by"), ("nodes", "claimed_by"),
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
        # partiel : deux comptes d'une même personne qui ont chacun un datastore du
        # même nom (« prospects »…) font lever `UniqueViolation` à l'étape 3 et
        # échouer TOUT le merge. Reproduit sur base réelle le 2026-09-02.
        #
        # Il n'est pas corrigé ICI parce que le geste mécanique des autres familles —
        # DELETE de la ligne en trop — serait PIRE que la panne : `datastore_rows` est
        # en `ON DELETE CASCADE` sur `user_datastores(id)`, donc supprimer le
        # datastore de l'ancien compte détruit ses LIGNES. Un merge qui échoue est
        # bruyant et rejouable ; des lignes effacées en silence ne se rattrapent pas.
        # La résolution correcte est probablement de RENOMMER le datastore repris,
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


def _colonnes_jsonb() -> set[tuple[str, str]]:
    """{(table, colonne)} de type JSONB : blocs CREATE de `_SCHEMA`, et les
    `ALTER TABLE … ADD COLUMN IF NOT EXISTS … JSONB` de `_SCHEMA` et de `_init.py`."""
    cols: set[tuple[str, str]] = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                         _SCHEMA, re.S):
        for lm in re.finditer(r"^\s*(\w+)\s+JSONB\b", m.group(2), re.M):
            cols.add((m.group(1), lm.group(1)))
    for source in (_SCHEMA, _INIT_SRC):
        for am in re.finditer(
                r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)\s+JSONB\b", source):
            cols.add((am.group(1), am.group(2)))
    return cols


def test_toute_colonne_jsonb_est_triee_pour_la_fusion():
    """Le tripwire des subs DANS un JSONB (#1251). `test_migrate_sub_sub_bearing_columns
    _are_triaged` lit les NOMS de colonnes TEXT : une liste de principaux en JSONB
    (`share_side` = `["user:<sub>", "group:3"]`) lui échappait — et la fusion la laissait
    sur l'ancien sub : le compte prêté par son nom cessait de servir l'emprunteur
    fusionné, sans refus qui l'explique.

    Le nom d'une colonne JSONB ne dit pas ce qu'elle contient : TOUTE colonne JSONB du
    DDL est donc triée — suivie par la fusion (`_JSONB_SUB_COLUMNS`), ou rangée ci-dessous
    AVEC sa raison. Une colonne JSONB neuve arrive ROUGE."""
    import inspect

    from oto_mcp import credentials_store
    from oto_mcp.db import users
    from oto_mcp.db.users import _JSONB_SUB_COLUMNS

    jsonb = _colonnes_jsonb()
    assert ("connector_credentials", "share_side") in jsonb, (
        "le parse du DDL ne trouve plus `share_side` — test à réparer")

    # Ne nomme personne : configuration, structure, contenu opaque des utilisateurs ou
    # d'un tiers. Le code n'y lit aucune référence de principal.
    sans_personne = {
        # The in-app agent's transcript and pending state (messages, queue, results,
        # the page actions offered, the kinds remembered) and an undo's payload: the
        # content of a conversation, read by no principal check — ownership is the
        # `sub` column, not these.
        ("copilot_threads", "client_tools"), ("copilot_threads", "messages"),
        ("copilot_threads", "pending"), ("copilot_threads", "queue"),
        ("copilot_threads", "remembered"), ("copilot_threads", "results"),
        ("copilot_undos", "payload"),
        ("apollo_phone_reveals", "payload"), ("blocks", "props"),
        ("connector_instances", "config"), ("connector_schemas", "schema"),
        # La relève d'un travail asynchrone : sa fonction et l'id du travail chez le
        # fournisseur.
        ("credit_holds", "job"),
        ("datastore_rows", "data"), ("datastore_row_revisions", "diff"),
        ("doctrine_library", "slots"), ("function_versions", "sources"),
        # Une app : ses tableaux, champs, agents et sa vignette — des ids, jamais un sub.
        ("apps", "manifest"), ("apps", "thumbnail"),
        ("app_revisions", "manifest"), ("app_revisions", "thumbnail"),
        ("function_versions", "test_report"), ("grants", "constraints"),
        ("jev_jobs", "counters"), ("jev_jobs", "errors"), ("jev_jobs", "params"),
        ("nodes", "data"), ("nodes", "props"), ("org_instructions", "slots"),
        ("org_instruction_revisions", "slots"), ("orgs", "email_settings"),
        ("orgs", "field_filters"), ("orgs", "time_saved_minutes"),
        ("orgs", "default_sharing"),
        ("process_shares", "preview_shape"),
        ("process_shares", "preview_views"), ("project_links", "config"),
        # L'affichage d'une recette : étiquettes, phrases, crédits déclarés par étape.
        ("recipes", "display"),
        ("recipe_versions", "body"), ("recipe_versions", "test_report"),
        ("runner_fleets", "descriptions_outils"), ("runner_fleets", "row_filter"),
        ("runner_fleets", "tools"), ("runner_triggers", "payload_fields"),
        ("runner_triggers", "tools"), ("runner_triggers", "outils_plateforme"),
        # Les webhooks inscrits chez la SOURCE d'un agent (`connect_source`) :
        # identifiants chez le tiers, événement, campagne — personne d'oto.
        ("runner_triggers", "hook_sources"),
        ("tenants", "brand"), ("tenants", "hosts"),
        ("tenants", "link_paths"), ("tenants", "logto_mgmt"),
        ("transcription_jobs", "result"), ("transcription_jobs", "transcript"),
        ("user_api_tokens", "scopes"), ("user_datastores", "schema"),
        # Le profil de la personne : il suit SA ligne (étape 2), rien à réécrire dedans.
        ("user_account_profile", "profile"),
    }
    # Journaux et instantanés : une trace historique garde ce qui a été dit au moment
    # du geste — on ne la réécrit pas (même règle que `connector_loan_events.target`).
    # `portee_elargissements.destinataires` porte des subs nus : qui AURAIT été prévenu.
    journaux = {
        ("tool_calls", "args"), ("run_messages", "content"),
        ("run_messages", "provider_raw"), ("runner_jobs", "payload"),
        ("runner_jobs", "result"), ("runner_jobs", "attempt_errors"),
        ("access_shadow_l7", "sample"), ("credential_disparitions", "agents"),
        # Qui a soigné un agent (`medecin`, ou le sub de l'opérateur) : un journal du geste.
        ("agent_episodes", "reparations"),
        ("portee_elargissements", "destinataires"),
        # Un transfert de solde entre deux comptes fusionnés nomme les deux payeurs.
        ("credit_movements", "meta"),
    }
    allow = sans_personne | journaux

    manquantes = jsonb - set(_JSONB_SUB_COLUMNS) - allow
    assert not manquantes, (
        "colonnes JSONB NON triées pour la fusion de comptes :\n  "
        + "\n  ".join(f"{t}.{c}" for t, c in sorted(manquantes))
        + "\nSi elles portent un sub (`user:<sub>`, un sub nu), les faire suivre "
          "(`_JSONB_SUB_COLUMNS` + étape 3 quater de `repointer_patrimoine`) ; sinon "
          "les ranger ici AVEC leur raison.")
    mortes = (allow | set(_JSONB_SUB_COLUMNS)) - jsonb
    assert not mortes, (
        f"entrées sans colonne JSONB dans le DDL : {sorted(mortes)} — retirer l'entrée "
        "(la colonne a disparu) ou réparer le parse.")
    doubles = allow & set(_JSONB_SUB_COLUMNS)
    assert not doubles, f"suivies ET allowlistées : {sorted(doubles)}"
    # Déclarer une colonne suivie ne suffit pas : une étape qui la réécrit doit la nommer.
    etapes = (credentials_store.suivre_les_prets_recus,
              credentials_store.suivre_les_accords_recus)
    non_ecrites = [f"{t}.{c}" for t, c in _JSONB_SUB_COLUMNS
                   if not any(re.search(rf"\bUPDATE {t} SET\b.*\b{c} = ",
                                        inspect.getsource(e), re.S) for e in etapes)]
    assert not non_ecrites, (
        f"déclarées suivies mais jamais réécrites par l'étape 3 quater "
        f"(`suivre_les_prets_recus`, `suivre_les_accords_recus`) : {non_ecrites}")
    # Et chaque étape doit être APPELÉE par la fusion.
    fusion = inspect.getsource(users.repointer_patrimoine)
    absentes = [e.__name__ for e in etapes if f"credentials_store.{e.__name__}(" not in fusion]
    assert not absentes, f"étapes jamais appelées par `repointer_patrimoine` : {absentes}"
