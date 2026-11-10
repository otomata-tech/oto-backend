"""Registry declaration of the `anthropic` key carrier.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). Cf. `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# ⚠️ `kind="credential"`: NO tool. This object only serves to carry the key
# that the platform uses ON BEHALF OF the org — a scheduled agent consumes
# it, no tool exposes it. An ordinary connector with empty namespaces
# would present itself as a connector without having its effects; the
# distinct type lets the screen say what it is.
#
# `byo_org`: the key is the ORGANIZATION's, not a person's —
# it is what pays for the turns, and a scheduled agent outlives its author.
#
# `platform` (18/10/2026): a SHARED key — set at the platform level of the instance,
# or by the tenant — serves an org that deposited none, and each of its model calls is
# reserved then settled in the units budget (`access/credits.py`, `anthropic.messages`,
# in tokens, `key_mode` tenant|platform) for the commerce to charge back. CLOSED (`platform_key_open=False`): it only
# serves the orgs it is granted to — the grant IS the right, and removing it cuts the
# org at the next claim. Cf. `capabilities/_cle_exigee.cle_partagee`.
CONNECTOR = _c(
    "anthropic", [], kind="credential", auth_modes={"byo_org", "platform"}, keyed=True,
    platform_key_open=False,
    # ⚠️ SINGLE-account, declared: the derivation would yield `multi` (api_key), and
    # the screen would offer to set a second key that nothing could choose between.
    # A run uses ONE key — two deposits for the same org would be two
    # invoices for the same work, and the worker has no criterion to decide.
    cardinality="mono",
    # `fields` (14/09/2026): the key, and the WORKSPACE of an organization key. A
    # key created for the whole Anthropic organization, and not in a workspace, makes
    # every request that does not name the workspace to bill be refused (header
    # `anthropic-workspace-id`). The workspace is not secret and lives in `meta`
    # (`in_meta`): the encrypted blob keeps the RAW key, and keys already deposited
    # read back identically. The worker receives it at claim, next to the key.
    secret_kind="fields", label="Anthropic",
    credential_fields=(
        # The three Console API-key shapes, all sent as `x-api-key`: `api` (historical),
        # `usr` (personal or workspace key, the Console's shape since 2026), `svc`
        # (service account). A session token (`sk-ant-oat…`) or an admin key
        # (`sk-ant-admin…`) is not a model key: refused at deposit, not at the claim.
        # Then the key is TRIED at Anthropic before it is written (`anthropic_cle`, called
        # by `credentials_store.preparer_pose`): `GET /v1/models?limit=1`, no token spent.
        # Rejected key, organization key without its workspace, or Anthropic unreachable:
        # the deposit is refused — an unverified key is never stored.
        CredentialField("key", "API key", secret=True,
                        prefixes=("sk-ant-api", "sk-ant-usr", "sk-ant-svc")),
        CredentialField(
            "workspace_id", "Workspace", secret=False, required=False, in_meta=True,
            help="Only for an ORGANIZATION key (created outside a workspace): "
                 "the id of the workspace to bill (`wrkspc_…`). Empty for a workspace key. "
                 "The key is tried at Anthropic on deposit: an organization key without "
                 "its workspace is refused."),
    ),
    help="Anthropic model key — used by the organization's scheduled agents, never by a tool. "
         "Without one, a platform key granted to the organization is used and billed.",
    href="https://console.anthropic.com/settings/keys",
)

CATEGORY = "AI & automation"
PUBLISHER = "Anthropic"
LOGO_DOMAIN = "anthropic.com"
