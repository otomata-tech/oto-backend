"""Le store de l'agent intégré (`docs/copilot.md`) : conversations et annulations.

Trois règles tenues ici plutôt qu'en prose :

1. **Une conversation appartient à son `sub`.** Toute lecture qui sert une requête passe
   par `lire(thread_id, sub)` : un autre compte reçoit `None`, exactement comme une
   conversation qui n'existe pas — pas d'oracle d'existence.
2. **Une étape à la fois** (`prendre` / `rendre`) : le verrou est une ligne de la base,
   pas un drapeau en mémoire — bleu, vert et préprod partagent la même base, deux
   réponses à la même carte peuvent arriver sur deux processus. Il a une échéance :
   un processus mort en pleine étape ne bloque pas la conversation à jamais.
3. **Une annulation se consomme UNE fois** (`consommer`), atomiquement, et pas après
   son échéance.
4. **Une pièce jointe est à son `sub` DANS son org** (`pieces`) : un autre compte, ou le
   même dans une autre org, ne la voit pas — même réponse qu'une pièce qui n'existe pas.
"""
from __future__ import annotations

import json
from typing import Optional

from ._conn import _connect

#: Les colonnes JSON d'une conversation que la boucle réécrit à chaque étape.
CHAMPS_JSON = ("messages", "queue", "results", "pending", "remembered", "client_tools")
_CHAMPS_SIMPLES = ("project_id", "run_id", "model_calls", "mode", "model")

#: Le modèle qui répond à une conversation (`capabilities/copilot/palier.py`) ; NULL = une
#: conversation d'avant, qui garde le modèle et les outils de son début. Le même DDL pour
#: le démarrage (`_init`, sous garde de catalogue) et pour la révision
#: `20261030_1000_copilot_model`. Nullable sans défaut : écriture de catalogue seule.
COLONNE_MODELE = "model"
DDL_COLONNE_MODELE = f"ALTER TABLE copilot_threads ADD COLUMN IF NOT EXISTS {COLONNE_MODELE} TEXT"


def creer(thread_id: str, sub: str, org_id: Optional[int], project_id: Optional[int],
          system: str, model: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO copilot_threads (id, sub, org_id, project_id, system, model) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (thread_id, sub, org_id, project_id, system, model))


def lire(thread_id: str, sub: str) -> Optional[dict]:
    """La conversation, si elle est à `sub` ; sinon `None` (même réponse qu'absente)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM copilot_threads WHERE id = %s AND sub = %s",
            (thread_id, sub)).fetchone()


def enregistrer(thread_id: str, champs: dict) -> None:
    """Réécrit l'état de la conversation (les seuls champs nommés) et la date de vie."""
    poses, valeurs = [], []
    for k, v in champs.items():
        if k in CHAMPS_JSON:
            poses.append(f"{k} = %s::jsonb")
            valeurs.append(json.dumps(v, ensure_ascii=False))
        elif k in _CHAMPS_SIMPLES:
            poses.append(f"{k} = %s")
            valeurs.append(v)
        else:
            raise ValueError(f"champ de conversation inconnu : {k}")
    poses.append("last_seen = NOW()")
    with _connect() as conn:
        conn.execute(f"UPDATE copilot_threads SET {', '.join(poses)} WHERE id = %s",
                     (*valeurs, thread_id))


def prendre(thread_id: str, jeton: str, secondes: int) -> bool:
    """Prend le verrou d'étape s'il est libre (ou échu). Vrai = pris."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE copilot_threads SET busy_token = %s, "
            "busy_until = NOW() + make_interval(secs => %s) "
            "WHERE id = %s AND (busy_until IS NULL OR busy_until < NOW()) RETURNING id",
            (jeton, secondes, thread_id)).fetchone()
    return row is not None


def rendre(thread_id: str, jeton: str) -> None:
    """Rend le verrou — seulement s'il est encore à ce jeton."""
    with _connect() as conn:
        conn.execute(
            "UPDATE copilot_threads SET busy_token = NULL, busy_until = NULL "
            "WHERE id = %s AND busy_token = %s", (thread_id, jeton))


def ouvertes_recentes(sub: str, minutes: int) -> int:
    """Combien de conversations de `sub` ont vécu ces `minutes` dernières."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM copilot_threads WHERE sub = %s "
            "AND last_seen > NOW() - make_interval(mins => %s)", (sub, minutes)).fetchone()
    return int(row["n"])


def inactives_avec_run(sub: str, minutes: int, limite: int = 20) -> list[dict]:
    """Les conversations de `sub` sans vie depuis `minutes` qui tiennent encore un run."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, run_id, org_id FROM copilot_threads WHERE sub = %s "
            "AND run_id IS NOT NULL AND last_seen < NOW() - make_interval(mins => %s) "
            "AND (busy_until IS NULL OR busy_until < NOW()) LIMIT %s",
            (sub, minutes, limite)).fetchall()


