"""Capacités d'écriture des secrets d'org (ADR 0009, barreau 2b).

Même réconciliation que les membres : MCP platform-admin-only vs REST org_admin
self-service → unifié sur **`ORG_ADMIN_OF`**. Multi-binding (self + admin). La
validation provider/base_url passe par **`providers.org_secret_meta`** (source
unique — le REST l'utilisait déjà ; le MCP avait une validation à la main,
supprimée). Les secrets ne sont jamais renvoyés en clair.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import providers, credentials_store, org_store
from ...auth import hubspot as hubspot_oauth
from .._authz import ORG_ADMIN_OF
from .._refus_de_pose import REFUS_DE_VERIFICATION, refus_de_pose
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)

from ..registry import CAPABILITIES

_ID = {"id": "org_id"}


class OrgSecretSet(BaseModel):
    """Credential partagé d'org posé / roté. La réponse n'écho **rien** de ce qui a
    été écrit : le coffre ne restitue aucun secret, même à l'auteur du POST.

    ⚠️ C'est un **REMPLACEMENT**, jamais un merge : pour un connecteur multi-champs
    (zoho, silae…), `fields` doit porter TOUS les champs requis — en envoyer un
    sous-ensemble est refusé (`missing_credentials`), pas complété par l'existant.

    ⚠️ `ok: true` dit « écrit et chiffré », **pas** « ce credential fonctionne » : rien
    n'est appelé chez le fournisseur ici. Tester = `POST /api/me/connectors/{provider}/verify`.
    Seule exception : une clé `anthropic` est essayée chez Anthropic AVANT l'écriture, et
    refusée si elle n'y passe pas (400), ou si Anthropic ne répond pas (503)."""
    ok: bool
    org_id: int
    provider: str


class OrgSecretDeleted(BaseModel):
    """Retrait du credential partagé d'org. ⚠️ **`deleted: false` n'est pas une
    erreur** : avec `ok: true` en HTTP 200, il dit qu'il n'y avait rien à retirer.
    L'opération est idempotente et ne rend jamais 404 — contrairement à la pose, qui
    valide le provider et peut refuser en 400.

    Retirer le secret d'org ne coupe pas forcément le connecteur pour les membres :
    la cascade (clé membre > secret d'équipe > secret d'org > grant plateforme)
    retombe sur le grant plateforme s'il existe, et un membre qui avait sa propre
    clé n'était de toute façon jamais servi par celui-ci."""
    ok: bool
    org_id: int
    provider: str
    deleted: bool


class SetSecretInput(BaseModel):
    org_id: int
    provider: str
    api_key: str = ""                  # connecteurs mono-champ (clé simple)
    fields: Optional[dict[str, str]] = None   # connecteurs multi-champs (zoho/silae…)
    base_url: Optional[str] = None     # connecteurs remote uniquement (endpoint du bridge)
    # Multi-compte (Phase 2, 2026-08-25) : nom du compte à ce palier ("" = mono legacy).
    account: str = ""


class DeleteSecretInput(BaseModel):
    org_id: int
    provider: str
    account: str = ""


def _set_secret(ctx: ResolvedCtx, inp: SetSecretInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    base_url = (inp.base_url or "").strip() or None
    meta, code = providers.org_secret_meta(inp.provider, base_url)
    if code:
        raise AuthzDenied(400, code, f"Provider/base_url invalide : {code}.")
    account = (inp.account or "").strip()
    # Écriture PARTIELLE (#448) : mêmes règles qu'au palier équipe — les champs
    # absents sont complétés par le coffre, côté serveur ; un champ vide efface.
    # Mono-champ (api_key) ou multi-champs (fields packés), et les champs rangés dans
    # `meta` (`in_meta`) — source unique, `credentials_store.preparer_pose`.
    try:
        secret, meta_champs = credentials_store.preparer_pose(
            "org", str(inp.org_id), inp.provider, account, inp.api_key, inp.fields)
    except ValueError as e:
        # Saisie refusée (400) ou non vérifiable chez le fournisseur (503).
        raise refus_de_pose(e)
    # Même garde de pose qu'au palier membre (source unique, #409) : coexistence des
    # comptes si le connecteur est multi, refus nommé du compte nommé s'il est mono.
    try:
        credentials_store.guard_account_write("org", str(inp.org_id), inp.provider,
                                              account, org=inp.org_id)
    except credentials_store.NamedAccountRequired as e:
        raise AuthzDenied(409, "account_required", str(e))
    except credentials_store.SingleAccountConnector as e:
        raise AuthzDenied(400, "single_account_connector", str(e))
    org_store.set_org_secret(inp.org_id, inp.provider, secret, set_by=ctx.sub,
                             meta={**(meta or {}), **meta_champs} or None, account=account)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider}


def _delete_secret(ctx: ResolvedCtx, inp: DeleteSecretInput) -> dict:
    account = (inp.account or "").strip()
    if inp.provider == hubspot_oauth.CONNECTOR:
        hubspot_oauth.uninstall_before_clear("org", str(inp.org_id), account)
    deleted = org_store.delete_org_secret(inp.org_id, inp.provider, account=account)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider, "deleted": deleted}


CAPABILITIES += [
    Capability(
        # MCP retiré (2026-06-25) : pose de secret brut = dashboard-only. REST conservé.
        key="org.secret.set", handler=_set_secret, Input=SetSecretInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgSecretSet,
        description=("Set/rotate an org's shared account credential for a provider "
                     "(org-shareable only). Single-key connectors: pass `api_key`, or `fields` "
                     "with the key under its declared field name. "
                     "Multi-field connectors (zoho/silae…): pass `fields` "
                     "(all declared credential fields). base_url for remote bridges."),
        errors=(DeclaredError(400, "single_account_connector",
                              "un `account` nommé sur un connecteur qui n'en gère "
                              "qu'un — la clé écraserait l'unique"),
                DeclaredError(409, "account_required",
                              "connecteur multi-compte sans `account` : il faut "
                              "nommer le compte, sans quoi la pose est ambiguë"),
                DeclaredError(404, "unknown_org", "org inconnue")) + REFUS_DE_VERIFICATION,
        rest=(RestBinding("PUT", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("PUT", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
    Capability(
        key="org.secret.delete", handler=_delete_secret, Input=DeleteSecretInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgSecretDeleted,
        description="Remove an org's shared secret for a provider.",
        rest=(RestBinding("DELETE", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("DELETE", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
]
