"""Le COMPTE — ce que le dashboard lit de la personne connectée.

Trois routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009)
— mêmes chemins, mêmes codes, même corps sur le fil :

- `GET /api/me`                  → profil + org/équipe EFFECTIVES (seam `current_org`,
                                   ADR 0023) + rôle + statut des connecteurs + flags
- `GET /api/me/calls`            → journal des appels MCP de l'appelant, dans SON org
- `GET /api/me/activity-summary` → agrégats du même flux, fenêtre `?days=`

Ce qui change est ce que la surface DIT d'elle-même. `GET /api/me` est la première
requête de tout front qui se branche (dashboard, extension, front partenaire) et
l'OpenAPI dérivé n'en décrivait RIEN — l'intégrateur découvrait `active_org_readonly`
ou `features.billing` en sondant. Les trois réponses sont désormais déclarées.

**Pas de face MCP** (`mcp=None`) : l'identité de la session MCP est `oto_whoami` (org
et équipe RÉSOLUES pour cette conversation), et l'activité d'un membre se lit par les
lentilles de monitoring. Un second chemin dirait la même chose autrement.

⚠️ **Tolérance de saisie PRÉSERVÉE sur `?limit=` et `?days=`.** Les handlers d'origine
faisaient `int(...)` sous `try/except ValueError` et retombaient sur le défaut : un
`?days=abc` rendait 200 avec la fenêtre par défaut, jamais un 400. Pydantic, lui,
refuserait. Les validateurs `mode="before"` ci-dessous rejouent exactement l'ancien
repli — le schéma OpenAPI annonce quand même un entier, qui est la valeur ATTENDUE.
Sans eux, la migration transformerait un lien mal formé en erreur visible.

Les deux lentilles d'activité sont scopées `(sub, org active)` : un membre voit SA
propre activité dans l'org chargée — à ne pas confondre avec `/api/admin/monitoring/*`
et `/api/orgs/{id}/monitoring/*`, qui agrègent tout le monde (`capabilities/monitoring.py`,
`capabilities/org_monitoring.py`).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, field_validator

from .. import access, billing, db, group_store, org_store
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


def _repli(defaut):
    """Validateur `before` qui rejoue le `try/except ValueError → défaut` historique.

    Rendre `defaut` (et non lever) pour une valeur illisible est le comportement SERVI
    depuis toujours sur ces deux lentilles ; le changer ferait apparaître des 400 sur
    des liens déjà en circulation."""
    def _coerce(v):
        if v is None or v == "":
            return defaut
        try:
            return int(v)
        except (TypeError, ValueError):
            return defaut
    return _coerce


# --- Entrées ----------------------------------------------------------------

class MeInput(BaseModel):
    """Aucun paramètre : le compte lu est celui du porteur du jeton."""


class MyCallsInput(BaseModel):
    limit: int = 200
    # Nom d'outil EXACT (pas un préfixe) — le filtre est une égalité SQL.
    tool: Optional[str] = None
    # `'1'` ou `'true'` = n'afficher que les échecs. Toute autre valeur = tout.
    errors: Optional[str] = None
    days: Optional[int] = None

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, v):
        return _repli(200)(v)

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v):
        return _repli(None)(v)


class ActivitySummaryInput(BaseModel):
    days: int = 7

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v):
        return _repli(7)(v)


# --- Sorties ----------------------------------------------------------------

class MeFeatures(BaseModel):
    """Flags par-DÉPLOIEMENT (dark launch), pas par-utilisateur : le dashboard dérive
    sa navigation de l'effet backend plutôt que de dupliquer un flag côté front."""
    billing: bool


class MeView(BaseModel):
    """Le compte tel que le voit le porteur du jeton.

    ⚠️ **`active_*` ≠ `home_*`, et c'est le piège de cette réponse.** `active_org` est
    l'org EFFECTIVE de la requête (seam `current_org`, ADR 0023) : elle reflète la
    consultation `X-Oto-Org` si un opérateur en a posé une, sinon l'org maison.
    `home_org` est le défaut PERSISTANT. Un front qui scope ses vues sur `home_org`
    montre les données d'une autre org que celle qu'il affiche."""
    sub: str
    email: Optional[str] = None
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    # 'en' | 'fr' | null — null = non définie, le front retombe sur la langue du
    # navigateur. Écrite par `PUT /api/me/locale`.
    locale: Optional[str] = None
    # Rôle PLATEFORME (user | admin | super_admin) — à ne pas confondre avec `org_role`.
    role: Optional[str] = None
    active_org: Optional[int] = None
    active_org_name: Optional[str] = None
    # Logo EFFECTIF : upload sinon dérivé logo.dev du domaine déclaré.
    active_org_logo_url: Optional[str] = None
    org_role: Optional[str] = None
    # Opérateur plateforme consultant une org tierce : org active posée, aucun rôle
    # réel dedans. Le backend rejette déjà toute mutation (GET-only au middleware) —
    # ce flag sert au bandeau et au mode lecture du front. Un membre : toujours False.
    active_org_readonly: bool = False
    # Espace privé mono-membre : le front adapte son vocabulaire (un « solo » ne lit
    # jamais « org » ni « équipe »).
    active_org_is_personal: bool = False
    active_org_require_mfa: bool = False
    home_org: Optional[int] = None
    home_org_name: Optional[str] = None
    active_group: Optional[int] = None
    active_group_name: Optional[str] = None
    group_role: Optional[str] = None
    home_group: Optional[int] = None
    home_group_name: Optional[str] = None
    features: MeFeatures
    # Accès effectif par connecteur (mode de clé, quota, état) — la forme varie avec
    # le registre de connecteurs, donc un objet ouvert plutôt qu'une énumération qui
    # mentirait au premier connecteur ajouté.
    providers: dict[str, Any]


