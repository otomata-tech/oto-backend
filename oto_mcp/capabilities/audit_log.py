"""Export du journal d'audit org-scopé (ADR 0009 ; oto-backend#67).

Le trust center public annonce un « journal d'audit de tous les appels d'outils ».
Ce journal existe (`tool_calls`, via le calllog) mais n'était lisible que par un
opérateur plateforme (`/api/admin/monitoring/*`). Ici on l'ouvre à un **org_admin**
pour SON org — preuve de conformité (RGPD art. 28, ISO 42001), revue, dossier client.

Surface : capacité **REST-only** `GET /api/orgs/{id}/audit-log/export`, gatée
`ORG_ADMIN_OF`. Retourne du JSON structuré (`{org_id, count, calls[]}`) — le bouton
« exporter CSV » du dashboard sérialise ce JSON côté client (l'adaptateur REST des
capacités ne produit que du JSON ; pas de stream text/csv ici).

Org-scoping = **exact** : on filtre `tool_calls.org_id` (l'org sous laquelle l'appel a
été émis, stampée par le seam `current_org` à l'insert) — PAS l'appartenance des
membres (un membre de N orgs ne pollue donc pas l'export). ⚠ Les appels antérieurs à la
colonne `org_id` (NULL) n'apparaissent dans aucun export — non reconstructibles.
Jamais d'args ni de secret (garantie calllog) — colonnes : horodatage, user (sub/email),
outil, namespace, durée, ok, erreur.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import db
from ..tool_visibility import namespace_of
from ._authz import ORG_ADMIN_OF
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class AuditExportInput(BaseModel):
    org_id: int
    since: Optional[str] = None       # borne basse ISO (timestamptz), incluse
    until: Optional[str] = None       # borne haute ISO, incluse
    limit: int = 1000


def _export(ctx: ResolvedCtx, inp: AuditExportInput) -> dict:
    calls = db.list_tool_calls_for_org(inp.org_id, since=inp.since, until=inp.until, limit=inp.limit)
    for c in calls:
        c["namespace"] = namespace_of(c["tool"]) if c.get("tool") else None
    return {"org_id": inp.org_id, "since": inp.since, "until": inp.until,
            "count": len(calls), "calls": calls}


CAPABILITIES += [
    Capability(
        key="org.audit_log.export", handler=_export, Input=AuditExportInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Org audit log of tool calls (org_admin): timestamp, user, tool, "
                    "namespace, duration, ok/error — never args or secrets. Window via "
                    "since/until (ISO). Scoped to calls emitted UNDER this org.",
        rest=RestBinding("GET", "/api/orgs/{id}/audit-log/export", _ID),
    ),
]
