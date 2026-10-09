"""Palier tenant (ADR 0052) — lecture de l'étage d'identité.

Source du registre d'émetteurs (`tenancy.build`), et **suivi** des tenants pour la
console plateforme. Toujours en LECTURE SEULE : déclarer un tenant reste un runbook
(une instance Logto par tenant, barreau B4) — un écran qui poserait un émetteur
donnerait l'illusion qu'il suffit d'une ligne en base, alors que le registre est
construit AU BOOT et que l'annuaire n'existe pas encore côté partenaire.

⚠️ **Un tenant se compte par DEUX sources qui peuvent diverger**, et c'est
précisément ce que le suivi doit rendre visible :

- `orgs.tenant_id` — le rattachement DÉCLARÉ d'une organisation (posé par L1 sur
  l'existant, par le provisioning ensuite) ;
- le **préfixe du sub** — la qualification par tenant (`tenancy.qualify`), qui suit
  l'émetteur du jeton et rien d'autre.

Rien ne les tient ensemble : un compte peut être qualifié `<tenant>:…` pendant que
son organisation reste sur le tenant `oto` (c'est l'état laissé par une bascule
L3bis partielle). Les compteurs les gardent donc SÉPARÉS et nomment l'écart
(`orgs_desalignees`) plutôt que d'en dériver un chiffre unique qui mentirait.
"""
from __future__ import annotations

from .. import tenancy
from ._conn import _connect
from . import journal_jour
from .lecture_bornee import lecture_d_agregat

# Bornes des listes servies par la fiche d'un tenant : une fiche rend son INDEX,
# pas la population (cf. §« Ce qu'un outil RENVOIE a un budget »).
_TENANT_LIST_CAP = 50


def list_tenant_issuers() -> list:
    """Tenants qui déclarent un émetteur, ordre stable.

    Le tenant `oto` n'y figure **pas** : son émetteur est l'env (`LOGTO_ENDPOINT`),
    donc DB-indépendant — l'authentification canonique ne doit jamais dépendre
    d'une lecture de table. Une ligne qui le redéclarerait est de toute façon
    ignorée par le registre (l'env gagne).
    """
    with _connect() as conn:
        rows = conn.execute(
            # `name` et `hosts` servent la DÉCOUVERTE (lot L3 : PRM et 401 sensibles
            # au host), jamais la vérification d'un jeton — celle-ci ne connaît que
            # l'émetteur. Les lire ici ne change donc rien au chemin d'auth.
            "SELECT slug, name, issuer, jwks_uri, hosts, oauth_client_id, "
            "dashboard_url, link_paths, tool_prefix, brand, logto_mgmt, disabled_at "
            "FROM tenants "
            "WHERE issuer IS NOT NULL AND btrim(issuer) <> '' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def tenant_exists(slug: str) -> bool:
    """Le slug désigne-t-il une ligne `tenants` ? Garde de pose d'une clé de tenant
    (L-clés PR 1) : un slug inconnu ne doit pas fabriquer une ligne de coffre que
    personne ne lira. Lecture par PK, sans compteur — la fiche de suivi coûte trop
    pour une garde."""
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM tenants WHERE slug = %s", (slug,)).fetchone() is not None


# ── Le rôle « admin de tenant » (L-clés PR 2) ────────────────────────────────

def is_tenant_admin(slug: str, sub: str) -> bool:
    """Lu à l'appel par `_authz.TENANT_ADMIN_OF` — UNE lecture par PK."""
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM tenant_admins WHERE slug = %s AND sub = %s",
                            (slug, sub)).fetchone() is not None


def list_tenant_admins(slug: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, granted_by, granted_at FROM tenant_admins WHERE slug = %s "
            "ORDER BY granted_at, sub", (slug,)).fetchall()
    return [dict(r) for r in rows]


