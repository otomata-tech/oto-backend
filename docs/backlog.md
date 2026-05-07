# Backlog

## Auth — passer sur Logto (équivalent du MCP GR)

**Contexte.** Aujourd'hui : `InMemoryOAuthProvider` + DCR + un seul mot de
passe partagé (`OTO_MCP_OAUTH_PASSWORD`). N'importe qui qui a le mot de passe
voit tout le serveur — single-user MVP. Le MCP GR (mission Générations
Renouvelables) utilise Logto comme IdP, ce qui donne :
- vraies sessions par utilisateur (compte Logto)
- client_id/secret stables émis par Logto
- révocation par compte, audit
- pas de mot de passe partagé qui circule

**Quoi faire.**

1. Créer une app M2M ou Native dans le tenant Logto utilisé par les autres
   services oto.zone (cf. `auth.oto.zone` / `logto.oto.zone`).
2. Configurer une resource `https://mcp.oto.ninja` côté Logto + scopes
   (`mcp:read` minimum).
3. Remplacer `PasswordOAuthProvider` par un délégué qui valide les access
   tokens Logto via JWKS (vérif signature + audience + scopes).
4. Supprimer `/login` + le form mot de passe.
5. Mettre à jour `README.md` et `deploy/DEPLOY.md`.
6. Brancher la redirect URI Logto sur `https://mcp.oto.ninja/callback`.

**Ce qu'il faut conserver.** Le edge bearer-gate Caddy reste utile pour rejeter
les probes anonymes avant que ça touche l'app.

**Quand.** Pas urgent tant qu'on est seul user. Devient nécessaire dès qu'on
ouvre l'accès à quelqu'un d'autre, ou si on veut ajouter des tools sensibles
(ex. wrappers Pennylane, Attio, gmail).

**Référence d'implémentation.** Regarder `mcp.memento.otomata.tech` (memento-mcp)
ou le serveur GR qui font déjà ce pattern Logto + JWKS.

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
