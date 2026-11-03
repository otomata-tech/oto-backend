CREATE TABLE IF NOT EXISTS copilot_slack_threads (
thread_id TEXT PRIMARY KEY REFERENCES copilot_threads(id) ON DELETE CASCADE,
team_id TEXT NOT NULL,
channel TEXT NOT NULL,
thread_ts TEXT NOT NULL,
sub TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_copilot_slack_threads_cle
ON copilot_slack_threads (team_id, channel, thread_ts, sub);
CREATE TABLE IF NOT EXISTS copilot_slack_events (
channel TEXT NOT NULL,
ts TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (channel, ts)
);
CREATE INDEX IF NOT EXISTS idx_copilot_slack_events_created
ON copilot_slack_events (created_at);
