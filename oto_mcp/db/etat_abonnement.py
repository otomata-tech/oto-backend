"""L'état d'abonnement d'une org, poussé par le commerce (`subscription_states`).

Le commerce facture ; le cœur ne fait que GARDER et SERVIR ce qu'il pousse (fiche d'admin
d'org, bloc `billing`). Deux gestes :

- `poser(org_id, etat)` : remplace l'état de l'org, sauf s'il est plus ancien
  (`emitted_at`) que celui déjà reçu — `EtatPerime` alors, rien n'est écrit. Un envoi au
  même `emitted_at` est rejoué tel quel (idempotent).
- `lire_etat(conn, org_id)` : LA lecture du miroir, pour tout lecteur (fiche d'admin
  d'org, garde d'effacement…). Rend l'état reçu ; `status = 'none'` (aucun abonnement au
  commerce) est un état, pas une absence. Rien de poussé pour l'org : `EtatJamaisPousse`,
  jamais un repli sur une autre source. C'est à l'APPELANT de dire ce que vaut, pour
  lui, cette absence (la fiche d'admin : `billing` `state: missing`, servie entière).
- `preleve_encore(etat)` : l'état dit-il que le commerce prélève encore l'org (actif ou en
  impayé, sans résiliation programmée) ?
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ._conn import _connect

#: Le statut d'une org qui n'a aucun abonnement au commerce.
AUCUN = "none"
STATUTS = (AUCUN, "incomplete", "active", "past_due", "canceled")
CHAMPS = ("status", "block_code", "block_since", "current_period_end", "grace_until",
          "canceled_at", "emitted_at")
_DATES = ("block_since", "current_period_end", "grace_until", "canceled_at", "emitted_at",
          "received_at")


#: Le code qui nomme l'absence d'état poussé (`EtatJamaisPousse.code`) : la fiche d'admin
#: le sert dans son bloc `billing` (`state: missing`), une garde peut le lever.
CODE_JAMAIS_POUSSE = "subscription_state_missing"
#: Les statuts où le commerce tire encore des échéances.
_QUI_PRELEVENT = ("active", "past_due")


class EtatJamaisPousse(LookupError):
    """Le commerce n'a jamais poussé l'état de l'org : on ne sait pas, et on le dit."""

    code = CODE_JAMAIS_POUSSE

    def __init__(self, org_id: int):
        super().__init__(f"aucun état d'abonnement reçu du commerce pour l'org #{org_id} : "
                         "il le pousse à chaque changement, et son remplissage "
                         "(`oto-commerce etat pousser --tous`) le pose pour toutes les orgs")
        self.org_id = org_id


class EtatPerime(Exception):
    """L'envoi est plus ancien que l'état déjà reçu : il ne l'écrase pas."""

    def __init__(self, recu: datetime, envoye: datetime):
        super().__init__(f"état émis le {envoye.isoformat()}, plus ancien que celui déjà "
                         f"reçu (émis le {recu.isoformat()})")
        self.recu, self.envoye = recu, envoye


def _iso(v) -> Optional[str]:
    """ISO 8601 au décalage UTC explicite : la fabrique de lignes rend les dates en texte
    UTC sans fuseau (`_conn._str_dict_row`, session en `TimeZone=UTC`)."""
    if v is None:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()


def _ligne(r) -> dict:
    d = {k: r[k] for k in ("org_id",) + CHAMPS + ("received_at",)}
    for k in _DATES:
        d[k] = _iso(d[k])
    return d


def _lire(conn, org_id: int, *, verrou: bool = False) -> Optional[dict]:
    r = conn.execute(
        "SELECT org_id, " + ", ".join(CHAMPS) + ", received_at FROM subscription_states "
        "WHERE org_id = %s" + (" FOR UPDATE" if verrou else ""), (org_id,)).fetchone()
    return _ligne(r) if r else None


def lire_etat(conn, org_id: int) -> dict:
    """L'état d'abonnement poussé pour `org_id` (`status`, `block_code`, `block_since`,
    `current_period_end`, `grace_until`, `canceled_at`, `emitted_at`, `received_at`).
    Lève `EtatJamaisPousse` si le commerce n'a rien poussé pour l'org."""
    etat = _lire(conn, org_id)
    if etat is None:
        raise EtatJamaisPousse(org_id)
    return etat


def preleve_encore(etat: dict) -> bool:
    """Vrai si le commerce prélève encore l'org : abonnement actif ou en impayé, sans
    résiliation programmée."""
    return etat["status"] in _QUI_PRELEVENT and etat["canceled_at"] is None


def poser(org_id: int, etat: dict) -> tuple[dict, bool]:
    """Pose `etat` (les clés de `CHAMPS`, dates à fuseau) pour `org_id`. Rend l'état relu et
    `changed` (vrai si un champ a bougé). Lève `EtatPerime` sur un envoi plus ancien."""
    for k in _DATES[:-1]:
        if etat.get(k) is not None and etat[k].tzinfo is None:
            raise ValueError(f"`{k}` sans fuseau : l'instant est ambigu")
    with _connect() as conn, conn.transaction():
        avant = _lire(conn, org_id, verrou=True)
        if avant is not None:
            recu = datetime.fromisoformat(avant["emitted_at"])
            if etat["emitted_at"] < recu:
                raise EtatPerime(recu, etat["emitted_at"])
        valeurs = {k: etat.get(k) for k in CHAMPS}
        conn.execute(
            "INSERT INTO subscription_states (org_id, " + ", ".join(CHAMPS) + ") "
            "VALUES (%(org_id)s, " + ", ".join(f"%({k})s" for k in CHAMPS) + ") "
            "ON CONFLICT (org_id) DO UPDATE SET "
            + ", ".join(f"{k} = EXCLUDED.{k}" for k in CHAMPS) + ", received_at = NOW()",
            {"org_id": org_id, **valeurs})
        apres = _lire(conn, org_id)
    change = avant is None or any(avant[k] != apres[k] for k in CHAMPS if k != "emitted_at")
    return apres, change
