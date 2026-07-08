"""Capacités de gestion de la file d'envoi d'email différé (ADR 0009).

`email_send` peut différer un envoi (paramètre `send_at` ou garde-fou quiet hours
de l'org). Ces capacités permettent de **lister** et d'**annuler** les emails encore
en attente. Lecture + annulation = membre de l'org (un envoi part au nom de l'org).

Une déclaration → MCP `oto_*` + REST `/api/orgs/{id}/scheduled-emails`.
Pattern de référence : `orgs_invites.py` (list + action par id).
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import org_store
from ._authz import ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class ScheduledListInput(BaseModel):
    org_id: int
    status: str = "pending"     # pending | sent | failed | cancelled | all


class ScheduledCancelInput(BaseModel):
    org_id: int
    email_id: int


def _scheduled_list(ctx: ResolvedCtx, inp: ScheduledListInput) -> dict:
    return {"scheduled_emails": org_store.list_scheduled_emails(inp.org_id, status=inp.status)}


def _scheduled_cancel(ctx: ResolvedCtx, inp: ScheduledCancelInput) -> dict:
    if not org_store.cancel_scheduled_email(inp.org_id, inp.email_id):
        raise AuthzDenied(404, "unknown_scheduled_email",
                          "Email introuvable, déjà parti ou déjà annulé.")
    return {"ok": True, "cancelled": inp.email_id}


CAPABILITIES += [
    Capability(
        key="org.scheduled_email.list", handler=_scheduled_list, Input=ScheduledListInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("List the org's scheduled (deferred) emails. `status` filters "
                     "pending|sent|failed|cancelled|all (default pending)."),
        rest=RestBinding("GET", "/api/orgs/{id}/scheduled-emails", _ID),
    ),
    Capability(
        key="org.scheduled_email.cancel", handler=_scheduled_cancel, Input=ScheduledCancelInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="Cancel a still-pending scheduled email of the org by id.",
        rest=RestBinding("DELETE", "/api/orgs/{id}/scheduled-emails/{eid}",
                         {"id": "org_id", "eid": "email_id"}),
    ),
]
