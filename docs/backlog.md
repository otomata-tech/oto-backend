# Backlog

Issues GitHub : https://github.com/otomata-tech/oto-mcp/issues

## Org-gating sur l'auth Logto

L'auth Logto valide aujourd'hui n'importe quel user du tenant. Pour
restreindre à une organisation, sous-classer `JWTVerifier` et vérifier un
claim `organizations` (pattern `isOrgMember` dans GR :
`/data/missions/generations-renouvelables/server/src/services/logto.ts`).

Pas urgent — single-tenant + invitation manuelle suffit pour l'instant.

## WhatsApp pairing UI

Today : `whatsapp_*` réservé aux admins, pairing manuel (rsync session ou
auth CLI interactif sur le serveur). Pour ouvrir aux members, il faut une UI
sur `/account` qui :
- déclenche un `WhatsAppClient.auth()` côté serveur
- streame le QR code Baileys (généré sur stderr) vers le browser via SSE/WS
- détecte la fin du pairing et persiste la session

Garder l'auth_dir per-sub déjà en place (`<OTO_MCP_DATA_DIR>/whatsapp/<sub>/`).

## Futurs connecteurs

Tools déjà ajoutés : SIRENE, Serper (web+news), Hunter, LinkedIn (5), WhatsApp (3).

À envisager (par ordre de pertinence prospection) :
- `oto.tools.serper` — `serper_maps`, `serper_scrape` (manquants vs CLI)
- `oto.tools.browser.pappers` — bilans détaillés (browser, dépend extension cookie capture, cf. `extension/`)
- `oto.tools.browser.crunchbase` — funding signals (browser)
- `oto.tools.attio` / `oto.tools.folk` — CRM writes, attendent un consentement scoped par user
- `oto.tools.gmail` — read-only, attend consentement par user

Règle : **read-only par défaut**, les writes attendent un scope OIDC explicite.
