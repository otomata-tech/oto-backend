CREATE TABLE IF NOT EXISTS org_model_subscription_modes (
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
famille TEXT NOT NULL,
mode TEXT CHECK (mode IN ('personnel', 'pool')),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_by TEXT,
PRIMARY KEY (org_id, famille)
);
CREATE TABLE IF NOT EXISTS user_model_subscription_loans (
sub TEXT NOT NULL,
famille TEXT NOT NULL,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
servi_at TIMESTAMPTZ,
PRIMARY KEY (sub, famille, org_id)
);
CREATE INDEX IF NOT EXISTS idx_user_model_subscription_loans_org
ON user_model_subscription_loans(org_id, famille);
