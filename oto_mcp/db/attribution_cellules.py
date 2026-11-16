"""QUI A TROUVÉ la valeur que porte chaque case — une ligne par case TROUVÉE par un outil.

`datastore_cell_sources (ns_id, row_id, champ)` → l'outil (`jev_rows`,
`waterfall_email_act`, l'outil qu'un `data_write(via=…)` a prouvé), le fournisseur
(l'outil de l'étape gagnante d'une cascade) et la face. C'est ce que lit l'en-tête d'une
colonne (`GET …/column-sources`).

**Pourquoi une table et pas le journal des révisions.** Le journal dit tout, mais il se
lit mal pour ça : (1) sa purge de 90 jours GARDE les révisions d'import d'une ligne
vivante — une case importée puis enrichie se lirait « import » une fois la révision
d'enrichissement purgée, une étiquette FAUSSE ; (2) son coût de lecture suit l'HISTOIRE
du tableau, pas sa taille. Ici : une ligne par case attribuée, jamais purgée, lue par sa
clé primaire.

**Ce qui l'écrit : un déclencheur à part** (`datastore_rows_40_attribution_*`), pas le
corps du journal. Le corps du journal est reposé à chaque démarrage par TOUTE version
servie sur la base partagée : une ancienne version qui redémarre poserait un corps qui
n'entretient pas cette table, et une case réécrite à la main garderait « Hunter ». Ce
déclencheur-ci, aucune ancienne version ne le connaît : elle ne le touche pas.

**La règle, case par case**, sur les colonnes dont la VALEUR change (le jumeau SQL
d'`unwrap`, `paths._valeur_jsonb` — une écriture qui ne touche qu'une couche,
`{"email": {"comment": …}}`, ne change pas la valeur et ne déplace rien) :
- valeur tenue (ni absente, ni `null`) ET écriture ATTRIBUÉE (un fournisseur, ou un
  outil qui n'est pas de la plateforme : `data_*`, `oto_*`, `run_*`, `feedback`) →
  la case est posée / remplacée ;
- sinon (à la main, import, agent qui n'a pas nommé l'outil, valeur effacée) → la case
  est RETIRÉE : ce qui la tient n'est plus ce qui l'avait trouvée.
Une ligne supprimée emporte ses cases ; un tableau supprimé aussi (clé étrangère).

Il lit l'estampille (`oto.outil`, `oto.fournisseur`, `oto.source`) ; il ne suit PAS
l'interrupteur du journal (une réécriture faite journal coupé doit retirer l'attribution). Jamais bloquant : pas d'`EXCEPTION`
avalée, mais rien qui puisse lever sur une donnée — un `data` qui n'est pas un objet
n'écrit rien.

⚠️ Couvre les écritures faites APRÈS sa pose : une case écrite avant n'a pas de ligne
ici, elle compte comme « non attribuée ».
"""
from __future__ import annotations

from typing import Optional

from .paths import _valeur_jsonb

TABLE = "datastore_cell_sources"
NOM_FONCTION = "datastore_attribution_cellules"
DECLENCHEUR_INSERT = "datastore_rows_40_attribution_insert"
DECLENCHEUR_UPDATE = "datastore_rows_40_attribution_update"
DECLENCHEUR_DELETE = "datastore_rows_40_attribution_delete"

#: Ce qui n'est jamais une trouvaille : la plateforme elle-même (même règle que
#: `datastore/via.py::_PLATEFORME` et que le client).
PLATEFORME_SQL = "'^(data|oto|run)_|^feedback$'"

