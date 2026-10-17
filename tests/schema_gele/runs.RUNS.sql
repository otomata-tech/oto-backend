CREATE TABLE IF NOT EXISTS runs (
run_id TEXT PRIMARY KEY,
sub TEXT,
org_id BIGINT,
project_id BIGINT,
label TEXT NOT NULL,
doctrine TEXT,
outcome TEXT,
note TEXT,
started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
finished_at TIMESTAMPTZ,
lignes_reservees INT
);
CREATE INDEX IF NOT EXISTS idx_runs_sub_org ON runs(sub, org_id, started_at DESC);
CREATE TABLE IF NOT EXISTS run_messages (
id BIGSERIAL PRIMARY KEY,
run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
seq INT NOT NULL,
role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
content JSONB NOT NULL,
provider_raw JSONB,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS runner_fleets (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
sub TEXT NOT NULL,
label TEXT NOT NULL,
procedure TEXT NOT NULL,
project_id BIGINT,
tools JSONB NOT NULL,
input TEXT,
max_steps INT,
max_run_seconds INT,
namespace TEXT,
row_filter JSONB,
provider TEXT,
model TEXT,
temperature REAL,
descriptions_outils JSONB,
workers INT NOT NULL DEFAULT 1,
rows_at_launch INT,
max_rows INT,
max_tokens BIGINT,
max_consecutive_failures INT,
max_tokens_per_row INT,
status TEXT NOT NULL DEFAULT 'draft'
CHECK (status IN ('draft', 'armed', 'running', 'stopping', 'stopped',
'done', 'failed')),
stop_reason TEXT,
armed_at TIMESTAMPTZ,
started_at TIMESTAMPTZ,
stopping_at TIMESTAMPTZ,
heartbeat_at TIMESTAMPTZ,
taken_by TEXT,
stopped_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_runner_fleets_org
ON runner_fleets(org_id, status);
CREATE TABLE IF NOT EXISTS runner_jobs (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
kind TEXT NOT NULL CHECK (kind IN ('start', 'continue')),
run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
payload JSONB,
status TEXT NOT NULL DEFAULT 'pending'
CHECK (status IN ('pending', 'held', 'claimed', 'done', 'failed', 'expired')),
attempts INT NOT NULL DEFAULT 0,
max_attempts INT NOT NULL DEFAULT 3,
claimed_by TEXT,
lease_until TIMESTAMPTZ,
last_error TEXT,
due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
finished_at TIMESTAMPTZ,
fleet_id BIGINT REFERENCES runner_fleets(id) ON DELETE SET NULL,
sub TEXT,
result JSONB
);
CREATE INDEX IF NOT EXISTS idx_runner_jobs_live
ON runner_jobs(org_id, due_at) WHERE status IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS idx_runner_jobs_expired
ON runner_jobs(org_id) WHERE status = 'expired';
CREATE TABLE IF NOT EXISTS runner_triggers (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
sub TEXT NOT NULL,
label TEXT,
folder TEXT,
procedure TEXT NOT NULL,
project_id BIGINT,
tools JSONB NOT NULL,
input TEXT,
max_steps INT,
max_tokens INT,
max_run_seconds INT,
model TEXT,
kind TEXT NOT NULL DEFAULT 'schedule',
cron TEXT,
tz TEXT NOT NULL DEFAULT 'Europe/Paris',
enabled BOOLEAN NOT NULL DEFAULT TRUE,
next_due TIMESTAMPTZ,
last_enqueued_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
hook_auth TEXT NOT NULL DEFAULT 'bearer',
hook_signing_secret_enc TEXT,
max_per_day INT,
hook_slug TEXT
);
CREATE INDEX IF NOT EXISTS idx_runner_triggers_due
ON runner_triggers(next_due) WHERE enabled;
CREATE TABLE IF NOT EXISTS runner_workers (
org_id BIGINT NOT NULL,
worker_sub TEXT NOT NULL,
last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (org_id, worker_sub)
);
CREATE TABLE IF NOT EXISTS runner_platform_workers (
worker_sub TEXT PRIMARY KEY,
last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS secret_hash TEXT UNIQUE;
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS attempt_errors JSONB
NOT NULL DEFAULT '[]'::jsonb;
CREATE TABLE IF NOT EXISTS runner_hook_deliveries (
id BIGSERIAL PRIMARY KEY,
trigger_id BIGINT NOT NULL,
org_id BIGINT NOT NULL,
received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
outcome TEXT NOT NULL,
job_id BIGINT,
due_at TIMESTAMPTZ,
source TEXT,
external_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_hook_deliveries_fenetre
ON runner_hook_deliveries(trigger_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_hook_deliveries_creneaux
ON runner_hook_deliveries(trigger_id, due_at DESC) WHERE due_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS runner_platform_depots (
worker_sub TEXT NOT NULL,
depot TEXT NOT NULL,
last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (worker_sub, depot)
);
CREATE TABLE IF NOT EXISTS user_model_subscriptions (
sub TEXT NOT NULL,
famille TEXT NOT NULL,
sandbox_id TEXT,
statut TEXT NOT NULL DEFAULT 'disconnected',
plan TEXT,
method TEXT,
limit_reset_at TIMESTAMPTZ,
last_ok_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
limite_pct SMALLINT CHECK (limite_pct BETWEEN 1 AND 100),
PRIMARY KEY (sub, famille)
);
