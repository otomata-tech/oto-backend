"""Récupère l'identité de l'utilisateur courant côté MCP tool.

Le bearer JWT est validé par FastMCP en amont des handlers (auth provider) ;
ici on lit juste le sub depuis le contexte. `OTO_MCP_DEV_SUB` : repli d'identité
en **dev local uniquement** (opt-in par env, jamais posé en prod). Depuis le
retrait du transport stdio (2026-06-13), le serveur est toujours en
streamable_http authentifié — ce repli ne sert plus qu'à un run http local sans
vrai Logto, et reste sans effet tant que l'env n'est pas posée.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Iterator, Optional

from ..tenant_migration import alias_drain_armed

logger = logging.getLogger(__name__)


class IdentiteIndisponible(RuntimeError):
    """La canonicalisation du `sub` a échoué pour une raison d'INFRASTRUCTURE (base
    injoignable, pool épuisé…) — ni un alias refusé, ni un compte en pause, qui ont
    leurs propres exceptions et se lèvent telles quelles.

    Existe pour une seule raison : le texte. Les middlewares du handshake et de la
    liste ne sont pas sous l'enveloppe d'erreur, et le SDK amont sert alors `str(exc)`
    tel quel au client — celui d'un pilote de base peut nommer un hôte ou un port. Ce
    message-ci dit ce qui se passe et quoi faire, sans rien de l'intérieur ; la cause
    d'origine reste chaînée (`__cause__`) pour le journal et Sentry."""

    def __init__(self, raison: str):
        self.raison = raison
        super().__init__(
            f"identité indisponible ({raison}) : la requête est refusée plutôt que "
            "servie sans identité — réessaie dans un instant")


def _raison_d_echec(exc: BaseException) -> str:
    """La RAISON d'un échec de canonicalisation, en une étiquette stable pour le
    journal : `alias_<motif>` (chaîne d'alias refusée), `compte_en_pause`,
    `tenant_desactive`, sinon la classe de l'erreur d'infrastructure."""
    from ..db.sub_aliases import AliasNonResolvable
    from ..db.tenants import TenantDesactive
    from ..db.users import CompteEnPause
    if isinstance(exc, AliasNonResolvable):
        return f"alias_{exc.motif}"
    if isinstance(exc, CompteEnPause):
        return "compte_en_pause"
    if isinstance(exc, TenantDesactive):
        return "tenant_desactive"
    return f"base_indisponible:{type(exc).__name__}"


def _decrire_jeton(token) -> str:
    """`client=… jeton=…` du jeton présenté — de quoi retrouver l'appelant dans le
    journal (application OAuth, jeton nommé), jamais une valeur de jeton."""
    claims = getattr(token, "claims", None) or {}
    client = claims.get("azp") or claims.get("client_id") or getattr(token, "client_id", None)
    return (f"client={client or '-'} jeton={claims.get('token_kind') or 'oauth'}"
            f"#{claims.get('token_id') or '-'}")

# Override d'identité pour la face REST (contextvar, par requête). La face MCP lit
# le sub du token via `get_access_token()` (contextvar posé par FastMCP) ; en REST
# ce contextvar n'existe pas. Quand un handler REST veut INVOQUER un tool sous
# l'identité de l'appelant (ex. « tester un outil » depuis le dashboard), il pose
# ce sub-override → `resolve_api_key`/`current_org`/… résolvent la bonne identité
# et les gates de call-time (credential, RBAC connecteur) restent intacts. Copié
# dans le threadpool avec le contexte (anyio `to_thread` propage les contextvars).
_sub_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_rest_sub_override", default=None)


@contextlib.contextmanager
def sub_override(sub: Optional[str]) -> Iterator[None]:
    """Fixe l'identité (`sub`) courante le temps d'un bloc — face REST uniquement.

    Ne touche PAS la face MCP (qui lit toujours le token). Réentrant (reset propre)."""
    token = _sub_override.set(sub)
    try:
        yield
    finally:
        _sub_override.reset(token)