def add_tenant_admin(slug: str, sub: str, granted_by: "str | None" = None) -> None:
    """Idempotent : re-déclarer un admin ne change rien (ni l'auteur, ni la date)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tenant_admins (slug, sub, granted_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (slug, sub) DO NOTHING", (slug, sub, granted_by))


def remove_tenant_admin(slug: str, sub: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tenant_admins WHERE slug = %s AND sub = %s",
                           (slug, sub))
        return (cur.rowcount or 0) > 0


# ── Désactiver un tenant (oto-backend#1165) ──────────────────────────────────
#
# L'état vit sur la ligne (`disabled_at`, `disabled_by`, `disabled_reason`), JAMAIS dans
# l'absence d'émetteur : vider `issuer` ne coupait que les jetons signés par l'annuaire
# du tenant, dans le seul processus rechargé — les jetons d'API, de délégation et les
# anciens identifiants redirigés vers ses comptes continuaient de servir. Le prédicat est
# lu à CHAQUE vérification d'identité (`garde_identite`), sans cache : une désactivation
# mord à la requête suivante, dans tous les processus, jeton déjà émis compris.

class TenantDesactive(RuntimeError):
    """Le geste refusé porte sur un compte d'un tenant DÉSACTIVÉ (`tenants.disabled_at`).

    Levée par `users.upsert_user` quand elle ferait NAÎTRE un compte qualifié sous un
    tenant désactivé : un annuaire qui continue d'émettre des jetons ne crée plus rien
    chez nous. Exception plutôt que valeur falsy pour la même raison que
    `CompteEnPause` : les appelants d'`upsert_user` ignorent sa valeur de retour."""

    code = "tenant_disabled"

    def __init__(self, slug: str, motif: str, quoi: str):
        # Le motif reste à l'exploitant (journal) : le message part au PORTEUR, un compte
        # du tenant — même règle que `tenant_desactive.message`.
        self.slug, self.motif = slug, motif
        super().__init__(f"{quoi} : l'accès par l'espace « {slug} » est désactivé — "
                         "le rétablissement est un acte de l'exploitant de la plateforme")


_DESACTIVATION_COLS = "slug, disabled_at, disabled_by, disabled_reason"

# Le tenant DÉSACTIVÉ qui qualifie un sub : `<slug>:` en tête, mot pour mot. Un slug ne
# contient pas de `:` (`tenancy._SLUG_RE`) et un sub Logto non plus : la comparaison de
# préfixe classe sans ambiguïté, sans jamais découper le sub. Lue dans la BASE et pas
# dans le registre du processus : un tenant dont l'émetteur a été retiré du registre doit
# rester refusé, et un processus qui n'a pas été rechargé aussi.
_TENANT_DESACTIVE_DU_SUB_SQL = f"""
    SELECT {_DESACTIVATION_COLS} FROM tenants
     WHERE disabled_at IS NOT NULL
       AND left(%(sub)s, length(slug) + 1) = slug || ':'
     LIMIT 1
"""


def tenant_desactive_du_sub(sub: str) -> "dict | None":
    """L'état de désactivation du tenant qui qualifie `sub`, ou `None` (cas de tous).

    SOURCE UNIQUE du prédicat servi : le seam `tenant_desactive` est le seul appelant
    prévu. Ne rattrape rien — un hoquet de base REMONTE : rendre `None` servirait un
    compte coupé comme s'il était vivant."""
    with _connect() as conn:
        row = conn.execute(_TENANT_DESACTIVE_DU_SUB_SQL, {"sub": sub}).fetchone()
    return dict(row) if row else None


def tenant_ligne(slug: str) -> "dict | None":
    """`{id, slug, disabled_at}` d'un tenant, ou `None` s'il n'existe pas — la garde du
    geste de désactivation (inconnu ⇒ 404, ligne 1 ⇒ refus)."""
    with _connect() as conn:
        row = conn.execute("SELECT id, slug, disabled_at FROM tenants WHERE slug = %s",
                           (slug,)).fetchone()
    return dict(row) if row else None


def tenant_desactive_de_l_org(conn, org_id: int) -> "dict | None":
    """`{slug, disabled_reason}` du tenant DÉSACTIVÉ auquel l'org est rattachée
    (`orgs.tenant_id`), `None` sinon. Lue dans la connexion de l'appelant :
    `org_store.resume_org` la lit sous le verrou de la ligne de l'org qu'il va lever.

    Une des deux lectures du rattachement que fait la désactivation (avec le balayage
    de `desactiver_tenant`), gardées ici, dans le module de suivi que
    `tests/test_tenant_l1_migration.py` admet comme lecteur."""
    row = conn.execute(
        "SELECT t.slug, t.disabled_reason FROM orgs o "
        "JOIN tenants t ON t.id = o.tenant_id AND t.disabled_at IS NOT NULL "
        "WHERE o.id = %s", (org_id,)).fetchone()
    return dict(row) if row else None


