"""YouTube : la file des mises en ligne d'une vidéo DÉPOSÉE sur un projet.

Une vidéo de plusieurs Go (déposée par parties, `large_uploads`) ne tient ni dans la
mémoire du backend ni dans les 120 s d'un appel d'outil : `youtube_act
op=upload_video` dépose un travail, et `youtube_upload_worker` relaie le fichier du
stockage vers YouTube un morceau à la fois, en reprenant là où YouTube s'est arrêté
— même après un redémarrage (la session reprenable vit ici, chiffrée).
"""
from __future__ import annotations

YOUTUBE_UPLOAD_JOBS = """
-- `file_id` désigne le fichier de projet (FK : un fichier supprimé emporte son
-- travail) ; aucune clé d'objet recopiée ici. `session_url_enc` : l'adresse de la
-- session reprenable de YouTube, CHIFFRÉE (même enveloppe que le coffre) — elle
-- désigne un envoi en cours sur la chaîne. `next_offset` : le prochain octet que
-- YouTube attend, tel qu'IL le dit (jamais notre propre compte).
-- `org_id`, `project_id` et `account` : le contexte d'appel figé au dépôt, que le
-- worker repose pour résoudre le MÊME compte Google, hors de tout appel.
CREATE TABLE IF NOT EXISTS youtube_upload_jobs (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_id BIGINT NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    org_id BIGINT,
    account TEXT,
    -- pending | running | done | failed
    status TEXT NOT NULL DEFAULT 'pending',
    total_bytes BIGINT NOT NULL,
    mime TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    notify_subscribers BOOLEAN NOT NULL DEFAULT TRUE,
    session_url_enc TEXT,
    next_offset BIGINT NOT NULL DEFAULT 0,
    video_id TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- `sub` : la fusion de comptes (`migrate_sub`) repointe ses lignes par cette colonne.
CREATE INDEX IF NOT EXISTS idx_youtube_upload_jobs_sub ON youtube_upload_jobs(sub);
-- Partiel : la population que le worker regarde à chaque tour.
CREATE INDEX IF NOT EXISTS idx_youtube_upload_jobs_ouverts
    ON youtube_upload_jobs(created_at) WHERE status IN ('pending', 'running');
"""
