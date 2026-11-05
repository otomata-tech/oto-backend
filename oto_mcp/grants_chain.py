"""La clé plateforme entre dans le modèle d'accès par chaîne de grants (ADR 0053, L5).

**Ce que ce module remplace.** Aujourd'hui, le droit d'utiliser une clé plateforme
est écrit DANS la ligne du coffre : `share_mode`/`share_down` (qui a le droit) et
`meta.rate_limit_by` (combien). C'est la cause racine nommée par 0053 §1 — une règle
commerciale codée à l'intérieur d'une résolution de credential — et c'est ce qui a
brûlé le 31/07 : poser un grant individuel a basculé la clé partagée `open`→`closed`
avec une allowlist d'une personne, la fermant **pour tous les autres**.

**Ce qu'il installe.** Une arête `plateforme —grant→ bénéficiaire` dans la table
`grants`, portant sa contrainte. Poser un grant AJOUTE une arête ; il ne retire rien
à personne. L'incident du 31/07 devient structurellement impossible : il n'existe
plus d'écriture qui, en accordant à l'un, retire à l'autre.

**Un connecteur à la fois** (`CHAIN_CONNECTORS`). Les autres ne voient pas une ligne
de ce module s'exécuter — ni requête, ni branche : le gate est la PREMIÈRE ligne de
chaque entrée publique.

**Les trois états de la chaîne**, et c'est le cœur du lot :

| état | condition | effet |
|---|---|---|
| ACCORDE | ≥1 arête vivante pour (instance, bénéficiaire) | la chaîne résout — clé + quota de l'arête |
| REFUSE | des arêtes existent, toutes révoquées | accès **coupé**, sans repli |
| MUETTE | aucune arête, jamais | repli sur l'ancien chemin, **à l'identique** |

L'état MUET est ce qui rend la bascule sûre : personne ne perd d'accès le jour du
déploiement. Là où la chaîne a un avis (ACCORDE ou REFUSE), l'ancien chemin n'est plus
lu (R6). L'état REFUSE est ce qui rend la révocation vraie —
sans lui, révoquer une arête laisserait l'ancien chemin free-tier re-accorder aussitôt,
et « la révocation coupe l'accès » serait faux. Ensemble, ils disent l'extinction de
`platform_key_open` pour ce connecteur : **pour qui la chaîne connaît, l'ouverture
implicite n'existe plus.**

**Réversibilité.** Retirer les arêtes (DELETE) ramène tout à l'état MUET, donc à
l'ancien chemin exact : rien de l'existant n'a été supprimé (`share_down`,
`meta.rate_limit_by` et le flag DB `share_mode` sont intacts). ⚠️ La FK de
`grant_counters` (sans CASCADE, délibérément) refuse la suppression d'une arête déjà
comptée : un rollback supprime les compteurs d'abord. Retirer le connecteur de
`CHAIN_CONNECTORS` suffit à défaire le lot côté code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import credentials_store, instance_refs
from .db import _connect
from .db import grants as db_grants

logger = logging.getLogger(__name__)

# ── Le périmètre du lot ────────────────────────────────────────────────────────
# **fullenrich, et lui seul.** Choisi au plus petit rayon, mesuré sur la prod le
# 12/08/2026 (30 jours) parmi les dix connecteurs à `platform_key_open` :
#
#   connecteur   appels 30j   appels comptés   bénéficiaires   arêtes à migrer
#   serper           9 759            9 417         8                    11
#   unipile          3 616                —         4                     —
#   hunter           2 274            2 192         3                     7
#   apollo             484              168         4                     2
#   serpapi            117              117         2                     3
#   kaspr               24                1         3                     4
#   fullenrich          15                6         2                     2
#   reddit               6                0         1                     0
#   searchapi            2                0         1                (pas de clé)
#
# `fullenrich` est le plus petit qui exerce encore TOUT le mécanisme : il a une clé
# plateforme, des grants, et un quota. `reddit`/`searchapi` sont plus petits mais
# n'ont aucune arête à migrer — ils ne prouveraient rien. Et le fait décisif : les
# deux seuls sujets à avoir appelé fullenrich en trois mois sont **exactement** les
# deux qui portent un grant. Le rayon réel est nul.
#
# **Vague 2 (23/08, GO Alexis « tout ce qui est dévable »)** : les connecteurs à
# clé plateforme ET grants passent à la chaîne — leurs arêtes sont semées au boot
# (`_seed_platform_grants_as_edges`), leurs grants/révocations passent par ici,
# leur usage débite l'arête. **À la différence du pilote, leur `platform_key_open`
# n'est PAS éteint** : le rayon de fullenrich avait été mesuré NUL (les deux seuls
# appelants étaient les deux grantees), celui de serper ne l'est pas (9 759 appels
# /30 j). Couper un free-tier se décide connecteur par connecteur, mesure en main —
# jusqu'à cette décision, un appelant SANS arête retombe sur le chemin ouvert
# (état MUET, identique), et un appelant à arête RÉVOQUÉE est coupé (la révocation
# devient vraie, c'est le but). Restent dehors : `unipile` (son mode plateforme est
# gouverné par option comp + comptes opérés, pas par share_down), `searchapi` (pas
# de clé plateforme) et `sirene` (idem).
CHAIN_CONNECTORS = frozenset({
    "fullenrich",                                     # pilote (12/08), flag éteint
    "serper", "hunter", "apollo", "serpapi", "kaspr", # vague 2 — flag intact
    "reddit",                                         # vague 2 — 0 arête, no-op prouvé
})

# Le propriétaire d'une clé plateforme (0053-D3 : « la plateforme est un scope
# propriétaire comme les autres »). Convention maison — `platform` n'a pas d'id,
# comme `guides.owner_id`.
PLATFORM_SCOPE = ("platform", "platform")

# ── L'arête « TOUT LE MONDE » (lot L7, arbitrage du 29/08) ────────────────────
# 0053 n'avait pas de bénéficiaire « tout le monde », et c'était le seul vrai trou du
# modèle : une clé plateforme OUVERTE (`share_mode='open'`, `share_down` vide) accorde
# à quiconque, ce qu'aucune arête ne savait dire.
#
# Le mot existait déjà, il n'a pas fallu l'inventer : la visibilité dérivée (R9, retirée
# par #1499) appelait EVERYONE le scope `platform` depuis R9. On le reprend tel quel — le CHECK de
# `grants.grantee_kind` accepte déjà `platform`, donc **aucune migration de schéma**.
# L'arête est `platform:platform → platform:platform` sur l'instance : le propriétaire
# accorde à tout le monde, ce qui est exactement ce que la ligne du coffre disait.
#
# ⚠️ **Elle ne se lit PAS sur le chemin servi d'aujourd'hui.** `platform_rung` (la
# fenêtre L5) ne la voit pas : lui faire voir une arête « tout le monde » ferait
# ressusciter un accès individuel RÉVOQUÉ sur une clé ouverte, et « la révocation
# coupe » — l'acquis de L5 — deviendrait faux en silence. Elle n'est lue que par la
# résolution de chaîne du lot L7, et là avec sa règle propre : une arête qui NOMME
# l'appelant prime, révoquée comprise, et c'est elle qui refuse.
EVERYONE = ("platform", "platform")


def is_chained(provider: str) -> bool:
    """Ce connecteur est-il passé au modèle d'accès par chaîne ? Gate unique."""
    return provider in CHAIN_CONNECTORS


