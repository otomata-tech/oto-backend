"""Org suspendue : le prédicat, la phrase qui la refuse, et ce qui reste ouvert.

**Le cran au-dessus de la pause de compte** (`account_suspension`). Une pause de
compte arrête une PERSONNE, dans toutes ses orgs. Une suspension d'org arrête un
ESPACE, pour tous ses membres — et seulement lui : un membre garde ses autres orgs.
C'est le geste d'un tenant qui facture ses orgs (02/10/2026 : un essai fini sans
abonnement arrête l'app, pas seulement les clés payées par le tenant).

**Où il tombe.** Il n'y a pas UN point d'entrée par org, donc quatre gardes, toutes
sur ce module :
- les capacités, juste après la règle d'autz, dans les deux adaptateurs (MCP et
  REST) — c'est là que l'org de l'appel est connue, champ d'entrée compris ;
- les outils de connecteur, appel direct comme `oto_call`, dans
  `activation_gate.require_active` — leur seam commun ;
- les travaux de fond : un travail d'org suspendue n'est pas réservé
  (`claim_next_job`), un webhook entrant est refusé, une échéance cron n'enfile rien.

**Ce qui reste ouvert** (`OUVERTES`) : se voir, lister ses orgs, lire l'org, en
changer. Sans ça, un membre dont l'org PAR DÉFAUT est suspendue ne pourrait même plus
passer à une autre. Les opérations de plateforme (`admin.*`) aussi : c'est par elles
qu'on lève la suspension.

**Ce qui ne s'arrête pas** : rien n'est supprimé ni détaché. Les routes REST écrites à
la main (export CSV d'un tableau, logo…) ne passent pas par ces gardes — un export de
ses propres données reste possible, c'est voulu.

**Aucune lecture de base par appel** : la liste des orgs suspendues est gardée en
mémoire et relue toutes les `TTL_S` secondes (une requête par processus). Une org
active — la quasi-totalité — se tranche par une recherche dans un ensemble. La base
n'est lue que pour une org SUSPENDUE, pour le motif de son refus. Le prix : une
suspension ou une levée atteint les AUTRES processus en `TTL_S` au plus ; celui qui
a traité le geste d'admin la voit tout de suite (`invalider`).

⚠️ Fail-closed comme la pause de compte : si la PREMIÈRE lecture échoue, l'appel
échoue. Une relecture qui échoue ensuite garde la dernière liste connue (une
suspension posée n'est pas oubliée) et retente sous `RETRY_S` secondes.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import org_store

logger = logging.getLogger(__name__)

CODE = "org_suspended"

#: Les capacités qu'une org suspendue sert encore. Les `me.*` résolvent l'org PAR
#: DÉFAUT du compte : sans cette liste, un membre dont l'espace perso est suspendu
#: perdrait aussi ses réglages de COMPTE alors qu'il travaille dans une org qui paie.
#: N'y entrent que des gestes de compte, ou qui lisent / retirent — jamais ce qui
#: consomme ou agit dans l'org (`me.tools.call`, `me.project_file.*`, les connexions).
OUVERTES = frozenset({
    # se repérer, changer d'org, en partir
    "me.get", "me.context", "me.leave_org",
    "org.list", "org.get", "org.use_org", "org.set_home", "org.clear",
    # le compte lui-même
    "me.legal.get", "me.legal.accept", "me.avatar.clear",
    "me.token.list", "me.token.create", "me.token.delete",
    # lire ou retirer un accès — jamais en ouvrir un
    "me.credential.get", "me.credential.clear",
    "me.connector_status", "me.connector_disconnect",
    "me.unipile.status", "me.unipile.disconnect",
    "me.federation.google.status", "me.federation.google.revoke",
    "me.model_subscriptions.list", "me.model_subscriptions.remove",
})


TTL_S = 30.0
RETRY_S = 5.0

_verrou = threading.Lock()
_cache: dict = {"ids": frozenset(), "lu_a": None}


def _suspendues() -> frozenset:
    """La liste en mémoire, relue au plus toutes les `TTL_S` secondes."""
    lu_a = _cache["lu_a"]
    if lu_a is not None and time.monotonic() - lu_a < TTL_S:
        return _cache["ids"]
    with _verrou:
        lu_a = _cache["lu_a"]
        if lu_a is not None and time.monotonic() - lu_a < TTL_S:
            return _cache["ids"]
        try:
            ids = frozenset(org_store.suspended_org_ids())
        except Exception as e:
            if lu_a is None:
                raise                   # jamais lue : refuser, pas laisser passer
            logger.warning("liste des orgs suspendues illisible, dernière liste gardée "
                           "(%d org(s)) : %s", len(_cache["ids"]), e)
            _cache["lu_a"] = time.monotonic() - TTL_S + RETRY_S
            return _cache["ids"]
        _cache.update(ids=ids, lu_a=time.monotonic())
        return ids


def invalider() -> None:
    """Relire la liste au prochain appel — après un geste d'admin dans CE processus."""
    with _verrou:
        # Jamais lue : la laisser à `None`. Y mettre une date ferait d'un premier
        # échec de lecture une liste vide servie — un laisser-passer.
        if _cache["lu_a"] is not None:
            _cache["lu_a"] = time.monotonic() - TTL_S


def etat(org_id: Optional[int]) -> Optional[dict]:
    """L'état de suspension, ou `None` si l'org est active (ou absente). Une org active
    se tranche en mémoire ; seule une org suspendue lit son détail en base."""
    if not org_id or int(org_id) not in _suspendues():
        return None
    return org_store.get_org_suspension(int(org_id))


def message(org_id: int) -> str:
    return (f"L'espace #{org_id} est suspendu : il ne peut plus agir tant qu'il n'a pas "
            "d'abonnement. Rien n'a été supprimé. Un administrateur de l'espace peut "
            "choisir un plan depuis l'app ; d'ici là, agis dans un autre espace "
            "(oto_list_orgs, puis `_org`).")


def refus(org_id: Optional[int]) -> Optional[str]:
    """Le message à servir si l'org est suspendue, `None` sinon. Journalise le refus."""
    pause = etat(org_id)
    if not pause:
        return None
    logger.warning("org suspendue refusée : org=%s depuis=%s motif=%s",
                   org_id, pause.get("suspended_at"), pause.get("suspended_reason"))
    return message(int(org_id))


#: Les surfaces d'OPÉRATEUR : c'est par elles que le service d'usage d'un tenant
#: coupe et rend ses leviers (grants, plafond de messagerie, arêtes de tenant) et lève
#: la suspension. Leurs règles (super admin, admin de tenant) résolvent l'org de
#: l'APPELANT, pas la cible — mais une garde qui dépendrait de ce détail casserait
#: le jour où une règle lirait l'org visée. On les exempte par nom.
_OPERATEUR = ("admin.", "platform.")


def ouverte(cap_key: str) -> bool:
    """Une capacité qu'une org suspendue sert encore."""
    return cap_key in OUVERTES or cap_key.startswith(_OPERATEUR)


def garde_capacite(cap_key: str, ctx) -> None:
    """La garde des capacités, posée dans les DEUX adaptateurs juste après la règle
    d'autz (l'org de l'appel n'est connue qu'à ce moment, champ d'entrée compris).
    Un worker de plateforme n'a pas d'org : ses travaux sont gardés à la réservation."""
    if ctx.platform_worker or ctx.org_id is None or ouverte(cap_key):
        return
    if (texte := refus(ctx.org_id)):
        from .capabilities._types import AuthzDenied
        raise AuthzDenied(403, CODE, texte)
