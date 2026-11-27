## prerequisite — twilio Account SID + Auth Token

an admin of the organization opens the Twilio Console (Account Info) and pastes **both** values into oto: the `Account SID` (starts with `AC`) and the `Auth Token`. Reference: [Twilio API authentication](https://www.twilio.com/docs/usage/requests-to-twilio).
- the Auth Token is asked for, not an API key: it is also what signs the webhooks Twilio sends for the account
- your own Twilio account: Twilio bills numbers, minutes and messages to it directly
- **no Twilio account?** Buy a number on the Phone page instead: it is billed in credits, and the same tools reach it (`twilio(op="numbers")`, `twilio_act(op="send_sms"|"call")`) without any key here
- ⚠️ a **trial** account only calls and texts **verified** numbers, from one shared trial number: upgrade it to reach anyone (`twilio(op="account")` says `type`: Trial or Full)

## usage — numbers, calls, SMS

- "which numbers do we have?" → `twilio(op="numbers")`
- "find a French number that can text" → `twilio(op="available_numbers", country="FR", number_type="mobile", sms_enabled=true)`, then `twilio_act(op="add_number", phone_number="+33…")` (org admins only; billed by Twilio)
- "call this lead from my phone" → `twilio_act(op="call", rep="+33<my phone>", to=["+33<lead>"], from_="+33<our number>")`: your phone rings first, then Twilio dials the lead and records both sides
- "transcribe that call" → `twilio(op="recordings", sid="CA…")`, then `twilio(op="recording_audio", sid="RE…")` and `transcription_create(source=<that source, as is>, _project=…)`
- "text these people" → `twilio_act(op="send_sms", from_="+33…", to=["+33…", …], body="…")`, up to 50 per call; one receipt line per recipient
- "what did they answer?" → `twilio(op="messages", from_="+33<them>")` (and `to="+33<them>"` for what we sent)
- every op of `twilio_act` takes `dry_run=true`: same checks, nothing sent

## note — what can mislead

- ⚠️ **a number is billed for the whole month when bought**, and releasing it refunds nothing: `remove_number` is irreversible, the number may never come back
- ⚠️ **number types do not all text**: in France, a mobile number (+33 6/7) is for person-to-person SMS; automated or business sending goes through a +33 9 39 platform number or an alphanumeric sender name (which cannot be replied to). Each search result says its capabilities and `address_requirements`
- many countries refuse a purchase without an approved **regulatory bundle** (`bundle_sid`) in your organization's name, created in the Twilio Console
- no content rule is added by oto: no opt-out mention, no sending hours. What Twilio or the carriers refuse comes back as their error
- inbound calls and voicemail are not handled by oto yet on your OWN account: inbound SMS and recordings are read back from it. On numbers bought from the platform, inbound calls ring the Phone page and go to voicemail
- on numbers bought from the platform an agent only reads `numbers`, `messages`, `calls` and acts with `send_sms` / `call`: buying, releasing and listening to recordings happen on the Phone page
