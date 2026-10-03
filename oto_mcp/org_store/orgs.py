"""La fiche d'ORG : cycle de vie de la ligne `orgs` et ses réglages en colonne.

Créer / lire / éditer / archiver une org, son identité de marque (domaine, logo),
la baseline de connecteurs qu'elle propose (ADR 0019), l'ancre du projet KB, le
quota d'espaces créés et le rattrapage de boot `backfill_org_front`.

Ne connaît RIEN de l'appartenance (`members`) ni du coffre (`vault`) : ce module
est une feuille du package — il n'importe aucun de ses frères.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .. import config
from .. import logodev
from ..db import _connect
from ..db.billing import ABONNEMENT_QUI_PRELEVE
from ..tenancy import primary_slug

_log = logging.getLogger(__name__)


# --- écritures + lectures de gestion (barreau 3, meta-tools platform_admin) --

def create_org(name: str, created_by: Optional[str] = None,
               front_base_url: Optional[str] = None,
               front_brand: Optional[str] = None,
               front_of: Optional[str] = None) -> int:
    """Crée une org. `front_base_url`/`front_brand` = le front qui l'héberge :
    NULL/NULL = oto, le défaut. Écrits à l'INSERT plutôt qu'en seconde écriture — une
    org ne doit jamais exister, fût-ce un instant, sans la marque de ses liens
    sortants. Cf. les colonnes `orgs.front_*` (db/_init) et `org_front` ci-dessous.

    **Dérivé ICI quand l'appelant ne le pose pas** (`config.front_for`), du tenant de
    `front_of` — le compte pour qui l'org est créée — sinon de `created_by`. c1896d0
    avait confié la dérivation à l'appelant, et un seul des trois créateurs d'org la
    faisait (`capabilities/orgs.py`) : la console admin et l'org PERSO semée à
    l'inscription repartaient à NULL — donc toute invitation émise depuis un espace
    perso d'un tenant tiers pointait `oto.cx` (vécu le 26/08 sur une org cliente, et
    30 orgs perso du même tenant dans le même état). Un écrivain unique ne peut pas oublier.
    Passer explicitement `None, None` n'est pas « pas de front », c'est « dérive ».

    **Pose aussi `orgs.tenant_id`** (2026-09-03), dérivé de la MÊME source par
    `config.tenant_slug_for` — et c'est le seul endroit du code qui l'écrit. Avant, la
    colonne restait au DEFAULT : 165 orgs sur 165 portaient le tenant primaire, dont
    les 65 hébergées par un partenaire. Le rattachement n'est pas déclarable par
    l'appelant, pour la même raison que la marque de front — il suivrait alors ce
    qu'on demande, pas l'émetteur du jeton."""
    name = (name or "").strip()
    if not name:
        raise ValueError("nom d'org requis")
    if front_base_url is None and front_brand is None:
        front_base_url, front_brand = config.front_for(front_of or created_by)
    # Le RATTACHEMENT de l'org à son tenant, posé au même INSERT que la marque et
    # pour la même raison : une org ne doit jamais exister, fût-ce un instant, sans
    # déclarer de qui elle relève. `tenant_slug_for` n'est PAS déductible de
    # `front_brand` — elle n'a pas sa condition de `dashboard_url` (cf. sa docstring).
    tenant_slug = config.tenant_slug_for(front_of or created_by)
    with _connect() as conn:
        # Le slug se résout en id DANS l'INSERT : une seule aller-retour, et surtout
        # pas de fenêtre entre « quel tenant » et « écris-le ». Slug inconnu de la
        # table ⟹ le tenant primaire (`COALESCE(…, 1)`) : la FK refuserait un id
        # inventé, et faire échouer une création d'org sur un registre en avance sur
        # la base serait le mauvais sens du refus. ⚠️ L'écart n'est plus rapporté nulle
        # part (contrôle retiré le 03/09/2026) — il est sans conséquence : ce
        # rattachement ne décide rien que les deux dérivations de `db.org_tenant_slug`
        # ne décident déjà.
        row = conn.execute(
            "INSERT INTO orgs (name, created_by, front_base_url, front_brand, "
            "tenant_id) VALUES (%s, %s, %s, %s, "
            "COALESCE((SELECT id FROM tenants WHERE slug = %s), 1)) RETURNING id",
            (name, created_by, front_base_url or None, front_brand or None,
             tenant_slug),
        ).fetchone()
        return int(row["id"])


def org_front(org_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Front qui héberge l'org : `(base_url, brand)`, `(None, None)` pour oto (le
    défaut) et pour une org inconnue/absente. Lecture ciblée plutôt qu'un élargissement
    de `get_org` : c'est de la config de déploiement, elle n'a rien à faire dans la
    fiche d'org rendue aux appelants. Cf. les colonnes `orgs.front_*` (db/_init)."""
    if not org_id:
        return (None, None)
    with _connect() as conn:
        row = conn.execute(
            "SELECT front_base_url, front_brand FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    return (row["front_base_url"], row["front_brand"]) if row else (None, None)


def get_org_suspension(org_id: int) -> Optional[dict]:
    """L'état de suspension d'une org : `None` si elle est active (le cas de toutes).
    Lecture sur clé primaire — la SOURCE UNIQUE du prédicat (`org_suspension`)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, suspended_at, suspended_by, suspended_reason FROM orgs "
            "WHERE id = %s AND suspended_at IS NOT NULL", (org_id,)).fetchone()
    return dict(row) if row else None


def suspended_org_ids() -> list[int]:
    """Toutes les orgs suspendues — la liste que `org_suspension` garde en mémoire.
    Petite par nature (index partiel `idx_orgs_suspended_by`)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM orgs WHERE suspended_at IS NOT NULL").fetchall()
    return [int(r["id"]) for r in rows]


def suspend_org(org_id: int, *, by: str, reason: str) -> Optional[dict]:
    """Suspend une org. Rend l'état posé, `None` si l'org n'existe pas. Ne réécrit
    pas une suspension en cours (même règle que `suspend_account`)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE orgs SET suspended_at = COALESCE(suspended_at, NOW()), "
            "  suspended_by = COALESCE(suspended_by, %s), "
            "  suspended_reason = COALESCE(suspended_reason, %s) "
            "WHERE id = %s "
            "RETURNING id, suspended_at, suspended_by, suspended_reason",
            (by, reason, org_id)).fetchone()
    return dict(row) if row else None


def resume_org(org_id: int) -> bool:
    """Lève la suspension. True si une suspension a bien été levée."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE orgs SET suspended_at = NULL, suspended_by = NULL, "
            "  suspended_reason = NULL "
            "WHERE id = %s AND suspended_at IS NOT NULL RETURNING id",
            (org_id,)).fetchone()
    return bool(row)


