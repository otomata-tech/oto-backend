"""Le TABLEAU lui-même : son existence, son nom, et qui y a droit.

Extrait de `db/datastore.py` sans un changement de comportement (#325). La couture
sépare le CONTENANT (un namespace, son propriétaire, ses partages) de son CONTENU (les
lignes) — deux préoccupations qui n'ont jamais évolué ensemble.

Les partages (`resource_grants`, ADR 0030/0048) vivent ici parce qu'ils répondent à la
même question que le namespace : à qui cette ressource appartient-elle, et qui d'autre
y accède. Le rôle porté par un grant est la source, la permission lecture/écriture en
est la PROJECTION — jamais l'inverse.
"""

# ── LA FRONTIÈRE DE TRADUCTION ──────────────────────────────────────────────────
#
# ⚠️ **La colonne s'appelle `namespace` en base et se SERT sous le nom `datastore`.**
# C'est délibéré, et c'est ici — dans les projections SQL — que les deux mondes se
# rejoignent.
#
# Le renommage du 08/09/2026 s'arrête au bord du stockage : renommer la colonne
# imposerait une migration, et surtout prod et préproduction partagent la même base
# tandis que le déploiement bleu/vert fait tourner les DEUX couleurs ensemble —
# l'ancienne lirait une colonne disparue, et le retour arrière deviendrait impossible.
#
# La conséquence, si on l'oublie : les WHERE et les INSERT ci-dessous nomment la
# COLONNE (`namespace`), les résultats portent la CLÉ (`datastore`). Confondre les deux
# ne lève aucune erreur — ça rend une ligne dont la clé attendue est absente, donc
# `None`, donc un filtre qui ne matche rien. Le mode d'échec est un ensemble vide, et
# il s'est produit trois fois dans la journée avant d'être compris.

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import psycopg

from ._conn import _connect
from ._partage_vivant import PARTAGE_VIVANT, PARTAGE_VIVANT_G, partage_vivant
from .users import upsert_user

logger = logging.getLogger(__name__)

# L'org de contexte d'un tableau personnel (oto#160). Même forme dans le `CREATE TABLE`
# (`db/schema/datastore.py::DATASTORE`), dans la révision `0017` et au démarrage : une
# base neuve, une base migrée et une base que le démarrage rattrape ont la même colonne.
COLONNE_CONTEXTE_ORG = "context_org_id"
DDL_COLONNE_CONTEXTE_ORG = (f"ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS "
                            f"{COLONNE_CONTEXTE_ORG} BIGINT "
                            f"REFERENCES orgs(id) ON DELETE SET NULL")


def create_datastore(owner_type: str, owner_id: str, namespace: str, *,
                     context_org_id: Optional[int] = None) -> int:
    """Crée un namespace possédé par `(owner_type, owner_id)` (ADR 0030). `owner_type`
    ∈ {user, org, group} ; `owner_id` = sub | org.id::text | group.id::text. Lève si
    le même propriétaire a déjà ce nom.

    `context_org_id` = l'org active de l'appel qui crée (oto#160), retenue pour un
    tableau PERSONNEL seulement — écrite, plus lue par aucune liste depuis la décision
    du 28/09/2026 (colonne gardée, sa suppression est un autre lot) : un tableau d'org ou d'équipe tient son contexte de
    son propriétaire, et une seconde source dirait un jour autre chose que lui. Toute
    voie de création du code la passe — `tests/datastore/test_contexte_org_160.py`
    le vérifie sur le source ; le défaut `None` ne sert qu'aux bancs."""
    if owner_type == "user":
        upsert_user(owner_id)
    contexte = int(context_org_id) if owner_type == "user" and context_org_id else None
    with _connect() as conn:
        try:
            row = conn.execute(
                f"INSERT INTO user_datastores (owner_type, owner_id, namespace, "
                f"{COLONNE_CONTEXTE_ORG}) VALUES (%s, %s, %s, %s) RETURNING id",
                (owner_type, owner_id, namespace, contexte),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"namespace `{namespace}` existe déjà") from e
        return int(row["id"])


def get_datastore(owner_type: str, owner_id: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace AS datastore, created_at FROM user_datastores "
            "WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
            (owner_type, owner_id, namespace),
        ).fetchone()
        return dict(row) if row else None


