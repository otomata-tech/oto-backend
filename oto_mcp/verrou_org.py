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


def exiger(sub: Optional[str], org_id: object, *, route: str) -> None:
    """Lève `HorsVerrou` si `org_id` est hors de l'org du travail du porteur."""
    if hors(sub, org_id, route=route):
        raise HorsVerrou(org_id, courant())  # type: ignore[arg-type]
