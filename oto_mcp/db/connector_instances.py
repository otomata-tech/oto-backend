"""L'instance de connecteur comme OBJET — SQL seul (blueprint ADR 0053-D9, lot L6).

La table `connector_instances` est posée par `db/schema/connectors.py` ; ce module
est son unique lecteur/écrivain. Il ne porte AUCUNE politique : ce qui est visible,
ce qui résout, ce qui est partagé continue de se décider ailleurs (`access/`,
`grants_chain`, `capabilities/connectors/instances`). Ici, des requêtes.

⚠️ **Trois voisins à ne pas confondre** : `connector_grants` (comptes opérés, #55),
`grants` (le droit d'UTILISER une ressource, chaîne matérialisée 0053) et
`connector_credentials` (le coffre, où vit le secret). `connector_instances` est
l'OBJET qu'on possède, qu'on désigne et qu'on partage ; le credential en est un
attribut, pas l'identité.

**Le lien vers le coffre est un quadruplet, pas un id** — et ce n'est pas un pis
aller : le coffre n'a pas de clé de substitution, sa PK EST
`(entity_type, entity_id, connector, account)`, et l'AAD du chiffrement en dérive
(`credentials_store._aad`). Les quatre colonnes portées ici sont donc le pointeur
lui-même, à l'octet près ; c'est aussi ce qui fait qu'une instance SANS ligne de
coffre reste représentable (instance `api` qui ne fige qu'une `base_url`,
sous-instance qui ne pose qu'une détermination).

⚠️ **Aucune fonction d'ici n'est sur un chemin de résolution.** `walk_cascade` et
`resolve_credential` ne connaissent pas cette table (lot L7). La seule lecture
servie aujourd'hui est la PROJECTION `GET /api/me/connector-instances`, qui n'en
tire qu'un identifiant d'affichage.

**Pièce 2 — l'instance naît à la POSE, plus au boot.** Les trois primitives
d'écriture ci-dessous (`name_vault_row`, `revoke_instances_for_vault_rows`,
`move_instance_to_account`) sont appelées par le coffre lui-même, DANS sa
transaction : la ligne du secret et son instance commitent ou rollbackent ensemble.
Elles ne sont appelées de nulle part ailleurs — le point d'accroche est l'entonnoir
d'écriture du coffre (`credentials_store._upsert` / `._delete`), pas les surfaces :
il n'existe qu'UN `INSERT` et qu'UN `DELETE` sur `connector_credentials` dans tout
le dépôt, et toutes les surfaces déclaratives (clé membre, org, groupe, plateforme,
session navigateur, OAuth) y aboutissent. `name_vault_rows_as_instances` reste, et
devient un FILET : après ce lot il ne nomme plus rien (0 ligne, mesuré en preprod).
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from ._conn import _connect

logger = logging.getLogger(__name__)

# Le vocabulaire de propriétaire ACCEPTÉ par la table (CHECK nommé côté DDL).
# `tenant` y est prévu et INERTE — l'entité `tenant` du coffre est le lot L-clés.
OWNER_KINDS = ("platform", "tenant", "org", "group", "member", "user")

# La même liste, en littéral SQL. Construite explicitement et pas par le `repr` du
# tuple : à un seul élément, `repr` rend `('x',)` — une virgule finale que PostgreSQL
# refuse. Le vocabulaire est une CONSTANTE de module, jamais une entrée d'appelant.
_OWNER_KINDS_SQL = "(" + ", ".join(f"'{k}'" for k in OWNER_KINDS) + ")"

_INSTANCE_COLS = ("id, connector, owner_type, owner_id, account, label, config, "
                  "visibility, parent_id, created_at, revoked_at, revoked_reason")

# Les motifs d'archivage POSÉS par ce dépôt. Constante et pas CHECK en base : le
# vocabulaire n'est fermé que par le code qui écrit, et un CHECK ferait une migration
# du prochain motif.
REVOKED_CREDENTIAL_REMOVED = "credential_removed"      # le cas normal : la clé a été retirée
REVOKED_RENAMED_ONTO_EXISTING = "renamed_onto_existing"  # renommage vers une instance déjà vivante
REVOKED_VAULT_ROW_MISSING = "vault_row_missing"        # maintenance : orpheline d'avant la pièce 2


def instance_id_for_vault_row(owner_type: str, owner_id: str, connector: str,
                              account: str = "", conn=None) -> Optional[int]:
    """Id de l'instance VIVANTE nommant cette ligne de coffre, ou None.

    `account` suit la convention du coffre : `''` = mono-compte, jamais NULL."""
    sql = ("SELECT id FROM connector_instances WHERE owner_type = %s AND owner_id = %s "
           "AND connector = %s AND account = %s AND revoked_at IS NULL")
    params = (owner_type, str(owner_id), connector, account or "")
    if conn is not None:
        row = conn.execute(sql, params).fetchone()
    else:
        with _connect() as c:
            row = c.execute(sql, params).fetchone()
    return row["id"] if row else None


def most_recent_revocation(owner_type: str, owner_id: str, connector: str) -> Optional[dict]:
    """La révocation la plus récente pour ce (propriétaire, connecteur), tout compte
    confondu — ou None si aucune instance de ce connecteur n'a jamais existé ici.

    oto#42, entrée 11 du lot 1 : « aucun credential configuré » ne dit pas si le
    connecteur n'a JAMAIS été posé ou s'il l'a été puis RETIRÉ (`revoked_at`) — deux
    faits différents, un seul message. Lecture seule, hors chemin de résolution
    (cf. en-tête de module : `walk_cascade`/`resolve_credential` ne DÉCIDENT toujours
    rien d'ici) — c'est un hint construit APRÈS l'échec, par `rbac.<hint>`, jamais un
    critère d'aiguillage."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT revoked_at, revoked_reason FROM connector_instances "
            "WHERE owner_type = %s AND owner_id = %s AND connector = %s "
            "  AND revoked_at IS NOT NULL "
            "ORDER BY revoked_at DESC LIMIT 1",
            (owner_type, str(owner_id), connector),
        ).fetchone()
    return dict(row) if row else None