# ── Désignation d'une instance de clé plateforme ───────────────────────────────
# `grants.resource_id` est un TEXT (0053 §4 l'a voulu ainsi : dès que `resource_kind`
# varie, la forme de l'id varie). L'instance, elle, n'a PAS encore d'identifiant
# stable — c'est le lot L6, bloqué par l'arbitrage R1.
#
# On n'invente donc PAS de désignation : `instance_refs` est le **domicile unique du
# format de ref** dans ce dépôt (ADR 0038 §B) — `platform:{connector}:{label}`, avec
# ses segments percent-encodés (un label PEUT contenir un `:` : `zoho` porte
# `editor:eu` en prod). Et ce choix se paie tout seul le jour de L6 : le plan de
# survie de `instance_refs` prévoit déjà que `parse_ref` accepte `inst:{id}` — le
# `resource_id` des arêtes suivra sans migration de forme.

def instance_ref(label: str, provider: str) -> str:
    return instance_refs.make_platform_ref(provider, label)


def parse_instance_ref(ref: str) -> Optional[tuple[str, str]]:
    """Ref d'instance PLATEFORME → (label, provider). None si le ref est malformé ou
    vise un autre niveau (member/group/org) — la chaîne n'accorde que des clés
    plateforme dans ce lot."""
    try:
        parsed = instance_refs.parse_ref(ref)
    except ValueError:
        return None
    if parsed.level != "platform" or not parsed.label or not parsed.connector:
        return None
    return (parsed.label, parsed.connector)


