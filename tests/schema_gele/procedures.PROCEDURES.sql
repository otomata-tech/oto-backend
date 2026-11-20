CREATE TABLE IF NOT EXISTS org_instructions (
org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
owner_type TEXT NOT NULL DEFAULT 'org',
owner_id TEXT NOT NULL,
slug TEXT NOT NULL,
title TEXT NOT NULL DEFAULT '',
description TEXT NOT NULL DEFAULT '',
body_md TEXT NOT NULL,
slots JSONB NOT NULL DEFAULT '[]'::jsonb,
version INTEGER NOT NULL DEFAULT 1,
set_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
owner_sub TEXT,
CONSTRAINT org_instructions_owner_pkey PRIMARY KEY (owner_type, owner_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_instructions_org ON org_instructions(org_id);
CREATE TABLE IF NOT EXISTS org_instruction_revisions (
org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
owner_type TEXT NOT NULL DEFAULT 'org',
owner_id TEXT NOT NULL,
slug TEXT NOT NULL,
version INTEGER NOT NULL,
title TEXT NOT NULL DEFAULT '',
description TEXT NOT NULL DEFAULT '',
body_md TEXT NOT NULL,
slots JSONB NOT NULL DEFAULT '[]'::jsonb,
set_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT org_instruction_revisions_owner_pkey PRIMARY KEY (owner_type, owner_id, slug, version)
);
CREATE TABLE IF NOT EXISTS doctrine_library (
id BIGSERIAL PRIMARY KEY,
slug TEXT NOT NULL,
title TEXT NOT NULL DEFAULT '',
description TEXT NOT NULL DEFAULT '',
body_md TEXT NOT NULL,
slots JSONB NOT NULL DEFAULT '[]'::jsonb,
author_kind TEXT NOT NULL,
author_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL,
author_display TEXT NOT NULL DEFAULT '',
category TEXT NOT NULL DEFAULT '',
tags TEXT[] NOT NULL DEFAULT '{}',
visibility TEXT NOT NULL DEFAULT 'public',
source_org_id BIGINT,
source_slug TEXT,
forked_from BIGINT REFERENCES doctrine_library(id) ON DELETE SET NULL,
version INTEGER NOT NULL DEFAULT 1,
published_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (slug)
);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_visibility ON doctrine_library(visibility);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_author ON doctrine_library(author_kind, author_org_id);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_category ON doctrine_library(category);
