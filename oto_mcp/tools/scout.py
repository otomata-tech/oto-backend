"""Tools MCP `scout_*` — harnais prospection (ADR 0008).

Surface agent du harnais : piloter la file de prospection depuis Claude. Wrappers
fins sur `factgraph.prospection` (la même couche service que l'adaptateur REST
`api_routes_scout`), scopés à l'**org active** du token.

Vocabulaire : un *prospect* = un fact `entreprise` du graphe, avec ses contacts
et son historique d'actions ; le statut est dérivé de la dernière action.
"""
from __future__ import annotations

import logging

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, org_store
from ..factgraph import prospection
from ..factgraph.schemas import SchemaError

logger = logging.getLogger(__name__)


def _err(message: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _require_org() -> tuple[str, int]:
    sub = access.current_user_sub_from_token()
    if not sub:
        raise _err("Auth requise — transport HTTP authentifié uniquement.")
    org_id = org_store.get_active_org(sub)
    if org_id is None:
        raise _err("Aucune org active. Choisis-en une avec `oto_use_org`.")
    return sub, org_id


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def scout_queue(ctx: Context, limit: int = 50) -> dict:
        """Prospects à appeler en priorité (file Blitz Day) de l'org active.

        Renvoie les `qualified` non claimés, triés heat (hot>warm>cold) puis fit.
        Pour t'attribuer le prochain et l'ouvrir, utilise `scout_claim_next`.
        """
        _, org_id = _require_org()
        items = prospection.queue(org_id, min(limit, 500))
        return {"items": items, "count": len(items)}

    @mcp.tool()
    async def scout_claim_next(ctx: Context) -> dict:
        """T'attribue atomiquement le prochain prospect libre le mieux scoré.

        Anti-collision : deux opérateurs concurrents obtiennent deux prospects
        différents. Le claim expire après 20 min d'inactivité. Renvoie le
        prospect claimé (ou `null` si la file est vide).
        """
        sub, org_id = _require_org()
        return {"prospect": prospection.claim_next(org_id, who=sub)}

    @mcp.tool()
    async def scout_prospect(ctx: Context, prospect_id: int) -> dict:
        """Fiche complète d'un prospect : entreprise + contacts + historique d'actions + statut/score."""
        _, org_id = _require_org()
        try:
            return prospection.get_detail(org_id, prospect_id)
        except (KeyError, ValueError):
            raise _err(f"Prospect {prospect_id} introuvable.")

    @mcp.tool()
    async def scout_add_prospect(ctx: Context, siren: str, nom: str,
                                 bp_an: int | None = None, idcc: str | None = None) -> dict:
        """Ajoute un prospect (entreprise) dans la file de l'org active. Renvoie sa fiche."""
        sub, org_id = _require_org()
        try:
            fid = prospection.add_prospect(org_id, siren=siren, nom=nom,
                                           bp_an=bp_an, idcc=idcc, created_by=sub)
        except SchemaError as e:
            raise _err(str(e))
        return prospection.get_detail(fid)

    @mcp.tool()
    async def scout_add_contact(ctx: Context, prospect_id: int, nom: str,
                                tel: str | None = None, linkedin: str | None = None) -> dict:
        """Ajoute un contact (personne) à un prospect. Renvoie la fiche mise à jour."""
        sub, org_id = _require_org()
        try:
            prospection.add_contact(org_id, prospect_id, nom=nom, tel=tel, linkedin=linkedin, created_by=sub)
        except KeyError:
            raise _err(f"Prospect {prospect_id} introuvable.")
        except (SchemaError, ValueError) as e:
            raise _err(str(e))
        return prospection.get_detail(org_id, prospect_id)

    @mcp.tool()
    async def scout_record_action(ctx: Context, prospect_id: int, canal: str,
                                  outcome: str, note: str | None = None) -> dict:
        """Enregistre une action commerciale sur un prospect (fait avancer son statut).

        `canal` : 'appel' | 'email'. `outcome` : ex 'rdv', 'talked', 'sent', 'dead',
        'called'. Le statut du prospect est dérivé de la dernière action. Renvoie la fiche.
        """
        sub, org_id = _require_org()
        try:
            return prospection.record_action(org_id, prospect_id, canal=canal, outcome=outcome,
                                             note=note, created_by=sub)
        except KeyError:
            raise _err(f"Prospect {prospect_id} introuvable.")
        except (SchemaError, ValueError) as e:
            raise _err(str(e))