def oublier_run(thread_id: str, run_id: str) -> None:
    """La conversation ne tient plus ce run (il a été clos)."""
    with _connect() as conn:
        conn.execute("UPDATE copilot_threads SET run_id = NULL WHERE id = %s AND run_id = %s",
                     (thread_id, run_id))


def offrir_annulation(undo_id: str, thread_id: str, sub: str, label: str, kind: str,
                      payload: dict, secondes: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO copilot_undos (id, thread_id, sub, label, kind, payload, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW() + make_interval(secs => %s))",
            (undo_id, thread_id, sub, label, kind, json.dumps(payload, ensure_ascii=False),
             secondes))


def consommer(undo_id: str, sub: str) -> Optional[dict]:
    """L'annulation, marquée utilisée dans le même ordre — `None` si elle n'est pas à
    `sub`, déjà utilisée ou échue. Une fois, même si défaire échoue ensuite à moitié."""
    with _connect() as conn:
        return conn.execute(
            "UPDATE copilot_undos u SET used_at = NOW() FROM copilot_threads t "
            "WHERE u.id = %s AND u.sub = %s AND u.used_at IS NULL "
            "AND u.expires_at > NOW() AND t.id = u.thread_id "
            "RETURNING u.id, u.thread_id, u.label, u.kind, u.payload, t.org_id, "
            "t.project_id, t.run_id", (undo_id, sub)).fetchone()


def prolonger(thread_id: str, jeton: str, secondes: int) -> bool:
    """Repousse l'échéance du verrou tenu par ce jeton (une étape longue)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE copilot_threads SET busy_until = NOW() + make_interval(secs => %s) "
            "WHERE id = %s AND busy_token = %s RETURNING id",
            (secondes, thread_id, jeton)).fetchone()
    return row is not None


def schema_stocke(ns_id: int) -> Optional[dict]:
    """Le schéma d'un tableau tel qu'il est STOCKÉ (sans projection de face), pour
    comparer avant de défaire une modification de champs."""
    with _connect() as conn:
        row = conn.execute("SELECT schema FROM user_datastores WHERE id = %s",
                           (int(ns_id),)).fetchone()
    return row["schema"] if row else None


def tableau_vide(ns_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM datastore_rows WHERE ns_id = %s) "
                           "AS plein", (int(ns_id),)).fetchone()
    return not row["plein"]


def ajouter_depense(sub: str, micro_usd: int) -> None:
    """Ajoute le coût d'un appel au modèle à la dépense du jour (UTC) de `sub`, en
    millionièmes de dollar (`capabilities/copilot/depense.py`)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO copilot_spend (sub, day, usd_micros) "
            "VALUES (%s, (NOW() AT TIME ZONE 'UTC')::date, %s) "
            "ON CONFLICT (sub, day) DO UPDATE "
            "SET usd_micros = copilot_spend.usd_micros + EXCLUDED.usd_micros",
            (sub, int(micro_usd)))


def depense_du_jour(sub: str) -> int:
    """La dépense du jour (UTC) de `sub`, en millionièmes de dollar ; 0 sans appel."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT usd_micros FROM copilot_spend WHERE sub = %s "
            "AND day = (NOW() AT TIME ZONE 'UTC')::date", (sub,)).fetchone()
    return int(row["usd_micros"]) if row else 0


def creer_piece(piece_id: str, sub: str, org_id: Optional[int], s3_key: str, name: str,
                media_type: str, size: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO copilot_attachments (id, sub, org_id, s3_key, name, media_type, size) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (piece_id, sub, org_id, s3_key, name, media_type, int(size)))


def pieces(ids: list[str], sub: str, org_id: Optional[int]) -> dict[str, dict]:
    """Les pièces de `ids` qui sont à `sub` dans `org_id`, par identifiant. Celles d'un
    autre (compte ou org) manquent, exactement comme celles qui n'existent pas."""
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, s3_key, name, media_type, size FROM copilot_attachments "
            "WHERE id = ANY(%s) AND sub = %s AND org_id IS NOT DISTINCT FROM %s",
            (list(ids), sub, org_id)).fetchall()
    return {r["id"]: r for r in rows}


def volume_depose(sub: str, heures: int) -> int:
    """Les octets que `sub` a déposés en pièces jointes ces `heures` dernières heures,
    toutes orgs confondues (le quota de dépôt, `capabilities/copilot/pieces.py`) ; 0 sans
    dépôt. La fenêtre glisse : l'index `(created_at)` borne la lecture aux dépôts récents."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS octets FROM copilot_attachments "
            "WHERE sub = %s AND created_at > NOW() - make_interval(hours => %s)",
            (sub, int(heures))).fetchone()
    return int(row["octets"])


def pieces_purgeables(jours: int, limite: int = 5000) -> list[dict]:
    """Les pièces déposées il y a plus de `jours` (leur clé, pour retirer l'objet)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, s3_key FROM copilot_attachments "
            "WHERE created_at < NOW() - make_interval(days => %s) ORDER BY created_at "
            "LIMIT %s", (int(jours), int(limite))).fetchall()


