"""Palier PLATEFORME des connecteurs : le cran d'activation, et l'accès ouvert par
la plateforme à une org ou à un membre.

Cinq routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `GET|POST|DELETE /api/admin/connectors/activation`            → le cran d'activation (ADR 0010 B4)
- `GET|POST        /api/admin/connectors/{provider}/platform-access` → l'accès plateforme (ADR 0044 §H)

C'est l'étage manquant d'une famille dont les DEUX autres paliers étaient déjà des
capacités : `connectors.activation.{org_list,set_org,clear_org}` et
`{group_list,set_group,clear_group}`. Même métier, trois étages, une seule façon de
le décrire désormais.

⚠️ **Le gate d'activation est au CHARGEMENT** (`register_all`, au boot) : basculer le
master GLOBAL ne prend effet qu'au prochain redémarrage du serveur — d'où
`restart_required` dans la réponse. Un override d'ORG, lui, est lu à la résolution et
prend effet tout de suite. Les deux passent par le même corps, la différence ne tient
qu'à la présence d'`org_id` : c'est le genre de piège qu'un `Output` déclaré rend
visible sans avoir à lire le code.

**Pas de face MCP** (`mcp=None`) : basculer le master global est un acte de
DÉPLOIEMENT (il demande un redémarrage), et ouvrir l'accès plateforme est un acte
commercial. Un agent n'a rien à en faire, et les paliers qu'un utilisateur peut
réellement piloter — org et équipe — sont déjà servis par `oto_connector_activation`.

`/api/admin/*` est retiré du descriptif OpenAPI public : une console de plateforme n'a
pas d'intégrateur tiers. L'`Output` sert ici la génération de types côté dashboard et
la lisibilité du contrat, pas la publication.
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, StrictBool, StrictInt

from .. import access, connector_activation, db, org_store, providers
from ._authz import PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ACTIVATION = "/api/admin/connectors/activation"
_ACCESS = "/api/admin/connectors/{provider}/platform-access"


# --- Entrées ----------------------------------------------------------------

class ActivationListInput(BaseModel):
    """Aucun paramètre : l'admin voit TOUT le registre, y compris ce qui est OFF —
    c'est sa surface pour l'activer."""


class ActivationSetInput(BaseModel):
    connector: str = ""
    # ⚠️ `Strict*` + défaut `None` : la route rend `400 enabled_must_be_bool` quand le
    # champ MANQUE, pas le `400 invalid_input` de pydantic. Le défaut `None` fait donc
    # tomber le cas manquant — le seul qui se produise — dans le handler, avec son code
    # historique. Un champ présent mais MAL TYPÉ reste refusé par pydantic (même 400,
    # code `invalid_input`) : personne n'en envoie, et le schéma reste juste.
    enabled: Optional[StrictBool] = None
    # Absent ⇒ master GLOBAL. Présent ⇒ override de CETTE org.
    org_id: Optional[StrictInt] = None


class ActivationClearInput(BaseModel):
    # Query string : les deux arrivent en TEXTE. On les garde tels quels et on convertit
    # dans le handler, pour rendre `connector_and_org_id_required` et `org_id_must_be_int`
    # — les deux codes servis — plutôt qu'un `invalid_input` générique.
    connector: Optional[str] = None
    org_id: Optional[str] = None


class PlatformAccessInput(BaseModel):
    provider: str


class PlatformAccessSetInput(BaseModel):
    provider: str
    scope: Optional[str] = None            # 'org' | 'user'
    # Un id d'org arrive en nombre, un sub en texte : les deux sont acceptés et
    # normalisés en texte, comme le faisait `str(body.get("id", ""))`.
    id: Union[int, str, None] = None
    on: bool = False


# --- Sorties ----------------------------------------------------------------

class ActivationOverride(BaseModel):
    org_id: int
    enabled: bool


class ActivationRow(BaseModel):
    """⚠️ **`enabled: null` n'est pas « inconnu », c'est OFF.** Le master n'a jamais été
    posé, et la règle est deny-by-default : le connecteur n'est pas exposé. Un front qui
    traite `null` comme « activé » ou comme un état indéterminé se trompe."""
    connector: str
    label: Optional[str] = None
    help: Optional[str] = None
    namespaces: list[str]
    enabled: Optional[bool] = None
    # Les orgs qui dérogent au master global, dans un sens comme dans l'autre.
    overrides: list[ActivationOverride]
    # Option payante qui conditionne ce connecteur (couche 3, ADR 0044 §H), ou `null`.
    paid_option: Optional[str] = None


class ActivationListView(BaseModel):
    connectors: list[ActivationRow]


class ActivationSetView(BaseModel):
    """⚠️ `restart_required: true` (master global) veut dire que la bascule est ÉCRITE
    mais pas encore SERVIE : le chargement des tools est résolu au boot. Un override
    d'org, lui, prend effet immédiatement."""
    ok: bool
    connector: str
    enabled: bool
    org_id: Optional[int] = None
    restart_required: bool


