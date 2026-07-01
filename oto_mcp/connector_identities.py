"""Sélecteur d'identité connectée (ADR 0024) — surface unifiée « lister / choisir
une identité », backend PAR CONNECTEUR.

Deux modèles de stockage coexistent derrière la même surface (on n'en force pas un
seul — l'unification est au niveau surface, pas stockage) :
- **Google** : N credentials du coffre (`account=email`), défaut = `meta.is_default`.
- **Unipile** : 1 clé → N identités distantes (handles opaques renvoyés par l'API),
  choix per-canal dans `unipile_accounts`. **BYO-only** : sous clé plateforme (revente)
  on garde le hosted-auth qui crée un compte dédié (pas d'exposition cross-client).

Contrat commun `Identity` = `{id, label, status, is_default, channel}` (`channel` None
hors multi-canal — fuite assumée : unipile est par-canal, Google par-service). Champs
additifs pour un compte PARTAGÉ (#55) : `granted=True` + `owner={sub,email,name}`.

**Comptes accordés (otomata-private#55)** : un compte dont le propriétaire a accordé
l'opération au user (`connector_account_grants`) apparaît dans la liste et peut être
sélectionné — la sélection pose le pointeur `unipile_operated_accounts` (elle ne touche
JAMAIS la ligne de connexion `unipile_accounts` du grantee). La validation du select
d'un compte accordé = le grant lui-même (deny-by-default), pas `cli.list_accounts`
(en revente le grantee n'a pas de clé BYO). Résolution à l'appel :
`resolve_operated_account_id` (revalidée contre les grants vivants, backstop dur).
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
    from . import db
    if not db.set_default_google_account(sub, identity_id):
        raise ValueError(f"Compte Google inconnu : {identity_id}")
    return {"id": identity_id, "is_default": True, "channel": None}


# --- Unipile : 1 clé → N identités distantes (BYO-only) ---------------------
# + comptes ACCORDÉS par leur propriétaire (#55, tout mode — y compris revente).

def resolve_operated_account_id(sub: str, provider: str) -> str | None:
    """Compte Unipile opéré par `sub` sur ce canal (LE point de résolution #55).

    Pointeur « identité opérée » posé → REVALIDÉ contre les grants VIVANTS à
    chaque appel (révocation ou déconnexion du owner = effet immédiat, backstop
    dur). Pointeur invalide → `ValueError` EXPLICITE, jamais de repli silencieux
    sur le compte propre : l'agent croirait agir comme le owner et agirait comme
    soi (un message parti sous la mauvaise identité est irréversible).
    Pas de pointeur → compte connecté propre (ou None)."""
    from . import db
    op = db.get_operated_account(sub, provider)
    if op:
        if op["account_id"] in db.granted_accounts_for(sub, provider):
            return op["account_id"]
        raise ValueError(
            f"Le compte {provider.title()} qui t'était accordé n'est plus opérable "
            "(autorisation révoquée ou compte déconnecté par son propriétaire). "
            "Resélectionne ton identité (oto_set_connector_identity ou "
            "https://dashboard.oto.ninja/console/connectors).")
    return db.get_unipile_account_id(sub, provider)


def _unipile_chosen(sub: str, provider: str) -> str | None:
    """Compte effectivement opéré pour l'affichage `is_default` (pointeur valide
    sinon compte propre) — version fail-soft de `resolve_operated_account_id`
    (une liste d'identités ne doit pas lever sur un pointeur orphelin)."""
    from . import db
    op = db.get_operated_account(sub, provider)
    if op and op["account_id"] in db.granted_accounts_for(sub, provider):
        return op["account_id"]
    return db.get_unipile_account_id(sub, provider)


def _unipile_client(sub: str):
    """(client, byo) — résout clé+DSN du credential BYO ; None si non-BYO/absent."""
    from . import access
    if access.credential_mode_for(sub, "unipile") not in access.BYO_MODES:
        return None  # revente (clé plateforme) → hosted-auth, pas de sélecteur
    rc = access.resolve_credential("unipile", want="byo", sub=sub)
    from oto.tools.unipile import UnipileClient
    return UnipileClient(api_key=rc.key, dsn=rc.config.get("dsn"))


def _unipile_list(sub: str) -> list[dict]:
    from . import db
    granted = [g for g in db.list_account_grants_to(sub) if g.get("active")]
    out = []
    cli = _unipile_client(sub)
    if cli is not None:  # BYO : les comptes de la clé (liste existante)
        try:
            accounts = cli.list_accounts()
        except Exception:
            accounts = []
        for a in accounts:
            ch = (a.get("type") or "").upper() or None
            sources = a.get("sources") or []
            out.append({
                "id": a.get("id"),
                "label": a.get("name"),
                "status": (sources[0].get("status") if sources else None) or "ok",
                "is_default": bool(ch) and a.get("id") == _unipile_chosen(sub, ch),
                "channel": ch,
            })
    elif granted:
        # Revente AVEC grants : les comptes PROPRES connectés, pour le retour-à-soi.
        # Sans grant, liste vide comme avant (hosted-auth, rien à choisir).
        for a in db.list_unipile_accounts(sub):
            out.append({
                "id": a["account_id"],
                "label": a.get("account_name") or a["account_id"],
                "status": "ok",
                "is_default": a["account_id"] == _unipile_chosen(sub, a["provider"]),
                "channel": a["provider"],
            })
    # Comptes ACCORDÉS (#55), tout mode. Une clé BYO partagée liste déjà le compte
    # du owner → on ANNOTE l'entrée existante plutôt que de la dupliquer.
    seen = {i["id"]: i for i in out}
    for g in granted:
        owner = {"sub": g["owner_sub"], "email": g.get("owner_email"),
                 "name": g.get("owner_name")}
        existing = seen.get(g["account_id"])
        if existing is not None:
            existing["granted"] = True
            existing["owner"] = owner
            continue
        who = g.get("owner_name") or g.get("owner_email") or g["owner_sub"]
        out.append({
            "id": g["account_id"],
            "label": f"{g.get('account_name') or g['account_id']} — compte de {who}",
            "status": "ok",
            "is_default": g["account_id"] == _unipile_chosen(sub, g["provider"]),
            "channel": g["provider"],
            "granted": True,
            "owner": owner,
        })
    return out


def _unipile_select(sub: str, identity_id: str) -> dict:
    from . import db
    # 1) Compte ACCORDÉ (#55) : pose le POINTEUR « identité opérée » — ne touche
    #    JAMAIS la ligne de connexion `unipile_accounts` du grantee. La validation
    #    = le grant vivant (deny-by-default), pas la clé.
    g = next((r for r in db.list_account_grants_to(sub)
              if r.get("active") and r["account_id"] == identity_id), None)
    if g:
        db.set_operated_account(sub, g["provider"], identity_id, g["owner_sub"])
        return {"id": identity_id, "channel": g["provider"], "is_default": True,
                "granted": True}
    # 2) Retour à SOI (tout mode, y compris revente) : efface le pointeur du canal.
    own = next((a for a in db.list_unipile_accounts(sub)
                if a["account_id"] == identity_id), None)
    if own:
        db.clear_operated_account(sub, own["provider"])
        return {"id": identity_id, "channel": own["provider"], "is_default": True}
    # 3) Chemin BYO existant : choisir un compte de SA clé (bascule la connexion).
    cli = _unipile_client(sub)
    if cli is None:
        raise ValueError("Choix de compte indisponible (clé plateforme — passe par "
                         "la connexion hébergée).")
    match = next((a for a in cli.list_accounts() if a.get("id") == identity_id), None)
    if match is None:  # anti-binding : l'id DOIT exister sur la clé (ou être accordé)
        raise ValueError(f"Compte Unipile inconnu sur cette clé : {identity_id}")
    ch = (match.get("type") or "LINKEDIN").upper()
    # BYO → org porteur None (abonnement propre, pas de plafond org), cohérent
    # avec unipile_connect. Bascule de connexion = retour-à-soi sur ce canal.
    db.set_unipile_account(sub, identity_id, match.get("name"), org_id=None, provider=ch)
    db.clear_operated_account(sub, ch)
    return {"id": identity_id, "channel": ch, "is_default": True}


_LISTERS = {"google": _google_list, "unipile": _unipile_list}
_SELECTORS = {"google": _google_select, "unipile": _unipile_select}
