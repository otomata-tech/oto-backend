"""Le périmètre d'un EFFACEMENT : une org, ses équipes, les comptes qui partent avec elle.

Ce n'est pas le périmètre de l'export (`export_perimetre.perimetre.resoudre`) :
l'export emporte les membres et leurs orgs personnelles, et refuse un compte partagé
avec une org hors périmètre. L'effacement, lui, efface UNE org et rien d'autre :

- l'org, ses équipes, les lignes de ses membres DANS l'org (`member` `'<org>:<sub>'`,
  et toute table possédée par org) — quel que soit le membre ;
- les comptes dont c'était la SEULE org (`partants`, décision du 12/11/2026) : leur
  compte entier part avec elle — sa ligne `users`, son profil, ses jetons, ses objets
  personnels, son compte de connexion ;
- un membre qui appartient aussi à une autre org (`restants`) garde son compte et tout
  ce qui est à lui hors de cette org.

Le tenant n'est JAMAIS du périmètre : ses lignes sont celles de l'instance.

Le périmètre se FIGE à la confirmation (`figer`) et voyage dans la demande
(`org_erasures.snapshot`) : la purge efface les appartenances qui le définissent, elle
ne pourrait pas le recalculer en chemin.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db.billing import ABONNEMENT_QUI_PRELEVE


class EffacementRefuse(RuntimeError):
    """L'effacement ne peut pas partir. `code` est le jeton servi en 409."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Instantane:
    org_id: int
    groupes: tuple[int, ...]
    partants: tuple[str, ...]
    restants: tuple[str, ...]

    def parametres(self) -> dict:
        """Les paramètres des règles d'appartenance (`export_perimetre.regles`). Le
        tenant reste VIDE : `ParTenant` et la branche `tenant` de `ParEntite` ne
        retiennent rien."""
        return {
            "orgs": [self.org_id],
            "orgs_txt": [str(self.org_id)],
            "groupes": list(self.groupes),
            "groupes_txt": [str(g) for g in self.groupes],
            "subs": list(self.partants),
            "tenants": [],
            "tenants_slug": [],
        }

    def vers_json(self) -> dict:
        return {"org_id": self.org_id, "groupes": list(self.groupes),
                "partants": list(self.partants), "restants": list(self.restants)}

    @classmethod
    def depuis_json(cls, d: dict) -> "Instantane":
        return cls(int(d["org_id"]), tuple(int(g) for g in d["groupes"]),
                   tuple(d["partants"]), tuple(d["restants"]))


def _valeurs(conn, sql: str, params) -> list:
    return [r["v"] for r in conn.execute(sql, params)]


def refus(conn, org_id: int) -> None:
    """Lève `EffacementRefuse` si l'org ne peut pas être effacée MAINTENANT."""
    org = conn.execute("SELECT id FROM orgs WHERE id = %s", (org_id,)).fetchone()
    if org is None:
        raise EffacementRefuse("unknown_org", f"Org #{org_id} inconnue.")
    if conn.execute("SELECT 1 FROM org_erasures WHERE org_id = %s AND state = 'done'",
                    (org_id,)).fetchone():
        raise EffacementRefuse("already_erased", f"L'org #{org_id} est déjà effacée.")
    if conn.execute(f"SELECT 1 FROM org_subscriptions s WHERE s.org_id = %s "
                    f"AND {ABONNEMENT_QUI_PRELEVE}", (org_id,)).fetchone():
        raise EffacementRefuse(
            "subscription_active",
            "L'org porte un abonnement qui prélève : le résilier d'abord. Effacer une org "
            "qui paie encore laisserait des prélèvements sans espace derrière eux.")


