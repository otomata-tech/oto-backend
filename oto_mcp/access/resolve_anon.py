"""La résolution pour un endpoint MCP ANONYME (ADR 0032) — le miroir org-only.

Extrait de `resolve.py` le 2026-08-29 (cliquet des 500 lignes, #584) : c'est une voie
à part entière, avec son ADR, ses appelants (`subdomain_project`) et ses tests — pas
une branche du chemin identifié. Elle partage le walker et le type de retour, rien
d'autre.

⚠️ **L'étage tenant, ici, ne vient QUE d'une arête** (L-clés PR 2). Le tenant d'un
appelant se lit sur son sub qualifié, et l'anonyme n'en a pas ; lire le rattachement
de l'org du projet est précisément ce qu'un chemin de résolution ne fait pas (lot
L1). C'est le walker qui cherche l'arête vivante tenant→org (`grants_chain.
tenant_for_org`) ; sans elle, la cascade reste `org > plateforme`.
"""
from __future__ import annotations

from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, org_store, providers
from . import cascade, tenant_budget
from .resolved_credential import ResolvedCredential


def _resolve_credential_anon(provider: str, want: str, org_id: Optional[int]) -> ResolvedCredential:
    """Résolution pour un endpoint MCP ANONYME (ADR 0032) : aucun `sub`, aucune session
    per-user → cascade réduite `org_secret > grant plateforme d'org > clé plateforme
    ouverte`, scopée sur l'org PROPRIÉTAIRE du projet. Pas de user_key/group (inexistants
    sans identité), pas de quota per-sub (le rate-limit du sous-domaine borne l'abus).
    Miroir org-only des paliers de `_resolve_credential_impl` — ce qui n'est pas résoluble
    au niveau org (oauth/cookie per-user) lève une McpError actionnable, fail-closed."""
    con = providers.connector_for_provider(provider)
    if con is None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Provider inconnu: {provider}"))
    if org_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme n'a pas d'org propriétaire pour résoudre "
                     f"`{provider}` (projet sans org).")))
    # Walker avec sub=None : les barreaux membre/groupe/tenant se sautent d'eux-mêmes
    # → cascade réduite org > plateforme (ADR 0044 §F R3 : anon → instance 'open'
    # free-tier, ou 'closed' dont le share_down vise `org:<org_id>`).
    # ⚠️ Le barreau org sélectionne son COMPTE comme le chemin réel (`_org_fetch` :
    # unique/`is_default`), jamais `''` en dur — `ensure_named_coexistence` migre la
    # ligne mono vers « principal » au premier compte nommé, et l'endpoint anonyme
    # cessait alors de résoudre pendant que `has_org_secret` disait « configuré »
    # (review #399 F3). Pas de compte nommable ici : aucun sub, aucun axe d'appel.
    def _anon_org_fetch(oid: int, mprov: str):
        # Mono D'ABORD : la ligne `''` historique répond sans lire la table des
        # comptes (zéro coût ajouté pour les orgs pré-migration, et le contrat des
        # tests qui stubbent `get_org_secret` seul reste entier). La sélection
        # nommée n'est tentée QUE si la ligne mono manque — le cas F3, où
        # `ensure_named_coexistence` l'a migrée vers « principal ».
        key = org_store.get_org_secret(oid, mprov)
        if key or not cascade._is_multi_account(mprov, oid):
            return key
        eff = cascade._shared_auto_account("org", str(oid), mprov,
                                   "pour l'org de ce projet", scope="org")
        if not eff:
            return None
        key = org_store.get_org_secret(oid, mprov, eff)
        return (key, eff) if key else None

    # `legacy_user` n'est jamais atteint ici (le barreau se gate sur `sub is not
    # None`, et l'anonyme n'en a pas) — la sonde réelle est passée quand même,
    # champ requis de `CascadeProbe` (#409 : une sonde qui l'omettrait le
    # sauterait en silence si un jour ce barreau devenait atteignable ici).
    probe = cascade.CascadeProbe(member=cascade.FETCH_PROBE.member,
                         member_cross=cascade.FETCH_PROBE.member_cross,
                         legacy_user=cascade.FETCH_PROBE.legacy_user,
                         group=cascade.FETCH_PROBE.group, org=_anon_org_fetch,
                         tenant=cascade.FETCH_PROBE.tenant,
                         platform=cascade.FETCH_PROBE.platform)
    win = cascade.cascade_winner(None, provider, org=org_id, group=None,
                         probe=probe, want=want)
    if win is None:
        if want == "byo":
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Aucun credential `{provider}` configuré pour l'org de ce projet."))
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme ne peut pas résoudre `{provider}` : configure "
                     f"une clé d'org, ou grant une clé plateforme à l'org du projet.")))
    if win.mode == "org":
        return ResolvedCredential(provider, win.payload, False, "org", "org",
                                  str(org_id), account=win.account)
    if win.mode == "tenant":
        # Servi par une arête vivante (jamais autrement pour l'anonyme) : son budget
        # par org s'applique — l'org entière y puise, anonyme compris.
        tenant_budget.enforce(win.entity_id, providers.credential_provider(provider), org_id)
        return ResolvedCredential(provider, win.payload, False, "tenant",
                                  credentials_store.TENANT, win.entity_id,
                                  account=win.account)
    # Même règle qu'au palier plateforme du chemin identifié : le secret reste
    # dans le `CascadeRung` (repr expurgé), jamais dans un dict nu de cette frame.
    return ResolvedCredential(provider, win.payload["secret"], True, "platform",
                              credentials_store.PLATFORM, win.payload["label"])
