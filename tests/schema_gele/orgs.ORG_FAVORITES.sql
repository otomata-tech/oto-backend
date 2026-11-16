CREATE TABLE IF NOT EXISTS org_favorites (
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
owner_sub TEXT REFERENCES users(sub) ON DELETE CASCADE,
kind TEXT NOT NULL CHECK (kind IN ('doc', 'table')),
ref_id BIGINT NOT NULL,
added_by TEXT,
added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT org_favorites_une_ligne UNIQUE NULLS NOT DISTINCT (org_id, owner_sub, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_org_favorites_owner ON org_favorites(owner_sub)
WHERE owner_sub IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_org_favorites_cible ON org_favorites(kind, ref_id);
CREATE INDEX IF NOT EXISTS idx_org_favorites_added_by ON org_favorites(added_by)
WHERE added_by IS NOT NULL;
