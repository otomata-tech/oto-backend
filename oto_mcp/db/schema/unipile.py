"""DDL du domaine « unipile » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# comptes messagerie hébergés et leurs prêts
UNIPILE = """
-- Unipile : mapping per-user du compte LinkedIn connecté sous l'abonnement
-- Unipile (B3). La CLÉ Unipile est partagée (org secret) ; chaque user connecte
-- SON LinkedIn par hosted-auth → un `account_id` distinct sous la même clé. Ce
-- n'est PAS un secret (handle opaque), d'où une table en clair (≠ coffre chiffré).
-- `resolve` : clé partagée + account_id per-user → chacun agit comme lui-même.
CREATE TABLE IF NOT EXISTS unipile_accounts (
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    -- canal Unipile (LINKEDIN/WHATSAPP/TELEGRAM/INSTAGRAM/…) : un user a un
    -- account_id DISTINCT par canal, sous la même clé partagée.
    provider TEXT NOT NULL DEFAULT 'LINKEDIN',
    account_id TEXT NOT NULL,
    account_name TEXT,
    -- org de CONTEXTE du binding (scope membre, ADR 0033 B4) : le compte n'est
    -- joignable que depuis cette org. Un même canal peut être connecté dans
    -- N orgs (PK composite).
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    -- le compte consomme un siège de la clé PLATEFORME (comptage/facturation
    -- par org — revendeur/passthrough). FALSE en BYO (l'user paie son instance).
    platform_seat BOOLEAN NOT NULL DEFAULT FALSE,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- SOFT disconnect : la ligne survit (disconnected_at posé) au lieu d'être
    -- supprimée. C'est la PREUVE DE PROPRIÉTÉ durable du compte : une reconnexion
    -- qui RÉUTILISE le même account_id chez Unipile (comportement observé) est
    -- rebindée déterministiquement au même sub — sans elle, l'heuristique du
    -- poll-and-bind (compte créé APRÈS le pending) rate toute réutilisation.
    disconnected_at TIMESTAMPTZ,
    PRIMARY KEY (sub, org_id, provider)
);
-- ⚠️ PAS d'index sur `org_id` ICI : la colonne est aussi posée par un
-- `ALTER … ADD COLUMN` de `_init.py`, donc elle peut MANQUER sur une base qui
-- existe déjà (le `CREATE TABLE IF NOT EXISTS` ci-dessus y est sauté) — et le
-- DDL assemblé s'exécute AVANT les ALTER. L'index vit là où il doit vivre :
-- dans `_init.py`, juste après l'ALTER (`docs/live-migrations.md`, § « Piège :
-- CREATE INDEX d'une NOUVELLE colonne »). Retiré le 2026-09-01 (#781) : il
-- était posé aux DEUX endroits, et celui-ci aurait tué le premier boot d'une
-- base antérieure à la colonne.

-- Corrélation hosted-auth (B3, voie webhook) : le `name` posé sur le lien Unipile
-- ne revient PAS dans /accounts → on pose un **nonce** aléatoire comme `name` et on
-- le mappe au sub. Au succès, Unipile POST {name=nonce, account_id} sur le webhook ;
-- on résout nonce→sub. Le nonce (non devinable, courte vie) sécurise un webhook
-- non authentifié. Consommé à la résolution, pruné après expiration.
CREATE TABLE IF NOT EXISTS unipile_pending (
    nonce TEXT PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    org_id BIGINT,                       -- org de contexte au connect (porté au compte)
    provider TEXT NOT NULL DEFAULT 'LINKEDIN',  -- canal demandé (B1, multi-canal)
    platform_seat BOOLEAN NOT NULL DEFAULT FALSE,  -- siège clé plateforme (ADR 0033 B4)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