def get_org(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, description, created_by, created_at, logo_url, "
            "domain, industry, location FROM orgs WHERE id = %s",
            (org_id,),
        ).fetchone()
        return dict(row) if row else None


# Domaine de marque d'une org : hostname nu, minuscule (acme.com). Tolère une
# saisie en URL (schéma/chemin/`www.` retirés) ; lève sur une forme non-domaine
# (pas de fallback silencieux). Le domaine vide efface (colonne NULL).
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def normalize_domain(raw: str) -> Optional[str]:
    d = (raw or "").strip().lower()
    if not d:
        return None
    d = re.sub(r"^[a-z+]+://", "", d)          # https://acme.com/… → acme.com/…
    d = d.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if not _DOMAIN_RE.match(d):
        raise ValueError(f"domaine invalide : {raw!r}")
    return d


def effective_logo_url(org: dict) -> Optional[str]:
    """Logo affiché pour une org : l'upload (`logo_url`, Object Storage) prime,
    sinon dérivé du CDN logo.dev à partir du `domain` déclaré (même patron que
    le catalogue connecteurs). None → monogramme côté UI."""
    return org.get("logo_url") or logodev.logo_url(org.get("domain"))


def update_org(org_id: int, name: Optional[str] = None,
               description: Optional[str] = None,
               domain: Optional[str] = None,
               industry: Optional[str] = None,
               location: Optional[str] = None) -> bool:
    """Édite le profil d'une org. None = conserver le champ ; chaîne vide =
    effacer (domain → NULL). False si absente.

    Miroir de `group_store.update_group` au grain org. Métadonnées en clair
    (nom/prose/domaine), hors coffre."""
    sets, params = [], []
    if name is not None:
        n = name.strip()
        if not n:
            raise ValueError("nom d'org vide")
        sets.append("name = %s")
        params.append(n)
    if description is not None:
        sets.append("description = %s")
        params.append(description.strip())
    if domain is not None:
        sets.append("domain = %s")
        params.append(normalize_domain(domain))
    if industry is not None:
        sets.append("industry = %s")
        params.append(industry.strip())
    if location is not None:
        sets.append("location = %s")
        params.append(location.strip())
    if not sets:
        return get_org(org_id) is not None
    params.append(org_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE orgs SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )
        return (cur.rowcount or 0) > 0


