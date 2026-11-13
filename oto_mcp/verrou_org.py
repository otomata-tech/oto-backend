"""Le verrou d'org d'un jeton de délégation : un travail du runner agit dans l'org de
son travail, et dans aucune autre.

Un travail programmé s'exécute au nom de son porteur (`capabilities/runner_jobs._delegue`
émet un jeton `kind="delegation"`). Le porteur peut être membre de plusieurs orgs ; son
travail, lui, appartient à UNE org. Le jeton porte désormais cette org, posée à
l'émission (`user_api_tokens.verrou_org*`), et toute résolution qui viserait une autre org
pour le porteur est refusée :

- `roles.effective_org_role` (le seam unique derrière `is_org_member`, `is_org_admin` et
  `ownership.can_access`) ne rend aucun rôle hors de l'org du travail — AVANT l'escalade
  plateforme ;
- `session_org.set_call_org`, où aboutit tout jeton d'appel qui pose une org (`_org`,
  `_project`, `_group`, `_instance`), refuse une autre org avec un code nommé ;
- `access.current_org` rend l'org du travail en premier, jamais la maison du porteur ;
- `ownership.accessor_scope` borne les partages reçus à l'org du travail.

**Le porteur seul est concerné** (`sub` = celui du jeton) : une lecture sur un TIERS
(membres d'une org, rôle d'un autre compte) garde son chemin d'avant.

**Une seule source par requête** : une ContextVar posée au bord — l'authentification
REST (`api/base._authenticate`) et l'`on_request` MCP (`middleware/verrou_org.py`). Sans
elle, rien n'est verrouillé : un jeton humain ou d'API n'en porte jamais.

`OTO_VERROU_ORG_DELEGATION` : `enforce` (défaut), `report` (journalise ce qui serait
refusé et laisse passer) ou `off` — un retour arrière sans déploiement.

**Les rôles PLATEFORME du porteur** (10/10/2026) : le verrou d'org ne bornait que le
rôle d'ORG ; `super_admin`/`admin` restaient les siens sous le jeton, et avec eux toute
garde plateforme (`PLATFORM_ADMIN`, `SUPER_ADMIN`, l'escalade `roles.is_platform_admin`,
la visibilité des `oto_admin_*`, la gouvernance de `ownership`). Sous un jeton de
délégation, un agent hébergé n'est pas l'opérateur : `role_plateforme` ramène ces rôles
à `member` — UN seul point, `access.scope.role_effectif`, d'où tous les prédicats
plateforme dérivent. Réglage PROPRE, `OTO_VERROU_ROLES_PLATEFORME` : `enforce`
(DÉFAUT, et la valeur d'une valeur inconnue), `report` (posé explicitement seulement :
journalise ce qui serait retiré, ne retire rien) ou `off`. Indépendant du verrou
d'org : couper l'un ne coupe pas l'autre. Un agent d'opérateur qui doit garder des
outils d'admin les nomme dans sa liste (`platform_tools`, ci-dessous).
**Les outils d'admin PERMIS à un agent** (19/10/2026, revue de #1274) : sous `enforce`,
un agent ne garde le rôle plateforme de son porteur QUE pour les `oto_admin_*` que son
agent nomme (`runner_triggers.outils_plateforme`), et seulement PENDANT l'appel de l'un
d'eux (`poser_outil`, posé là où la liste du travail est tenue : `CallContextMiddleware`
et `meta.executer_cible`). Tout autre appel du même travail est lu `member`. Un agent qui
lit du contenu extérieur ne porte donc pas tous les droits d'admin : une injection de
prompt n'atteint que les outils nommés. ⚠️ Seulement pour un agent que SON PORTEUR SEUL
peut changer (`db.outils_plateforme_du_travail`) : un agent tourne sous son propriétaire
quel que soit celui qui l'a modifié, donc un agent qu'un autre peut retoucher (clé d'org,
éditeur nommé) prêterait le rôle à cet autre — sa liste ne vaut rien. Un travail enfilé à
la main ou d'une flotte (aucun `trigger_id`) n'a pas de liste. Sans outil posé (la face
REST), rien n'est permis. **La trace reste** : chaque outil exercé ainsi est journalisé,
une ligne par outil et par requête, quel que soit le mode.
"""
from __future__ import annotations

