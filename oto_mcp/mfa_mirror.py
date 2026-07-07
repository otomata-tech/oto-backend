"""MFA obligatoire par org — miroir « organization Logto ».

Une org oto qui active `require_mfa` obtient une **organization Logto miroir**
(`isMfaRequired=true`) dont les membres sont synchronisés **par `sub`**. Combiné
au réglage tenant `organizationRequiredMfaPolicy=Mandatory` (Sign-in Experience,
posé UNE fois côté infra), Logto force alors le 2ᵉ facteur au **login ordinaire**
de tout membre — sans token org-scopé, sans toucher au flux de login d'oto : le
MFA d'org est évalué par Logto sur l'**appartenance** de l'user (agrégation de
toutes ses orgs), pas sur l'org du token. Voir `docs/auth-logto.md` §MFA par org.

Séparation des rôles : la **source de vérité** (org, membres, droits) reste le PG
oto ; l'org Logto n'est qu'un **miroir d'enforcement** au login, sans aucune
autorité. Ce module ne fait que refléter le PG vers Logto (add-on par `sub`, ce
qui évite le piège phantom-user des emails).

⚠️ **Pas de fail-open à l'activation** : on provisionne AVANT de poser le drapeau
côté appelant, et on **lève** si Logto échoue — un contrôle de sécurité ne doit
jamais laisser l'org croire le MFA actif alors qu'il ne l'est pas.
"""
from __future__ import annotations

import logging

import requests

from . import org_store
from .oauth_facade import _UA, _logto_base, _mgmt_token

_log = logging.getLogger("oto_mcp.mfa_mirror")
_TIMEOUT = 15


def _headers() -> dict:
    return {"Authorization": f"Bearer {_mgmt_token()}", "User-Agent": _UA,
            "Content-Type": "application/json"}


# ── Client Management API — organizations Logto ───────────────────────────────
def _create_logto_org(name: str, description: str = "") -> str:
    r = requests.post(f"{_logto_base()}/api/organizations",
                      json={"name": name, "description": description},
                      headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()["id"]


def _set_mfa_required(logto_org_id: str, required: bool) -> None:
    r = requests.patch(f"{_logto_base()}/api/organizations/{logto_org_id}",
                       json={"isMfaRequired": bool(required)},
                       headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()


def _list_logto_members(logto_org_id: str) -> set[str]:
    """Subs membres de l'org Logto (paginé, page_size max 100 côté Logto)."""
    out: set[str] = set()
    page = 1
    while True:
        r = requests.get(f"{_logto_base()}/api/organizations/{logto_org_id}/users",
                         params={"page": page, "page_size": 100},
                         headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.update(u["id"] for u in batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def _add_logto_members(logto_org_id: str, subs: list[str]) -> None:
    if not subs:
        return
    r = requests.post(f"{_logto_base()}/api/organizations/{logto_org_id}/users",
                      json={"userIds": subs}, headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()


def _remove_logto_member(logto_org_id: str, sub: str) -> None:
    r = requests.delete(f"{_logto_base()}/api/organizations/{logto_org_id}/users/{sub}",
                        headers=_headers(), timeout=_TIMEOUT)
    if r.status_code not in (204, 404):   # 404 = déjà absent → tolérer
        r.raise_for_status()


# ── Sync PG oto → org Logto miroir ────────────────────────────────────────────
def _member_subs(org_id: int) -> set[str]:
    """TOUS les subs membres de l'org (une ligne `org_members` = une appartenance).
    ⚠️ NE PAS filtrer sur `is_active` : ce flag marque l'org active PAR DÉFAUT du sub
    (une seule par sub), pas l'appartenance — filtrer dessus exclurait les membres
    dont l'org active est ailleurs → ils échapperaient au MFA (fuite d'enforcement)."""
    return {m["sub"] for m in org_store.list_org_members(org_id)}


def _org_label(org_id: int) -> tuple[str, str]:
    org = org_store.get_org(org_id) or {}
    name = org.get("name") or f"org-{org_id}"
    return (f"[oto:{org_id}] {name}",
            f"Miroir MFA de l'org oto #{org_id} (isMfaRequired). Géré par oto — "
            "ne pas éditer à la main.")


def sync_members(org_id: int) -> None:
    """Réconcilie l'appartenance de l'org Logto miroir avec les membres actifs de
    l'org oto (ajoute les manquants, retire les partis). No-op si l'org n'a pas de
    miroir (pas de `logto_org_id`). Auto-réparateur : un ajout/retrait manqué est
    rattrapé au prochain appel. Lève si Logto échoue."""
    logto_org_id = org_store.get_org_mfa(org_id)["logto_org_id"]
    if not logto_org_id:
        return
    want = _member_subs(org_id)
    have = _list_logto_members(logto_org_id)
    _add_logto_members(logto_org_id, sorted(want - have))
    for sub in sorted(have - want):
        _remove_logto_member(logto_org_id, sub)


def ensure_mirror(org_id: int) -> str:
    """Active le MFA pour l'org : crée/retrouve l'organization Logto miroir, pose
    `isMfaRequired=true`, synchronise les membres, mémorise `logto_org_id`. Renvoie
    le `logto_org_id`. **Lève** si Logto échoue (pas de fail-open sécurité)."""
    logto_org_id = org_store.get_org_mfa(org_id)["logto_org_id"]
    if not logto_org_id:
        name, desc = _org_label(org_id)
        logto_org_id = _create_logto_org(name, desc)
        org_store.set_org_logto_org_id(org_id, logto_org_id)
    _set_mfa_required(logto_org_id, True)
    sync_members(org_id)
    _log.info("MFA mirror activé pour org %s (logto_org=%s)", org_id, logto_org_id)
    return logto_org_id


def disable_mirror(org_id: int) -> None:
    """Désactive le MFA pour l'org : lève `isMfaRequired` sur l'org Logto miroir (si
    elle existe). On **conserve** l'organization Logto et son id (réactivation
    rapide, pas de recréation/re-sync) — seul le drapeau retombe à false. Lève si
    Logto échoue."""
    logto_org_id = org_store.get_org_mfa(org_id)["logto_org_id"]
    if logto_org_id:
        _set_mfa_required(logto_org_id, False)
        _log.info("MFA mirror désactivé pour org %s (logto_org=%s)", org_id, logto_org_id)


def on_membership_changed(org_id: int) -> None:
    """Hook à appeler après tout ajout/retrait de membre d'une org oto (B3).
    Délègue à `sync_members` (no-op si l'org n'impose pas le MFA). **Best-effort** :
    loggue et n'empêche pas l'opération d'appartenance ; le prochain sync rattrape."""
    try:
        sync_members(org_id)
    except Exception:
        _log.exception("MFA mirror: sync membres échoué pour org %s", org_id)