def list_all_orgs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_by, created_at, logo_url, domain FROM orgs "
            "WHERE archived_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def list_orgs_page(after_id: int, limit: int) -> list[dict]:
    """Les orgs non archivées d'id > `after_id`, par id croissant, au plus `limit` —
    la liste paginée par curseur du service commerce (`service.orgs.list`)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at FROM orgs "
            "WHERE archived_at IS NULL AND id > %s ORDER BY id LIMIT %s",
            (int(after_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


class OrgAvecAbonnementActif(Exception):
    """L'org porte un abonnement qui prélève : elle ne s'archive pas (#400).

    Archiver est un soft-delete qui la rend invisible de tous les listings — plus
    personne ne pourrait résilier son abonnement, qui continuerait d'être prélevé.
    Les capacités d'archivage la traduisent en `409 org_has_active_subscription`."""

    def __init__(self, org_id: int):
        super().__init__(f"org #{org_id} : abonnement actif, à résilier avant archivage")
        self.org_id = org_id


def archive_org(org_id: int) -> bool:
    """Archive (soft-delete) une org : masquée de tous les listings, réversible
    (DB : `UPDATE orgs SET archived_at = NULL`). Aucune FK touchée (membres,
    credentials, usage restent). Les membres qui l'avaient pour org
    active basculent sur leur plus ancienne org NON archivée restante (miroir
    `remove_org_member`) ; sans org restante → plus d'org active (= perso).
    False si l'org est inconnue ou déjà archivée.

    Lève `OrgAvecAbonnementActif` si l'org porte un abonnement qui prélève
    (`db.billing.ABONNEMENT_QUI_PRELEVE`) : à résilier d'abord. Le contrôle et la pose
    d'`archived_at` sont UNE transaction, sous le verrou de mise à jour de la ligne `orgs` ;
    la souscription prend le verrou partagé de la même ligne (`billing.upsert_org_
    subscription`) — l'une attend l'autre, aucune ne passe sur un instantané périmé."""
    with _connect() as conn:
        with conn.transaction():
            # `archived_at IS NULL` dans le verrou : déjà archivée → False, idempotent,
            # même si elle porte un abonnement (préexistant à ce refus). `NO KEY UPDATE`
            # et non `UPDATE` : il suffit à exclure le verrou partagé de la souscription
            # (`FOR SHARE`) — c'est ce que l'`UPDATE` d'en dessous prendrait de toute
            # façon — sans bloquer, en plus, toute insertion qui référence l'org (un
            # verrou `FOR UPDATE` interdit même la simple vérification de clé étrangère).
            if conn.execute(
                "SELECT 1 FROM orgs WHERE id = %s AND archived_at IS NULL "
                "FOR NO KEY UPDATE",
                (org_id,),
            ).fetchone() is None:
                return False
            if conn.execute(
                "SELECT 1 FROM org_subscriptions s "
                f"WHERE s.org_id = %s AND {ABONNEMENT_QUI_PRELEVE}",
                (org_id,),
            ).fetchone() is not None:
                raise OrgAvecAbonnementActif(org_id)
            cur = conn.execute(
                "UPDATE orgs SET archived_at = now() WHERE id = %s AND archived_at IS NULL",
                (org_id,),
            )
            if (cur.rowcount or 0) == 0:
                return False
            # Une org archivée est invisible à `get_personal_org` (filtre
            # `archived_at IS NULL`) : elle doit AUSSI libérer le slot perso
            # (`uq_orgs_personal_of`, partiel `personal_of IS NOT NULL` mais PAS
            # sur l'archivage) — sinon elle occupe le slot d'un user sans être
            # trouvable → `ensure_personal_org` recrée en boucle une org qui
            # échoue sur l'UPDATE personal_of (UniqueViolation) à chaque boot.
            conn.execute(
                "UPDATE orgs SET personal_of = NULL WHERE id = %s", (org_id,)
            )
            stranded = conn.execute(
                "SELECT sub FROM org_members WHERE org_id = %s AND is_active", (org_id,)
            ).fetchall()
            for r in stranded:
                sub = r["sub"]
                conn.execute(
                    "UPDATE org_members SET is_active = FALSE WHERE sub = %s AND org_id = %s",
                    (sub, org_id),
                )
                conn.execute(
                    """
                    UPDATE org_members SET is_active = TRUE
                     WHERE sub = %s AND org_id = (
                         SELECT m.org_id FROM org_members m JOIN orgs o ON o.id = m.org_id
                          WHERE m.sub = %s AND m.org_id <> %s AND o.archived_at IS NULL
                          ORDER BY m.joined_at ASC LIMIT 1
                     )
                    """,
                    (sub, sub, org_id),
                )
            return True


def is_archived_org(org_id: int) -> bool:
    """L'org est-elle archivée (soft-delete) ? Inconnue ⟹ False — « archivée » est un
    ÉTAT, pas une absence : c'est `get_org` qui répond de l'existence, et confondre les
    deux ferait rendre `unknown_org` sur une org bien présente mais archivée.

    Ce prédicat existe parce que le palier org le lisait de deux façons contradictoires
    (signal d'usage #467, 15/08). Toutes les LECTURES joignent `orgs` et filtrent
    `archived_at IS NULL` (`list_orgs_for_user`, `resolve_org_for_user`, `list_all_orgs`,
    `get_personal_org`) : l'org archivée n'existe plus pour elles. Le RÔLE, lui, sort de
    `get_org_role`, qui lit `org_members` SEULE — sans jointure sur `orgs`, donc
    `archived_at` ne l'atteint pas. `roles.is_org_admin` rendait donc True sur une org
    archivée, et `org.update` la renommait pendant que `_org=<id>` répondait à la même
    personne qu'elle n'était membre de rien.

    On ne corrige PAS `get_org_role` : sur un soft-delete les membres sont conservés, le
    rôle reste un fait vrai, et `org.archive` a besoin qu'il survive à l'archivage pour
    rester idempotent. C'est ce que la CAPACITÉ en déduit qui devait changer — d'où un
    prédicat explicite, lu par la règle d'autz `ORG_ADMIN_OF_LIVE` (capabilities/_authz.py)
    plutôt qu'une clause de plus enfouie dans un handler."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT archived_at FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        return bool(row and row["archived_at"] is not None)


# --- baseline de connecteurs proposés par l'org (ADR 0019) ------------------

def get_org_default_connectors(org_id: int) -> Optional[list[str]]:
    """Baseline de connecteurs par défaut de l'org (« org propose », ADR 0019),
    ou None si l'org n'en pose pas. Depuis peu : c'est la source RÉELLE du socle
    de départ d'un NOUVEAU membre (union avec le socle plateforme au seed,
    `session_visibility.compute_hidden_tools`) — plus seulement un badge
    consultatif. N'affecte jamais un membre déjà seedé (existant) : ceux-là
    restent sur leurs propres choix, sauf poussée explicite via `connectors.
    bulk_select`. Le membre reste toujours libre de (dé)sélectionner APRÈS
    son seed initial — cette baseline ne fixe qu'un point de départ."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT default_connectors FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    if not row:
        return None
    dc = row["default_connectors"]
    return list(dc) if dc is not None else None


