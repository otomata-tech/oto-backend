CREATE TABLE IF NOT EXISTS agent_causes (
cle TEXT PRIMARY KEY,
empreinte TEXT NOT NULL,
classe TEXT NOT NULL,
cote TEXT NOT NULL CHECK (cote IN ('plateforme', 'proprietaire', 'inconnu')),
statut TEXT NOT NULL DEFAULT 'proposee'
CHECK (statut IN ('proposee', 'approuvee', 'refusee')),
copie JSONB,
gestes JSONB NOT NULL DEFAULT '[]'::jsonb,
vue_premiere_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
