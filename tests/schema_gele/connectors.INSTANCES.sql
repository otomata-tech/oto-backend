CREATE TABLE IF NOT EXISTS connector_settings (
scope_type TEXT NOT NULL,
scope_id TEXT NOT NULL,
connector TEXT NOT NULL,
key TEXT NOT NULL,
value TEXT NOT NULL,
set_by TEXT,
set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT connector_settings_scope_type_check
CHECK (scope_type IN ('platform', 'org', 'group')),
CONSTRAINT connector_settings_pkey
PRIMARY KEY (scope_type, scope_id, connector, key)
);
CREATE TABLE IF NOT EXISTS connector_instances (
id BIGSERIAL PRIMARY KEY,
connector TEXT NOT NULL,
owner_type TEXT NOT NULL,
owner_id TEXT NOT NULL,
account TEXT NOT NULL DEFAULT '',
label TEXT,
config JSONB NOT NULL DEFAULT '{}'::jsonb,
parent_id BIGINT REFERENCES connector_instances(id),
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
revoked_at TIMESTAMPTZ,
revoked_reason TEXT,
CONSTRAINT connector_instances_owner_type_check
CHECK (owner_type IN ('platform', 'tenant', 'org', 'group', 'member', 'user'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_instances_vault
ON connector_instances(owner_type, owner_id, connector, account)
WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_connector_instances_vault_all
ON connector_instances(owner_type, owner_id, connector, account);
CREATE INDEX IF NOT EXISTS idx_connector_instances_parent
ON connector_instances(parent_id);
