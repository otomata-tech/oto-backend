## prerequisite — a crisp website token and the website id

a **workspace owner** in Crisp generates a website token in Settings → Workspace Settings → Advanced configuration → *API Token*, and pastes its **identifier** and its **key** into oto, with the **website ID** read in Settings → Workspace Settings → Setup Instructions. Reference: [Crisp REST API authentication](https://docs.crisp.chat/guides/rest-api/authentication/).
- ⚠️ **the key is shown only ONCE**, when the token is generated; rolling the token revokes the previous one
- a token reaches only the workspace it was generated for: the website ID must be that workspace's
- byo-only: no shared oto key — these are your customers' conversations, each organization sets its own
- read-only: the connector never sends a message or a note, never changes a conversation or a contact
- the "test connection" button reads the workspace: no side effect

## usage — conversations, messages, contacts, knowledge base

- "the conversations updated since Monday" → `crisp(op="conversations", date_start="2026-11-02T00:00:00Z", order="updated", per_page=50)`
- "the unresolved conversations of this inbox" → `crisp(op="inboxes")`, then `crisp(op="conversations", inbox_id="…", resolved=false)`
- "what was said in this conversation" → `crisp(op="messages", session_id="session_…")`, then `timestamp_before=<oldest timestamp received>` for older messages
- "the conversations about a delivery delay" → `crisp(op="search_conversations", query="delivery delay")`
- "this contact and its conversations" → `crisp(op="person", people_id="<id or email>")`, `crisp(op="person_conversations", people_id="…")`
- "the help article on refunds" → `crisp(op="helpdesk_tree", locale="en", content_type="articles", search_title="refund")`, then `crisp(op="helpdesk_content", locale="en", content_type="articles", tree_path="…")`

## note — personal data, channels, limits

- ⚠️ **conversations, messages and contacts may carry personal or health data**: read only what the task needs, and do not look a person up by name or phone number unless the task requires it
- the channel of a conversation is `meta.origin` (chat, email or a urn): the list has no channel filter, an inbox whose conditions gather a channel does the job
- no field says whether a channel's messaging window (such as WhatsApp's 24 hours) is open: the contact's last message is the latest one with `from` user
- lists give neither a total nor a next page: page until a page comes back shorter than `per_page`
- a website token is limited to **10,000 requests per day**; a plan that does not include a resource answers 402
