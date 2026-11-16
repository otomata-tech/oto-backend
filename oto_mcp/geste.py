"""Le GESTE en cours : qui écrit, par quelle face, sous quel identifiant (oto#273, M2).

Le journal des révisions de ligne (`db/journal_revisions.py`) est écrit par un
déclencheur, qui ne voit que la base. Ce qu'il ne peut pas deviner — l'acteur, la
source, le geste, le run — le serveur le lui passe par des réglages de transaction
(`oto.acteur`, `oto.source`, `oto.geste_id`, `oto.run_id`), posés par
`db.estampille.ecriture_de_lignes`, le point de passage de toute écriture de ligne.

Ce module porte le CONTEXTE d'où ces valeurs sont tirées : une `ContextVar` par
requête, du même modèle que les axes d'appel de `session_org` — posée à l'entrée d'une
face, remise à sa valeur d'avant à la sortie, jamais un état de module.

## Qui pose quoi

| face | où | `source` | `acteur` | `geste_id` |
|---|---|---|---|---|
| outil MCP | `calllog.ToolCallLogger` | `agent` | sub de l'appelant | `call_uid` de la ligne `tool_calls` |
| REST, session (JWT) | `capabilities._rest_adapter` | `console` | sub du porteur | neuf, versé à `tool_calls.call_uid` |
| REST, jeton `oto_` | idem | `api` | sub du porteur | idem |
| REST, secret de worker | idem | `system` | `worker_sub` | idem |
| upload signé vers un tableau | `api.uploads` | `upload` | sub scellé au jeton | `jti` du jeton |
| `donnees_d_origine=true` (upload compris, scellé au jeton) | le store (`import_si_donnees_d_origine`) | `import` | inchangé | inchangé |
| travail de fond | `interne(nom)` | `system` | `service:<nom>` | neuf |
| rien de tout ça | — | `system` | NULL | neuf, par transaction |

Le run n'est pas dans le geste : c'est un axe d'appel à part entière
(`session_org.current_call_or_session_run`), posé par la face MCP (`_run_id=`, sinon le run
actif de la session, #830) comme par la face REST (`X-Oto-Run`). Il est lu au moment d'écrire.
"""
from __future__ import annotations

import contextvars
import functools
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator, Optional

IMPORT = "import"
AGENT = "agent"
CONSOLE = "console"
API = "api"
UPLOAD = "upload"
SYSTEM = "system"
SOURCES = frozenset({IMPORT, AGENT, CONSOLE, API, UPLOAD, SYSTEM})


@dataclass(frozen=True)
class Geste:
    source: str
    acteur: Optional[str]
    geste_id: str
    #: L'OUTIL qui écrit : le nom de l'outil MCP de l'appel (`oto_call` déplié vers sa
    #: cible), celui qu'un travail de fond rejoue (`jev_rows` en tâche de fond), ou
    #: celui qu'un `data_write(via=…)` a prouvé dans son run. `None` = la face ne le
    #: sait pas (console, API, maintenance) — jamais deviné. C'est ce que lit l'en-tête
    #: d'une colonne (« trouvé par Jev »), `db/historique.sources_des_colonnes`.
    outil: Optional[str] = None


_GESTE: contextvars.ContextVar[Optional[Geste]] = contextvars.ContextVar(
    "oto_geste", default=None)

#: Le FOURNISSEUR derrière la valeur écrite, plus fin que l'outil et propre à CHAQUE
#: écriture : une cascade (`waterfall_email_act(op="update_rows")`) trouve l'email d'une
#: ligne chez Hunter et celui de la suivante chez FullEnrich, sous le même appel. Nommé
#: par l'OUTIL de l'étape qui a trouvé (`hunter_email_finder`) : c'est ce qu'un client
#: résout vers son connecteur. Hors
#: du geste pour cette raison : il se pose autour d'UNE écriture (`par_fournisseur`),
#: pas pour la requête entière.
_FOURNISSEUR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_geste_fournisseur", default=None)


def nouvel_identifiant() -> str:
    return uuid.uuid4().hex


def service(nom: str) -> str:
    """L'acteur d'une écriture interne : `service:<nom>`, jamais un sub."""
    return f"service:{nom}"


def _verifier(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"source de geste inconnue : {source!r} "
                         f"(attendu : {', '.join(sorted(SOURCES))})")
    return source