def set_org_default_connectors(org_id: int, connectors: Optional[list[str]]) -> bool:
    """Pose (ou efface si None) la baseline de connecteurs proposés de l'org. False si absente."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET default_connectors = %s WHERE id = %s",
            (list(connectors) if connectors is not None else None, org_id),
        )
        return (cur.rowcount or 0) > 0


def set_org_logo(org_id: int, url: Optional[str]) -> None:
    """Pose (ou efface si url=None) l'URL publique du logo de l'org.

    URL publique (Object Storage), pas un secret → colonne en clair."""
    with _connect() as conn:
        conn.execute("UPDATE orgs SET logo_url = %s WHERE id = %s", (url, org_id))


# --- Projet KB ancré par id (lot 3, chantier 0.3) ---------------------------
# Fin de l'identification « par son NOM » (renommable → 2 KB, transfert → KB cassée) :
# `orgs.kb_project_id` = l'ancre. kb.py résout par id + auto-répare (clear/claim).

def get_kb_project_id(org_id: int) -> Optional[int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT kb_project_id FROM orgs WHERE id = %s", (org_id,)).fetchone()
        return int(row["kb_project_id"]) if row and row["kb_project_id"] is not None else None


def claim_kb_project(org_id: int, project_id: int) -> bool:
    """Pose l'ancre SI ELLE EST LIBRE (verrou optimiste de création — deux appels
    concurrents créent chacun leur projet, un seul claim gagne, le perdant archive
    son doublon). True = ce projet est désormais LA KB de l'org."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET kb_project_id = %s WHERE id = %s AND kb_project_id IS NULL",
            (project_id, org_id))
        return (cur.rowcount or 0) > 0


