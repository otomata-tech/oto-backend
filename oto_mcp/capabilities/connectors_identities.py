"""Capacités « sélecteur d'identité connectée » (ADR 0024) — surface unifiée,
co-déclarée MCP + REST, per-membre (`SUB_ONLY`). Backend par-connecteur dans
`connector_identities` (Google = comptes du coffre ; Unipile = identités distantes
d'une clé BYO). Le dashboard pose dessus le picker (liste + défaut)."""
from __future__ import annotations

import inspect

from pydantic import BaseModel

from .. import connector_identities
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding


class IdentitiesInput(BaseModel):
    connector: str                       # nom de connecteur (path {connector})


class SetIdentityInput(BaseModel):
    connector: str                       # path {connector}
    identity_id: str                     # body — id renvoyé par connectors.identities


# Handlers async : un backend d'identités enregistré (`connector_identities.register`)
# peut être async (Browserbase — pennylaneged) ; les deux adaptateurs (MCP/REST)
# awaitent les handlers awaitable, on relaie ici.
def _require_known_connector(name: str) -> None:
    """Slug hors catalogue → erreur explicite, jamais le même payload qu'un
    connecteur connu sans identités (feedback #162 : `linkedin` rendait
    `{supported:false, identities:[]}` comme un nom bidon — faux négatif
    silencieux pour l'agent qui s'est trompé de slug)."""
    from .. import providers
    if name in providers.REGISTRY:
        return
    hint = " (LinkedIn passe par le connecteur `unipile`)" if name == "linkedin" else ""
    raise AuthzDenied(
        404, "unknown_connector",
        f"Connecteur inconnu : `{name}`{hint}. Slugs valides : `oto_my_connectors`.")


async def _list(ctx: ResolvedCtx, inp: IdentitiesInput) -> dict:
    _require_known_connector(inp.connector)
    ids = connector_identities.list_identities(ctx.sub, inp.connector)
    if inspect.isawaitable(ids):
        ids = await ids
    return {
        "connector": inp.connector,
        "supported": connector_identities.supports(inp.connector),
        "identities": ids,
    }


async def _set_default(ctx: ResolvedCtx, inp: SetIdentityInput) -> dict:
    _require_known_connector(inp.connector)
    try:
        res = connector_identities.select_identity(ctx.sub, inp.connector, inp.identity_id)
        if inspect.isawaitable(res):
            res = await res
    except ValueError as e:
        raise AuthzDenied(404, "unknown_identity", str(e))
    return {"connector": inp.connector, **res}


CAPABILITIES_DOC_LIST = (
    "List the connected identities/accounts your credential can act as for a connector "
    "(e.g. the LinkedIn accounts under your Unipile key, or your Google accounts), with "
    "which one is currently the default. Empty when the connector has no identity choice "
    "(or uses a shared platform key — connect via hosted auth instead)."
)
CAPABILITIES_DOC_SET = (
    "Choose which connected identity/account to act as for a connector (identity_id from "
    "connectors.identities). Unipile → picks the LinkedIn (or other channel) account; "
    "Google → sets the default account. Rejects an id not reachable by your credential."
)

from .registry import CAPABILITIES  # noqa: E402

CAPABILITIES += [
    Capability(
        key="connectors.identities", handler=_list, Input=IdentitiesInput, authz=SUB_ONLY,
        description=CAPABILITIES_DOC_LIST,
        mcp="oto_connector_identities",
        rest=RestBinding("GET", "/api/connectors/{connector}/identities"),
    ),
    Capability(
        key="connectors.set_default_identity", handler=_set_default, Input=SetIdentityInput,
        authz=SUB_ONLY, description=CAPABILITIES_DOC_SET,
        mcp="oto_set_connector_identity",
        rest=RestBinding("PUT", "/api/connectors/{connector}/identities/default"),
    ),
]
