"""Sessions Google OAuth multi-compte dans le coffre.

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect
from . import regles_de_comptes
from .users import upsert_user

#: Ce qu'un défaut porte avec lui (#1499, D3) : gardé tel quel quand la ligne se ré-écrit.
_MARQUE_DU_DEFAUT = ("is_default", "default_set_by", "default_set_at")


GOOGLE = "google"   # connecteur Google dans le coffre (account = email)


def _ent(sub: str, org_id: int) -> tuple[str, str]:
    """Entité coffre du scope MEMBRE (ADR 0033 B3) : les comptes Google d'un user
    sont scopés (sub, org) — connectés dans l'org A, ils ne résolvent pas depuis
    l'org B. L'org est TOUJOURS passée par l'appelant (google_oauth résout le
    contexte via `access.current_org` ; la couche db ne le lit jamais)."""
    from .. import credentials_store
    return credentials_store.MEMBER, credentials_store.member_id(org_id, sub)


def _google_row(account: str, cur: dict) -> dict:
    """Reconstruit le dict legacy (contrat google_oauth.py) depuis une ligne coffre
    (cur = {secret, meta, set_at})."""
    m = cur["meta"]
    return {
        "google_email": account or None,
        "refresh_token": cur["secret"],
        "access_token": m.get("access_token"),
        "expires_at": m.get("expires_at"),
        "scopes": m.get("scopes"),
        "is_default": bool(m.get("is_default")),
        "granted_at": m.get("granted_at"),
        "updated_at": cur["set_at"],
        # Le client OAuth qui a émis ce jeton (`None` : avant qu'on le note, donc le
        # nôtre) — un jeton ne se rafraîchit qu'avec lui (`google_oauth.credentials_for`).
        "client_id": m.get("client_id"),
    }


def set_google_oauth(
    sub: str,
    org_id: int,
    google_email: str,
    refresh_token: str,
    scopes: str,
    access_token: Optional[str] = None,
    expires_at: Optional[str] = None,
    make_default: Optional[bool] = None,
    client_id: Optional[str] = None,
) -> None:
    """Upsert un compte Google dans le COFFRE (connector='google', account=email ;
    satellites — access_token/expires_at/scopes/is_default/granted_at/client_id — dans
    meta). `client_id` = le client OAuth ÉMETTEUR du jeton (pas un secret).

    `make_default` None → défaut si 1er compte. is_default conservé si déjà défaut
    (existing OR new). Claime la ligne mono pré-migration (account='').
    """
    upsert_user(sub)
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    account = google_email or ""
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    n_named = sum(1 for a in accts if a["account"])
    prior = next((a for a in accts if a["account"] == account), None)
    if make_default is None:
        make_default = n_named == 0
    granted_at = (prior["meta"].get("granted_at") if prior else None) \
        or datetime.now(timezone.utc).isoformat()
    meta = {"access_token": access_token, "expires_at": expires_at, "scopes": scopes,
            "is_default": False, "granted_at": granted_at, "client_id": client_id}
    if prior and prior["meta"].get("is_default"):   # le défaut reste, avec sa marque
        meta.update({k: prior["meta"][k] for k in _MARQUE_DU_DEFAUT if k in prior["meta"]})
    with _connect() as conn:
        with conn.transaction():
            if account:   # claim l'éventuelle ligne mono pré-migration (account='')
                credentials_store.clear_credential(et, eid, GOOGLE, account="", conn=conn)
            credentials_store.set_credential(
                et, eid, GOOGLE, refresh_token, set_by=sub,
                meta=meta, account=account, conn=conn)
            if make_default:   # un seul défaut, posé par `sub` (#1499, D3)
                regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, account, par=sub)


def get_google_oauth(sub: str, org_id: Optional[int], account: Optional[str] = None) -> Optional[dict]:
    """Renvoie un compte Google du user depuis le COFFRE (déchiffre le
    refresh_token). `account` (email) cible un compte ; None = le défaut
    (meta.is_default), à défaut le plus ancien (granted_at)."""
    if org_id is None:
        return None
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    if account:
        cur = credentials_store.get_credential_with_meta(et, eid, GOOGLE, account=account)
        return _google_row(account, cur) if cur else None
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    if not accts:
        return None
    chosen = next((a for a in accts if a["meta"].get("is_default")), None) \
        or min(accts, key=lambda a: a["meta"].get("granted_at") or "")
    cur = credentials_store.get_credential_with_meta(et, eid, GOOGLE, account=chosen["account"])
    return _google_row(chosen["account"], cur) if cur else None


def list_google_accounts(sub: str, org_id: Optional[int]) -> list[dict]:
    """Liste les comptes Google connectés dans CETTE org (sans les tokens)."""
    if org_id is None:
        return []
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    out = [{
        "google_email": a["account"] or None,
        "is_default": bool(a["meta"].get("is_default")),
        "scopes": a["meta"].get("scopes"),
        "granted_at": a["meta"].get("granted_at"),
        "updated_at": a["set_at"],
        "health_ko": bool(a["meta"].get("health_ko")),
    } for a in accts]
    out.sort(key=lambda r: (not r["is_default"], r["granted_at"] or ""))
    return out


def set_default_google_account(sub: str, org_id: int, account: str) -> bool:
    """Marque `account` comme défaut (meta.is_default, auteur `sub`, date) dans le coffre
    — scope (sub, org). False si le compte n'existe pas dans cette org."""
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    if not any(a["account"] == account for a in accts):
        return False
    with _connect() as conn:
        regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, account, par=sub)
    return True