def desactiver_tenant(slug: str, *, by: str, reason: str) -> "dict | None":
    """Désactive un tenant ET révoque ce qui est émis pour ses comptes, en UNE
    transaction. `None` si le slug n'existe pas.

    1. **L'état** : `disabled_*` posés. Re-désactiver ne réécrit RIEN (ni l'auteur, ni la
       date, ni le motif d'origine) — `changed` dit si CE geste a posé l'état.
    2. **La révocation** des jetons que NOUS émettons et stockons (`user_api_tokens`,
       jetons d'API comme jetons de délégation d'un travail d'agent) de TOUS les comptes
       qualifiés sous le tenant, vivants et non expirés : `revoked_*` posés, la ligne
       reste (#523). Rejouée à chaque geste, même sur un tenant déjà désactivé : elle
       ramasse ce qui aurait été émis depuis (un travail réservé entre-temps).
    3. **Les comptes et les anciens identifiants coupés**, comptés pour le rapport : ce
       que la garde d'identité refuse désormais à chaque requête.
    4. **Ses orgs suspendues** (`orgs.tenant_id`), par la suspension d'org existante
       (`org_suspension`) : plus personne n'y agit, membre d'un autre tenant compris,
       ses projets publiés ne sont plus servis, ses automatisations n'enfilent ni ne
       réservent plus rien. Chaque suspension posée ICI porte son origine
       (`suspended_tenant_id`), que `reactiver_tenant` lève, et elle seule ; une org
       déjà suspendue pour une autre raison n'est pas touchée (comptée à part). Rejouée
       à chaque geste comme la révocation : elle rattrape une org née depuis, ou un
       tenant désactivé avant que le geste ne suspende ses orgs.

    Ce qui n'est PAS stocké ici ne se révoque pas ici : les jetons signés par l'annuaire
    du tenant (session de tableau de bord, jeton d'accès et de rafraîchissement OAuth du
    MCP) sont des JWT sans état chez nous. Ils restent signés jusqu'à leur expiration —
    et sont refusés par `garde_identite` à chaque présentation."""
    pfx = f"{slug}:%"
    motif_jeton = f"tenant {slug} désactivé : {reason}"
    with _connect() as conn:
        avant = conn.execute("SELECT id, disabled_at FROM tenants WHERE slug = %s "
                             "FOR UPDATE", (slug,)).fetchone()
        if avant is None:
            return None
        etat = conn.execute(
            "UPDATE tenants SET disabled_at = COALESCE(disabled_at, NOW()), "
            "  disabled_by = COALESCE(disabled_by, %s), "
            "  disabled_reason = COALESCE(disabled_reason, %s) "
            f"WHERE slug = %s RETURNING {_DESACTIVATION_COLS}",
            (by, reason, slug)).fetchone()
        revoques: dict[str, int] = {}
        for r in conn.execute(
                "UPDATE user_api_tokens SET revoked_at = NOW(), revoked_by = %s, "
                "  revoked_reason = %s "
                "WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW()) "
                "  AND sub LIKE %s RETURNING kind",
                (by, motif_jeton, pfx)).fetchall():
            revoques[r["kind"]] = revoques.get(r["kind"], 0) + 1
        comptes = conn.execute("SELECT COUNT(*) AS n FROM users WHERE sub LIKE %s",
                               (pfx,)).fetchone()["n"]
        alias = conn.execute("SELECT COUNT(*) AS n FROM sub_aliases WHERE new_sub LIKE %s",
                             (pfx,)).fetchone()["n"]
        orgs = [int(r["id"]) for r in conn.execute(
            "UPDATE orgs SET suspended_at = NOW(), suspended_by = %s, "
            "  suspended_reason = %s, suspended_tenant_id = %s "
            "WHERE tenant_id = %s AND suspended_at IS NULL RETURNING id",
            (by, motif_jeton, avant["id"], avant["id"])).fetchall()]
        deja = conn.execute(
            "SELECT COUNT(*) AS n FROM orgs WHERE tenant_id = %s "
            "AND suspended_at IS NOT NULL AND suspended_tenant_id IS DISTINCT FROM %s",
            (avant["id"], avant["id"])).fetchone()["n"]
    return {**dict(etat), "changed": avant["disabled_at"] is None,
            "revoked": revoques, "accounts_cut": int(comptes), "aliases_cut": int(alias),
            "orgs_suspended": sorted(orgs), "orgs_already_suspended": int(deja)}


def reactiver_tenant(slug: str) -> "dict | None":
    """Lève la désactivation ET les suspensions d'org que SA désactivation a posées
    (`orgs.suspended_tenant_id`), en UNE transaction. `None` si le slug n'existe pas ;
    sinon `changed` (la désactivation était posée) et `orgs_resumed` (les orgs
    rouvertes). Une org suspendue pour une autre raison le reste. Ne rétablit AUCUN
    jeton : la révocation est définitive, les personnes se reconnectent."""
    with _connect() as conn:
        avant = conn.execute("SELECT id, disabled_at FROM tenants WHERE slug = %s "
                             "FOR UPDATE", (slug,)).fetchone()
        if avant is None:
            return None
        conn.execute(
            "UPDATE tenants SET disabled_at = NULL, disabled_by = NULL, "
            "  disabled_reason = NULL WHERE id = %s", (avant["id"],))
        orgs = [int(r["id"]) for r in conn.execute(
            "UPDATE orgs SET suspended_at = NULL, suspended_by = NULL, "
            "  suspended_reason = NULL, suspended_tenant_id = NULL "
            "WHERE suspended_tenant_id = %s RETURNING id", (avant["id"],)).fetchall()]
    return {"changed": avant["disabled_at"] is not None, "orgs_resumed": sorted(orgs)}


