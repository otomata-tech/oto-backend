"""Initialisation du schéma + migrations idempotentes au boot.

Extrait de l'ex-monolithe `db.py` (barreau 2). `init_db()` applique `_SCHEMA`
puis les ALTER/backfill idempotents. Appelé une fois au démarrage du serveur.
"""
from __future__ import annotations

import json
import logging
import os

import psycopg

from ._conn import _connect
from ._schema import _SCHEMA

logger = logging.getLogger(__name__)

def init_db() -> None:
    with _connect() as conn:
        # AVANT _SCHEMA : renomme l'ancienne tool_call_log vers le schéma canonique
        # (sinon CREATE IF NOT EXISTS poserait une tool_calls vide à côté).
        _migrate_tool_call_log(conn)
        conn.execute(_SCHEMA)
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas les
        # nouvelles colonnes sur les tables existantes.
        conn.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS daily_quota INTEGER")
        # ADR 0032 §2 : le lien projet→entité porte un `role` (pourquoi cette entité est ici).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS role TEXT")
        # ADR 0032 §4 (B2) : surcharge contextuelle préfaite du lien (connecteur → identité/instructions).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'")
        # Corrélation des appels (ADR 0017, extension OTO-LOCALE de tool_calls).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS session_id TEXT")
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS run_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, created_at) WHERE run_id IS NOT NULL")
        # Org de l'appel (#67, scope d'audit exact) — extension OTO-LOCALE.
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS org_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_org ON tool_calls(org_id, created_at DESC) WHERE org_id IS NOT NULL")
        # Résolution des signaux d'usage (ADR 0017) : marquer un feedback/gap traité.
        # NULL = ouvert. resolution = note libre de l'opérateur (ce qui a été fait).
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_by TEXT")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolution TEXT")
        # Unipile revendeur (org_id porté au compte + plafond par org).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unipile_accounts_org ON unipile_accounts(org_id)")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS org_id BIGINT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS unipile_account_limit INTEGER")
        # Multi-canal Unipile : un account_id par (sub, provider). Migration de la
        # PK sub → (sub, provider) ; les lignes existantes prennent 'LINKEDIN' (DEFAULT).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_pkey")
        conn.execute("ALTER TABLE unipile_accounts ADD PRIMARY KEY (sub, provider)")
        # Horodatage du dernier sync du feed (miroir home, datastore linkedin-feed) :
        # gouverne la fraîcheur du cache (TTL) côté unipile_feed. NULL = jamais sync.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS feed_synced_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        # Retrait du rôle `guest` (2026-06-15) : défaut → member + migration des
        # lignes existantes (guest était un alias sans effet, cf. access.py).
        conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member'")
        conn.execute("UPDATE users SET role = 'member' WHERE role = 'guest'")
        # Accès plateforme & invitation virale (ADR 0013, barreau 1).
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_quota INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_granted_at TIMESTAMPTZ")
        # access_status : backfill ONE-SHOT à la création de la colonne — les comptes
        # existants sont pré-alpha → 'active'. Garder hors d'un ADD COLUMN ... DEFAULT
        # (qui poserait tout le monde 'pending') ET hors d'un UPDATE inconditionnel
        # rejoué à chaque boot (qui ré-activerait les pending et tuerait le gate).
        _has_access = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'access_status'"
        ).fetchone()
        if not _has_access:
            conn.execute("ALTER TABLE users ADD COLUMN access_status TEXT")
            conn.execute("UPDATE users SET access_status = 'active', access_granted_at = NOW()")
            conn.execute("ALTER TABLE users ALTER COLUMN access_status SET DEFAULT 'pending'")
            conn.execute("ALTER TABLE users ALTER COLUMN access_status SET NOT NULL")
        # L'admin bootstrap (OTO_MCP_ADMIN_SUB) ne tombe jamais en waitlist.
        _admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
        if _admin_sub:
            conn.execute(
                "UPDATE users SET access_status = 'active', "
                "access_granted_at = COALESCE(access_granted_at, NOW()) "
                "WHERE sub = %s AND access_status <> 'active'",
                (_admin_sub,),
            )
        # Invitation unifiée (ADR 0013) : org_id nullable + source (idempotent pour
        # les DB créées avant). NULL = referral alpha, l'invité crée sa propre org.
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN org_id DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS source TEXT")
        # Lien referral réutilisable + invitation par code court partageable à la
        # main (2026-06-22). referral_code = code stable par user (diffusable) ;
        # org_invitations.code = code single-use d'une invitation nominative ;
        # email nullable (émission sans envoi mail = code à partager soi-même).
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
                     "ON users(referral_code) WHERE referral_code IS NOT NULL")
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN email DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_invitations_code "
                     "ON org_invitations(code) WHERE code IS NOT NULL")
        # Datastore multi-compte (oto-backend#9) : compte Google propriétaire du sheet.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_email TEXT")
        # Datastore = spine natif PG (ADR 0016) : `spreadsheet_id` devient une
        # relique Sheets (nullable, plus écrite). DROP des colonnes Sheets différé
        # post-backfill — ici on lève juste le NOT NULL pour que les créations PG
        # natives passent. Idempotent.
        conn.execute("ALTER TABLE user_datastores ALTER COLUMN spreadsheet_id DROP NOT NULL")
        conn.execute("ALTER TABLE datastore_shares ALTER COLUMN spreadsheet_id DROP NOT NULL")
        # Primitive de ressource possédée (ADR 0030) : scope d'ownership porté par
        # la ressource. `owner_type` défaut 'user' (classeur perso, l'existant) ;
        # `owner_id` = sub pour un user, org.id::text pour un classeur d'org.
        # Backfill owner_id ← sub (idempotent : ne touche que les lignes non backfillées).
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'user'")
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_id TEXT")
        conn.execute("UPDATE user_datastores SET owner_id = sub WHERE owner_id IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_datastores_owner "
                     "ON user_datastores(owner_type, owner_id)")
        # Swap de contrainte (ADR 0030) : la clé logique passe de (sub, namespace) à
        # (owner_type, owner_id, namespace) — requis pour les classeurs org-owned.
        # `sub` devient une relique nullable (DROP de la colonne différé à la Phase H).
        conn.execute("ALTER TABLE user_datastores ALTER COLUMN sub DROP NOT NULL")
        conn.execute("ALTER TABLE user_datastores DROP CONSTRAINT IF EXISTS user_datastores_sub_namespace_key")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_user_datastores_owner_ns "
                     "ON user_datastores(owner_type, owner_id, namespace)")
        # Backfill datastore_shares → resource_grants (ADR 0030). One-shot idempotent :
        # ON CONFLICT DO NOTHING + clé stable resource_id = user_datastores.id::text.
        # On joint sur (owner_sub, namespace) pour retrouver l'id du namespace.
        conn.execute(
            "INSERT INTO resource_grants "
            "(resource_type, resource_id, principal_type, principal_id, permission, granted_at) "
            "SELECT 'datastore_namespace', d.id::text, 'user', s.shared_with_sub, "
            "       s.permission, s.created_at "
            "FROM datastore_shares s "
            "JOIN user_datastores d ON d.sub = s.owner_sub AND d.namespace = s.namespace "
            "ON CONFLICT DO NOTHING"
        )
        # Avatar utilisateur + logo d'org (2026-06-16) : URL publique (Scaleway
        # Object Storage), pas un secret → colonne en clair, hors coffre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logo_url TEXT")
        # Description libre de l'org (self-service org_admin) — prose, pas un secret.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        # Baseline de toolset par org (ADR 0015) : preset de visibilité curé par
        # l'org_admin, miroir d'org_groups.default_tools. NULL = pas de baseline.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS default_tools TEXT[]")
        # Baseline de connecteurs proposés par l'org (ADR 0019, B2) : miroir de
        # default_tools au grain connecteur (« org propose »). NULL = pas de baseline.
        # Inerte tant que la capacité B7 ne la lit pas.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS default_connectors TEXT[]")
        # Redaction de champs par org (FieldFilter) : politique par connecteur,
        # gouvernée par l'org_admin. Forme JSONB :
        #   { "<service>": { "salt": str?, "rules": [ {fields, action, ...} ] } }
        # {} = aucune config → repli sur le défaut serveur (field_filter_defaults).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS field_filters JSONB NOT NULL DEFAULT '{}'::jsonb")
        # Adresses expéditrices d'email de l'org, keyées PAR CONNECTEUR (scaleway/resend).
        #   { "<connector>": { "senders": [{email, name?, reply_to?}], "quiet_hours"?: {...} } }
        # {} = aucune adresse → email_send retombe sur la marque oto@otomata.tech (super_admin).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS email_settings JSONB NOT NULL DEFAULT '{}'::jsonb")
        # Migration ONE-SHOT (idempotente, gardée sur le format PLAT = clé `senders` au
        # top-level) : {senders:[{...,transport}], quiet_hours} → keyé par connecteur.
        # transport 'resend'→'resend', sinon 'scaleway' ; transport retiré du sender ;
        # quiet_hours global recopié sur chaque connecteur recevant ≥1 sender.
        for _row in conn.execute(
                "SELECT id, email_settings FROM orgs WHERE email_settings ? 'senders'").fetchall():
            _flat = _row["email_settings"] or {}
            _qh = _flat.get("quiet_hours")
            _grouped: dict = {}
            for _s in _flat.get("senders", []):
                _cn = "resend" if (_s.get("transport") == "resend") else "scaleway"
                _grouped.setdefault(_cn, {"senders": []})["senders"].append(
                    {_k: _v for _k, _v in _s.items() if _k != "transport"})
            if _qh:
                for _blk in _grouped.values():
                    _blk["quiet_hours"] = _qh
            conn.execute("UPDATE orgs SET email_settings = %s::jsonb WHERE id = %s",
                         (json.dumps(_grouped), _row["id"]))
        # Archivage (soft-delete) d'une org : masquée de tous les listings, réversible
        # (NULL = active). Pas de hard-delete — les FK (membres, credentials, usage,
        # billing, invitations, groupes) restent intactes pour audit/restauration.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        # Org PERSO (suppression du perso) : `personal_of` = sub dont c'est l'espace
        # privé mono-membre (NULL = org partagée). Unicité : 1 org perso par user.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS personal_of TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_personal_of "
                     "ON orgs(personal_of) WHERE personal_of IS NOT NULL")
        # Identité par org (ADR 0015) : visibilité scopée par (sub, org_id) ; org_id=0
        # = profil perso/global. Migration ONE-SHOT (gardée sur l'absence d'org_id) :
        # ajoute la colonne (existants → 0 = perso), re-keye les PK, puis BACKFILL =
        # copie le profil perso de chacun vers son org active (on retrouve sa config
        # là où on est aujourd'hui). Idempotent (ON CONFLICT) + joué une seule fois.
        _has_vis_orgid = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user_disabled_tools' AND column_name = 'org_id'"
        ).fetchone()
        if not _has_vis_orgid:
            for _t in ("user_disabled_tools", "user_enabled_tools", "user_presets"):
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN org_id BIGINT NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {_t} DROP CONSTRAINT IF EXISTS {_t}_pkey")
            conn.execute("ALTER TABLE user_disabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_enabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_presets ADD PRIMARY KEY (sub, org_id, name)")
            conn.execute(
                "INSERT INTO user_disabled_tools (sub, org_id, tool_name, disabled_at) "
                "SELECT d.sub, m.org_id, d.tool_name, d.disabled_at FROM user_disabled_tools d "
                "JOIN org_members m ON m.sub = d.sub AND m.is_active WHERE d.org_id = 0 "
                "ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO user_enabled_tools (sub, org_id, tool_name, enabled_at) "
                "SELECT e.sub, m.org_id, e.tool_name, e.enabled_at FROM user_enabled_tools e "
                "JOIN org_members m ON m.sub = e.sub AND m.is_active WHERE e.org_id = 0 "
                "ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO user_presets (sub, org_id, name, enabled_tools, created_at, updated_at) "
                "SELECT p.sub, m.org_id, p.name, p.enabled_tools, p.created_at, p.updated_at "
                "FROM user_presets p JOIN org_members m ON m.sub = p.sub AND m.is_active "
                "WHERE p.org_id = 0 ON CONFLICT DO NOTHING")
        # Suppression du perso : les profils de visibilité `org_id=0` (perso/global)
        # ont été copiés vers l'org active ci-dessus et ne sont plus jamais relus
        # (`session_visibility` lit l'org active, toujours posée). Purge des orphelins
        # (idempotent : no-op une fois vide ; plus aucune écriture en org_id=0).
        for _t in ("user_disabled_tools", "user_enabled_tools", "user_presets"):
            conn.execute(f"DELETE FROM {_t} WHERE org_id = 0")
        # Coffre chiffré : colonnes courantes (idempotent pour les DB créées avant).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS secret_enc TEXT")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT IF EXISTS connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ADD PRIMARY KEY (entity_type, entity_id, connector, account)")
        conn.execute("ALTER TABLE platform_keys ADD COLUMN IF NOT EXISTS api_key_enc TEXT")
        # TTL opt-in des tokens API (audit 2026-06-13) : NULL = non-expirant.
        conn.execute("ALTER TABLE user_api_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        _drop_legacy_plaintext_stores(conn)
        # Décommission du substrat « fact graph » (ex-ADR 0008/0027) : le schéma
        # factgraph et toutes ses tables sont supprimés (idempotent). Plus aucune
        # capacité/outil/vue ne s'y adosse.
        conn.execute("DROP SCHEMA IF EXISTS factgraph CASCADE")
        # Cran d'activation des connecteurs (ADR 0010, B1) — table + seed unique
        # (snapshot du registre courant à ON). Aucun lecteur encore (canari) :
        # le câblage catalogue/chargement suit en B2/B3.
        from .. import connector_activation as _conn_act
        _conn_act.init_schema(conn)
        _conn_act.seed_initial(conn)
        # Sélection de connecteurs par membre (ADR 0019, B1) — table seule, aucun
        # lecteur encore (canari, no-behavior-change) ; le câblage lecture/mutation
        # (capacité connectors.me/select/pause) et le masquage pause au middleware
        # suivent en B3/B4/B5.
        from .. import connector_selection as _conn_sel
        _conn_sel.init_schema(conn)
    # Borne la volumétrie du journal de monitoring (hors transaction schéma).
    try:
        from .usage import prune_tool_calls
        prune_tool_calls(int(os.environ.get("OTO_MCP_CALL_LOG_RETENTION_DAYS", "30")))
    except Exception as e:
        logger.warning("prune_tool_calls failed: %s", e)


def _migrate_tool_call_log(conn: psycopg.Connection) -> None:
    """tool_call_log → tool_calls (schéma canonique otomata-calllog,
    2026-06-13) : renomme table + colonnes, ajoute server/email/args.
    Idempotent — no-op si l'ancienne table n'existe plus (ou jamais existé)."""
    exists = conn.execute(
        "SELECT to_regclass('tool_call_log') IS NOT NULL AND to_regclass('tool_calls') IS NULL AS go"
    ).fetchone()
    if not exists or not exists["go"]:
        return
    conn.execute("ALTER TABLE tool_call_log RENAME TO tool_calls")
    conn.execute("ALTER TABLE tool_calls RENAME COLUMN tool_name TO tool")
    conn.execute("ALTER TABLE tool_calls RENAME COLUMN called_at TO created_at")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS server TEXT NOT NULL DEFAULT 'oto'")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS email TEXT")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS args JSONB")
    logger.info("tool_call_log migrée vers tool_calls (schéma canonique)")


