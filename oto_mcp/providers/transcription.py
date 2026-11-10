"""Registry declaration of the `transcription` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# transcription: a project audio file becomes a project page (ADR 0074). The
# provider is Mistral (Voxtral); the connector carries the VERB, not the brand —
# `mistral` already exists as a key carrier WITHOUT a tool (`kind="credential"`, scheduled
# agents), and a tool connector does not graft onto it.
#
# `byo_org` + `platform`: the key is the organization's (it is the one paying for the
# audio minute) OR a platform instance granted to an org (the org without
# its own key, e.g. a pilot: the key, language and vocabulary are then those of the
# platform instance). The org that has its own instance takes precedence over the platform
# (cascade). MONO-account, declared: a call
# transcribes with ONE key and ONE vocabulary, and two instances set on the same org
# would have no criterion to tell them apart — attaching to a project goes through
# a slot (ADR 0035), like any instance.
#
# Two NON-secret fields make the instance "one key × one language × one
# vocabulary" (ADR 0074 D1):
# - `language`: language code, `fr` when it is empty; `auto` = detection by
#   the upstream;
# - `vocabulary`: business terms entered by hand, separated by commas or
#   line breaks — the space is SIGNIFICANT there (« pompe à chaleur »), hence
#   `whitespace_significant`: the default cleanup would strip all blanks.
CONNECTOR = _c(
    "transcription", ["transcription"], auth_modes={"byo_org", "platform"},
    secret_kind="fields", cardinality="mono",
    label="Transcription (Mistral)",
    help="transcribes a project audio file into a project page — speakers "
         "distinguished, organization vocabulary",
    href="https://console.mistral.ai/api-keys",
    credential_fields=(
        CredentialField("api_key", "Mistral API key", secret=True,
                        help="created at console.mistral.ai"),
        CredentialField("language", "Language", secret=False, required=False,
                        help="language code of the audio (default: fr); \"auto\" = "
                             "automatic detection"),
        CredentialField("vocabulary", "Vocabulary", secret=False, required=False,
                        whitespace_significant=True,
                        help="business terms to spell correctly (structures, "
                             "materials, proper names), separated by commas or "
                             "line breaks — 100 words at most"),
    ),
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Mistral AI"
LOGO_DOMAIN = "mistral.ai"

DESCRIPTION = (
    "Transcription of audio recordings (visits, meetings, voice notes) with "
    "Mistral Voxtral, hosted in the EU: the file dropped on a project becomes a "
    "project page, one paragraph per speaker turn, with the organization's "
    "business vocabulary."
)
