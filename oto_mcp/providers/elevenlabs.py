"""Registry declaration of the `elevenlabs` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# elevenlabs: voice and audio generation — speech (with each word's timing),
# music, sound effects, voice design previews — plus the account's voices,
# models and credit balance.
#
# ONE key in the `xi-api-key` header (`keyed=True`, vault field `key`). A key can
# be restricted to some endpoints when it is created in ElevenLabs: the help says
# which permissions the tools need.
#
# BYO (user OR org: the customer's own credits) or a platform key on explicit grant
# (`platform_key_open=False`, `default_quota=0`: an org without a grant never reaches
# it). The platform key serves the ops the instance chose (`tools/elevenlabs.py::
# platform_ops`, by default speech only); music, sound and voice design ask for the
# customer's own key unless the instance opens them. Each call served on it notes the
# ElevenLabs credits it spent (`units`, `quantity`). Every grant carries a daily quota in
# those credits (`grant_quota_unit`): the default, 0, would be unlimited on our account.
CONNECTOR = _c(
    "elevenlabs", ["elevenlabs"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False, grant_quota_unit="ElevenLabs credits",
    keyed=True, secret_kind="api_key",
    credential_fields=(
        CredentialField(
            "key", "API key", secret=True,
            help="ElevenLabs → Developers → API Keys → Create key. A restricted key "
                 "needs text to speech, music and sound effects for the generations, "
                 "and user read, voices read and models read for the reads."),
    ),
    label="ElevenLabs",
    help="voice and audio: speech with word timings, music, sound effects, voice "
         "design, and the account's voices, models and credits; the shared key, when "
         "granted, covers speech",
    href="https://elevenlabs.io",
)

CATEGORY = "AI & automation"
PUBLISHER = "ElevenLabs"
LOGO_DOMAIN = "elevenlabs.io"

DESCRIPTION = (
    "Generate audio with your ElevenLabs account: a script read by any of your "
    "voices, with each word's timing for captions and edits; music from a prompt or "
    "a section-by-section plan; sound effects; previews of a voice described in "
    "words. Also reads your voices, the models and the credits left. Audio comes "
    "back as a download link. Your own API key, or the shared key when it is granted "
    "to your org: it covers speech (the instance may open more); every other "
    "generation needs your own key."
)
