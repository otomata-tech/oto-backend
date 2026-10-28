CREATE TABLE IF NOT EXISTS app_consents (
id BIGSERIAL PRIMARY KEY,
token_hash TEXT NOT NULL UNIQUE,
app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
action TEXT NOT NULL,
fingerprint TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ NOT NULL,
used_at TIMESTAMPTZ,
geste_id TEXT,
job_id BIGINT
);
CREATE INDEX IF NOT EXISTS app_consents_app_idx ON app_consents (app_id, id DESC);
CREATE INDEX IF NOT EXISTS app_consents_sub_idx ON app_consents (sub);