# ── Suivi (console plateforme) ───────────────────────────────────────────────

# Le tenant d'un sub, EN SQL — miroir de `tenancy.IssuerRegistry.tenant_of` : le
# préfixe déclaré le plus long qui matche, sinon le tenant primaire (sub nu).
#
# ⚠️ Deux différences assumées avec le registre du process, toutes deux dans le sens
# du suivi : ici on classe sur les tenants de la BASE (le registre, lui, ignore une
# ligne sans émetteur), et le tenant primaire est la LIGNE `oto`, pas l'env. Un
# compte qualifié sous un tenant dont l'émetteur n'est pas encore déclaré est donc
# COMPTÉ ici alors que ses jetons sont rejetés — c'est exactement l'état qu'un suivi
# doit montrer, pas masquer.
_SUB_TENANT_SQL = """
    SELECT u.sub, u.created_at,
           COALESCE((SELECT p.id FROM pref p
                      WHERE u.sub LIKE p.p || '%%'
                      ORDER BY length(p.p) DESC LIMIT 1),
                    (SELECT id FROM tenants WHERE slug = %(primary)s)) AS tenant_id
      FROM users u
"""

# `slug` ne contient ni `%` ni `_` (`tenancy._SLUG_RE`), donc le `LIKE` ci-dessus
# compare bien un préfixe littéral — pas un motif que le slug pourrait ouvrir.
_TENANT_PREF_SQL = "SELECT id, slug || ':' AS p FROM tenants WHERE slug <> %(primary)s"


def _tenant_counts_sql(appels_sql: str, where_tenant: str = "") -> str:
    """Les compteurs d'un tenant, une passe. `appels_sql` : la source des appels de la
    fenêtre (`journal_jour.source`, totaux par jour + journal direct, #1147).
    `where_tenant` borne à un tenant."""
    return f"""
    WITH pref AS ({_TENANT_PREF_SQL}),
         sub_tenant AS ({_SUB_TENANT_SQL}),
         org_counts AS (
             SELECT tenant_id,
                    COUNT(*) FILTER (WHERE archived_at IS NULL) AS orgs,
                    COUNT(*) FILTER (WHERE archived_at IS NOT NULL) AS orgs_archivees
               FROM orgs GROUP BY tenant_id
         ),
         acct_counts AS (
             SELECT tenant_id, COUNT(*) AS comptes, MAX(created_at) AS dernier_compte_at
               FROM sub_tenant GROUP BY tenant_id
         ),
         call_counts AS (
             -- kind='mcp' : le trafic d'OUTILS, iso avec le reste du monitoring
             -- (`rest`/`protocol`/`connector` mesurent autre chose).
             SELECT st.tenant_id,
                    SUM(c.appels)::bigint AS appels,
                    COUNT(DISTINCT c.sub) AS comptes_actifs,
                    MAX(c.dernier_at) AS last_seen_at
               FROM ({appels_sql}) c JOIN sub_tenant st ON st.sub = c.sub
              GROUP BY st.tenant_id
         ),
         drift AS (
             -- L'écart entre les deux sources : une org rattachée à ce tenant dont
             -- le CRÉATEUR est qualifié sous un autre. `created_by` est le seul sub
             -- que porte la table `orgs` — l'appartenance vit dans `org_members`,
             -- donc c'est une SONDE, pas un recensement (elle ne voit pas une org
             -- dont seuls les membres ont basculé).
             SELECT o.tenant_id, COUNT(*) AS orgs_desalignees
               FROM orgs o JOIN sub_tenant st ON st.sub = o.created_by
              WHERE o.archived_at IS NULL AND o.tenant_id <> st.tenant_id
              GROUP BY o.tenant_id
         )
    SELECT t.id, t.slug, t.name, t.issuer, t.jwks_uri, t.hosts, t.oauth_client_id,
           t.dashboard_url, t.link_paths, t.tool_prefix, t.brand, t.logto_mgmt,
           t.created_at, t.disabled_at, t.disabled_by, t.disabled_reason,
           COALESCE(oc.orgs, 0) AS orgs,
           COALESCE(oc.orgs_archivees, 0) AS orgs_archivees,
           COALESCE(ac.comptes, 0) AS comptes,
           ac.dernier_compte_at,
           COALESCE(cc.appels, 0) AS appels,
           COALESCE(cc.comptes_actifs, 0) AS comptes_actifs,
           cc.last_seen_at,
           COALESCE(dr.orgs_desalignees, 0) AS orgs_desalignees
      FROM tenants t
      LEFT JOIN org_counts oc ON oc.tenant_id = t.id
      LEFT JOIN acct_counts ac ON ac.tenant_id = t.id
      LEFT JOIN call_counts cc ON cc.tenant_id = t.id
      LEFT JOIN drift dr ON dr.tenant_id = t.id
     {where_tenant}
     ORDER BY t.id
    """