class ToolCall(BaseModel):
    """Un appel MCP du journal. `tool_name`/`called_at` sont des ALIAS SQL (colonnes
    `tool`/`created_at`) : ce sont eux qui sont servis, pas les noms de colonnes."""
    id: int
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    tool_name: Optional[str] = None
    called_at: Optional[Any] = None
    duration_ms: Optional[int] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    org_id: Optional[int] = None
    # Id d'événement Sentry, quand l'échec en a produit un — permet d'ouvrir la trace
    # sans chercher par horodatage.
    sentry_event_id: Optional[str] = None


class MyCallsView(BaseModel):
    """Récent d'abord. La liste est BORNÉE (`limit`, plafond dur 1000 côté store) :
    une absence n'est pas une preuve d'inexistence, c'est peut-être la page suivante."""
    calls: list[ToolCall]


class ByTool(BaseModel):
    tool_name: Optional[str] = None
    calls: int
    errors: int
    avg_ms: Optional[int] = None
    p95_ms: Optional[int] = None


class ByUser(BaseModel):
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    calls: int
    errors: int


class ByDay(BaseModel):
    day: str
    calls: int
    errors: int


class ActivitySummaryView(BaseModel):
    """Agrégats de MON activité dans l'org active. Un workspace neuf rend des agrégats
    vides — c'est le résultat attendu, pas une panne.

    `by_user` ne porte qu'une ligne ici (la fenêtre est déjà scopée au demandeur) : la
    forme est celle des lentilles de monitoring, dont ces agrégats sont la restriction."""
    since_days: int
    total_calls: int
    error_count: int
    active_users: int
    by_tool: list[ByTool]
    by_user: list[ByUser]
    by_day: list[ByDay]


# --- Handlers ---------------------------------------------------------------

