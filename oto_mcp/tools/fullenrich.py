"""FullEnrich — waterfall multi-provider contact enrichment (phones + emails).

~70% phone hit rate. Async bulk API (POST → poll). Pay-per-result.

⚠️ Surface MCP async assumée (signal #252) : l'ex-tool synchrone pollait
in-process 131-147s → tout client MCP raccroche (~60s), résultat perdu ET crédits
consommés. Désormais : `fullenrich_enrich_linkedin` SOUMET le job (~1s, bulk
jusqu'à 100 contacts) et `fullenrich_result` relève le statut/le résultat —
le polling appartient à l'agent.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify

_CREDITS_URL = "https://app.fullenrich.com/api/v1/account/credits"


def _verify(fields: dict, config: dict | None = None) -> dict:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth+quota`.

    `GET /api/v1/account/credits` (⚠️ v1, PAS le v2 du reste du client — deux
    préfixes de version distincts chez FullEnrich, vérifié). Bearer token,
    lecture de solde sans effet de bord. Aucune mention explicite de
    « gratuit » dans ce qu'on a trouvé — absence de mention, indice, pas une
    preuve, comme Folk et Pennylane.

    Le solde (`balance`) distingue une clé morte d'un compte à sec — recharger
    n'est pas reconnecter.
    """
    import requests
    from oto.tools.fullenrich.client import FullenrichClient

    headers = FullenrichClient(api_key=fields["key"])._headers()
    r = requests.get(_CREDITS_URL, headers=headers, timeout=15)
    r.raise_for_status()
    infos = r.json() or {}
    restant = infos.get("balance")
    if not isinstance(restant, int):
        raise RuntimeError(
            "FullEnrich a répondu sans solde de crédits lisible : "
            f"{str(infos)[:200]}")
    if restant <= 0:
        raise connector_verify.QuotaEpuise(
            "La clé FullEnrich est bonne, mais le compte est à sec (0 crédit "
            "restant). Recharge le compte chez FullEnrich — reconnecter n'y "
            "changerait rien.")
    return {"quota": {"restant": restant, "unite": "crédits"}}


def register(mcp: FastMCP) -> None:
    from oto.tools.fullenrich.client import FullenrichClient

    connector_verify.register("fullenrich", _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client() -> tuple[FullenrichClient, bool]:
        key, is_platform = access.resolve_api_key("fullenrich")
        return FullenrichClient(api_key=key), is_platform

    @mcp.tool()
    def fullenrich_enrich_linkedin(
        contacts: list[dict],
        enrich_fields: Optional[list[str]] = None,
    ) -> dict:
        """Submit an ASYNC enrichment job (phones + emails) via FullEnrich (waterfall 20+ providers).

        Returns immediately with an `enrichment_id` — the job runs server-side for
        ~30s to 4min. THEN call `fullenrich_result(enrichment_id)` to collect (first
        poll after ~30s, then every ~20-30s until status FINISHED).

        Args:
            contacts: 1-100 contacts in ONE job (batch friends — one job for a whole
                list beats parallel single calls). Each: {"first_name": str,
                "last_name": str, "linkedin_slug": str (e.g. "alexis-laporte",
                NOT a URL — best matching), "domain": str (company website
                domain), "company_name": str (optional)}. Each contact MUST
                carry linkedin_slug OR domain (FullEnrich rejects the job
                otherwise).
            enrich_fields: subset of ["contact.work_emails", "contact.phones",
                "contact.personal_emails"]. Default: work_emails + phones.
                Only ask what you need — pricing is pay-per-result:
                10 credits/phone, 1/work_email, 3/personal_email.
        """
        client, is_platform = _client()
        try:
            enrichment_id = client.submit(contacts, enrich_fields=enrich_fields)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        if is_platform:
            # Un job = un contact facturé par contact : la consommation est le
            # NOMBRE de contacts, comptée en un seul geste (l'ancienne boucle faisait
            # une requête par contact — jusqu'à 100 par job).
            access.record_platform_usage("fullenrich", len(contacts))
        return {
            "enrichment_id": enrichment_id,
            "submitted": len(contacts),
            "next_step": ("Job accepted. Call fullenrich_result(enrichment_id) "
                          "in ~30s (typical completion 30s-4min)."),
        }

    @mcp.tool()
    def fullenrich_result(enrichment_id: str) -> dict:
        """Collect the result of a FullEnrich job submitted with fullenrich_enrich_linkedin.

        Single status check, returns immediately. If `done` is false, wait ~20-30s
        and call again (jobs typically finish in 30s-4min). When done, `profiles`
        holds one entry per submitted contact: {found, linkedin_slug, full_name,
        title, company_name, phones[], work_emails[], personal_emails[], location}.
        """
        client, _ = _client()
        res = client.fetch(enrichment_id)
        if res["status"] != "FINISHED":
            return {
                "done": False,
                "status": res["status"],
                "next_step": "Still running — call fullenrich_result again in ~20-30s.",
            }
        return {
            "done": True,
            "status": "FINISHED",
            "profiles": [p.to_dict() for p in res["profiles"]],
        }
