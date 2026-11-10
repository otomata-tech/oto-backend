"""Registry declaration of the `typeform` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# typeform: online forms — workspaces, forms (read, create, patch, replace,
# delete), responses (read, summary, delete), webhooks. byo (user OR org),
# resolved via `resolve_credential_fields`: a personal token acts in the name of
# its creator and carries ITS scopes, no shared platform key. A read-only token
# keeps every read; a write it lacks the scope for is refused by Typeform (403),
# and the tool names the scope.
# TWO fields: the token, and the account's region. Typeform has three hosts (one
# per data center) and an account's RESPONSES can only be read in its own:
# elsewhere, the list comes back empty without error. The region is DECLARED at setup,
# closed set (`choices`) — never a free URL, hence no typed-in destination.
# Three tool modules, by object (the 500-line cap): the base and the responses,
# the forms, the webhooks.
CONNECTOR = _c(
    "typeform", ["typeform"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Typeform",
    help="online forms: workspaces, forms (read, create, edit, publish, delete), "
         "responses (read, statistics, delete), webhooks",
    href="https://www.typeform.com",
    modules=("typeform", "typeform_formulaires", "typeform_webhooks"),
    credential_fields=(
        CredentialField(
            "key", "Typeform personal access token", secret=True,
            help="Typeform → Account → Personal tokens → Generate a new token "
                 "(`tfp_…`). Read scopes: forms:read, responses:read, "
                 "workspaces:read, webhooks:read. To create, edit or delete add "
                 "forms:write, responses:write, webhooks:write — without them "
                 "everything stays readable and writes are refused. The token "
                 "acts as the account that created it."),
        CredentialField(
            "region", "Data center", secret=False, required=False,
            choices=("us", "eu", "eu2"),
            help="Where the account's responses are stored: empty or « us » "
                 "(api.typeform.com), « eu » (api.eu.typeform.com), « eu2 » "
                 "(api.typeform.eu, the newer EU data center). Responses read "
                 "from another data center come back empty."),
    ),
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Typeform"
LOGO_DOMAIN = "typeform.com"

DESCRIPTION = (
    "Online forms built with Typeform: workspaces, forms with their questions, "
    "responses filtered by date, completion or text, and their statistics "
    "(answer rates, choices, scores, NPS). With write scopes, create, edit, "
    "publish or delete forms, delete responses, and send responses to a webhook "
    "— irreversible changes ask for confirmation. Each person or organization "
    "connects its own personal access token, with the account's data center "
    "(US or EU) — no shared platform key."
)
