ALTER TABLE apps ADD COLUMN IF NOT EXISTS owner_type TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS context_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL;
ALTER TABLE apps ALTER COLUMN project_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS apps_owner_idx ON apps (owner_type, owner_id);
CREATE INDEX IF NOT EXISTS apps_owner_id_idx ON apps (owner_id);
CREATE INDEX IF NOT EXISTS apps_context_org_idx ON apps (context_org_id);
CREATE TABLE IF NOT EXISTS app_projects (
app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
filed_by TEXT,
filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (app_id, project_id)
);
CREATE INDEX IF NOT EXISTS app_projects_project_idx ON app_projects (project_id);
CREATE INDEX IF NOT EXISTS app_projects_filed_by_idx ON app_projects (filed_by);
