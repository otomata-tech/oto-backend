CREATE TABLE IF NOT EXISTS resource_grants (
resource_type TEXT NOT NULL,
resource_id TEXT NOT NULL,
principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'org')),
principal_id TEXT NOT NULL,
permission TEXT NOT NULL DEFAULT 'write' CHECK (permission IN ('read', 'write')),
role TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('viewer', 'contributor', 'editor', 'manager')),
granted_by TEXT,
granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
expires_at TIMESTAMPTZ,
PRIMARY KEY (resource_type, resource_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_grants_principal
ON resource_grants(principal_type, principal_id, resource_type);
CREATE TABLE IF NOT EXISTS grants (
id BIGSERIAL PRIMARY KEY,
resource_kind TEXT NOT NULL DEFAULT 'connector_instance',
resource_id TEXT NOT NULL,
grantor_kind TEXT NOT NULL,
grantor_id TEXT NOT NULL,
grantee_kind TEXT NOT NULL,
grantee_id TEXT NOT NULL,
constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
parent_id BIGINT REFERENCES grants(id),
source TEXT NOT NULL DEFAULT 'manual',
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
revoked_at TIMESTAMPTZ,
CONSTRAINT grants_grantor_kind_check
CHECK (grantor_kind IN ('platform', 'tenant', 'org', 'group', 'user')),
CONSTRAINT grants_grantee_kind_check
CHECK (grantee_kind IN ('platform', 'tenant', 'org', 'group', 'user')),
CONSTRAINT grants_constraints_vocabulary CHECK (
constraints - ARRAY['role', 'quota', 'budget', 'rate', 'expiration'] = '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS idx_grants_grantee
ON grants(grantee_kind, grantee_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_grants_parent ON grants(parent_id);
CREATE INDEX IF NOT EXISTS idx_grants_resource_grantee
ON grants(resource_id, grantee_kind, grantee_id);
CREATE TABLE IF NOT EXISTS grant_counters (
grant_id BIGINT NOT NULL REFERENCES grants(id),
window_start DATE NOT NULL,
calls BIGINT NOT NULL DEFAULT 0,
spend NUMERIC NOT NULL DEFAULT 0,
CONSTRAINT grant_counters_pkey PRIMARY KEY (grant_id, window_start)
);
CREATE TABLE IF NOT EXISTS access_shadow_l7 (
day DATE NOT NULL,
connector TEXT NOT NULL,
org_id BIGINT NOT NULL DEFAULT 0,
classe TEXT NOT NULL,
n BIGINT NOT NULL DEFAULT 0,
first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
last_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
sample JSONB NOT NULL DEFAULT '{}'::jsonb
);
