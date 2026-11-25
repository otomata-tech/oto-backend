CREATE TABLE IF NOT EXISTS app_library (
id BIGSERIAL PRIMARY KEY,
slug TEXT NOT NULL UNIQUE,
title TEXT NOT NULL DEFAULT '',
description TEXT NOT NULL DEFAULT '',
category TEXT NOT NULL DEFAULT '',
tags TEXT[] NOT NULL DEFAULT '{}',
thumbnail JSONB,
html TEXT NOT NULL,
manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
tables JSONB NOT NULL DEFAULT '{}'::jsonb,
author_kind TEXT NOT NULL CHECK (author_kind IN ('otomata', 'org')),
author_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL,
author_display TEXT NOT NULL DEFAULT '',
visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'unlisted')),
source_app_id BIGINT,
version INTEGER NOT NULL DEFAULT 1,
published_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_app_library_author ON app_library (author_kind, author_org_id);
CREATE INDEX IF NOT EXISTS idx_app_library_category ON app_library (category);
CREATE INDEX IF NOT EXISTS idx_app_library_published_by
ON app_library (published_by) WHERE published_by IS NOT NULL;
