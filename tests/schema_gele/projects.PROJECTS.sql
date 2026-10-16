CREATE TABLE IF NOT EXISTS projects (
id BIGSERIAL PRIMARY KEY,
owner_type TEXT NOT NULL DEFAULT 'user',
owner_id TEXT NOT NULL,
name TEXT NOT NULL,
icon TEXT,
folder TEXT,
brief_md TEXT NOT NULL DEFAULT '',
created_by TEXT,
is_template BOOLEAN NOT NULL DEFAULT FALSE,
mcp_slug TEXT UNIQUE,
mcp_access TEXT NOT NULL DEFAULT 'off',
mcp_tools TEXT[] NOT NULL DEFAULT '{}',
mcp_expose_datastore BOOLEAN NOT NULL DEFAULT FALSE,
mcp_expose_datastore_write BOOLEAN NOT NULL DEFAULT FALSE,
mcp_expose_docs BOOLEAN NOT NULL DEFAULT FALSE,
mcp_instructions_md TEXT,
copied_from BIGINT,
archived_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS project_links (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
target_type TEXT NOT NULL,
target_ref TEXT NOT NULL,
label TEXT,
role TEXT,
slot TEXT,
config JSONB NOT NULL DEFAULT '{}',
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE(project_id, target_type, target_ref)
);
CREATE INDEX IF NOT EXISTS idx_project_links_project ON project_links(project_id);
CREATE TABLE IF NOT EXISTS docs (
id BIGSERIAL PRIMARY KEY,
project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
parent_id BIGINT REFERENCES docs(id) ON DELETE CASCADE,
title TEXT NOT NULL,
body_md TEXT NOT NULL DEFAULT '',
kind TEXT NOT NULL DEFAULT 'doc',
description TEXT,
position INTEGER,
public_token TEXT,
created_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project_id);
CREATE INDEX IF NOT EXISTS idx_docs_parent ON docs(parent_id);
CREATE TABLE IF NOT EXISTS doc_revisions (
id BIGSERIAL PRIMARY KEY,
doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
title TEXT NOT NULL,
body_md TEXT NOT NULL DEFAULT '',
edited_by TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
face TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_revisions_doc ON doc_revisions(doc_id, created_at DESC);
CREATE TABLE IF NOT EXISTS doc_change_requests (
id BIGSERIAL PRIMARY KEY,
doc_id BIGINT REFERENCES docs(id) ON DELETE CASCADE,
project_id BIGINT REFERENCES projects(id) ON DELETE CASCADE,
proposed_parent_id BIGINT REFERENCES docs(id) ON DELETE SET NULL,
proposed_kind TEXT,
requested_by TEXT,
proposed_title TEXT,
proposed_body_md TEXT NOT NULL DEFAULT '',
message TEXT,
status TEXT NOT NULL DEFAULT 'pending',
resolved_by TEXT,
resolved_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT dcr_target CHECK (doc_id IS NOT NULL OR project_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS doc_links (
from_doc BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
to_doc BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
PRIMARY KEY (from_doc, to_doc)
);
CREATE INDEX IF NOT EXISTS idx_doc_links_to ON doc_links(to_doc);
