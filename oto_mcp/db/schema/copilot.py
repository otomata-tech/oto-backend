"""DDL de l'agent intégré à l'application (« copilot », `docs/copilot.md`).

Trois tables, et seulement l'état d'une conversation en cours et la dépense du jour :

- `copilot_threads` : une conversation, LIÉE au compte qui l'a ouverte (`sub`) et à
  l'org consultée (`org_id`). Ses messages (forme de l'API du modèle, blocs de
  réflexion compris, rejoués tels quels), la file des appels d'outils du tour en cours,
  l'appel qui attend une réponse (une carte d'approbation, une action de page), les
  sortes de changement que la personne a dit de ne plus demander. `busy_*` = le verrou
  d'une étape : une seule à la fois par conversation, sur toutes les instances (bleu,
  vert, préprod — une seule base).
- `copilot_undos` : ce qu'un changement fait par l'agent sait défaire, une fois, avant
  son échéance.
- `copilot_spend` : ce que chaque compte a fait dépenser au modèle, par jour UTC, en
  millionièmes de dollar — le plafond quotidien (`capabilities/copilot/depense.py`).

Et, à part (`PIECES`), `copilot_attachments` : un fichier que la personne joint à un
message (image, PDF, petit texte). Ses octets sont dans le stockage objet, sous
`copilot-attachments/` ; la ligne dit à qui il est — `sub` ET `org_id` : personne
d'autre ne peut s'en servir.

Toutes se purgent à 30 jours par la maintenance (`oto-mcp maintenance copilot`).
Ce n'est PAS le journal : chaque appel d'outil est déjà dans `tool_calls`, sous le run
de la conversation. Ces tables se purgent sans rien amputer.
"""
from __future__ import annotations

COPILOT = """
-- Une conversation de l'agent intégré. `id` = `thr_<hex>`. Lue et reprise par son seul
-- `sub` : un autre compte reçoit « introuvable ».
CREATE TABLE IF NOT EXISTS copilot_threads (
    id TEXT PRIMARY KEY,
    sub TEXT NOT NULL,
    org_id BIGINT,
    project_id BIGINT,
    -- Le run ouvert pour la conversation ; NULL = clos (inactivité), rouvert au besoin.
    run_id TEXT,
    system TEXT NOT NULL DEFAULT '',
    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
    queue JSONB NOT NULL DEFAULT '[]'::jsonb,
    results JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending JSONB,
    remembered JSONB NOT NULL DEFAULT '[]'::jsonb,
    client_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- `ask` : chaque écriture attend la carte ; `act` : les écritures simples passent,
    -- envois, suppressions et lancements demandent toujours.
    mode TEXT NOT NULL DEFAULT 'ask',
    -- Appels au modèle déjà faits : le rang du suivant, clé de sa réservation.
    model_calls INTEGER NOT NULL DEFAULT 0,
    busy_token TEXT,
    busy_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_copilot_threads_sub ON copilot_threads (sub, last_seen);

-- Une annulation offerte. `kind` dit comment défaire, `payload` de quoi le faire.
CREATE TABLE IF NOT EXISTS copilot_undos (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES copilot_threads(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

-- La dépense du jour (UTC) d'un compte, en millionièmes de dollar : le plafond
-- quotidien de l'agent intégré.
CREATE TABLE IF NOT EXISTS copilot_spend (
    sub TEXT NOT NULL,
    day DATE NOT NULL,
    usd_micros BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (sub, day)
);
"""

PIECES = """
-- Un fichier joint par la personne à un message de l'agent intégré. `id` = `att_<hex>`.
-- Les octets sont dans le stockage objet (`s3_key`) ; n'en usent que `sub` dans `org_id`.
-- Aucune clé étrangère vers une conversation : la pièce est déposée AVANT le message.
CREATE TABLE IF NOT EXISTS copilot_attachments (
    id TEXT PRIMARY KEY,
    sub TEXT NOT NULL,
    org_id BIGINT,
    s3_key TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_copilot_attachments_created
    ON copilot_attachments (created_at);
"""

SLACK = """
-- Où vit, dans Slack, une conversation de l'agent intégré (`slack_copilot.py`) : un fil
-- d'un canal (`thread_ts` = le message de la mention) ou, en message direct, la
-- conversation courante du canal (`thread_ts` = ''). Une conversation reste À SON `sub` :
-- une autre personne qui mentionne l'app dans le même fil en ouvre une à elle. Un fil
-- sans ligne ici n'est la demande de personne, et un message qui y tombe est ignoré.
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

-- Les messages de Slack déjà pris en charge : une mention arrive DEUX fois (l'événement
-- `app_mention` et l'événement `message` du même message), un retry de Slack une de plus.
-- Clé `(channel, ts)` : un `ts` est unique dans son canal. Purgé avec l'agent intégré.
CREATE TABLE IF NOT EXISTS copilot_slack_events (
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel, ts)
);
CREATE INDEX IF NOT EXISTS idx_copilot_slack_events_created
    ON copilot_slack_events (created_at);
"""
