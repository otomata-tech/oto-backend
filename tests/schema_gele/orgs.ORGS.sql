CREATE TABLE IF NOT EXISTS orgs (
id BIGSERIAL PRIMARY KEY,
name TEXT NOT NULL,
description TEXT NOT NULL DEFAULT '',
logo_url TEXT,
domain TEXT,
industry TEXT NOT NULL DEFAULT '',
location TEXT NOT NULL DEFAULT '',
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
suspended_at TIMESTAMPTZ,
suspended_by TEXT,
suspended_reason TEXT
);