# Mémoire d'identité PAR MESSAGE MCP — la canonicalisation résolue une fois, relue
# par tous ceux qui la redemandent dans le MÊME appel.
#
# ⚠️ **Pourquoi ce cache ne peut pas servir une identité à la place d'une autre**,
# ce qui serait infiniment pire que le gel qu'il corrige. Trois barrières,
# indépendantes :
#
# 1. **la clé EST l'identité.** L'entrée est indexée par le `sub` BRUT lu du jeton,
#    et la valeur est sa forme canonique. Demander l'identité de A ne peut donc pas
#    rendre celle de B : ce serait chercher `A` et trouver ce qui a été rangé sous
#    `B`. Une portée trop large ne produirait pas une confusion d'identité, mais une
#    péremption (un alias posé pendant la requête, non vu) — un défaut d'une autre
#    nature, et sans conséquence de sécurité ;
# 2. **la portée est ouverte par le message, pas par le processus.** `identity_scope`
#    pose un dictionnaire NEUF ; le défaut de la ContextVar est `None`, jamais un
#    dictionnaire partagé au niveau module. Deux requêtes concurrentes ne peuvent pas
#    tomber sur le même objet : chacune fait son `set` dans le contexte de SA tâche,
#    et un `set` n'est jamais vu par une tâche sœur ;
# 3. **hors portée, il n'existe pas.** `None` ⟹ le chemin d'avant, à l'octet près.
#    Un script, un timer, un test qui n'ouvre pas de portée ne mémorise rien.
#
# L'override REST (`_sub_override`) court-circuite tout ceci en amont : il rend son
# sub avant même qu'on regarde le cache, donc « tester un outil sous l'identité de
# l'appelant » ne peut ni lire ni garnir la mémoire d'un autre.
_identity_cache: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "oto_identity_cache", default=None)


@contextlib.contextmanager
def identity_scope() -> Iterator[None]:
    """Ouvre une mémoire d'identité pour la durée d'UN message servi.

    Réentrant : une portée imbriquée pose son propre dictionnaire et le rend à la
    sortie. Hors de ce bloc, `current_user_sub_from_token` retrouve exactement le
    comportement qu'il avait avant l'existence de ce cache."""
    token = _identity_cache.set({})
    try:
        yield
    finally:
        _identity_cache.reset(token)


def prime_identity() -> None:
    """Résout l'identité UNE fois et garnit la portée courante — appelée HORS boucle.

    C'est le geste que `_calllog_sink` fait déjà pour son insertion (`asyncio.to_thread`,
    « ne pas geler l'event loop sur le chemin chaud de chaque tool call ») : le même
    remède, appliqué à l'identité juste au-dessus, qu'il avait manquée.

    Sans portée ouverte, sans commande de drain, ou sous override REST : ne fait rien —
    il n'y a alors rien à résoudre, et rien à mémoriser. Ne rattrape aucune erreur :
    l'appelant (la portée) décide quoi faire d'un échec, et sa décision est de laisser
    le chemin d'avant se rejouer là où il se rejouait."""
    if _identity_cache.get() is None or _sub_override.get() or not alias_drain_armed():
        return
    current_user_sub_from_token()


