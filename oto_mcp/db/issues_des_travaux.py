"""Ce que l'agent a CONCLU de son travail — et ce qui en fait un travail mort (09/11/2026).

Un travail `done` dit seulement que son run s'est TERMINÉ. Un agent qui bute sur une clé
retirée, un outil qui lui manque ou un canal archivé conclut `run_finish(outcome=
"blocked")` — et son travail finit `done` comme un autre. Mesuré le 09/11/2026 : un agent
de Klark arrêté sur `serper_search` absent de ses outils, ceux de Growth Room sur une clé
lemlist dont le compte avait quitté l'équipe — trois jours « verts », aucun épisode,
aucune alerte : le médecin des agents ne lit que `failed`.

Ce module tient les deux colonnes qui le disent, posées sur le travail AU `run_finish`
(`tools/guide_run.run_finish`), quand la clôture arrive sous le jeton de délégation de
ce travail (`verrou_org.travail_de_la_requete`) :

- `run_outcome` — l'issue la PLUS GRAVE déclarée sous ce travail (`failed` > `blocked`
  > `partial` > `done`) : un agent peut ouvrir plusieurs runs (sa procédure, une
  sous-procédure) ; l'issue d'un sous-run bloqué ne s'efface pas sous un `done` de
  l'enveloppe ;
- `run_note` — la note de CETTE issue, bornée.

⚠️ Pourquoi l'écrire sur le travail plutôt que la relire du journal : le run de la
procédure n'est pas celui que le travail lie (`runner_jobs.run_id` = le run de
plateforme) ; le seul lien est le jeton de délégation, et ces jetons sont PURGÉS à
expiration (`tokens.purger_delegations_expirees`). Une jointure par le jeton perdrait
l'issue au travail suivant.

Et les deux prédicats SQL qui en découlent, partagés par tous les relevés (épisodes,
alerte au titulaire, guérison) — une seule définition de « mort » et de « réussi » :

- MORT   = `failed`, ou `done` dont l'agent a conclu `blocked`/`failed` ;
- REUSSI = `done` sans une de ces deux issues.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE_ISSUE = "run_outcome"
COLONNE_NOTE = "run_note"
DDL_COLONNE_ISSUE = f"ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS {COLONNE_ISSUE} TEXT"
DDL_COLONNE_NOTE = f"ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS {COLONNE_NOTE} TEXT"

#: Les issues d'un run, de la moins grave à la plus grave (`run_status`).
GRAVITE = ("done", "partial", "blocked", "failed")

#: Les issues qui font d'un travail `done` un travail MORT.
ISSUES_MORTES = ("blocked", "failed")

#: Ce qu'on garde de la note : assez pour la classer et l'expliquer, pas un rapport.
NOTE_MAX = 1000

#: Le travail `j` est mort / a réussi. Littéraux, jamais une entrée d'appelant.
MORT = ("(j.status = 'failed' OR (j.status = 'done' "
        "AND j.run_outcome IN ('blocked', 'failed')))")
REUSSI = ("(j.status = 'done' AND (j.run_outcome IS NULL "
          "OR j.run_outcome NOT IN ('blocked', 'failed')))")
#: Le motif d'un travail mort : l'erreur du runner, à défaut la note de l'agent.
MOTIF = "COALESCE(NULLIF(j.last_error, ''), j.run_note)"


def _rang() -> str:
    cas = " ".join(f"WHEN '{issue}' THEN {i}" for i, issue in enumerate(GRAVITE))
    return f"CASE {{col}} {cas} ELSE -1 END"


def noter_issue(job_id: int, sub: str, outcome: str, note: Optional[str]) -> bool:
    """Pose l'issue que l'agent a déclarée sur SON travail — la plus grave gagne, sa note
    avec elle. Scopée au porteur du travail (`sub`) : un jeton d'autrui n'écrit rien.
    Rend True si la ligne a été retenue (même sans changement d'issue)."""
    if outcome not in GRAVITE:
        return False
    rang = _rang()
    with _connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE runner_jobs j
               SET {COLONNE_NOTE} = CASE
                       WHEN {rang.format(col='%(issue)s::text')} >= {rang.format(col='j.' + COLONNE_ISSUE)}
                       THEN %(note)s ELSE j.{COLONNE_NOTE} END,
                   {COLONNE_ISSUE} = CASE
                       WHEN {rang.format(col='%(issue)s::text')} >= {rang.format(col='j.' + COLONNE_ISSUE)}
                       THEN %(issue)s ELSE j.{COLONNE_ISSUE} END
             WHERE j.id = %(job)s AND j.sub = %(sub)s
            """,
            {"issue": outcome, "note": (note or "")[:NOTE_MAX] or None,
             "job": int(job_id), "sub": sub},
        )
        return (cur.rowcount or 0) > 0
