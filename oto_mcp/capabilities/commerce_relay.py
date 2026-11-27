"""Ce que le COMMERCE tient et qu'un agent doit pouvoir lire : crédits, tarifs, limites de
dépense — relayé tel quel, en lecture seule.

**Pourquoi.** Le cœur ne tient pas les crédits : une unité y est un entier, que seul le
commerce convertit (`docs/credits-au-moment-de-l-appel.md`). Le solde de la période,
l'allocation, ce qui est coupé et pourquoi, le barème, les limites de dépense du jour ne
vivent que chez lui. Un membre les lit sur les pages du commerce ; un agent, lui, n'avait
rien, et lançait une campagne sans savoir ce qu'il lui restait. Ce module ne recopie rien
de ces chiffres : il demande au commerce, au moment de l'appel, et rend sa réponse.

**Config, sans défaut** (une instance qui n'en veut pas ne voit pas l'outil) :

- `OTO_COMMERCE_RELAY_URL` — la base des lectures, sans `/` final ; les chemins du
  contrat ci-dessous y sont ajoutés. Vide = outil non monté.
- `OTO_COMMERCE_RELAY_TOKEN` — envoyé en `Authorization: Bearer`. Vide = outil non monté
  non plus : on n'envoie jamais un appel de service sans secret.

**Le contrat** (ce que le commerce sert, tout en `GET`) :

| op | chemin | qui |
|---|---|---|
| `usage` | `/orgs/{org_id}/credits` | membre |
| `daily` | `/orgs/{org_id}/credits/daily?days=` | membre ; le commerce peut le réserver à l'admin |
| `agents` | `/orgs/{org_id}/credits/agents?days=` | idem |
| `spend_limits` | `/orgs/{org_id}/spend-limits` | membre (le commerce borne ce qu'il voit) |
| `pricing` | `/pricing` | tout compte |

Chaque appel porte `X-Oto-Sub` (la personne) et `X-Oto-Org-Role` — son rôle **dans
l'org** (`org_admin` | `org_member`, escalade plateforme incluse, comme `ORG_ADMIN_OF`),
jamais le rôle de plateforme que porte `ResolvedCtx.role`. L'appartenance est vérifiée
ICI d'abord (règle d'autz) : le commerce ne reçoit jamais une org que l'appelant ne voit
pas, et décide seul de ce qu'un membre non admin y lit.

**Tenant primaire seulement**, comme le crochet de création d'org : l'org d'un tenant
hébergé ne relève pas du commerce de l'instance. Tenant primaire non déclaré = refus,
on ne devine pas.

**Lecture seule.** Aucune op n'écrit : une limite, un plan, une recharge se changent sur
les pages du commerce, où un humain les voit.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

import requests
from pydantic import BaseModel, ConfigDict, Field

from .. import roles, tenancy
from ..db import tenants
from ._authz import BY_OP, ORG_MEMBER_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_TIMEOUT_S = 20

_CHEMINS = {
    "usage": "/orgs/{org_id}/credits",
    "daily": "/orgs/{org_id}/credits/daily",
    "agents": "/orgs/{org_id}/credits/agents",
    "spend_limits": "/orgs/{org_id}/spend-limits",
    "pricing": "/pricing",
}


def _config() -> "tuple[str, str] | None":
    url = os.environ.get("OTO_COMMERCE_RELAY_URL", "").strip().rstrip("/")
    token = os.environ.get("OTO_COMMERCE_RELAY_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def is_enabled() -> bool:
    """Gate de montage : l'outil n'existe que si l'instance déclare son commerce."""
    return _config() is not None


class CreditsInput(BaseModel):
    op: Literal["usage", "daily", "agents", "spend_limits", "pricing"]
    org_id: Optional[int] = Field(
        default=None, description="The org to read (required except for op=pricing).")
    days: Optional[Literal[7, 30, 90]] = Field(
        default=None, description="op=daily|agents: the window, 7, 30 or 90 days "
                                  "(default 30).")


