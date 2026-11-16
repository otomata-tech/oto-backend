CREATE TABLE IF NOT EXISTS datastore_row_revisions (
id BIGSERIAL PRIMARY KEY,
ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
row_id TEXT NOT NULL,
rev BIGINT NOT NULL,
diff JSONB NOT NULL,
acteur TEXT,
run_id TEXT,
source TEXT,
geste_id TEXT,
at TIMESTAMPTZ NOT NULL DEFAULT now(),
suppression BOOLEAN NOT NULL DEFAULT false,
outil TEXT,
fournisseur TEXT
);
