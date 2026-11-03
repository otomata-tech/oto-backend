# Security

Security posture of **oto-backend** as deployed by Otomata, written to be read by people and by agents analysing this repository. Every claim points at code, config or a public document, so it can be checked rather than trusted.

Two rules govern what is written here. **Facts are derived from the code that is served and the infrastructure that runs it**, never from an intention — paths below are real paths in this repo. And **work in progress is marked as such**, dated, with a reference: a control that is designed but not yet active is in [Known limitations](#10-known-limitations), not in the section it will eventually belong to.

Last reviewed: 2026-09-24.

## 1. Scope

`oto-backend` is one Python service exposing two faces over the same core: an **MCP server** (Streamable HTTP) at `/mcp`, and a **REST API** under `/api/*`. It holds the credential vault, organizations, guides and the call journal. Connector clients live in a separate library, `oto-core`, pinned to an exact git tag in `pyproject.toml`.

**Hosted deployment.** Production runs on a Scaleway instance in `fr-par` (Paris, France), dedicated to the oto platform since 2026-06-11. Customer data — managed PostgreSQL, object storage, secrets vault, transactional email — is hosted in the European Union. Production endpoint is `https://mcp.oto.cx/mcp`; `mcp.oto.ninja` serves **pre-production**, not production.

Public ingress to the instance is **ports 80 and 443 only**; SSH is not exposed to the internet (verified 2026-08-27) and administrative access goes through an identity-gated tunnel. Deployment is pull-based via a self-hosted runner whose privilege is a single `sudo` entry on one deploy script — there is no inbound SSH deploy path and no deploy key with shell access. TLS is terminated by Caddy on the instance.

**Self-hosting.** The service is configured entirely through environment variables and ships a `Dockerfile`. Nothing here about the *hosted* deployment applies to a self-hosted instance. Machine-level detail (addresses, secret and instance identifiers, systemd units, runbooks) is deliberately kept private and is not published here.

## 2. Reporting a vulnerability

Email **security@otomata.tech**. Please do **not** open a public GitHub issue for a suspected vulnerability.

- Expected first response: within 5 business days. Otomata is a very small team. There is **no bug bounty** and no paid reward programme.
- Include enough detail to reproduce, and allow a reasonable period to ship a fix before public disclosure.
- Reports concerning a *customer's* data or account should go through that customer: under the DPA, the customer is the controller and we are the processor.

## 3. Supported versions

A **single trunk**, `main`, and no long-lived release branches.

| Ref | Environment | How it ships |
|---|---|---|
| `main` | pre-production | every push deploys automatically |
| tag `vX.Y.Z` | production | explicit act: the new instance must answer healthy before it receives traffic; failure means no switch |

**Only the latest production tag is supported.** There are no backports; a fix ships as a new tag. Tags matching `v*` are **immutable**: an active repository ruleset (`prod-tags-immutable`) blocks deletion, update and non-fast-forward on `refs/tags/v*`. The deploy script refuses to run without an explicit tag argument, so production is never deployed from a branch. The release log is maintained by hand in `otomata-tech/oto` (`RELEASES.md`), never generated from commits.

## 4. Authentication and authorization

**Identity.** Authentication is delegated to a **self-hosted Logto** instance. Tokens are issued and signed by Logto; this service only verifies them — asymmetric **ES384** against Logto's JWKS, with the primary issuer read from the environment and never from the database. On 401 the response carries an RFC 9728 `WWW-Authenticate` pointing at `/.well-known/oauth-protected-resource/mcp`.

**Client registration.** Its RFC 9728 protected-resource metadata (`oto_mcp/auth/decouverte.py`) names the Logto issuer itself as the authorization server; MCP clients register there through Client ID Metadata Documents (CIMD), so an instance requires a Logto with CIMD enabled (1.44 or later). For clients without CIMD, `oto_mcp/auth/inscription_dynamique.py` serves, under the issuer's domain, Logto's own authorization-server metadata plus a `registration_endpoint`, and an RFC 7591 registration that creates one Logto application per client type and redirect-URI set: redirect URIs come from a closed list (exact match, or one fixed prefix), prefix-admitted registrations are capped, and any Logto failure is an error, never a fallback. No token is minted here. The anonymous OAuth shim of published projects (`oto_mcp/auth/anon.py`) issues no privilege and accepts redirect URIs only from a fixed set of known MCP client hosts (`_redirect_ok`). Connector OAuth flows use PKCE S256 with the verifier carried in an HMAC-signed `state` (`oto_mcp/auth/pkce.py`).

**MFA.** An organization can require multi-factor authentication (`orgs.require_mfa`). Enabling it provisions a mirror Logto organization with `isMfaRequired=true` and syncs members by subject id; Logto then enforces the second factor at ordinary login. **No fail-open**: enabling provisions *before* setting the flag and raises if Logto fails, so an organization is never told MFA is active when it is not (`oto_mcp/mfa_mirror.py`).

**API tokens.** Long-lived tokens are `oto_` + 256 bits from `secrets.token_urlsafe(32)`. Only a **SHA-256 hash** is stored (`oto_mcp/db/tokens.py`); the plaintext is returned once at creation and cannot be recovered.

- **A token cannot mint another token.** The six token-management routes set `allow_api_token=False` and answer `403 api_token_forbidden`: only an interactive Logto session can issue or delete a token, so a leaked token cannot make its own access self-sustaining.
- **Scopes are supported and enforced** (`oto_mcp/auth/token_scopes.py`). A scoped token is **deny-by-default**: an explicit allowlist names the only routes it can reach, so a route added tomorrow is refused unless it is added to that list. Scopes grant read or read/write on named datastore tables and read on named projects; they never grant governance (create, delete, rename, share) and never reach identity, connector or capability routes. Out of scope answers `403 token_scope_forbidden`.
- **A scoped token is refused on the MCP face**, fail-closed: its gate reasons on HTTP method and path, which an MCP call does not have, and accepting it would silently widen it.
- ⚠️ A token created **without** `scopes` keeps the full authority of its owner, and self-service tokens carry **no expiry**. See [Known limitations](#10-known-limitations).

**Roles.** Resolved in one place (`oto_mcp/roles.py`, `oto_mcp/access/scope.py`): the platform tier is `member` → `admin` (operational supervision, no mass escalation) → `super_admin`; the organization tier escalates downward, `platform_admin ⊇ org_admin ⊇ group_admin ⊇ member`.

**What a `super_admin` can and cannot do**, stated plainly, because it is a deliberate design decision (ADR 0012) and not an oversight:

- **Can** administer any organization and any group, manage platform roles and platform keys, and **govern** a third party's resources — re-share, list, revoke, delete — *without reading them*.
- **Cannot** read the *content* of a personal resource by escalation alone: content access is owner-match or explicit grant only (`oto_mcp/ownership.py`, `can_access` vs `can_govern`). The operator read path is a separate, audited "view-as": REST-only, `GET`-only (mutations answer `403 view_as_read_only`), and absent from the MCP face — there is no impersonation inside an LLM context.
- **An organization admin can "view as" a member of their own organization**, read-only and **bounded to that organization** (oto#270): same middleware, same read-only guard, same audit column. The view requires the organization (`X-Oto-Org`), a real `org_admin` role in it, and a target who is a real member and not a platform operator. It never writes (`403 view_as_write_forbidden` even with the acceptance header), and it is **deny-by-default**: only a closed list of reads passes, `/api/orgs/{id}` and `/api/groups/{id}` are pinned to that organization, and anything else answers `403 view_as_hors_org` — account-wide lists (API tokens, connector-account grants, tables shared to the person, model subscriptions, connector instances, billing) included. Inside the view, the ownership seam shows only what the member sees in that organization: never another organization's resource, never a personal share received, a personal resource only when it belongs to that organization, and never a secret value.
- **Cannot obtain a secret through any API.** Since 2026-08-31 this holds for *everyone*, owners included: a credential read returns only the fields a connector declares **non-secret** (a base URL, a region, an email). The value of a secret field is never returned — its key is **absent** from the body, replaced by whether one is set, when, by whom, and a four-character non-reversible fingerprint bound to that vault row; asking for the value answers `403 secret_never_revealed`. Organization and platform keys never returned their secret either; a platform key is *lent*, never revealed. An operator with shell access to the production instance can decrypt a credential ad hoc — that is a documented operational procedure, and the honest bound on the above.

**Connector authorization.** Who may use a connector is decided **at call time** by credential resolution: a key resolves only for the principals of the level it was set at — a personal key for its owner alone, a team key for that team's members, an organization key for the whole organization. Restricting a connector means setting its key at the right level; there is no separate per-member access list on top (removed on 2026-09-24). Connector activation is deny-by-default — `enabled: null` means off, not undetermined — and it is also checked **at call time** (`oto_mcp/connectors/activation_gate.py`). Once the call context is set (`_org=`, `_group=`, the run's organization), a tool whose connector is off for that context — platform master switch off with no organization override, organization override off, or a cut by the active team — is refused `connector_disabled`, naming the connector, the organization and who can reopen it. The same gate runs on a direct call and inside `oto_call`, which bypasses only the display filter (oto-backend#1064: before this gate, `oto_call` served such a tool). Visibility masking above these gates **fails open by design** so a database blip cannot black out the surface; the call-time checks behind it fail closed.

**Credential cascade.** One walker (`oto_mcp/access/cascade.py`) resolves in order: member (scoped to the active organization) → personal cross-org instance → group → organization → platform key (metered, and only for providers declaring a platform auth mode; for the free-tier connectors below, reachable by **any** account without a grant). A named account found nowhere raises, never silently falls back to a platform key.

**Free tier — what a brand-new account can call** (stated 2026-09-23, oto-backend#804). Sign-up is self-serve and open. An account with no grant, no option and no subscription already resolves the platform key of some connectors, within a daily cap. **This is intended**: it is how a new account discovers the platform. It is not a gap, and it must not be described as one that is closed. A connector is on the free tier when it declares `platform_key_open` in the registry (`oto_mcp/providers/`) **and** its platform key is stored in `open` sharing mode with no named grantee (`oto_mcp/access/platform_grant.py`). The second condition is database state, not code: read it per connector as `open_tier` on `GET /api/admin/connectors/{provider}/platform-access`.

| connector | default daily cap, per user |
|---|---|
| `apollo` | 20 |
| `hunter` | 5 |
| `kaspr` | 5 |
| `reddit` | 100 |
| `serper` | 200 |
| `serpapi` | 200 |
| `searchapi` | 200 |
| `tavily` | 100 |
| `sirene` | no cap |
| `unipile` (hosted messaging, and the channels that borrow its key) | no cap, but every use of the platform key requires the organization's paid option `unipile` |

The cap counts per user and per key (`usage_today`). It is the key's own `rate_limit` when one is stored, otherwise `OTO_MCP_QUOTA_<PROVIDER>_DAILY`, otherwise the registry default above; an organization holding the declared right `platform_unmetered` has no cap (`oto_mcp/access/resolve.py`, `_win_quota`). A paid option (`unipile`) is a live declared right of the organization (`org_entitlements`, ADR 0070 §7), re-read on **every** use of the platform key (`quotas.exiger_option_payante`), not only when an account is connected; a right granted to a single person no longer opens it. **Installing a connector is not a gate**: it is a display filter, and `oto_call` reaches a connector that is exposed but not installed. For connectors moved to the grant chain (`grants_chain.CHAIN_CONNECTORS`), an account whose grant was revoked is refused, with no fall-back to the free tier. The table is held to the registry by `tests/test_securite_palier_gratuit.py`.

**Shared projects do not lend the owner's keys** (since 2026-09-23, oto-backend#480). Opening a project makes its owning organization the call's context. Someone the project is shared with — a person, a team or a whole organization, the same rule for all three — who is **not a member** of that organization works with **their own keys**: the organization's shared keys, its platform allowances and its plan are not reachable (`oto_mcp/access/heritage.py`). The owner's keys are lent only if the sharer declares it on the share (`credentials="inherit"`), only if the sharer is a member of that organization, re-checked on every call, and revocable. Before this date the organization rung had no membership check, so a non-member recipient acting inside the project resolved the organization's keys.

**Acting organization.** One seam, `access.current_org(sub)`, resolves session → consultation → home, and always returns the **requester's** context. The REST consultation header is applied by middleware only *after* membership validation (anti-IDOR). Content listings scope on the active organization, never on the union of the actor's organizations — a tripwire test exists because conflating the two caused a cross-organization visibility leak on 2026-06-30, since fixed.

## 5. Secrets and credentials

Connector credentials are stored **encrypted at rest** in `connector_credentials` (`oto_mcp/crypto.py`, `oto_mcp/credentials_store.py`).

- **Algorithm**: AES-256-GCM. Envelope = `key_ref(1B) ‖ nonce(12B) ‖ ciphertext+tag`, base64.
- **Key custody**: the master key lives **outside the database**, in the process environment (`OTO_MCP_MASTER_KEY`, 32 bytes). In the hosted deployment it is fetched from **Scaleway Secret Manager at boot** and never written to disk in clear; the only on-disk credential for that fetch is a scoped read-only IAM key.
- **Mandatory**: `encrypt`/`decrypt` raise when the key is absent. No plaintext column and no plaintext fallback path exist — a database dump is ciphertext only.
- **AAD binds each ciphertext to its own row**: `connector_credentials:{entity_type}:{entity_id}:{connector}[:{account}]`. A blob copied to another connector, entity or organization does not decrypt, so re-parenting a row requires decrypt-and-re-encrypt, never an `UPDATE`.
- **Loud failure**: a wrong key yields an authentication-tag failure, never a silent bad decryption. Decryption is just-in-time inside resolution and **is never logged**; presence checks never decrypt.

**Credentials are scoped to `(subject, organization)`** (ADR 0033): a member credential is stored under `entity_id = "{org_id}:{sub}"`, so it is *cryptographically* bound to the organization it was created in. The same person's key in another organization is a different row that will not decrypt.

**Secrets are never MCP tool arguments** — a raw secret in a tool argument would transit through the model's context. Credentials are posted only through the authenticated REST/dashboard surface, and a partial update is merged server-side from the vault, so the existing secret never travels back over the wire.

⚠️ **One master key, not one per organization.** Per-organization key material (KEK/DEK envelope) is specified in **ADR 0039 and explicitly deferred**. Consequences, as that ADR states them: rotation is all-or-nothing, there is no per-organization cryptographic erasure, and a leaked master key would expose the whole vault. The `key_ref` byte exists for a future key ring but is never dispatched today — **there is no key rotation**.

## 6. Data

**At rest.** The database backing this service is a managed PostgreSQL instance with **volume-level encryption at rest** enabled, in Scaleway `fr-par`. The application's database role is scoped to its own database and is not a superuser. This statement is bounded to the oto platform's own databases and must not be extended to other Otomata projects.

⚠️ Production and pre-production **share that database**. Environments are separated by token audience, not by data: a pre-production write is a production write. This is a deliberate trade — a canary on real data — and it is why pre-production is treated as production for access purposes.

**Call journal.** Every tool call is recorded in `tool_calls`: tool name, caller subject and email, **arguments (truncated at write)**, outcome, duration, and correlation ids (session, run, organization, client application). **Response bodies are not recorded.** Retention is set per deployment (`OTO_JOURNAL_RETENTION_DAYS`); the hosted policy keeps **30 days online** (90 days from 2026-08-27 to 2026-10-22), after which each closed month is exported as a compressed CSV to **private** object storage, readable on request, and deleted from the database, by a daily job running outside the server process (`deploy/archive_tool_calls.py`). Deletion is authorised only after the uploaded archive has been re-read and its record count reconciled against the database. Run boundary events are **exempt and kept indefinitely** — they are the source of truth for execution history, not merely logs.

Reads of the journal are tiered: a member sees their own activity in the active organization; an `org_admin` sees everything emitted under their organization, including per-call arguments; the platform tier sees everything. Identifiers are sequential and therefore guessable, so a cross-organization lookup returns the same `404` as a nonexistent one.

**Response redaction.** A middleware applies the active organization's redaction policy to the **result of every MCP tool call**, by leaf key name, recursively (`oto_mcp/middleware/field_redaction.py`). It is **fail-closed**: if a policy exists and cannot be applied, output is withheld rather than returned raw. Both response channels are re-emitted from the redacted value, and the universal dispatch path re-applies the same logic so it cannot be routed around. **Nothing is redacted by default** — the server default set is empty and an organization opts in. ⚠️ The **REST face is not covered** by this middleware.

**Error responses.** Every tool exception is rewritten into a uniform, scrubbed envelope — no stack trace, no internal route, no technical identifier — carrying a stable code, a retryable flag and a hint (`oto_mcp/middleware/error_envelope.py`). Unknown request fields are **refused and named** (`400 unknown_fields`), never silently ignored.

**Error tracking.** Sentry is optional and gated on a DSN. When active it runs with `send_default_pii=False` — no IP addresses, cookies or headers auto-collected — and **tool call arguments are never attached to an event**. What is attached is the tool name, the client application id and the opaque Logto subject id (`oto_mcp/sentry_setup.py`). The project is hosted in the EU region.

**Other data-path controls.** Upload URLs are HMAC tokens sealing `(subject, organization, target)`, short-lived and single-use, with the target's authorization re-applied server-side.

- **The token payload is signed, not encrypted, and this is accepted** (oto-backend#562, decided 2026-09-23). Anyone holding the link can base64-decode it and read the issuing account's subject id, the organization id, the target descriptor (kind, project or document id, file name, title, declared type, datastore id) and the expiry. None of it is a secret: it grants nothing without the signature, which is verified in constant time before any body is read; the subject and organization ids are opaque identifiers that authenticate nothing on their own; and the target is re-authorized server-side on receipt. The token lives **15 minutes** and is **consumed on first use**. Whoever mints a link is handing it to a third party on purpose — it should not be minted with a title or file name that must stay confidential.
- **The body is bounded while it is read, not after** (#562): a declared `Content-Length` above the ceiling (25 MB by default) is refused without reading, and the stream stops at the first byte over it.
- **A stored project file is served under a type decided from its content, never from its declaration alone** (#562, `media_store.type_servi`). A recognized format signature wins; declared passive UTF-8 text (plain, CSV, Markdown, JSON) is kept if it carries no active markup; everything else — unrecognized, or active (HTML, SVG, XML, JavaScript, whatever type was declared) — is written as `application/octet-stream` with `Content-Disposition: attachment`, so a browser downloads it and never renders it. Nothing is refused. Objects stored before 2026-09-23 keep the type they were written with.

Public project sharing is zero-knowledge: the dashboard encrypts client-side and the decryption key travels in the URL fragment, never reaching the server. File extraction is bounded against decompression bombs by reading the archive catalogue rather than the archive. The REST surface answers with credentials only for an explicit origin allowlist configured per deployment; there is no wildcard origin on any authenticated route.

**HTTP security headers** (oto-backend#565). The split is decided once: what depends on the page is set by this service, next to the code that writes the page; what holds for a whole domain — HSTS first — belongs to the reverse proxy, outside this repository. The pages this service renders itself — the shared-document page `/p/d/<token>` in all three variants (HTML, Markdown, JSON) and its "not found" page, the browsable UI of a published project, and the signed-upload form — are served with `Referrer-Policy: no-referrer`, a `Cache-Control` that no shared cache may keep (`private`, or `no-store` for the "not found" page — never `public`), and a Content-Security-Policy listing exactly what that page loads (`oto_mcp/entetes_securite.py`): `default-src 'none'`, `frame-ancestors 'none'`, `base-uri 'none'`, then fonts, images and scripts as each page needs them. A test renders each page and checks it against its policy, so a resource added without its policy fails the suite rather than the reader's browser. The shared-document page runs **no script at all**, so its policy is a second line behind the escaping; the upload form's single inline script is allowed **by its hash**. ⚠️ The published-project UI still uses inline event handlers and therefore `script-src 'unsafe-inline'`: there the policy blocks third-party scripts, outbound requests, forms and framing, but **not** an injected inline script — escaping remains the only defence against that.

**Connector egress.** Seven connectors take their destination from the credential — an address chosen by an administrator of the client organization, not by the calling agent. Those destinations are checked **at resolution**, not on the shape of the string: every address a host resolves to must be global, so a public-looking domain name that points inside is refused like a literal one, and a host resolving to both a public and an internal address is refused outright (`oto_mcp/egress.py`). Legitimate internal destinations — a bridge hosted on the loopback — exist only as **named exceptions declared per deployment** in `OTO_EGRESS_ALLOW`, keyed by literal address *and* port so that one exception opens one service rather than the whole machine. The default is empty and an undeclared internal destination fails loudly, naming the variable and the format. Two windows stay open and are named: DNS rebinding between the check and the connection, and a 3xx redirect into the internal network, since the check holds where the client is built rather than in the transport. At the network layer the service unit additionally denies egress to link-local addresses — link-local only, not a general private-range block.

## 7. Tenant isolation

A **tenant** is the identity layer above organizations (ADR 0052): its own issuer, domains and organizations. The first third-party tenant has been served since 2026-08-13.

The mechanism is **subject qualification at a single entry point**: a subject from a third-party tenant becomes `"<slug>:<sub>"`, while the primary tenant's subjects stay bare. Downstream the subject is an opaque string that is never parsed (`oto_mcp/tenancy.py`). Because the vault's AAD derives from the subject, this yields **cryptographic partitioning between tenants**, with no re-encryption of existing rows. Qualification is the *last* gesture: a token rejected on audience or issuer never produces a subject at all. A forged token claiming another tenant's issuer is rejected by that tenant's own verifier — signature against its JWKS and a byte-for-byte issuer match. There is no identity federation between tenants, by design.

⚠️ **The isolation is real but not complete, and the gaps are named.** Strict per-tenant token audience is implemented but **inert**: the strict check in `oto_mcp/server.py` cannot bite while a transitional environment variable still publishes an audience globally, so an audience accepted for one host is not yet prevented from being presented at another. Separately, platform administration surfaces — user and organization lists, monitoring, exports — have **no tenant axis** and mix populations without a marker. Both are tracked below.

## 8. Sub-processors

The maintained list is the trust center: **<https://trust.oto.cx>**. It currently names Cloudflare, FullEnrich, Hunter, Kaspr, PostHog, Scaleway, Sentry, Unipile, and a payment provider. The Data Processing Agreement served at <https://oto.cx/dpa> (version 2.0, 2026-07-10) names a partly different set: Scaleway, Logto, PostHog, Sentry, Anthropic, a payment provider, and optional connector services.

⚠️ **These two published lists do not currently agree with each other, nor with this code, on the identity of the payment provider.** The payment integration in this repository is Mollie (switched 2026-07-24); the trust center and the DPA each still name an earlier candidate. Reconciling them — along with stale pricing in the terms — is tracked in [oto-websites#74](https://github.com/otomata-tech/oto-websites/issues/74). Until it lands, treat the trust center as the list under active maintenance, and this paragraph as the correction.

**No third-party certification.** The trust center publishes **no audit report and no compliance framework**: there is no SOC 2 and no ISO 27001 certification today. Internal policies — information security, access control, secure development, data retention, sub-processor management, backup and continuity, incident response, GDPR Art. 33-34 breach notification — are listed there and released on request under NDA.

## 9. Development practices

- **Pull requests are required** on `main`; direct pushes, force-pushes and branch deletion are blocked.
- **A green `test` check is required to merge**, and both deploy jobs depend on it, so a red test blocks the merge *and* the deployment. CI runs the full `pytest` suite plus a compile pass on Python 3.10 — the interpreter that actually runs in production, which is older and stricter than the one the suite runs on.
- **Silence lint** (`scripts/lint_silences.py`, exercised by `tests/test_no_silent_except.py`): a broad `except` must re-raise, log, or return a *named* refusal. The only escape hatch is `# noqa: SILENT — <reason>`, and the reason is mandatory. The rule exists because a silent fail-open combined with stubbed tests once hid a broken authorization function; the ten most dangerous sites were fixed on 2026-08-27, and the remaining annotated ones are declared debt, not a permit.
- **Structural tripwires** guard the properties this document describes — the organization seam, the credential scope, redaction and middleware order, blocking I/O in the single event loop, and the reserved call-context argument names.
- **Dependencies** are updated weekly by Dependabot. `oto-core` is pinned to an exact git tag and bumped by hand, so a deployed version is a reproducible coordinate.
- **External contributions** require signing the CLA (`CLA.md` in `otomata-tech/oto`), enforced by a workflow on every pull request; that workflow reads pull request metadata only and never checks out fork code with repository privileges. Workflow runs on fork pull requests require manual approval for each new commit.
- Licensed **MIT** (`LICENSE`).

⚠️ Honest limits of the above: review **approval** is not required (the approving-review count is zero), branch protection does **not** apply to repository administrators, and commit signature verification is not enforced. This is a small-team project and the controls reflect that.

## 10. Known limitations

Open, dated, referenced. This list is the point of the document.

| Limitation | State | Reference |
|---|---|---|
| One flat master key: rotation is all-or-nothing, no per-organization cryptographic erasure, and no key rotation is implemented at all | Deferred by decision | ADR 0039 |
| Strict per-tenant token audience implemented but inert while a transitional global audience list is in place | In progress | ADR 0052, lot L3 |
| Platform administration surfaces have no tenant axis — user/organization lists, monitoring and exports mix tenant populations without a marker | Open | [oto-backend#442](https://github.com/otomata-tech/oto-backend/issues/442) |
| Tokens created from the dashboard are unscoped — full authority of their owner over the whole organization — and this is not announced at creation. Scopes must currently be passed explicitly to the API | Open | [oto-backend#514](https://github.com/otomata-tech/oto-backend/issues/514) |
| Revoking a token deletes its row, leaving no trace of who held it, when it was cut, or why — unlike connector shares, which keep a revocation date | Open | [oto-backend#523](https://github.com/otomata-tech/oto-backend/issues/523) |
| Self-service tokens and resource shares have no expiry date, so neither expiration nor periodic review is possible | Open | [oto#39](https://github.com/otomata-tech/oto/issues/39) |
| Cross-organization project sharing: a non-member beneficiary can resolve the owning organization's connector credentials — the organization rung of the cascade lacks a membership check | Open | [oto-backend#480](https://github.com/otomata-tech/oto-backend/issues/480) |
| Published sub-processor lists disagree with each other and with the code on the payment provider; terms carry stale pricing | Open | [oto-websites#74](https://github.com/otomata-tech/oto-websites/issues/74) |
| No per-session revocation: a stolen token stays valid until expiry, and blocking an account does not close its open sessions. No refresh-token rotation per application, and no step-up authentication — the identity provider does not expose the authentication level in the token | Open | internal security epic |
| Account recovery is email-based and therefore effectively single-factor. Organization MFA is enforced at login, but the recovery paths have not been audited against it — do not read organization MFA as a complete second factor | Open | internal security epic |
| HSTS is not sent on the production domains (`mcp.oto.cx`, `manage.oto.cx`, `dashboard.oto.cx`, measured 2026-09-24): it belongs to the reverse proxy, whose configuration lives outside this repository. Pre-production (`*.oto.ninja`) receives it from its CDN | Open — proxy configuration | §6 |
| The published-project UI allows inline scripts (`'unsafe-inline'`, inline event handlers): its Content-Security-Policy does not stop an injected inline script there | Open | §6 |
| Redaction covers the MCP face only; the REST face shares no rendering code with it and is not redacted | Gap acknowledged | §6 |
| For Google OAuth connections, the short-lived access token is stored in cleartext metadata; only the refresh token is encrypted | Open | §5 |
| The dynamic client registration facade returns a client id even when the identity provider's management API is unreachable | Fail-open, deliberate | §4 |
| Connector visibility gating fails open on infrastructure error; the call-time resolution behind it fails closed | By design | ADR 0053 |
| Production and pre-production share one database — a pre-production write is a production write | Accepted trade-off | ADR 0040 |
| Disk encryption is the hosting provider's baseline only: no guest-side full-disk encryption and no hardware attestation, so a snapshot exfiltrated through a compromised cloud account remains a risk | Accepted residual | ADR 0002 |
| Service bootstrap environment (database URL, identity-provider machine credentials, object storage and payment keys) is still read from an on-disk `.env`; only the vault master key is fetched from the secrets manager at boot | In progress | §5 |
| No third-party security audit or certification | Open | §8 |

ADRs referenced above are internal architecture decision records; their substance is summarised here and the records themselves are not public. The security epic is tracked in a private repository — ask at the address in §2 for its current state.
