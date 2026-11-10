"""Registry declaration of the `unipile` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it doesn't
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# unipile: hosted LinkedIn (search/scrape/messaging) via the Unipile API.
# The LinkedIn session lives at Unipile (real Chrome + residential proxy) →
# sidesteps TLS fingerprint + session isolation of the local browser (#5). Keyed
# api_key (resolved via resolve_api_key, cascade user > org). byo_user (BYO) OR
# byo_org (the org sets up the Otomata subscription, its members connect their LinkedIn
# via hosted-auth). Outside the base set (like the whole catalog, 16/07); the paid option
# (layer 3) gates platform usage, BYO stays free. The **dsn** (API v2:
# gateway `api.unipile.com`) comes from the BYO credential's `meta`; the platform key
# takes the oto-core client's default (api.unipile.com), which reads no env — NOT a
# credential field as long as a BYO
# on another endpoint doesn't exist (deferred; single-field = compatible with the existing
# org-secret storage, single-valued).
# ⚠️ Namespace of the LinkedIn tools = `linkedin_unipile` (multi-token), not `unipile`:
# ADR 0010 §Amendment 2026-08-10 — the namespace carries the CAPABILITY, suffixed with the
# PROVIDER when several non-substitutable providers render it (here Unipile,
# operated session · AI Ark, purchased data). `namespace_of` resolves to the longest prefix
# DECLARED here: both therefore keep a distinct gate. `unipile` stays declared for
# `unipile_connect_start` (multi-channel: linkedin|whatsapp|… — it belongs to no
# capability, its target place is `oto_connector op=connect`, see oto-backend#279).
CONNECTOR = _c(
    # ⚠️ A single namespace since the split of 2026-08-28: the channels
    # (`linkedin_unipile`, `whatsapp`, `telegram`, `instagram` — X and Messenger
    # removed on 2026-09-15, the Unipile v2 API doesn't serve them) became connectors in their own right — each with its card, its
    # activation, its ACL, its selection — and a namespace belongs to only ONE
    # connector. This is the ONLY line of this declaration that the split touches:
    # everything else (key, hosted-auth, multi-channel flow, label, modules) is the
    # production code as is. The channels BORROW the key from here
    # (`credential_of="unipile"`, see `channel` at the bottom of this file).
    "unipile", ["unipile"],
    auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", hosted_auth=True, personal_cross_org=True,
    # free-tier: platform key OPEN to everyone, guarded by the layer-3 OPTION (has_option),
    # NOT by a key allowlist. Without this flag, a platform grant (onboarding a user
    # to unipile via the dashboard) turns the key from `open` to `closed`+share_down=[this user]
    # and cuts ALL the others (all-users outage seen 2×  — org 194, then a user). With the
    # flag, `platform_grant` ONLY sets the quota, never closes the key (see oto-backend#245).
    platform_key_open=True,
    label="Hosted messaging (Unipile)",
    # Since the split of 2026-08-28, the six capabilities promised here ARE six other
    # connectors, and this one only exposes a single tool: `unipile_connect_start`.
    # The help kept promising all six (fixed on 2026-09-02).
    help="connect your LinkedIn, WhatsApp, Telegram or Instagram account — the "
         "prerequisite for the connectors of these networks",
    href="https://www.unipile.com",
    modules=("unipile", "whatsapp", "telegram", "instagram"),
)

CATEGORY = "Outreach"
PUBLISHER = "Unipile"
LOGO_DOMAIN = "unipile.com"
DESCRIPTION = (
    "The Unipile subscription that opens hosted messaging: each member then "
    "connects their own LinkedIn, WhatsApp, Telegram or "
    "Instagram account — each with its own card, its activation "
    "and its rights. This card manages the subscription key, not a "
    "conversation."
)


# --- the SHAPE of a hosted connection (the key holder describes it) ------------

def channel(name: str, *, hosted_channel: str, label: str, help: str,
            href: str, modules: tuple[str, ...] = ()):
    """Registry entry of ONE hosted channel — six connectors, a single shape.

    Each channel has its home (`providers/<name>.py`) and declares there what
    DISTINGUISHES it: its name (= its tools namespace), its Unipile channel, its label,
    its brand. What it shares with the other five — the auth modes, the credential
    delegation, the per-person nature — is described HERE, at the key holder,
    because it's a property of the ACCOUNT and not of the channel.
    Copying these flags six times means giving ourselves five chances to make them
    diverge.

    The channel OWNS nothing: `credential_of="unipile"` points vault, quota, platform
    key and layer-3 option to the account. What it owns itself is
    what is governed per channel — activation, ACL, selection, tool visibility,
    and its hosted connection (one flow per card, no parameter).

    ⚠️ `platform_key_open` is NOT copied: it governs the SHARING of a platform
    key and is read on the holder, after normalization by the cascade. Setting it
    on a channel would be dead configuration that the next reader would
    take for live — the exact shape of the all-users outage of #245.
    `personal_cross_org`, ON THE OTHER HAND, is copied: `call_axes` resolves by NAMESPACE, and
    it's this flag that makes the `_account=` axis exist on the channel's tools
    — hence the granted accounts (#55). The two don't answer the same question.

    `href` is that of the channel's BRAND, never the provider's: what the
    person connects is their LinkedIn or their WhatsApp.

    ⚠️ `publisher` is the ONLY field of the card that names the provider, and its
    definition requires it: the publisher names **who receives the call**
    (`docs/connector-vault.md` §"What the card SAYS", 2026-09-02). A
    `whatsapp_act` send goes to Unipile, which holds the operated account's
    session — the message is sent BY a third-party gateway. Until 2026-09-02
    these six cards fell back on the default "Otomata": we were attributing the
    sending to ourselves. It's declared HERE, in a single copy, because it's a property of the
    ACCOUNT (like the key, the quota, the option) and not of the channel — copying it six
    times would mean giving ourselves five chances to make it diverge. Same shape as
    `reddit` → `redditapis.com`: the gateway is named, the brand keeps the
    label, the logo and the `href`.

    ⚠️ **And each channel's `help` says it TOO, in one clause.** The publisher is not
    enough: the catalog block injected at the handshake only serves `"label : help"`
    — not the publisher — and the help is what one reads BEFORE installing. All six got
    it on 2026-09-02 (`docs/connector-vault.md`: "the help says that an intermediary
    exists, in one clause and without jargon"). **Same shape for all six**, and a channel
    added tomorrow must have it too: ratchet in `tests/test_unipile_split.py`."""
    return _c(
        name, [name],
        auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
        secret_kind="api_key", hosted_auth=True, personal_cross_org=True,
        credential_of="unipile", hosted_channel=hosted_channel,
        publisher="Unipile",
        label=label, help=help, href=href,
        modules=modules or (name,),
    )