def clear_kb_project(org_id: int, expected_project_id: int) -> None:
    """Lève une ancre PENDOUILLANTE (projet archivé/transféré hors org) — compare-and-
    clear pour ne jamais écraser une réparation concurrente déjà re-posée."""
    with _connect() as conn:
        conn.execute(
            "UPDATE orgs SET kb_project_id = NULL WHERE id = %s AND kb_project_id = %s",
            (org_id, expected_project_id))


def backfill_org_front() -> dict:
    """Idempotent (boot) : une org créée par un compte d'un tenant tiers porte le
    front de ce tenant — rattrape les lignes nées à NULL avant que `create_org` ne
    dérive lui-même (cf. sa docstring). Joint sur le PRÉFIXE du `created_by`
    (`<slug>:`), la même classification que `tenancy.tenant_of`, contre la table
    `tenants` — la source du `dashboard_url`, pas le registre du process, dont la
    péremption est justement l'autre façon de produire ces NULL.

    Ne touche JAMAIS une org du tenant `oto` : son créateur porte un sub NU, qui ne
    matche aucun préfixe `slug:`. Ne touche jamais une ligne déjà posée. Un tenant
    sans `dashboard_url` ne pose rien (même inertie que `config.front_for`)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            UPDATE orgs o
               SET front_base_url = t.dashboard_url, front_brand = t.slug
              FROM tenants t
             WHERE o.front_base_url IS NULL AND o.front_brand IS NULL
               AND t.slug <> %(primary)s AND t.dashboard_url IS NOT NULL
               AND o.created_by LIKE t.slug || ':%%'
            RETURNING o.id, t.slug
            """,
            {"primary": primary_slug()},
        ).fetchall()
    if rows:
        _log.info("backfill_org_front: %d org(s) rattachée(s) à leur front : %s",
                  len(rows), ", ".join(f"#{r['id']}→{r['slug']}" for r in rows))
    return {"updated": len(rows)}


# --- création self-serve + invitations (onboarding SaaS) --------------------

def count_orgs_created_by(sub: str) -> int:
    """Espaces créés par ce sub qui OCCUPENT une place du quota (`org.create`).

    Le compte est celui d'un plafond qu'on peut redescendre, pas d'un total
    historique : il ne retient donc que les orgs qui coûtent encore quelque chose.

    - `archived_at IS NULL` — `archive_org` est le SEUL geste par lequel un
      utilisateur peut relâcher une place (il n'y a pas de hard-delete, ADR : les FK
      restent pour l'audit). Compter les archivées rendait le plafond
      **irréversible** : l'org disparaissait de tous les listings (`list_orgs_for_user`
      filtre déjà `archived_at IS NULL`) sans rendre sa place, donc le refus tombait
      sur un compte que rien ne pouvait plus faire baisser.
    - `personal_of IS NULL` — l'espace personnel n'est pas un espace *choisi* : il est
      posé d'office par `ensure_personal_org` et son archivage est refusé
      (`is_personal_org`, il serait recréé au boot suivant). Le compter facturait donc
      une place que son propriétaire n'a ni demandée ni le droit de libérer — le
      plafond RÉEL valait un de moins que celui annoncé par le message d'erreur.

    L'axe `created_by` est inchangé : c'est bien « créés », pas « dont on est membre »
    — rejoindre l'org d'autrui n'a jamais consommé de place et ne doit pas commencer.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM orgs "
            " WHERE created_by = %s AND archived_at IS NULL AND personal_of IS NULL",
            (sub,),
        ).fetchone()["n"]
