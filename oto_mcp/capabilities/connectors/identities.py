"""« Connected identity selector » capabilities (ADR 0024) — unified surface,
co-declared MCP + REST, per-member (`SUB_ONLY`). Per-connector backend in
`connector_identities` (Google = vault accounts; Unipile = remote identities
of a BYO key). The dashboard builds the picker (list + default) on top of it."""
from __future__ import annotations

import inspect
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from ... import account_suspension
from ...connectors import identities as connector_identities
from .._authz import SUB_ONLY, _refus_chef_d_equipe, _refus_org_admin
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

logger = logging.getLogger(__name__)


class IdentitiesInput(BaseModel):
    connector: str                       # connector name (path {connector})
    # Phase 2 (2026-08-25): `org` / `group` = the named accounts of the shared tier
    # (generic keyed backend only). Default: your own.
    scope: str = "member"


class SetIdentityInput(BaseModel):
    connector: str                       # path {connector}
    identity_id: str                     # body — id returned by connectors.identities
    scope: str = "member"                # `org`/`group`: tier admin required


class RenameIdentityInput(BaseModel):
    connector: str                       # path {connector}
    identity_id: str                     # path {identity_id} — the current name
    name: str                            # body — the new name (= the value of `_account=`)
    scope: str = "member"                # `org`/`group`: tier admin required


class RenamedIdentity(BaseModel):
    connector: str
    id: str                              # the new name
    previous_id: str
    is_default: bool


class IdentityOwner(BaseModel):
    """Owner of a GRANTED account (#55) — present only on an identity
    that is operated without being owned."""
    sub: str
    email: Optional[str] = None
    name: Optional[str] = None
    org: Optional[int] = None               # org under which the owner connected the account
    org_name: Optional[str] = None


class Identity(BaseModel):
    """Common `Identity` contract of the three backends (Google = vault accounts,
    Unipile = remote identities of a key, generic keyed = vault rows).
    The unification is at SURFACE level, not storage."""
    # Opaque, and of a different nature depending on the backend: Google email, Unipile
    # remote handle, vault account name. Never parse it.
    id: str
    label: Optional[str] = None
    # `ok` by default. On a hosted Unipile account, the status is confirmed by
    # a liveness probe (users/me) and downgraded to `disconnected` — the account
    # status reported by the provider can stay « OK » while the session
    # is dead (#236). Fail-soft: a probe incident leaves `ok`.
    status: Optional[str] = None
    # Identity actually operated on ITS channel. Several entries can therefore
    # be `is_default` at the same time on a multi-channel connector (one per channel).
    is_default: bool
    # `null` outside multi-channel (Google) — an accepted leak of the Unipile model, which is
    # per-channel where Google is per-service.
    channel: Optional[str] = None
    granted: Optional[bool] = None          # present (true) if the account is GRANTED (#55)
    owner: Optional[IdentityOwner] = None   # the lender, present with `granted`
    # `true` = an account SHARED by the caller's team or org (Google, 2026-09-27),
    # reachable via `_account=` but not theirs: the member cannot revoke it (a team or
    # org admin does, from the shared tier). Always served, `false` by default — every
    # other backend (Microsoft included) lists the member's own accounts.
    shared: bool = False
    # The tier that shares it — `team` | `org` — present with `shared`, `null` otherwise.
    shared_scope: Optional[Literal["team", "org"]] = None


class ConnectorIdentities(BaseModel):
    """Identities reachable by the caller's resolved credential for a connector."""
    connector: str
    # `false` = this connector has NO identity selector. But the converse does not
    # hold: `supported:true` with `identities: []` is normal (Unipile platform key
    # → go through the hosted connection, or no account connected). An
    # UNKNOWN slug never returns this payload — it raises a 404 (feedback #162:
    # `{supported:false, identities:[]}` made a bogus name indistinguishable from a real
    # connector without identities).
    supported: bool
    identities: list[Identity]
    # The provider's WORD for an account of this connector — « workspace » at
    # Slack, « organisation » at Zoho, « site » for the connected browser,
    # « compte » by default. Served here because it is the answer read by whoever
    # CHOOSES: saying « account » for a Slack workspace forces them to translate, and neither
    # the agent nor the screen has any way to guess the provider's vocabulary.
    noun: str = "compte"
    # ── Why the list is EMPTY (signal #504) ── present ONLY on `[]`.
    # `no_credential` | `paid_option_off` | `over_quota` | `credential_rejected`
    # (a layer is missing, see `connectors/readiness.py`) | `no_identity_connected`
    # (everything is in place, one
    # remains to be connected). The defect of #504 was not the CONTENT of the
    # list — verified on prod on 28/08, it was empty because there was
    # nothing to list — it was its SILENCE: `[]` did not say whether there was no
    # account, no key, or a key that sees nothing, and the caller invented the
    # wrong cause for four days.
    reason: Optional[str] = None
    next_step: Optional[str] = None         # the action, returned as is


