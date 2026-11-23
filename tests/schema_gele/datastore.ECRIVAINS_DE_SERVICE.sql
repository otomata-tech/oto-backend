CREATE TABLE IF NOT EXISTS datastore_service_writers (
ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
role TEXT NOT NULL,
granted_by TEXT NOT NULL,
granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
PRIMARY KEY (ns_id, role)
);
CREATE INDEX IF NOT EXISTS idx_datastore_service_writers_granted_by
ON datastore_service_writers (granted_by);
