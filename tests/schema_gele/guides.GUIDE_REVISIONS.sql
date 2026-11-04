CREATE TABLE IF NOT EXISTS guide_revisions (
id BIGSERIAL PRIMARY KEY,
owner_type TEXT NOT NULL,
owner_id TEXT NOT NULL,
slug TEXT NOT NULL,
delivery TEXT NOT NULL,
title TEXT NOT NULL DEFAULT '',
description TEXT NOT NULL DEFAULT '',
body_md TEXT NOT NULL DEFAULT '',
rev TEXT NOT NULL,
reason TEXT NOT NULL,
edited_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT guide_revisions_reason_check
CHECK (reason IN ('write', 'init', 'delete', 'restore'))
);
CREATE INDEX IF NOT EXISTS idx_guide_revisions_couche
ON guide_revisions(owner_id, owner_type, slug, delivery, id DESC);
CREATE INDEX IF NOT EXISTS idx_guide_revisions_edited_by ON guide_revisions(edited_by);
