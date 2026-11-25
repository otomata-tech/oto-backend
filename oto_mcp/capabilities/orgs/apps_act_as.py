"""Capacité : les apps de l'org peuvent-elles PRÊTER l'accès d'une personne nommée
(`orgs.apps_act_as`, `docs/apps.md` § Une app qui prête l'accès de quelqu'un).

Une app ouverte lit avec les droits de son lecteur ; une app qui prête montre ce qu'elle
déclare avec ceux de la personne qui s'y est nommée (`oto_apps op=act_as`), à qui elle
est partagée. Le réglage de l'ORG de l'app décide s'il peut y en avoir : le choix d'un
admin (`true` / `false`), sinon le défaut de l'instance (`OTO_APPS_ACT_AS_DEFAULT`),
éteint s'il n'est pas déclaré. Éteint, aucune app de l'org ne prête — celles qui
prêtaient reviennent aux droits de leur lecteur, sans rien perdre de leur réglage.

Lecture = membre ; écriture = org_admin. Une déclaration → deux surfaces : REST
`/api/orgs/{id}/apps-act-as` ici, MCP par la console
`oto_org_settings domain=apps_act_as` (`org_console.py`).
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel

from ... import db, org_store
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/apps-act-as"


class GetAppsActAsInput(BaseModel):
    org_id: int


class SetAppsActAsInput(BaseModel):
    org_id: int
    enabled: bool


class AppsActAs(BaseModel):
    org_id: int
    #: Ce qui s'applique : les apps de l'org peuvent-elles prêter.
    enabled: bool
    #: Le choix d'un admin de l'org ; `None` = aucun, le défaut de l'instance s'applique.
    chosen: Optional[bool] = None
    #: Le défaut de l'instance (`OTO_APPS_ACT_AS_DEFAULT`).
    instance_default: bool


def defaut_de_l_instance() -> bool:
    """Le défaut de l'instance quand une org n'a rien choisi : éteint s'il n'est pas
    déclaré (`OTO_APPS_ACT_AS_DEFAULT`). Lu à chaque appel."""
    return os.environ.get("OTO_APPS_ACT_AS_DEFAULT", "").strip().lower() in (
        "1", "true", "yes", "on")


def permis(org_id: Optional[int]) -> bool:
    """Les apps de l'org `org_id` peuvent-elles prêter : le choix de l'org, sinon le
    défaut de l'instance. Aucune org = non."""
    if org_id is None:
        return False
    choix = db.apps.apps_act_as_de_l_org(int(org_id))
    return choix if choix is not None else defaut_de_l_instance()


def _lire(org_id: int) -> dict:
    if org_store.get_org(org_id) is None:
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    choix = db.apps.apps_act_as_de_l_org(org_id)
    defaut = defaut_de_l_instance()
    return {"org_id": org_id, "enabled": choix if choix is not None else defaut,
            "chosen": choix, "instance_default": defaut}


def lire(org_id: int) -> dict:
    return _lire(org_id)


def poser(org_id: int, enabled: bool) -> dict:
    if not db.apps.poser_apps_act_as(org_id, enabled):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    return _lire(org_id)


def _get(ctx: ResolvedCtx, inp: GetAppsActAsInput) -> dict:
    return _lire(inp.org_id)


def _set(ctx: ResolvedCtx, inp: SetAppsActAsInput) -> dict:
    return poser(inp.org_id, inp.enabled)


_ERREURS = (DeclaredError(404, "unknown_org", "l'org n'existe pas"),)

CAPABILITIES += [
    Capability(
        key="org.apps_act_as.get", handler=_get, Input=GetAppsActAsInput,
        authz=ORG_MEMBER_OF("org_id"), Output=AppsActAs, errors=_ERREURS,
        description=("Read whether the org's apps may lend a named person's access "
                     "(`oto_apps op=act_as`): `enabled` = what applies, `chosen` = the "
                     "org admin's choice (null = none), `instance_default`."),
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="org.apps_act_as.set", handler=_set, Input=SetAppsActAsInput,
        authz=ORG_ADMIN_OF("org_id"), Output=AppsActAs, errors=_ERREURS,
        description=("Allow or turn off, for this org, apps that show what they declare "
                     "with a named person's access to the people they are shared with "
                     "(`oto_apps op=act_as`). Off: no app of the org lends anything; "
                     "their viewers read with their own access again. Org admin."),
        rest=RestBinding("PUT", _PATH, _ID),
    ),
]
