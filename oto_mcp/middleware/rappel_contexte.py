"""Le rappel du contexte d'org en tête des réponses d'outils (oto-backend#1041).

La règle (quand rappeler, d'où vient le verdict, ses bornes) vit dans
`oto_mcp/rappel_contexte.py`. Ici, la PLOMBERIE — en deux temps, parce que la chaîne
l'impose :

- le verdict a besoin de l'**org de l'appel**, que `CallContextMiddleware` pose
  (`_org=`, projet, run) et retire à sa sortie : il se prend DANS sa portée —
  `ConstatContexteMiddleware`, juste sous lui ;
- la ligne doit survivre à tout ce qui réécrit le canal texte : le rendu du vide
  (`EmptyResult`) et le corps markdown (`MarkdownBody`) remplacent le contenu entier,
  et ils sont PLUS EXTERNES que le contexte d'appel. Elle se pose donc au-dessus d'eux —
  `RappelContexteMiddleware`, juste sous le refus des comptes en pause.

Les deux se parlent par un relevé posé en `ContextVar` par l'externe et rempli par
l'interne (même tâche, même contexte). L'interne sans l'externe ne sert rien ;
l'externe sans l'interne ne sert rien non plus — ni l'un ni l'autre n'échoue.
"""
from __future__ import annotations

import contextvars
import logging

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.concurrency import run_in_threadpool

from .. import rappel_contexte, session_org
from ..db._hors_boucle import HorsBoucle
from .call_context import _cible

logger = logging.getLogger(__name__)

_RELEVE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "oto_rappel_contexte", default=None)


def avec_lignes_en_tete(result, lignes):
    """`result` avec `lignes` (les vides écartées) EN TÊTE de son canal texte — le
    reste tel quel. Une réponse en erreur, ou sans ligne, passe inchangée."""
    lignes = [t for t in lignes if t]
    if not lignes or getattr(result, "is_error", False):
        return result
    return ToolResult(
        content=[*(TextContent(type="text", text=t) for t in lignes),
                 *(result.content or [])],
        structured_content=getattr(result, "structured_content", None),
        meta=getattr(result, "meta", None),
    )


class RappelContexteMiddleware(Middleware):
    """Pose la ligne de rappel EN TÊTE du canal texte d'une réponse réussie."""

    async def on_call_tool(self, context, call_next):
        releve: dict = {}
        avis: list = []
        jeton = _RELEVE.set(releve)
        jeton_avis = session_org.set_call_avis(avis)
        try:
            result = await call_next(context)
        finally:
            session_org.reset_call_avis(jeton_avis)
            _RELEVE.reset(jeton)
        # Les avis de l'appel (`session_org.noter_avis`) prennent la même place que le
        # rappel, et pour la même raison : rien de ce qui réécrit le canal texte ne
        # passe au-dessus.
        return avec_lignes_en_tete(result, [releve.get("ligne"), *avis])


class ConstatContexteMiddleware(Middleware):
    """Prend le verdict dans la portée du contexte d'appel, hors de la boucle ; éteint
    le rappel quand l'outil servi est `oto_context` lui-même."""

    async def on_call_tool(self, context, call_next):
        releve = _RELEVE.get()
        if releve is None:
            return await call_next(context)
        # `oto_call` dispatche un autre outil (ADR 0036) : c'est la CIBLE qui compte —
        # `oto_call(name="oto_context")` est une lecture, et n'est pas rappelé.
        outil = _cible(getattr(context.message, "name", "") or "",
                       getattr(context.message, "arguments", None) or {})
        cle = None
        try:
            cle, releve["ligne"] = await run_in_threadpool(rappel_contexte.pour_l_appel, outil)
        except HorsBoucle:
            raise
        except Exception:  # noqa: BLE001 — un rappel ne casse jamais l'appel qu'il accompagne
            logger.warning("rappel du contexte d'org indisponible pour %s", outil,
                           exc_info=True)
        result = await call_next(context)
        if (cle is not None and outil == rappel_contexte.OUTIL_CONTEXTE
                and not getattr(result, "is_error", False)):
            rappel_contexte.constater_lecture(cle)
        return result
