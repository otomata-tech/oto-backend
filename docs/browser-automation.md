# Browser automation — substrat hébergé Browserbase (ADR 0026)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


⚠️ **Plus de browser in-process sur la box, plus de délégation à un conteneur
o-browser-full** (`OBROWSER_URL`/`RemoteBrowser` = 0 référence — jamais portée). Le
harnais browser server-side = **service navigateur HÉBERGÉ Browserbase**
(`oto_mcp/browserbase.py`, seam à sens unique ADR 0004), réutilisable par tout
connecteur d'**API privée cookie-bound**. État réel (2026-06-24) :

- **LinkedIn** : `tools/linkedin.py` **supprimé** — passe par **Unipile** (hébergé,
  connecteur `unipile`). Le browser LinkedIn local ne survit que dans oto-cli
  (fallback) et oto-core (`oto.tools.browser.linkedin/`).
- **Substrat `browserbase.py`** : Chrome HÉBERGÉ (off-box → anti-OOM) + **Context**
  per-user (profil persistant = la session loguée, = le credential coffre) + **Live
  View** pour le login interactif (l'user gère SSO/captcha/2FA — pas d'export de
  cookie, la session naît native, zéro `li_at` kill). Exécution = `run_fetch(ctx,
  method, path, body, *, base, app)` : ouvre une session éphémère sur le Context, charge
  une page `app` puis exécute un `fetch(base+path)` **same-origin** (la session vivante
  porte les cookies). `base`/`app` sont **propres au connecteur** (le substrat n'en
  hardcode aucun). Creds plateforme env `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID`.
- **Connexion = depuis le DASHBOARD** (voie produit, pas MCP) : bouton « Connecter »
  → Live View Browserbase **en iframe** ; l'user se logue ; « vérifier » persiste le
  Context. Servie en REST `POST /api/me/connectors/{name}/session/{start,finalize}`
  ET en MCP (`<name>_connect_start`/`_connect_status`) par **un seul corps de logique**
  (`browser_session.py`, seam : `start()` générique + `finalize()` avec **verify
  par-connecteur enregistré** — brevo=cookie `auth`, crunchbase=sonde API ; les tools
  MCP ne sont que de minces délégations). ⚠️ **Un connecteur browser-session s'enregistre
  avec une `login_url`** (`browser_session.register(name, verify, login_url=…)`) : `start()`
  amène la session **sur cette page** avant d'afficher la Live View (best-effort). Sans elle,
  la Live View reste sur `about:blank` (l'user ne sait pas où se loguer — vécu pennylaneged
  2026-07-01). **Sécu** : la session émise est liée au `sub`
  (`_PENDING`, anti-IDOR — `finalize` refuse un Context tiers) et **aucune exception
  brute n'est renvoyée** (l'URL CDP porte `?apiKey=…` → loggué, message propre). L'état
  (`configured` + `session_set_at`) sort dans `me.providers[name]` via `status_for`
  (les connecteurs `secret_kind="cookie"`) — ADR 0026 avait retiré `me.crunchbase` sans
  jamais câbler ce relais (UX cassée bout-en-bout, corrigé 2026-06-30). Déconnexion =
  DELETE générique `/api/settings/api-keys/{name}` (byo_user, plus de route dédiée).
- **Connecteurs sur le substrat** (tous deux : Live View ci-dessus, Context au coffre,
  family dérivée=`api`, plus aucun browser local) :
  - **`brevo`** (`tools/brevo.py`) — automations marketing via l'API privée
    `workflow-apis.brevo.com/v1` (cookie `auth` httpOnly). **Prouvé 200** le 2026-06-24.
  - **`crunchbase`** (`tools/crunchbase.py`) — fiches société/personne via l'API privée
    du frontend `www.crunchbase.com/v4/data` (schéma v4 sans `user_key` ; lookup
    `entities/organizations|people/{slug}` + cards `founders`/`raised_funding_rounds`,
    recherche via `autocompletes`). **Migré du scraping DOM in-process** (ADR 0026,
    `BROWSER_PROVIDERS` désormais vide, plus de `CrunchbaseClient` o-browser ni de Chrome
    sur la box). ⚠️ **Reste à smoke en live** (Browserbase + login crunchbase réel) :
    confirmer `field_ids`/`card_ids`/`collection_ids` et l'absence de header anti-CSRF.
  - **`pennylaneged`** (`tools/pennylaneged.py`, issue otomata-private#31) — GED (DMS)
    Pennylane via l'API interne de la SPA (`/companies/{cid}/dms/…`, CSRF tournant lu
    in-page à chaque appel via `browserbase.run_page_eval` — l'eval générique, `run_fetch`
    ne suffit pas). Upload = control plane seul (URL S3 présignée), les octets PUT **en
    local** (RGPD). **GED cible (une par client)** : `company_id` optionnel sur tous les
    tools, défaut = la société choisie via le **sélecteur d'identité générique** (ADR
    0024) — backend enregistré par `connector_identities.register()` (patron
    `browser_session`), identités = les sociétés du cabinet (`/crm/flow_companies`),
    sélection validée anti-binding (tree 200 sur LA session) puis mémorisée au `meta` du
    credential (`default_identity_id`/`default_identity_label`, exposés par `status_for`
    → picker de la carte dashboard sans louer de session ; `identities` au catalogue).
    ⚠️ **Reste à smoker en live** (login Pennylane réel + forme exacte de
    `flow_companies`).
- **Leçons empiriques (toujours valides)** : (1) un `httpx`/curl brut est **rejeté
  (403)** — transport obligatoirement **browser-driven** (`page.evaluate(fetch())`) ;
  (2) une session **ne se transplante pas** par export de cookie (le faux négatif « auth
  cookie missing » venait d'une extraction sur **profil déconnecté**) → login-en-place
  via Live View ; (3) capter/vérifier sur une session **vivante** (fetch sanity = 200).

## LinkedIn — cookies & isolation de session


⚠️ **Isolation de session (constaté 2026-06-04, issue #5 ouverte)** : injecter le
cookie `li_at` d'un user **côté serveur** (IP datacenter ≠ son IP) **déconnecte sa
propre session LinkedIn** (LinkedIn invalide/rotate le `li_at` partagé). Le vrai
Chrome règle l'empreinte TLS mais PAS ce partage de session. → l'outreach par un
user réel doit passer par une **session dédiée** (profil/VNC côté serveur, ou CLI
local sur son device), pas par son cookie injecté côté serveur. ✅ Le scraping
serveur est désormais **profil-only** (fallback cookie **supprimé**) et délégué au
conteneur (voir §Browser automation). #5 reste ouvert pour le pairing/CLI local.

Le couple `(li_at, user_agent)` est stocké par `sub` en PG. Le UA
matche le browser d'origine (capturé via `navigator.userAgent` au moment du
save) — sinon LinkedIn flag rapidement les sessions cookie/UA mismatch.

Si le user n'a rien configuré, les tools `linkedin_*` lèvent une `McpError`
qui pointe vers `https://app.oto.ninja/`.

Pour les non-tech : extension Chrome Oto Companion (repo `oto-app/extension/`,
MV3) qui capture le couple `(li_at, user_agent)` et le push automatiquement
via `POST /api/settings/linkedin` (auth Logto PKCE). Auto-resync via
`chrome.cookies.onChanged` quand LinkedIn rotate la session.
