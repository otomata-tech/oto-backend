CREATE TABLE IF NOT EXISTS subscription_states (
org_id BIGINT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
status TEXT NOT NULL CHECK (status IN ('none', 'incomplete', 'active', 'past_due', 'canceled')),
block_code TEXT,
block_since TIMESTAMPTZ,
current_period_end TIMESTAMPTZ,
grace_until TIMESTAMPTZ,
canceled_at TIMESTAMPTZ,
emitted_at TIMESTAMPTZ NOT NULL,
received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CHECK ((block_code IS NULL) = (block_since IS NULL))
);
