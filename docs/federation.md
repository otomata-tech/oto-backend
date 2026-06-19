# Fédération MCP & comptes (otomata#16)

Deux mécanismes de fédération coexistent (cf. `tools/mount.py` vs `tools/remote.py`) :
- **mount** (`kind="mount"`) — monte les outils NATIFS d'un MCP distant (vrais schémas,
  `<ns>_<tool>`), credential **per-user** (token OAuth) injecté par requête. Pilote = **memento**.
- **remote** (ADR 0003, data-driven) — tunnel `<ns>_describe`/`<ns>_call` vers un bridge, credential
  d'**org** (token M2M + `meta.base_url`). Pilote = movinmotion.

**Fédération memento = systématique** (tranché 2026-06-17) :
- **Compte oto créé ⇒ compte memento créé.** `db.upsert_user` détecte le **vrai INSERT**
  (`RETURNING (xmax = 0)`) et appelle `memento_federation.provision_async(sub, email)` :
  POST best-effort (thread daemon, jamais bloquant) vers `MEMENTO_PROVISION_URL`
  (défaut `https://me.mento.cc/api/federation/provision`) avec le secret partagé
  `MEMENTO_PROVISION_BEARER`. memento provisionne le compte par **email** (oto=Logto,
  memento=Supabase → jointure email) via son `ensureAccount` (idempotent). **No-op** si
  `MEMENTO_PROVISION_BEARER` absent (fédération désactivée par défaut).
- **Mount memento monté d'office.** `OTO_MCP_MOUNTS_ENABLED` **non défini** → défaut
  `{memento}` (`_DEFAULT_ENABLED_MOUNTS`) ; `*` = tous, CSV = liste, `""` = kill-switch.
- **Connecteur visible de tous.** memento est passé `self_serve` (plus `platform_granted`/
  grant-only) → il apparaît dans le catalogue de chaque user → la carte « federated mcp »
  du dashboard invite à connecter son compte (**auto-prompt**). Un appel d'outil sans compte
  connecté lève une McpError actionnable (`resolve_mount_token`) pointant vers le dashboard.
- `/api/me` renvoie `memento: {connected, set_at}` (alimente l'auto-prompt). Le flow OAuth
  per-user reste `api_routes_memento.py` + `memento_oauth.py` (inchangé).
- Env requis pour activer la création de compte : `MEMENTO_PROVISION_BEARER` (+ côté memento,
  même secret en `MEMENTO_PROVISION_BEARER`). Limite : le catalogue mount est figé au boot
  (≥1 user connecté requis pour le charger ; refresh à chaud via `oto_admin_refresh_mount`).
