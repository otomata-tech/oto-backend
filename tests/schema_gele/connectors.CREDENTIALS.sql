CREATE TABLE IF NOT EXISTS connector_credentials (
entity_type TEXT NOT NULL,
entity_id TEXT NOT NULL,
connector TEXT NOT NULL,
account TEXT NOT NULL DEFAULT '',
secret_enc TEXT NOT NULL,
secret_kind TEXT NOT NULL DEFAULT 'api_key',
meta JSONB NOT NULL DEFAULT '{}',
set_by TEXT,
set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
version INTEGER NOT NULL DEFAULT 1,
share_down JSONB NOT NULL DEFAULT '[]',
share_mode TEXT NOT NULL DEFAULT 'open',
PRIMARY KEY (entity_type, entity_id, connector, account)
);
CREATE INDEX IF NOT EXISTS idx_conn_cred_entity ON connector_credentials(entity_type, entity_id);
