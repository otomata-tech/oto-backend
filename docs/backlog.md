# Backlog

Issues GitHub : https://github.com/otomata-tech/oto-mcp/issues

## Org-gating sur l'auth Logto

L'auth Logto valide aujourd'hui n'importe quel user du tenant. Pour
restreindre à une organisation, sous-classer `JWTVerifier` et vérifier un
claim `organizations` (pattern `isOrgMember` dans GR :
`/data/missions/generations-renouvelables/server/src/services/logto.ts`).

Pas urgent — single-tenant + invitation manuelle suffit pour l'instant.

## Futurs connecteurs

Tools déjà ajoutés : SIRENE, NAF, Serper (web+news), Hunter, LinkedIn (5).

À envisager (par ordre de pertinence prospection) :
- `oto.tools.serper` — `serper_maps`, `serper_scrape` (manquants vs CLI)
- `oto.tools.browser.pappers` — bilans détaillés (browser, attendre l'extension Chrome cf. issue #2)
- `oto.tools.browser.crunchbase` — funding signals (browser)
- `oto.tools.attio` / `oto.tools.folk` — CRM writes, attendent un consentement scoped par user
- `oto.tools.gmail` — read-only, attend consentement par user

Règle : **read-only par défaut**, les writes attendent un scope OIDC explicite.
