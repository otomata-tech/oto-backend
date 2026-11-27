ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS app_role TEXT;
CREATE SEQUENCE IF NOT EXISTS resource_grants_share_id_seq;
ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS share_id BIGINT;
ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS app_scope JSONB;
