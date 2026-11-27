CREATE TABLE IF NOT EXISTS images_connues (
empreinte TEXT PRIMARY KEY,
url TEXT NOT NULL,
origine TEXT NOT NULL,
etat TEXT NOT NULL DEFAULT 'a_copier',
essais INT NOT NULL DEFAULT 0,
detail TEXT,
octets INT,
type_image TEXT,
appris_le TIMESTAMPTZ NOT NULL DEFAULT NOW(),
prochain_essai TIMESTAMPTZ NOT NULL DEFAULT NOW(),
reservee_jusqu_a TIMESTAMPTZ,
copiee_le TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS images_connues_a_copier_idx
ON images_connues (prochain_essai) WHERE etat = 'a_copier';