class ActivationClearView(BaseModel):
    """L'override retiré : le connecteur retombe sur le master global pour cette org."""
    ok: bool
    connector: str
    org_id: int


class Beneficiary(BaseModel):
    """Une org ou un membre à qui la plateforme ouvre ce connecteur. `has_key` = grant
    sur la clé plateforme (couche 2) ; `has_option` = option offerte (couche 3). Les
    deux sont indépendants : l'un sans l'autre est un état normal, pas une incohérence."""
    scope: str                         # 'org' | 'user'
    id: str
    has_key: bool
    has_option: bool
    label: Optional[str] = None
    logo_url: Optional[str] = None     # orgs
    email: Optional[str] = None        # membres


class PlatformAccessView(BaseModel):
    """Vue connecteur-centrique de l'accès plateforme (ADR 0044 §H) — elle remplace les
    leviers dispersés `/platform/orgs` et `/platform/users`. **Aucun secret n'en sort.**

    ⚠️ `open_tier: true` change la lecture de `beneficiaries` : une instance plateforme
    en partage `open` ouvre le connecteur à TOUS sans grant nominatif, donc la liste
    n'est plus la population servie — elle ne dit plus que les grants explicites."""
    connector: str
    paid_option: Optional[str] = None   # None = pas d'option payante (couche 3)
    platform_key: bool                  # une clé plateforme existe (couche 2)
    open_tier: bool                     # free-tier : ouvert à tous sans grant
    beneficiaries: list[Beneficiary]


class PlatformAccessSetView(BaseModel):
    """L'acte UNIQUE « accès plateforme » : il pose ENSEMBLE ce que le backend couplait
    déjà — l'option comp (si le connecteur en a une) ET le grant de clé plateforme (s'il
    en existe une). `paid_option`/`platform_key` disent ce qui a effectivement été touché."""
    ok: bool
    connector: str
    scope: str
    id: str
    on: bool
    paid_option: Optional[str] = None
    platform_key: bool


# --- Handlers ---------------------------------------------------------------

def _known(connector: str, *, code: str, status: int):
    if connector not in providers.REGISTRY:
        raise AuthzDenied(status, code)
    return providers.REGISTRY[connector]


def _list_activation(ctx: ResolvedCtx, inp: ActivationListInput) -> dict:
    """Tout le registre × son état d'activation (global + overrides d'org)."""
    glob: dict[str, bool] = {}
    overrides: dict[str, list] = {}
    for r in connector_activation.list_activations():
        if r["org_id"] is None:
            glob[r["connector"]] = bool(r["enabled"])
        else:
            overrides.setdefault(r["connector"], []).append(
                {"org_id": r["org_id"], "enabled": bool(r["enabled"])}
            )
    out = [
        {
            "connector": name,
            "label": c.label,
            "help": c.help,
            "namespaces": list(c.namespaces),
            "enabled": glob.get(name),  # None = jamais posé = OFF
            "overrides": overrides.get(name, []),
            "paid_option": access.paid_option_for(name),  # couche 3 (ADR 0044 §H) ou None
        }
        for name, c in providers.REGISTRY.items()
    ]
    return {"connectors": out}