def get_datastore_by_id(ns_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace AS datastore, schema, created_at, "
            "context_org_id FROM user_datastores WHERE id = %s",
            (ns_id,),
        ).fetchone()
        return dict(row) if row else None


def get_datastore_owner(ns_id: int) -> Optional[dict]:
    """`{owner_type, owner_id}` d'un tableau, ou `None` s'il n'existe pas — la lecture
    d'appartenance (`ownership.owner_of`), sans le `schema` jsonb que relisait et
    décodait `get_datastore_by_id` à chaque contrôle d'accès (#1108)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT owner_type, owner_id FROM user_datastores WHERE id = %s",
            (ns_id,),
        ).fetchone()
        return dict(row) if row else None


def set_datastore_schema(ns_id: int, schema: Optional[dict]) -> None:
    """Pose (ou retire si None) le schéma typé d'un namespace (ADR 0032 §6 / 0029, B6).
    Soft : aucune validation des rows existantes — c'est un schéma de rendu, pas une
    contrainte d'écriture."""
    cfg = json.dumps(schema) if schema is not None else None
    with _connect() as conn:
        conn.execute("UPDATE user_datastores SET schema = %s::jsonb WHERE id = %s",
                     (cfg, ns_id))


def set_datastore_semantic(ns_id: int, enabled: bool) -> int:
    """Active/désactive la recherche SÉMANTIQUE d'un namespace (#67 V2.2, opt-in). À
    l'ACTIVATION, marque toutes ses rows dirty (le worker les indexe) et renvoie leur
    nombre ; à la DÉSACTIVATION, purge les embeddings + lève le dirty (renvoie 0)."""
    with _connect() as conn:
        conn.execute("UPDATE user_datastores SET semantic_search = %s WHERE id = %s",
                     (enabled, ns_id))
        if enabled:
            return conn.execute(
                "UPDATE datastore_rows SET embed_dirty = TRUE WHERE ns_id = %s",
                (ns_id,)).rowcount or 0
        conn.execute("DELETE FROM datastore_row_embeddings WHERE ns_id = %s", (ns_id,))
        conn.execute("UPDATE datastore_rows SET embed_dirty = FALSE "
                     "WHERE ns_id = %s AND embed_dirty", (ns_id,))
        return 0