def instances_for_vault_rows(
        keys: Sequence[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], dict]:
    """Résolution EN LOT des quadruplets → `{id, visibility}`, pour une projection qui
    en tient des dizaines (`GET /api/me/connector-instances`).

    **Une seule requête**, jamais une par instance : la projection tourne sur un
    serveur MONO-LOOP, et N lookups par PK y coûtent N allers-retours vers une base
    managée DISTANTE — le mode de panne que `docs/event-loop-perf.md` documente. Les
    couples sont comparés colonne à colonne (`(a,b,c,d) IN ((…),(…))`), servis par
    `idx_connector_instances_vault`.

    Rend `visibility` avec l'id parce que les deux se lisent au même endroit et
    servent la même réponse : la SURCHARGE du propriétaire (R9) entre dans le calcul
    de `visible_to`, et aller la chercher séparément doublerait la requête pour une
    colonne de la même ligne."""
    uniques = sorted({(k[0], str(k[1]), k[2], k[3] or "") for k in keys})
    if not uniques:
        return {}
    clause = ", ".join(["(%s, %s, %s, %s)"] * len(uniques))
    params: list = []
    for k in uniques:
        params += list(k)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, owner_type, owner_id, connector, account, visibility "
            "FROM connector_instances "
            "WHERE revoked_at IS NULL AND (owner_type, owner_id, connector, account) "
            f"IN ({clause})", params).fetchall()
    return {(r["owner_type"], r["owner_id"], r["connector"], r["account"]):
            {"id": r["id"], "visibility": r["visibility"]} for r in rows}


def instance_ids_for_vault_rows(
        keys: Sequence[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], int]:
    """`{quadruplet: id}` — la forme d'origine (pièce 1), DÉRIVÉE de la précédente et
    pas une seconde requête. Conservée parce que la surface `db.<fn>` est un cliquet :
    on y ajoute, on n'en retire jamais."""
    return {k: v["id"] for k, v in instances_for_vault_rows(keys).items()}