import contextvars
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: Le code nommé d'un refus. Testé par les appelants, jamais la phrase.
CODE = "org_out_of_job"
_ENV = "OTO_VERROU_ORG_DELEGATION"
_MODES = ("enforce", "report", "off")


@dataclass(frozen=True)
class Verrou:
    """Le porteur du jeton, l'org de son travail (None = aucune org : portée
    personnelle seule) et le travail, pour le journal."""
    sub: str
    org_id: Optional[int]
    job_id: Optional[int] = None


class HorsVerrou(Exception):
    """Une résolution vise une org hors du travail. Les adaptateurs traduisent."""

    def __init__(self, org_id: object, verrou: Verrou):
        self.org_id, self.verrou = org_id, verrou
        ici = (f"org {verrou.org_id}" if verrou.org_id is not None
               else "no org (personal scope only)")
        super().__init__(
            f"This hosted agent runs for its job's {ici}; org {org_id} is out of its "
            "scope. Its calls cannot target another organization.")


_COURANT: contextvars.ContextVar[Optional[Verrou]] = contextvars.ContextVar(
    "oto_verrou_org", default=None)

_ENV_ROLES = "OTO_VERROU_ROLES_PLATEFORME"
#: Les rôles plateforme qu'un jeton de délégation ne porte pas (`access.scope`).
_ROLES_PLATEFORME = ("admin", "super_admin")
#: Les seuls outils qu'une liste d'agent peut nommer (`runner_triggers.outils_plateforme`).
PREFIXE_ADMIN = "oto_admin_"
#: Le verrou dont le rôle plateforme a déjà été signalé en `report` : une ligne par
#: requête, pas une par prédicat — `get_user_role` est lu des dizaines de fois.
_ROLE_SIGNALE: contextvars.ContextVar[Optional[Verrou]] = contextvars.ContextVar(
    "oto_verrou_role_signale", default=None)
#: Les outils d'admin permis au travail de ce verrou, déjà lus en base : une lecture par
#: requête (`outils_permis`).
_PERMIS: contextvars.ContextVar[Optional[tuple[Verrou, frozenset]]] = contextvars.ContextVar(
    "oto_verrou_outils_permis", default=None)
#: L'outil dont l'appel est en cours (au canonique), posé par `poser_outil` — None hors
#: d'un appel d'outil (`tools/list`, la face REST) : rien n'y est permis.
_OUTIL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_verrou_outil", default=None)
#: Faux le temps d'une lecture qui n'EXERCE rien (la visibilité de `tools/list`) : elle
#: ne laisse pas de trace d'exercice.
_TRACER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oto_verrou_tracer", default=True)
#: Les outils déjà tracés pour ce verrou : une ligne par outil et par requête.
_TRACES: contextvars.ContextVar[Optional[tuple[Verrou, frozenset]]] = contextvars.ContextVar(
    "oto_verrou_traces", default=None)


def poser(verrou: Optional[Verrou]) -> contextvars.Token:
    """Pose le verrou de la requête — y compris à None (jeton humain, JWT) pour
    qu'aucun verrou ne survive à sa requête."""
    return _COURANT.set(verrou)


def lever(jeton: contextvars.Token) -> None:
    _COURANT.reset(jeton)


def depuis_ligne(row: Optional[dict]) -> Optional[Verrou]:
    """Le verrou porté par une ligne de jeton (`db.verify_api_token`), ou None."""
    if not row or not row.get("verrou_org"):
        return None
    org = row.get("verrou_org_id")
    job = row.get("job_id")
    return Verrou(sub=str(row["sub"]), org_id=int(org) if org is not None else None,
                  job_id=int(job) if job is not None else None)


def mode() -> str:
    """Lu à chaque appel : un basculement d'environnement mord au redémarrage suivant
    sans code. Une valeur inconnue vaut `enforce` — le mode sûr."""
    m = (os.environ.get(_ENV) or "enforce").strip().lower()
    return m if m in _MODES else "enforce"


def courant() -> Optional[Verrou]:
    """Le verrou actif, ou None (aucun, ou désactivé)."""
    v = _COURANT.get()
    if v is None or mode() == "off":
        return None
    return v


