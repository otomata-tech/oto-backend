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
from .users import upsert_user


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
) -> None:
    """Upsert un compte Google dans le COFFRE (connector='google', account=email ;
    satellites — access_token/expires_at/scopes/is_default/granted_at — dans meta).

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
    is_default = bool(prior and prior["meta"].get("is_default")) or make_default
    granted_at = (prior["meta"].get("granted_at") if prior else None) \
        or datetime.now(timezone.utc).isoformat()
    meta = {"access_token": access_token, "expires_at": expires_at, "scopes": scopes,
            "is_default": is_default, "granted_at": granted_at}
    with _connect() as conn:
        with conn.transaction():
            if account:   # claim l'éventuelle ligne mono pré-migration (account='')
                credentials_store.clear_credential(et, eid, GOOGLE, account="", conn=conn)
            if make_default:   # un seul défaut : retire le flag aux autres comptes
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'false') "
                    "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account<>%s",
                    (et, eid, GOOGLE, account),
                )
            credentials_store.set_credential(
                et, eid, GOOGLE, refresh_token, set_by=sub,
                meta=meta, account=account, conn=conn)


def update_google_access_token(
    sub: str, org_id: int, google_email: Optional[str], access_token: str, expires_at: str
) -> None:
    """Met à jour SEULEMENT l'access_token + expiry (sur refresh) — merge meta dans
    le coffre, SANS re-chiffrer le refresh_token. `google_email` None = compte mono
    (account='')."""
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    account = google_email or ""
    credentials_store.update_meta(
        et, eid, GOOGLE, account,
        {"access_token": access_token, "expires_at": expires_at})


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
    } for a in accts]
    out.sort(key=lambda r: (not r["is_default"], r["granted_at"] or ""))
    return out


def set_default_google_account(sub: str, org_id: int, account: str) -> bool:
    """Marque `account` comme défaut (meta.is_default) dans le coffre — scope
    (sub, org). False si le compte n'existe pas dans cette org."""
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    accts = credentials_store.list_accounts(et, eid, GOOGLE)
    if not any(a["account"] == account for a in accts):
        return False
    with _connect() as conn:
        conn.execute(
            "UPDATE connector_credentials "
            "SET meta = jsonb_set(meta, '{is_default}', to_jsonb(account = %s)) "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s",
            (account, et, eid, GOOGLE),
        )
    return True


def delete_google_oauth(sub: str, org_id: int, account: Optional[str] = None) -> None:
    """Supprime un compte (account=email) ou tous (account=None) du coffre — scope
    (sub, org). Si on retire le défaut et qu'il reste des comptes, promeut le plus ancien."""
    from .. import credentials_store
    et, eid = _ent(sub, org_id)
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                conn.execute(
                    "DELETE FROM connector_credentials "
                    "WHERE entity_type=%s AND entity_id=%s AND connector=%s", (et, eid, GOOGLE))
                return
            credentials_store.clear_credential(et, eid, GOOGLE, account=account, conn=conn)
            # promotion du défaut : lire le RESTANT dans CETTE transaction (voit le delete)
            rem = conn.execute(
                "SELECT account, meta FROM connector_credentials "
                "WHERE entity_type=%s AND entity_id=%s AND connector=%s", (et, eid, GOOGLE)).fetchall()
            if rem and not any((r["meta"] or {}).get("is_default") for r in rem):
                oldest = min(rem, key=lambda r: (r["meta"] or {}).get("granted_at") or "")["account"]
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'true') "
                    "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
                    (et, eid, GOOGLE, oldest))
