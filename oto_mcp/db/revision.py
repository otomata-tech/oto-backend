"""La RÉVISION d'une ligne de tableau — un compteur que PostgreSQL avance, pas le code.

**Pourquoi elle existe (12/09/2026).** Mesuré sur l'arbre servi, base jetable : deux
patchs par `id` sur deux colonnes DIFFÉRENTES, partis ensemble, perdaient l'un des deux
dans 100 % des cas — lecture dans une connexion, fusion en Python, JSON entier réécrit
dans une autre. Router le patch sous le verrou de ligne ferme ce cas-là. Il ne ferme pas
celui de l'écrivain qui LIT, recalcule (un journal renvoyé entier, un statut choisi
d'après le statut en place) puis écrit : le verrou ne protège pas une lecture faite par
le client. Pour lui, une précondition — « écris seulement si la ligne est encore celle
que j'ai lue » — et donc un numéro de version de la ligne.

**Pourquoi un DÉCLENCHEUR, et pas `rev = rev + 1` dans l'UPDATE du code.** Préproduction
et production partagent la base, et la bascule bleu/vert sert deux versions pendant
120 s. Une écriture de l'ANCIEN code, qui ne connaît pas `rev`, doit aussi la faire
avancer — sinon une précondition passe par-dessus une écriture qu'elle n'a pas vue. Seul
PostgreSQL voit toutes les écritures, quel que soit le code qui les émet.

C'est le **premier déclencheur du dépôt**. L'ADR 0065 (migrations) n'en dit rien ; le
plan l'a jugé justifié pour la raison ci-dessus.

**Ce qui avance la révision** : un changement de `data` (valeur, couche, clé ajoutée ou
retirée — absent → `null` → `[]` sont trois états), de `claimed_by`, `claimed_run` ou
`claimed_until`. Réserver, libérer et RENOUVELER un bail la font donc avancer
(`claimed_until` inclus par arbitrage du plan). **Ce qui ne l'avance pas** : une
écriture sans effet, un JSON seulement réordonné (`jsonb` normalise l'ordre des clés),
`updated_at` seul, `claims`, `abandon_reason`, `embed_dirty`, `search_vec`.

⚠️ **Ordre des déclencheurs.** PostgreSQL exécute les déclencheurs `BEFORE ROW` d'une
même table dans l'ordre ALPHABÉTIQUE de leur nom : d'où le préfixe `20_`, qui laisse
passer avant lui un `datastore_rows_10_bail` éventuel.

⚠️ **Posé seulement s'il manque.** `CREATE TRIGGER` prend un verrou `SHARE ROW
EXCLUSIVE` sur la table : le reprendre à chaque démarrage bloquerait les écritures de la
production, sur la base partagée, à chaque boot de préproduction. Le prix : changer la
CONDITION ne se propage pas d'elle-même — c'est un nouveau nom, et le retrait explicite
de l'ancien. La FONCTION, elle, est reposée à chaque démarrage : `CREATE OR REPLACE
FUNCTION` ne verrouille pas la table.
"""
from __future__ import annotations

NOM_DECLENCHEUR = "datastore_rows_20_revision"
NOM_FONCTION = "datastore_revision_avance"

# Lue AU MOMENT de la pose (attribut de module) : la condition est la seule chose qui
# décide de ce que la précondition voit.
CONDITION = ("OLD.data IS DISTINCT FROM NEW.data "
             "OR OLD.claimed_by IS DISTINCT FROM NEW.claimed_by "
             "OR OLD.claimed_run IS DISTINCT FROM NEW.claimed_run "
             "OR OLD.claimed_until IS DISTINCT FROM NEW.claimed_until")


def poser_revision_de_ligne(conn) -> bool:
    """Colonne, fonction, déclencheur — sur la connexion DDL du boot (`lock_timeout`
    borné, advisory lock pris). Rend True si le déclencheur vient d'être créé."""
    # `DEFAULT` constant sur table existante : rangé au catalogue (PG >= 11), aucune
    # réécriture — et 0 est bien la révision d'une ligne jamais modifiée depuis.
    conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS "
                 "rev BIGINT NOT NULL DEFAULT 0")
    conn.execute(
        f"CREATE OR REPLACE FUNCTION {NOM_FONCTION}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN NEW.rev := OLD.rev + 1; RETURN NEW; END $$")
    present = conn.execute(
        "SELECT 1 AS ok FROM pg_trigger "
        "WHERE tgrelid = 'datastore_rows'::regclass AND tgname = %s",
        (NOM_DECLENCHEUR,)).fetchone()
    if present:
        return False
    conn.execute(
        f"CREATE TRIGGER {NOM_DECLENCHEUR} BEFORE UPDATE ON datastore_rows "
        f"FOR EACH ROW WHEN ({CONDITION}) EXECUTE FUNCTION {NOM_FONCTION}()")
    return True