def instance_by_id(instance_id: int, conn=None) -> Optional[dict]:
    """L'instance et son quadruplet de coffre — la résolution d'un `inst:{id}`.

    Rend AUSSI les archivées (`revoked_at` non nul) : l'appelant tranche. Un binding
    ou un grant peut désigner une instance retirée, et « elle a été retirée » n'est
    pas le même verdict que « elle n'a jamais existé »."""
    sql = f"SELECT {_INSTANCE_COLS} FROM connector_instances WHERE id = %s"
    if conn is not None:
        row = conn.execute(sql, (instance_id,)).fetchone()
    else:
        with _connect() as c:
            row = c.execute(sql, (instance_id,)).fetchone()
    return dict(row) if row else None


def connector_instance_counts(conn=None) -> dict[str, int]:
    """Compte des instances VIVANTES par `owner_type` — le relevé que le boot
    journalise, et la mesure que la revue lit."""
    sql = ("SELECT owner_type, COUNT(*) AS n FROM connector_instances "
           "WHERE revoked_at IS NULL GROUP BY owner_type ORDER BY owner_type")
    if conn is not None:
        rows = conn.execute(sql).fetchall()
    else:
        with _connect() as c:
            rows = c.execute(sql).fetchall()
    return {r["owner_type"]: r["n"] for r in rows}


# ── L'écriture : naître, mourir, suivre ────────────────────────────────────────
#
# Trois primitives, appelées par le COFFRE et par lui seul, dans SA transaction.
# Aucune ne décide quoi que ce soit : elles enregistrent qu'une ligne de coffre vient
# d'apparaître, de disparaître, ou de changer de compte. La politique (qui voit, qui
# résout, qui partage) continue de vivre ailleurs.


class OwnerKindUnknown(ValueError):
    """Le type de propriétaire n'est pas au vocabulaire de la table.

    Refus NOMMÉ, et pas un repli : nommer une ligne de coffre est désormais une partie
    de sa pose, donc un type inconnu doit faire échouer la pose ENTIÈRE plutôt que de
    laisser naître une clé que rien ne désigne. Sans cette classe, l'appelant se
    prendrait une violation de CHECK PostgreSQL — vraie, mais illisible, et qui ne dit
    pas quelle valeur corriger. Mesuré en prod avant la pièce 1 : **zéro** ligne de
    coffre hors vocabulaire (133 lignes énumérées) ; ce refus garde le zéro."""


def name_vault_row(conn, owner_type: str, owner_id: str, connector: str,
                   account: str = "") -> None:
    """Donne son instance à une ligne de coffre qu'on vient d'écrire. Idempotent.

    **Une seule instruction**, et c'est ce qui la rend sûre : un `SELECT` suivi d'un
    `INSERT` laisserait la fenêtre où deux poses concurrentes de la même clé créent
    deux instances. L'inférence porte sur l'index unique PARTIEL — d'où le prédicat
    répété dans le `ON CONFLICT` : sans lui PostgreSQL ne sait pas quel index viser et
    refuse. Le partiel est voulu : une instance ARCHIVÉE n'interdit pas d'en poser une
    neuve sur la même ligne (reposer une clé retirée doit donner un id NEUF, jamais
    ressusciter l'histoire close d'un partage qu'on avait coupé).

    Un aller-retour SQL de plus par pose de clé, dans la transaction existante — une
    pose n'est pas un chemin chaud (la PROJECTION, elle, en lit des dizaines et reste
    à une requête)."""
    if owner_type not in OWNER_KINDS:
        raise OwnerKindUnknown(
            f"type de propriétaire d'instance inconnu : {owner_type!r} "
            f"(attendu {list(OWNER_KINDS)}) — la ligne de coffre ne peut pas être "
            "nommée, donc elle ne se pose pas.")
    conn.execute(
        "INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (owner_type, owner_id, connector, account) "
        "WHERE revoked_at IS NULL DO NOTHING",
        (connector, owner_type, str(owner_id), account or ""))