class CreditsOutput(BaseModel):
    """Ce que le commerce a rendu, sous `result`, tel quel : sa forme est la sienne."""
    model_config = ConfigDict(extra="allow")
    op: str
    org_id: Optional[int] = None
    result: dict


def _du_tenant_primaire(org_id: int) -> None:
    try:
        primaire = tenancy.primary_slug()
    except tenancy.TenantPrimaireNonDeclare:
        raise AuthzDenied(503, "commerce_not_configured",
                          "Primary tenant not declared on this instance.")
    if tenants.org_tenant_slug(org_id) != primaire:
        raise AuthzDenied(404, "no_commerce",
                          f"Org #{org_id} is not billed by this instance's commerce.")


def _relayer(ctx: ResolvedCtx, inp: CreditsInput) -> dict:
    cfg = _config()
    if cfg is None:  # gate de montage contournée (test, rechargement d'env) : jamais ouvert
        raise AuthzDenied(503, "commerce_not_configured",
                          "OTO_COMMERCE_RELAY_URL / OTO_COMMERCE_RELAY_TOKEN unset.")
    base, token = cfg
    headers = {"Authorization": f"Bearer {token}", "X-Oto-Sub": ctx.sub or "",
               "Accept": "application/json"}
    params = {}
    if inp.op == "pricing":
        org_id = None
        # Pas d'org : la personne seule, sans rôle d'org à faire valoir.
        headers["X-Oto-Org-Role"] = roles.ORG_MEMBER
    else:
        org_id = ctx.org_id
        _du_tenant_primaire(org_id)
        headers["X-Oto-Org-Role"] = (roles.ORG_ADMIN
                                     if roles.effective_org_role(ctx.sub, org_id) == roles.ORG_ADMIN
                                     else roles.ORG_MEMBER)
        if inp.op in ("daily", "agents"):
            params["days"] = inp.days or 30
    url = base + _CHEMINS[inp.op].format(org_id=org_id)
    try:
        r = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        logger.warning("commerce relay: op=%s org=%s unreachable: %s", inp.op, org_id,
                       type(e).__name__)
        raise AuthzDenied(502, "commerce_unreachable",
                          "The billing service did not answer; try again shortly.")
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code == 403 and isinstance(body, dict) and body.get("error") == "admin_required":
        raise AuthzDenied(403, "admin_required",
                          f"op={inp.op} is reserved to the org's admins.")
    if r.status_code >= 400 or not isinstance(body, dict):
        code = body.get("error") if isinstance(body, dict) else None
        logger.warning("commerce relay: op=%s org=%s answered %s (%s)", inp.op, org_id,
                       r.status_code, code)
        raise AuthzDenied(502, "commerce_error",
                          f"The billing service answered {r.status_code}"
                          + (f" ({code})" if code else "") + ".")
    return {"op": inp.op, "org_id": org_id, "result": body}


_MEMBRE = ORG_MEMBER_OF("org_id")

CAPABILITIES += [
    Capability(
        key="org.commerce.credits", handler=_relayer, Input=CreditsInput,
        Output=CreditsOutput,
        authz=BY_OP({"usage": _MEMBRE, "daily": _MEMBRE, "agents": _MEMBRE,
                     "spend_limits": _MEMBRE, "pricing": SUB_ONLY}),
        description=(
            "Credits of an org, read live from the billing service — read-only. "
            "op=usage (`org_id`): the current period — used vs allowance, credit_state, "
            "the breakdown by connector/tool, the connectors paused right now and why "
            "(`paused_reason` credits|payment|trial|seats|dispute) / daily (`org_id`, "
            "`days` 7|30|90): credits per day and tool / agents (`org_id`, `days`): "
            "credits per hosted agent — daily and agents are the org admins' read / "
            "spend_limits (`org_id`): the daily and monthly spend limits with today's "
            "spend against each; an admin sees every limit, a member only their own / "
            "pricing: what each tool costs in credits, and the plans. Check `usage` "
            "before launching a large campaign. To buy credits or change a limit, send "
            "the person to the app's billing pages: nothing here writes."),
        mcp="oto_credits",
        gate=is_enabled,
    ),
]