def poser(source: str, acteur: Optional[str],
          geste_id: Optional[str] = None, *,
          outil: Optional[str] = None) -> contextvars.Token:
    """Pose le geste de la requête courante. Rend le jeton à passer à `retirer`."""
    return _GESTE.set(Geste(_verifier(source), acteur or None,
                            geste_id or nouvel_identifiant(), outil or None))


def retirer(jeton: contextvars.Token) -> None:
    _GESTE.reset(jeton)


def courant() -> Optional[Geste]:
    return _GESTE.get()


@contextmanager
def portee(source: str, acteur: Optional[str],
          geste_id: Optional[str] = None, *,
          outil: Optional[str] = None) -> Iterator[Geste]:
    jeton = poser(source, acteur, geste_id, outil=outil)
    try:
        yield _GESTE.get()
    finally:
        retirer(jeton)


_GARDER = object()


@contextmanager
def comme(source: str, acteur=_GARDER) -> Iterator[Geste]:
    """Requalifie le geste EN COURS : même identifiant, autre source (et, si donné,
    autre acteur). Sans geste en cours, en ouvre un neuf — l'acteur est alors celui
    passé, ou NULL."""
    avant = _GESTE.get()
    if avant is None:
        g = Geste(_verifier(source), None if acteur is _GARDER else acteur,
                  nouvel_identifiant())
    else:
        g = replace(avant, source=_verifier(source),
                    **({} if acteur is _GARDER else {"acteur": acteur}))
    jeton = _GESTE.set(g)
    try:
        yield g
    finally:
        _GESTE.reset(jeton)


@contextmanager
def via(outil: str) -> Iterator[Optional[Geste]]:
    """Requalifie l'OUTIL du geste en cours, le temps d'une écriture : `data_write(via=…)`
    écrit ce qu'un autre outil du même run a trouvé, et le journal le nomme. Le geste,
    l'acteur et la source restent ceux de l'appel. Sans geste en cours : rien à
    requalifier, rien n'est inventé."""
    avant = _GESTE.get()
    if avant is None:
        yield None
        return
    jeton = _GESTE.set(replace(avant, outil=outil or None))
    try:
        yield _GESTE.get()
    finally:
        _GESTE.reset(jeton)


@contextmanager
def par_fournisseur(nom: Optional[str]) -> Iterator[None]:
    """Le fournisseur de la valeur écrite DANS ce bloc (`hunter`, `fullenrich`…)."""
    jeton = _FOURNISSEUR.set(str(nom).strip().lower()[:128] if nom else None)
    try:
        yield
    finally:
        _FOURNISSEUR.reset(jeton)


def interne(nom: str):
    """Le geste d'un travail de FOND (boucle, maintenance) : `system`, acteur
    `service:<nom>`, un identifiant neuf. Gestionnaire de contexte ou décorateur."""
    return portee(SYSTEM, service(nom))


def import_si_donnees_d_origine(fn):
    """Décorateur des écritures du store : `donnees_d_origine=True` requalifie le geste
    en `import` le temps de l'appel. L'argument est nommé dans toutes les signatures."""
    @functools.wraps(fn)
    def _enveloppe(*args, **kwargs):
        if not kwargs.get("donnees_d_origine"):
            return fn(*args, **kwargs)
        with comme(IMPORT):
            return fn(*args, **kwargs)
    return _enveloppe


def source_rest(token_kind: Optional[str]) -> str:
    """La source d'une requête REST, d'après le porteur que l'authentification a
    résolu : une session interactive (JWT, aucun jeton nommé) est la console, un
    secret de worker est la plateforme, tout jeton `oto_` (utilisateur ou délégation)
    est l'API."""
    if token_kind is None:
        return CONSOLE
    if token_kind == "worker":
        return SYSTEM
    return API


def estampille() -> dict:
    """Les six réglages de transaction que lit le déclencheur du journal.

    Sans geste en cours : `source='system'`, acteur NULL, un identifiant neuf — une
    écriture dont le serveur ne connaît pas l'origine le DIT, elle ne s'invente pas
    d'auteur."""
    from . import session_org
    g = _GESTE.get()
    return {
        "acteur": g.acteur if g else None,
        "run_id": session_org.current_call_or_session_run(),
        "source": g.source if g else SYSTEM,
        "geste_id": g.geste_id if g else nouvel_identifiant(),
        "outil": g.outil if g else None,
        "fournisseur": _FOURNISSEUR.get(),
    }