def current_client_id_from_token() -> Optional[str]:
    """`azp`/`client_id` du bearer JWT — l'application OAuth CLIENTE qui porte le
    grant (claude.ai, Claude Code, ChatGPT…), PAS l'utilisateur (`sub`). Axe de
    télémétrie/UX seulement : sous CIMD, c'est l'URL du document de métadonnées que le
    client publie lui-même → identification lisible, jamais une frontière d'autz. NULL
    en REST/dev local."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
        if token and getattr(token, "claims", None):
            return token.claims.get("azp") or token.claims.get("client_id")
    # noqa: SILENT — hors contexte de requête MCP : pas de client OAuth à nommer
    except Exception:
        pass
    return None


def current_token_axes() -> dict:
    """`{token_id, token_kind}` du jeton NOMMÉ de la requête MCP en cours — quel jeton a
    servi, jamais sa valeur (otomata-tech/oto#187). Posés par `server._verify_api_token`
    sur un jeton d'API (`kind` : `user` | `delegation`) ; `{}` pour une session OAuth
    (aucun jeton nommé) et hors requête MCP. Lecture de contexte, aucune base."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
    # noqa: SILENT — hors contexte de requête MCP : aucun jeton à nommer
    except Exception:
        return {}
    claims = getattr(token, "claims", None) or {}
    return {k: claims[k] for k in ("token_id", "token_kind") if claims.get(k) is not None}


def current_user_sub_from_token() -> Optional[str]:
    """Sub de l'utilisateur courant depuis le bearer JWT MCP (ou l'override REST)."""
    override = _sub_override.get()
    if override:
        return override
    # ⚠️ Le `try` ne couvre QUE la récupération du jeton — absence de contexte fastmcp,
    # import indisponible : là, retomber sur `OTO_MCP_DEV_SUB` est le comportement voulu.
    # Il couvrait aussi la canonicalisation ci-dessous, et c'est ce qui rendait le refus
    # de `resolve_sub` inopérant : un échec d'IDENTITÉ y était reclassé en « pas de
    # jeton » et la requête repartait anonyme, sans un mot (`docs/silences-2026-08-27.md`,
    # site B5). Un échec d'identité se lève ; il ne se dégrade pas en anonymat.
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
    # noqa: SILENT — hors contexte de requête MCP (REST, dev local) : repli sur OTO_MCP_DEV_SUB
    except Exception:  # noqa: BLE001 — hors contexte de requête MCP (REST, dev local)
        token = None
    if token is None:
        return os.environ.get("OTO_MCP_DEV_SUB")
    # Un jeton VÉRIFIÉ sans `sub` n'est pas une absence de jeton : c'est une identité
    # illisible. Elle ne se sert ni sous le compte de dev (`OTO_MCP_DEV_SUB` ne vaut
    # que SANS jeton) ni en silence — la requête part sans identité, et le journal
    # dit pourquoi (oto-backend#464).
    claims = getattr(token, "claims", None) or {}
    sub = claims.get("sub")
    if not sub:
        logger.warning(
            "identité MCP illisible — raison=%s %s : la requête n'a pas d'identité",
            "claims_absentes" if not claims else "sub_absent", _decrire_jeton(token))
        return None
    # Drain d'alias (B1, otomata#35) : canonicaliser le sub (vieux jeton →
    # compte migré). ⚠️ Contrairement à la porte REST, `upsert_user` est ICI
    # sous la commande du drain — un compte MCP-only n'est donc rafraîchi que
    # commande posée. Ce passager est relevé par le test du même nom ; le
    # sortir de là est un changement de comportement, pas un nettoyage.
    if not alias_drain_armed():
        return sub
    # Une portée ouverte ⟹ la canonicalisation (un SELECT) et le
    # rafraîchissement (une lecture, une écriture seulement si l'adresse ou le
    # nom change) ne se paient qu'UNE fois par message, quel que soit le nombre
    # d'intermédiaires qui redemandent la même identité dans le même
    # appel. Mesuré le 09/09/2026 sur la chaîne servie : 10 allers-retours
    # PG par `tools/call`, tous dans la boucle, pour UNE valeur.
    # ⚠️ La clé est le sub BRUT du jeton : c'est ce qui rend impossible
    # de servir l'identité d'un autre (cf. `_identity_cache`).
    cache = _identity_cache.get()
    if cache is not None and sub in cache:
        return cache[sub]
    from .. import db
    try:
        canonique = db.resolve_sub(sub)
        db.upsert_user(canonique, email=claims.get("email"), name=claims.get("name"))
    except Exception as exc:
        # C'est ICI, et nulle part ailleurs, que se dit POURQUOI une requête n'a pas
        # d'identité (oto-backend#464) : les appelants ne rattrapent plus l'échec pour
        # repartir anonymes — ils le laissent monter. La raison est une étiquette
        # stable (`alias_<motif>`, `compte_en_pause`, `base_indisponible:<classe>`),
        # le sub est celui du JETON (le seul qu'on connaisse à ce stade).
        raison = _raison_d_echec(exc)
        logger.warning("identité MCP refusée — raison=%s sub_jeton=%s %s : la requête "
                       "est refusée, pas servie anonyme", raison, sub,
                       _decrire_jeton(token), exc_info=raison.startswith("base_"))
        if raison.startswith("base_"):
            raise IdentiteIndisponible(raison) from exc
        raise
    # Après les deux appels, jamais avant : un refus (`AliasNonResolvable`,
    # `CompteEnPause`) ne se mémorise pas — il doit se lever pour CHAQUE
    # demandeur, comme avant, plutôt que d'être rendu en valeur.
    if cache is not None:
        cache[sub] = canonique
    return canonique
