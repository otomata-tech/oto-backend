"""La SOUS-INSTANCE d'un compte de messagerie hébergée : la cible de ses règles d'accès.

Un compte Unipile (`unipile_accounts`, une ligne par `(sub, org_id, provider)`) n'a pas
de ligne de coffre : c'est un handle sous une CLÉ Unipile, plateforme ou propre à
l'org. Il n'avait donc pas d'instance, et une règle d'accès (`connector_account_rules`,
`db/regles_de_comptes`) n'aurait rien à viser. Ce module lui en donne une, ENFANT de
l'instance de la clé qui le porte (0053-D9-3 : une sous-instance ne pose qu'une
détermination, « le LinkedIn de Jane »).

**La forme** : `connector = 'unipile'`, propriétaire `member` `'<org_id>:<sub>'` (le
compte est à la personne, dans l'org où il est connecté), `account = 'canal:<provider>'`
— le préfixe la distingue de toute ligne de coffre (`account` d'une clé Unipile est le
nom de compte du coffre, jamais préfixé ainsi), et c'est lui qui dit « sans ligne de
coffre, par construction » à la maintenance des orphelines (`connector_instances`).

**Le parent** : l'instance vivante de la clé qui porte le compte, lue au catalogue des
instances, sans résolution :
- siège plateforme (`platform_seat`) : l'instance `platform` de `unipile`, si elle est
  unique ;
- clé du client : celle de la personne dans l'org (`member`), sinon celle de l'org, si
  elle est unique à son palier.
Sinon `parent_id` reste NULL : la sous-instance existe quand même, pour que la règle ait
une cible. Le parent n'est lu par rien avant la bascule (étape 3, lot L3), qui
désignera de toute façon la clé par la résolution ; une clé d'équipe n'est pas cherchée
(laquelle, sans l'équipe active ?). Chaque passe recalcule le parent.

Deux écrivains : la connexion d'un compte (`db.unipile.set_unipile_account`, dans sa
transaction) et la reprise des prêts (`regles_de_comptes.reprendre_les_prets`), qui les
pose toutes. La fusion de deux comptes les fait suivre (`suivre`).
"""
from __future__ import annotations

# Le parent, sur la ligne `ua` de `unipile_accounts` : un candidat UNIQUE par palier.
_PARENT = """(CASE WHEN ua.platform_seat THEN
    (SELECT CASE WHEN count(*) = 1 THEN min(p.id) END FROM connector_instances p
      WHERE p.owner_type = 'platform' AND p.connector = 'unipile'
        AND p.revoked_at IS NULL)
  ELSE COALESCE(
    (SELECT CASE WHEN count(*) = 1 THEN min(p.id) END FROM connector_instances p
      WHERE p.owner_type = 'member' AND p.owner_id = ua.org_id::text || ':' || ua.sub
        AND p.connector = 'unipile' AND NOT starts_with(p.account, 'canal:')
        AND p.revoked_at IS NULL),
    (SELECT CASE WHEN count(*) = 1 THEN min(p.id) END FROM connector_instances p
      WHERE p.owner_type = 'org' AND p.owner_id = ua.org_id::text
        AND p.connector = 'unipile' AND p.revoked_at IS NULL))
  END)"""

# Une instance par ligne de `unipile_accounts`, déconnectée comprise (la ligne survit à
# une déconnexion, et un partage suit le compte à sa reconnexion). Une seule
# instruction : l'index unique partiel des instances vivantes tient le doublon.
_POSER = f"""
INSERT INTO connector_instances (connector, owner_type, owner_id, account, parent_id)
SELECT 'unipile', 'member', ua.org_id::text || ':' || ua.sub, 'canal:' || ua.provider,
       {_PARENT}
  FROM unipile_accounts ua
 WHERE {{filtre}}
ON CONFLICT (owner_type, owner_id, connector, account) WHERE revoked_at IS NULL
DO UPDATE SET parent_id = EXCLUDED.parent_id
 WHERE connector_instances.parent_id IS DISTINCT FROM EXCLUDED.parent_id
RETURNING (xmax = 0) AS posee
"""

# Le retour arrière de la révision qui les a posées (`20261105_0900_regles_de_comptes`).
ARCHIVER_TOUTES = ("UPDATE connector_instances SET revoked_at = NOW(), "
                   "revoked_reason = 'regles_de_comptes_retirees' "
                   "WHERE owner_type = 'member' AND connector = 'unipile' "
                   "AND starts_with(account, 'canal:') AND revoked_at IS NULL")

# La sous-instance d'une ligne `ua` (jointure de la reprise).
JOINTURE = ("si.owner_type = 'member' AND si.owner_id = ua.org_id::text || ':' || ua.sub "
            "AND si.connector = 'unipile' AND si.account = 'canal:' || ua.provider "
            "AND si.revoked_at IS NULL")


def _compter(cur) -> dict:
    lignes = cur.fetchall()
    posees = sum(1 for r in lignes if (r["posee"] if isinstance(r, dict) else r[0]))
    return {"posees": posees, "reparentees": len(lignes) - posees}


def poser(conn, sub: str, org_id: int, provider: str) -> dict:
    """La sous-instance du compte `(sub, org_id, provider)`, posée ou reparentée, dans la
    transaction de l'appelant. Idempotent."""
    cur = conn.execute(_POSER.format(filtre="ua.sub = %s AND ua.org_id = %s "
                                            "AND ua.provider = %s"),
                       (sub, org_id, provider))
    return _compter(cur)


def poser_toutes(conn) -> dict:
    """Une sous-instance pour CHAQUE compte Unipile. Idempotent : rejouée, elle ne pose
    rien et ne reparente que ce dont la clé a changé."""
    return _compter(conn.execute(_POSER.format(filtre="TRUE")))


def suivre(conn, old_sub: str, new_sub: str) -> None:
    """La fusion de deux comptes (`users.repointer_patrimoine`) : les sous-instances de
    l'ancien passent au nouveau, comme ses lignes `unipile_accounts`. Une sous-instance
    que le nouveau porte déjà sur la même place garde la sienne : celle de l'ancien
    s'archive (motif `merged_onto_existing`)."""
    ancienne = ("owner_type = 'member' AND connector = 'unipile' "
                "AND starts_with(account, 'canal:') AND revoked_at IS NULL "
                "AND owner_id ~ '^[0-9]+:' "
                "AND substr(owner_id, strpos(owner_id, ':') + 1) = %(old)s")
    conn.execute(
        "UPDATE connector_instances a SET revoked_at = NOW(), "
        "revoked_reason = 'merged_onto_existing' "
        f"WHERE {ancienne} AND EXISTS ("
        "SELECT 1 FROM connector_instances b WHERE b.owner_type = 'member' "
        "AND b.connector = 'unipile' AND b.account = a.account AND b.revoked_at IS NULL "
        "AND b.owner_id = split_part(a.owner_id, ':', 1) || ':' || %(new)s)",
        {"old": old_sub, "new": new_sub})
    conn.execute(
        "UPDATE connector_instances SET owner_id = split_part(owner_id, ':', 1) || ':' "
        f"|| %(new)s WHERE {ancienne}", {"old": old_sub, "new": new_sub})
