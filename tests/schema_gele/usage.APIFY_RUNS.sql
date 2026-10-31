CREATE TABLE IF NOT EXISTS apify_runs (
apify_run_id TEXT PRIMARY KEY,
org_id BIGINT,
sub TEXT,
outil TEXT NOT NULL,
actor_id TEXT NOT NULL,
run_id TEXT,
plafond_usd NUMERIC,
lance_le TIMESTAMPTZ NOT NULL DEFAULT NOW(),
releve_le TIMESTAMPTZ,
statut TEXT,
cout_usd NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_apify_runs_a_relever
ON apify_runs(lance_le) WHERE releve_le IS NULL;
CREATE INDEX IF NOT EXISTS idx_apify_runs_lance_le ON apify_runs(lance_le);
CREATE INDEX IF NOT EXISTS idx_apify_runs_sub ON apify_runs(sub);
