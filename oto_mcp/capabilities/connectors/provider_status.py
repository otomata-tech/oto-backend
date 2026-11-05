"""The access verdict for ONE connector, as `access.status_for` produces it.

Served under `providers` by `GET /api/me` since forever, and declared `dict[str, Any]`
until 2026-09-01: rich, consumed by the product dashboard, and named nowhere. A front end
building a “status” column therefore couldn't derive anything from it without observing
the payload — that is the reason for #669.

⚠️ **The written fear that kept `Any` concerns the dictionary's KEYS, not the shape of a
value**: “an open object rather than an enumeration that would lie at the first connector
added”. The keys stay open (`dict[str, ProviderStatus]`) — it is the VALUE that is
declared, and it has been stable for months.

Four families produce an entry, and their fields differ: keyed (`keyed`), keyless,
`cookie` and `oauth`. Hence optional fields **per family** and not out of uncertainty:
`identity_label` makes no sense for a keyed connector, and its absence is information,
not a gap.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProviderStatus(BaseModel):
    """The effective access to a connector, for the actor and in the active org.

    ⚠️ **Two different refusals, which a screen must not confuse**:
    `mode='forbidden'` = no key resolves; `health_ko` = the key is there but it no
    longer responds. There is no longer any rule reserving a connector for some of the
    members (removed on 24/09/2026, ADR 0053 D1): no screen should display
    “reserved for some teams”.
    """

    mode: str = Field(description=(
        "How access resolves — the winning tier of the cascade, or its refusal. "
        "Values served as of 2026-09-01: `user` | `group` | `org` | `tenant` | "
        "`platform` (the tier that supplies the key), `over_quota` (a key resolves but "
        "today's quota is exhausted), `forbidden` (no key resolves), `elsewhere` "
        "(nothing of this org serves: an account of ANOTHER org does — "
        "`lent_by_org` or `your_account_in_org` says which). Declared "
        "`str` and not enumerated on purpose: the first five come from the cascade, "
        "which has its own home — an enum here would break a generated client the "
        "day it returns a sixth."))

    # ── What is SET, tier by tier ─────────────────────────────────────────────
    # Three booleans rather than a single `mode`: the mode says who WINS, these say
    # what EXISTS. A “remove my key” screen needs to know it is there even when it is
    # the org's key that resolves.
    user_key_configured: bool = False
    group_secret_configured: bool = False
    org_secret_configured: bool = False
    # The label of the REACHABLE platform key — not “the one that resolves”.
    # LEVEL flag: served even when a closer key responds, because
    # “what you would fall back on” is a valid piece of information (the front end
    # displays it since v1.12.0). NOT to be confused with the quota fields below, which
    # describe the CURRENT effect and stay silent outside the platform tier.
    platform_key_label: Optional[str] = None
    # The team whose key would be REACHABLE for this connector, when there is one.
    team_key_group: Optional[int] = None
    # `mode='elsewhere'` (#1499, step 5): the org of the account that serves — lent to
    # you by a rule of that org, or your own key set there. Null otherwise.
    lent_by_org: Optional[str] = None
    your_account_in_org: Optional[str] = None

    # ── The quota, when the key that RESPONDS carries one ─────────────────────
    # Both are read on the WINNING tier, never on the mere presence of a platform tier
    # in the cascade: outside that tier, the quota is neither counted
    # (the settlement only debits a call served by a platform key) nor enforced
    # (`resolve_api_key` returns before `_win_quota`). `null` on both sides = this
    # access path has no quota — NOT “zero allowed”.
    # ⚠️ `quota_used_today` used to be `int = 0`: serving `null` on a connector without a
    # cap then raised a ValidationError on the whole route, not an empty field. Making it
    # Optional is part of the same fix, not a cleanup.
    quota_used_today: Optional[int] = None
    quota_daily: Optional[int] = None

    # ── `cookie` and `oauth` families: a session, not a key ───────────────────
    session_set_at: Optional[str] = None
    group_session_set_at: Optional[str] = None
    org_session_set_at: Optional[str] = None
    # The default identity of a connector that carries several (the hosted
    # channels). Absent everywhere else.
    identity_id: Optional[str] = None
    identity_label: Optional[str] = None

    # ── The verdicts a screen must distinguish ───────────────────────────────
    pending_action: Optional[str] = Field(default=None, description=(
        "The step that remains to be done even though the key already resolves — linking "
        "a channel, for example. Filled in by the connector's module, `null` wherever "
        "there is nothing to do. ⚠️ This is NOT a refusal: access exists, it is "
        "incomplete."))
    health_ko: Optional[bool] = Field(default=None, description=(
        "The key is set but the connector no longer responds (expired session, revoked "
        "token…), observed by the verification probe and **persistent** until a "
        "reconnection or a successful test. Absent as long as nothing has been observed."))
    health_reason: Optional[str] = Field(default=None, description=(
        "Why the key no longer responds, when the probe was able to say — its text, "
        "as is. ⚠️ May be `null` **even though `health_ko` is true**: we know it no "
        "longer responds, without knowing why. Display the state without inventing the "
        "cause — a fabricated message would send people looking in the wrong place."))