def list_datastores_for_owners(owners: list[tuple[str, str]]) -> list[dict]:
    """Namespaces possédés par l'un des `(owner_type, owner_id)` fournis. Rend
    `context_org_id`, que lit `ownership.mes_tableaux_ici` : un tableau perso se liste,
    pour son propriétaire, dans l'org où il l'a créé (29/09/2026)."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace AS datastore, d.schema, "
            "       d.created_at, d.context_org_id "
            "FROM user_datastores d "
            "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
            "  ON d.owner_type = o.t AND d.owner_id = o.i "
            "ORDER BY d.namespace",
            (otypes, oids),
        ).fetchall()
        return [dict(r) for r in rows]


class AdresseAmbigue(LookupError):
    """Une adresse de tableau en CHIFFRES désigne deux tableaux visibles : celui dont
    c'est l'IDENTIFIANT, et un autre dont c'est le NOM (#365).

    Levée plutôt que tranchée. Les ponts qui adressent un tableau par sa clé (fiche de
    nœud, lignes d'un nœud, `slot:`) passent l'identifiant en chiffres ; préférer le
    nom — l'ancienne règle — laissait un tableau NOMMÉ « 77 », posé par n'importe quel
    membre de l'org, capter tout ce qui visait le tableau 77. Préférer l'identifiant
    trahirait à l'inverse qui a nommé son tableau « 2024 ». Aucun des deux n'est sûr :
    on refuse, en nommant les deux."""

    def __init__(self, adresse: str, par_id: int, par_nom: int):
        self.adresse, self.par_id, self.par_nom = adresse, par_id, par_nom
        super().__init__(adresse)


def resolve_datastore_ns(
    namespace: str, *, sub: str, org_ids: list[int], group_ids: list[int],
) -> Optional[dict]:
    """Résout un namespace VISIBLE par l'acteur, par NOM **ou par ID** numérique, parmi :
    possédé en perso, possédé par une de ses orgs, ou accordé (grant user/org/group).
    Priorité perso > org > grant. Retourne la ligne `user_datastores` (avec `id`) ou None.
    La décision read/write fine est ensuite faite par `ownership.can_access` sur l'id.

    ⚠️ **id OU nom** : un lien de projet stocke souvent le `target_ref` = **id numérique**
    (le picker dashboard, `EntityPickerDialog`) alors que l'agent lie par **nom** — les deux
    doivent résoudre (sinon l'aperçu tableau tombait en 404 → « Aperçu indisponible »). Le
    prédicat de VISIBILITÉ est identique quelle que soit la clé (aucun IDOR : un id hors de
    la portée de l'acteur ne résout pas).

    ⚠️ **Des chiffres qui sont À LA FOIS l'identifiant d'un tableau visible et le nom
    d'un autre** lèvent `AdresseAmbigue` (#365) : c'était le nom qui gagnait, en
    silence, et les ponts qui adressent un tableau par sa clé passent justement des
    chiffres."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    ns_id = int(namespace) if str(namespace).isdigit() else None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace AS datastore, d.schema, d.created_at "
            "FROM user_datastores d "
            "WHERE (d.namespace = %(ns)s OR d.id = %(nsid)s) AND ("
            "     (d.owner_type = 'user' AND d.owner_id = %(sub)s)"
            "  OR (d.owner_type = 'org'  AND d.owner_id = ANY(%(org)s))"
            # ADR 0049 (cadrage 10/07) : team-owned = visible dans le contexte de l'org
            # parente (le caller passe mes équipes — ou toutes celles de l'org si admin).
            "  OR (d.owner_type = 'group' AND d.owner_id = ANY(%(grp)s))"
            "  OR EXISTS ("
            "       SELECT 1 FROM resource_grants g"
            "        WHERE g.resource_type = 'datastore_namespace' AND g.resource_id = d.id::text"
            f"          AND {PARTAGE_VIVANT_G}"
            "          AND ( (g.principal_type = 'user'  AND g.principal_id = %(sub)s)"
            "             OR (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "             OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ))"
            ") "
            "ORDER BY CASE WHEN d.namespace = %(ns)s THEN 0 ELSE 1 END, "
            "         CASE WHEN d.owner_type='user' AND d.owner_id=%(sub)s THEN 0 "
            "              WHEN d.owner_type='org' THEN 1 ELSE 2 END",
            {"ns": namespace, "nsid": ns_id, "sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchall()
    if not rows:
        return None
    premier = dict(rows[0])
    if ns_id is not None and int(premier["id"]) != ns_id \
            and any(int(r["id"]) == ns_id for r in rows):
        raise AdresseAmbigue(str(namespace), par_id=ns_id, par_nom=int(premier["id"]))
    return premier


def resolve_datastore_ids_by_name(
    names: list[str], *, sub: str, org_ids: list[int], group_ids: list[int],
) -> tuple[dict[str, int], set[str]]:
    """Les identifiants des tableaux NOMMÉS `names`, vus par un principal donné — même
    prédicat de visibilité que `resolve_datastore_ns`, en UNE requête pour toute la
    liste. Rend `(résolus, ambigus)` : un nom qui ne résout pas dans cette portée est
    absent des deux — l'appelant garde le nom et n'invente pas d'identifiant.

    ⚠️ **Le principal n'est pas forcément celui qui LIT, et c'est tout l'objet.** Un
    lien de projet qui désigne son tableau par un NOM doit désigner le MÊME tableau
    pour quiconque ouvre le projet. Résolu une fois ici, au nom du PROPRIÉTAIRE du
    projet, l'identifiant est stable et se sert tel quel ; refait chez chaque lecteur,
    il dérive vers l'homonyme personnel de chacun (oto#160). C'est pourquoi cette
    résolution appartient au serveur : un écran qui la referait la referait faux.

    ⚠️ **Un nom qui désigne plusieurs tableaux au même rang est AMBIGU, pas résolu**
    (#365). Rang, du plus spécifique au plus large : possédé en perso par `sub`, par
    une équipe de la portée, par une org de la portée, puis reçu en partage. Le premier
    rang qui trouve gagne — c'est la règle du propriétaire, qui reconnaît d'abord ce
    qu'il possède. Mais deux tableaux
    « vivier » partagés par deux orgs différentes sont au même rang, et le plus petit
    identifiant l'emportait en silence : le lien pointait l'un ou l'autre selon l'ordre
    de création. Ce nom-là sort dans `ambigus`, et personne ne le sert."""
    if not names:
        return {}, set()
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.namespace, d.id, "
            "       CASE WHEN d.owner_type = 'user' AND d.owner_id = %(sub)s THEN 0 "
            "            WHEN d.owner_type = 'group' AND d.owner_id = ANY(%(grp)s) THEN 1 "
            "            WHEN d.owner_type = 'org' AND d.owner_id = ANY(%(org)s) THEN 2 "
            "            ELSE 3 END AS rang "
            "FROM user_datastores d "
            "WHERE d.namespace = ANY(%(names)s) AND ("
            "     (d.owner_type = 'user' AND d.owner_id = %(sub)s)"
            "  OR (d.owner_type = 'org'  AND d.owner_id = ANY(%(org)s))"
            "  OR (d.owner_type = 'group' AND d.owner_id = ANY(%(grp)s))"
            "  OR EXISTS ("
            "       SELECT 1 FROM resource_grants g"
            "        WHERE g.resource_type = 'datastore_namespace' AND g.resource_id = d.id::text"
            f"          AND {PARTAGE_VIVANT_G}"
            "          AND ( (g.principal_type = 'user'  AND g.principal_id = %(sub)s)"
            "             OR (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "             OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ))"
            ")",
            {"names": list(names), "sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchall()
    meilleurs: dict[str, tuple[int, set[int]]] = {}
    for r in rows:
        rang, ids = meilleurs.get(r["namespace"], (4, set()))
        if r["rang"] < rang:
            meilleurs[r["namespace"]] = (r["rang"], {int(r["id"])})
        elif r["rang"] == rang:
            ids.add(int(r["id"]))
    resolus = {nom: next(iter(ids)) for nom, (_, ids) in meilleurs.items() if len(ids) == 1}
    return resolus, {nom for nom, (_, ids) in meilleurs.items() if len(ids) > 1}


def list_datastores_granted_to(
    sub: str, org_ids: list[int], group_ids: list[int],
) -> list[dict]:
    """Namespaces accordés à l'**org active / groupe actif** via `resource_grants`
    (principal org/group), avec la permission gagnante.

    Volontairement **PAS** les grants `principal_type='user'` : un partage *en propre*
    (cross-org, ex. un namespace de ton org perso partagé à ton compte) ne doit pas
    polluer la vue Données de CHAQUE org — l'org est le contexte (ADR 0023, scope décidé
    avec l'utilisateur le 2026-07-01). La résolution par nom (`resolve_datastore_ns`) est
    elle aussi scopée à l'org active côté appelant (2026-07-03). `sub` ne sert plus qu'à
    exclure les reliques perso possédées (gérées à part)."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace AS datastore, d.created_at, "
            "       max(g.permission) AS permission "
            "FROM resource_grants g "
            "JOIN user_datastores d ON d.id::text = g.resource_id "
            f"WHERE g.resource_type = 'datastore_namespace' AND {PARTAGE_VIVANT_G} AND ("
            "     (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "  OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ) "
            # ⚠️ L'exclusion « AND NOT (owner_type='user' AND owner_id=sub) » est
            # RETIRÉE (oto-backend#870). Son commentaire disait « reliques perso
            # possédées (gérées à part) » — et ce « à part » ne pointait vers rien :
            # `list_datastores` ne listait que l'org, donc un tableau personnel
            # n'était rendu par AUCUN des deux chemins. La déduplication par id, en
            # amont, suffit à ne pas le compter deux fois.

            "GROUP BY d.id, d.owner_type, d.owner_id, d.namespace, d.created_at "
            "ORDER BY d.namespace",
            {"sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchall()
        return [dict(r) for r in rows]


def list_datastores_shared_to_user(sub: str) -> list[dict]:
    """Les tableaux partagés NOMINATIVEMENT à `sub` — `principal_type='user'` ET
    `principal_id = sub`, rien d'autre.

    ⚠️ **C'est tout le périmètre, et il est étroit à dessein.** Ni les droits d'org ni
    ceux d'équipe (ils se rangent dans la liste de l'org, `list_datastores_granted_to`),
    ni le contexte d'org de l'appel : un partage à une personne n'appartient à aucune
    org (arbitrage d'Alexis, otomata-tech/oto#160, 10/09/2026). La seule clé qui ouvre
    une ligne ici est le `sub` de l'appelant — c'est ce qui interdit de rejouer
    l'incident du 30/06 (des ressources d'une autre org visibles dans une vue d'org) :
    rien n'entre qui n'ait été donné À CETTE PERSONNE.

    Un tableau que `sub` possède lui-même n'est pas « partagé avec lui » : exclu, même
    s'il s'est posé un droit sur son propre tableau."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.context_org_id, "
            "       d.namespace AS datastore, d.schema, "
            "       d.created_at, g.permission, g.granted_by "
            "FROM resource_grants g "
            "JOIN user_datastores d ON d.id::text = g.resource_id "
            "WHERE g.resource_type = 'datastore_namespace' "
            f"  AND {PARTAGE_VIVANT_G} "
            "  AND g.principal_type = 'user' AND g.principal_id = %(sub)s "
            "  AND NOT (d.owner_type = 'user' AND d.owner_id = %(sub)s) "
            "ORDER BY d.namespace",
            {"sub": sub},
        ).fetchall()
        return [dict(r) for r in rows]


