"""Ce qui est MÉTRÉ et ce qui est PAYÉ (ADR 0043).

Deux crans distincts, souvent confondus :

- le **quota** journalier d'une clé PLATEFORME (`quota_for`, `_org_unmetered`,
  `record_platform_usage`) — un garde-fou d'essai, levé par un plan `unmetered` ;
- l'**option payante** d'un connecteur (`paid_option_for`, `has_option`) — le
  cran d'entitlement de l'abonnement d'org, seam unique à deux sources (comp
  admin sur l'user ou l'org, OU plan de l'abonnement actif).

Ne dépend que de `scope` (le contexte de l'acteur). Le verdict « l'option est-elle
LEVÉE pour ce connecteur » (qui tient compte du BYO) vit dans `views.option_open`,
au-dessus de la cascade.
"""
from __future__ import annotations

import os
from typing import Optional

from .. import providers, db, grants_chain
from ..auth.hooks import current_user_sub_from_token
from . import scope

# DÉRIVÉ du registre source unique (package `providers/`) : quota daily par
# provider (fallback si pas d'env ni de grant).
_QUOTA_DEFAULTS = providers.QUOTA_DEFAULTS


# Add-on payant requis par un connecteur (couche 3, ADR 0043). None = aucun. HOME
# canonique de ce mapping (les surfaces org ET user en dérivent — derive don't duplicate).
_PAID_OPTION_BY_CONNECTOR = {"unipile": "unipile"}


def paid_option_for(connector: str) -> Optional[str]:
    """Option payante requise par un connecteur (ou None).

    Suit la **délégation de credential** : les six canaux unipile n'ont pas d'option
    à eux, ils partagent celle du compte. Une option par canal serait un contresens
    métier (l'option paie des SIÈGES sur la clé plateforme, et un siège est un compte
    chez le fournisseur, pas un canal) et une régression : le comp `unipile` d'un
    client abonné cesserait d'ouvrir WhatsApp le jour du split."""
    return _PAID_OPTION_BY_CONNECTOR.get(
        connector) or _PAID_OPTION_BY_CONNECTOR.get(
        providers.credential_provider(connector))


def has_option(sub: str, option: str, *, org: "int | None | object" = scope._UNSET) -> bool:
    """Couche 3 du modèle de connecteur (cf. docs/connector-model.md) : l'option de
    connecteur `option` (ex. `unipile`) est-elle débloquée pour `sub` ? **Seam unique** —
    deux sources (ADR 0043) : un **comp admin** sur l'USER ou l'ORG active, OU
    l'**abonnement actif de l'org** dont le plan inclut l'option (mapping
    `billing.plan_options`, miroir `org_subscriptions` — `past_due` reste ouvert
    tant que la grace court ; la fermeture est un acte du billing_runner).
    Ne JAMAIS lire les sources en direct ailleurs (un nouveau chemin passe par ici).
    `org` explicite (≠ _UNSET) = calcul pour un tiers contre une org donnée (fiche admin),
    sans current_org (anti-fuite de contexte)."""
    if db.has_option_comp("user", sub, option):
        return True
    org = scope.current_org(sub) if org is scope._UNSET else org
    if org is None:
        return False
    if db.has_option_comp("org", str(org), option):
        return True
    plan = db.subscription_plan_for_org(int(org))
    if plan is not None:
        from .. import billing  # import tardif (billing tire mollie/httpx)

        return option in billing.plan_options(plan)
    return False


def quota_for(provider: str) -> int:
    """Quota journalier de la clé PLATEFORME d'un connecteur.

    Normalisé vers le PORTEUR du credential : un quota est une propriété de la clé,
    et six canaux qui empruntent la même clé partagent forcément son compteur. Six
    compteurs indépendants laisseraient consommer 6× le quota sur une seule clé."""
    provider = providers.credential_provider(provider)
    raw = os.environ.get(f"OTO_MCP_QUOTA_{provider.upper()}_DAILY")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _QUOTA_DEFAULTS.get(provider, 0)


def usage_today(sub: str, provider: str) -> int:
    """Consommation du jour SUR LA CLÉ de `provider` — normalisée vers son porteur.

    Pendant de `quota_for` : compteur et plafond doivent nommer la même clé, sinon
    un canal lirait 0 face au plafond d'une clé déjà épuisée (« quota intact » chez
    quelqu'un qui n'a plus rien). Tout lecteur de quota passe par ici."""
    return db.get_usage_today(sub, providers.credential_provider(provider))


def _org_unmetered(org: int) -> bool:
    """L'org a-t-elle un plan actif qui lève les quotas plateforme ? (ADR 0043)"""
    plan = db.subscription_plan_for_org(int(org))
    if plan is None:
        return False
    from .. import billing  # import tardif (billing tire mollie/httpx)

    return billing.plan_is_unmetered(plan)


def record_platform_usage(provider: str, calls: int = 1) -> None:
    """À appeler APRÈS un appel réussi avec la platform key. No-op si pas authentifié.

    Deux compteurs pendant la fenêtre de double lecture (blueprint ADR 0053, L5) :
    l'historique `usage(sub, tool, day)` — qui garde l'AUTORITÉ du refus, cf.
    `grants_chain` §Le comptage — et le compteur d'ARÊTE de 0053-D7, tenu en parallèle
    pour que la bascule d'autorité soit vérifiée avant d'être faite. No-op (et aucune
    requête) hors connecteurs basculés.

    `calls` = consommation d'UN appel qui compte pour plusieurs (un bulk facturé au
    contact). L'historique reste incrémenté un par un — sa signature n'accepte pas de
    pas —, mais la chaîne débite en UNE fois : sur un bulk de 100, c'est la différence
    entre 5 requêtes et 500 sur le chemin chaud."""
    sub = current_user_sub_from_token()
    if not sub:
        return
    # Métré sur la clé RÉELLEMENT consommée (délégation) : un appel WhatsApp brûle
    # le quota du compte unipile, pas celui d'un compteur « whatsapp » que personne
    # ne lit. Écriture et lecture (`usage_today`) normalisent pareil.
    provider = providers.credential_provider(provider)
    for _ in range(max(1, calls)):
        db.increment_usage(sub, provider)
    if grants_chain.is_chained(provider):
        grants_chain.record_usage(sub, provider, scope.current_org(sub), max(1, calls))
