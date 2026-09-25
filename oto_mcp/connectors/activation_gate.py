"""Garde d'appel de l'activation d'un connecteur — refus nommé `connector_disabled`.

La visibilité de session masque les outils d'un connecteur coupé, mais elle est
**fail-open** (gouvernance d'affichage, ADR 0031) et `oto_call` la traverse par
construction (ADR 0036) : mesuré le 24/09/2026 (oto-backend#1064), un outil d'un
connecteur coupé — master plateforme OFF, ou override d'org OFF — était SERVI par
`oto_call`, handler exécuté. Cette garde-ci est le contrôle d'appel qui manquait.

**Un seul point de passage pour les deux chemins** : elle se joue une fois que le
contexte de l'appel est posé (axes `_org=`/`_group=`/`_project=`, org du run), donc
contre l'org et l'équipe sous lesquelles la cible va RÉSOUDRE — pas l'org maison.
Le middleware de contexte (`CallContextMiddleware`, appel direct) et `oto_call`
(qui rejoue ce contexte hors chaîne) l'appellent au même endroit : juste après
`run_org.pin_for_call`, juste avant le handler.

**Fail-closed** : une lecture d'activation qui échoue fait échouer l'appel, elle
ne le laisse pas passer. Sans sub (stdio local), rien n'est gardé : la surface
multi-utilisateur seule est visée.
Les outils plateforme (`oto_*`, `data_*`, `run_*`…) n'ont pas de connecteur au
registre : jamais gardés, sans lecture de base.
"""
from __future__ import annotations

from typing import Optional

from mcp.types import INVALID_PARAMS, ErrorData
from starlette.concurrency import run_in_threadpool

from .. import access, call_axes, providers
from ..mcp_errors import McpError
from ..tool_visibility import namespace_of
from . import activation

CODE = "connector_disabled"


def _refus(connector: str) -> Optional[ErrorData]:
    """Sync (threadpool) : l'erreur à rendre si `connector` est coupé pour l'org et
    l'équipe sous lesquelles l'appel résout, sinon `None`. L'identité se lit ici, pas
    dans la boucle : la canonicaliser peut toucher la base (drain d'alias)."""
    sub = call_axes.current_user_sub_from_token()
    if not sub:
        return None
    org = access.current_org(sub)
    group = access.current_group(sub)
    cran = activation.cran_qui_coupe(connector, org, group)
    if cran is None:
        return None
    ou = f"l'organisation {org}" if org is not None else "ton compte (aucune organisation active)"
    if cran == "org":
        pourquoi = f"désactivé pour {ou} par un réglage de l'organisation"
        geste = (f"un admin de l'org l'active : oto_connector_activation(op='set', "
                 f"scope='org', org_id={org}, name='{connector}', enabled=true).")
    elif cran == "tenant":
        pourquoi = "coupé par l'hébergeur de ton organisation, pour toute son offre"
        geste = ("seul un admin de cet hébergeur (le tenant) le rouvre, depuis son "
                 "tableau de bord ; aucune organisation ni équipe ne le peut.")
    elif cran == "group":
        pourquoi = f"coupé par ton équipe {group}"
        geste = (f"un chef de l'équipe retire la coupure : oto_connector_activation("
                 f"op='clear', scope='group', group_id={group}, name='{connector}').")
    else:
        pourquoi = f"désactivé par la plateforme pour {ou}"
        geste = ("seul un admin de la plateforme peut l'ouvrir : une organisation ne "
                 "le peut pas, le plafond plateforme n'est jamais relâché.")
    return ErrorData(
        code=INVALID_PARAMS,
        message=(f"Refus `{CODE}` : le connecteur `{connector}` est {pourquoi}. L'appel "
                 f"n'est servi ni directement ni par oto_call. Pour l'activer, {geste}"),
        data={"code": CODE, "retryable": False, "connector": connector,
              "org_id": org, "group_id": group, "scope": cran},
    )


async def require_active(tool_name: str) -> None:
    """Lève `connector_disabled` si l'outil `tool_name` appartient à un connecteur
    coupé pour l'appel courant. À appeler APRÈS la pose du contexte d'appel."""
    con = providers.connector_for_namespace(namespace_of(tool_name))
    if con is None:
        return
    refus = await run_in_threadpool(_refus, con.name)
    if refus is not None:
        raise McpError(refus)
