CREATE TABLE IF NOT EXISTS recipes (
id BIGSERIAL PRIMARY KEY,
owner_type TEXT NOT NULL,
owner_id TEXT NOT NULL,
slug TEXT NOT NULL,
title TEXT NOT NULL,
description TEXT NOT NULL DEFAULT '',
published_version INTEGER,
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (owner_type, owner_id, slug)
);
CREATE TABLE IF NOT EXISTS recipe_versions (
id BIGSERIAL PRIMARY KEY,
recipe_id BIGINT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
version INTEGER NOT NULL,
status TEXT NOT NULL DEFAULT 'proposee'
CHECK (status IN ('proposee', 'publiee', 'refusee')),
body JSONB NOT NULL,
note TEXT,
proposed_by TEXT,
proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
decided_by TEXT,
decided_at TIMESTAMPTZ,
test_report JSONB,
UNIQUE (recipe_id, version)
);