def active_group(sub: Optional[str], active_org: Optional[int]) -> Optional[int]:
    """L'équipe ACTIVE de l'appelant, si elle est l'une des SIENNES dans l'org active —
    le scope `group` d'une arête (#1239). `None` sans sub, sans org, sans équipe active,
    ou si l'équipe active n'est pas une équipe dont il est membre dans cette org (un axe
    `_group=` lisible sans appartenance n'ouvre pas la clé incluse d'une équipe).

    Le même grain que l'org : l'équipe ACTIVE, jamais toutes les appartenances — un grant
    d'équipe se métre par contexte, comme le grant d'org."""
    if not sub or active_org is None:
        return None
    from . import access                    # tardif : `access` lit ce module
    g = access.current_group(sub)
    if g is None:
        return None
    from . import group_store
    siennes = {int(r["group_id"]) for r in group_store.list_groups_for_user(sub, active_org)}
    return int(g) if int(g) in siennes else None


def grantee_scopes(sub: Optional[str], active_org: Optional[int]) -> list[tuple[str, str]]:
    """Les scopes qui peuvent viser cet appelant, du plus spécifique au moins :
    `user:<sub>`, puis l'équipe ACTIVE (`group:<gid>`), puis l'org ACTIVE.

    **Miroir EXACT d'`access._platform_grantee_scope`** : jamais l'appartenance seule.
    Un membre de l'org X actif dans Y ne profite pas du grant de X : le grant d'org est
    métré per-contexte-d'org, et ce n'est pas un détail d'implémentation mais la
    sémantique que la migration doit reproduire au caractère près.

    **Le scope `group` (décision d'Alexis du 13/10/2026, #1239)** — que cette docstring
    réservait à l'ADR : la clé incluse s'accorde aussi à une ÉQUIPE, entre l'utilisateur et
    l'org. Il se lit sur l'équipe ACTIVE, et seulement si l'appelant en est membre dans
    l'org active (`active_group`) : le même grain par contexte que l'org. Avec la politique
    « clé perso interdite », c'est ce qui réserve un connecteur à une équipe."""
    out: list[tuple[str, str]] = []
    if sub:
        out.append(("user", sub))
    g = active_group(sub, active_org)
    if g is not None:
        out.append(("group", str(g)))
    if active_org is not None:
        out.append(("org", str(active_org)))
    return out


# ── La contrainte de quota ─────────────────────────────────────────────────────
# 0053-D4 ferme le vocabulaire à cinq entrées et décrit `quota` comme « n appels /
# période ». Elle ne fige pas la FORME de la valeur. Retenu ici, minimal : un entier
# = n appels par FENÊTRE DE COMPTEUR, et la fenêtre est le jour (`grant_counters.
# window_start DATE`) — la seule que la table sache tenir. Une période autre (mois,
# cycle de facturation) est un amendement, pas une valeur de plus.
# Convention conservée de l'ancien chemin : **0 ou absent = illimité**.

