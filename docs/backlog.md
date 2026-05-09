# Backlog

Issues GitHub : https://github.com/otomata-tech/oto-mcp/issues

## Org-gating sur l'auth Logto

L'auth Logto valide aujourd'hui n'importe quel user du tenant. Pour
restreindre à une organisation, sous-classer `JWTVerifier` et vérifier un
claim `organizations` (pattern `isOrgMember` dans GR :
`/data/missions/generations-renouvelables/server/src/services/logto.ts`).

Pas urgent — single-tenant + invitation manuelle suffit pour l'instant.

## CWS publication de l'extension

Le paquet `extension/dist/oto-companion-1.0.0.zip` est prêt + listing copy +
4 screenshots dans `extension/dist/cws-screenshots/`. Reste à uploader sur
le devconsole. Au retour : récupérer l'extension-ID assigné par CWS,
remplacer le redirect URI Logto + `customClientMetadata.corsAllowedOrigins`
de l'app `Oto Companion (Chrome Extension)`, puis remplacer le wording
"installation unpacked" par un lien Web Store dans `AccountView.vue` et
`McpView.vue` côté oto.ninja.

## Futurs connecteurs

Tools déjà ajoutés : SIRENE, Serper (web+news), Hunter, LinkedIn (5), WhatsApp (3).

À envisager (par ordre de pertinence prospection) :
- `oto.tools.serper` — `serper_maps`, `serper_scrape` (manquants vs CLI)
- `oto.tools.browser.pappers` — bilans détaillés (browser, dépend extension cookie capture, cf. `extension/`)
- `oto.tools.browser.crunchbase` — funding signals (browser)
- `oto.tools.attio` / `oto.tools.folk` — CRM writes, attendent un consentement scoped par user
- `oto.tools.gmail` — read-only, attend consentement par user

Règle : **read-only par défaut**, les writes attendent un scope OIDC explicite.