class SelectedIdentity(BaseModel):
    """The chosen identity, as returned by the connector's backend. The keys
    vary with the branch taken: a GRANTED account carries `granted`, the generic
    keyed backend returns a `label`, the others neither —
    hence the openness to additional fields."""
    model_config = ConfigDict(extra="allow")

    connector: str
    id: str
    is_default: bool                        # always true — it is the effect of the verb
    # `null` for a non-multi-channel connector. On Unipile, selecting
    # ONE'S OWN account clears the channel's « operated identity » pointer (back to
    # self); the return is the same in both cases.
    channel: Optional[str] = None
    label: Optional[str] = None
    granted: Optional[bool] = None


# Async handlers: a registered identities backend (`connector_identities.register`)
# can be async (Browserbase — pennylaneged); the two adapters (MCP/REST)
# await awaitable handlers, we relay here.
def _require_known_connector(name: str) -> None:
    """Slug outside the catalog → explicit error, never the same payload as a
    known connector without identities (feedback #162: `linkedin` returned
    `{supported:false, identities:[]}` like a bogus name — a silent false negative
    for the agent that got the slug wrong).

    ⚠️ `linkedin` remains a tricky alias even since this slug DESIGNATES a real
    connector (#231: B2B search via AI Ark, app-credits key ONLY, no
    notion of a connected account — distinct from `aiark`, which keeps its BYO): an agent
    that types `linkedin` almost always means THEIR personal LinkedIn account, which
    lives under `unipile`. We therefore keep the hint BEFORE the registry check — otherwise the
    same confusion is reborn in a different form (`{supported:false}` instead
    of 404)."""
    from ... import providers
    if name == "linkedin":
        raise AuthzDenied(
            404, "unknown_connector",
            "Unknown connector for identities: `linkedin` (B2B search, "
            "shared app-credits key, no personal account) has no identities. "
            "Your personal LinkedIn account goes through the `unipile` connector. "
            "Valid slugs: `oto_connector(op='list')`.")
    if name in providers.REGISTRY:
        return
    raise AuthzDenied(
        404, "unknown_connector",
        f"Unknown connector: `{name}`. Valid slugs: `oto_connector(op='list')`.")


def _require_scope(ctx: ResolvedCtx, scope: str, *, write: bool) -> None:
    """`member`: always. `org` / `group`: org member (read); tier admin
    to choose the default (write) — the shared key is everyone's."""
    if scope not in connector_identities.SCOPES:
        raise AuthzDenied(400, "bad_scope", f"unknown scope: `{scope}`.")
    if scope == "member" or not write:
        return
    from ... import access, roles
    org = access.current_org(ctx.sub)
    if org is None:
        raise AuthzDenied(400, "no_org_context", "No context org.")
    if scope == "org" and not roles.is_org_admin(ctx.sub, org):
        raise _refus_org_admin(org, sub=ctx.sub)
    if scope == "group":
        gid = access.current_group(ctx.sub)
        if gid is None:
            raise AuthzDenied(400, "no_group_context", "No context team.")
        if not roles.can_admin_group(ctx.sub, gid):
            raise _refus_chef_d_equipe(gid, sub=ctx.sub)


