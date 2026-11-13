"""DDL du domaine « effacements » — fragment du schéma assemblé par `db/_schema.py`.

L'effacement d'une org (RGPD art. 17, `docs/effacement-d-org.md`) : une demande, sa
confirmation par le titulaire, le délai de grâce, la purge et son reçu. Ce module ne
porte QUE du DDL ; la révision `20261112_1200_org_erasures` l'exécute tel quel.
"""
from __future__ import annotations

# Une ligne par DEMANDE d'effacement. Elle survit à l'org qu'elle efface : c'est la
# preuve (RGPD art. 5 §2, responsabilité) que la demande a été reçue, confirmée et
# exécutée, et quand. Ce qu'elle porte de personnel (le nom de l'org, l'adresse qui a
# confirmé, les comptes du périmètre) est RETIRÉ à la fin de la purge : le reçu ne
# garde que des comptes par table et des dates.
#
# Pas de clé étrangère vers `orgs` : la ligne `orgs` devient une pierre tombale sans
# rien de personnel, mais la demande ne doit dépendre d'aucune autre table pour
# rester lisible.
EFFACEMENTS = """
CREATE TABLE IF NOT EXISTS org_erasures (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL,
    state TEXT NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'scheduled', 'running', 'done', 'cancelled',
                         'expired', 'failed')),
    -- `owner` : un admin de l'org l'a demandé ; `operator` : un super admin, pour le
    -- compte du client, avec un motif écrit.
    via TEXT NOT NULL CHECK (via IN ('owner', 'operator')),
    reason TEXT,
    requested_by TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Le lien de confirmation : seule son EMPREINTE est gardée (sha256 du jeton).
    token_hash TEXT,
    token_expires_at TIMESTAMPTZ,
    -- Le titulaire qui doit confirmer, et l'adresse où le lien est parti. Retirés à la
    -- clôture (`done`, `cancelled`, `expired`).
    confirm_sub TEXT,
    confirm_email TEXT,
    org_name TEXT,
    confirmed_at TIMESTAMPTZ,
    scheduled_for TIMESTAMPTZ,
    cancelled_by TEXT,
    cancelled_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    -- Le périmètre FIGÉ à la confirmation (org, équipes, comptes qui partent) : la purge
    -- efface ce qui le désigne, elle ne peut donc pas le recalculer en chemin.
    snapshot JSONB,
    -- Ce qui serait effacé, conservé, anonymisé (comptes par table) à la confirmation ;
    -- puis ce qui l'a été.
    plan JSONB,
    receipt JSONB,
    -- Le commerce (facturation, crédits) efface sa part et le dit ici.
    commerce_erased_at TIMESTAMPTZ
);
-- Une seule demande VIVANTE par org.
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_erasures_vivante
    ON org_erasures (org_id) WHERE state IN ('requested', 'scheduled', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_erasures_jeton
    ON org_erasures (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_org_erasures_echeance
    ON org_erasures (scheduled_for) WHERE state = 'scheduled';
"""