# Le tenant EFFECTIF d'une org, en UNE requête — l'UNION des trois axes qui peuvent
# la rattacher à un tenant tiers, dans l'ordre du plus déclaré au plus dérivé. Union
# et non « le meilleur axe » : chacun a un angle mort connu, et le coût d'un faux
# négatif (traiter l'org d'un partenaire comme la nôtre) est ce qu'on refuse.
#
#   1. `orgs.tenant_id` — le rattachement DÉCLARÉ (ADR 0052 L1). ⚠️ **Il a été inerte
#      jusqu'au 2026-09-03, il ne l'est plus.** Mesuré vide le 2026-09-02 (160 orgs
#      sur 160 portant le tenant primaire, dont les 61 qui vivaient chez un
#      partenaire) ; ALIMENTÉ le 2026-09-03 par `scripts/migrate_org_tenant --apply`
#      — 65 orgs repointées, `orgs_desalignees` de 48 à 0 — et posé à la naissance
#      par `org_store.create_org` depuis le même jour. Cet axe porte, désormais.
#      ⚠️ Ce qui ne fait PAS de lui un axe suffisant seul, et c'est pourquoi les deux
#      autres restent : il est ÉCRIT par quelqu'un, quand les deux suivants se
#      DÉRIVENT de l'émetteur du jeton à chaque lecture. Un écrivain peut cesser
#      d'écrire sans bruit — c'est exactement ce qui a produit le trou d'origine.
#   2. `orgs.front_brand` — le front qui HÉBERGE l'org, dérivé de l'émetteur du jeton
#      de son créateur à l'INSERT (`config.front_for`, écrivain unique dans
#      `org_store.create_org`). Non déclarable par l'appelant, donc non revendicable.
#      C'est l'axe qui porte. ⚠️ Son angle mort est historique : avant que la
#      dérivation soit confiée à l'écrivain unique, deux des trois créateurs d'org
#      repartaient à NULL — des orgs de partenaire ont donc pu naître sans marque.
#   3. Le PRÉFIXE du sub d'un membre (`tenancy.qualify` : `<slug>:<sub>`), qui suit
#      l'émetteur du jeton et rien d'autre. C'est lui qui couvre l'angle mort de (2).
#      ⚠️ Son propre angle mort : une org de partenaire sans aucun membre qualifié
#      (invitée depuis notre front) — que (2) couvre. Les deux se tiennent.
#
# Croisement mesuré le 2026-09-02 sur les 160 orgs : (2) et (3) rendent le MÊME
# ensemble de 61 orgs, zéro désaccord dans les deux sens. Deux dérivations
# indépendantes qui concordent, c'est ce qui permet d'affirmer l'absence de faux
# négatif aujourd'hui ; l'union est ce qui la maintient demain.
#
# L'EXPRESSION est exportée à part de la requête : tout dispositif qui trie une
# POPULATION d'orgs (et pas une seule) doit trancher avec exactement les mêmes trois
# axes — sinon deux définitions du « chez le partenaire » divergent, et la seconde
# sera la moins prudente. Aujourd'hui : `db/outreach.py` (l'audience d'une relance).
# `o` est l'alias attendu pour `orgs`, `%(primary)s` le slug du tenant primaire.
# Les trois axes, NOMMÉS un par un — parce que deux expressions différentes en ont
# besoin, et qu'elles ne prennent pas les mêmes. Les recopier serait la voie normale
# vers deux définitions du « chez le partenaire » qui divergent en silence.
_AXE_DECLARE = """(SELECT t.slug FROM tenants t
        WHERE t.id = o.tenant_id AND t.slug <> %(primary)s)"""
