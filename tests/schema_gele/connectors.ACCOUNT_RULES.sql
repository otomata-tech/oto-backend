CREATE TABLE IF NOT EXISTS connector_account_rules (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
instance_id BIGINT NOT NULL REFERENCES connector_instances(id) ON DELETE CASCADE,
subject_user TEXT REFERENCES users(sub) ON DELETE CASCADE,
subject_group BIGINT REFERENCES org_groups(id) ON DELETE CASCADE,
subject_org BOOLEAN NOT NULL DEFAULT FALSE,
level TEXT NOT NULL,
services TEXT[],
origine TEXT NOT NULL DEFAULT 'manuelle',
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT connector_account_rules_level_check
CHECK (level IN ('none', 'read', 'write', 'sensitive', 'manage')),
CONSTRAINT connector_account_rules_one_subject
CHECK (num_nonnulls(subject_user, subject_group) + subject_org::int = 1),
CONSTRAINT connector_account_rules_origine_check
CHECK (origine IN ('manuelle', 'reprise')),
CONSTRAINT connector_account_rules_instance_subject_key
UNIQUE NULLS NOT DISTINCT (instance_id, subject_user, subject_group, subject_org)
);
CREATE INDEX IF NOT EXISTS idx_account_rules_org ON connector_account_rules (org_id);
CREATE INDEX IF NOT EXISTS idx_account_rules_subject_user
ON connector_account_rules (subject_user) WHERE subject_user IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_account_rules_created_by
ON connector_account_rules (created_by) WHERE created_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_account_rules_subject_group
ON connector_account_rules (subject_group) WHERE subject_group IS NOT NULL;
CREATE TABLE IF NOT EXISTS connector_account_share_events (
id BIGSERIAL PRIMARY KEY,
at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
org_id BIGINT NOT NULL,
instance_id BIGINT NOT NULL REFERENCES connector_instances(id) ON DELETE CASCADE,
action TEXT NOT NULL,
subject_kind TEXT NOT NULL,
subject_id TEXT,
level TEXT NOT NULL,
previous_level TEXT,
services TEXT[],
actor_sub TEXT,
CONSTRAINT connector_account_share_events_action_check
CHECK (action IN ('set', 'removed')),
CONSTRAINT connector_account_share_events_subject_kind_check
CHECK (subject_kind IN ('user', 'group', 'org'))
);
CREATE INDEX IF NOT EXISTS idx_account_share_events_instance
ON connector_account_share_events (instance_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_account_share_events_org
ON connector_account_share_events (org_id);
CREATE INDEX IF NOT EXISTS idx_account_share_events_actor
ON connector_account_share_events (actor_sub) WHERE actor_sub IS NOT NULL;
