"""GoCardless — prélèvements SEPA (lecture seule).

Clé résolue par appel via `access.resolve_api_key("gocardless")` : modèle
clé-per-user (comme Pennylane/Attio), pas de clé plateforme. Chaque
utilisateur pose sa propre clé GoCardless — ses prélèvements ne sont
visibles que par lui.

Surface strictement en lecture : GoCardless est une source (réconciliation,
traitement des échecs). Aucune mutation exposée — un agent ne peut pas
annuler un prélèvement.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.gocardless import GoCardlessClient

    def _client() -> GoCardlessClient:
        # Backstop d'autorisation au call-time (gocardless = namespace grant-only,
        # financier sensible). SANS ça, l'autz reposait uniquement sur le masquage
        # de visibilité — qu'un org_admin contourne en posant un org_secret
        # gocardless (autorisé sans entitlement) → tout org_member pouvait appeler.
        # Miroir de mm (remote.py) / memento (mount.py). require_namespace lit
        # granted_namespaces_for (grants user ∪ entitlements org) + bypass admin.
        access.require_namespace("gocardless")
        key, _is_platform = access.resolve_api_key("gocardless")
        return GoCardlessClient(api_key=key)

    @mcp.tool()
    def gocardless_creditors() -> list:
        """Comptes marchands GoCardless (compte encaisseur)."""
        return _client().list_creditors()

    @mcp.tool()
    def gocardless_payments(
        status: Optional[str] = None,
        limit: int = 50,
        mandate: Optional[str] = None,
        customer: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list:
        """Liste de prélèvements (1 page).

        Args:
            status: failed, confirmed, paid_out, submitted, cancelled, charged_back…
            limit: taille de page (max 500).
            mandate: filtrer par mandat (MD…).
            customer: filtrer par customer (CU…).
            since: ISO8601, prélèvements créés après cette date.
        """
        return _client().list_payments(
            status=status, limit=limit, mandate=mandate,
            customer=customer, created_gt=since,
        )

    @mcp.tool()
    def gocardless_payment(payment_id: str) -> dict:
        """Détail brut d'un prélèvement (PM…)."""
        return _client().get_payment(payment_id)

    @mcp.tool()
    def gocardless_events(
        payment: Optional[str] = None,
        mandate: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        """Timeline d'events. Motif d'échec : action='failed' sur un payment."""
        return _client().list_events(
            payment=payment, mandate=mandate, action=action, limit=limit,
        )

    @mcp.tool()
    def gocardless_payment_party(payment_id: str) -> dict:
        """Résout payment → mandat → customer (email, société, metadata aplatis).

        ⚠️ La metadata GoCardless peut ne pas porter d'identifiant client
        externe (selon le marchand).
        """
        return _client().payment_party(payment_id)

    @mcp.tool()
    def gocardless_failure_reason(payment_id: str) -> dict:
        """Motif du dernier échec d'un prélèvement (cause, description,
        will_attempt_retry). Si will_attempt_retry est True, GoCardless va
        retenter — ne pas émettre d'avoir tant que ce n'est pas False."""
        return _client().failure_reason(payment_id)

    @mcp.tool()
    def gocardless_failed(
        since: Optional[str] = None,
        limit: int = 200,
    ) -> list:
        """Prélèvements refusés enrichis, en un seul appel.

        Renvoie une ligne par échec avec client (nom/email), montant,
        charge_date, failed_at, cause/reason_code, will_attempt_retry et
        état du mandat — la chaîne payment→mandat→customer + le motif sont
        résolus côté serveur. Triés par date d'échec décroissante.

        ⚠️ Faits seulement, pas d'action décidée : « relancer vs refaire un
        mandat » reste un jugement métier (agent/doctrine). Et tant que
        will_attempt_retry est True, ne rien émettre — GoCardless va retenter.

        Args:
            since: ISO8601 (ex '2026-05-25'). Filtre sur created_at : un
                paiement créé avant mais échoué après ne ressort pas.
            limit: taille de page des failed à enrichir (max 500).
        """
        return _client().failed_payments(since=since, limit=limit)
