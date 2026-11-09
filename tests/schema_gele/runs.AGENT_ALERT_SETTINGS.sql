CREATE TABLE IF NOT EXISTS agent_alert_settings (
org_id BIGINT PRIMARY KEY,
enabled BOOLEAN NOT NULL DEFAULT TRUE,
channel_id TEXT,
channel_name TEXT,
dm_user TEXT,
dm_name TEXT,
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