def revoke_instances_for_vault_rows(conn, owner_type: str, owner_id: str,
                                    connector: "str | None" = None,
                                    account: "str | None" = None,
                                    reason: str = REVOKED_CREDENTIAL_REMOVED) -> int:
    """Archive les instances VIVANTES qui nomment ces lignes de coffre. Jamais un
    DELETE (0053-D7 : un binding, une arête ou une consommation qui les désignent
    doivent pouvoir les relire après le retrait).

    Le filtre se resserre de gauche à droite : sans `connector`, toutes les instances
    de l'entité (suppression d'un groupe) ; avec `connector` et sans `account`, tous
    les comptes de ce connecteur (déconnexion de tous les comptes Google). C'est ce
    qui permet aux trois retraits en MASSE du dépôt de passer par ici plutôt que par
    un `DELETE` brut qui laisserait les instances vivantes derrière lui.

    Inconditionnel et idempotent : appelé sur une ligne déjà absente, il ne fait rien
    — et appelé sur une instance vivante sans ligne de coffre, il RÉPARE l'écart."""
    sql = ("UPDATE connector_instances SET revoked_at = NOW(), revoked_reason = %s "
           "WHERE owner_type = %s AND owner_id = %s AND revoked_at IS NULL")
    params: list = [reason, owner_type, str(owner_id)]
    if connector is not None:
        sql += " AND connector = %s"
        params.append(connector)
        if account is not None:
            sql += " AND account = %s"
            params.append(account or "")
    return conn.execute(sql, tuple(params)).rowcount or 0


def move_instance_to_account(conn, owner_type: str, owner_id: str, connector: str,
                             old_account: str, new_account: str) -> "tuple[bool, int | None, int | None]":
    """Fait SUIVRE l'instance à un renommage de compte — **son id ne change pas**.

    C'est la raison d'être du lot exercée sur le seul geste qui DÉPLACE une ligne de
    coffre : `rename_account` rechiffre (l'`account` entre dans l'AAD), donc il écrit
    une ligne neuve et supprime l'ancienne. Laissé aux seuls crochets de pose et de
    retrait, il tuerait l'instance et en ferait naître une autre — soit exactement le
    ref composé qu'on remplace. D'où l'appel EXPLICITE, **avant** l'écriture du
    coffre : après lui, le crochet de pose trouve l'instance déjà vivante à l'arrivée
    (il ne fait rien) et le crochet de retrait n'en trouve plus au départ (il ne fait
    rien non plus).

    Rend `(déplacée, id_archivé, id_conservé)`. Le cas où une instance vivante existe
    DÉJÀ à l'arrivée est une réparation d'écart (une instance sans sa ligne de
    coffre) : l'arrivée gagne, le départ s'archive — et le geste le DIT, il ne
    l'avale pas."""
    arrivee = instance_id_for_vault_row(owner_type, owner_id, connector,
                                        new_account, conn=conn)
    depart = instance_id_for_vault_row(owner_type, owner_id, connector,
                                       old_account, conn=conn)
    if arrivee is not None:
        if depart is not None and depart != arrivee:
            conn.execute("UPDATE connector_instances SET revoked_at = NOW(), "
                         "revoked_reason = %s WHERE id = %s",
                         (REVOKED_RENAMED_ONTO_EXISTING, depart))
            logger.warning(
                "L6 instances: renommage %s/%s %s '%s' -> '%s' — instance %d ARCHIVÉE "
                "au profit de %d, qui existait déjà (écart réparé : une instance "
                "vivante sans sa ligne de coffre)",
                owner_type, owner_id, connector, old_account, new_account,
                depart, arrivee)
            return (False, depart, arrivee)
        return (False, None, arrivee)
    if depart is None:
        return (False, None, None)
    conn.execute("UPDATE connector_instances SET account = %s WHERE id = %s",
                 (new_account or "", depart))
    return (True, None, depart)