def quota_of(edge: dict) -> Optional[int]:
    q = (edge.get("constraints") or {}).get("quota")
    try:
        return int(q) if q is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ChainVerdict:
    """Ce que la chaîne répond. `granted=False` = REFUS explicite (arêtes révoquées) ;
    l'état MUET n'est pas un verdict — c'est `None`."""
    granted: bool
    label: Optional[str] = None
    quota: Optional[int] = None
    grant_id: Optional[int] = None
    resource_id: Optional[str] = None
    grantee: Optional[tuple[str, str]] = None


def revoked_scopes(edges: list[dict], scopes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Les scopes de l'appelant RÉVOQUÉS sur le connecteur : des arêtes, aucune vivante.

    `edges` = les arêtes de TOUTES les clés du connecteur. Une arête archivée n'est pas
    forcément une révocation : `grant` archive la précédente avant de poser la neuve.
    Mais un remplacement laisse toujours une arête VIVANTE du même scope, et une
    révocation (sans libellé) archive celles de toutes les clés : « des arêtes, aucune
    vivante » est donc exactement « révoqué, et aucun grant ne l'a levé depuis ».
    Aucune marque de plus, aucune comparaison de dates."""
    out = []
    for sc in scopes:
        mine = [e for e in edges if (e["grantee_kind"], e["grantee_id"]) == sc]
        if mine and all(e.get("revoked_at") is not None for e in mine):
            out.append(sc)
    return out


def platform_rung(sub: Optional[str], provider: str,
                  active_org: Optional[int], *,
                  instances: "list[dict] | None" = None) -> Optional[ChainVerdict]:
    """Le barreau plateforme, vu par la chaîne. `None` = la chaîne n'a pas d'avis
    (connecteur non basculé, ou aucune arête n'a jamais visé cet appelant) ⟹
    l'appelant retombe sur l'ancien chemin, à l'identique.

    **La révocation prime sur tout accord, quel que soit le grain** (même règle que
    `access.platform_grant._platform_denied`) : un scope de l'appelant révoqué sur le
    connecteur (`revoked_scopes`) le refuse, même s'il a une arête vivante sur l'autre
    scope — sinon un accord individuel rouvrait le blocage de crédit de son org. La
    priorité `user:` > `org:` ne sert qu'à choisir le QUOTA de l'arête gagnante.

    Profondeur 1 : toutes les arêtes de ce lot sont des racines (`parent_id IS NULL`)
    posées par le propriétaire plateforme. Le « min des contraintes le long de la
    chaîne » (0053-D5) est donc la contrainte de l'arête elle-même — la marche
    récursive arrive avec L7, quand des scopes intermédiaires (org → équipe → user)
    existeront réellement.

    `instances` : les lignes PLATEFORME de ce `provider`, déjà lues — évite de
    relire la même table que `_legacy_platform_grant_meta` (le repli de
    `_platform_grant_meta` quand la chaîne est muette). `None` = lit elle-même,
    comme avant (repli, pas un défaut plus permissif)."""
    if not is_chained(provider) or not sub:
        return None
    scopes = grantee_scopes(sub, active_org)
    if not scopes:
        return None
    # Ordre des instances = celui de l'ancien chemin (récente d'abord), pour que la
    # clé gagnante soit la même des deux côtés.
    par_cle = []
    for inst in (instances if instances is not None
                 else credentials_store.list_platform_instances(provider)):
        ref = instance_ref(inst["label"], provider)
        par_cle.append((inst, ref, db_grants.edges_for(ref, scopes)))
    revoques = revoked_scopes([e for _, _, edges in par_cle for e in edges], scopes)
    if revoques:
        inst, ref = next((i, r) for i, r, edges in par_cle
                         if any((e["grantee_kind"], e["grantee_id"]) == revoques[0]
                                for e in edges))
        return ChainVerdict(False, inst["label"], resource_id=ref, grantee=revoques[0])
    for inst, ref, edges in par_cle:
        live = [e for e in edges if e.get("revoked_at") is None]
        if not live:
            continue
        # Plusieurs voies vers la MÊME instance (0053-D5) : la plus favorable
        # s'applique. `grantee_scopes` est ordonné du plus spécifique au moins ;
        # à scope égal, `edges_for` a déjà trié la plus récente en tête.
        best = min(live, key=lambda e: scopes.index((e["grantee_kind"], e["grantee_id"]))
                   if (e["grantee_kind"], e["grantee_id"]) in scopes else len(scopes))
        return ChainVerdict(True, inst["label"], quota_of(best), int(best["id"]), ref,
                            (best["grantee_kind"], best["grantee_id"]))
    return None


# ── L'arête tenant→org (L-clés PR 2, 0053-D3 : « le tenant s'insère dans la même chaîne ») ──
# La clé d'un tenant est une instance comme une autre ; son ref est celui du coffre
# (`tenant:{slug}:{connecteur}`, `instance_refs`). Pas de gate `CHAIN_CONNECTORS` : la
# clé de tenant est née AVEC la chaîne, il n'y a pas d'ancien chemin à doubler —
# l'état MUET (aucune arête) EST le comportement de la PR 1, à l'identique.

def tenant_ref(slug: str, provider: str) -> str:
    return instance_refs.make_tenant_ref(slug, provider)


def tenant_rung(slug: str, provider: str, org: Optional[int]) -> Optional[ChainVerdict]:
    """Le barreau TENANT vu par la chaîne, pour l'org des CLÉS de l'appel — l'org de
    contexte dont l'appelant est membre (`access.scope.org_d_appartenance`) : un
    bénéficiaire HORS de l'org d'un projet partagé arrive ici avec `None` et ne
    lit pas l'arête de cette org (ni son budget, ni sa révocation). `None` = MUETTE
    (aucune arête n'a jamais visé cette org ⟹ la clé sert comme en PR 1) ;
    `granted=False` = REFUSE (toutes révoquées ⟹ le barreau se saute, l'org retombe
    sur la plateforme) ; sinon ACCORDE, avec le budget de l'arête (R10 : partagé par
    l'org). Une lecture indexée, et seulement quand une clé de tenant existe."""
    if org is None:
        return None
    ref = tenant_ref(slug, provider)
    edges = db_grants.edges_for(ref, [("org", str(org))])
    if not edges:
        return None
    live = [e for e in edges if e.get("revoked_at") is None]
    if not live:
        return ChainVerdict(False, slug, resource_id=ref)
    best = live[0]                       # `edges_for` : vivantes d'abord, récentes d'abord
    return ChainVerdict(True, slug, quota_of(best), int(best["id"]), ref, ("org", str(org)))


def tenant_for_org(org: int, provider: str) -> Optional[str]:
    """L'ANONYME (ADR 0032) n'a pas d'identité : son tenant ne se lit que sur une
    arête VIVANTE `tenant:*:{provider} → org:{org}` — jamais sur le rattachement de
    l'org (lot L1). Rend le slug, ou None (aucune arête, ou toutes révoquées)."""
    for e in db_grants.live_edges_for_grantee("org", str(org), "tenant:"):
        try:
            parsed = instance_refs.parse_ref(e["resource_id"])
        except ValueError:
            continue
        if parsed.level == "tenant" and parsed.connector == provider and parsed.tenant:
            return parsed.tenant
    return None


def tenant_grant(slug: str, provider: str, org_id: int, daily_quota: Optional[int] = None,
                 created_by: Optional[str] = None) -> int:
    """Accorde la clé du tenant à une org, avec son budget (D6 : le précédent est
    ARCHIVÉ, jamais deux arêtes vivantes). Rend l'id de l'arête."""
    ref = tenant_ref(slug, provider)
    db_grants.revoke_edges(ref, "org", str(org_id))
    return db_grants.insert_grant(
        resource_id=ref, grantor_kind="tenant", grantor_id=slug,
        grantee_kind="org", grantee_id=str(org_id),
        constraints={"quota": int(daily_quota)} if daily_quota else {},
        source="manual", created_by=created_by)


def tenant_revoke(slug: str, provider: str, org_id: int) -> int:
    """Archive les arêtes vivantes : l'org est REFUSÉE sur cette clé à la lecture
    suivante (elle retombe sur la plateforme) — ce n'est pas « revenir à sans arête »."""
    return db_grants.revoke_edges(tenant_ref(slug, provider), "org", str(org_id))


def tenant_org_grants(slug: str, provider: str) -> list[dict]:
    """Les orgs accordées, budget et consommation du jour (surface d'affichage)."""
    ref = tenant_ref(slug, provider)
    out = []
    for e in db_grants.live_edges_for_resource(ref):
        if e.get("grantee_kind") != "org":
            continue
        out.append({"org_id": int(e["grantee_id"]), "daily_quota": quota_of(e),
                    "used_today": db_grants.counter_sum_today(ref, "org", e["grantee_id"]),
                    "grant_id": int(e["id"]), "created_at": e.get("created_at")})
    return out


# ── Le comptage (0053-D7) ──────────────────────────────────────────────────────
# L'arête porte la règle ET les incréments. Ce qui suit débite l'arête gagnante à
# chaque appel réussi sous clé plateforme (le règlement de l'appel, L8).
#
# **L'autorité du refus (R6, `docs/credits-au-moment-de-l-appel.md`).** Sous une arête
# `user`, le refus lit le compteur d'arête (`quotas.platform_used_today`). Sous une
# arête `org` ou `group`, il lit `usage(sub, tool, day)`, par personne : un compteur
# d'arête compte pour TOUS ses bénéficiaires, et le plafond d'une arête d'org est un
# plafond par personne — le basculer changerait le plafond de chacun. `grant_counters`
# n'a pas de colonne de bénéficiaire ; le pot commun des crédits le remplacera.

def record_usage(sub: str, provider: str, active_org: Optional[int],
                 calls: int = 1) -> None:
    """Débite l'arête gagnante (best-effort). No-op hors connecteurs basculés.

    Ré-résout la chaîne au lieu de transporter l'arête depuis la résolution : une
    lecture indexée (0,035 ms au banc) contre une ContextVar qui devrait traverser
    un threadpool — le prix est payé pour ne pas risquer de débiter l'arête d'un
    autre appel."""
    if not is_chained(provider):
        return
    verdict = None
    try:
        verdict = platform_rung(sub, provider, active_org)
        if verdict is None or not verdict.granted or verdict.grant_id is None:
            return
        db_grants.bump_counter(verdict.grant_id, calls)
    except Exception:  # noqa: BLE001
        # Le fail-open est JUSTE : le metering ne casse jamais un appel qui a réussi.
        # C'est le NIVEAU qui en faisait un silence — `debug` ne sort pas en prod, donc
        # « le quota n'a pas bougé » n'était imputable à rien (inventaire des silences
        # du 2026-08-27, site B10). La ligne porte de quoi retrouver l'arête et
        # rattraper le compte à la main.
        logger.warning(
            "grants-chain: débit de compteur ÉCHOUÉ — provider=%s sub=%s org=%s "
            "grant=%s calls=%s (l'appel a réussi, le quota n'a PAS bougé)",
            provider, sub, active_org,
            getattr(verdict, "grant_id", None), calls, exc_info=True)


# ── L'écriture d'un grant (la surface admin) ───────────────────────────────────

def grant(provider: str, scope: str, daily_quota: Optional[int] = None,
          label: Optional[str] = None, created_by: Optional[str] = None) -> Optional[int]:
    """Accorde l'accès plateforme à `scope` (`user:<sub>` | `group:<gid>` | `org:<id>`)
    en posant une ARÊTE. Rend son id, ou None si le scope est hors vocabulaire.

    **Additif par construction** : aucune écriture ne touche la ligne du coffre, donc
    accorder à l'un ne peut plus retirer à l'autre. C'est la sortie de l'incident du
    31/07 — pas un garde-fou de plus autour du même geste, l'absence du geste."""
    grantee = _parse_scope(scope)
    if grantee is None:
        logger.warning("grants-chain: scope %r hors vocabulaire (user:/group:/org:) — ignoré",
                       scope)
        return None
    label = label or _latest_label(provider)
    if label is None:
        raise ValueError(f"aucune instance plateforme pour {provider!r}")
    ref = instance_ref(label, provider)
    # Un grant re-posé remplace le précédent (D6 : le remplacé est ARCHIVÉ) — sinon
    # deux arêtes vivantes coexisteraient et la plus favorable gagnerait, donc
    # baisser un quota n'aurait aucun effet.
    db_grants.revoke_edges(ref, *grantee)
    return db_grants.insert_grant(
        resource_id=ref, grantor_kind=PLATFORM_SCOPE[0], grantor_id=PLATFORM_SCOPE[1],
        grantee_kind=grantee[0], grantee_id=grantee[1],
        constraints={"quota": int(daily_quota)} if daily_quota else {},
        source="manual", created_by=created_by)


def revoke(provider: str, scope: str, label: Optional[str] = None) -> int:
    """Archive les arêtes vivantes de `scope` sur l'instance. L'accès est coupé à la
    lecture SUIVANTE — rien à invalider, aucun cache à purger.

    F-046 : sans AUCUNE arête pour ce scope, il n'y avait rien à archiver, donc la chaîne
    restait MUETTE (`platform_rung` → None) et l'appelant retombait sur l'ancien chemin —
    une clé ouverte le servait comme avant. Révoquer sur une clé ouverte jamais accordée
    (le blocage de crédit d'une org gratuite) ne coupait rien. On pose alors une arête
    née archivée : la chaîne a un avis (« révoqué ») et refuse sans repli. Un `grant`
    ultérieur la remplace, comme toute arête. Une seule écriture : posée vivante puis
    archivée, elle accordait la clé sans quota entre les deux — sur une clé fermée
    comme ouverte — et pour toujours si l'archivage échouait.

    Sans `label`, la révocation vaut pour le CONNECTEUR : elle archive les arêtes du
    scope sur TOUTES ses clés. N'archiver que la plus récente laissait `platform_rung`
    servir l'appelant révoqué par l'arête vivante d'une autre clé. L'arête née archivée
    se pose sur la plus récente ; une clé posée ensuite, sans arête, ne rouvre rien
    (`platform_rung` rend le refus vu sur l'autre clé). Une seule transaction : une
    révocation n'est jamais à moitié faite."""
    grantee = _parse_scope(scope)
    if grantee is None:
        return 0
    labels = ([label] if label
              else [i["label"] for i in credentials_store.list_platform_instances(provider)])
    if not labels:
        return 0
    refs = [instance_ref(lb, provider) for lb in labels]
    with _connect() as conn:
        n = sum(db_grants.revoke_edges(ref, *grantee, conn=conn) for ref in refs)
        if n == 0 and not any(db_grants.edge_exists(ref, *grantee, conn=conn)
                              for ref in refs):
            db_grants.insert_grant(
                resource_id=refs[0], grantor_kind=PLATFORM_SCOPE[0],
                grantor_id=PLATFORM_SCOPE[1], grantee_kind=grantee[0],
                grantee_id=grantee[1], constraints={}, source="manual",
                created_by=None, revoked=True, conn=conn)
            n = 1
    return n


def granted_instances(scope_kind: str, scope_id: str) -> list[dict]:
    """Les instances plateforme accordées à ce scope, forme de l'ancien
    `db._grants_for_scope` : `{provider, label, daily_quota}`. Sert les surfaces
    d'affichage (KeyStack, fiche admin) pour qu'elles ne mentent pas pendant la
    fenêtre — un connecteur basculé n'apparaît plus dans `share_down`."""
    out = []
    for e in db_grants.live_edges_for_grantee(scope_kind, scope_id, "platform:"):
        parsed = parse_instance_ref(e["resource_id"])
        if parsed is None or not is_chained(parsed[1]):
            continue
        label, provider = parsed
        out.append({"provider": provider, "label": label, "daily_quota": quota_of(e)})
    return out


def _parse_scope(scope: str) -> Optional[tuple[str, str]]:
    kind, _, ident = str(scope).partition(":")
    return (kind, ident) if kind in ("user", "group", "org") and ident else None


def _latest_label(provider: str) -> Optional[str]:
    insts = credentials_store.list_platform_instances(provider)
    return insts[0]["label"] if insts else None
