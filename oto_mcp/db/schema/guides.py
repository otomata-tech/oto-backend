"""DDL du domaine « prose de plateforme » — fragment assemblé par `db/_schema.py`.

Il reste ici `platform_instructions` (blocs A/B, #50) et, depuis le 03/11/2026,
`guide_revisions` (les versions gardées d'une couche de contexte) : la table `guides`
a été retirée du code le 23/09/2026 (oto#239), ses lignes vivent dans `nodes`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# instructions plateforme (#50). Les guides, eux, sont des nœuds (lot M1).
GUIDES = """
-- Instructions injectées AU NIVEAU PLATEFORME (#50, bloc A « secret sauce » +
-- bloc B « onboarding »). Singleton par `key` ('secret_sauce' | 'onboarding').
-- Éditable seulement par l'admin plateforme (inviolable par l'org — frontière
-- plateforme/org nette). Seedé au boot depuis les constantes de `instructions.py`
-- (INSERT ON CONFLICT DO NOTHING) → le code reste le défaut/fallback, la DB porte
-- l'override éditable. En CLAIR (prose, pas un credential).
CREATE TABLE IF NOT EXISTS platform_instructions (
    key TEXT PRIMARY KEY,                       -- 'secret_sauce' (bloc A) | 'onboarding' (bloc B)
    body_md TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);

-- ⚠️ La table `guides` a été RETIRÉE DU CODE le 23/09/2026 (otomata-tech/oto#239).
-- Ses lignes vivent dans `nodes` depuis le lot M1 (blueprint ADR 0054/0063) : une
-- couche de contexte EST une page, `delivery` n'est qu'une propriété. Plus rien ici
-- ne la crée, ne l'altère, ne l'indexe ni ne la recopie — un `CREATE TABLE IF NOT
-- EXISTS` laissé en place la faisait RENAÎTRE au démarrage suivant un `DROP`, et le
-- boot continuait d'écrire dans une table que plus rien ne lit pour servir.
-- Une base servie la perd par la révision `0051_guides_retiree` : un retrait n'est
-- jamais au démarrage (docs/migrations-versionnees.md §5.3).
"""

# Les versions gardées d'une couche de contexte — fragment séparé (révision
# `20261103_1400_guide_revisions`, `db/guide_revisions.py`).
GUIDE_REVISIONS = """
-- Les VERSIONS d'une couche de contexte (guide à charger ou readme injecté, des nœuds
-- `kind='page'`) : l'état qu'une écriture REMPLACE ou qu'une suppression EMPORTE, gardé
-- dans la transaction même de l'écriture (`db/guides.py`). Une première écriture (rien
-- avant) n'en laisse pas ; une réécriture au même contenu non plus.
-- L'identité est la CLÉ NATURELLE de la couche (owner_type, owner_id, slug, delivery),
-- pas l'id du nœud : un guide supprimé n'a plus de nœud, et c'est alors que sa
-- dernière version compte. Donc pas de clé étrangère (même raison que le journal des
-- lignes de tableau). `owner_type`/`owner_id` : le vocabulaire de `nodes`.
-- `rev` = l'empreinte du contenu (`db.guide_rev`), celle que la lecture sert et que
-- `expected_rev` compare. `edited_by` = qui a fait le geste qui a remplacé cet état
-- (convention de `doc_revisions`), `reason` = lequel.
-- Rétention, au fil de l'écriture et sans tâche planifiée : une version reste si elle a
-- moins de 30 jours OU si elle est parmi les 50 plus récentes de sa couche.
-- Index en TÊTE sur `owner_id` et sur `edited_by` : la fusion de comptes les repointe
-- (`_SUB_COLUMNS`), et une colonne repointée sans index balaie la table.
CREATE TABLE IF NOT EXISTS guide_revisions (
    id BIGSERIAL PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    delivery TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '',
    rev TEXT NOT NULL,
    reason TEXT NOT NULL,
    edited_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT guide_revisions_reason_check
        CHECK (reason IN ('write', 'init', 'delete', 'restore'))
);
CREATE INDEX IF NOT EXISTS idx_guide_revisions_couche
    ON guide_revisions(owner_id, owner_type, slug, delivery, id DESC);
CREATE INDEX IF NOT EXISTS idx_guide_revisions_edited_by ON guide_revisions(edited_by);
"""