def move_instance_to_owner(conn, owner_type: str, old_owner_id: str,
                           new_owner_id: str, connector: str,
                           account: str = "") -> "int | None":
    """Fait SUIVRE l'instance vivante d'une ligne de coffre qui change de
    PROPRIÉTAIRE — **son id ne change pas** (oto-backend#439).

    Pendant de `move_instance_to_account` pour l'autre segment du quadruplet : une
    fusion de comptes rechiffre les clés personnelles de l'ancien identifiant sous le
    nouveau (`credentials_store.rekey_personal_credentials`). Ne pas la déplacer
    laisserait l'instance désigner une ligne partie — une orpheline — et casserait
    tout binding, arête ou consommation qui la nomme par `inst:{id}`.

    L'appelant a vérifié que l'arrivée est LIBRE (ni ligne de coffre, ni instance
    vivante) : l'index unique partiel lèverait sinon, et c'est le bon comportement —
    un écart pareil ne se répare pas au fond d'une fusion. Rend l'id déplacé, ou
    `None` si la ligne n'avait pas d'instance (le crochet de pose n'est pas appelé
    ici : une ligne sans instance le reste, le backfill de boot la nommera)."""
    row = conn.execute(
        "UPDATE connector_instances SET owner_id = %s "
        "WHERE owner_type = %s AND owner_id = %s AND connector = %s AND account = %s "
        "AND revoked_at IS NULL RETURNING id",
        (str(new_owner_id), owner_type, str(old_owner_id), connector,
         account or "")).fetchone()
    return row["id"] if row else None

# ── La maintenance : l'ORPHELINE ──────────────────────────────────────────────
#
# Une instance vivante SANS ligne de coffre est un objet qui désigne une clé qui
# n'existe pas — et qu'un binding, une arête ou une consommation peuvent nommer. Elles
# ne naissent plus depuis la pièce 2 (retirer une clé archive son instance dans la même
# transaction), mais celles d'AVANT restent : entre le filet de boot qui nommait, et un
# retrait qui ne débaptisait pas, chaque suppression de credential en fabriquait une.
# Mesuré sur la base servie le 2026-08-28 : **2** sur 139 instances vivantes.
#
# ⚠️ **Ce nettoyage n'est PAS au boot** (ADR 0065) et ce n'est pas une précaution de
# style : le filet de boot, lui, ne peut pas le faire — il ne sait qu'INSÉRER, et le
# faire archiver au démarrage transformerait un ordonnanceur de maintenance en écrivain
# de masse sur une base partagée avec la production. C'est une commande EXPLICITE
# (`scripts/archive_orphan_instances.py`), à sec par défaut, lancée une fois, à la main.
#
# ⚠️ La SOUS-INSTANCE d'un compte de messagerie (`db/sous_instances`, account
# `canal:<provider>`) n'a PAS de ligne de coffre, par construction : elle n'est pas une
# orpheline, et les deux requêtes ci-dessous l'écartent.


def list_orphan_instances(conn=None) -> list[dict]:
    """Les instances VIVANTES dont la ligne de coffre n'existe pas (ou plus).

    Le sens que la requête d'invariant appelle « instance sans ligne de coffre ». On
    rend les lignes ENTIÈRES et pas un compte : ce qui se supprime se regarde d'abord,
    et l'appelant (le script) les imprime avant d'écrire quoi que ce soit."""
    sql = f"""
    SELECT {_INSTANCE_COLS} FROM connector_instances i
     WHERE i.revoked_at IS NULL
       AND NOT (i.connector = 'unipile' AND starts_with(i.account, 'canal:'))
       AND NOT EXISTS (
           SELECT 1 FROM connector_credentials c
            WHERE c.entity_type = i.owner_type AND c.entity_id = i.owner_id
              AND c.connector = i.connector    AND c.account   = i.account)
     ORDER BY i.owner_type, i.owner_id, i.connector, i.account
    """
    if conn is not None:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    with _connect() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def archive_orphan_instances(conn=None) -> int:
    """Archive les orphelines, motif `vault_row_missing`. Rend le nombre archivé.

    **Idempotent par construction** : le prédicat est « vivante ET sans ligne de
    coffre » ; une fois archivées, elles n'y répondent plus. Rejouer rend 0.

    Archive, jamais un DELETE — même règle que partout ailleurs ici (0053-D7) : si
    l'une d'elles a été désignée par un binding ou une arête, on veut pouvoir répondre
    « elle a été retirée », pas « elle n'a jamais existé »."""
    sql = """
    UPDATE connector_instances i SET revoked_at = NOW(), revoked_reason = %s
     WHERE i.revoked_at IS NULL
       AND NOT (i.connector = 'unipile' AND starts_with(i.account, 'canal:'))
       AND NOT EXISTS (
           SELECT 1 FROM connector_credentials c
            WHERE c.entity_type = i.owner_type AND c.entity_id = i.owner_id
              AND c.connector = i.connector    AND c.account   = i.account)
    """
    if conn is not None:
        return conn.execute(sql, (REVOKED_VAULT_ROW_MISSING,)).rowcount or 0
    with _connect() as c:
        return c.execute(sql, (REVOKED_VAULT_ROW_MISSING,)).rowcount or 0