def oublier_pieces(ids: list[str]) -> int:
    if not ids:
        return 0
    with _connect() as conn:
        return int(conn.execute("DELETE FROM copilot_attachments WHERE id = ANY(%s)",
                                (list(ids),)).rowcount)


def compter_purgeables(jours: int) -> dict:
    """Ce que `purger(jours)` retirerait (maintenance à blanc)."""
    with _connect() as conn:
        fils = conn.execute(
            "SELECT COUNT(*) AS n FROM copilot_threads "
            "WHERE last_seen < NOW() - make_interval(days => %s)", (int(jours),)).fetchone()
        jours_ = conn.execute(
            "SELECT COUNT(*) AS n FROM copilot_spend "
            "WHERE day < (NOW() AT TIME ZONE 'UTC')::date - %s", (int(jours),)).fetchone()
    return {"threads": int(fils["n"]), "spend_days": int(jours_["n"])}


# --- l'agent intégré dans Slack (`slack_copilot.py`) ------------------------------------

#: Un message de Slack déjà pris en charge se garde ce long : le temps d'un retry et du
#: second événement de la même mention, pas plus.
SLACK_EVENEMENT_JOURS = 2


def slack_reclamer(channel: str, ts: str) -> bool:
    """Prend en charge le message `(channel, ts)` : vrai pour le PREMIER événement qui le
    réclame, faux pour les suivants (l'autre événement de la même mention, un retry)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO copilot_slack_events (channel, ts) VALUES (%s, %s) "
            "ON CONFLICT (channel, ts) DO NOTHING RETURNING ts", (channel, ts)).fetchone()
    return row is not None


def slack_fil_existe(team_id: str, channel: str, thread_ts: str) -> bool:
    """Un fil de Slack porte-t-il une conversation, de qui que ce soit ? La lecture bon
    marché, avant tout identifiant : chaque réponse de chaque canal où l'app siège arrive
    ici, et presque aucune n'est sous une de nos conversations."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM copilot_slack_threads WHERE team_id = %s AND channel = %s "
            "AND thread_ts = %s LIMIT 1", (team_id, channel, thread_ts)).fetchone() is not None


def slack_fil(team_id: str, channel: str, thread_ts: str, sub: str) -> Optional[dict]:
    """La conversation de `sub` dans ce fil de Slack (`thread_ts` = '' : le message
    direct) : `{thread_id, inactif_min}` — depuis combien de minutes elle n'a pas vécu —,
    ou `None`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT s.thread_id, EXTRACT(EPOCH FROM (NOW() - t.last_seen)) / 60 AS inactif_min "
            "FROM copilot_slack_threads s JOIN copilot_threads t ON t.id = s.thread_id "
            "WHERE s.team_id = %s AND s.channel = %s AND s.thread_ts = %s AND s.sub = %s",
            (team_id, channel, thread_ts, sub)).fetchone()
    return {"thread_id": row["thread_id"], "inactif_min": float(row["inactif_min"])} \
        if row else None


def slack_lier(thread_id: str, team_id: str, channel: str, thread_ts: str,
               sub: str) -> None:
    """Range la conversation sous son fil de Slack. Une conversation neuve REMPLACE celle
    du même fil (un message direct dont la précédente a vieilli)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM copilot_slack_threads WHERE team_id = %s AND channel = %s "
            "AND thread_ts = %s AND sub = %s", (team_id, channel, thread_ts, sub))
        conn.execute(
            "INSERT INTO copilot_slack_threads (thread_id, team_id, channel, thread_ts, sub) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, team_id, channel, thread_ts, sub))


def slack_de(thread_id: str) -> Optional[dict]:
    """Où est la conversation dans Slack : `{team_id, channel, thread_ts, sub}`, ou `None`
    (une conversation de l'application)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT team_id, channel, thread_ts, sub FROM copilot_slack_threads "
            "WHERE thread_id = %s", (thread_id,)).fetchone()
    return dict(row) if row else None


def purger(jours: int) -> dict:
    """Retire les conversations sans vie depuis `jours` (leurs annulations partent avec,
    `ON DELETE CASCADE`) et les jours de dépense plus vieux que `jours`. Une conversation
    dont une étape tient le verrou n'est pas retirée."""
    with _connect() as conn:
        fils = conn.execute(
            "DELETE FROM copilot_threads WHERE last_seen < NOW() - make_interval(days => %s) "
            "AND (busy_until IS NULL OR busy_until < NOW())", (int(jours),)).rowcount
        jours_ = conn.execute(
            "DELETE FROM copilot_spend WHERE day < (NOW() AT TIME ZONE 'UTC')::date - %s",
            (int(jours),)).rowcount
        conn.execute(
            "DELETE FROM copilot_slack_events WHERE created_at < NOW() - make_interval(days => %s)",
            (SLACK_EVENEMENT_JOURS,))
    return {"threads": int(fils), "spend_days": int(jours_)}