def rename_datastore_by_id(ns_id: int, new: str) -> bool:
    """Renomme un namespace par id (l'id BIGSERIAL est conservé → URL/deeplink/grants
    stables ; les grants sont keyés par id, donc rien à propager). Lève si le même
    propriétaire a déjà ce nom, ou si l'id est introuvable."""
    new = (new or "").strip()
    if not new:
        raise ValueError("nouveau nom de namespace requis")
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "SELECT owner_type, owner_id, namespace AS datastore FROM user_datastores WHERE id = %s FOR UPDATE",
                (ns_id,),
            ).fetchone()
            if not cur:
                raise ValueError("namespace introuvable")
            if cur["datastore"] == new:
                return True
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (cur["owner_type"], cur["owner_id"], new),
            ).fetchone():
                raise ValueError(f"un namespace `{new}` existe déjà")
            conn.execute(
                "UPDATE user_datastores SET namespace = %s WHERE id = %s", (new, ns_id),
            )
    return True


def delete_datastore_by_id(ns_id: int) -> bool:
    """Supprime un namespace par id (CASCADE sur `datastore_rows`) + ses grants
    (`resource_grants` n'a pas de FK car `resource_id` est générique) + son
    éventuel index de clé métier (#109 ch.3 — orphelin inoffensif sinon, mais
    autant nettoyer)."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM resource_grants WHERE resource_type = 'datastore_namespace' AND resource_id = %s",
                (str(ns_id),),
            )
            cur = conn.execute("DELETE FROM user_datastores WHERE id = %s", (ns_id,))
    # Import LOCAL : l'index de clé métier vit avec les lignes, pas avec le tableau.
    # Le faire en tête créerait un cycle (les lignes connaissent déjà le tableau).
    from .datastore import KeyIndexStillEnforced, datastore_drop_key_index
    try:
        datastore_drop_key_index(ns_id)
    except KeyIndexStillEnforced as e:
        # oto#82 : le retrait est borné. Ici la suppression a DÉJÀ abouti, et l'index
        # qui survit ne garde plus rien (le tableau n'existe plus, les ids ne se
        # réutilisent pas) — c'est le cas « orphelin inoffensif » que dit le docstring
        # ci-dessus. On le journalise au lieu de faire échouer une suppression faite,
        # et l'attente exclusive sur la table commune ne retient plus personne.
        logger.warning("ds_bkey ns=%s : index non retiré à la suppression — %s", ns_id, e)
    return cur.rowcount > 0


def reparent_datastore(ns_id: int, new_owner_type: str, new_owner_id: str) -> None:
    """Re-parente un namespace vers un nouveau propriétaire (cœur du transfert).
    Lève si le destinataire possède déjà un namespace de ce nom."""
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT namespace FROM user_datastores WHERE id = %s FOR UPDATE", (ns_id,),
            ).fetchone()
            if not row:
                raise ValueError("namespace introuvable")
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (new_owner_type, new_owner_id, row["namespace"]),
            ).fetchone():
                raise ValueError(f"le destinataire possède déjà un namespace `{row['namespace']}`")
            conn.execute(
                "UPDATE user_datastores SET owner_type = %s, owner_id = %s WHERE id = %s",
                (new_owner_type, new_owner_id, ns_id),
            )


def list_all_datastores() -> list[dict]:
    """Tous les namespaces, toutes propriétés confondues — pour l'object-browser
    PLATEFORME (gate super_admin/platform_admin côté capacité)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, owner_type, owner_id, namespace AS datastore, created_at "
            "FROM user_datastores ORDER BY owner_type, owner_id, namespace",
        ).fetchall()
        return [dict(r) for r in rows]