# ── Le backfill de boot ────────────────────────────────────────────────────────

# Taille de lot du backfill. Le volume réel est de l'ordre de la centaine de lignes
# de coffre ; le lot existe pour que la transaction de schéma d'un boot ne devienne
# jamais une écriture de masse sur une base PARTAGÉE prod/preprod, quel que soit ce
# que l'adoption y aura mis d'ici là.
BACKFILL_BATCH = 500

# Additive et idempotente à deux crans, comme `db.grants.edge_exists` au lot L5 :
# · `NOT EXISTS` **sans filtre sur `revoked_at`** — rejouer ne duplique pas, et
#   surtout ne RESSUSCITE pas une instance retirée à la main entre deux boots ;
# · `ON CONFLICT DO NOTHING` — la ceinture mécanique, côté base, si deux migrateurs
#   passaient malgré l'advisory lock d'`init_db`.
# `entity_type` est filtré sur le vocabulaire ACCEPTÉ : une valeur inconnue au coffre
# ferait échouer le CHECK, donc la transaction de schéma, donc le boot — sur une base
# partagée avec la production. Ce qui sort du vocabulaire est compté et journalisé,
# jamais inventé (même règle qu'au lot L5 pour les scopes hors `user:`/`org:`).
_BACKFILL_SQL = f"""
INSERT INTO connector_instances (connector, owner_type, owner_id, account, created_at)
SELECT c.connector, c.entity_type, c.entity_id, c.account, c.set_at
  FROM connector_credentials c
 WHERE c.entity_type IN {_OWNER_KINDS_SQL}
   AND NOT EXISTS (
       SELECT 1 FROM connector_instances i
        WHERE i.owner_type = c.entity_type
          AND i.owner_id   = c.entity_id
          AND i.connector  = c.connector
          AND i.account    = c.account)
 ORDER BY c.entity_type, c.entity_id, c.connector, c.account
 LIMIT %s
ON CONFLICT DO NOTHING
"""


def name_vault_rows_as_instances(conn, batch: int = BACKFILL_BATCH) -> int:
    """Une instance par ligne de coffre existante — le backfill de boot du lot L6.

    **Ce que le backfill NOMME, et ce qu'il ne touche pas.** Il pose l'identité
    (`connector`, le propriétaire, le compte) et la date de naissance de la ligne du
    coffre. Il laisse VIDES `label` et `config` : le nom affiché reste dérivé de
    `meta.label` et la config publique vit dans `meta` — les recopier ici ferait un
    second domicile pour une donnée que rien ne lit encore. Aucune ligne du coffre
    n'est lue en écriture, aucun secret n'est déchiffré, l'AAD ne bouge pas.

    Tourne à CHAQUE boot (pas de marqueur « déjà fait ») et c'est délibéré. ⚠️ **Depuis
    la pièce 2, c'est un FILET, plus le chemin de naissance** : la pose crée l'instance
    dans sa propre transaction, donc un boot ne trouve plus rien à nommer (0 ligne,
    mesuré en preprod). On le garde pour ce qu'un filet fait : rattraper les lignes
    posées avant le lot, et celles qu'un chemin d'écriture futur poserait sans passer
    par l'entonnoir.

    ⚠️ **Son angle mort, nommé plutôt que bouché** : la garde `NOT EXISTS` ne filtre pas
    `revoked_at`, donc le filet REFUSE de nommer une ligne de coffre qui porte déjà une
    instance ARCHIVÉE — c'est ce qui l'empêche de ressusciter une instance retirée à la
    main entre deux boots. Après la pièce 2, ce cas ne naît plus que d'un geste manuel
    en base (archiver une instance en laissant vivre sa clé). Ce n'est donc pas au
    filet de le rattraper : c'est à la requête d'INVARIANT de le montrer — elle vit
    dans `tests/test_connector_instances_birth_live.py` (`INVARIANT_SQL`) et dit les
    deux sens en un passage.

    Rend le nombre d'instances créées."""
    total = 0
    while True:
        cur = conn.execute(_BACKFILL_SQL, (batch,))
        posees = cur.rowcount or 0
        total += posees
        if posees < batch:
            break
    hors = conn.execute(
        "SELECT entity_type, COUNT(*) AS n FROM connector_credentials "
        f"WHERE entity_type NOT IN {_OWNER_KINDS_SQL} GROUP BY entity_type").fetchall()
    for r in hors:
        logger.warning(
            "L6 instances: %d ligne(s) de coffre en entity_type=%r NON nommées "
            "(hors du vocabulaire de propriétaire %s) — les inventer serait décider "
            "à la place du modèle", r["n"], r["entity_type"], list(OWNER_KINDS))
    return total


