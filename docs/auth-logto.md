---
title: Auth — Logto
type: reference
description: >-
  Contrat d'authentification JWT entre oto-backend et Logto self-hosted (auth.oto.zone) :
  algorithme ES384 (gotcha RS256 → tout rejeté), discovery OAuth RFC 9728 via
  WWW-Authenticate sur 401, et façade DCR (oauth_facade.py) qui émule le Dynamic
  Client Registration absent de Logto pour permettre l'auto-installation par Claude,
  ChatGPT et Mistral sans client_id fixe. Inclut les variables d'environnement
  requises (LOGTO_ENDPOINT, OTO_MCP_CLAUDE_APP_ID, OTO_MCP_LOGTO_M2M_*) et les
  garde-fous _redirect_ok ; à consulter dès qu'un 401 JWT ou un échec d'installation
  MCP est à diagnostiquer.
---

# Auth — Logto

Le backend valide les bearer JWT émis par `auth.oto.zone/oidc`. Sur 401, le
header `WWW-Authenticate` pointe vers `/.well-known/oauth-protected-resource/mcp`
(RFC 9728) ce qui amorce le discovery OAuth côté client MCP.

**Gotcha** : Logto self-hosted signe en `ES384` (P-384 ECDSA). Le default de
`JWTVerifier` est RS256 → tous les tokens rejetés. Vérifié sur
`GET /oidc/jwks`.

Logto self-hosted n'expose pas DCR. La **façade DCR** (`oauth_facade.py`) le
supplée : métadonnée AS augmentée (`registration_endpoint` à nous) + à chaque
`POST /oauth/register` elle **enregistre dynamiquement le `redirect_uri` du client
dans l'app Logto partagée** (Management API via M2M dédié `OTO_MCP_LOGTO_M2M_*`)
puis renvoie le `client_id` partagé (`OTO_MCP_CLAUDE_APP_ID`). → les clients MCP
qui exigent DCR (Claude, **ChatGPT**, **Mistral**) s'installent **sans coller de
client_id ni intervention manuelle**, même quand le redirect varie par connecteur
(ChatGPT : `chatgpt.com/connector/oauth/<id>`). Garde-fou `_redirect_ok` : n'autorise
QUE des hosts connus (claude.ai/.com, chatgpt.com préfixe `/connector/oauth/`,
callback.mistral.ai, localhost) — pas un registrar ouvert. **Nouveau client qui
échoue** : son redirect est loggé (`DCR refusé — redirect_uris=…` en journalctl) →
ajouter son host à `_redirect_ok`. Fail-open : Management API en panne → `client_id`
renvoyé quand même (Claude, redirect pré-enregistré, jamais cassé).

**Onboarding actuel = self-serve ouvert.** Le tenant a sign-up activé par
email magic link, sans allowlist. Quiconque trouve l'URL peut s'inscrire,
mais c'est sans risque pour les clés serveur car les platform keys ne sont
accessibles qu'avec un grant explicite (cf. `access.py`).

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`,
`OTO_MCP_ADMIN_SUB` (sub Logto admin = **otomata `eufbvubidpyp`**, canonique, pas
le gmail dual-sub), `OTO_MCP_CLAUDE_APP_ID` (client partagé) + `OTO_MCP_LOGTO_M2M_*`
(M2M dédié pour la façade DCR). S3 Scaleway (`OTO_MCP_S3_*`, bucket `oto-media`)
pour les avatars/logos. Tous ces secrets sont dans SOPS `projects/oto-mcp.yaml`.
