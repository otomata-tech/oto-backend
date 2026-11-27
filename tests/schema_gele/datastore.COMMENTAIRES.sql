CREATE TABLE IF NOT EXISTS datastore_row_comments (
id BIGSERIAL PRIMARY KEY,
ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
row_id TEXT,
column_key TEXT,
auteur_sub TEXT,
body TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
deleted_at TIMESTAMPTZ,
CONSTRAINT datastore_row_comments_cible CHECK (row_id IS NOT NULL OR column_key IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_datastore_row_comments_ligne
ON datastore_row_comments (ns_id, row_id, id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_datastore_row_comments_auteur
ON datastore_row_comments (auteur_sub);
