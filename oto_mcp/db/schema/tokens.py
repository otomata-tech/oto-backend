"""DDL du domaine « tokens » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# jetons d'API et d'upload
TOKENS = """
CREATE TABLE IF NOT EXISTS user_api_tokens (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'cli',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,  -- NULL = non-expirant (token CLI long-lived). Sinon rejeté passé l'échéance.
    -- Portée du jeton (`token_scopes.py`). NULL = jeton NON PORTÉ : il est le sub,
    -- pleins pouvoirs (comportement historique de tous les jetons émis à ce jour).
    -- Non NULL = deny-by-default : seules les routes que la portée nomme passent,
    -- p.ex. {"namespaces": {"leads-dormants": "read"}} pour une intégration tierce.
    scopes JSONB,
    -- QUI a demandé ce jeton : l'UTILISATEUR (`user`) ou l'EXÉCUTION
    -- (`delegation` — émis à la réservation d'un travail, au nom de son
    -- demandeur, borné au bail).
    --
    -- ⚠️ Ce n'est pas de l'étiquetage, c'est ce qui empêche deux choses de nature
    -- différente d'apparaître dans la même liste. L'écran des jetons annonce
    -- « long-lived tokens for the oto cli and ci environments » : y voir des
    -- jetons de 12 minutes émis automatiquement par dizaines fait MENTIR l'écran,
    -- et met un bouton « révoquer » sur un accès en cours d'usage.
    --
    -- ⚠️ Une COLONNE, pas un filtre sur le libellé : `label` est du texte libre —
    -- un utilisateur peut nommer son jeton « runner job 42 ». Filtrer sur du
    -- texte libre n'est pas une garantie, c'est une convention qu'on espère.
    kind TEXT NOT NULL DEFAULT 'user',
    -- Révocation (#523) : la ligne RESTE, `verify_api_token` refuse le jeton.
    -- Qui (`revoked_by`, le sub qui a coupé — pas de FK : la trace survit au
    -- compte), quand, et pourquoi (texte libre, facultatif). Base existante :
    -- révision Alembic `0007_jetons_revocation_tracee`, jamais le démarrage.
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    revoked_reason TEXT,
    -- Jeton de DÉLÉGATION seulement : le travail pour lequel il a été émis, et l'org
    -- de ce travail. `verrou_org` vrai = toute résolution du porteur hors de
    -- `verrou_org_id` est refusée (`oto_mcp/verrou_org.py`) ; `verrou_org_id` NULL
    -- avec le verrou = aucune org (portée personnelle seule). NULL partout = jeton
    -- non verrouillé (humain, API, délégation émise avant le verrou). Base
    -- existante : révision Alembic `0032_verrou_org_delegation`, jamais le démarrage.
    job_id BIGINT,
    verrou_org BOOLEAN,
    verrou_org_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_sub ON user_api_tokens(sub);

-- Jetons d'upload signés à USAGE UNIQUE (issue oto-backend#105). Un `oto_upload_url`
-- rend une URL signée HMAC (payload scellé sub/org/cible + TTL) sur laquelle un agent
-- PUT du contenu volumineux hors-bande. Le jeton lui-même est STATELESS ; on ne
-- persiste que le `jti` déjà consommé, pour interdire le rejeu. TTL court → purge
-- opportuniste des lignes anciennes à chaque consommation.
CREATE TABLE IF NOT EXISTS upload_tokens_used (
    jti TEXT PRIMARY KEY,
    used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
