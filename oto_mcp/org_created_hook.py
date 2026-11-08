"""Avertit un service externe qu'une org vient d'être créée — au plus un POST, sans jamais
bloquer ni faire échouer la création.

**Pourquoi.** Certains droits d'une org neuve sont posés par un service à part (les clés
plateforme payantes d'une instance commerciale : il décide, selon la facturation, qui les
reçoit). Il les posait à son prochain passage périodique — des minutes pendant lesquelles
l'org n'a pas les connecteurs que son plan comprend. Le crochet le prévient à la
création : il peut agir tout de suite. Son passage périodique reste le rattrapage, donc
un appel perdu coûte une attente, jamais un droit.

**Config, sans défaut** (une instance qui n'en veut pas ne voit rien changer) :

- `OTO_ORG_CREATED_HOOK_URL` — l'adresse appelée en POST ; `{org_id}` y est remplacé par
  l'identifiant de l'org. Vide = pas de crochet.
- `OTO_ORG_CREATED_HOOK_TOKEN` — envoyé en `Authorization: Bearer`. Vide = pas de crochet
  non plus : on n'envoie jamais un appel de service sans secret.

**Tenant primaire seulement.** Le crochet ne part que pour une org du tenant primaire de
l'instance (`tenancy.primary_slug`) : l'org d'un tenant hébergé ne regarde pas le service
appelé. Tenant primaire non déclaré = pas d'appel, on ne devine pas.

**Garanties.** Appelé APRÈS le commit de l'INSERT (le service rappelle l'API et doit y
trouver l'org). Fil dédié : la création n'attend ni la réponse ni le délai. Toute erreur
est journalisée en `error` (Sentry la voit) puis avalée. Le corps est vide : l'org vient
du chemin.
"""
from __future__ import annotations

import logging
import os
import threading

import requests

from . import tenancy

logger = logging.getLogger(__name__)

_TIMEOUT_S = 60


def _config() -> "tuple[str, str] | None":
    url = os.environ.get("OTO_ORG_CREATED_HOOK_URL", "").strip()
    token = os.environ.get("OTO_ORG_CREATED_HOOK_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def _post(org_id: int, url: str, token: str) -> None:
    try:
        r = requests.post(url.replace("{org_id}", str(org_id)),
                          headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT_S)
        if r.status_code >= 400:
            logger.error("org-created hook: org=%s answered %s", org_id, r.status_code)
    except Exception as e:  # noqa: BLE001 — un crochet ne fait jamais échouer une création
        logger.error("org-created hook: org=%s failed: %s", org_id, type(e).__name__)


def notify(org_id: int, tenant_slug: "str | None") -> None:
    """Prévient le service configuré que `org_id` existe, si elle relève du tenant
    primaire. N'attend pas, ne lève pas.

    `tenant_slug` est ce que `config.tenant_slug_for` rend à `create_org` : **`None`
    pour le tenant primaire** (le cas de toute org d'un client du tenant primaire),
    son slug sinon. Le comparer au seul slug primaire laissait passer `None` pour un
    autre tenant : le crochet ne partait pour aucune org, et une org neuve attendait
    le prochain passage du service (mesuré le 08/11/2026 : org 525 créée à 17:27:55,
    clés posées à 17:34 par le passage, aucun POST reçu)."""
    cfg = _config()
    if cfg is None:
        return
    try:
        if tenant_slug is not None and tenant_slug != tenancy.primary_slug():
            return
    except tenancy.TenantPrimaireNonDeclare:
        logger.error("org-created hook: tenant primaire non déclaré — org=%s non notifiée", org_id)
        return
    threading.Thread(target=_post, args=(org_id, *cfg), daemon=True,
                     name=f"org-created-hook-{org_id}").start()
