"""Un travail hébergé tourne sur la clé de modèle de SON org — ou ne tourne pas.

Sans cette garde, une org qui n'a pas déposé de clé ne se voit rien refuser : le
worker retombe sur la clé de SON environnement, c'est-à-dire la nôtre (oto-runner,
`agent_llm.complete` : `api_key or resolve_key()`). « Chaque client paie avec sa
propre clé » était donc une POLITIQUE que le système n'appliquait pas — et le
défaut est silencieux par construction : les runs aboutissent, seule la facture
change de destinataire.

⚠️ **Un réglage, pas une constante, et éteint par défaut.** Le poser en dur aurait
arrêté, le jour du déploiement, tous les agents programmés des orgs qui n'ont pas
encore déposé de clé — les nôtres comprises. L'ordre qui ne casse personne est :
déposer la clé dans nos orgs, relever les orgs clientes qui ont des agents vivants
sans clé, PUIS allumer. Le réglage vit dans `connector_settings`, que la console
admin écrit déjà (`oto_admin_connector_setting`) :

```
connector=anthropic  key=runner.org_key_required  value=true             plateforme
connector=anthropic  key=runner.org_key_required  value=false  org_id=N  exemption d'une org
```

L'org l'emporte sur la plateforme. Par CONNECTEUR, parce que la clé exigée est
celle du fournisseur que le worker sert : exiger une clé Mistral d'une org servie
par des workers Anthropic n'aurait aucun sens.

⚠️ **Deux moments, un seul verdict.** À la POSE d'un agent (création, rallumage,
armement d'un passage) : refus lisible, `model_key_required`, au moment où l'on
peut encore déposer la clé. À la RÉSERVATION : arrêt DÉFINITIF du travail, avec sa
raison écrite. Le premier est un confort ; le second est la garantie — une clé
retirée après la pose, ou un agent posé avant l'allumage, n'échappe pas au second.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import credentials_store, providers
from ..db import connector_settings
from ._types import AuthzDenied

logger = logging.getLogger(__name__)

#: La clé du réglage dans `connector_settings`.
CLE_REGLAGE = "runner.org_key_required"
VRAI, FAUX = "true", "false"


def fournisseurs_de_modele() -> list[str]:
    """Les connecteurs qui portent une clé de MODÈLE — ceux du type `credential`.
    Dérivés du registre, jamais recopiés : un fournisseur ajouté demain est couvert
    sans toucher à cette garde."""
    return sorted(c.name for c in providers.REGISTRY.values()
                  if getattr(c, "kind", None) == "credential")


def cle_exigee(org_id: int, fournisseur: str) -> bool:
    """L'org doit-elle fournir SA clé `fournisseur` pour qu'un agent tourne ?

    L'org d'abord (une exemption, ou une exigence posée avant la plateforme), puis
    la plateforme, puis FAUX — le comportement d'avant."""
    for portee, ident in (("org", str(org_id)), ("platform", "platform")):
        # Par le MODULE : `connector_settings` n'est pas aplati dans la façade `db`.
        v = connector_settings.get_connector_setting(portee, ident, fournisseur, CLE_REGLAGE)
        if v is not None:
            return v == VRAI
    return False


def cle_deposee(org_id: int, fournisseur: str) -> bool:
    """Présence du dépôt — sans déchiffrer (`secret_enc IS NOT NULL`), sur le
    mono-compte, exactement ce que la remise au worker lit. Une lecture qui LÈVE
    se lit « non déposée » et le dit : pour une garde d'argent, le doute arrête le
    travail plutôt que de le faire payer par nous."""
    try:
        return credentials_store.has_credential("org", str(org_id), fournisseur,
                                                account="")
    except Exception:
        logger.warning("présence de la clé `%s` illisible pour l'org %s — lue comme "
                       "absente", fournisseur, org_id, exc_info=True)
        return False


def manquantes(org_id: int, fournisseurs: Optional[list[str]] = None) -> list[str]:
    """Les fournisseurs dont une clé est EXIGÉE et que l'org n'a pas déposée."""
    return [f for f in (fournisseurs or fournisseurs_de_modele())
            if cle_exigee(org_id, f) and not cle_deposee(org_id, f)]


def raison_du_refus(fournisseurs: list[str]) -> str:
    noms = ", ".join(f"`{f}`" for f in fournisseurs)
    return (f"cette organisation n'a pas déposé sa clé de modèle ({noms}). Les agents "
            "hébergés tournent sur la clé de l'organisation qui les demande ; sans "
            "elle, ils ne tournent pas. Dépose la clé (Connecteurs, ou la fiche de "
            "l'agent), puis rallume l'agent.")


def exiger_a_la_pose(org_id: int) -> None:
    """Le refus LISIBLE, au moment de poser un agent. Lève `model_key_required`.

    ⚠️ Toutes les familles exigées sont regardées, pas une seule : à la pose, on ne
    sait pas encore quel worker réservera le travail, donc pas quelle clé il
    demandera. Exiger toutes celles qui sont allumées est la lecture qui ne laisse
    rien passer — une seule allumée (le cas réel), une seule vérifiée."""
    absentes = manquantes(org_id)
    if absentes:
        raise AuthzDenied(400, "model_key_required", raison_du_refus(absentes))