# ── La config possédée, une CLÉ à la fois (jamais le document entier). Premier écrivain :
# le connecteur `api` (`connectors/api_operations.py`). Aucune résolution n'en dépend.


def live_instance_configs(owner_type: str, owner_id: str, connector: str,
                          account: Optional[str] = None) -> list[dict]:
    """`[{id, account, config}]` des instances VIVANTES de ce propriétaire pour ce
    connecteur — un compte si `account` est donné, tous sinon. Une requête."""
    sql = ("SELECT id, account, config FROM connector_instances WHERE owner_type = %s "
           "AND owner_id = %s AND connector = %s AND revoked_at IS NULL")
    params: list = [owner_type, str(owner_id), connector]
    if account is not None:
        sql, params = sql + " AND account = %s", params + [account]
    with _connect() as c:
        rows = c.execute(sql + " ORDER BY account", params).fetchall()
    return [{"id": r["id"], "account": r["account"], "config": r["config"] or {}}
            for r in rows]


def live_instance_config(owner_type: str, owner_id: str, connector: str,
                         account: str = "") -> Optional[dict]:
    """`{id, config}` de l'instance VIVANTE de cette ligne de coffre, ou None."""
    rows = live_instance_configs(owner_type, owner_id, connector, account or "")
    return {"id": rows[0]["id"], "config": rows[0]["config"]} if rows else None


def merge_instance_config(instance_id: int, patch: dict) -> bool:
    """Pose les CLÉS de `patch` dans `config` d'une instance vivante (les autres
    restent). False si elle n'existe pas ou est archivée."""
    from psycopg.types.json import Jsonb
    with _connect() as c:
        row = c.execute(
            "UPDATE connector_instances SET config = config || %s::jsonb "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (Jsonb(patch), instance_id)).fetchone()
    return row is not None


def copy_instance_definition(conn, owner_type: str, owner_id: str, old: str, new: str,
                             account: str = "") -> bool:
    """Recopie `config`, `label` et `visibility` de l'instance VIVANTE de la ligne de
    coffre `old` sur celle de `new` (même propriétaire, même compte) — quand un connecteur
    RENOMMÉ reprend le coffre de l'ancien nom (`credentials_store.copy_connector_rows`).
    Dans la transaction `conn` de la copie, juste après la naissance de l'instance neuve.
    False si l'une des deux manque."""
    row = conn.execute(
        "UPDATE connector_instances n SET config = o.config, label = o.label, "
        "visibility = o.visibility FROM connector_instances o "
        "WHERE o.owner_type = %s AND o.owner_id = %s AND o.connector = %s "
        "AND o.account = %s AND o.revoked_at IS NULL "
        "AND n.owner_type = o.owner_type AND n.owner_id = o.owner_id "
        "AND n.connector = %s AND n.account = o.account AND n.revoked_at IS NULL "
        "RETURNING n.id",
        (owner_type, str(owner_id), old, account or "", new)).fetchone()
    return row is not None


def configs_for_instance_ids(ids: Sequence[int]) -> dict[int, dict]:
    """`{id: config}` des instances VIVANTES demandées — une requête."""
    if not ids:
        return {}
    with _connect() as c:
        rows = c.execute("SELECT id, config FROM connector_instances "
                         "WHERE id = ANY(%s) AND revoked_at IS NULL",
                         (list(ids),)).fetchall()
    return {r["id"]: r["config"] or {} for r in rows}
