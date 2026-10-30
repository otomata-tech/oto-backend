"""Ce que fait un appel d'outil de connecteur, lu dans le verbe de son op : lire, écrire,
envoyer ou détruire.

Une recette-code tourne avec les droits de QUI L'EXÉCUTE, jamais de qui l'a écrite :
elle ne peut rien faire que son exécutant ne pourrait faire lui-même avec ses outils
(#1202). Ce qu'elle écrit chez un tiers n'a donc plus besoin d'une liste d'outils
NOMMÉS (`ecritures.ECRITURES`, qui reste la classe FORCÉE de ses outils) : tout outil de
connecteur se classe ici, par le verbe de son op — ou de son nom, sans op. Le classement
ne décide d'aucun droit ; il dit seulement ce que la recette doit DÉCLARER, ce qu'une
épreuve peut jouer et ce qui ne s'ouvre jamais :

- `lire` : seulement un verbe de `LIRE`, liste courte et positive. Un verbe inconnu
  n'est JAMAIS présumé lecteur : c'est une écriture, à déclarer, jamais jouée en épreuve.
- `envoyer` : un verbe d'`ENVOI` (un message, une invitation, une publication, une piste
  poussée en séquence) — déclaré, et `allow_sending: true`.
- `detruire` : un verbe de `DESTRUCTION`, `merge` ou une op `bulk_*`. Refusé : une
  recette relancée repart du début, et une boucle sans modèle qui se trompe détruirait
  en série.
- `ecrire` : tout le reste.
"""
from __future__ import annotations

from typing import Optional

from ..tool_visibility import namespace_of

LIRE = frozenset({"get", "list", "search", "find", "lookup", "read", "fetch", "count",
                  "stats", "status", "show", "describe", "download", "history", "preview"})
ENVOI = frozenset({"send", "reply", "forward", "publish", "broadcast", "invite", "remind",
                   "post", "react", "comment", "endorse", "notify", "message", "launch",
                   "call", "request", "push", "share", "schedule"})
DESTRUCTION = frozenset({"delete", "remove", "rm", "trash", "archive", "clear", "purge",
                         "reset", "revoke", "cancel", "unshare", "disable", "withdraw",
                         "unsubscribe", "untag", "detach", "unassign", "unlink", "merge",
                         "bulk", "drop", "destroy", "erase", "empty", "replace"})
CLASSES = ("lire", "ecrire", "envoyer", "detruire")


def verbe(outil: str, op: Optional[str]) -> str:
    """Le verbe d'un appel : le premier mot de son op ; sans op, celui du nom de l'outil
    privé de son espace (`slack_post_message` → `post`)."""
    texte = op
    if not texte:
        ns = namespace_of(outil)
        texte = outil[len(ns) + 1:] if outil.startswith(ns + "_") else outil
    return (texte or "").split("_", 1)[0].lower()


def classer(outil: str, op: Optional[str]) -> str:
    """`lire`, `ecrire`, `envoyer` ou `detruire` — jamais `lire` sur un verbe inconnu."""
    v = verbe(outil, op)
    if v in DESTRUCTION:
        return "detruire"
    if v in ENVOI:
        return "envoyer"
    if v in LIRE:
        return "lire"
    return "ecrire"
