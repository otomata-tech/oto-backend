# Backlog

## Org-gating sur l'auth Logto (optionnel)

L'auth Logto est en place (cf. `deploy/DEPLOY.md` étape 3). Pour l'instant
n'importe quel user du tenant Logto peut récupérer un token et appeler le
MCP. Si on veut restreindre à une organisation Logto précise, ajouter un
`TokenVerifier` custom qui sous-classe `JWTVerifier` et vérifie un claim
`organizations` (cf. pattern `isOrgMember` dans la mission GR :
`/data/missions/generations-renouvelables/server/src/services/logto.ts`).

Pas urgent — single-tenant Logto + provisioning manuel de comptes côté admin
suffit pour l'instant.

---

## Futurs connecteurs

À ajouter dans `oto_mcp/tools.py` selon besoins :

- `oto.tools.sirene.SireneClient` — recherche INSEE plus brute (SIRET, etabs)
- `oto.tools.serper` — search web/news
- `oto.tools.pappers` (browser) — données légales enrichies
- `oto.tools.attio` / `oto.tools.folk` — CRM (sensible, à brancher après Logto)
- `oto.tools.gmail` — read-only (idem, après Logto)

Garder le serveur **read-only par défaut** : les writes (envoi mail, création
de contacts) attendent de l'auth user.