async def _list(ctx: ResolvedCtx, inp: IdentitiesInput) -> dict:
    _require_known_connector(inp.connector)
    _require_scope(ctx, inp.scope, write=False)
    try:
        ids = connector_identities.list_identities(ctx.sub, inp.connector, inp.scope)
        if inspect.isawaitable(ids):
            ids = await ids
    except AuthzDenied:
        raise
    except Exception as e:
        # oto-backend#867 — a slow/down Unipile must return a named error,
        # never a freeze nor a silently empty list. `_unipile_list` no longer
        # swallows it for BYO (main list): it surfaces here.
        if inp.connector != "unipile":
            raise
        raise AuthzDenied(502, "unipile_list_failed",
                          f"Unipile did not respond (timeout or outage): {e}")
    from ... import access
    noun = access.account_noun(inp.connector)
    out = {
        "connector": inp.connector,
        "supported": connector_identities.supports(inp.connector),
        "noun": noun,
        "identities": ids,
    }
    if not ids:
        out.update(_why_empty(ctx, inp.connector, noun))
    return out


def _why_empty(ctx: ResolvedCtx, connector: str, noun: str) -> dict:
    """The WHY of an empty list (#504) — never a silent `[]`.

    What the signal claimed: « `oto_identity(op=list, connector=unipile)` returns
    `identities:[]` while the LinkedIn account is connected and operational ».
    What prod shows (verified on 28/08/2026): the three reads of this account
    date from 14/08 at 14:00:44, 14:00:55 and 14:02:09 — the account itself was linked at
    **14:03:30**. Replayed today on the same sub, the list does return the account.
    It was empty because there was nothing to list; the REAL defect is the
    silence, which let people conclude it was a bug for four days.

    Same family as #476 (see `connectors/readiness.py`), and same seam: a missing
    layer is named here as there. `[]` with no missing layer means
    exactly one thing — nothing is connected yet — and that is said too.

    Fail-VISIBLE: if the diagnosis cannot be read, we SAY so (`reason:"unknown"`)
    rather than returning an empty list again without explanation."""
    from ... import access
    from ...connectors import readiness as connector_readiness
    try:
        diag = connector_readiness.diagnose(
            ctx.sub, connector, org=ctx.org_id, group=access.current_group(ctx.sub))
    except Exception:
        logger.warning("empty-identities diagnosis unavailable for %s (fail-visible)",
                       connector, exc_info=True)
        return {"reason": "unknown",
                "next_step": (f"Empty list, and the state of the layers (key, option) "
                              f"could not be read — check `{connector}` with "
                              f"oto_connector(op='list', name='{connector}').")}
    # `pending_step` ⟹ it really is « no linked account »: we name it in the
    # vocabulary of THIS surface, relaying the connector's action as is.
    if diag is None or diag.reason == connector_readiness.PENDING_STEP:
        # ⚠️ **`pending_step` means « the LAYERS are good »** — the key
        # resolves, the connector can work perfectly well. This empty list
        # therefore describes only the identity registry, never the connector's health,
        # and returning a plain « nothing is connected » made it read like an
        # outage diagnosis (#850, 10/09/2026).
        #
        # The measured case: in the SAME minute this list returned `[]`, the same
        # caller joined a channel and read 37 messages on the targeted
        # workspace. The day before, all calls really were failing and this list
        # answered `[]` **too** — so it served as corroboration for a
        # wrong conclusion. *A read that returns the same answer when everything is fine
        # and when everything is broken corroborates nothing: it confirms what its
        # reader already believes.* That is worse than no read at all.
        couches_ok = (diag is not None
                      and diag.reason == connector_readiness.PENDING_STEP)
        out = {"reason": "no_identity_connected",
               "next_step": (diag.next_step if diag is not None
                             else connector_readiness.no_identity_step(
                                 ctx.sub, connector, noun))}
        if couches_ok:
            # ⚠️ **In a SEPARATE field, not in `next_step`.** The latter is the
            # action declared by the connector, relayed as is: two surfaces that
            # reword it tell two stories, and its bench keeps that exact
            # equality. The caveat concerns the SCOPE of the read,
            # which is a different fact — so it gets its own key.
            out["layers_ok"] = True
            out["scope_note"] = (
                f"This says NOTHING about the health of `{connector}`: its layers "
                "resolve (key, option), so its calls may succeed right "
                "now. This list only describes the identity registry — "
                "do not conclude that a failing call fails for this reason, "
                f"check with oto_instance(op='verify', connector='{connector}')."
            )
        return out
    return {"reason": diag.reason, "next_step": diag.next_step}