def count_datastore_rows_for_ns(ns_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s", (ns_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


# ADR 0048 — le rôle est la source de vérité ; `permission` (plan CONTENU) en dérive.
_ROLE_TO_PERMISSION = {"viewer": "read", "editor": "write", "manager": "write"}
_PERMISSION_TO_ROLE = {"read": "viewer", "write": "editor"}


def _normalize_role(role: Optional[str], permission: Optional[str]) -> str:
    """Rôle effectif d'un grant. `role` prime ; sinon rétro-compat depuis `permission`
    (read→viewer, write→editor) ; défaut `editor`."""
    if role in _ROLE_TO_PERMISSION:
        return role
    return _PERMISSION_TO_ROLE.get(permission or "", "editor")


def grant_resource(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: Optional[str] = None, granted_by: Optional[str] = None,
    role: Optional[str] = None, ttl_days: Optional[int] = None,
) -> Optional[object]:
    """Accorde (ou met à jour) un RÔLE à un principal sur une ressource (ADR 0048).
    `role` ∈ {viewer, editor, manager} prime ; à défaut `permission` read/write est mappé
    (rétro-compat). `permission` (plan CONTENU) est TOUJOURS dérivée du rôle (viewer→read,
    editor/manager→write) → tout le SQL du plan contenu reste inchangé. Idempotent :
    ON CONFLICT met à jour rôle + permission.

    L'échéance (otomata-tech/oto#39) : `ttl_days` (entier ≥ 1) la pose à `NOW() + N
    jours`. Omis, un partage VIVANT garde la sienne — un re-partage qui ne change qu'un
    rôle ne retire pas une échéance en silence — et un partage neuf ou ÉCHU n'en a pas :
    re-partager ce qui a expiré le rouvre, sans le laisser mort-né. Rend l'échéance
    ÉCRITE (None = sans échéance), pour que la réponse décrive la base."""
    if ttl_days is not None and (isinstance(ttl_days, bool) or int(ttl_days) < 1):
        raise ValueError(f"ttl_days doit être un entier ≥ 1 (reçu {ttl_days!r})")
    eff_role = _normalize_role(role, permission)
    eff_perm = _ROLE_TO_PERMISSION[eff_role]
    ttl = int(ttl_days) if ttl_days is not None else None
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO resource_grants "
            "(resource_type, resource_id, principal_type, principal_id, permission, role, "
            " granted_by, expires_at) "
            "VALUES (%(rt)s, %(rid)s, %(pt)s, %(pid)s, %(perm)s, %(role)s, %(by)s, "
            "        NOW() + make_interval(days => %(ttl)s::int)) "
            "ON CONFLICT (resource_type, resource_id, principal_type, principal_id) "
            "DO UPDATE SET permission = EXCLUDED.permission, role = EXCLUDED.role, "
            "granted_by = EXCLUDED.granted_by, "
            "expires_at = CASE WHEN %(ttl)s::int IS NOT NULL THEN EXCLUDED.expires_at "
            f"                 WHEN {partage_vivant('resource_grants')} "
            "                 THEN resource_grants.expires_at END "
            "RETURNING expires_at",
            {"rt": resource_type, "rid": resource_id, "pt": principal_type,
             "pid": principal_id, "perm": eff_perm, "role": eff_role, "by": granted_by,
             "ttl": ttl},
        ).fetchone()
    return row["expires_at"] if row else None


