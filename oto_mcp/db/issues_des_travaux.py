"""Ce que l'agent a CONCLU de son travail — et ce qui en fait un travail mort.

Un travail `done` dit seulement que son run s'est TERMINÉ. Un agent qui bute sur une clé
retirée, un outil qui lui manque ou un canal archivé conclut `run_finish(outcome=
"blocked")` — et son travail finit `done` comme un autre. Constaté le 09/11/2026 : des
agents arrêtés, l'un sur un outil de recherche absent de ses outils, un autre sur une clé
dont le compte avait quitté l'équipe du fournisseur — trois jours « verts », aucun
épisode, aucune alerte : le médecin des agents ne lisait que `failed`.

**Sous un réglage d'instance** (`ENV_COMPTE_LES_BLOQUES`, lu par `interrupteurs.oui_non`
à chaque appel) : éteint (absent, vide, non), rien de ce module ne s'applique — le travail
n'est mort que `failed`, réussi que `done`, son motif est l'erreur du runner, et
`run_finish` n'écrit rien sur le travail. Une valeur illisible LÈVE.

Allumé, ce module tient les deux colonnes qui le disent, posées sur le travail AU
`run_finish` (`tools/guide_run.run_finish`), quand la clôture arrive sous le jeton de
délégation de ce travail (`verrou_org.travail_de_la_requete`) :

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

Et les prédicats SQL qui en découlent, partagés par tous les relevés (épisodes, alerte au
titulaire, guérison) — une seule définition de « mort » et de « réussi » :

- `mort()`   = `failed`, ou (allumé) `done` dont l'agent a conclu `blocked`/`failed` ;
- `reussi()` = `done`, sans (allumé) une de ces deux issues ;
- `motif()`  = l'erreur du runner, à défaut (allumé) la note de l'agent.
"""
from __future__ import annotations

import os
from typing import Optional

from .. import interrupteurs
from ._conn import _connect

#: Le réglage d'instance : un `done` que l'agent conclut `blocked`/`failed` compte-t-il
#: comme un travail mort ? Éteint par défaut.
ENV_COMPTE_LES_BLOQUES = "OTO_RUNS_BLOQUES_COMPTES"

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


def compte_les_bloques() -> bool:
    """Le réglage de l'instance, lu à chaque appel. Une valeur illisible lève
    (`interrupteurs.InterrupteurIllisible`)."""
    return interrupteurs.oui_non(ENV_COMPTE_LES_BLOQUES,
                                 os.environ.get(ENV_COMPTE_LES_BLOQUES))


# Littéraux, jamais une entrée d'appelant.
_MORT_SEUL = "(j.status = 'failed')"
_REUSSI_SEUL = "(j.status = 'done')"
_MORT = ("(j.status = 'failed' OR (j.status = 'done' "
         "AND j.run_outcome IN ('blocked', 'failed')))")
_REUSSI = ("(j.status = 'done' AND (j.run_outcome IS NULL "
           "OR j.run_outcome NOT IN ('blocked', 'failed')))")


def mort() -> str:
    """Le prédicat SQL « le travail `j` est mort »."""
    return _MORT if compte_les_bloques() else _MORT_SEUL


def reussi() -> str:
    """Le prédicat SQL « le travail `j` a réussi »."""
    return _REUSSI if compte_les_bloques() else _REUSSI_SEUL


def motif() -> str:
    """L'expression SQL du motif d'un travail mort `j`."""
    return ("COALESCE(NULLIF(j.last_error, ''), j.run_note)" if compte_les_bloques()
            else "j.last_error")


def _rang() -> str:
    cas = " ".join(f"WHEN '{issue}' THEN {i}" for i, issue in enumerate(GRAVITE))
    return f"CASE {{col}} {cas} ELSE -1 END"


def noter_issue(job_id: int, sub: str, outcome: str, note: Optional[str]) -> bool:
    """Pose l'issue que l'agent a déclarée sur SON travail — la plus grave gagne, sa note
    avec elle. Scopée au porteur du travail (`sub`) : un jeton d'autrui n'écrit rien.
    Rend True si la ligne a été retenue (même sans changement d'issue). L'appelant
    (`guide_run._noter_sur_le_travail`) ne l'appelle que réglage allumé."""
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
