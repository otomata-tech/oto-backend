"""`data_write(via=…)` : nommer l'outil qui a TROUVÉ la valeur qu'on écrit.

Un agent cherche un email avec `apollo_match_person`, puis l'écrit avec `data_write` :
sans plus, le journal des révisions nomme `data_write` — vrai, mais ce n'est pas ce que
l'en-tête de la colonne veut dire (« trouvé par Apollo »). `via` le dit, et le journal
l'écrit en `outil` sur chaque révision de l'appel (`geste.via`).

**Prouvé, jamais déclaré — et jamais bloquant.** `via` n'est retenu que si cet outil
a RÉUSSI dans le run de l'appel (`tool_calls`, même `run_id`, `ok`) — `oto_call` déplié
vers sa cible. Sinon (pas de run, outil que le run n'a pas appelé, outil de la
plateforme), l'écriture se fait QUAND MÊME, sans attribution, et la réponse le dit
(`via_dropped`) : la donnée ne doit jamais payer pour son étiquette. Un nom qu'on
pourrait poser librement finirait par dire n'importe quoi, et un en-tête qui ment est
pire qu'un en-tête vide.

Les outils de la plateforme (`data_*`, `oto_*`, `run_*`, `feedback`) ne sont pas une
provenance : refusés aussi — `data_rows` n'a rien trouvé, il a relu.
"""
from __future__ import annotations

import functools
import re
from typing import Optional

from .. import geste, session_org

#: Ce qui n'est jamais une provenance : la plateforme elle-même.
_PLATEFORME = re.compile(r"^(data|oto|run)_|^feedback$")
#: Jusqu'où l'on remonte le run (les appels les plus récents d'abord, index
#: `idx_tool_calls_run`). Un run plus long que ça a fait sa recherche plus tôt : refusé,
#: le refus le dit.
_FENETRE = 2000

DESCRIPTION = (
    "the tool that FOUND what this call writes (`apollo_match_person`, "
    "`waterfall_email`, `serper_search`…), when you looked it up first and write it now. "
    "The row's journal records it, and the column header shows it (\"found by Apollo\"). "
    "It counts only for a tool that SUCCEEDED in THIS run (`run_start`, then `_run_id=`); "
    "otherwise the write still happens, unattributed, and the reply says why "
    "(`via_dropped`). Never a platform tool (`data_*`, `oto_*`, `run_*`). Omit it when "
    "you write what you established yourself.")


def _appele_dans_le_run(run_id: str, outil: str) -> bool:
    from ..db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM (SELECT tool, args, ok FROM tool_calls "
            "WHERE run_id = %s ORDER BY created_at DESC LIMIT %s) t "
            "WHERE t.ok AND (t.tool = %s OR (t.tool = 'oto_call' AND t.args->>'name' = %s)) "
            "LIMIT 1", (run_id, _FENETRE, outil, outil)).fetchone() is not None


def verifier(via: Optional[str]) -> Optional[str]:
    """Le nom à estampiller, ou `ValueError` qui nomme le geste qui réussit."""
    if via is None:
        return None
    nom = str(via).strip()
    if not nom:
        return None
    if _PLATEFORME.search(nom):
        raise ValueError(
            f"`via={nom}` ignored: a platform tool found nothing — name the tool that "
            f"looked the value up (e.g. `apollo_match_person`). Written, unattributed.")
    run_id = session_org.current_call_or_session_run()
    if not run_id:
        raise ValueError(
            f"`via={nom}` ignored: it needs a run — `run_start`, then `_run_id=` on the "
            f"lookup AND on this write. Written, unattributed.")
    if not _appele_dans_le_run(str(run_id), nom):
        raise ValueError(
            f"`via={nom}` ignored: no successful `{nom}` call in run {run_id} — it is "
            f"never guessed. Written, unattributed.")
    return nom[:128]


def sous_via(fn):
    """Décorateur d'un outil d'écriture qui porte `via` : vérifié avant l'écriture ;
    prouvé → l'écriture entière sous `geste.via(nom)` ; sinon l'écriture se fait sans
    attribution et la réponse porte `via_dropped` (la raison)."""
    @functools.wraps(fn)
    def _enveloppe(*args, **kwargs):
        try:
            nom = verifier(kwargs.get("via"))
        except ValueError as e:
            out = fn(*args, **kwargs)
            if isinstance(out, dict):
                out = {**out, "via_dropped": str(e)}
            return out
        if nom is None:
            return fn(*args, **kwargs)
        with geste.via(nom):
            return fn(*args, **kwargs)
    _enveloppe.__doc__ = (fn.__doc__ or "").replace("<<via>>", DESCRIPTION)
    return _enveloppe
