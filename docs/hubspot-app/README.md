# The instance's HubSpot apps — "Connect HubSpot" in one click

`auth/hubspot.py` runs the consent; this folder is the HubSpot side of it: **two**
Developer Platform projects an operator uploads once per instance. Nothing here is a
secret.

| Folder | Used when the user connects… | `isUserLevel` | Settings it fills |
|---|---|---|---|
| `org/` | for the org or a team (admin / team lead) | `false` | `client_id`, `client_secret` |
| `personal/` | for themselves | `true` | `user_client_id`, `user_client_secret` |

Why two: HubSpot fixes `isUserLevel` when an app is created. The personal app makes
every call run **as the HubSpot user who connected** (their HubSpot permissions,
"[User] via <app>" in HubSpot's audit log), and the records oto creates through it
default to them as owner. The org app is installed once per portal and shared, like a
pasted key.

## What to fill before uploading

Each `src/app/app-hsmeta.json` carries placeholders (`<…>`): the app name the instance
shows, its support email and pages, and the **redirect URL**:
`https://<public base URL>/api/hubspot/oauth/callback` — the SAME for both apps (the
signed state says which app a return belongs to). HubSpot matches it byte for byte —
read it from the instance (`oauth_flow.redirect_uri`, i.e. `config.public_base_url()`),
never by hand. List the preprod host too.

⚠️ **`requiredScopes` and `optionalScopes` are those of `auth/hubspot.SCOPES_BY_APP`**
(derived from `tools/hubspot_scopes.SCOPES`). `tests/test_hubspot_oauth.py` keeps each
file equal to its app's lists: change the scopes in the code, regenerate here, upload.
Changing a REQUIRED scope after the first install makes every connection of that app
reconnect. The personal app requires less on purpose: a user can install it only if
their own HubSpot permissions cover every required scope.

⚠️ **`isUserLevel` cannot be changed later**: HubSpot does not migrate an app between
levels — a new app is needed and every connection reconnects.

## Steps

1. In a HubSpot developer account owned by the instance's organisation (not a
   person's): `hs project upload` from `org/`, then from `personal/` (HubSpot CLI ≥ 8.15).
2. Auth tab of each app → copy its client id and secret into the instance:
   `oto_admin_connector_setting(op="set", connector="hubspot", key="client_id", value=…)`
   and `key="client_secret"` for the org app; `key="user_client_id"` and
   `key="user_client_secret"` for the personal app.
3. The connector card shows **Connect HubSpot** on the tabs whose app is set
   (`connect.app_ready_scopes`: `member` for the personal app, `org`/`group` for the org
   app). A tab without its app keeps the pasted-key form alone.
4. Until an app is listed on the Marketplace, HubSpot caps it at **25** installs
   (developer test accounts excepted), per app. Listing needs 3 active installs outside
   the publishing organisation.
