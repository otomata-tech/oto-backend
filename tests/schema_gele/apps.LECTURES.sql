CREATE TABLE IF NOT EXISTS app_reads (
app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
day DATE NOT NULL,
ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
sub TEXT NOT NULL,
access TEXT NOT NULL CHECK (access IN ('own', 'app', 'run')),
row_reads INTEGER NOT NULL DEFAULT 0,
aggregate_reads INTEGER NOT NULL DEFAULT 0,
rows_served BIGINT NOT NULL DEFAULT 0,
first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
last_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (app_id, day, ns_id, sub, access)
);
CREATE INDEX IF NOT EXISTS app_reads_ns_idx ON app_reads (ns_id, day);
CREATE INDEX IF NOT EXISTS app_reads_day_idx ON app_reads (day);
CREATE INDEX IF NOT EXISTS app_reads_sub_idx ON app_reads (sub);
