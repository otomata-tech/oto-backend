"""Adaptateur MCP de la couche capacité (ADR 0009).

Boucle sur le registre et monte un tool FastMCP par capacité ayant un binding
`mcp`. Chaque tool applique, avant le handler : validation `Input` → autz →
handler. L'`AuthzDenied` neutre est traduit en `McpError`. Le schéma du tool
est aplati (params plats) via `apply_flat_signature`.

⚠️ **Ce qui est SYNC part en threadpool, et c'est le point du montage.** FastMCP
route un `@mcp.tool` sync en threadpool tout seul — mais le tool qu'on monte ici est
`async def` (il lui FAUT des `await` : handler async, refresh de visibilité), donc
FastMCP le laisse dans la boucle, et tout ce qu'il appelle nûment avec. Autz, handler
sync et écho d'org touchent la base : appelés nûment, ils tenaient la boucle du serveur
MONO-LOOP pendant toute leur durée. Vécu le 2026-09-01 — 12 min 48 s de production
muette, 376 connexions acceptées par le noyau et jamais servies, pendant qu'une pose
d'index attendait derrière une lecture ouverte. Cf. `docs/event-loop-perf.md` mode n°4,
et le garde-fou `tests/test_capacites_hors_boucle.py`.

Dépend du core (sens unique ADR 0004) ; le core n'importe pas cet adaptateur.
"""
from __future__ import annotations

import dataclasses
import inspect
import logging
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from starlette.concurrency import run_in_threadpool
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import session_org
from ..auth.hooks import current_user_sub_from_token
from ..session_visibility import apply_session_visibility
from ._execution import execute
from ._types import (AuthzDenied, Capability, NotModified, RawCtx,
                     apply_flat_signature)

logger = logging.getLogger(__name__)


def _org_echo(org_id: int) -> dict:
    """Écho de l'org effective (`_org`) dans les payloads MCP org-sensibles.

    Lève l'ambiguïté « sous quelle org ai-je agi ? » après un `oto_use_org` : le
    client voit l'org résolue par le serveur à CHAQUE réponse, sans avoir à la
    déduire. Best-effort (le nom peut manquer, jamais l'id)."""
    try:
        from .. import org_store
        org = org_store.get_org(org_id)
        return {"id": org_id, "name": (org or {}).get("name")}
    # noqa: SILENT — écho d'org : l'id suffit quand le nom n'est pas lisible
    except Exception:
        return {"id": org_id}


def _org_param_reserved(cap: Capability) -> bool:
    """`_org=` (jeton de contexte d'appel) est injecté sur toute cap exposée en MCP.

    Le préfixe `_` (issue #250) supprime la question qui se posait avant : `org=` NU
    entrait en collision avec un champ métier homonyme (`UseOrgInput.org` = l'org CIBLE
    d'`oto_use_org`, pas le contexte), et retirer l'un mangeait l'autre — régression prod
    du 2026-07-04. Une cap peut désormais déclarer `org` sans que ça concerne le contexte.
    Le test sur `_org` ne reste que pour échouer FORT (paramètre dupliqué au boot) si une
    cap venait un jour à s'approprier le nom réservé."""
    return cap.mcp is not None and "_org" not in cap.Input.model_fields


def reserved_org_tool_names(capabilities: list[Capability]) -> frozenset:
    """Noms des tools MCP où `_org=` est injecté comme axe-contexte → pilote
    `CallContextMiddleware` (pose la ContextVar `_CALL_ORG` autour de toute la chaîne)."""
    return frozenset(cap.mcp for cap in capabilities if _org_param_reserved(cap))


