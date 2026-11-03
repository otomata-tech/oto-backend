"""Capabilities "Apify ceiling per run of an org" — the org admin sets it, within the
platform's bounds.

Decision of 03/11/2026: the ceiling of every Apify launch (`tools/apify.py`,
`_borne_du_lancement`) is a setting of the ORG (`connector_settings`, scope `org`, key
`apify.CLE_PLAFOND_ORG`; no team scope), $25 when the org set none. On the platform's
key it never exceeds the platform maximum (`max_charge_cap_usd`); on a key another org
lends, that org's ceiling too (the stricter of the two: the lender pays); on any org's or
person's key, the platform's safety bound (`byok_charge_cap_usd`, $1,000).

Here, the two verbs: read (any member: whoever meets the refusal may read the bound) and
set (`ORG_ADMIN_OF`). A setting is refused above the bound the org can reach: the
platform maximum when the org has no Apify key of its own (it runs on the platform's),
the safety bound when it has one. Who set it and when = the row's `set_by`/`set_at`,
like the access level. REST `/api/orgs/{id}/connectors/{name}/charge-cap` — a generic
path like the access level's (no REST face names a connector); a `name` other than
`apify` is refused (404 `no_charge_cap`). MCP face =
`oto_org_settings` domain `apify_charge_cap` (`org_console.py`).
"""
from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel

from ... import org_store
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}      # placeholder {id} → Input field org_id
CONNECTOR = "apify"


class OrgChargeCapInput(BaseModel):
    org_id: int
    name: str = CONNECTOR      # connector (placeholder {name}, auto-mapped)


class OrgChargeCapSetInput(OrgChargeCapInput):
    usd: float


class ChargeCapState(BaseModel):
    """The Apify ceiling per run of an org. `usd` = what the org set (None = nothing,
    `default_usd` applies). `own_key` = the org has an Apify key of its own; `bound_usd`
    = the highest value it may set (`bound`: `platform` = the platform maximum, without
    its own key; `byok` = the platform's safety bound, with it). The ceiling in force
    on a launch: `platform_key_usd` on the platform's key, `own_key_usd` on the org's or
    a member's own key. `set_by`/`set_at`: who set it, and when."""
    connector: str
    org_id: int
    usd: Optional[float] = None
    default_usd: float
    own_key: bool
    bound: str
    bound_usd: float
    platform_key_usd: float
    own_key_usd: float
    set_by: Optional[str] = None
    set_at: Optional[str] = None


def _exiger_apify(name: str) -> None:
    """Only `apify` has a ceiling per run: another connector is refused by name."""
    if name != CONNECTOR:
        raise AuthzDenied(404, "no_charge_cap",
                          f"Connector `{name}` has no ceiling per run: only `apify` does.")


def _exiger_org(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} unknown.")


def _ligne(org_id: int) -> Optional[dict]:
    from ...db import connector_settings
    from ...tools import apify
    for r in connector_settings.list_connector_settings(apify.CLE_PLAFOND_ORG):
        if (r["scope_type"] == "org" and str(r["scope_id"]) == str(org_id)
                and r["connector"] == CONNECTOR):
            return r
    return None


def _state(org_id: int) -> dict:
    from ...tools import apify
    pose = apify._plafond_de_l_org(org_id)
    ligne = _ligne(org_id) if pose is not None else None
    valeur = apify._PLAFOND_ORG_DEFAUT_USD if pose is None else pose
    maximum, byok = apify._plafond_max(), apify._borne_byok()
    propre = org_store.has_org_secret(org_id, CONNECTOR)
    set_at = ligne.get("set_at") if ligne else None
    return {"connector": CONNECTOR, "org_id": org_id, "usd": pose,
            "default_usd": apify._PLAFOND_ORG_DEFAUT_USD, "own_key": propre,
            "bound": "byok" if propre else "platform",
            "bound_usd": byok if propre else maximum,
            "platform_key_usd": min(valeur, maximum), "own_key_usd": min(valeur, byok),
            "set_by": ligne.get("set_by") if ligne else None,
            "set_at": set_at if isinstance(set_at, (str, type(None))) else set_at.isoformat()}


def _get(ctx: ResolvedCtx, inp: OrgChargeCapInput) -> dict:
    _exiger_apify(inp.name)
    _exiger_org(inp.org_id)
    return _state(inp.org_id)


def _set(ctx: ResolvedCtx, inp: OrgChargeCapSetInput) -> dict:
    from ...db import connector_settings
    from ...tools import apify
    _exiger_apify(inp.name)
    _exiger_org(inp.org_id)
    usd = inp.usd
    if not (math.isfinite(usd) and usd > 0):
        raise AuthzDenied(400, "invalid_charge_cap",
                          f"`usd` must be a positive amount of dollars (got {usd!r}). "
                          "Nothing was changed.")
    if org_store.has_org_secret(inp.org_id, CONNECTOR):
        borne = apify._borne_byok()
        if usd > borne:
            raise AuthzDenied(
                409, "charge_cap_above_byok_bound",
                f"${usd:g} is above this platform's safety bound of ${borne:g} per Apify "
                "run, which holds even on your organization's own Apify key. Nothing was "
                "changed.")
    else:
        maximum = apify._plafond_max()
        if usd > maximum:
            raise AuthzDenied(
                409, "charge_cap_above_platform_max",
                f"${usd:g} is above this platform's maximum of ${maximum:g} per Apify "
                f"run: organization #{inp.org_id} has no Apify key of its own, so its runs "
                "go on the platform's key, which this maximum bounds. Set your "
                "organization's Apify key to go beyond it. Nothing was changed.")
    connector_settings.set_connector_setting(
        "org", str(inp.org_id), CONNECTOR, apify.CLE_PLAFOND_ORG, repr(float(usd)), ctx.sub)
    return _state(inp.org_id)


_PATH = "/api/orgs/{id}/connectors/{name}/charge-cap"
_ORG_ERR = (DeclaredError(404, "no_charge_cap",
                          "seul le connecteur apify a un plafond par lancement"),
            DeclaredError(404, "unknown_org", "l'org n'existe pas"))

CAPABILITIES += [
    Capability(
        key="connectors.apify.charge_cap.org_get", handler=_get,
        Input=OrgChargeCapInput, Output=ChargeCapState,
        authz=ORG_MEMBER_OF("org_id"), errors=_ORG_ERR,
        description="[org member] Your org's ceiling per Apify run: the value set (or "
                    "the $25 default), the highest one it may set, and the ceiling in "
                    "force on the platform's key and on an own key.",
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="connectors.apify.charge_cap.set_org", handler=_set,
        Input=OrgChargeCapSetInput, Output=ChargeCapState,
        authz=ORG_ADMIN_OF("org_id"),
        errors=_ORG_ERR + (
            DeclaredError(400, "invalid_charge_cap", "montant non positif ou non fini"),
            DeclaredError(409, "charge_cap_above_platform_max",
                          "sans clé Apify propre, au-delà du maximum de la plateforme"),
            DeclaredError(409, "charge_cap_above_byok_bound",
                          "avec sa clé Apify, au-delà de la borne de sécurité de la "
                          "plateforme")),
        description="[org admin] Set your org's ceiling per Apify run (`usd`): every "
                    "launch of its members is refused above it. Without an Apify key of "
                    "its own, at most the platform maximum; with one, at most the "
                    "platform's safety bound.",
        rest=RestBinding("PUT", _PATH, _ID),
    ),
]
