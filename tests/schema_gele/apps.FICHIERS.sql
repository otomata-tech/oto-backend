CREATE TABLE IF NOT EXISTS app_files (
id BIGSERIAL PRIMARY KEY,
app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
s3_key TEXT NOT NULL,
name TEXT NOT NULL,
media_type TEXT NOT NULL,
size BIGINT NOT NULL,
text TEXT,
text_status TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS app_files_app_sub_idx ON app_files (app_id, sub, id DESC);
CREATE INDEX IF NOT EXISTS app_files_sub_idx ON app_files (sub);