def revoke_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            "AND principal_type = %s AND principal_id = %s",
            (resource_type, resource_id, principal_type, principal_id),
        )
        return cur.rowcount > 0


def get_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT permission, role FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            f"AND principal_type = %s AND principal_id = %s AND {PARTAGE_VIVANT}",
            (resource_type, resource_id, principal_type, principal_id),
        ).fetchone()
        return dict(row) if row else None


def best_resource_grants(resource_type: str, resource_ids: list[str],
                         principals: list[tuple[str, str]]) -> dict[str, str]:
    """`{resource_id: meilleure permission}` des partages VIVANTS de ces ressources à ces
    principals (`write` > `read`) — `get_resource_grant` pour une LISTE, en une requête.
    Une ressource sans partage n'a pas d'entrée."""
    if not resource_ids or not principals:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.resource_id, MAX(g.permission) AS permission FROM resource_grants g "
            "JOIN unnest(%s::text[], %s::text[]) AS pr(t, i) "
            "  ON g.principal_type = pr.t AND g.principal_id = pr.i "
            "WHERE g.resource_type = %s AND g.resource_id = ANY(%s) "
            f"AND {PARTAGE_VIVANT_G} GROUP BY g.resource_id",
            ([p[0] for p in principals], [str(p[1]) for p in principals], resource_type,
             [str(r) for r in resource_ids]),
        ).fetchall()
    return {str(r["resource_id"]): r["permission"] for r in rows}