def travail_de_la_requete() -> Optional[Verrou]:
    """Le verrou posé pour la requête, QUEL QUE SOIT le mode du verrou d'org : il dit quel
    travail porte le jeton. La liste d'outils de ce travail (`outils_du_travail`) n'est pas
    le verrou d'org et ne se coupe pas avec lui (`OTO_VERROU_ORG_DELEGATION=off`)."""
    return _COURANT.get()


def borne(sub: object, *, route: str, ecart: bool) -> Optional[Verrou]:
    """Le verrou à APPLIQUER pour ce porteur — None pour un tiers, sans verrou, ou hors
    `enforce`. Pour les points qui bornent au lieu de refuser (`current_org`,
    `accessor_scope`) : en `report`, `ecart` dit si le résultat aurait changé, et
    seul ce cas est journalisé."""
    v = courant()
    if v is None or sub is None or str(sub) != v.sub:
        return None
    if mode() == "report":
        if ecart:
            logger.warning("verrou d'org (report) : %s aurait été borné à l'org %s "
                           "(travail %s)", route, v.org_id, v.job_id)
        return None
    return v


def hors(sub: Optional[str], org_id: object, *, route: str) -> bool:
    """`org_id` est-elle hors de l'org du travail, pour CE porteur ? En `report`, le dit
    au journal et rend False. Un tiers (`sub` ≠ porteur) n'est jamais concerné."""
    v = courant()
    if v is None or sub is None or str(sub) != v.sub:
        return False
    try:
        cible = int(org_id) if org_id is not None else None
    except (TypeError, ValueError):
        cible = None
    if cible is not None and cible == v.org_id:
        return False
    if cible is None and v.org_id is None:
        return False
    if mode() == "report":
        logger.warning("verrou d'org (report) : %s viserait l'org %s hors du travail %s "
                       "(org %s)", route, cible, v.job_id, v.org_id)
        return False
    logger.warning("verrou d'org : %s refusé, org %s hors du travail %s (org %s)",
                   route, cible, v.job_id, v.org_id)
    return True


def interdit(sub: Optional[str], *, route: str) -> bool:
    """Un geste interdit au porteur verrouillé QUELLE QUE SOIT l'org visée : changer
    l'org maison de l'humain (`org.set_home`, `org.clear`, `group.set_home`, la face REST
    de `group.use`) ou le faire quitter une org (`me.leave_org`). Le jeton porte une org de
    travail ; il ne déplace pas le défaut de la personne. En `report`, le dit au journal
    et rend False. Un tiers n'est jamais concerné."""
    v = courant()
    if v is None or sub is None or str(sub) != v.sub:
        return False
    if mode() == "report":
        logger.warning("verrou d'org (report) : %s aurait été refusé (travail %s)",
                       route, v.job_id)
        return False
    logger.warning("verrou d'org : %s refusé sous le travail %s (org %s)",
                   route, v.job_id, v.org_id)
    return True


def exiger(sub: Optional[str], org_id: object, *, route: str) -> None:
    """Lève `HorsVerrou` si `org_id` est hors de l'org du travail du porteur."""
    if hors(sub, org_id, route=route):
        raise HorsVerrou(org_id, courant())  # type: ignore[arg-type]


def mode_roles() -> str:
    """Le mode du verrou des rôles plateforme, lu à chaque appel. Absent, vide ou
    inconnu, il vaut `enforce` — le mode sûr ; `report` ne vaut que posé explicitement."""
    m = (os.environ.get(_ENV_ROLES) or "enforce").strip().lower()
    return m if m in _MODES else "enforce"


def poser_outil(name: Optional[str]) -> contextvars.Token:
    """Pose l'outil dont l'appel commence (`lever_outil` au retour). Au canonique : la
    liste d'un agent se tient aux noms canoniques, comme sa liste d'outils."""
    from . import deprecations
    return _OUTIL.set(deprecations.tool_canonique(name) if name else None)


def lever_outil(jeton: contextvars.Token) -> None:
    _OUTIL.reset(jeton)


def outils_permis(v: Optional[Verrou] = None) -> frozenset:
    """Les `oto_admin_*` que le travail de `v` (par défaut, celui de la requête) exerce
    avec le rôle plateforme de son porteur — vide sans travail, ou si son agent n'est pas
    au seul porteur (`db.outils_plateforme_du_travail`). Lu au plus une fois par requête.
    Seuls les `oto_admin_*` comptent : un autre nom n'ouvre rien, même stocké."""
    v = v if v is not None else _COURANT.get()
    if v is None or v.job_id is None:
        return frozenset()
    deja = _PERMIS.get()
    if deja is not None and deja[0] is v:
        return deja[1]
    from . import db
    permis = outils_d_admin(db.outils_plateforme_du_travail(v.job_id))
    _PERMIS.set((v, permis))
    return permis


