"""Capacité : MFA obligatoire par org (ADR 0009 + voie « org Logto miroir »).

Un org_admin active/désactive l'exigence du 2ᵉ facteur pour les membres de l'org.
L'activation provisionne une **organization Logto miroir** (isMfaRequired + membres
synchronisés par `sub`) via `mfa_mirror` ; combiné au réglage tenant
`organizationRequiredMfaPolicy=Mandatory`, Logto force alors le MFA au **login
ordinaire** de tout membre. Voir `mfa_mirror.py` et `docs/auth-logto.md` §MFA par org.

Lecture = membre ; écriture = org_admin. **Pas de fail-open** : si le provisioning
Logto échoue, le drapeau n'est pas posé (activation) ou reste posé (désactivation)
— l'état PG ne prétend jamais un MFA actif qui ne l'est pas.

Une déclaration → deux surfaces (MCP `oto_*` + REST `/api/orgs/{id}/mfa`).
Pattern de référence : `orgs_field_filters.py`.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import mfa_mirror, org_store
from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class GetOrgMfaInput(BaseModel):
    org_id: int


class SetOrgMfaInput(BaseModel):
    org_id: int
    require: bool


def _get_org_mfa(ctx: ResolvedCtx, inp: GetOrgMfaInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    state = org_store.get_org_mfa(inp.org_id)
    return {"org_id": inp.org_id, "require_mfa": state["require_mfa"],
            "provisioned": bool(state["logto_org_id"])}


def _set_org_mfa(ctx: ResolvedCtx, inp: SetOrgMfaInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if inp.require:
        # Provisionner AVANT de poser le drapeau : si Logto échoue, ensure_mirror
        # lève → drapeau reste false, l'org sait que ce n'est PAS actif (pas de
        # fail-open sur un contrôle de sécurité).
        try:
            mfa_mirror.ensure_mirror(inp.org_id)
        except Exception as e:
            raise AuthzDenied(502, "logto_provisioning_failed",
                              f"Impossible d'activer le MFA côté Logto : {e}")
        org_store.set_org_require_mfa(inp.org_id, True)
    else:
        # Retirer l'exigence Logto AVANT le drapeau : si ça échoue, le drapeau reste
        # true (toujours enforced) — fail-closed pour un contrôle de sécurité.
        try:
            mfa_mirror.disable_mirror(inp.org_id)
        except Exception as e:
            raise AuthzDenied(502, "logto_deprovisioning_failed",
                              f"Impossible de retirer l'exigence MFA côté Logto : {e}")
        org_store.set_org_require_mfa(inp.org_id, False)
    return {"ok": True, "org_id": inp.org_id, "require_mfa": inp.require}


CAPABILITIES += [
    Capability(
        key="org.mfa.get", handler=_get_org_mfa, Input=GetOrgMfaInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("Read whether this org requires its members to use MFA (a second "
                     "factor). Returns require_mfa and whether the Logto enforcement "
                     "mirror is provisioned."),
        rest=RestBinding("GET", "/api/orgs/{id}/mfa", _ID),
    ),
    Capability(
        key="org.mfa.set", handler=_set_org_mfa, Input=SetOrgMfaInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Turn the org's mandatory-MFA requirement on/off (require=true|false). "
                     "When on, every member must enroll and use a second factor at their "
                     "next sign-in (enforced by Logto). Provisions/updates the Logto "
                     "enforcement mirror; on Logto failure it errors WITHOUT changing state."),
        rest=RestBinding("PUT", "/api/orgs/{id}/mfa", _ID),
    ),
]
