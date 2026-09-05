"""Ce que la chaîne de grants DÉSIGNE — la résolution de 0053, isolée (lot L7).

Une lecture impossible LÈVE : elle ne signifie jamais « clé absente, essayer plus
bas ». Seul l'observateur de comparaison peut absorber une erreur de cette chaîne.

**Pourquoi ce module existe à part.** C'est la moitié du lot qui SURVIT : quand
`walk_cascade` sera retiré (PR 3), l'observation et son compteur disparaissent, mais
ceci reste — c'est la résolution servie. Les tenir dans le même fichier aurait mélangé
ce qu'on installe et ce qu'on jette.

**Ce qu'il calcule**, tel que [0053-D2](blueprint) le pose :

1. l'**ensemble atteignable** — les instances des scopes dont le sujet est MEMBRE,
   plus celles qui lui descendent par une arête de `grants` vivante ;
2. la **désignation** — l'appel qui nomme une instance et le binding de procédure
   priment, mais ils court-circuitent déjà la marche en amont (`resolve`), donc ce
   qui reste ici est la **proximité** : `user > group > org > platform`.

Et surtout **ce qu'il ne calcule pas** : la restriction de `connector_acl`. C'est
0053-D1 — restreindre, c'est PLACER l'ownership au bon niveau, jamais poser une
interdiction par-dessus.

**Deux règles de méthode, tenues mécaniquement :**

1. **Aucune règle n'est recopiée.** Les crans du connecteur sont lus à leur SOURCE —
   le registre (`is_byo_user`, `org_shareable`, `auth_modes`), la suspension d'une
   instance, les arêtes de `grants`. Ce module écrit une TRAVERSÉE différente, pas
   une seconde copie des gates. Même discipline que
   `connectors/instance_visibility.py`, qui inverse déjà le walker sans le cloner.
2. **La désignation porte sur le PALIER, pas sur le compte.** Le choix de compte
   multi-identités est un cran de l'instance (0053-D9), pas une autorisation :
   `rung_for_pick` le délègue à la SONDE que `resolve` a composée, et ne le rejoue
   jamais.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .. import (credentials_store, grants_chain, group_store, org_store, providers,
                tenant_vault)
from ..db import grants as db_grants
from . import scope

logger = logging.getLogger(__name__)

# Les deux NUANCES du trou — « le coffre accorde, la chaîne ne sait pas le dire ».
# Elles vivent ici parce que c'est la résolution qui les CONSTATE ; `chain_shadow` les
# reprend telles quelles dans son vocabulaire de classes, sans les redéclarer (une
# valeur servie déclarée deux fois finit par diverger).
FREE_TIER_HORS_MODELE = "free_tier_hors_modele"
PARTAGE_HORS_MODELE = "partage_hors_modele"

# ── L'ensemble atteignable, et sa désignation ─────────────────────────────────

@dataclass(frozen=True)
class ChainPick:
    """Ce que la chaîne DÉSIGNERAIT. `mode` parle le même vocabulaire que
    `CascadeRung.mode`, pour que la comparaison soit une égalité et pas une
    traduction. `via` dit POURQUOI l'instance est atteignable — appartenance au
    scope propriétaire (D1, premier membre de phrase) ou arête de grant (second)."""
    mode: str                       # user | group | org | tenant | platform
    entity_type: Optional[str]
    entity_id: Optional[str]
    via: str = "appartenance"       # appartenance | grant
    group_id: Optional[int] = None


def _group_ids(sub: str, org: Optional[int]) -> list[int]:
    """Toutes les équipes du sujet dans l'org de contexte — **toutes**, pas l'active.
    C'est là que 0053-D2 élargit, et l'élargissement est le sujet de la mesure."""
    if org is None:
        return []
    return sorted(int(g["group_id"]) for g in group_store.list_groups_for_user(sub, org))


def _platform_pick(sub: str, provider: str, org: Optional[int]) -> "tuple[Optional[ChainPick], Optional[str]]":
    """Le palier plateforme vu par la CHAÎNE SEULE, et rien d'autre.

    Rend `(pick, hors_modele)`. À la différence de `grants_chain.platform_rung`,
    aucun gate `CHAIN_CONNECTORS` : L7 fait de la chaîne l'unique autorité, donc la
    question « et pour un connecteur non basculé ? » est précisément celle qu'on
    mesure. Les arêtes sont lues par la MÊME fonction que le chemin servi
    (`db_grants.edges_for`) — pas une requête recopiée.

    **`hors_modele` nomme la NUANCE du trou**, et ce n'est plus un booléen. Une ligne
    du coffre peut accorder de deux façons que la chaîne ne sait pas encore dire, et
    elles n'ont ni le même remède ni la même lecture :

    - **ouverte à tous** (`share_mode='open'`, aucune allowlist) ⟹ il manque l'arête
      « tout le monde » ;
    - **fermée sur une allowlist** (`share_down`) ⟹ il manque les arêtes NOMINATIVES
      de cette allowlist. Le semis de L5 ne couvrait que `CHAIN_CONNECTORS`, donc
      toute clé fermée hors de cette liste est dans ce cas.

    Les distinguer n'est pas un raffinement : sans la seconde, une divergence
    parfaitement explicable tombait en `inconnu` — la classe qui doit rester à zéro
    pour autoriser le retrait — et fermait la porte pour une raison fausse. Vécu le
    2026-08-29 : 17 observations sur `aiark` et `apify`, deux clés FERMÉES accordées
    à une org, sans une seule arête.

    La forme est lue au coffre, à sa source, sans rejouer la règle d'accès de
    l'ancien chemin : on regarde ce que l'instance EST, pas qui elle autorise."""
    nominatifs = grants_chain.grantee_scopes(sub, org)
    hors_modele = None
    for inst in credentials_store.list_platform_instances(provider):
        ref = grants_chain.instance_ref(inst["label"], provider)
        edges = db_grants.edges_for(ref, nominatifs + [grants_chain.EVERYONE])
        nomme = [e for e in edges
                 if (e["grantee_kind"], e["grantee_id"]) != grants_chain.EVERYONE]
        tous = [e for e in edges
                if (e["grantee_kind"], e["grantee_id"]) == grants_chain.EVERYONE
                and e.get("revoked_at") is None]
        if not edges:
            # Rien à dire sur CETTE instance : on note de quelle nuance de trou il
            # s'agirait si l'ancien chemin, lui, accordait. La première rencontrée
            # gagne — même ordre que l'ancien chemin (récente d'abord).
            if hors_modele is None:
                ouverte = (inst.get("share_mode") != "closed"
                           and not (inst.get("share_down") or []))
                hors_modele = (FREE_TIER_HORS_MODELE if ouverte
                               else PARTAGE_HORS_MODELE)
            continue
        # Une arête qui NOMME l'appelant prime sur « tout le monde », vivante ou non :
        # sans cette priorité, révoquer l'accès d'une personne sur une clé ouverte ne
        # couperait rien (l'arête « tout le monde » la re-accorderait aussitôt) — le
        # mode de panne exact que L5 avait éliminé en refusant sans repli.
        if nomme:
            if any(e.get("revoked_at") is None for e in nomme):
                return (ChainPick("platform", credentials_store.PLATFORM,
                                  inst["label"], via="grant"), hors_modele)
            # Toutes révoquées : la chaîne REFUSE cette instance, sans repli (D6).
            return (None, hors_modele)
        if tous:
            return (ChainPick("platform", credentials_store.PLATFORM, inst["label"],
                              via="tout_le_monde"), hors_modele)
    return (None, hors_modele)


def _paliers(sub: str, provider: str, org: Optional[int], want: str,
             *, group=scope._UNSET):
    """Les paliers ATTEIGNABLES, dans l'ordre, en **générateur** — et la nuance du
    trou en valeur de retour (PEP 380, lue par `StopIteration.value`).

    ⚠️ **Générateur et non liste, et ce n'est pas un détail de style.** Une liste
    sonderait TOUS les paliers à chaque appel — le membre, chaque équipe, l'org, le
    tenant, plus les instances plateforme — sur le chemin le plus chaud du produit,
    pour un résultat dont on ne consomme presque toujours que le premier élément. Le
    générateur rend le coût nominal identique à celui d'aujourd'hui : on ne sonde le
    palier suivant que si le précédent n'a rien rendu.

    ⚠️ **Et ce n'est pas une forme AJOUTÉE : c'est celle que la traversée avait
    perdue.** Le chemin historique (`walk_cascade`) est un générateur — un compte
    nommé absent au palier membre passait la main au palier org, contrat écrit dans
    son code. La chaîne l'a remplacé par un `return` au premier palier qui DÉTIENT une
    clé, et la lecture, elle, résout au fetch : un miss devenait un refus sec au lieu
    d'un repli (#673). Céder au lieu de retourner rend le repli, sans rien réécrire de
    la lecture.

    Les crans du connecteur (byo_user, org-partageable, palier plateforme déclaré,
    instance suspendue) sont lus à leur source — ce sont des propriétés de
    l'instance, pas des autorisations, et ils valent des deux côtés de la fenêtre.
    La restriction `connector_acl`, elle, n'est PAS lue : c'est le fond du lot.
    """
    porteur = providers.credential_provider(provider)
    if org is not None and providers.is_byo_user(porteur):
        if (credentials_store.has_credential(
                credentials_store.MEMBER, credentials_store.member_id(org, sub),
                porteur, account=None)
                and not credentials_store.instance_suspended(
                    credentials_store.MEMBER, credentials_store.member_id(org, sub), porteur)):
            # Le drapeau free-tier ne sert QUE si la chaîne se tait.
            yield ChainPick("user", credentials_store.MEMBER,
                            credentials_store.member_id(org, sub))
    if porteur in providers.ORG_SHAREABLE_PROVIDERS:
        # À proximité égale, l'équipe ACTIVE d'abord — c'est la voie la plus
        # favorable au sens de D5, et ça rend la désignation déterministe quand le
        # sujet appartient à plusieurs équipes qui détiennent toutes une clé.
        active = scope.current_group(sub) if group is scope._UNSET else group
        active = active() if callable(active) else active
        gids = _group_ids(sub, org)
        if active is not None and int(active) in gids:
            gids = [int(active)] + [g for g in gids if g != int(active)]
        for gid in gids:
            if group_store.has_group_secret(gid, porteur):
                yield ChainPick("group", "group", str(gid), group_id=gid)
        if org is not None:
            if org_store.has_org_secret(org, porteur):
                yield ChainPick("org", "org", str(org))
        # Étage TENANT (L-clés PR 1) : le même que dans le walker, lu à la même source
        # (`rung_tenant` — le sub qualifié, jamais l'org). Sans lui, chaque clé tenant
        # servie compterait une divergence `inconnu` que ce lot aurait créée.
        slug = tenant_vault.rung_tenant(sub)
        if slug is not None:
            if (credentials_store.has_credential(credentials_store.TENANT, slug, porteur)
                    and not credentials_store.instance_suspended(
                        credentials_store.TENANT, slug, porteur)):
                # Même arête que le walker : MUETTE ⟹ appartenance ; REFUSE ⟹ suite.
                verdict = grants_chain.tenant_rung(slug, porteur, org)
                if verdict is None or verdict.granted:
                    yield ChainPick("tenant", credentials_store.TENANT, slug,
                                    via="grant" if verdict else "appartenance")
    if want != "byo":
        con = providers.connector_for_provider(porteur)
        if con is not None and "platform" in con.auth_modes:
            pick, hors_modele = _platform_pick(sub, porteur, org)
            if pick is not None:
                yield pick
            # La nuance ne se calcule QUE si la chaîne se tait — le `_platform_pick`
            # ci-dessus est le seul passage qui lit les instances plateforme, et elle
            # en sort. La rendre ici la garde attachée à sa passe : la sortir du
            # générateur demanderait une seconde lecture sur le connecteur le plus
            # trafiqué (une clé ouverte EST le cas où la chaîne se tait).
            return hors_modele
    return None


def chain_verdict(sub: str, provider: str, *, org: Optional[int],
                  want: str = "auto") -> "tuple[Optional[ChainPick], Optional[str]]":
    """L'instance que 0053-D2 DÉSIGNERAIT, **et** la nuance du trou si elle se tait.

    La désignation reste ce qu'elle était : le PREMIER palier atteignable. Ce que la
    traversée a regagné (#673) sert à la LECTURE, pas à la comparaison — le relevé de
    fenêtre compare des désignations, et lui donner une liste ferait bouger ce qu'il
    mesure au moment où on corrige autre chose.

    On ne consomme donc qu'un élément : les paliers suivants ne sont jamais sondés si
    le premier répond. La nuance, elle, est la valeur de RETOUR du générateur — elle
    n'existe que lorsqu'il s'épuise sans rien céder, c'est-à-dire exactement quand la
    chaîne se tait.
    """
    paliers = _paliers(sub, provider, org, want)
    try:
        return (next(paliers), None)
    except StopIteration as fin:
        return (None, fin.value)


def chain_paliers(sub: str, provider: str, *, org: Optional[int],
                  want: str = "auto", group=scope._UNSET):
    """Les paliers atteignables DANS L'ORDRE — ce que la lecture parcourt.

    C'est la surface que `rung_for_picks` consomme : elle s'arrête au premier palier
    qui RÉPOND, là où `chain_verdict` s'arrête au premier qui EXISTE. La différence
    entre les deux est tout le sujet de #673.
    """
    return _paliers(sub, provider, org, want, group=group)


def chain_winner(sub: str, provider: str, *, org: Optional[int],
                 want: str = "auto") -> Optional[ChainPick]:
    """`chain_verdict` sans son drapeau — la vue qui se lit, et celle que la PR 2
    promouvra en résolution servie."""
    return chain_verdict(sub, provider, org=org, want=want)[0]



def rung_for_picks(paliers, probe, sub: str, provider: str, org: Optional[int]):
    """Le premier palier qui RÉPOND, en parcourant les paliers atteignables.

    ⚠️ **C'est le repli, et il était mort.** La chaîne désigne un palier sur la
    PRÉSENCE d'un credential (`has_credential(account=None)` — n'importe quel compte) ;
    la lecture, elle, résout au FETCH, avec la sélection de compte nommé. Les deux ne
    répondent donc pas toujours la même chose : un compte nommé absent au palier
    membre existe « en présence » et manque « au fetch ». Le chemin historique passait
    alors la main au palier suivant — « l'org a eu, le membre non », contrat écrit
    dans son code. Désigner UN palier puis rendre `None` sur un miss transformait ce
    repli en **refus sec** (#673).

    On parcourt donc, et le coût reste celui d'avant : le générateur ne sonde le
    palier suivant que si le précédent n'a rien rendu, et le cas nominal s'arrête au
    premier.
    """
    return next(rungs_for_picks(paliers, probe, sub, provider, org), None)


def rungs_for_picks(paliers, probe, sub: str, provider: str, org: Optional[int]):
    """Les réponses de la sonde, sans consommer les paliers suivants en avance."""
    for pick in paliers:
        rung = rung_for_pick(pick, probe, sub, provider, org)
        if rung is not None:
            yield rung


def rung_for_pick(pick: Optional[ChainPick], probe, sub: str, provider: str,
                  org: Optional[int]):
    """Le barreau SERVI correspondant à la désignation de la chaîne, ou None.

    **On ne réécrit pas le FETCH, on réutilise les sondes.** Le walker faisait deux
    choses : traverser (l'ordre des barreaux, les gates) et lire (la sonde, avec sa
    sélection de compte multi-identités, sa suspension, son déchiffrement du seul
    gagnant). L7 ne remplace que la **traversée** ; la lecture reste la sonde que
    `resolve` a déjà composée. C'est ce qui fait qu'inverser l'autorité ne rejoue
    aucune règle de compte — donc n'en fait diverger aucune.

    Rend un `cascade.CascadeRung`, la même forme que ce que `cascade_winner` rendait :
    tout ce qui suit dans `resolve` (garde du compte nommé, quota, `ResolvedCredential`)
    est alors inchangé, ligne pour ligne."""
    if pick is None:
        return None
    from . import cascade  # import tardif : `cascade` est un frère, pas une dépendance
    if pick.mode == "user":
        hit = probe.member(sub, org, provider)
        if hit is None:
            return None
        payload, account = hit
        return cascade.CascadeRung("user", pick.entity_type, pick.entity_id, payload,
                                   account)
    if pick.mode == "group":
        hit = probe.group(int(pick.entity_id), provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("group", "group", pick.entity_id, payload, account)
    if pick.mode == "org":
        hit = probe.org(int(pick.entity_id), provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("org", "org", pick.entity_id, payload, account)
    if pick.mode == "tenant":
        # ⚠️ **Ce barreau manquait, et son absence était INVISIBLE.** Sans lui, une
        # désignation `tenant` retombait dans la branche plateforme ci-dessous : la
        # sonde `probe.tenant` n'était jamais appelée, la clé SERVIE devenait celle de
        # la plateforme, et `tenant_budget.enforce` — conditionné à `win.mode ==
        # "tenant"` chez l'appelant — était sauté. Le shadow, lui, comparait deux
        # DÉSIGNATIONS et voyait un `accord` : le drapeau aurait annulé en silence la
        # pièce 1 des L-clés, qui est en prod. Dormant tant qu'aucune clé de tenant
        # n'est posée ; la première pose l'aurait réveillé.
        # Le `via` vient de la DÉSIGNATION (la chaîne a déjà lu l'arête tenant→org) :
        # le relire ici en ferait une seconde source, et deux sources d'un même
        # verdict finissent par diverger. Il est TRADUIT dans le vocabulaire du
        # walker — qui dit `local` là où la chaîne dit `appartenance` — parce que le
        # barreau servi doit être celui que le walker aurait produit, à l'octet :
        # `status.py` lit `via == "local"` pour dire « clé perso configurée ».
        hit = probe.tenant(pick.entity_id, provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("tenant", credentials_store.TENANT, pick.entity_id,
                                   payload, account,
                                   via="grant" if pick.via == "grant" else "local")
    # Palier plateforme : la sonde rend le grant résolu (label + secret + quota). La
    # chaîne a déjà dit QUELLE instance ; la sonde de `resolve` lit celle que l'ancien
    # chemin lirait. Tant que les deux désignent la même, c'est la même clé — et quand
    # elles divergent, la fenêtre de shadow l'a dit avant qu'on bascule.
    grant = probe.platform(sub, provider, org)
    if not grant:
        return None
    return cascade.CascadeRung("platform", credentials_store.PLATFORM,
                               grant.get("label"), grant)
