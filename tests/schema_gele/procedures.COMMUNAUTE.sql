CREATE TABLE IF NOT EXISTS community_moderation (
kind TEXT NOT NULL CHECK (kind IN ('procedure', 'app')),
entry_id BIGINT NOT NULL,
state TEXT NOT NULL CHECK (state IN ('listed', 'removed')),
reason TEXT NOT NULL DEFAULT '',
removed_by TEXT,
removed_at TIMESTAMPTZ,
restored_by TEXT,
restored_at TIMESTAMPTZ,
PRIMARY KEY (kind, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_community_moderation_removed_by
ON community_moderation (removed_by) WHERE removed_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_community_moderation_restored_by
ON community_moderation (restored_by) WHERE restored_by IS NOT NULL;
CREATE TABLE IF NOT EXISTS community_installs (
id BIGSERIAL PRIMARY KEY,
kind TEXT NOT NULL CHECK (kind IN ('procedure', 'app')),
entry_id BIGINT NOT NULL,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
installed_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_community_installs_entry
ON community_installs (kind, entry_id, created_at);
CREATE INDEX IF NOT EXISTS idx_community_installs_org ON community_installs (org_id);
CREATE INDEX IF NOT EXISTS idx_community_installs_installed_by
ON community_installs (installed_by) WHERE installed_by IS NOT NULL;
