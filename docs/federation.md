---
title: Fédération MCP & comptes (otomata#16)
type: explanation
description: >-
  Explique les deux mécanismes de fédération MCP coexistants dans oto-backend : mount
  (kind="mount", tools natifs du MCP distant, token OAuth per-user injecté par requête,
  pilote memento — DÉMONTÉ du montage d'office le 2026-07-02, régime commun d'activation)
  et remote (ADR 0003, tunnel <ns>_describe/<ns>_call data-driven, credential M2M d'org,
  pilote = un connecteur remote client). Détaille le provisionnement automatique compte memento à la
  création oto (db.upsert_user + MEMENTO_PROVISION_BEARER, toujours actif), l'activation
  des mounts (connector_activation ∪ OTO_MCP_MOUNTS_ENABLED), et la limite catalogue figé
  au boot (refresh via oto_admin_refresh_mount). À lire pour comprendre pourquoi/comment
  un MCP distant est monté, ou pour ajouter un nouveau mount ou bridge.
adr:
  - "0003"
---

# Fédération MCP & comptes (otomata#16)

Deux mécanismes de fédération coexistent (cf. `tools/mount.py` vs `tools/remote.py`) :
- **mount** (`kind="mount"`) — monte les outils NATIFS d'un MCP distant (vrais schémas,
  `<ns>_<tool>`), credential **per-user** (token OAuth) injecté par requête. Pilote = **memento**.
- **remote** (ADR 0003, data-driven) — tunnel `<ns>_describe`/`<ns>_call` vers un bridge, credential
  d'**org** (token M2M + `meta.base_url`). Pilote = un connecteur remote client.

## Mount SANS auth (endpoint hébergé public)

Un mount `kind="mount"` dont `auth_modes` est **VIDE** est un **mount no-auth** :
l'endpoint distant est hébergé et **public** (aucune clé, aucun compte, catalogue
product-level anonyme). Pilote = **justicelibre** (`https://justicelibre.org/mcp` —
droit français & européen, législation LEGI/JORF/KALI + jurisprudence
Cass/Judilibre/CE/CC/CEDH/CJUE/CNIL ; MIT + Licence Ouverte Etalab 2.0).

`tools/mount.py` détecte `not connector.auth_modes` et prend un chemin dédié qui
**court-circuite tout le machinery per-user** :
- `_fetch_catalog` liste les outils au boot **sans token ni user connecté** (le
  fallback historique « token d'un user déjà connecté » ne s'applique pas) ;
- `_make_factory` renvoie une factory qui forwarde **sans header `Authorization`**,
  **sans `resolve_mount_token`** et sans exiger un sub courant.

**Gating d'exposition** = `connector_activation` (ADR 0010/0011), comme n'importe
quel connecteur — c'est le SEUL levier ici (pas de credential per-user à connecter).
justicelibre est **opt-in par org** : master OFF au registre d'activation, hors
`_DEFAULT_ENABLED_MOUNTS`, hors bundle par défaut. Une org l'active via l'écran
connector activation → le mount **suit** (`_db_activated_mounts` inclut tout mount
ayant ≥1 activation ON, master OU override d'org). Comme le catalogue est **figé au
boot**, la 1ʳᵉ activation demande un **restart** OU un `oto_admin_refresh_mount
justicelibre` (admin plateforme) pour le monter à chaud.

Dégradation propre : endpoint distant down au boot → 0 outil fédéré, le reste d'oto
intact (le fetch est sous try/except). Test : `tests/test_mount_noauth.py`.

**Fédération memento — DÉMONTÉE le 2026-07-02** (était « systématique », tranché
2026-06-17 ; plus utilisée → « ça alourdit pour rien », 34 tools dans chaque session).
Le code est intact et réactivable ; état courant :
- **Plus aucun mount monté d'office.** `_DEFAULT_ENABLED_MOUNTS` est **vide** ; un mount
  se monte via le régime commun `connector_activation` (master/override ON) ∪ env
  `OTO_MCP_MOUNTS_ENABLED` (`*` = tous, CSV = liste, `""` = kill-switch absolu). En prod :
  masters memento/atlassian/justicelibre **OFF**, env = `planity` seul.
- **Réactiver memento** = master ON au dashboard (le mount suit au restart, ou
  `oto_admin_refresh_mount`) ; le connecteur redevient visible au catalogue (`self_serve`,
  carte « federated mcp », auto-prompt) et un appel sans compte connecté lève une McpError
  actionnable (`resolve_mount_token`).
- **Provisioning de compte TOUJOURS ACTIF** (non démonté) : `db.upsert_user` sur vrai
  INSERT (`RETURNING (xmax = 0)`) appelle `memento_federation.provision_async(sub, email)` —
  POST best-effort (thread daemon) vers `MEMENTO_PROVISION_URL` (défaut
  `https://me.mento.cc/api/federation/provision`) avec le secret `MEMENTO_PROVISION_BEARER`
  (absent = no-op). memento provisionne par **email** (oto=Logto, memento=Supabase →
  jointure email), idempotent. Couper la fédération complètement = retirer aussi ce bearer.
- `/api/me` renvoie `memento: {connected, set_at}`. Flow OAuth per-user :
  `api_routes_memento.py` + `memento_oauth.py` (dormants). Limite inchangée : catalogue
  mount figé au boot (≥1 credential connecté requis pour le charger).
