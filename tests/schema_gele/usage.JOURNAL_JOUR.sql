CREATE TABLE IF NOT EXISTS journal_jours_consolides (
jour DATE PRIMARY KEY,
lignes BIGINT NOT NULL,
consolide_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS journal_totaux_jour (
jour DATE NOT NULL REFERENCES journal_jours_consolides(jour) ON DELETE CASCADE,
kind TEXT NOT NULL,
org_id BIGINT,
sub TEXT,
tool TEXT NOT NULL,
ok BOOLEAN NOT NULL,
key_mode TEXT,
client_name TEXT,
appels INTEGER NOT NULL,
quantite BIGINT NOT NULL,
duree_n INTEGER NOT NULL,
duree_somme BIGINT NOT NULL,
durees INTEGER[] NOT NULL,
taille_n INTEGER NOT NULL,
taille_somme BIGINT NOT NULL,
tailles INTEGER[] NOT NULL,
dernier_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_jour ON journal_totaux_jour (jour);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_org ON journal_totaux_jour (org_id, jour);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_sub ON journal_totaux_jour (sub, jour);
CREATE TABLE IF NOT EXISTS journal_jobs_jour (
jour DATE NOT NULL REFERENCES journal_jours_consolides(jour) ON DELETE CASCADE,
org_id BIGINT NOT NULL,
tool TEXT NOT NULL,
key_mode TEXT,
job_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_jobs_jour_org ON journal_jobs_jour (org_id, jour);
