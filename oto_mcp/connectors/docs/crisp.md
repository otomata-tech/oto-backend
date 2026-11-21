## prerequisite — a crisp website token and the website id

a **workspace owner** in Crisp generates a website token in Settings → Workspace Settings → Advanced configuration → *API Token*, and pastes its **identifier** and its **key** into oto, with the **website ID** read in Settings → Workspace Settings → Setup Instructions. Reference: [Crisp REST API authentication](https://docs.crisp.chat/guides/rest-api/authentication/).
- ⚠️ **the key is shown only ONCE**, when the token is generated; rolling the token revokes the previous one
- a token reaches only the workspace it was generated for: the website ID must be that workspace's
- byo-only: no shared oto key — these are your customers' conversations, each organization sets its own
- two tools: `crisp` reads, `crisp_act` writes and sends — your MCP client asks you to confirm `crisp_act`; nothing deletes
- the "test connection" button reads the workspace: no side effect

## usage — conversations, messages, contacts, knowledge base

- "the conversations updated since Monday" → `crisp(op="conversations", date_start="2026-11-02T00:00:00Z", order="updated", per_page=50)`
- "the unresolved conversations of this inbox" → `crisp(op="inboxes")`, then `crisp(op="conversations", inbox_id="…", resolved=false)`
- "what was said in this conversation" → `crisp(op="messages", session_id="session_…")`, then `timestamp_before=<oldest timestamp received>` for older messages
- "the conversations about a delivery delay" → `crisp(op="search_conversations", query="delivery delay")`
- "this contact and its conversations" → `crisp(op="person", people_id="<id or email>")`, `crisp(op="person_conversations", people_id="…")`
- "the help article on refunds" → `crisp(op="helpdesk_tree", locale="en", content_type="articles", search_title="refund")`, then `crisp(op="helpdesk_content", locale="en", content_type="articles", tree_path="…")`

## usage — writing and replying (`crisp_act`)

⚠️ every write acts on **real conversations of real people** (patients included), and **a message sent cannot be taken back**.

- "leave a note for the team on this conversation" → `crisp_act(op="add_note", session_id="session_…", text="…")` — the contact never sees it
- "resolve this conversation" → `crisp_act(op="set_state", session_id="…", state="resolved")` (pending, unresolved, resolved)
- "tag this conversation callback" → `crisp_act(op="add_segments", session_id="…", segments=["callback"])`; `remove_segments` to untag; `people_id` instead of `session_id` for a contact — the other segments are kept, and the answer is the list Crisp holds after the change
- "fix this contact's phone" → `crisp_act(op="update_person", people_id="…", phone="+33…")`; the details a conversation carries → `op="update_conversation"`; custom data → `op="update_person_data", data={…}` (merged with the keys already set)
- "reply to the patient" → `crisp_act(op="send_message", session_id="…", text="…")` returns a **preview only** (channel, contact, time of the contact's last message): nothing is sent until the same call comes with `confirm_send=true`, once the person approved the text

## note — personal data, channels, limits

- ⚠️ **conversations, messages and contacts may carry personal or health data**: read only what the task needs, and do not look a person up by name or phone number unless the task requires it
- the channel of a conversation is `meta.origin` (chat, email or a urn): the list has no channel filter, an inbox whose conditions gather a channel does the job
- no field says whether a channel's messaging window (such as WhatsApp's 24 hours) is open: the contact's last message is the latest one with `from` user; Crisp's API reference says nothing either about a message sent once the window is closed (no refusal code, no template), so a reply to a WhatsApp contact silent for more than 24 hours may not be delivered
- lists give neither a total nor a next page: page until a page comes back shorter than `per_page`
- a website token is limited to **10,000 requests per day**; a plan that does not include a resource answers 402
