"""DDL du budget d'unités : barème, payeurs, soldes, réservations et mouvements.

Le commerce ÉCRIT (barème, versements), le cœur RELIT et règle à chaque appel payant
(`docs/credits-au-moment-de-l-appel.md`). ⚠️ **Pas d'argent ici** : aucune colonne de
montant en monnaie, de devise, de prix, de facture ni de moyen de paiement — une unité
de budget est un ENTIER opaque, que seul le commerce convertit. Le gel le vérifie
(`tests/test_credits_schema_sans_argent.py`).
"""
from __future__ import annotations

CREDITS = """
-- BARÈME versé par le commerce : une version entière, posée d'un bloc, jamais
-- réécrite. La version la plus haute fait foi ; chaque réservation note celle
-- qu'elle a lue. `budget` = unités de budget par paquet de `per_n` unités de la
-- fonction (un tarif qui ne tombe pas juste s'exprime par paquet ; la réservation
-- et le règlement s'arrondissent au plafond).
CREATE TABLE IF NOT EXISTS credit_scales (
    version BIGINT PRIMARY KEY CHECK (version > 0),
    posed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS credit_scale_lines (
    version BIGINT NOT NULL REFERENCES credit_scales(version) ON DELETE CASCADE,
    connector TEXT NOT NULL,
    function TEXT NOT NULL,
    unit TEXT NOT NULL,
    budget BIGINT NOT NULL CHECK (budget >= 0),
    per_n BIGINT NOT NULL DEFAULT 1 CHECK (per_n > 0),
    PRIMARY KEY (version, connector, function, unit)
);

-- QUI PAIE dans une org : `org` (défaut, sans ligne) ou `person`. Une donnée versée
-- par le commerce, jamais une règle du moteur.
CREATE TABLE IF NOT EXISTS credit_payer_modes (
    org_id INTEGER PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('org', 'person')),
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Le SOLDE d'un payeur (`org:<id>` ou `user:<sub>`, chaîne opaque) : `balance` =
-- versé − réglé ; `holds_open` = réservé et pas encore réglé. Disponible =
-- balance − holds_open. Une ligne par payeur : la réservation la verrouille, ce qui
-- sérialise les appels d'un même payeur et rien d'autre.
CREATE TABLE IF NOT EXISTS credit_balances (
    payer TEXT PRIMARY KEY,
    balance BIGINT NOT NULL DEFAULT 0,
    holds_open BIGINT NOT NULL DEFAULT 0 CHECK (holds_open >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Une RÉSERVATION par appel (`call_uid`, idempotente) : le pire cas, au barème de
-- `scale_version`, jusqu'au règlement, à la libération ou à l'échéance. `job`
-- porte la relève d'un travail asynchrone (fonction, arguments, délai).
CREATE TABLE IF NOT EXISTS credit_holds (
    call_uid TEXT PRIMARY KEY,
    payer TEXT NOT NULL,
    connector TEXT NOT NULL,
    function TEXT NOT NULL,
    unit TEXT NOT NULL,
    scale_version BIGINT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    reserved BIGINT NOT NULL CHECK (reserved >= 0),
    deadline TIMESTAMPTZ NOT NULL,
    job JSONB,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'settled', 'released', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_credit_holds_ouvertes
    ON credit_holds(deadline) WHERE state = 'open';
-- La réservation qu'attend la relève d'un travail asynchrone (`job`).
CREATE INDEX IF NOT EXISTS idx_credit_holds_releve
    ON credit_holds((job->>'function'), (job->'args'))
    WHERE state = 'open' AND job IS NOT NULL;

-- Les MOUVEMENTS, tous payeurs : versements du commerce, règlements (une échéance
-- est un règlement marqué) et transferts (deux comptes d'une même personne fusionnés :
-- le journal ne se réécrit pas, il se complète). Une réservation et sa libération
-- n'en laissent pas : elles ne consomment rien, `credit_holds` les porte.
-- `amount` = effet sur `balance`, `held` = effet sur `holds_open`. `xid` = la transaction qui l'a validé : le curseur du commerce ne
-- sert que les mouvements sous l'horizon `pg_snapshot_xmin`, triés par (xid, id),
-- et n'en manque donc aucun. `idempotency_key` est unique PAR PAYEUR.
CREATE TABLE IF NOT EXISTS credit_movements (
    id BIGSERIAL PRIMARY KEY,
    xid XID8 NOT NULL DEFAULT pg_current_xact_id(),
    payer TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('deposit', 'settle', 'transfer')),
    amount BIGINT NOT NULL DEFAULT 0,
    held BIGINT NOT NULL DEFAULT 0,
    reason TEXT,
    idempotency_key TEXT,
    call_uid TEXT,
    connector TEXT,
    function TEXT,
    quantity BIGINT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_movements_idempotence
    ON credit_movements(payer, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_credit_movements_curseur
    ON credit_movements(xid, id);
"""

# QUI a agi pour une réservation — la personne, pas le payeur. Le payeur est l'org par
# défaut (`credit_payer_modes`) : sans cette colonne, un mouvement réglé ne dit plus
# quelle personne l'a consommé, et le commerce ne peut pas tenir une limite de dépense
# PAR PERSONNE (`docs/limites-de-depense.md`). Posée à la réservation, jamais réécrite ;
# NULL = réservation d'avant la colonne, ou sans personne (travail sans auteur).
ACTEUR_DES_RESERVATIONS = """
ALTER TABLE credit_holds ADD COLUMN IF NOT EXISTS acting_sub TEXT;
"""

# Les BLOCAGES DU JOUR poussés par le commerce : une personne (`sub`) ou toute l'org
# (`sub = ''`) a atteint une limite de dépense qu'un admin de l'org a fixée. Le cœur ne
# compte rien et ne connaît aucune limite : il APPLIQUE la liste, et seulement sur les
# clés que l'opérateur refacture. `until` = la remise à zéro : passé cet instant, la
# ligne ne bloque plus, même si le commerce ne l'a pas retirée. Une ligne par (org,
# personne) : le commerce remplace la liste d'une org d'un bloc.
SPEND_BLOCKS = """
CREATE TABLE IF NOT EXISTS spend_blocks (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    sub TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL CHECK (scope IN ('org', 'group', 'member')),
    group_id BIGINT,
    until TIMESTAMPTZ NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, sub),
    CHECK ((sub = '') = (scope = 'org')),
    CHECK ((group_id IS NOT NULL) = (scope = 'group'))
);
"""