def _drop_legacy_plaintext_stores(conn: psycopg.Connection) -> None:
    """Purge des emplacements plaintext supersédés par le coffre chiffré
    `connector_credentials` (migration terminée + soak nullé en prod, cf.
    `project_oto_connector_vault`). Idempotent (IF EXISTS) — no-op sur une DB
    fraîche (on-prem). Le chiffrement est désormais obligatoire : plus aucun
    chemin plaintext (writers/reveal en chiffré-seul)."""
    # connector_credentials.secret (plaintext interne du coffre) + platform_keys.api_key
    conn.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS secret")
    conn.execute("ALTER TABLE platform_keys DROP COLUMN IF EXISTS api_key")
    # Colonnes legacy users.<provider>_api_key + sessions linkedin/crunchbase.
    for col in ("serper_api_key", "hunter_api_key", "sirene_api_key", "attio_api_key",
                "lemlist_api_key", "kaspr_api_key", "pennylane_api_key", "slack_api_key",
                "fullenrich_api_key", "linkedin_cookie", "linkedin_user_agent",
                "linkedin_cookie_set_at", "crunchbase_cookies", "crunchbase_user_agent",
                "crunchbase_set_at"):
        conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}")
    # Tables legacy entièrement foldées dans le coffre.
    conn.execute("DROP TABLE IF EXISTS org_secrets")
    conn.execute("DROP TABLE IF EXISTS user_google_oauth")
