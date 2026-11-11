"""Le JOURNAL des révisions de ligne — ce que chaque révision a changé, valeurs comprises.

oto#273, jalon M1 : le déclencheur écrit, ce module le pose. La LECTURE (M3) vit dans
`db/historique.py`, qui sert `GET …/rows/{row_id}/history`, `data_row_history` et le
parcours d'une ligne (`docs/datastore.md` §Journal des révisions).

**Pourquoi un déclencheur, comme `rev`.** Pour la même raison que `db/revision.py` : la
base est partagée entre la préproduction et la production, et une écriture de l'ANCIEN
code, pendant la bascule bleu/vert, doit être journalisée aussi. Seul PostgreSQL voit
toutes les faces d'écriture — MCP, REST, import, maintenance, une requête à la main.

**Pourquoi APRÈS, et pas dans le déclencheur de révision.** `datastore_rows_20_revision`
est un `BEFORE UPDATE` dont la condition inclut les colonnes du bail : il avance `rev`
quand seul le bail bouge, ce que le journal ne doit PAS voir. Un `AFTER` a trois
avantages : il lit la ligne FINALE (et donc la `rev` déjà avancée par le `BEFORE`), il
couvre l'INSERT que le déclencheur de révision ignore, et sa condition `WHEN` sur `data`
seule écarte un mouvement de bail SANS appeler la fonction — le coût d'une réservation,
d'un renouvellement ou d'une libération reste exactement celui d'aujourd'hui.

Trois déclencheurs et deux fonctions : la condition d'un déclencheur d'INSERT ne peut
pas nommer `OLD`, donc INSERT et UPDATE ne partagent pas le même `WHEN` ; la suppression
a sa propre fonction (plus bas).

**La forme du diff** : `{champ: {"avant": v, "apres": v}}`, clé par clé de `data`, valeur
ENTIÈRE (couches comprises). Un champ absent d'un côté n'a PAS la clé de ce côté :
`{"apres": 1}` est un ajout, `{"avant": 1}` un retrait, `{"avant": null}` une valeur
`null` qui a changé. Absent, `null` et `[]` restent trois états (`db/revision.py`).
Une insertion porte tous ses champs en `apres`. Une écriture sans effet n'écrit rien.

**La suppression d'une ligne est une révision** (`suppression = true`) : tous ses champs
en `avant`, la `rev` de la ligne au moment où elle part, l'estampille de qui l'a
supprimée. Une colonne plutôt qu'une convention dans `diff` : une mise à jour qui retire
tous les champs a exactement le même diff, et une clé réservée dans `diff` pourrait être
le nom d'une colonne de l'utilisateur. Sa FONCTION est une autre que celle des écritures
(`NOM_FONCTION_SUPPRESSION`) : le corps de `NOM_FONCTION` est reposé à chaque démarrage
par TOUTE version servie sur la base partagée, et une version d'avant la suppression ne
sait pas traiter `TG_OP = 'DELETE'` (sans `NEW`, son `INSERT` lèverait sur `ns_id` NULL,
et chaque suppression de ligne échouerait avec elle).

⚠️ **Un tableau supprimé** part avec ses lignes (`ON DELETE CASCADE` depuis
`user_datastores`) et son historique (même cascade). Le déclencheur de suppression le
voit : le tableau n'existe déjà plus quand il s'exécute, et journaliser là écrirait une
révision orpheline — la clé étrangère la refuserait, et la suppression du tableau avec.
Il n'écrit donc rien quand le tableau est parti : c'est voulu, l'historique d'un tableau
supprimé part avec lui.

**L'estampille (acteur, run, source, geste), M2.** La fonction la lit dans des réglages
de transaction `oto.acteur`, `oto.run_id`, `oto.source`, `oto.geste_id`, que le serveur
pose au point de passage de toute écriture de ligne (`db/estampille.py`, `set_config(…,
true)`, jamais `SET` : une connexion du pool sert tout le monde). Une écriture qui ne
passe pas par le serveur ne pose rien : ses colonnes restent `NULL`.

**L'interrupteur** `OTO_JOURNAL_REVISIONS=off` (défaut `on`) coupe le journal POUR LES
ÉCRITURES DE CE PROCESSUS : le pool ouvre ses connexions avec
`-c oto.journal_revisions=off`, que la fonction lit à chaque ligne. Il n'est pas
posé dans le corps de la fonction, qui est commun à toutes les versions servies sur la
base partagée : un boot de préproduction réglé autrement rallumerait le journal de la
production. Toute autre valeur que `on`/`off` LÈVE — à l'ouverture du pool, donc au boot.

⚠️ **Posé seulement s'il manque** (même prix que `db/revision.py`) : `CREATE TRIGGER`
prend un verrou `SHARE ROW EXCLUSIVE` sur `datastore_rows`, et `CREATE INDEX` un verrou
`SHARE` sur le journal que chaque écriture de ligne alimente. Les reprendre à chaque
démarrage bloquerait les écritures de la production. Changer une CONDITION, c'est un
nouveau nom et le retrait explicite de l'ancien ; les FONCTIONS, elles, sont reposées à
chaque démarrage — sans verrou de table.
"""
from __future__ import annotations