async def _set_default(ctx: ResolvedCtx, inp: SetIdentityInput) -> dict:
    _require_known_connector(inp.connector)
    _require_scope(ctx, inp.scope, write=True)
    try:
        res = connector_identities.select_identity(ctx.sub, inp.connector, inp.identity_id, inp.scope)
        if inspect.isawaitable(res):
            res = await res
    except account_suspension.PreteurEnPause as e:
        # #898: the account exists and is lent to you — it is its lender who is paused.
        # An « unknown » 404 would send people looking for an identity that has not vanished.
        raise AuthzDenied(403, account_suspension.CODE_PRETEUR, str(e))
    except ValueError as e:
        raise AuthzDenied(404, "unknown_identity", str(e))
    except AuthzDenied:
        raise
    except Exception as e:
        # oto-backend#867 — same rule as `_list`: `_unipile_select` raises a
        # `RuntimeError` (not a `ValueError`) precisely so as not to be confused
        # with an unknown id — a Unipile outage/slowness is not a 404.
        if inp.connector != "unipile":
            raise
        raise AuthzDenied(502, "unipile_list_failed", str(e))
    return {"connector": inp.connector, **res}


def _rename_sync(ctx: ResolvedCtx, inp: RenameIdentityInput) -> dict:
    _require_known_connector(inp.connector)
    _require_scope(ctx, inp.scope, write=True)
    try:
        res = connector_identities.rename_identity(
            ctx.sub, inp.connector, inp.identity_id, inp.name, inp.scope)
    except ValueError as e:
        raise AuthzDenied(400, "rename_refused", str(e))
    return {"connector": inp.connector, "previous_id": inp.identity_id, **res}


async def _rename(ctx: ResolvedCtx, inp: RenameIdentityInput) -> dict:
    # Roles + vault = SQL: off the loop (mono-loop, `docs/event-loop-perf.md`).
    return await run_in_threadpool(_rename_sync, ctx, inp)


CAPABILITIES_DOC_LIST = (
    "List the connected identities/accounts your credential can act as for a connector "
    "(e.g. the LinkedIn accounts under your Unipile key, or your Google accounts), with "
    "which one is currently the default. An EMPTY list always says why: `reason` is "
    "no_credential / paid_option_off / over_quota / credential_rejected (a layer is "
    "missing — credential_rejected = the key resolves but the provider refused it at "
    "the last connection test) or "
    "no_identity_connected (everything resolves, nothing linked yet), with `next_step`. "
    "Never read an empty list as a bug before reading its reason."
)
CAPABILITIES_DOC_SET = (
    "Choose which connected identity/account to act as for a connector (identity_id from "
    "connectors.identities). Unipile → picks the LinkedIn (or other channel) account; "
    "Google → sets the default account. Rejects an id not reachable by your credential."
)
CAPABILITIES_DOC_RENAME = (
    "Rename a named account of a multi-account connector (one key per company, "
    "workspace…). The name is what `_account=` targets, so callers must use the new one. "
    "Refuses an unknown account or a name already taken at that level."
)

from ..registry import CAPABILITIES  # noqa: E402

CAPABILITIES += [
    Capability(
        key="connectors.identities", handler=_list, Input=IdentitiesInput, authz=SUB_ONLY,
        Output=ConnectorIdentities,
        description=CAPABILITIES_DOC_LIST,
        rest=RestBinding("GET", "/api/connectors/{connector}/identities"),
    ),
    Capability(
        key="connectors.set_default_identity", handler=_set_default, Input=SetIdentityInput,
        authz=SUB_ONLY, Output=SelectedIdentity, description=CAPABILITIES_DOC_SET,
        rest=RestBinding("PUT", "/api/connectors/{connector}/identities/default"),
    ),
    Capability(
        key="connectors.rename_identity", handler=_rename, Input=RenameIdentityInput,
        authz=SUB_ONLY, Output=RenamedIdentity, description=CAPABILITIES_DOC_RENAME,
        rest=RestBinding("PATCH", "/api/connectors/{connector}/identities/{identity_id}"),
    ),
]