def delete_google_oauth(sub: str, org_id: int, account: Optional[str] = None) -> None:
    """Supprime un compte (account=email) ou tous (account=None) du coffre — scope
    (sub, org). Si on retire le défaut et qu'il reste des comptes, promeut le plus ancien
    (posé par `sub`, qui retire)."""
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                # Par la primitive du coffre et non par un `DELETE` brut (L6 pièce 2) :
                # sans elle, les instances des comptes déconnectés resteraient vivantes.
                credentials_store.clear_connector_credentials(et, eid, GOOGLE, conn=conn)
                return
            credentials_store.clear_credential(et, eid, GOOGLE, account=account, conn=conn)
            # promotion du défaut : lire le RESTANT dans CETTE transaction (voit le delete)
            rem = conn.execute(
                "SELECT account, meta FROM connector_credentials "
                "WHERE entity_type=%s AND entity_id=%s AND connector=%s", (et, eid, GOOGLE)).fetchall()
            if rem and not any((r["meta"] or {}).get("is_default") for r in rem):
                oldest = min(rem, key=lambda r: (r["meta"] or {}).get("granted_at") or "")["account"]
                regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, oldest, par=sub)


# --- comptes PARTAGÉS : l'org ou l'équipe détient le compte (2026-09-27) --------
#
# Un admin d'org (ou un chef d'équipe) connecte UN compte Google au nom de tous —
# une boîte partagée `hello@…`, un agenda d'équipe. Même ligne de coffre qu'un compte
# membre (connector='google', account=email, satellites dans meta), sous l'entité de
# l'org (`("org", org_id)`) ou de l'équipe (`("group", group_id)`) — le rangement que
# Salesforce emploie déjà pour ses connexions d'org et d'équipe. Les fonctions MEMBRE
# ci-dessus ne bougent pas : ce sont deux chemins, pas un chemin qui devine.

SHARED_SCOPES = ("org", "group")


def _shared_ent(scope: str, target_id: int) -> tuple[str, str]:
    if scope not in SHARED_SCOPES:
        raise ValueError(f"scope partagé invalide : {scope!r} (attendu 'org' ou 'group')")
    return scope, str(int(target_id))


def set_shared_google_oauth(scope: str, target_id: int, *, set_by: str, google_email: str,
                            refresh_token: str, scopes: str,
                            access_token: Optional[str] = None,
                            expires_at: Optional[str] = None,
                            client_id: Optional[str] = None) -> None:
    """Upsert d'un compte Google PARTAGÉ (org ou équipe). Premier compte ⟹ défaut."""
    from .. import credentials_store
    et, eid = _shared_ent(scope, target_id)
    account = google_email or ""
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    prior = next((a for a in accts if a["account"] == account), None)
    make_default = not any(a["account"] for a in accts)
    granted_at = (prior["meta"].get("granted_at") if prior else None) \
        or datetime.now(timezone.utc).isoformat()
    meta = {"access_token": access_token, "expires_at": expires_at, "scopes": scopes,
            "is_default": False, "granted_at": granted_at, "client_id": client_id,
            "connected_by": set_by}
    if prior and prior["meta"].get("is_default"):   # le défaut reste, avec sa marque
        meta.update({k: prior["meta"][k] for k in _MARQUE_DU_DEFAUT if k in prior["meta"]})
    with _connect() as conn:
        with conn.transaction():
            credentials_store.set_credential(
                et, eid, GOOGLE, refresh_token, set_by=set_by,
                meta=meta, account=account, conn=conn)
            if make_default:   # le premier compte, posé par qui le connecte (#1499, D3)
                regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, account, par=set_by)