import os

VARIABLE = "OTO_JOURNAL_REVISIONS"
REGLAGE_PG = "oto.journal_revisions"
TABLE = "datastore_row_revisions"
INDEX = "idx_datastore_row_revisions_ligne"
NOM_FONCTION = "datastore_journal_revision"
# `_30_` : après `_20_revision` si un jour l'un d'eux passe du même côté. Les AFTER ROW
# d'une table s'exécutent eux aussi dans l'ordre alphabétique de leur nom.
DECLENCHEUR_INSERT = "datastore_rows_30_journal_insert"
DECLENCHEUR_UPDATE = "datastore_rows_30_journal_update"
DECLENCHEUR_DELETE = "datastore_rows_30_journal_delete"
NOM_FONCTION_SUPPRESSION = "datastore_journal_suppression"
# La marque d'une révision de suppression. Même forme dans le `CREATE TABLE`
# (`db/schema/datastore.py::REVISIONS`), dans la révision `0016` et ci-dessous : une
# base neuve, une base migrée et une base que le démarrage rattrape ont la même colonne.
COLONNE_SUPPRESSION = "suppression"
DDL_COLONNE_SUPPRESSION = (f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "
                           f"{COLONNE_SUPPRESSION} BOOLEAN NOT NULL DEFAULT false")
