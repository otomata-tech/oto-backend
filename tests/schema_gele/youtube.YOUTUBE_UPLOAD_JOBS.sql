CREATE TABLE IF NOT EXISTS youtube_upload_jobs (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
file_id BIGINT NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
org_id BIGINT,
account TEXT,
status TEXT NOT NULL DEFAULT 'pending',
total_bytes BIGINT NOT NULL,
mime TEXT NOT NULL,
meta JSONB NOT NULL DEFAULT '{}'::jsonb,
notify_subscribers BOOLEAN NOT NULL DEFAULT TRUE,
session_url_enc TEXT,
next_offset BIGINT NOT NULL DEFAULT 0,
video_id TEXT,
error TEXT,
attempts INTEGER NOT NULL DEFAULT 0,
lease_until TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_youtube_upload_jobs_sub ON youtube_upload_jobs(sub);
CREATE INDEX IF NOT EXISTS idx_youtube_upload_jobs_ouverts
ON youtube_upload_jobs(created_at) WHERE status IN ('pending', 'running');
