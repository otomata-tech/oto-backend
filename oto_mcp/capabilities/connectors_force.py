"""Forcer un connecteur dans la toolbox d'un membre (ADR 0031) — surface org_admin.

L'org_admin POUSSE un connecteur à un membre nommé de son org : pose un override
positif de visibilité (`user_enabled_tools`) sur tous les tools du connecteur, scopé
sur cette org. Le membre le voit sans rien activer, et reste libre de le re-masquer
(`oto_disable_tool` lève l'override). C'est de la **VISIBILITÉ** (préférence imposée),
PAS un grant d'accès — l'accès réel reste gardé au call-time (credential + ADR 0025).

Pendant de `connectors_acl` (ADR 0025) : l'ACL *restreint* un connecteur à un
sous-ensemble de l'org (deny) ; ici on *pousse* un connecteur à un membre (allow).

autz `ORG_ADMIN_OF` : l'org_admin gouverne SON org (super_admin escalade via roles).
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db, org_store, providers, tool_registry
from ..tool_visibility import namespace_of
from ._authz import ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID_CONN = {"id": "org_id", "connector": "connector"}


class ForceConnectorInput(BaseModel):
    org_id: int
    connector: str
    member: str  # sub Logto OU email du membre cible


def _resolve_member(org_id: int, target: str) -> str:
    """Résout le sub du membre cible (email accepté) + vérifie son appartenance à l'org."""
    sub = target
    if "@" in target:
        u = db.get_user_by_email(target)
        if not u:
            raise AuthzDenied(404, "unknown_user", f"Aucun user avec l'email `{target}`.")
        sub = u["sub"]
    if org_store.get_org_role(org_id, sub) is None:
        raise AuthzDenied(400, "user_not_in_org",
                          f"`{target}` n'est pas membre de l'org #{org_id}.")
    return sub


async def _force_connector(ctx: ResolvedCtx, inp: ForceConnectorInput) -> dict:
    con = providers.connector_for_provider(inp.connector)
    if con is None:
        raise AuthzDenied(400, "unknown_connector", f"Connecteur `{inp.connector}` inconnu.")
    sub = _resolve_member(inp.org_id, inp.member)
    reg = await tool_registry.build_registry()  # {tool_name: entry}, instance bindée au boot
    names = [n for n in reg if namespace_of(n) in con.namespaces]
    for n in names:
        db.add_user_enabled_tool(sub, n, inp.org_id)
    return {"ok": True, "org_id": inp.org_id, "connector": inp.connector,
            "member": sub, "tools_forced": len(names)}


CAPABILITIES += [
    Capability(
        key="connectors.force.member", handler=_force_connector, Input=ForceConnectorInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Force a connector into a member's toolbox IN THIS ORG: sets a "
                    "positive visibility override on all the connector's tools for the target member "
                    "(`member` = sub or email). They see it without enabling it, and can still hide it "
                    "(oto_disable_tool lifts the override). Visibility only — NOT an access grant.",
        rest=RestBinding("POST", "/api/orgs/{id}/connectors/{connector}/force", _ID_CONN),
    ),
]