# QUI A TROUVÉ la valeur : l'outil qui l'a écrite (`jev_rows`, `waterfall_email_act`,
# l'outil qu'un `data_write(via=…)` a prouvé) et le fournisseur derrière elle (`hunter`).
# Nullables, sans défaut : catalogue seul, aucune réécriture du journal. Mêmes formes
# dans le `CREATE TABLE`, dans la révision `20261111_1000_outil_revisions` et ici.
#
# ⚠️ La FONCTION est reposée à chaque démarrage par toute version servie sur la base
# partagée : une version d'avant ces colonnes qui redémarre repose l'ancien corps, qui
# ne les écrit pas — les révisions suivantes portent alors `NULL` (« l'outil n'est pas
# connu »), jusqu'au prochain démarrage d'une version qui les connaît. Jamais d'échec :
# l'ancien corps ne nomme pas ces colonnes, le nouveau ne lit que des réglages.
COLONNES_ATTRIBUTION = ("outil", "fournisseur")
DDL_COLONNES_ATTRIBUTION = tuple(
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {c} TEXT" for c in COLONNES_ATTRIBUTION)
# Le bail (`claimed_*`), `claims`, `rev`, `updated_at` : aucun ne fait une révision.
CONDITION_UPDATE = "OLD.data IS DISTINCT FROM NEW.data"


def journal_coupe() -> bool:
    """`True` si `OTO_JOURNAL_REVISIONS=off`. Absente ou `on` : le journal écrit.
    Illisible : lève — un interrupteur mal orthographié ne doit pas trancher en silence."""
    # Nom LITTÉRAL : l'inventaire (`env_inventory.py`) trouve ses lectures par l'AST.
    lu = os.environ.get("OTO_JOURNAL_REVISIONS", "on")
    brut = lu.strip().lower()
    if brut not in ("on", "off"):
        raise ValueError(
            f"{VARIABLE}={lu!r} : valeur illisible, attendu `on` ou `off`")
    return brut == "off"


RETENTION_DEFAUT = 90


def retention_jours() -> int:
    """La fenêtre de rétention du journal, en jours (`OTO_JOURNAL_REVISIONS_RETENTION_DAYS`,
    défaut 90 — décision du 23/09 dans oto#273). La purge (`db/retention_revisions.py`,
    travail `revisions` de `oto-mcp maintenance`) retire ce qui est plus vieux, sauf
    les révisions d'import d'une ligne qui existe encore.

    Illisible (pas un entier, ou moins d'un jour) : lève. Une rétention mal écrite ne
    doit pas se lire « défaut » en silence, ni « 0 » — qui purgerait tout."""
    # Nom LITTÉRAL : l'inventaire (`env_inventory.py`) trouve ses lectures par l'AST.
    lu = os.environ.get("OTO_JOURNAL_REVISIONS_RETENTION_DAYS", str(RETENTION_DEFAUT))
    try:
        jours = int(lu.strip())
    except ValueError:
        jours = 0
    if jours < 1:
        raise ValueError(
            f"OTO_JOURNAL_REVISIONS_RETENTION_DAYS={lu!r} : valeur illisible, attendu un "
            f"nombre entier de jours (1 ou plus)")
    return jours


# Une seule instruction par branche : le diff se calcule dans le moteur, clé par clé,
# sans boucle PL/pgSQL. `jsonb_object_agg` sur zéro ligne rend NULL : c'est « rien n'a
# changé », et rien ne s'écrit.
#
# JAMAIS BLOQUANT : le journal est une écriture fantôme, il ne fait pas échouer l'écriture
# d'une ligne pour une forme de données inattendue. Un `data` qui n'est pas un objet
# (`jsonb_each`/`jsonb_object_keys` lèveraient) n'écrit rien et émet un `WARNING` nommant
# `ns_id` et `row_id`. Aucune autre erreur n'est avalée : pas d'`EXCEPTION WHEN OTHERS`.
_FONCTION = f"""
CREATE OR REPLACE FUNCTION {NOM_FONCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    d jsonb;
BEGIN
    IF current_setting('{REGLAGE_PG}', true) = 'off' THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(NEW.data) <> 'object'
       OR (TG_OP = 'UPDATE' AND jsonb_typeof(OLD.data) <> 'object') THEN
        RAISE WARNING '{NOM_FONCTION} : data n''est pas un objet JSON '
                      '(ns_id=%, row_id=%, avant=%, apres=%) : révision non journalisée',
                      NEW.ns_id, NEW.row_id,
                      CASE WHEN TG_OP = 'UPDATE' THEN jsonb_typeof(OLD.data) END,
                      jsonb_typeof(NEW.data);
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT COALESCE(jsonb_object_agg(e.k, jsonb_build_object('apres', e.v)),
                        '{{}}'::jsonb)
          INTO d FROM jsonb_each(NEW.data) AS e(k, v);
    ELSE
        SELECT jsonb_object_agg(c.k,
                   CASE WHEN OLD.data ? c.k
                        THEN jsonb_build_object('avant', OLD.data -> c.k)
                        ELSE '{{}}'::jsonb END
                || CASE WHEN NEW.data ? c.k
                        THEN jsonb_build_object('apres', NEW.data -> c.k)
                        ELSE '{{}}'::jsonb END)
          INTO d
          FROM (SELECT jsonb_object_keys(OLD.data)
                UNION SELECT jsonb_object_keys(NEW.data)) AS c(k)
         WHERE (OLD.data -> c.k) IS DISTINCT FROM (NEW.data -> c.k);
        IF d IS NULL THEN
            RETURN NULL;
        END IF;
    END IF;
    INSERT INTO {TABLE} (ns_id, row_id, rev, diff, acteur, run_id, source, geste_id,
                         outil, fournisseur)
    VALUES (NEW.ns_id, NEW.row_id, NEW.rev, d,
            NULLIF(current_setting('oto.acteur', true), ''),
            NULLIF(current_setting('oto.run_id', true), ''),
            NULLIF(current_setting('oto.source', true), ''),
            NULLIF(current_setting('oto.geste_id', true), ''),
            NULLIF(current_setting('oto.outil', true), ''),
            NULLIF(current_setting('oto.fournisseur', true), ''));
    RETURN NULL;
END $$"""


# La suppression : tous les champs de la ligne en `avant`, jamais de condition `WHEN`
# (une ligne vide qui part est une suppression aussi, son diff est `{}`). Mêmes règles
# que `_FONCTION` : l'interrupteur, et un `data` qui n'est pas un objet n'écrit rien et
# le dit. La garde du tableau d'abord : pendant la cascade d'un `DELETE` de
# `user_datastores`, la ligne du tableau est déjà supprimée pour cette requête.
_FONCTION_SUPPRESSION = f"""
CREATE OR REPLACE FUNCTION {NOM_FONCTION_SUPPRESSION}() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    d jsonb;
BEGIN
    IF current_setting('{REGLAGE_PG}', true) = 'off' THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM user_datastores WHERE id = OLD.ns_id) THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(OLD.data) <> 'object' THEN
        RAISE WARNING '{NOM_FONCTION_SUPPRESSION} : data n''est pas un objet JSON '
                      '(ns_id=%, row_id=%, avant=%) : suppression non journalisée',
                      OLD.ns_id, OLD.row_id, jsonb_typeof(OLD.data);
        RETURN NULL;
    END IF;
    SELECT COALESCE(jsonb_object_agg(e.k, jsonb_build_object('avant', e.v)),
                    '{{}}'::jsonb)
      INTO d FROM jsonb_each(OLD.data) AS e(k, v);
    INSERT INTO {TABLE} (ns_id, row_id, rev, diff, {COLONNE_SUPPRESSION},
                         acteur, run_id, source, geste_id, outil)
    VALUES (OLD.ns_id, OLD.row_id, OLD.rev, d, true,
            NULLIF(current_setting('oto.acteur', true), ''),
            NULLIF(current_setting('oto.run_id', true), ''),
            NULLIF(current_setting('oto.source', true), ''),
            NULLIF(current_setting('oto.geste_id', true), ''),
            NULLIF(current_setting('oto.outil', true), ''));
    RETURN NULL;
END $$"""


def poser_journal_des_revisions(conn) -> list[str]:
    """Index, colonne `suppression`, fonctions, déclencheurs — sur la connexion DDL du boot, APRÈS `_SCHEMA`
    (qui crée la table) et après `revision.poser_revision_de_ligne` (qui crée `rev`).
    Rend les noms de ce qui vient d'être créé (vide au boot ordinaire)."""
    poses: list[str] = []
    # Pas d'import de `_init._index_absent` : ce module est importé PAR `_init.py`.
    if conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s",
                    (INDEX,)).fetchone() is None:
        # Non UNIQUE : une ligne supprimée puis recréée sous le même `row_id` repart
        # de `rev` 0, et ses révisions passées restent.
        conn.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} "
                     f"ON {TABLE} (ns_id, row_id, rev)")
        poses.append(INDEX)
    # La colonne AVANT la fonction qui l'écrit. Sur une base à jour, le catalogue le
    # dit et l'`ALTER` n'est pas envoyé : il prendrait sinon un `AccessExclusiveLock`
    # sur le journal, que chaque écriture de ligne alimente.
    if conn.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s",
                    (TABLE, COLONNE_SUPPRESSION)).fetchone() is None:
        conn.execute(DDL_COLONNE_SUPPRESSION)
        poses.append(f"{TABLE}.{COLONNE_SUPPRESSION}")
    # Même garde de catalogue pour l'attribution : l'`ALTER` n'est envoyé que s'il manque.
    for colonne, ddl in zip(COLONNES_ATTRIBUTION, DDL_COLONNES_ATTRIBUTION):
        if conn.execute("SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = %s",
                        (TABLE, colonne)).fetchone() is None:
            conn.execute(ddl)
            poses.append(f"{TABLE}.{colonne}")
    conn.execute(_FONCTION)
    conn.execute(_FONCTION_SUPPRESSION)
    for nom, quand, condition, fonction in (
            (DECLENCHEUR_INSERT, "INSERT", None, NOM_FONCTION),
            (DECLENCHEUR_UPDATE, "UPDATE", CONDITION_UPDATE, NOM_FONCTION),
            (DECLENCHEUR_DELETE, "DELETE", None, NOM_FONCTION_SUPPRESSION)):
        if conn.execute(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgrelid = 'datastore_rows'::regclass AND tgname = %s",
                (nom,)).fetchone():
            continue
        when = f"WHEN ({condition}) " if condition else ""
        conn.execute(
            f"CREATE TRIGGER {nom} AFTER {quand} ON datastore_rows "
            f"FOR EACH ROW {when}EXECUTE FUNCTION {fonction}()")
        poses.append(nom)
    return poses