_AXE_MARQUE = """NULLIF(btrim(COALESCE(o.front_brand, '')), '')"""
_AXE_MEMBRE = """(SELECT p.slug
         FROM org_members om
         JOIN (SELECT slug, slug || ':' AS pfx FROM tenants
                WHERE slug <> %(primary)s) p ON om.sub LIKE p.pfx || '%%'
        WHERE om.org_id = o.id
        ORDER BY length(p.pfx) DESC LIMIT 1)"""

_ORG_TENANT_EXPR = f"""COALESCE(
      {_AXE_DECLARE},
      {_AXE_MARQUE},
      {_AXE_MEMBRE},
      %(primary)s)"""

_ORG_TENANT_SQL = f"""
    SELECT {_ORG_TENANT_EXPR} AS slug
      FROM orgs o WHERE o.id = %(oid)s
"""


def org_tenant_slug(org_id: int, conn=None) -> str:
    """Le tenant EFFECTIF d'une organisation : `'oto'` (la nôtre) ou le slug du
    tenant tiers qui l'héberge. Union des trois axes ci-dessus, sans arbitrage.

    Sert à répondre à « cette organisation est-elle NOTRE cliente, ou celle d'un
    partenaire hébergé ? » — la question que doit poser tout dispositif qui
    S'ADRESSE au titulaire de l'org (badge, échéance, relance). Les clients d'un
    partenaire ne sont pas les nôtres : leur écrire dans son produit, c'est parler
    par-dessus lui.

    Org inconnue ⇒ tenant primaire : il n'y a personne à protéger derrière un id qui
    ne désigne rien, et rendre un tiers ici masquerait le vrai défaut (l'id est faux).
    Le fail-closed sur erreur appartient à l'appelant, pas à la lecture.

    ⚠️ Ce n'est PAS `front_brand` : la marque n'est qu'un des trois axes, et l'axe
    qui a un trou historique. Lire la colonne en direct rouvrirait ce trou.

    `conn` : une connexion DÉJÀ ouverte par l'appelant, pour lire le tenant dans la
    même que ses propres lectures (le cran tenant de l'activation, lu à chaque appel
    d'outil de connecteur) ; sans elle, la lecture ouvre la sienne.
    """
    if conn is not None:
        return _lire_org_tenant(conn, org_id)
    with _connect() as c:
        return _lire_org_tenant(c, org_id)


def _lire_org_tenant(conn, org_id: int) -> str:
    row = conn.execute(_ORG_TENANT_SQL,
                       {"primary": tenancy.primary_slug(), "oid": int(org_id)}).fetchone()
    return (row and row["slug"]) or tenancy.primary_slug()


def _shape_tenant(row: dict) -> dict:
    """Forme servie : les compteurs en entiers, et l'ÉTAT D'ÉMETTEUR dérivé ici —
    une seule dérivation, partagée par la liste et la fiche."""
    out = dict(row)
    issuer = (out.get("issuer") or "").strip()
    primaire = out.get("slug") == tenancy.primary_slug()
    # Ce que le tenant peut faire, tel que le registre le verra au prochain boot :
    # le primaire tient son émetteur de l'ENV (une ligne le redéclarant est ignorée),
    # les autres n'authentifient que si leur ligne porte un émetteur.
    out["issuer_source"] = "env" if primaire else ("db" if issuer else None)
    out["authenticates"] = bool(primaire or issuer)
    out["primary"] = primaire
    out["hosts"] = list(out.get("hosts") or [])
    out["link_paths"] = dict(out.get("link_paths") or {})
    # DÉCLARÉ, et sans secret : `credential` est un NOM de variable d'environnement.
    # Ce que le process peut vraiment faire de cet annuaire se lit sur le registre
    # (`tenants_admin._decorate` → `directory_admin`) — un accès déclaré ici mais dont
    # la clé n'est pas injectée s'affiche donc sans être utilisable, et c'est
    # exactement l'écart qu'un suivi doit montrer.
    mgmt = out.get("logto_mgmt")
    out["logto_mgmt"] = dict(mgmt) if isinstance(mgmt, dict) else {}
    # DÉCLARÉ seulement : ce que le process applique vraiment se lit sur le registre
    # (`tenants_admin._decorate` → `tool_prefix_effectif`). Un préfixe posé en base
    # après le dernier boot, ou refusé par `tool_alias.normalize_prefix`, s'affiche
    # ici sans être servi — et c'est justement l'écart qu'un suivi doit montrer.
    out["tool_prefix"] = (out.get("tool_prefix") or None)
    for k in ("orgs", "orgs_archivees", "comptes", "comptes_actifs", "appels",
              "orgs_desalignees"):
        out[k] = int(out.get(k) or 0)
    return out