def _set_activation(ctx: ResolvedCtx, inp: ActivationSetInput) -> dict:
    """Pose l'activation : master global si `org_id` absent, sinon override d'org."""
    if inp.connector not in providers.REGISTRY:
        raise AuthzDenied(400, "unknown_connector")
    if not isinstance(inp.enabled, bool):
        raise AuthzDenied(400, "enabled_must_be_bool")
    connector_activation.set_activation(inp.connector, inp.enabled, org_id=inp.org_id,
                                        set_by=ctx.sub)
    # Le chargement des tools est résolu au boot → un changement de master
    # global ne prend effet qu'au prochain redémarrage.
    return {
        "ok": True,
        "connector": inp.connector,
        "enabled": inp.enabled,
        "org_id": inp.org_id,
        "restart_required": inp.org_id is None,
    }


def _clear_override(ctx: ResolvedCtx, inp: ActivationClearInput) -> dict:
    """Supprime un override d'org (le connecteur retombe sur le master global)."""
    if not inp.connector or not inp.org_id:
        raise AuthzDenied(400, "connector_and_org_id_required")
    try:
        org_id = int(inp.org_id)
    except ValueError:
        raise AuthzDenied(400, "org_id_must_be_int")
    connector_activation.clear_activation(inp.connector, org_id)
    return {"ok": True, "connector": inp.connector, "org_id": org_id}


def _platform_access(ctx: ResolvedCtx, inp: PlatformAccessInput) -> dict:
    """[platform_admin] Les orgs et membres à qui la plateforme ouvre ce connecteur :
    grantees de la clé plateforme (`share_down` des instances scope PLATFORM, ADR 0044
    §F) ∪ bénéficiaires de l'option comp (couche 3). Aucun secret."""
    _known(inp.provider, code="unknown_connector", status=404)
    from .. import credentials_store
    option = access.paid_option_for(inp.provider)

    acc: dict[str, dict] = {}

    def touch(scope: str, sid: str) -> dict:
        k = f"{scope}:{sid}"
        if k not in acc:
            acc[k] = {"scope": scope, "id": sid, "has_key": False, "has_option": False}
        return acc[k]

    insts = credentials_store.list_platform_instances(inp.provider)
    open_tier = any(i["share_mode"] == "open" for i in insts)
    for inst in insts:
        for g in inst["share_down"]:
            scope, _, sid = str(g).partition(":")
            if scope in ("user", "org") and sid:
                touch(scope, sid)["has_key"] = True
    if option:
        for c in db.list_option_comps_for_option(option):
            if c["entity_type"] in ("user", "org"):
                touch(c["entity_type"], str(c["entity_id"]))["has_option"] = True

    out = []
    for rec in acc.values():
        if rec["scope"] == "org":
            o = org_store.get_org(int(rec["id"])) if rec["id"].isdigit() else None
            rec["label"] = o["name"] if o else f"org #{rec['id']}"
            rec["logo_url"] = org_store.effective_logo_url(o) if o else None
        else:
            u = db.get_user(rec["id"])
            rec["label"] = (u.get("name") or u.get("email") or rec["id"]) if u else rec["id"]
            rec["email"] = u.get("email") if u else None
        out.append(rec)
    out.sort(key=lambda r: (r["scope"], (r["label"] or "").lower()))
    return {
        "connector": inp.provider,
        "paid_option": option,          # None = pas d'option payante (couche 3)
        "platform_key": bool(insts),    # une clé plateforme existe (couche 2)
        "open_tier": open_tier,         # free-tier : ouvert à tous sans grant
        "beneficiaries": out,
    }


