"""Aide PARTAGÉE « marquer une ligne de coffre rejetée » (oto#25 lot b2).

Extrait de `capabilities/connectors/verify.py` (seul écrivain jusqu'ici, sous les
noms privés `_FLAGGABLE` / `_record_health`) pour que les modules qui RECONNAISSENT
eux-mêmes un grant mort (le motif `invalid_grant` et ses équivalents — aujourd'hui
atlassian, folk, salesforce, zoho ; google au refresh reste EXCLU, WIP concurrent
sur son retour OAuth) marquent la ligne RÉELLEMENT servie sans dupliquer le geste
ni sa garde. `verify.py` importe ce module à la place de ses définitions locales —
refactor pur, son comportement ne change pas.

Deux éléments :

- `FLAGGABLE_SCOPES` : les paliers dont la portée ne dépasse pas l'org (ou l'unique
  utilisateur) de celui qui déclenche le marquage. `tenant` et `platform` en sont
  TOUJOURS exclus — partagés par des orgs entières (ou plusieurs tenants), le hoquet
  d'un seul appelant n'a pas à les peindre en rouge pour tout le monde. `USER` (scope
  LEGACY `("user", sub)` d'avant ADR 0033, seule famille où il survit encore :
  atlassian/folkmcp/google) y est aussi narrow que `MEMBER` — un seul utilisateur —
  et n'atteint jamais `verify.py` (sa cascade ne produit que `MEMBER`/`group`/`org` :
  cf. `access/cascade.py`, qui yield `CascadeRung("user", credentials_store.MEMBER,
  …)` — la chaîne "user" y est un MODE, pas un `entity_type`). L'élargir ici ne
  change donc rien à ce que `verify.py` marque, et permet à atlassian/folk d'utiliser
  la MÊME garde sans en écrire une seconde.
- `record_health(provider, scope, ok, error)` : persiste `meta.health_ko` +
  `meta.health_reason` (merge, best-effort). `scope=None` → no-op. C'est la fonction
  qu'utilise `verify.py`, qui gère elle-même le DÉMARQUAGE (`ok=True` efface
  `health_ko`) — un geste que ce lot (b2) ne touche pas (b3, à venir).
- `mark_rejected(entity_type, entity_id, provider, account, error)` : la façade que
  ce lot ajoute pour un module qui connaît son ENTITÉ directement (pas de `ResolvedCtx`
  ni de scope pré-calculé) — bâtit le scope, applique la MÊME garde, ne démarque
  jamais (toujours `ok=False`) : marquer un rejet réel n'est jamais un fallback qui
  avale l'erreur, l'appelant RE-LÈVE toujours après l'avoir appelée.

Lu par `connectors/readiness.py` (via `access.credential_rejection_for`, qui lit
`credentials_store.credential_health`) — jamais l'inverse, ce module ne connaît pas
ses lecteurs.
"""
from __future__ import annotations

from typing import Optional

from .. import credentials_store

# Paliers dont on accepte de FLAGUER la clé — cf. docstring du module ci-dessus.
FLAGGABLE_SCOPES = (credentials_store.USER, credentials_store.MEMBER,
                    "group", credentials_store.ORG)


def record_health(provider: str, scope: "tuple | None", ok: bool,
                  error: "str | None") -> None:
    """Persiste l'état de santé du credential testé (`meta.health_ko` + raison) — lu
    par `status_for` (fiche) et `access.credential_rejection_for`, donc par le
    verdict `ready` de la carte connecteur. Merge (n'écrase rien), best-effort.
    `scope` = `(entity_type, entity_id, account)` de la ligne RÉELLEMENT
    testée/servie ; `None` (clé partagée au-delà de la garde) → on ne flague pas.

    ⚠️ Seule fonction qui DÉMARQUE (`ok=True` efface `health_ko`/`health_reason`) —
    `mark_rejected` ci-dessous ne l'appelle qu'avec `ok=False`."""
    if scope is None:
        return
    try:
        credentials_store.update_meta(
            scope[0], scope[1], provider, scope[2],
            {"health_ko": (not ok), "health_reason": (error if not ok else None)})
    # noqa: SILENT — dette déclarée : le flag de santé non écrit devrait se journaliser (#424, verdict C)
    except Exception:  # noqa: BLE001 — la santé est un bonus, jamais bloquant
        pass


def mark_rejected(entity_type: Optional[str], entity_id: Optional[str],
                  provider: str, account: str, error: "str | None") -> None:
    """Marque `health_ko` sur `(entity_type, entity_id, provider, account)` — jamais
    sur un scope hors `FLAGGABLE_SCOPES` (tenant/plateforme), jamais si `entity_id`
    est absent. Pour un module qui RECONNAÎT lui-même un grant mort sur la ligne
    qu'il sait être la bonne (jamais par déduction générique) — l'appelant RE-LÈVE
    toujours l'exception d'origine juste après : marquer n'est jamais un fallback
    qui avale l'erreur réelle."""
    if entity_type not in FLAGGABLE_SCOPES or not entity_id:
        return
    record_health(provider, (entity_type, entity_id, account or ""), False, error)
