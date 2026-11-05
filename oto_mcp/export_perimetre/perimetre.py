"""Le périmètre d'un propriétaire : des orgs DÉCLARÉES, et ce qui s'en dérive.

On déclare des orgs ; les équipes, les comptes, les orgs PERSONNELLES de ces comptes
(l'org que l'inscription d'un compte lui a posée et où il est encore seul,
`db.org_d_inscription.seule_d_inscription` — l'ex-org perso, plus d'étiquette
`personal_of` depuis le 05/11/2026) et le TENANT qui les héberge s'en dérivent. Rien
d'autre ne se devine, et chaque ambiguïté REFUSE en se nommant :

- un compte qui est AUSSI membre d'une org hors périmètre (`ComptesPartages`) : ses
  données personnelles ne s'attribuent ni à l'un ni à l'autre ;
- des orgs hébergées par plusieurs tenants (`TenantsMultiples`) : la cible n'a qu'un
  tenant primaire ;
- un tenant qui héberge aussi des orgs hors périmètre (`TenantPartage`) : sa ligne,
  ses clés et ses admins ne sont pas à un seul propriétaire ;
- un compte que le tenant ne qualifie pas (`ComptesHorsTenant`) : sur la cible, le
  tenant devient PRIMAIRE et ses subs y sont nus (`tenancy.qualify`) — un compte qui
  n'en porte pas le préfixe est celui d'un autre annuaire.

Le périmètre porte aussi les préfixes des tenants TIERS de la source
(`prefixes_tiers`) : un sub qui n'en porte aucun est un sub NU, celui de l'annuaire du
tenant primaire (`tenancy.tenant_of`). C'est ce qui classe les comptes hors périmètre
que portent des lignes du périmètre (`comptes`) — ceux-là ne sont pas des membres, et
aucun des refus ci-dessus ne les concerne.

Le tenant d'une org est son tenant EFFECTIF, l'union des trois axes de
`db.tenants.org_tenant_slug` (colonne, marque, préfixe des membres) — lue par la même
expression, jamais recopiée : la colonne de rattachement seule a un trou historique.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db.org_d_inscription import seule_d_inscription
from ..db.tenants import _ORG_TENANT_EXPR


class PerimetreRefuse(RuntimeError):
    """Le périmètre déclaré ne se résout pas en un propriétaire sans ambiguïté."""


class ComptesPartages(PerimetreRefuse):
    def __init__(self, partages: dict[str, list[int]]):
        self.partages = partages
        detail = "; ".join(f"{s} (aussi membre de {o})" for s, o in sorted(partages.items()))
        super().__init__(f"{len(partages)} compte(s) du périmètre sont aussi membres "
                         f"d'orgs hors périmètre — à trancher avant tout export : {detail}")


class TenantsMultiples(PerimetreRefuse):
    pass


class TenantPartage(PerimetreRefuse):
    pass


class ComptesHorsTenant(PerimetreRefuse):
    pass


@dataclass(frozen=True)
class Perimetre:
    orgs_declarees: tuple[int, ...]
    orgs: tuple[int, ...]          # déclarées + orgs personnelles de leurs comptes
    groupes: tuple[int, ...]
    subs: tuple[str, ...]
    id_tenant: int
    tenant_slug: str
    tenant_primaire_source: bool   # le tenant est-il la ligne 1 de la SOURCE ?
    prefixes_tiers: tuple[str, ...] = ()   # `<slug>:` de chaque tenant tiers de la source

    def parametres(self) -> dict:
        return {
            "orgs": list(self.orgs),
            "orgs_txt": [str(o) for o in self.orgs],
            "groupes": list(self.groupes),
            "groupes_txt": [str(g) for g in self.groupes],
            "subs": list(self.subs),
            "tenants": [self.id_tenant],
            "tenants_slug": [self.tenant_slug],
            "prefixe_tenant": self.prefixe_tenant,
            "prefixes_tiers": list(self.prefixes_tiers),
        }

    @property
    def prefixe_tenant(self) -> str | None:
        """Le préfixe que le tenant donne à ses subs ICI (`None` s'il est primaire : ses
        subs y sont déjà nus)."""
        return None if self.tenant_primaire_source else f"{self.tenant_slug}:"

    def comptes_cible(self) -> dict[str, str]:
        """Sub source → sub cible. Le tenant devient primaire sur la cible, où ses subs
        sont NUS : le préfixe `<slug>:` tombe (identité si il l'était déjà ici)."""
        prefixe = self.prefixe_tenant
        if prefixe is None:
            return {s: s for s in self.subs}
        return {s: s[len(prefixe):] for s in self.subs}


def _ids(conn, sql: str, params) -> tuple:
    return tuple(r["v"] for r in conn.execute(sql, params))


def resoudre(conn, orgs: list[int]) -> Perimetre:
    """Résout le périmètre des `orgs` déclarées, ou lève `PerimetreRefuse`."""
    declarees = tuple(sorted({int(o) for o in orgs}))
    if not declarees:
        raise PerimetreRefuse("aucune org déclarée : un périmètre se déclare, il ne se devine pas")
    inconnues = sorted(set(declarees) - set(_ids(
        conn, "SELECT id AS v FROM orgs WHERE id = ANY(%s)", (list(declarees),))))
    if inconnues:
        raise PerimetreRefuse(f"org(s) déclarée(s) introuvable(s) : {inconnues}")
    groupes = _ids(conn, "SELECT id AS v FROM org_groups WHERE org_id = ANY(%s)",
                   (list(declarees),))
    subs = set(_ids(conn, "SELECT sub AS v FROM org_members WHERE org_id = ANY(%(o)s) "
                          "UNION SELECT sub FROM org_group_members WHERE group_id = ANY(%(g)s)",
                    {"o": list(declarees), "g": list(groupes)}))
    toutes = tuple(sorted(set(declarees) | set(_ids(
        conn, "SELECT o.id AS v FROM org_members m JOIN orgs o ON o.id = m.org_id "
              f"WHERE m.sub = ANY(%s) AND {seule_d_inscription('o', 'm.sub')}",
        (list(subs),)))))
    partages = {r["sub"]: list(r["hors"]) for r in conn.execute(
        "SELECT sub, array_agg(org_id ORDER BY org_id) AS hors FROM org_members "
        "WHERE sub = ANY(%s) AND NOT (org_id = ANY(%s)) GROUP BY sub",
        (list(subs), list(toutes)))}
    if partages:
        raise ComptesPartages(partages)
    id_tenant, slug, primaire = _tenant(conn, toutes)
    if not primaire:
        hors = sorted(s for s in subs if not s.startswith(f"{slug}:"))
        if hors:
            raise ComptesHorsTenant(
                f"{len(hors)} compte(s) du périmètre ne sont pas des comptes du tenant "
                f"{slug!r} (sub sans le préfixe `{slug}:`) : {hors}")
    groupes = _ids(conn, "SELECT id AS v FROM org_groups WHERE org_id = ANY(%s) ORDER BY id",
                   (list(toutes),))
    return Perimetre(declarees, toutes, groupes, tuple(sorted(subs)), id_tenant, slug, primaire,
                     prefixes_tiers(conn))


def prefixes_tiers(conn) -> tuple[str, ...]:
    """Le préfixe `<slug>:` de chaque tenant TIERS de la base (tout sauf la ligne 1)."""
    return tuple(f"{r['v']}:" for r in conn.execute(
        "SELECT slug AS v FROM tenants WHERE id <> 1 ORDER BY slug"))


def _tenant(conn, orgs: tuple[int, ...]) -> tuple[int, str, bool]:
    """(id, slug, est-il la ligne 1 de la source) du tenant qui héberge les `orgs`."""
    primaire = conn.execute("SELECT slug FROM tenants WHERE id = 1").fetchone()
    if primaire is None:
        raise PerimetreRefuse("la base source n'a pas de tenant primaire (ligne 1 de `tenants`)")
    effectif = f"SELECT o.id, {_ORG_TENANT_EXPR} AS slug FROM orgs o"
    params = {"primary": primaire["slug"], "orgs": list(orgs)}
    slugs = sorted({r["slug"] for r in conn.execute(
        f"SELECT slug FROM ({effectif}) e WHERE id = ANY(%(orgs)s)", params)})
    if len(slugs) != 1:
        raise TenantsMultiples(f"les orgs du périmètre relèvent de plusieurs tenants {slugs} : "
                               "la cible n'en a qu'un, son tenant primaire")
    slug = slugs[0]
    ailleurs = [r["id"] for r in conn.execute(
        f"SELECT id FROM ({effectif}) e WHERE slug = %(slug)s AND NOT (id = ANY(%(orgs)s)) "
        "ORDER BY id", {**params, "slug": slug})]
    if ailleurs:
        raise TenantPartage(f"le tenant {slug!r} héberge aussi {len(ailleurs)} org(s) hors "
                            f"périmètre {ailleurs[:20]} : sa ligne, ses clés et ses admins "
                            "ne sont pas à un seul propriétaire")
    ligne = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if ligne is None:
        raise PerimetreRefuse(f"le tenant {slug!r} n'a pas de ligne dans `tenants`")
    return ligne["id"], slug, ligne["id"] == 1
