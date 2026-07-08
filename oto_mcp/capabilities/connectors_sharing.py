"""Capacité de PARTAGE d'instance (ADR 0044) — surface d'ÉCRITURE de `share_side`.

`share_side` = EXTENSION : un membre prête SON instance (sa clé dans l'org courante)
à un pair nommé. L'usage se fait ensuite en pinnant l'instance (`instance=`) ; la
garde `access.guard_instance_access` autorise le bénéficiaire (emprunte la clé, garde
son PROPRE contexte d'org — cross-org OK, le prêt nominatif est le consentement).
Owner-scopé : `SUB_ONLY`, le handler ne touche QUE la ligne du coffre du caller
(`member_id(org courante, sub)`) → on ne prête jamais que sa propre clé.

⚠️ `share_down` (RESTREINDRE une clé partagée d'org/équipe) est l'axe OPPOSÉ (deny-by-
default) et le MÊME primitif que `org_connector_access` (§B.bis) — surface d'écriture
distincte, à unifier, non exposée ici.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .. import access, credentials_store, db, providers
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class LendInstanceInput(BaseModel):
    connector: str
    to: str = Field(description="sub du pair à qui prêter (ou à qui révoquer le prêt)")
    account: str = ""
    revoke: bool = False


def _lend_instance(ctx: ResolvedCtx, inp: LendInstanceInput) -> dict:
    if providers.connector_for_provider(inp.connector) is None:
        raise AuthzDenied(400, "unknown_connector", f"Connecteur `{inp.connector}` inconnu.")
    org = access.current_org(ctx.sub)
    if org is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — impossible de prêter une instance.")
    if inp.to == ctx.sub:
        raise AuthzDenied(400, "self_lend", "Prêter à soi-même n'a pas de sens.")
    if db.get_user(inp.to) is None:
        raise AuthzDenied(404, "unknown_user", f"Utilisateur `{inp.to}` inconnu.")
    eid = credentials_store.member_id(org, ctx.sub)
    _, side = credentials_store.get_instance_sharing(
        credentials_store.MEMBER, eid, inp.connector, inp.account)
    entry = f"user:{inp.to}"
    side = list(side or [])
    if inp.revoke:
        side = [s for s in side if s != entry]
    elif entry not in side:
        side.append(entry)
    ok = credentials_store.set_instance_sharing(
        credentials_store.MEMBER, eid, inp.connector, inp.account, share_side=side)
    if not ok:
        raise AuthzDenied(404, "no_instance",
                          f"Aucune instance `{inp.connector}` posée dans cette org — "
                          f"rien à prêter (configure d'abord ta clé).")
    return {"ok": True, "connector": inp.connector, "revoked": inp.revoke,
            "lent_to": [s[len("user:"):] for s in side if s.startswith("user:")]}


CAPABILITIES += [
    Capability(
        key="connectors.lend_instance", handler=_lend_instance, Input=LendInstanceInput,
        authz=SUB_ONLY,
        description=("Lend YOUR connector instance (your key in your current org) to a "
                     "peer so they can use it by pinning it (instance=). `to`=peer's sub; "
                     "`revoke=true` takes it back. You only ever share your OWN key; the "
                     "borrower operates under THEIR own org context (ADR 0044 share_side)."),
        rest=RestBinding("POST", "/api/me/connectors/{connector}/lend", {"connector": "connector"}),
    ),
]