def list_tenants_overview(*, days: int = 30) -> list[dict]:
    """Tous les tenants + leur empreinte sur une fenêtre (défaut 30 j).

    Une ligne par tenant DÉCLARÉ, y compris ceux à zéro compte : un tenant provisionné
    dont personne ne s'est encore connecté est ce qu'on veut le plus voir.
    """
    objet = "vue des tenants"
    with lecture_d_agregat(objet) as conn:
        appels, params = journal_jour.source(
            conn, objet, kinds=("mcp",), mesures=("appels", "dernier_at"),
            jours=int(days))
        rows = conn.execute(_tenant_counts_sql(appels),
                            {**params, "primary": tenancy.primary_slug()}).fetchall()
    return [_shape_tenant(r) for r in rows]


def get_tenant_overview(slug: str, *, days: int = 30) -> dict | None:
    """La fiche d'un tenant : ses compteurs + les listes qui les expliquent.

    `None` si le slug n'existe pas — l'appelant en fait un 404, jamais une fiche vide
    (qui se lirait comme « ce tenant existe et n'a rien »).

    UN chemin, borné à SES comptes dès la première lecture (`_overview_par_comptes`) :
    mesuré le 25/09/2026 sur la console d'un tenant tiers, **97 s en moyenne, p95 à
    128 s** par la passe générique ; le 04/10/2026, la fiche du primaire prenait encore
    ~150 s — la passe générique classait chaque utilisateur de la plateforme par
    sous-requête corrélée, puis lisait 30 jours du journal ENTIER deux fois
    (compteurs, puis par compte) pour ne garder qu'un tenant. Les chiffres rendus sont
    les mêmes que la ligne de la liste : `tests/test_tenants_overview_pg.py` compare.
    """
    return _overview_par_comptes(slug, days=days,
                                 primaire=(slug == tenancy.primary_slug()))


_TENANT_ROW_SQL = """
    SELECT id, slug, name, issuer, jwks_uri, hosts, oauth_client_id, dashboard_url,
           link_paths, tool_prefix, brand, logto_mgmt, created_at,
           disabled_at, disabled_by, disabled_reason
      FROM tenants WHERE slug = %(slug)s
"""

# Le tenant du CRÉATEUR d'une org, pour l'écart : le préfixe déclaré le plus long qui
# matche, sinon le primaire — même règle que `_SUB_TENANT_SQL`, appliquée à UN sub.
_TENANT_DU_CREATEUR_SQL = """
    COALESCE((SELECT t2.slug FROM tenants t2
               WHERE t2.slug <> %(primary)s AND u.sub LIKE t2.slug || ':%%'
               ORDER BY length(t2.slug) DESC LIMIT 1),
             %(primary)s)
"""