def manager_resource_grants(resource_type: str, resource_ids: list[str],
                            principals: list[tuple[str, str]]) -> set[str]:
    """Les `resource_id` de ces ressources dont l'un de ces principals tient un partage
    VIVANT de rôle `manager` (gérant, ADR 0048) — la question de
    `ownership._has_manager_grant` (`get_resource_grant` par principal, puis le rôle)
    pour une LISTE, en une requête (`ownership.can_govern_many`)."""
    if not resource_ids or not principals:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT g.resource_id FROM resource_grants g "
            "JOIN unnest(%s::text[], %s::text[]) AS pr(t, i) "
            "  ON g.principal_type = pr.t AND g.principal_id = pr.i "
            "WHERE g.resource_type = %s AND g.resource_id = ANY(%s) "
            f"AND g.role = 'manager' AND {PARTAGE_VIVANT_G}",
            ([p[0] for p in principals], [str(p[1]) for p in principals], resource_type,
             [str(r) for r in resource_ids]),
        ).fetchall()
    return {str(r["resource_id"]) for r in rows}


def principals_with_live_grant(resource_type: str, resource_id: str) -> set[tuple[str, str]]:
    """Les principals `(type, id)` qui tiennent un partage VIVANT de cette ressource —
    ce que l'héritage des clés d'un projet (`access/heritage.evaluer`) croise avec ses
    arêtes : un prêt de clés ne survit pas à l'échéance du partage qui le porte."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT principal_type, principal_id FROM resource_grants "
            f"WHERE resource_type = %s AND resource_id = %s AND {PARTAGE_VIVANT}",
            (resource_type, resource_id),
        ).fetchall()
    return {(r["principal_type"], str(r["principal_id"])) for r in rows}


def list_resource_grants(resource_type: str, resource_id: str) -> list[dict]:
    """Bénéficiaires d'une ressource (principal + permission + email si user), pour
    l'UI de gestion du partage.

    ⚠️ La SEULE lecture qui rend les partages ÉCHUS (otomata-tech/oto#39), marqués
    `expired` : comme un jeton expiré, que son propriétaire doit pouvoir constater
    expiré plutôt que le voir disparaître. Elle ne donne aucun accès — elle répond à
    « à qui l'ai-je partagé, et jusqu'à quand ? »."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.principal_type, g.principal_id, g.permission, g.role, g.granted_at, "
            "       g.expires_at, (g.expires_at IS NOT NULL AND g.expires_at <= NOW()) AS expired, "
            "       u.email "
            "FROM resource_grants g "
            "LEFT JOIN users u ON g.principal_type = 'user' AND u.sub = g.principal_id "
            "WHERE g.resource_type = %s AND g.resource_id = %s "
            "ORDER BY g.granted_at",
            (resource_type, resource_id),
        ).fetchall()
        return [dict(r) for r in rows]
