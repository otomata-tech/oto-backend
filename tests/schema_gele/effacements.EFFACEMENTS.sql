CREATE TABLE IF NOT EXISTS org_erasures (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
state TEXT NOT NULL DEFAULT 'requested'
CHECK (state IN ('requested', 'scheduled', 'running', 'done', 'cancelled',
'expired', 'failed')),
via TEXT NOT NULL CHECK (via IN ('owner', 'operator')),
reason TEXT,
requested_by TEXT NOT NULL,
requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
token_hash TEXT,
token_expires_at TIMESTAMPTZ,
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
snapshot JSONB,
plan JSONB,
receipt JSONB,
commerce_erased_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_erasures_vivante
ON org_erasures (org_id) WHERE state IN ('requested', 'scheduled', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_erasures_jeton
ON org_erasures (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_org_erasures_echeance
ON org_erasures (scheduled_for) WHERE state = 'scheduled';
