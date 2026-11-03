"""Old tool results leave the replayed history when the conversation resumes COLD.

Every tool result is written to the prompt cache once and read back at every later call
of the conversation. When the person comes back after the cache's 5 minutes, the whole
history is re-written anyway: that is the moment to drop what the new question does not
need. Results of EARLIER questions are replaced by one line naming the tool and its
size (the call can be made again); the results of the last exchange stay whole, so a
follow-up ("and the second one?") still has its data. Only the results of READS go (the
copilot's own `classement`): what a write answered (an id created, what changed) is the
record of an act, which calling again would repeat; it stays whole.

Done once, in place, on the stored messages: the history is then stable again and the
cache reads it back. Never in the middle of a tool loop, and never on a warm cache —
rewriting the prefix of a warm cache would cost a full re-write for nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import classement

#: The cache's default lifetime: past it, the history is re-written whatever we do.
FROID_S = 300
#: A result shorter than this is not worth a stub.
RESULTAT_MIN = 2_000
#: Nothing is touched unless at least this much would leave the history.
GAIN_MIN = 20_000
_MARQUE = "removed from the history to save tokens"


def _taille(contenu) -> int:
    if isinstance(contenu, str):
        return len(contenu)
    if isinstance(contenu, list):
        return sum(len(b.get("text") or "") if isinstance(b, dict) else len(str(b))
                   for b in contenu)
    return len(str(contenu))


def _humain(message: dict) -> bool:
    """A message from the person: text, not a tool result."""
    if message.get("role") != "user":
        return False
    c = message.get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def inactif_depuis(vu) -> Optional[float]:
    """Seconds since the conversation's last activity (`last_seen`), None when unknown."""
    if not isinstance(vu, datetime):
        return None
    if vu.tzinfo is None:
        vu = vu.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - vu).total_seconds()


def est_froid(inactif_s: Optional[float]) -> bool:
    return inactif_s is not None and inactif_s > FROID_S


def compacter(messages: list, annotations: Callable[[str], Any]) -> int:
    """Replaces, in place, the long results of READS that come before the last message of
    the person; `annotations(target)` gives a tool's declared annotations, as the loop
    classes its calls (`Boite.annotations`). Returns the characters freed; 0 and no
    change under `GAIN_MIN`."""
    humains = [i for i, m in enumerate(messages) if isinstance(m, dict) and _humain(m)]
    if not humains:
        return 0
    limite = humains[-1]
    noms: dict[str, str] = {}
    lectures: set = set()
    cibles = []
    for i, m in enumerate(messages[:limite]):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                target, targs = classement.cible(b.get("name") or "", b.get("input"))
                noms[b.get("id")] = target
                if classement.classer(target, targs, annotations(target)) \
                        == classement.LECTURE:
                    lectures.add(b.get("id"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in lectures:
                n = _taille(b.get("content"))
                deja = isinstance(b.get("content"), str) and _MARQUE in b["content"][:200]
                if n >= RESULTAT_MIN and not deja:
                    cibles.append((b, n))
    if sum(n for _, n in cibles) < GAIN_MIN:
        return 0
    libre = 0
    for b, n in cibles:
        nom = noms.get(b.get("tool_use_id"), "a tool")
        b["content"] = (f"[Result of `{nom}` ({n} characters) {_MARQUE}; "
                        "call the tool again if you still need it.]")
        libre += n - len(b["content"])
    return libre