def figer(conn, org_id: int) -> Instantane:
    """Le périmètre de l'effacement de `org_id`, tel qu'il est à cet instant."""
    refus(conn, org_id)
    groupes = tuple(_valeurs(
        conn, "SELECT id AS v FROM org_groups WHERE org_id = %s ORDER BY id", (org_id,)))
    membres = sorted(set(_valeurs(
        conn, "SELECT sub AS v FROM org_members WHERE org_id = %(o)s "
              "UNION SELECT sub FROM org_group_members WHERE group_id = ANY(%(g)s)",
        {"o": org_id, "g": list(groupes)})))
    ailleurs = set(_valeurs(
        conn, "SELECT DISTINCT sub AS v FROM org_members "
              "WHERE sub = ANY(%s) AND org_id <> %s", (membres, org_id)))
    partants = tuple(s for s in membres if s not in ailleurs)
    _refuser_comptes(conn, partants)
    return Instantane(org_id, groupes, partants,
                      tuple(s for s in membres if s in ailleurs))


def _refuser_comptes(conn, partants: tuple[str, ...]) -> None:
    """Un compte qui partirait avec l'org mais qu'on ne supprime pas par ce geste."""
    operateurs = _valeurs(
        conn, "SELECT sub AS v FROM users WHERE sub = ANY(%s) "
              "AND role IN ('admin', 'super_admin')", (list(partants),))
    if operateurs:
        raise EffacementRefuse(
            "operator_account",
            f"{len(operateurs)} compte(s) d'opérateur de la plateforme n'ont que cette org : "
            "leur compte partirait avec elle. Leur retirer d'abord le rôle, ou les ajouter "
            "à une autre org.")
    admins = _valeurs(conn, "SELECT DISTINCT sub AS v FROM tenant_admins WHERE sub = ANY(%s)",
                      (list(partants),))
    if admins:
        raise EffacementRefuse(
            "tenant_admin_account",
            f"{len(admins)} compte(s) d'admin de tenant n'ont que cette org : leur compte "
            "partirait avec elle, et le tenant perdrait un admin. Le retirer d'abord "
            "(`oto_admin_tenant op=admin_remove`), ou l'ajouter à une autre org.")


def rafraichir(conn, inst: Instantane) -> Instantane:
    """Le périmètre figé à la confirmation, REVU au début de la purge. Pendant le délai
    de grâce, un partant a pu rejoindre une autre org (une invitation reste acceptable) :
    il devient restant et garde son compte. La liste des partants ne fait que
    RÉTRÉCIR — un compte arrivé depuis n'est pas ajouté à ceux qu'on supprime. Les refus
    sur les comptes se rejouent sur ce qui reste."""
    ailleurs = set(_valeurs(
        conn, "SELECT DISTINCT sub AS v FROM org_members WHERE sub = ANY(%s) AND org_id <> %s",
        (list(inst.partants), inst.org_id)))
    partants = tuple(s for s in inst.partants if s not in ailleurs)
    _refuser_comptes(conn, partants)
    return Instantane(inst.org_id, inst.groupes, partants,
                      tuple(sorted(set(inst.restants) | ailleurs)))


def titulaire(conn, org_id: int) -> dict:
    """Qui confirme : le créateur de l'org s'il en est encore admin, sinon l'admin le plus
    ancien. Avec l'adresse où le lien part, sa langue, et ce que le mail affiche (nom de
    l'org, front, marque). Lève `EffacementRefuse('no_owner')` sans admin joignable."""
    org = conn.execute("SELECT id, name, created_by, front_base_url, front_brand FROM orgs "
                       "WHERE id = %s", (org_id,)).fetchone()
    if org is None:
        raise EffacementRefuse("unknown_org", f"Org #{org_id} inconnue.")
    admins = conn.execute(
        "SELECT m.sub, u.email, u.locale FROM org_members m JOIN users u ON u.sub = m.sub "
        "WHERE m.org_id = %s AND m.org_role = 'org_admin' AND u.email IS NOT NULL "
        "  AND u.email <> '' AND u.suspended_at IS NULL "
        "ORDER BY (m.sub = %s) DESC, m.joined_at, m.sub", (org_id, org["created_by"])).fetchall()
    if not admins:
        raise EffacementRefuse("no_owner", "L'org n'a aucun admin joignable par mail : "
                               "personne ne peut confirmer son effacement.")
    a = admins[0]
    return {"sub": a["sub"], "email": a["email"], "locale": a["locale"],
            "org_name": org["name"], "front_base_url": org["front_base_url"],
            "front_brand": org["front_brand"]}
