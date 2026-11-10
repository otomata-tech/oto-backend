"""Les CLÉS D'API D'ORG : des jetons `oto_` qui appartiennent à l'org, pas à un membre
(oto-backend#1188, décisions d'Alexis du 08/10/2026 — `docs/cles-d-org.md`).

- `GET|POST /api/orgs/{id}/api-keys` + `DELETE …/{token_id}`

Une clé d'org agit au nom du COMPTE DE SERVICE de l'org (`org_store.members`) : membre
simple de son org et de nulle autre, il voit ce que l'org possède et ce qu'on lui
partage, jamais les objets personnels des membres, et il survit à tous les départs.

- **Seuls les admins de l'org** créent, listent et révoquent ses clés ; la création
  refuse une org archivée (`ORG_ADMIN_OF_LIVE`).
- **Toujours à portée** : tableaux et projets, ou un plafond d'émission (`issue`) — ni
  jeton non porté (`400 scopes_required`), ni runner. La portée se range dans l'org de
  la clé, quelle que soit l'org active de l'admin.
- **Enfermée dans son org** : chaque clé porte le verrou d'org (`verrou_org.py`), et ce
  qu'émet une clé émettrice aussi (`api_tokens._my_create`).
- `allow_api_token=False` sur les trois routes : une clé ne gère pas les clés de son
  org. Une clé ÉMETTRICE gère SES enfants par `/api/me/tokens`, comme tout émetteur —
  pour elle, « moi » est le compte de l'org.
- L'archivage de l'org révoque ses clés (`org_store.archive_org`).

**Aucune face MCP**, pour la même raison que `api_tokens` : un secret émis ne passe pas
par un outil.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from .. import db, org_store, verrou_org
from ..auth import token_scopes
from ._authz import ORG_ADMIN_OF, ORG_ADMIN_OF_LIVE
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .api_tokens import (_INCLURE_REVOQUES, _LABEL_MAX, _MOTIF, ApiToken,
                         ApiTokenCreated, TokenDeleted, _echeance_d_emetteur,
                         _echeance_plafonnee, _entier,
                         _jours, _libelle, _motif, _orgs_du_plafond, _par_identifiant,
                         _portee)
from .registry import CAPABILITIES

_CHEMIN = "/api/orgs/{id}/api-keys"
_ID = {"id": "org_id"}


# --- Entrées ----------------------------------------------------------------

class OrgApiKeyListInput(BaseModel):
    org_id: int
    include_revoked: bool = _INCLURE_REVOQUES


class OrgApiKeyCreateInput(BaseModel):
    org_id: int
    label: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _LABEL_MAX},
        description=("Comment reconnaître cette clé plus tard — au plus 32 caractères, "
                     "au-delà : 400 `label_too_long`. Absent ⇒ 'cli'."))
    scopes: Any = Field(
        None, description=("REQUIS. Tableaux et projets (`{\"namespaces\": {\"<id>\": "
                           "\"read|write\"}, \"projects\": {\"<id>\": \"read\"}}`), ou "
                           "un plafond d'émission (`{\"issue\": {\"orgs\": {\"<cette "
                           "org>\": \"write\"}}}`)."))
    ttl_days: Union[int, str, None] = Field(
        None, description=("Jours avant échéance, au plus 90 (au-delà : 400 "
                           "`token_ttl_too_long`). Requis pour une clé émettrice. "
                           "Absent ailleurs ⇒ 90 jours."))


class OrgApiKeyDeleteInput(BaseModel):
    org_id: int
    token_id: str = ""
    reason: Optional[str] = _MOTIF


# --- Sorties ----------------------------------------------------------------

class OrgApiKey(ApiToken):
    """Une clé de l'org, ou un jeton émis par l'une de ses clés émettrices
    (`parent_id`). `created_by` = l'admin qui a émis la clé ; null pour un enfant."""
    created_by: Optional[str] = None


class OrgApiKeyList(BaseModel):
    tokens: list[OrgApiKey]


# --- Handlers ---------------------------------------------------------------

def _list(ctx: ResolvedCtx, inp: OrgApiKeyListInput) -> dict:
    sub = org_store.compte_de_service_de(ctx.org_id)
    return {"tokens": db.list_api_tokens(sub, include_revoked=inp.include_revoked,
                                         kinds=("org", "user")) if sub else []}


