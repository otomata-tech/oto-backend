CREATE TABLE IF NOT EXISTS org_incidents_capacite (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL,
famille TEXT NOT NULL,
cause TEXT NOT NULL CHECK (cause IN ('plafond', 'reconnexion', 'pool_vide')),
par_le_pool BOOLEAN NOT NULL DEFAULT false,
vu_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
revu_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
libre_at TIMESTAMPTZ,
ouvert_at TIMESTAMPTZ,
clos_at TIMESTAMPTZ,
travaux INTEGER NOT NULL DEFAULT 0,
agents INTEGER NOT NULL DEFAULT 0,
reprise_at TIMESTAMPTZ,
titulaires TEXT[] NOT NULL DEFAULT '{}',
perimees INTEGER,
repris BOOLEAN,
alerte_at TIMESTAMPTZ,
alerte_par TEXT CHECK (alerte_par IN ('slack', 'courriel')),
titulaires_prevenus TEXT[] NOT NULL DEFAULT '{}',
titulaires_at TIMESTAMPTZ,
retour_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_incidents_capacite_vivant
ON org_incidents_capacite(org_id, famille) WHERE clos_at IS NULL;
