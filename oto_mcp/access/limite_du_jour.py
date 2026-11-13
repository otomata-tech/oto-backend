"""Limite de dépense du jour : appliquer la liste que pousse le commerce.

Un admin d'org fixe des limites de dépense quotidiennes — pour l'org, une équipe, une
personne — chez le COMMERCE, qui tient les crédits et compte la dépense du jour. Quand
une limite est atteinte, le commerce pousse un blocage (`service.org.spend_blocks.put`)
jusqu'à la remise à zéro. Le cœur ne compte RIEN et ne connaît aucune limite : il refuse
seulement, à une personne bloquée (ou à toute une org bloquée), les appels payés par une
clé que l'opérateur refacture — clé de plateforme, ou clé refacturée du tenant primaire.
La clé de l'org ou de la personne reste servie : elle ne consomme pas de crédits
(`docs/limites-de-depense.md`).

**Trois portes**, toutes sur ce module (`refus`) :
- l'outil : le résolveur, au moment où une clé refacturée gagne (`access/resolve.py`
  appelle `reserver`, qui refuse AVANT de réserver au budget d'unités) ;
- le tour de modèle d'un agent hébergé : sa réservation (`runner_jobs._reserver_un_tour`)
  — l'agent compte pour son AUTEUR (`runner_jobs.sub`) ;
- le tour de modèle de l'agent intégré (`copilot/cle.reserver`).

**Aucune lecture de base par appel** : la liste des blocages en cours est gardée en
mémoire et relue toutes les `TTL_S` secondes, comme celle des orgs suspendues
(`org_suspension`). Un blocage a sa fin (`until`) : passé cet instant, il ne bloque
plus, même si le commerce ne l'a pas retiré — une journée finie se rouvre seule.

⚠️ **FAIL-OPEN, à l'inverse de la suspension** : une limite de dépense est un garde-fou
que le client se pose, pas une sécurité. Une liste illisible ne bloque personne (la
dernière liste connue reste servie si elle existe), et le dit dans le journal.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from mcp.types import INVALID_PARAMS, ErrorData

from ..db import spend_blocks
from . import credits
from ..mcp_errors import McpError

logger = logging.getLogger(__name__)

CODE = "daily_spend_limit"

TTL_S = 30.0
RETRY_S = 5.0

_verrou = threading.Lock()
#: `{org_id: {sub: blocage}}` ; `sub == spend_blocks.TOUTE_L_ORG` = toute l'org.
_cache: dict = {"par_org": {}, "lu_a": None}


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _liste() -> dict:
    """La liste en mémoire, relue au plus toutes les `TTL_S` secondes."""
    lu_a = _cache["lu_a"]
    if lu_a is not None and time.monotonic() - lu_a < TTL_S:
        return _cache["par_org"]
    with _verrou:
        lu_a = _cache["lu_a"]
        if lu_a is not None and time.monotonic() - lu_a < TTL_S:
            return _cache["par_org"]
        try:
            par_org: dict = {}
            for b in spend_blocks.actifs():
                par_org.setdefault(int(b["org_id"]), {})[b["sub"]] = b
        except Exception as e:
            # Fail-open : la dernière liste connue (vide si jamais lue), relue bientôt.
            logger.warning("limites du jour : liste des blocages illisible, %s servie — %s",
                           "dernière liste" if lu_a is not None else "aucune", e)
            _cache["lu_a"] = time.monotonic() - TTL_S + RETRY_S
            return _cache["par_org"]
        _cache.update(par_org=par_org, lu_a=time.monotonic())
        return par_org


def invalider() -> None:
    """Relire la liste au prochain appel — après un geste du commerce dans CE processus."""
    with _verrou:
        if _cache["lu_a"] is not None:
            _cache["lu_a"] = time.monotonic() - TTL_S


def blocage(sub: Optional[str], org_id: Optional[int]) -> Optional[dict]:
    """Le blocage en cours qui touche `sub` agissant dans `org_id`, ou `None`. Celui de la
    personne (limite personnelle ou d'une de ses équipes) passe avant celui de l'org."""
    if not org_id:
        return None
    de_l_org = _liste().get(int(org_id))
    if not de_l_org:
        return None
    maintenant = _maintenant()
    for cle in ((sub,) if sub else ()) + (spend_blocks.TOUTE_L_ORG,):
        b = de_l_org.get(cle)
        if b is not None and b["until"] > maintenant:
            return b
    return None


_PORTEE = {"member": "your personal daily credit limit",
           "group": "your team's daily credit limit",
           "org": "your organization's daily credit limit"}


def message(b: dict) -> str:
    fin = b["until"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"You have reached {_PORTEE.get(b['scope'], 'a daily credit limit')}, set by "
            f"an admin of your organization. Calls on the platform's shared keys resume "
            f"at {fin}. Keys your organization or you set yourselves still work; an admin "
            "can raise the limit in the organization's usage settings.")


def donnees(b: dict) -> dict:
    """Ce que porte le refus, lisible par une machine (le front, un agent)."""
    return {"code": CODE, "retryable": False, "scope": b["scope"],
            "group_id": b.get("group_id"), "until": b["until"].isoformat()}


def refus(sub: Optional[str], org_id: Optional[int]) -> Optional[dict]:
    """Le blocage à opposer, journalisé, ou `None` si l'appel passe."""
    b = blocage(sub, org_id)
    if b is not None:
        logger.info("limite du jour : refus org=%s sub=%s portée=%s jusqu'à %s",
                    org_id, sub, b["scope"], b["until"])
    return b


def exiger(sub: Optional[str], org_id: Optional[int]) -> None:
    """La porte des OUTILS : lève le refus nommé (`McpError`, `data.code =
    daily_spend_limit`) quand `sub` est bloqué dans `org_id`."""
    if (b := refus(sub, org_id)) is not None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=message(b), data=donnees(b)))


def reserver(sub: str, org: Optional[int], key_mode: str) -> None:
    """La porte du RÉSOLVEUR, quand une clé que l'opérateur refacture gagne : la limite du
    jour d'abord — un refus ne laisse aucune réservation —, puis le budget d'unités
    (`credits.reserver`, inchangé)."""
    exiger(sub, org)
    credits.reserver(sub, org, key_mode)