def _portee_de_cle(brut: Any) -> dict:
    scopes = _portee(brut)
    if not scopes:
        raise AuthzDenied(400, "scopes_required",
                          "Une clé d'org est toujours à portée : nomme ses tableaux et "
                          "projets, ou son plafond d'émission (`issue`).")
    if scopes.get(token_scopes.RUNNER):
        raise AuthzDenied(400, "invalid_scopes",
                          "Une clé d'org ne porte pas la portée `runner`.")
    return scopes


def _create(ctx: ResolvedCtx, inp: OrgApiKeyCreateInput) -> dict:
    label = _libelle(inp.label)
    ttl_days = _jours(inp.ttl_days)
    brut = _portee_de_cle(inp.scopes)
    sub = org_store.assurer_compte_de_service(ctx.org_id, by=ctx.sub)
    # La portée se range dans ce que voit le compte de l'org DANS son org — pas dans
    # l'org active de l'admin, qui peut en être une autre.
    jeton = verrou_org.poser(verrou_org.Verrou(sub=sub, org_id=ctx.org_id))
    try:
        scopes = _par_identifiant(sub, brut)
    finally:
        verrou_org.lever(jeton)
    _echeance_d_emetteur(scopes, ttl_days)
    _orgs_du_plafond(sub, scopes)
    ttl_days = _echeance_plafonnee(ttl_days)
    token = db.create_api_token(sub, label=label, ttl_days=ttl_days, scopes=scopes,
                                kind="org", verrou_org=True, verrou_org_id=ctx.org_id,
                                created_by=ctx.sub)
    return {"token": token, "label": label, "scopes": scopes, "ttl_days": ttl_days}


def _delete(ctx: ResolvedCtx, inp: OrgApiKeyDeleteInput) -> dict:
    sub = org_store.compte_de_service_de(ctx.org_id)
    if not sub or not db.revoke_api_token(sub, _entier(inp.token_id),
                                          revoked_by=ctx.sub, reason=_motif(inp.reason)):
        raise AuthzDenied(404, "unknown_token")
    return {"ok": True}


# --- Déclarations -----------------------------------------------------------

_D_LIST = ("[admin d'org] Les clés d'API de l'org, sans leur secret, et ce que ses clés "
           "émettrices ont émis (`parent_id`) ; les révoquées avec `include_revoked`. "
           "Une clé d'org appartient à l'org, pas à un membre : elle survit aux départs. "
           "Réservé à une session interactive.")
_D_CREATE = ("[admin d'org] Émet une clé d'API de l'org. ⚠️ Le secret n'est rendu "
             "qu'une fois. Elle agit au nom de l'org : elle voit ce que l'org possède et "
             "ce qu'on lui partage, jamais les objets personnels des membres, et rien "
             "hors de l'org. `scopes` est REQUIS (`400 scopes_required`) : tableaux et "
             "projets, ou plafond d'émission `issue` (`ttl_days` alors requis). Réservé "
             "à une session interactive.")
_D_DELETE = ("[admin d'org] Révoque une clé de l'org, ou un jeton qu'elle a émis : il "
             "cesse de fonctionner, la trace reste (qui, quand, `reason`). Révoquer une "
             "clé émettrice révoque ses enfants. Réservé à une session interactive.")

CAPABILITIES += [
    Capability(
        key="org.api_key.list", handler=_list, Input=OrgApiKeyListInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgApiKeyList, description=_D_LIST,
        mcp=None, rest=RestBinding("GET", _CHEMIN, _ID, allow_api_token=False),
    ),
    Capability(
        key="org.api_key.create", handler=_create, Input=OrgApiKeyCreateInput,
        authz=ORG_ADMIN_OF_LIVE("org_id"), Output=ApiTokenCreated,
        description=_D_CREATE, mcp=None,
        rest=RestBinding("POST", _CHEMIN, _ID, status=201, allow_api_token=False),
    ),
    Capability(
        key="org.api_key.delete", handler=_delete, Input=OrgApiKeyDeleteInput,
        authz=ORG_ADMIN_OF("org_id"), Output=TokenDeleted, description=_D_DELETE,
        mcp=None,
        rest=RestBinding("DELETE", _CHEMIN + "/{token_id}", _ID, allow_api_token=False,
                         reads_body=True),
    ),
]