def _set_platform_access(ctx: ResolvedCtx, inp: PlatformAccessSetInput) -> dict:
    """[super_admin] Acte UNIQUE « accès plateforme » (ADR 0044 §H) : ouvre/ferme
    l'accès d'une org ou d'un membre à un connecteur = pose ENSEMBLE l'option comp
    (couche 3) ET le grant de la clé plateforme (couche 2) — ce que le backend
    couplait déjà, exposé en un geste."""
    _known(inp.provider, code="unknown_connector", status=404)
    sid = str(inp.id if inp.id is not None else "").strip()
    if inp.scope not in ("org", "user") or not sid:
        raise AuthzDenied(400, "invalid_body")
    # existence (pas de grant vers un fantôme)
    if inp.scope == "org":
        if not sid.isdigit() or not org_store.get_org(int(sid)):
            raise AuthzDenied(404, "unknown_org")
    elif not db.get_user(sid):
        raise AuthzDenied(404, "unknown_user")

    from .. import credentials_store
    option = access.paid_option_for(inp.provider)
    has_key = bool(credentials_store.list_platform_instances(inp.provider))
    if not option and not has_key:
        # ni option payante ni clé plateforme → rien à ouvrir côté plateforme
        raise AuthzDenied(400, "no_platform_access")
    gscope = f"{inp.scope}:{sid}"
    on = bool(inp.on)
    if on:
        if option:
            db.set_option_comp(inp.scope, sid, option, granted_by=ctx.sub)
        if has_key:
            credentials_store.platform_grant(inp.provider, gscope)
    else:
        if option:
            db.clear_option_comp(inp.scope, sid, option)
        if has_key:
            credentials_store.platform_revoke(inp.provider, gscope)
    return {
        "ok": True, "connector": inp.provider, "scope": inp.scope, "id": sid, "on": on,
        "paid_option": option, "platform_key": has_key,
    }


_DOC_LIST = (
    "Tout le registre de connecteurs × son état d'activation : master global et "
    "overrides par org. ⚠️ `enabled: null` veut dire OFF (jamais posé, deny-by-default), "
    "pas « indéterminé »."
)
_DOC_SET = (
    "Pose l'activation d'un connecteur : master GLOBAL si `org_id` est absent, override "
    "de CETTE org sinon. ⚠️ Le master global ne prend effet qu'au prochain redémarrage "
    "(le chargement des tools est résolu au boot) — `restart_required` le dit. Un "
    "override d'org prend effet tout de suite."
)
_DOC_CLEAR = (
    "Retire un override d'org : le connecteur retombe sur le master global. Les deux "
    "paramètres sont requis."
)
_DOC_ACCESS = (
    "Les orgs et membres à qui la PLATEFORME ouvre ce connecteur — grants de la clé "
    "plateforme et bénéficiaires de l'option offerte, réunis en une vue "
    "connecteur-centrique. Aucun secret. ⚠️ Si `open_tier` est vrai, le connecteur est "
    "ouvert à tous sans grant : la liste ne dit alors plus la population servie."
)
_DOC_SET_ACCESS = (
    "Ouvre ou ferme l'accès plateforme d'une org ou d'un membre à un connecteur, en un "
    "geste : pose ensemble l'option offerte et le grant de clé plateforme, selon ce que "
    "le connecteur possède. Refuse un grant vers une org ou un compte inexistant, et un "
    "connecteur qui n'a ni option ni clé plateforme (`no_platform_access`)."
)

CAPABILITIES += [
    Capability(
        key="platform.connector.activation_list", handler=_list_activation,
        Input=ActivationListInput, authz=PLATFORM_ADMIN, Output=ActivationListView,
        description=_DOC_LIST,
        mcp=None,   # acte de déploiement : les paliers pilotables sont org/équipe
        rest=RestBinding("GET", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.activation_set", handler=_set_activation,
        Input=ActivationSetInput, authz=PLATFORM_ADMIN, Output=ActivationSetView,
        description=_DOC_SET,
        mcp=None,
        rest=RestBinding("POST", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.activation_clear", handler=_clear_override,
        Input=ActivationClearInput, authz=PLATFORM_ADMIN, Output=ActivationClearView,
        description=_DOC_CLEAR,
        mcp=None,
        rest=RestBinding("DELETE", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.access_list", handler=_platform_access,
        Input=PlatformAccessInput, authz=PLATFORM_ADMIN, Output=PlatformAccessView,
        description=_DOC_ACCESS,
        mcp=None,
        rest=RestBinding("GET", _ACCESS),
    ),
    Capability(
        key="platform.connector.access_set", handler=_set_platform_access,
        Input=PlatformAccessSetInput, authz=SUPER_ADMIN, Output=PlatformAccessSetView,
        description=_DOC_SET_ACCESS,
        mcp=None,
        rest=RestBinding("POST", _ACCESS),
    ),
]
