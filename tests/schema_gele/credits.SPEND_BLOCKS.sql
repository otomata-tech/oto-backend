CREATE TABLE IF NOT EXISTS spend_blocks (
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
sub TEXT NOT NULL DEFAULT '',
scope TEXT NOT NULL CHECK (scope IN ('org', 'group', 'member')),
group_id BIGINT,
until TIMESTAMPTZ NOT NULL,
set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (org_id, sub),
CHECK ((sub = '') = (scope = 'org')),
CHECK ((group_id IS NOT NULL) = (scope = 'group'))
);
