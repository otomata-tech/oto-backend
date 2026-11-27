CREATE TABLE IF NOT EXISTS app_table_access (
app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'org')),
principal_id TEXT NOT NULL,
permission TEXT NOT NULL CHECK (permission IN ('read', 'write')),
share_id BIGINT NOT NULL,
granted_by TEXT,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (app_id, ns_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS app_table_access_principal_idx ON app_table_access (principal_id);
CREATE INDEX IF NOT EXISTS app_table_access_granted_by_idx ON app_table_access (granted_by);
