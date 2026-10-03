CREATE TABLE IF NOT EXISTS process_shares (
id BIGSERIAL PRIMARY KEY,
instruction_id BIGINT NOT NULL,
org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
token TEXT NOT NULL UNIQUE,
show_readers BOOLEAN NOT NULL DEFAULT TRUE,
preview_shape JSONB,
shape_version INTEGER,
preview_views JSONB NOT NULL DEFAULT '{}'::jsonb,
created_by TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_process_shares_actif
ON process_shares(instruction_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_process_shares_instruction
ON process_shares(instruction_id);
CREATE TABLE IF NOT EXISTS process_share_readers (
share_id BIGINT NOT NULL REFERENCES process_shares(id) ON DELETE CASCADE,
reader_sub TEXT NOT NULL,
first_read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
last_read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
reads INTEGER NOT NULL DEFAULT 1,
recorded BOOLEAN NOT NULL,
copied_at TIMESTAMPTZ,
copied_instruction_id BIGINT,
digested_at TIMESTAMPTZ,
PRIMARY KEY (share_id, reader_sub)
);
CREATE INDEX IF NOT EXISTS idx_process_share_readers_sub
ON process_share_readers(reader_sub);
CREATE TABLE IF NOT EXISTS process_readers_digest_optouts (
sub TEXT PRIMARY KEY,
source TEXT NOT NULL DEFAULT 'link',
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