def _me(ctx: ResolvedCtx, inp: MeInput) -> dict:
    sub = ctx.sub
    user = db.get_user(sub) or {}
    status = access.status_for(sub)
    # `active_org` = org EFFECTIVE (ADR 0023) : via `current_org` elle reflète
    # la consultation view-as (header X-Oto-Org) si posée, sinon la maison. Le
    # front scope ses vues là-dessus. `home_org` (ci-dessous) = le défaut brut.
    active_org = access.current_org(sub)
    active_org_name = None
    active_org_logo_url = None
    org_role = None
    active_org_require_mfa = False
    if active_org is not None:
        o = org_store.get_org(active_org)
        active_org_name = o["name"] if o else None
        # Logo EFFECTIF (upload > dérivé logo.dev du domaine déclaré).
        active_org_logo_url = org_store.effective_logo_url(o) if o else None
        org_role = org_store.get_org_role(active_org, sub)
        # MFA obligatoire de l'org (2ᵉ facteur imposé au login des membres,
        # enforcé par Logto via l'org miroir — cf. mfa_mirror).
        active_org_require_mfa = org_store.get_org_mfa(active_org)["require_mfa"]
    # Consultation d'une org tierce EN LECTURE SEULE par un opérateur plateforme :
    # org active posée (par X-Oto-Org) mais aucun rôle réel dans cette org. Le front
    # affiche un bandeau + traite l'écran en lecture (le backend rejette déjà toute
    # mutation — GET-only au middleware). Un membre a toujours un rôle → False.
    active_org_readonly = (
        active_org is not None and org_role is None
        and access.is_platform_operator(sub)
    )
    # Org perso (espace privé mono-membre) : le front adapte son vocabulaire
    # (principe 9 du CDC connecteurs — un « solo » ne lit jamais « org »/« équipe »).
    active_org_is_personal = (
        active_org is not None and org_store.is_personal_org(active_org))
    # Org MAISON (défaut persistant, colonne) — exposée distinctement pour que
    # le front affiche « ton défaut » et l'action « définir comme maison ».
    home_org = org_store.get_active_org(sub)
    home_org_name = None
    if home_org is not None and home_org != active_org:
        ho = org_store.get_org(home_org)
        home_org_name = ho["name"] if ho else None
    elif home_org is not None:
        home_org_name = active_org_name
    # Sous-palier groupe (ADR 0012) : équipe EFFECTIVE (consultation ?? maison,
    # ADR 0023) + rôle effectif (escalade). `home_group` = défaut persistant.
    active_group = access.current_group(sub)
    active_group_name = None
    group_role = None
    if active_group is not None:
        from .. import roles
        g = group_store.get_group(active_group)
        active_group_name = g["name"] if g else None
        group_role = roles.effective_group_role(sub, active_group)
    home_group = group_store.get_active_group(sub)
    home_group_name = None
    if home_group is not None and home_group != active_group:
        hg = group_store.get_group(home_group)
        home_group_name = hg["name"] if hg else None
    elif home_group is not None:
        home_group_name = active_group_name
    return {
        "sub": sub,
        "email": user.get("email"),
        "name": user.get("name"),
        "avatar_url": user.get("avatar_url"),
        # Préférence de langue de l'UI dashboard ('en'|'fr'), NULL = non définie
        # (le front retombe sur la langue du navigateur). Écrite via PUT /api/me/locale.
        "locale": user.get("locale"),
        "role": status["role"],
        "active_org": active_org,
        "active_org_name": active_org_name,
        "active_org_logo_url": active_org_logo_url,
        "org_role": org_role,
        "active_org_readonly": active_org_readonly,
        "active_org_is_personal": active_org_is_personal,
        "active_org_require_mfa": active_org_require_mfa,
        "home_org": home_org,
        "home_org_name": home_org_name,
        "active_group": active_group,
        "active_group_name": active_group_name,
        "group_role": group_role,
        "home_group": home_group,
        "home_group_name": home_group_name,
        # Feature flags par-déploiement (dark launch) : le dashboard dérive sa
        # nav de l'effet backend (ex. billing masqué en prod tant que le PSP
        # n'est pas live) — une seule source, pas de flag front dupliqué.
        "features": {"billing": billing.is_enabled()},
        # crunchbase = connecteur `personal_session` standard → exposé dans
        # `providers` (comme brevo), plus de bloc dédié (ADR 0026).
        "providers": status["providers"],
    }


def _my_calls(ctx: ResolvedCtx, inp: MyCallsInput) -> dict:
    calls = db.list_tool_calls(
        limit=inp.limit,
        sub=ctx.sub,
        org_id=access.current_org(ctx.sub),
        tool_name=inp.tool or None,
        errors_only=inp.errors in ("1", "true"),
        since_days=inp.days,
    )
    return {"calls": calls}


def _activity_summary(ctx: ResolvedCtx, inp: ActivitySummaryInput) -> dict:
    return db.tool_call_stats(since_days=inp.days,
                              org_id=access.current_org(ctx.sub), sub=ctx.sub)


_DOC_ME = (
    "Le compte de l'appelant : identité, rôle plateforme, org et équipe EFFECTIVES "
    "(celles de la requête — la consultation `X-Oto-Org` l'emporte sur la maison), "
    "rôles associés, flags de déploiement et accès effectif par connecteur. C'est la "
    "première requête d'un front qui se branche : tout le reste s'y scope."
)
_DOC_CALLS = (
    "Le journal de MES appels MCP dans l'org active, récent d'abord. Filtres : "
    "`limit` (défaut 200, plafond 1000), `tool` (nom EXACT), `errors=1` (échecs "
    "seuls), `days` (fenêtre). Ne montre jamais les appels d'un autre membre ni ceux "
    "émis sous une autre org — pour agréger tout le monde, ce sont les lentilles de "
    "monitoring d'org ou de plateforme."
)
_DOC_SUMMARY = (
    "Les agrégats de MON activité dans l'org active sur `days` jours (défaut 7) : "
    "total, échecs, membres actifs, et les ventilations par outil, par membre et par "
    "jour. Un workspace neuf rend des agrégats vides."
)

CAPABILITIES += [
    Capability(
        key="me.get", handler=_me, Input=MeInput, authz=SUB_ONLY,
        Output=MeView, description=_DOC_ME,
        mcp=None,   # l'identité d'une session MCP est `oto_whoami`
        rest=RestBinding("GET", "/api/me"),
    ),
    Capability(
        key="me.calls", handler=_my_calls, Input=MyCallsInput, authz=SUB_ONLY,
        Output=MyCallsView, description=_DOC_CALLS,
        mcp=None,
        rest=RestBinding("GET", "/api/me/calls"),
    ),
    Capability(
        key="me.activity_summary", handler=_activity_summary,
        Input=ActivitySummaryInput, authz=SUB_ONLY,
        Output=ActivitySummaryView, description=_DOC_SUMMARY,
        mcp=None,
        rest=RestBinding("GET", "/api/me/activity-summary"),
    ),
]
