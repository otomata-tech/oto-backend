"""Sélecteur d'identité connectée (ADR 0024) — surface unifiée « lister / choisir
une identité », backend PAR CONNECTEUR.

Deux modèles de stockage coexistent derrière la même surface (on n'en force pas un
seul — l'unification est au niveau surface, pas stockage) :
- **Google** : N credentials du coffre (`account=email`), défaut = `meta.is_default`.
- **Unipile** : 1 clé → N identités distantes (handles opaques renvoyés par l'API),
  choix per-canal dans `unipile_accounts`. **BYO-only** : sous clé plateforme (revente)
  on garde le hosted-auth qui crée un compte dédié (pas d'exposition cross-client).

Contrat commun `Identity` = `{id, label, status, is_default, channel}` (`channel` None
hors multi-canal — fuite assumée : unipile est par-canal, Google par-service).
"""
from __future__ import annotations


def supports(connector: str) -> bool:
    return connector in _LISTERS


def list_identities(sub: str, connector: str) -> list[dict]:
    """Identités joignables par le credential résolu du `sub` pour `connector`.
    [] si non supporté (ou rien à choisir, ex. clé plateforme unipile)."""
    fn = _LISTERS.get(connector)
    return fn(sub) if fn else []


def select_identity(sub: str, connector: str, identity_id: str) -> dict:
    """Choisit l'identité `identity_id`. Lève `ValueError` si non supporté ou si
    l'id n'existe pas pour ce credential (anti-binding arbitraire)."""
    fn = _SELECTORS.get(connector)
    if not fn:
        raise ValueError(f"Le connecteur `{connector}` ne gère pas le choix de compte.")
    return fn(sub, identity_id)


# --- Google : N credentials du coffre (account=email) -----------------------

def _google_list(sub: str) -> list[dict]:
    from . import google_oauth
    return [{"id": a["google_email"], "label": a["google_email"], "status": "ok",
             "is_default": a["is_default"], "channel": None}
            for a in google_oauth.list_accounts(sub) if a.get("google_email")]


def _google_select(sub: str, identity_id: str) -> dict:
    from . import access, db
    org = access.current_org(sub)
    if org is None or not db.set_default_google_account(sub, org, identity_id):
        raise ValueError(f"Compte Google inconnu : {identity_id}")
    return {"id": identity_id, "is_default": True, "channel": None}


# --- Unipile : 1 clé → N identités distantes (BYO-only) ---------------------

def _unipile_client(sub: str):
    """(client, byo) — résout clé+DSN du credential BYO ; None si non-BYO/absent."""
    from . import access
    if access.credential_mode_for(sub, "unipile") not in access.BYO_MODES:
        return None  # revente (clé plateforme) → hosted-auth, pas de sélecteur
    rc = access.resolve_credential("unipile", want="byo", sub=sub)
    from oto.tools.unipile import UnipileClient
    return UnipileClient(api_key=rc.key, dsn=rc.config.get("dsn"))


def _unipile_list(sub: str) -> list[dict]:
    from . import access, db
    cli = _unipile_client(sub)
    if cli is None:
        return []
    try:
        accounts = cli.list_accounts()
    except Exception:
        return []
    org = access.current_org(sub)
    out = []
    for a in accounts:
        ch = (a.get("type") or "").upper() or None
        chosen = db.get_unipile_account_id(sub, org, ch) if ch else None
        sources = a.get("sources") or []
        out.append({
            "id": a.get("id"),
            "label": a.get("name"),
            "status": (sources[0].get("status") if sources else None) or "ok",
            "is_default": a.get("id") == chosen,
            "channel": ch,
        })
    return out


def _unipile_select(sub: str, identity_id: str) -> dict:
    from . import db
    cli = _unipile_client(sub)
    if cli is None:
        raise ValueError("Choix de compte indisponible (clé plateforme — passe par "
                         "la connexion hébergée).")
    match = next((a for a in cli.list_accounts() if a.get("id") == identity_id), None)
    if match is None:  # anti-binding : l'id DOIT exister sur la clé
        raise ValueError(f"Compte Unipile inconnu sur cette clé : {identity_id}")
    ch = (match.get("type") or "LINKEDIN").upper()
    # Scope membre (ADR 0033 B4) : le binding vaut dans l'org de contexte. BYO →
    # pas un siège plateforme (platform_seat=False), cohérent avec unipile_connect.
    from . import access
    org = access.current_org(sub)
    if org is None:
        raise ValueError("Aucune org de contexte — impossible de rattacher le compte.")
    db.set_unipile_account(sub, identity_id, match.get("name"), org_id=org,
                           provider=ch, platform_seat=False)
    return {"id": identity_id, "channel": ch, "is_default": True}


_LISTERS = {"google": _google_list, "unipile": _unipile_list}
_SELECTORS = {"google": _google_select, "unipile": _unipile_select}