def outils_d_admin(noms) -> frozenset:
    """Ce qu'une liste `outils_plateforme` stockée ouvre vraiment : ses `oto_admin_*`,
    rien d'autre. Seul filtre, pour le travail en cours (`outils_permis`) comme pour
    celui qu'on s'apprête à lancer (`_outils_plateforme.exiger_un_lancement_sans_role_
    plateforme`)."""
    return frozenset(n for n in (noms or ())
                     if isinstance(n, str) and n.startswith(PREFIXE_ADMIN))


class lecture_pour_outil:
    """Le rôle qu'aurait le porteur PENDANT l'appel de `name`, lu sans l'exercer (la
    visibilité) : pose l'outil, coupe la trace, rétablit tout en sortant."""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self._jetons = (_OUTIL.set(self.name), _TRACER.set(False))
        return self

    def __exit__(self, *exc):
        _TRACER.reset(self._jetons[1])
        _OUTIL.reset(self._jetons[0])
        return False


def _tracer(v: Verrou, outil: str, role: str, mode: str, *, permis: bool) -> None:
    """La trace d'un outil d'admin appelé sous un jeton de délégation avec un rôle
    plateforme : exercé (permis par la liste de l'agent, quel que soit le mode — revue de
    #1274) ou lu `member` (hors liste, en `enforce`). Une ligne par outil et par requête ;
    aucune pour une lecture qui n'exerce rien (`lecture_pour_outil`)."""
    if not _TRACER.get():
        return
    deja = _TRACES.get()
    vus = deja[1] if deja is not None and deja[0] is v else frozenset()
    if outil in vus:
        return
    _TRACES.set((v, vus | {outil}))
    if permis:
        logger.warning("verrou des rôles plateforme (%s) : le travail %s (org %s) exerce "
                       "`%s` avec le rôle `%s` — permis par la liste de son agent",
                       mode, v.job_id, v.org_id, outil, role)
    else:
        logger.warning("verrou des rôles plateforme (%s) : le travail %s (org %s) appelle "
                       "`%s`, hors de la liste de son agent — rôle `%s` lu `member`",
                       mode, v.job_id, v.org_id, outil, role)


def role_plateforme(sub: Optional[str], role: str) -> str:
    """Le rôle plateforme de `sub` sous la requête courante : `member` à la place
    d'`admin`/`super_admin` pour le PORTEUR d'un jeton de délégation, en `enforce` —
    sauf pendant l'appel d'un outil que son agent permet (`outils_permis`), toujours
    tracé.

    Lit la ContextVar BRUTE, pas `courant()` : `OTO_VERROU_ORG_DELEGATION=off` ne doit
    pas rendre au porteur ses rôles plateforme — chaque réglage coupe son verrou. Un
    tiers (`sub` ≠ porteur) n'est jamais concerné : lister les opérateurs d'une org
    reste exact."""
    if role not in _ROLES_PLATEFORME:
        return role
    v = _COURANT.get()
    if v is None or sub is None or str(sub) != v.sub:
        return role
    m = mode_roles()
    if m == "off":
        return role
    outil = _OUTIL.get()
    # Le préfixe AVANT la base : seul un `oto_admin_*` peut être permis — un appel
    # ordinaire (le gros des appels d'un agent) ne lit rien.
    if outil is not None and outil.startswith(PREFIXE_ADMIN) and outil in outils_permis(v):
        _tracer(v, outil, role, m, permis=True)
        return role
    if m == "report":
        if _ROLE_SIGNALE.get() is not v:
            _ROLE_SIGNALE.set(v)
            logger.warning("verrou des rôles plateforme (report) : le porteur du travail "
                           "%s (org %s) agit avec le rôle `%s` — `enforce` le lirait "
                           "`member`", v.job_id, v.org_id, role)
        return role
    if outil is not None and outil.startswith(PREFIXE_ADMIN):
        _tracer(v, outil, role, m, permis=False)
    return "member"