def get_shared_google_oauth(scope: str, target_id: int,
                            account: Optional[str] = None) -> Optional[dict]:
    """Un compte partagé (déchiffré) : `account` ciblé, sinon le défaut, sinon le plus ancien."""
    from .. import credentials_store
    et, eid = _shared_ent(scope, target_id)
    if account:
        cur = credentials_store.get_credential_with_meta(et, eid, GOOGLE, account=account)
        return _google_row(account, cur) if cur else None
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    if not accts:
        return None
    chosen = next((a for a in accts if a["meta"].get("is_default")), None) \
        or min(accts, key=lambda a: a["meta"].get("granted_at") or "")
    cur = credentials_store.get_credential_with_meta(et, eid, GOOGLE, account=chosen["account"])
    return _google_row(chosen["account"], cur) if cur else None


def list_shared_google_accounts(scope: str, target_id: int) -> list[dict]:
    """Les comptes partagés de cette org/équipe (sans les tokens), défaut d'abord."""
    from .. import credentials_store
    et, eid = _shared_ent(scope, target_id)
    out = [{
        "google_email": a["account"] or None,
        "is_default": bool(a["meta"].get("is_default")),
        "scopes": a["meta"].get("scopes"),
        "granted_at": a["meta"].get("granted_at"),
        "updated_at": a["set_at"],
        "connected_by": a["meta"].get("connected_by"),
        "scope": scope, "target_id": int(target_id),
    } for a in credentials_store.list_accounts(et, eid, GOOGLE)]
    out.sort(key=lambda r: (not r["is_default"], r["granted_at"] or ""))
    return out


def set_default_shared_google_account(scope: str, target_id: int, account: str, *,
                                      par: str) -> bool:
    """Le défaut du palier partagé, posé par `par` (admin d'org, responsable d'équipe :
    `auth/google.scope_target`)."""
    from .. import credentials_store
    et, eid = _shared_ent(scope, target_id)
    if not any(a["account"] == account for a in credentials_store.list_accounts(et, eid, GOOGLE)):
        return False
    with _connect() as conn:
        regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, account, par=par)
    return True


def delete_shared_google_oauth(scope: str, target_id: int, account: Optional[str] = None, *,
                               par: str) -> None:
    """Retire un compte partagé (ou tous) ; promeut le plus ancien restant en défaut,
    posé par `par` (qui retire)."""
    from .. import credentials_store
    et, eid = _shared_ent(scope, target_id)
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                credentials_store.clear_connector_credentials(et, eid, GOOGLE, conn=conn)
                return
            credentials_store.clear_credential(et, eid, GOOGLE, account=account, conn=conn)
            rem = conn.execute(
                "SELECT account, meta FROM connector_credentials "
                "WHERE entity_type=%s AND entity_id=%s AND connector=%s", (et, eid, GOOGLE)).fetchall()
            if rem and not any((r["meta"] or {}).get("is_default") for r in rem):
                oldest = min(rem, key=lambda r: (r["meta"] or {}).get("granted_at") or "")["account"]
                regles_de_comptes.poser_le_defaut(conn, et, eid, GOOGLE, oldest, par=par)


def google_grant_holders(google_email: str) -> list[dict]:
    """Toutes les lignes du coffre qui portent CE compte Google, quelle que soit l'entité
    (membre de n'importe quelle org, équipe, org) : `entity_type`, `entity_id` et le
    client OAuth émetteur (`client_id`, `None` sur une ligne d'avant qu'on le note).

    Sert à une seule question : retirer une de ces lignes peut-il se faire en révoquant
    chez Google ? Lecture du meta seulement — rien n'est déchiffré."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_id, meta->>'client_id' AS client_id "
            "FROM connector_credentials WHERE connector=%s AND account=%s",
            (GOOGLE, google_email)).fetchall()
    return [{"entity_type": r["entity_type"], "entity_id": r["entity_id"],
             "client_id": r["client_id"]} for r in rows]