DDL_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    champ TEXT NOT NULL,
    outil TEXT,
    fournisseur TEXT,
    source TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ns_id, row_id, champ)
)"""

_AVANT = _valeur_jsonb("(OLD.data -> c.k)")
_APRES = _valeur_jsonb("(NEW.data -> c.k)")

FONCTION = f"""
CREATE OR REPLACE FUNCTION {NOM_FONCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    outil_ text := NULLIF(current_setting('oto.outil', true), '');
    fournisseur_ text := NULLIF(current_setting('oto.fournisseur', true), '');
    source_ text := NULLIF(current_setting('oto.source', true), '');
    attribuee boolean;
    changes text[];
    tenues text[];
BEGIN
    -- PAS d'interrupteur ici (`oto.journal_revisions=off` coupe le journal, pas
    -- ceci) : une réécriture faite journal coupé doit RETIRER l'attribution qu'elle
    -- remplace, sinon « Hunter » resterait sur une valeur écrite autrement.
    IF TG_OP = 'DELETE' THEN
        DELETE FROM {TABLE} WHERE ns_id = OLD.ns_id AND row_id = OLD.row_id;
        RETURN NULL;
    END IF;
    IF jsonb_typeof(NEW.data) <> 'object'
       OR (TG_OP = 'UPDATE' AND jsonb_typeof(OLD.data) <> 'object') THEN
        RETURN NULL;
    END IF;
    attribuee := fournisseur_ IS NOT NULL
                 OR (outil_ IS NOT NULL AND outil_ !~ {PLATEFORME_SQL});
    IF TG_OP = 'INSERT' THEN
        IF NOT attribuee THEN
            RETURN NULL;   -- une ligne neuve n'a rien à retirer
        END IF;
        SELECT array_agg(c.k) INTO tenues
          FROM jsonb_object_keys(NEW.data) AS c(k)
         WHERE {_APRES} IS NOT NULL AND {_APRES} <> 'null'::jsonb;
        changes := tenues;
    ELSE
        -- Le chemin de presque toutes les écritures : non attribuée, sur une ligne dont
        -- aucune case n'est attribuée — rien à poser, rien à retirer, une sonde d'index.
        IF NOT attribuee AND NOT EXISTS (
                SELECT 1 FROM {TABLE} WHERE ns_id = NEW.ns_id AND row_id = NEW.row_id) THEN
            RETURN NULL;
        END IF;
        SELECT array_agg(c.k) INTO changes
          FROM (SELECT jsonb_object_keys(OLD.data)
                UNION SELECT jsonb_object_keys(NEW.data)) AS c(k)
         WHERE {_AVANT} IS DISTINCT FROM {_APRES};
        IF changes IS NULL THEN
            RETURN NULL;   -- couches seules : la valeur n'a pas bougé
        END IF;
        IF attribuee THEN
            SELECT array_agg(c.k) INTO tenues
              FROM unnest(changes) AS c(k)
             WHERE {_APRES} IS NOT NULL AND {_APRES} <> 'null'::jsonb;
        END IF;
    END IF;
    IF changes IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        DELETE FROM {TABLE}
         WHERE ns_id = NEW.ns_id AND row_id = NEW.row_id
           AND champ = ANY (changes)
           AND NOT (champ = ANY (COALESCE(tenues, ARRAY[]::text[])));
    END IF;
    IF tenues IS NOT NULL THEN
        INSERT INTO {TABLE} (ns_id, row_id, champ, outil, fournisseur, source, at)
        SELECT NEW.ns_id, NEW.row_id, t.k, outil_, fournisseur_, source_, now()
          FROM unnest(tenues) AS t(k)
        ON CONFLICT (ns_id, row_id, champ) DO UPDATE
           SET outil = EXCLUDED.outil, fournisseur = EXCLUDED.fournisseur,
               source = EXCLUDED.source, at = EXCLUDED.at;
    END IF;
    RETURN NULL;
END $$"""

_DECLENCHEURS = (
    (DECLENCHEUR_INSERT, "INSERT", None),
    (DECLENCHEUR_UPDATE, "UPDATE", "OLD.data IS DISTINCT FROM NEW.data"),
    (DECLENCHEUR_DELETE, "DELETE", None),
)


def ddl_declencheur(nom: str, quand: str, condition: Optional[str]) -> str:
    when = f"WHEN ({condition}) " if condition else ""
    return (f"CREATE TRIGGER {nom} AFTER {quand} ON datastore_rows "
            f"FOR EACH ROW {when}EXECUTE FUNCTION {NOM_FONCTION}()")


def ddl_declencheur_si_absent(nom: str, quand: str, condition: Optional[str]) -> str:
    """Le `CREATE TRIGGER` sous garde de catalogue, en une instruction (migration) :
    rien n'est envoyé quand il existe — ni `DROP` (verrou `ACCESS EXCLUSIVE`), ni
    `CREATE`."""
    ddl = ddl_declencheur(nom, quand, condition).replace("'", "''")
    return (f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = "
            f"'datastore_rows'::regclass AND tgname = '{nom}') THEN EXECUTE '{ddl}'; "
            f"END IF; END $$")


def poser_attribution(conn) -> list[str]:
    """Table, fonction, déclencheurs — au démarrage, APRÈS le journal des révisions.
    Table et déclencheurs posés seulement s'ils MANQUENT (un `CREATE TRIGGER` prend un
    verrou `SHARE ROW EXCLUSIVE` sur `datastore_rows`) ; la fonction est reposée, sans
    verrou de table. Rend les noms de ce qui vient d'être créé."""
    poses: list[str] = []
    if conn.execute("SELECT to_regclass(%s) AS t", (TABLE,)).fetchone()["t"] is None:
        conn.execute(DDL_TABLE)
        poses.append(TABLE)
    conn.execute(FONCTION)
    for nom, quand, condition in _DECLENCHEURS:
        if conn.execute(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgrelid = 'datastore_rows'::regclass AND tgname = %s",
                (nom,)).fetchone():
            continue
        conn.execute(ddl_declencheur(nom, quand, condition))
        poses.append(nom)
    return poses