def _make_tool(cap: Capability):
    org_reserved = _org_param_reserved(cap)
    async def _tool(**kwargs):
        # `_org=` (axe-contexte, modèle sans état de session) est posé EN AMONT par
        # `CallContextMiddleware` (ContextVar per-appel, lue par `current_org`) → on le
        # retire des kwargs pour ne pas le passer à l'`Input`. Le préfixe `_` le distingue
        # d'un `org` MÉTIER (oto_use_org.org = l'org CIBLE) qu'on ne doit pas toucher —
        # avant lui, retirer l'un mangeait l'autre (`UseOrgInput.org Field required`,
        # régression #108 vécue en prod le 2026-07-04).
        if org_reserved:
            kwargs.pop("_org", None)

        raw = None
        before_org = None

        def _amont():
            """Identité → validation → autz → org de référence → handler SYNC.

            Tout ce bloc touche la base (l'autz marche la cascade des rôles, le
            handler fait son travail), et RIEN dedans n'est awaitable : il part donc
            en entier dans un thread. Le rendre en un seul morceau garde l'ordre et
            évite quatre allers-retours de thread par appel.

            La résolution d'identité en fait partie : quand le drain d'alias est armé
            (`tenant_migration.alias_drain_armed`), `current_user_sub_from_token`
            canonicalise le sub et repousse l'utilisateur — deux allers-retours DB sur
            CHAQUE appel.
            Le jeton se lit par ContextVar, que la copie de contexte transporte."""
            nonlocal raw, before_org
            raw = RawCtx(sub=current_user_sub_from_token())
            inp = cap.Input(**kwargs)                 # validation (seule source : Input)
            ctx = cap.authz(raw, inp)                 # autz (peut lire inp pour ORG_ADMIN_OF)
            # Le canal est posé ICI, au seuil : la règle d'autz sert les deux faces et
            # ne peut pas le savoir. `replace` plutôt qu'une mutation — un ctx est un
            # fait, pas un accumulateur.
            ctx = dataclasses.replace(ctx, channel="mcp")
            if ctx.org_id is not None and raw.sub:
                # Org résolue AVANT le handler (même garde que l'écho plus bas — une cap
                # non org-scopée, ou sans sub, n'a pas à toucher le seam) : sert de
                # référence pour détecter, après coup, une mutation PERSISTANTE faite
                # PAR le handler (#110, cf. plus bas). Ce n'est QU'une référence : jamais
                # ce qu'on échoue directement.
                from .. import access
                before_org = access.current_org(raw.sub)
            return ctx, inp

        try:
            # `run_in_threadpool` (anyio) exécute sur une COPIE du contexte : les
            # ContextVars posées en amont (axe `_org` de CallContextMiddleware, jeton
            # de token, contexte FastMCP) sont lues normalement depuis le thread.
            ctx, result = await execute(cap.handler, _amont)
        except AuthzDenied as d:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=d.message or d.code))
        if isinstance(result, NotModified):
            # MCP n'a pas de code d'état : « rien n'a changé » doit être une DONNÉE,
            # sinon une réponse vide se lit comme un résultat vide. On rend le `rev`
            # avec, pour que l'appelant sache sur quelle version il est resté.
            result = {"not_modified": True, "rev": result.rev}
        if isinstance(result, dict) and ctx.org_id is not None:
            # Org à ÉCHOER dans `_org` : par défaut `ctx.org_id`, résolu à l'autz — il
            # porte déjà tout PIN MÉTIER explicite qu'accepte l'`Input` d'une capacité
            # (ex. `oto_procedure(org=<id>)`, lecture cross-org d'une procédure par un
            # champ propre à CETTE capacité). `access.current_org` ne connaît RIEN de ce
            # pin — il ne lit que l'axe-contexte (`_org=`), l'org du run, la consultation
            # ou la maison persistée — et y retombait donc TOUJOURS, figé sur le premier
            # org du contexte quel que soit le `org=` métier passé ensuite (signal 528,
            # oto-backend#712 : `_org` d'`oto_procedure` restait cloué sur l'org maison en
            # passant `org=<autre>` à `get`/`list`/`set`).
            #
            # On ne s'écarte de `ctx.org_id` QUE si le handler a lui-même MUTÉ le
            # contexte persistant PENDANT son exécution — cas `oto_use_org` (#110) : la
            # bascule se fait DANS le handler, donc `ctx.org_id` (figé à l'autz, avant le
            # switch) montrerait l'org D'AVANT (`{active_org: 83, _org: {id: 2}}`). Cette
            # mutation se détecte en comparant l'org résolue avant et après le handler :
            # un écart ne peut venir QUE d'un changement d'état fait par le handler lui-
            # même (la ContextVar `_CALL_ORG` posée par le middleware, elle, est stable
            # sur toute la durée de l'appel) — jamais d'un pin métier, qui ne mute rien.
            def _echo():
                from .. import access
                after_org = access.current_org(raw.sub) if raw.sub else None
                eff = (after_org if (after_org is not None and after_org != before_org)
                       else ctx.org_id)
                return _org_echo(eff if eff is not None else ctx.org_id)
            # Deux lectures DB de plus, sur CHAQUE appel org-scopé : au thread, comme
            # le reste. C'est peu, et « peu » multiplié par la charge est exactement ce
            # qui a gelé la boucle les trois fois précédentes.
            result.setdefault("_org", await run_in_threadpool(_echo))
        if cap.refresh_visibility and raw.sub:
            # Bascule de profil (org/groupe actif déjà commitée par le handler) →
            # re-pousse la denylist de la NOUVELLE org sur la session MCP courante,
            # émettant tools/list_changed. Best-effort : un échec de refresh ne doit
            # pas faire échouer la bascule (la prochaine session corrigera).
            try:
                await apply_session_visibility(get_context(), raw.sub, reset=True)
            except Exception:
                logger.warning("refresh_visibility post-hook failed for %s/%s",
                               cap.key, raw.sub, exc_info=True)
        return result
    _tool.__name__ = cap.mcp
    _tool.__doc__ = cap.description or cap.key
    tool = apply_flat_signature(_tool, cap.Input)
    # Paramètre commun `_org=` (axe-contexte per-appel) ajouté au schéma plat SANS toucher
    # l'`Input` de chaque capacité. Prime sur l'org maison, robuste au reset/absence de
    # session (claude.ai) ; inerte pour les caps non org-scopées. (`_project=`/`_run_id=`
    # suivront en passe profonde.) La pose/garde de la ContextVar vit dans le middleware.
    if org_reserved:
        sig = tool.__signature__
        extra = inspect.Parameter("_org", inspect.Parameter.KEYWORD_ONLY,
                                  annotation=Optional[int], default=None)
        tool.__signature__ = sig.replace(parameters=[*sig.parameters.values(), extra])
        tool.__annotations__["_org"] = Optional[int]
    return tool


def register(instance: FastMCP, capabilities: list[Capability]) -> None:
    """Monte un tool par capacité MCP. No-op si la liste est vide (canari)."""
    for cap in capabilities:
        if cap.mcp is None:
            continue
        if not cap.is_exposed():
            continue
        instance.tool(name=cap.mcp, description=cap.description or None)(_make_tool(cap))
