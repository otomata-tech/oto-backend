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
        # AVANT _SCHEMA : droppe l'org_subscriptions du modèle Stripe retiré
        # (sinon CREATE IF NOT EXISTS saute et l'index 0043 explose au boot).
        _drop_legacy_org_subscriptions(conn)
        conn.execute(_SCHEMA)
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas les
        # nouvelles colonnes sur les tables existantes.
        conn.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS daily_quota INTEGER")
        # ADR 0032 §2 : le lien projet→entité porte un `role` (pourquoi cette entité est ici).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS role TEXT")
        # ADR 0032 §4 (B2) : surcharge contextuelle préfaite du lien (connecteur → identité/instructions).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'")
        # ADR 0032 §3 (B4b) : un « Autre document » peut être partagé publiquement.
        conn.execute("ALTER TABLE project_files ADD COLUMN IF NOT EXISTS public BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE project_files ADD COLUMN IF NOT EXISTS public_url TEXT")
        # ADR 0032 §7 (B5a) : un projet peut être publié comme MODÈLE (template) copiable.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_template BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_template ON projects(is_template) WHERE is_template")
        # ADR 0032 (amende #44) : publication d'un projet en endpoint MCP dédié
        # `<mcp_slug>.mcp.oto.cx` — anonyme (toolset figé, sans login) ou authed per-org.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_slug TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_access TEXT NOT NULL DEFAULT 'off'")
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_tools TEXT[] NOT NULL DEFAULT '{}'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_mcp_slug ON projects(mcp_slug) WHERE mcp_slug IS NOT NULL")
        # Opt-in explicite : exposer les tools `data_*` (datastore de l'org propriétaire)
        # sur un endpoint `secret` sans login — l'endpoint AGIT alors sous l'autorité de
        # l'org propriétaire (pas de sub). Défaut FALSE (le datastore reste privé). JAMAIS
        # honoré en `anonymous` (endpoint public listé) : cf. set_project_mcp_publication.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_expose_datastore BOOLEAN NOT NULL DEFAULT FALSE")
        # Opt-in ADDITIONNEL, séparé de la lecture (#193) : l'ÉCRITURE du datastore
        # (data_write/data_set_schema) sur l'endpoint partagé. Défaut FALSE (lecture seule).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_expose_datastore_write BOOLEAN NOT NULL DEFAULT FALSE")
        # ADR 0043 phase 2 (SEPA) : id du mandat Stancer (mndt_xxx) sur l'abonnement —
        # la table existait déjà (B1) quand la colonne est arrivée.
        conn.execute("ALTER TABLE org_subscriptions ADD COLUMN IF NOT EXISTS mandate_id TEXT")
        # « Ajouter à mon Oto » (otomata-private, canal d'acquisition) : un projet forké
        # depuis un partage public garde le pointeur vers sa source → import IDEMPOTENT
        # (on RÉCUPÈRE la copie déjà présente dans l'org au lieu d'en refaire une).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS copied_from BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_copied_from "
                     "ON projects(owner_type, owner_id, copied_from) WHERE copied_from IS NOT NULL")
        # Retrait du partage public CHIFFRÉ zero-knowledge (`/p/p`), supplanté par le
        # partage NAVIGABLE live sur `<slug>.share.oto.cx` (share_ui). La table ne stockait
        # que du ciphertext irrécupérable (clé jamais côté serveur) → drop sûr, pas de legacy.
        conn.execute("DROP TABLE IF EXISTS project_public_shares")
        # ADR 0032 §7 : l'onboarding n'est plus un mode spécial mais un projet « Découverte »
        # (semé à la création de l'org perso). On retire la machinerie d'accueil de la fiche
        # « situation avec oto » — il ne reste que le data model `profile`, relu à chaque session.
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS discovery_project_id")
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS onboarded")
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS onboarded_at")
        conn.execute("DELETE FROM platform_instructions WHERE key = 'onboarding'")
        # ADR 0042 (barreau 1) : `guides` unifie la PROSE d'instruction sur deux
        # livraisons — 'on-demand' (how-to `oto_guide`) et 'init' (readme injecté au
        # handshake). Colonne `delivery` (existants → 'on-demand' par DEFAULT = les
        # guides B5 restent des how-to) + backfill des readmes init platform + user
        # depuis les ex-tables (org/group suivent au barreau 2). ON CONFLICT DO NOTHING
        # = idempotent, ne réécrit jamais une ligne guides déjà posée.
        conn.execute("ALTER TABLE guides ADD COLUMN IF NOT EXISTS delivery TEXT NOT NULL DEFAULT 'on-demand'")
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, delivery, body_md, created_at, updated_at) "
            "SELECT 'platform', 'platform', key, 'init', body_md, "
            "       COALESCE(updated_at, NOW()), COALESCE(updated_at, NOW()) "
            "FROM platform_instructions WHERE key = 'secret_sauce' "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING")
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, delivery, body_md, created_at, updated_at) "
            "SELECT 'user', sub, 'readme', 'init', body_md, "
            "       COALESCE(created_at, NOW()), COALESCE(updated_at, NOW()) "
            "FROM user_agent_readme WHERE COALESCE(body_md, '') <> '' "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING")
        # Barreau 2 : readmes d'org + d'équipe (slug réservé claude_md) sortent de
        # `*_instructions` (qui ne gardent que les PROCÉDURES + versioning) vers `guides`.
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, delivery, body_md, created_at, updated_at) "
            "SELECT 'org', org_id::text, 'readme', 'init', body_md, "
            "       COALESCE(created_at, NOW()), COALESCE(updated_at, NOW()) "
            "FROM org_instructions WHERE slug = 'claude_md' AND COALESCE(body_md, '') <> '' "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING")
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, delivery, body_md, created_at, updated_at) "
            "SELECT 'group', group_id::text, 'readme', 'init', body_md, "
            "       COALESCE(created_at, NOW()), COALESCE(updated_at, NOW()) "
            "FROM org_group_instructions WHERE slug = 'claude_md' AND COALESCE(body_md, '') <> '' "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING")
        # ADR 0032 §6 / 0029 (B6) : mode typé optionnel d'un namespace de datastore.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS schema JSONB")
        # gap #4a : partage public d'un doc (token de lien public, lookup indexé).
        conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token TEXT")
        # ADR 0032 (« stop using slug ») : id surrogate stable + globalement unique pour
        # les doctrines. `org_instructions` garde (org_id, slug) comme clé naturelle
        # interne ; l'`id` devient l'identité PUBLIQUE (URL, project_links, runs). Backfill
        # des lignes existantes via une séquence (idempotent).
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS id BIGINT")
        conn.execute("CREATE SEQUENCE IF NOT EXISTS org_instructions_id_seq OWNED BY org_instructions.id")
        conn.execute("UPDATE org_instructions SET id = nextval('org_instructions_id_seq') WHERE id IS NULL")
        conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET DEFAULT nextval('org_instructions_id_seq')")
        conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_instructions_id ON org_instructions(id)")
        # B3 : migrer les liens projet→procédure de slug vers l'id de doctrine (org-owned ;
        # les projets user-owned gardent le slug, résolu à la lecture côté front). Idempotent
        # (guard `!~ '^[0-9]+$'` = pas déjà un id ; JOIN = seulement si la doctrine existe).
        conn.execute("""
            UPDATE project_links pl SET target_ref = oi.id::text
            FROM projects p JOIN org_instructions oi ON oi.org_id = p.owner_id::bigint
            WHERE pl.project_id = p.id AND pl.target_type = 'procedure'
              AND p.owner_type = 'org' AND oi.slug = pl.target_ref
              AND pl.target_ref !~ '^[0-9]+$'
        """)
        # ADR 0035 (B1) : slots de procédure — déclaration d'entités requises (JSON propre),
        # référencées par nom dans la prose (<slot:name>). Transportée par revisions +
        # copy/fork/publish. Canari no-op : aucune résolution runtime avant B3.
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE org_instruction_revisions ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE doctrine_library ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        # ADR 0035 (B2) : un lien peut BINDER un slot par NOM — vocabulaire DU PROJET
        # (deux procédures liées partageant `sortie` partagent le binding). Unicité
        # (projet, slot) = zéro ambiguïté par nommage explicite, refusée au link (409).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS slot TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_links_slot "
                     "ON project_links(project_id, slot) WHERE slot IS NOT NULL")
        # ADR 0032 §4 amendé (#57) : un projet peut lier N fois le même connecteur, chaque
        # binding distingué par une IDENTITÉ → une ligne par binding. Colonne `identity_ref`
        # (NULL = binding par défaut, rétro-compat), clé élargie NULLS NOT DISTINCT (PG15+ :
        # deux NULL = même binding par défaut, un seul autorisé).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS identity_ref TEXT")
        conn.execute("ALTER TABLE project_links DROP CONSTRAINT IF EXISTS project_links_project_id_target_type_target_ref_key")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_links_binding "
                     "ON project_links (project_id, target_type, target_ref, identity_ref) NULLS NOT DISTINCT")
        # B3 : l'identité épinglée quitte `config.identity_id` pour la clé de binding
        # `identity_ref` (fin du doublon). config ne garde que instructions_md. Idempotent
        # (les lecteurs legacy reçoivent identity_id re-dérivé de identity_ref, cf. list_project_links).
        conn.execute("""
            UPDATE project_links
            SET identity_ref = config->>'identity_id', config = config - 'identity_id'
            WHERE target_type = 'connecteur' AND identity_ref IS NULL
              AND COALESCE(config->>'identity_id', '') <> ''
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_public_token ON docs(public_token) WHERE public_token IS NOT NULL")
        # ADR 0032 §5/§6 (B3) : un run est rattaché au projet actif gelé à son ouverture.
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS project_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at DESC)")
        # Discriminateur d'événement (ADR 0017, « un seul flux ») : 'mcp' (défaut,
        # cas historique) / 'rest' / 'connector'. Les lignes existantes deviennent
        # 'mcp' par le DEFAULT → les lectures kind='mcp' restent iso (canari no-op).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'mcp'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_kind ON tool_calls(kind, created_at DESC)")
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
        # ⚠️ Le cycle de vie du PK d'unipile_accounts appartient à
        # db.backfill_unipile_member_scope() (ADR 0033 B4) — l'ex re-pose
        # inconditionnelle du PK (sub, provider) écraserait la migration.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS platform_seat BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS platform_seat BOOLEAN NOT NULL DEFAULT FALSE")
        # Horodatage du dernier sync du feed (miroir home, datastore linkedin-feed) :
        # gouverne la fraîcheur du cache (TTL) côté unipile_feed. NULL = jamais sync.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS feed_synced_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        # ADR 0044 (B0) : l'entrée du coffre devient une INSTANCE de connecteur (config
        # possédée). Colonnes DORMANTES — non lues par la résolution avant B2/B3 (canari
        # additif). `version` = verrou optimiste (B1) ; `share_down` = ouverture au
        # sous-arbre, mono-scope ; `share_side` = prêts nominatifs à des pairs.
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
        # share_down = ALLOWLIST deny-by-default ([] = ouvert au sous-arbre ; restreint aux
        # scopes listés) ; share_side = EXTENSION (prêts nominatifs à des pairs). Dormantes
        # jusqu'à l'enforcement (deny-check cascade + garde pin).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_down JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_side JSONB NOT NULL DEFAULT '[]'::jsonb")
        # GIN sur share_side pour la projection « partagé avec moi » (jsonb_exists_any /
        # `?|` = scan indexé au lieu d'un seq scan de tout le coffre).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conn_cred_share_side ON connector_credentials USING gin (share_side)")
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
        # Préférence de langue de l'UI dashboard (2026-07-07) : NULL = pas de
        # préférence explicite (le front retombe sur la langue du navigateur).
        # Validée à 'en'|'fr' en amont (capacité me.locale.set) ; colonne libre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locale TEXT")
        # Avatar utilisateur + logo d'org (2026-06-16) : URL publique (Scaleway
        # Object Storage), pas un secret → colonne en clair, hors coffre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logo_url TEXT")
        # Description libre de l'org (self-service org_admin) — prose, pas un secret.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        # Profil d'org (2026-07-02) : donner du corps à l'entreprise. `domain` =
        # domaine de marque (acme.com, normalisé org_store._normalize_domain) —
        # sert AUSSI à dériver le logo via logo.dev quand aucun logo n'est uploadé
        # (org_store.effective_logo_url, même CDN que le catalogue connecteurs).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS domain TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS industry TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT ''")
        # Baseline de toolset par org (ex-ADR 0015) RETIRÉE — les presets de tools
        # ont été supprimés : drop de la colonne si présente (idempotent).
        conn.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS default_tools")
        # Baseline de connecteurs proposés par l'org (ADR 0019, B2) : liste de
        # connecteurs recommandés (« org propose »). NULL = pas de baseline.
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
        # invitations, groupes) restent intactes pour audit/restauration.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        # Org PERSO (suppression du perso) : `personal_of` = sub dont c'est l'espace
        # privé mono-membre (NULL = org partagée). Unicité : 1 org perso par user.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS personal_of TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_personal_of "
                     "ON orgs(personal_of) WHERE personal_of IS NOT NULL")
        # MFA obligatoire par org (voie « org Logto miroir », ADR 0044/sécu-auth).
        #   require_mfa   = l'org impose le 2ᵉ facteur à ses membres (toggle org_admin).
        #   logto_org_id  = l'organization Logto MIROIR créée derrière l'org quand le
        #                   MFA est activé (isMfaRequired=true + membres synchronisés
        #                   par sub) ; NULL tant que le MFA n'est pas activé.
        # Source de vérité (org, membres, droits) = CE PG ; l'org Logto n'est qu'un
        # miroir d'enforcement MFA au login (aucune autorité). Voir docs/auth-logto.md.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS require_mfa BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logto_org_id TEXT")
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
            for _t in ("user_disabled_tools", "user_enabled_tools"):
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN org_id BIGINT NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {_t} DROP CONSTRAINT IF EXISTS {_t}_pkey")
            conn.execute("ALTER TABLE user_disabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_enabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
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
        # Suppression du perso : les profils de visibilité `org_id=0` (perso/global)
        # ont été copiés vers l'org active ci-dessus et ne sont plus jamais relus
        # (`session_visibility` lit l'org active, toujours posée). Purge des orphelins
        # (idempotent : no-op une fois vide ; plus aucune écriture en org_id=0).
        for _t in ("user_disabled_tools", "user_enabled_tools"):
            conn.execute(f"DELETE FROM {_t} WHERE org_id = 0")
        # Presets de tools (snapshots nommés) RETIRÉS : drop de la table si présente.
        conn.execute("DROP TABLE IF EXISTS user_presets")
        # Baseline de toolset d'équipe (ex-ADR 0012) RETIRÉE avec les presets : drop.
        conn.execute("ALTER TABLE org_groups DROP COLUMN IF EXISTS default_tools")
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
    # #109 ch.3 : matérialise les clés métier déclarées en contrainte (hors
    # transaction schéma — DDL par namespace, fail-open).
    _ensure_datastore_key_indexes()


def _ensure_datastore_key_indexes() -> None:
    """#109 ch.3 — pour chaque namespace dont le schéma déclare `key`, pose l'index
    UNIQUE partiel s'il manque, en résorbant D'ABORD les doublons hérités (merge
    chronologique dans la row la plus ancienne = ce que l'upsert applicatif aurait
    produit sans les courses ; les doublons SONT des artefacts du bug d'unicité).
    Fail-open PAR namespace : un tableau récalcitrant est loggé, ne bloque ni le
    boot ni les autres (son chemin d'écriture reste l'applicatif historique)."""
    from . import datastore as ds
    try:
        targets = ds.datastore_namespaces_with_key()
    except Exception:
        logger.warning("key-index migration: énumération échouée", exc_info=True)
        return
    for ns in targets:
        try:
            if ds.datastore_has_key_index(ns["id"]):
                continue
            removed = ds.datastore_merge_key_duplicates(ns["id"], ns["key"])
            ds.datastore_ensure_key_index(ns["id"], ns["key"])
            if removed:
                logger.info("key-index ns=%s key=%s : %d doublon(s) résorbé(s)",
                            ns["id"], ns["key"], removed)
        except Exception:
            logger.warning("key-index migration ns=%s : échec (fail-open)",
                           ns.get("id"), exc_info=True)


def _drop_legacy_org_subscriptions(conn: psycopg.Connection) -> None:
    """org_subscriptions du modèle Stripe (retiré par oto-backend#82 le
    2026-07-01 — le code est parti, la table est restée en prod) : forme
    incompatible avec l'ADR 0043 (PK (org_id, product), colonnes stripe_*) et
    données mortes avec le modèle → DROP, _SCHEMA recrée la forme 0043.
    Détection par la colonne `stripe_subscription_id`, jamais présente dans la
    forme 0043 — idempotent, no-op sur une table déjà migrée ou absente.
    Vécu 2026-07-06 : boot KO `column "next_billing_at" does not exist`
    (l'index partiel 0043 sur la table legacy), rollback auto du deploy."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'org_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    ).fetchone()
    if row:
        conn.execute("DROP TABLE org_subscriptions")
        logger.warning(
            "org_subscriptions legacy (modèle Stripe, #82) droppée — "
            "recréée à la forme ADR 0043 par _SCHEMA"
        )


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
