"""Secrets partagés d'un groupe (ADR 0012).

Autz = `GROUP_ADMIN_OF`. Les secrets de groupe utilisent la MÊME validation
provider/base_url que les secrets d'org (`providers.org_secret_meta`, source
unique) et le même coffre chiffré (entity_type='group').
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import providers, credentials_store, group_store
from ...auth import hubspot as hubspot_oauth
from .._authz import GROUP_ADMIN_OF
from .._refus_de_pose import REFUS_DE_VERIFICATION, refus_de_pose
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)
from ..registry import CAPABILITIES

_GID = {"id": "group_id"}


class SetGroupSecretInput(BaseModel):
    group_id: int
    provider: str
    api_key: str = ""                  # connecteurs mono-champ (clé simple)
    fields: Optional[dict[str, str]] = None   # connecteurs multi-champs (zoho/silae…)
    base_url: Optional[str] = None
    # Multi-compte (Phase 2, 2026-08-25) : nom du compte à ce palier ("" = mono legacy).
    account: str = ""


class DeleteGroupSecretInput(BaseModel):
    group_id: int
    provider: str
    account: str = ""


# --- Sorties ----------------------------------------------------------------

class GroupSecretSet(BaseModel):
    """Credential partagé d'équipe posé / roté. La réponse n'écho **rien** de ce qui a
    été écrit : le coffre ne restitue aucun secret, même à l'auteur du PUT.

    ⚠️ **C'est l'exception à « une équipe ne peut que rétrécir ce que l'org expose ».**
    Le secret d'équipe passe AVANT celui de l'org dans la cascade
    (`clé membre > équipe active > org > grant plateforme`) : poser un credential ici
    ne restreint pas l'org, il **réoriente** silencieusement les appels de l'équipe vers
    un autre compte chez le fournisseur. Les quotas, les journaux et la facturation
    côté tiers suivent — sans qu'aucun membre voie la bascule.

    ⚠️ **Il ne sert que les membres dont c'est l'équipe ACTIVE.** La cascade lit
    `current_group`, pas l'appartenance : un membre de l'équipe qui travaille sous une
    autre équipe (ou au niveau org) ne verra jamais ce credential. « Posé » ≠ « en
    vigueur pour l'équipe » — c'est ce qui rend un test de bout en bout trompeur si on
    n'a pas d'abord basculé son équipe active.

    ⚠️ Une clé ABSENTE du corps est **complétée par l'existant** (merge côté serveur,
    #448) ; une clé présente mais VIDE est un effacement explicite. Envoyer tous les
    champs reste donc un remplacement complet — ce que fait le formulaire du
    dashboard. Ce qui devient possible est « je change l'URL, je ne touche pas à la
    clé », sans avoir à retrouver un secret que rien ne restitue.
    ⚠️ Ceci a dit « REMPLACEMENT, jamais un merge — un sous-ensemble est refusé (400) »
    jusqu'au 2026-08-27 : c'était vrai, et c'était le piège. Combiné à l'absence de
    lecture révélante à ce palier, modifier un champ non secret obligeait à réécrire
    un secret irrécupérable.

    ⚠️ `ok: true` dit « écrit et chiffré », **pas** « ce credential fonctionne » : rien
    n'est appelé chez le fournisseur ici. Tester = `POST /api/me/connectors/{provider}/verify`.

    L'éligibilité est celle de l'org (mêmes providers org-partageables) : une équipe ne
    peut pas introduire un connecteur que son org ne pourrait pas partager."""
    ok: bool
    group_id: int
    provider: str


class GroupSecretDeleted(BaseModel):
    """Retrait du credential partagé d'équipe. ⚠️ **`deleted: false` n'est pas une
    erreur** : avec `ok: true` en HTTP 200, il dit qu'il n'y avait rien à retirer.
    L'opération est idempotente et ne rend jamais 404 — contrairement à la pose, qui
    valide le provider et peut refuser en 400.

    ⚠️ Retirer ce secret **ne coupe pas le connecteur** pour l'équipe : la cascade
    retombe sur le secret d'org puis sur le grant plateforme. Les appels continuent, sous
    une autre identité côté fournisseur, sans erreur ni avertissement. Si l'intention
    était de couper l'accès, c'est la gouvernance de connecteur qu'il faut toucher, pas
    le credential."""
    ok: bool
    group_id: int
    provider: str
    deleted: bool


def _set_secret(ctx: ResolvedCtx, inp: SetGroupSecretInput) -> dict:
    base_url = (inp.base_url or "").strip() or None
    meta, code = providers.org_secret_meta(inp.provider, base_url)
    if code:
        raise AuthzDenied(400, code, f"Provider/base_url invalide : {code}.")
    account = (inp.account or "").strip()
    # Écriture PARTIELLE (#448) : les champs absents du corps sont complétés par le
    # coffre, côté serveur — le secret ne repasse jamais par le client. C'est ce qui
    # permet à un admin d'équipe de repointer une `base_url` sans détenir le bearer.
    # Mono-champ (api_key) ou multi-champs (fields packés), et les champs rangés dans
    # `meta` (`in_meta`) — source unique, `credentials_store.preparer_pose`.
    try:
        secret, meta_champs = credentials_store.preparer_pose(
            "group", str(inp.group_id), inp.provider, account, inp.api_key, inp.fields)
    except ValueError as e:
        # Saisie refusée (400) ou non vérifiable chez le fournisseur (503).
        raise refus_de_pose(e)
    # Même garde de pose qu'au palier membre (source unique, #409) : coexistence des
    # comptes si le connecteur est multi, refus nommé du compte nommé s'il est mono.
    try:
        credentials_store.guard_account_write("group", str(inp.group_id), inp.provider,
                                              account, org=ctx.org_id)
    except credentials_store.NamedAccountRequired as e:
        raise AuthzDenied(409, "account_required", str(e))
    except credentials_store.SingleAccountConnector as e:
        raise AuthzDenied(400, "single_account_connector", str(e))
    group_store.set_group_secret(inp.group_id, inp.provider, secret, set_by=ctx.sub,
                                 meta={**(meta or {}), **meta_champs} or None, account=account)
    return {"ok": True, "group_id": inp.group_id, "provider": inp.provider}


def _delete_secret(ctx: ResolvedCtx, inp: DeleteGroupSecretInput) -> dict:
    account = (inp.account or "").strip()
    if inp.provider == hubspot_oauth.CONNECTOR:
        hubspot_oauth.uninstall_before_clear("group", str(inp.group_id), account)
    deleted = group_store.delete_group_secret(inp.group_id, inp.provider, account=account)
    return {"ok": True, "group_id": inp.group_id, "provider": inp.provider, "deleted": deleted}


CAPABILITIES += [
    Capability(
        key="group.secret.set", handler=_set_secret, Input=SetGroupSecretInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupSecretSet,
        description=("Set/rotate a group's shared account credential for a provider "
                     "(org-shareable only). Single-key connectors: pass `api_key`, or `fields` "
                     "with the key under its declared field name. "
                     "Multi-field connectors (zoho/silae…): pass `fields` (all declared "
                     "credential fields). Resolves BEFORE the org secret for members."),
        errors=(DeclaredError(400, "single_account_connector",
                              "un `account` nommé sur un connecteur qui n'en gère "
                              "qu'un — la clé écraserait l'unique"),
                DeclaredError(409, "account_required",
                              "connecteur multi-compte sans `account` : il faut "
                              "nommer le compte, sans quoi la pose est ambiguë"),
                ) + REFUS_DE_VERIFICATION,
        rest=RestBinding("PUT", "/api/groups/{id}/secrets/{provider}", _GID),
    ),
    Capability(
        key="group.secret.delete", handler=_delete_secret, Input=DeleteGroupSecretInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupSecretDeleted,
        description="Remove a group's shared secret for a provider.",
        rest=RestBinding("DELETE", "/api/groups/{id}/secrets/{provider}", _GID),
    ),
]
