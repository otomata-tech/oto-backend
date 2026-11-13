"""Org suspendue : le prédicat, la phrase qui la refuse, et ce qui reste ouvert.

**Le cran au-dessus de la pause de compte** (`account_suspension`). Une pause de
compte arrête une PERSONNE, dans toutes ses orgs. Une suspension d'org arrête un
ESPACE, pour tous ses membres — et seulement lui : un membre garde ses autres orgs.
C'est le geste d'un tenant qui facture ses orgs (02/10/2026 : un essai fini sans
abonnement arrête l'app, pas seulement les clés payées par le tenant).

**Où il tombe.** Il n'y a pas UN point d'entrée par org, donc cinq gardes, toutes
sur ce module :
- les capacités, juste après la règle d'autz, dans les deux adaptateurs (MCP et
  REST) — c'est là que l'org de l'appel est connue, champ d'entrée compris ;
- les outils hors capacité — de connecteur, et les outils de plateforme écrits à la
  main (`data_*`, `run_*`, `oto_doc_app`…) —, appel direct comme cible d'`oto_call`,
  dans `activation_gate.require_active` — leur seam commun (`outil_garde`) ;
- les travaux de fond : un travail d'org suspendue n'est pas réservé
  (`claim_next_job`), un webhook entrant est refusé, une échéance cron n'enfile rien ;
- l'endpoint d'un projet publié sans login (`subdomain_project`) : sans `sub`, aucune
  garde d'appel ne le voit, il se ferme entier.

**Suspendue par son tenant** : désactiver un tenant suspend toutes ses orgs par ce même
mécanisme, origine marquée (`orgs.suspended_tenant_id`) — sa réactivation ne lève que
celles-là (`docs/orgs-suspendues.md` §4).

**Ce qui reste ouvert** (`OUVERTES`) : se voir, lister ses orgs, lire l'org, en
changer. Sans ça, un membre dont l'org PAR DÉFAUT est suspendue ne pourrait même plus
passer à une autre. La facturation de l'org (`billing.*` hors `admin_*`) : sans elle,
l'admin ne pourrait pas payer pour lever la suspension. Les opérations de plateforme
(`admin.*`, `platform.*`) aussi, et le service du commerce (sans org, jamais gardé) :
c'est par eux qu'on suspend et qu'on lève.

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
    # rejoindre une AUTRE org par l'invitation qui y attend : l'appel résout sous
    # l'org maison, mais l'org rejointe est `{id}` — c'est elle que la capacité juge.
    "org.invite.claim",
    # même chose par le jeton du mail (04/11/2026 : un invité dont l'espace perso a
    # fini son essai ne pouvait plus rejoindre l'org qui l'invitait) — le handler juge
    # l'org de l'INVITATION. Décliner une invitation n'ouvre rien.
    "org.invite.accept", "org.invite.reject",
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


#: La facturation de l'org reste ouverte, sauf ses leviers d'opérateur : c'est par
#: elle qu'un admin choisit un plan et lève la suspension (le message de refus l'y
#: envoie). Préfixe plutôt que liste : un geste de facturation ajouté plus tard ne
#: doit pas se retrouver refusé dans l'org même qu'il sert à rouvrir.
_FACTURATION, _FACTURATION_OPERATEUR = "billing.", "billing.admin_"

#: Les outils hors capacité qu'une org suspendue sert encore : se voir, lire un
#: schéma — et `oto_call` lui-même, dont la CIBLE repasse par la garde, dans l'org
#: que ses axes posent (le garder ici refuserait l'appel avant que `_org` ne bascule
#: vers un espace actif, ce que le message de refus conseille justement).
OUTILS_OUVERTS = frozenset({"oto_whoami", "oto_tool_schema", "oto_call"})


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
    return (cap_key in OUVERTES or cap_key.startswith(_OPERATEUR)
            or (cap_key.startswith(_FACTURATION)
                and not cap_key.startswith(_FACTURATION_OPERATEUR)))


def _outils_de_capacite() -> frozenset:
    """Les noms d'outil MCP des capacités : elles sont gardées dans leur adaptateur
    (avec `OUVERTES`), pas une seconde fois au seam des outils."""
    from .capabilities.registry import CAPABILITIES
    return frozenset(c.mcp for c in CAPABILITIES if c.mcp)


def outil_garde(tool_name: str) -> bool:
    """Un outil SANS connecteur que la suspension garde au seam des outils : les
    outils de plateforme écrits à la main (`data_*`, `run_*`, `oto_doc_app`…). Ni une
    capacité (gardée dans son adaptateur), ni un outil ouvert (`OUTILS_OUVERTS`)."""
    return tool_name not in OUTILS_OUVERTS and tool_name not in _outils_de_capacite()


#: Les capacités d'une APP, jugées par l'org de l'APP et non par celle de l'appel
#: (13/11/2026). Un invité de l'extérieur ouvre une app partagée depuis SON org maison,
#: celle de l'appel (`ORG_MEMBER`, `SUB_ONLY`) : quand l'essai de cet espace à lui finit,
#: l'app d'une org qui paie ne doit pas s'éteindre pour lui — ni s'ouvrir quand c'est
#: l'org de l'app qui est suspendue. L'adaptateur les laisse passer ; leur handler
#: appelle `exiger` sur la bonne org (`capabilities/apps.py`, `apps_appel._app_pour`) :
#: celle de l'app pour un geste sur une app, l'org de l'appel pour créer ou lister.
JUGEES_PAR_L_APP = frozenset({"me.app", "me.app.call", "me.app.consent"})


def exiger(org_id: Optional[int]) -> None:
    """Refuse (403 `org_suspended`) si `org_id` est suspendue — la garde que le handler
    d'une capacité de `JUGEES_PAR_L_APP` pose lui-même, sur l'org qu'il sait juger."""
    if org_id is None:
        return
    if (texte := refus(int(org_id))):
        from .capabilities._types import AuthzDenied
        raise AuthzDenied(403, CODE, texte)


def garde_capacite(cap_key: str, ctx) -> None:
    """La garde des capacités, posée dans les DEUX adaptateurs juste après la règle
    d'autz (l'org de l'appel n'est connue qu'à ce moment, champ d'entrée compris).
    Un worker de plateforme n'a pas d'org : ses travaux sont gardés à la réservation."""
    if ctx.platform_worker or ctx.org_id is None or ouverte(cap_key):
        return
    if cap_key in JUGEES_PAR_L_APP:
        return  # jugée par son handler, sur l'org de l'app (`exiger`)
    if (texte := refus(ctx.org_id)):
        from .capabilities._types import AuthzDenied
        raise AuthzDenied(403, CODE, texte)
