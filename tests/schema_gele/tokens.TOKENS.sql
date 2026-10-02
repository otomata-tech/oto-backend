CREATE TABLE IF NOT EXISTS user_api_tokens (
id BIGSERIAL PRIMARY KEY,
sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
label TEXT NOT NULL DEFAULT 'cli',
token_hash TEXT NOT NULL UNIQUE,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
last_used_at TIMESTAMPTZ,
expires_at TIMESTAMPTZ,
scopes JSONB,
kind TEXT NOT NULL DEFAULT 'user',
revoked_at TIMESTAMPTZ,
revoked_by TEXT,
revoked_reason TEXT,
job_id BIGINT,
verrou_org BOOLEAN,
verrou_org_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_sub ON user_api_tokens(sub);
CREATE TABLE IF NOT EXISTS upload_tokens_used (
jti TEXT PRIMARY KEY,
used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
