CREATE TABLE IF NOT EXISTS doc_comments (
id BIGSERIAL PRIMARY KEY,
doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
parent_id BIGINT REFERENCES doc_comments(id) ON DELETE CASCADE,
quote TEXT,
quote_prefix TEXT,
auteur_sub TEXT,
body TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
deleted_at TIMESTAMPTZ,
suggestion TEXT,
resolution TEXT,
resolved_by TEXT,
resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_doc_comments_page
ON doc_comments (doc_id, id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_doc_comments_parent
ON doc_comments (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_doc_comments_auteur
ON doc_comments (auteur_sub);