def _overview_par_comptes(slug: str, *, days: int = 30, primaire: bool) -> dict | None:
    """La fiche d'un tenant, bornée à SES comptes dès la première lecture.

    Ordre des lectures : la ligne du tenant → ses utilisateurs → le journal d'appels
    sur la fenêtre, en UNE requête groupée par sub (les compteurs et la liste par
    compte en sortent ensemble, là où la passe générique lisait le journal deux fois)
    → ses orgs → l'écart.

    - Un tenant **tiers** : ses comptes portent le préfixe `<slug>:` (quelques
      dizaines), et le journal se lit **de ces subs seulement** (`idx_tool_calls_sub`)
      — aucune lecture ne touche les comptes ou les appels d'un autre tenant.
    - Le tenant **primaire** : ses comptes sont les subs NUS (sans préfixe d'un tenant
      tiers), c'est-à-dire presque tous — lire « leurs » appels par sub serait plus
      cher que lire la fenêtre une fois. Le journal se lit donc par la fenêtre, groupé
      par sub, et les subs d'autres tenants sont écartés au rapprochement. Une passe au
      lieu de deux, sans sous-requête corrélée par utilisateur ; elle reste une lecture
      de toute la fenêtre de la plateforme, bornée par `lecture_d_agregat`.

    ⚠️ Un slug ne contient ni `%` ni `_` (`tenancy._SLUG_RE`) : le `LIKE` compare un
    préfixe littéral. Et aucun slug n'en préfixe un autre suivi de `:` (le `:` est
    interdit dans un slug), donc « commence par `<slug>:` » classe sans ambiguïté —
    la règle du plus long préfixe de `_SUB_TENANT_SQL` n'a rien à départager ici.
    """
    params = {"primary": tenancy.primary_slug(), "days": int(days), "slug": slug,
              "pfx": f"{slug}:", "cap": _TENANT_LIST_CAP}
    # Les comptes du tenant, et l'écart (une org du tenant dont le créateur n'est PAS
    # l'un d'eux) : les deux seules lectures qui diffèrent entre tiers et primaire.
    nus = f"NOT EXISTS (SELECT 1 FROM ({_TENANT_PREF_SQL}) p WHERE u.sub LIKE p.p || '%%')"
    siens = nus if primaire else "u.sub LIKE %(pfx)s || '%%'"
    objet = "fiche du tenant primaire" if primaire else "fiche d'un tenant"
    with lecture_d_agregat(objet) as conn:
        row = conn.execute(_TENANT_ROW_SQL, params).fetchone()
        if row is None:
            return None
        fiche = dict(row)
        params["tid"] = fiche["id"]

        comptes = [dict(r) for r in conn.execute(
            f"""
            SELECT u.sub, u.email, u.name, u.role, u.created_at
              FROM users u WHERE {siens}
             ORDER BY u.created_at DESC
            """, params).fetchall()]
        params["subs"] = [c["sub"] for c in comptes]
        # Tiers : les appels de SES subs. Primaire : la fenêtre, groupée par sub — les
        # subs d'un autre tenant ne trouvent pas de compte au rapprochement ci-dessous.
        # Lu sur les totaux par jour (#1147) : la fenêtre glissante, jours consolidés et
        # journal direct pour le reste ; `sub` est une dimension.
        par_sub: dict = {}
        if params["subs"]:
            appels, pa = journal_jour.source(
                conn, objet, kinds=("mcp",), mesures=("appels", "dernier_at"),
                filtres={"subs": None if primaire else params["subs"]}, jours=int(days))
            par_sub = {r["sub"]: r for r in conn.execute(
                f"""
                SELECT sub, SUM(appels)::bigint AS appels, MAX(dernier_at) AS last_seen_at
                  FROM ({appels}) c
                 GROUP BY sub
                """, pa).fetchall()}
        for c in comptes:
            a = par_sub.get(c["sub"])
            c["appels"] = int(a["appels"]) if a else 0
            c["last_seen_at"] = a["last_seen_at"] if a else None
        # Les plus actifs d'abord, puis les plus récents : déjà triés par date, un
        # tri STABLE par appels garde cet ordre entre égaux (même ORDER BY que la
        # passe générique).
        comptes.sort(key=lambda c: c["appels"], reverse=True)

        oc = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE archived_at IS NULL) AS orgs,
                   COUNT(*) FILTER (WHERE archived_at IS NOT NULL) AS orgs_archivees
              FROM orgs WHERE tenant_id = %(tid)s
            """, params).fetchone()

        orgs_recentes = [dict(r) for r in conn.execute(
            """
            SELECT o.id, o.name, o.created_at, o.archived_at, o.personal_of IS NOT NULL
                   AS personal, o.front_base_url, o.front_brand,
                   (SELECT COUNT(*) FROM org_members m WHERE m.org_id = o.id) AS membres
              FROM orgs o WHERE o.tenant_id = %(tid)s
             ORDER BY o.archived_at IS NOT NULL, o.created_at DESC LIMIT %(cap)s
            """, params).fetchall()]

        # L'écart : une org rattachée à CE tenant dont le créateur n'est pas qualifié
        # sous son préfixe (un sub nu, ou celui d'un autre tenant). Même sonde que la
        # passe générique (`created_by`, joint sur `users`), lue sans limite pour le
        # COMPTE, bornée pour le détail.
        ecart = [dict(r) for r in conn.execute(
            f"""
            SELECT o.id, o.name, o.created_by, {_TENANT_DU_CREATEUR_SQL} AS tenant_du_createur
              FROM orgs o JOIN users u ON u.sub = o.created_by
             WHERE o.tenant_id = %(tid)s AND o.archived_at IS NULL
               AND NOT ({siens})
             ORDER BY o.id
            """, params).fetchall()]

    fiche.update({
        "orgs": oc["orgs"], "orgs_archivees": oc["orgs_archivees"],
        "comptes": len(comptes),
        "dernier_compte_at": max((c["created_at"] for c in comptes if c["created_at"]),
                                 default=None),
        "appels": sum(c["appels"] for c in comptes),
        "comptes_actifs": sum(1 for c in comptes if c["appels"]),
        "last_seen_at": max((c["last_seen_at"] for c in comptes if c["last_seen_at"]),
                            default=None),
        "orgs_desalignees": len(ecart),
    })
    fiche = _shape_tenant(fiche)
    fiche["orgs_recentes"] = orgs_recentes
    fiche["comptes_recents"] = comptes[:_TENANT_LIST_CAP]
    fiche["orgs_desalignees_detail"] = ecart[:_TENANT_LIST_CAP]
    return fiche
