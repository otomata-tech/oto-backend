"""Sélecteur d'identité connectée (ADR 0024) — surface unifiée « lister / choisir
une identité », backend PAR CONNECTEUR.

Trois modèles de stockage coexistent derrière la même surface (on n'en force pas un
seul — l'unification est au niveau surface, pas stockage) :
- **Google** : N credentials du coffre (`account=email`), défaut = `meta.is_default`.
- **Unipile** : 1 clé → N identités distantes (handles opaques renvoyés par l'API),
  choix per-canal dans `unipile_accounts`. **BYO-only** : sous clé plateforme (revente)
  on garde le hosted-auth qui crée un compte dédié (pas d'exposition cross-client).
- **Backend déclaré par le connecteur** (`register()`, patron `browser_session`) :
  la logique d'énumération vit dans SON module `tools/<name>.py` (ex. `pennylaneged` :
  les sociétés du cabinet = les GED cibles), le défaut dans le `meta` du credential.

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

⚠️ Un backend enregistré peut être **async** (ex. exécution Browserbase) :
`list_identities`/`select_identity` renvoient alors un awaitable — les capacités
(`capabilities/connectors/identities.py`) awaitent le résultat le cas échéant.
"""
from __future__ import annotations


def supports(connector: str) -> bool:
    return connector in _LISTERS


def register(connector: str, lister, selector) -> None:
    """Déclare le backend d'identités d'un connecteur (appelé à l'import de son
    module `tools/*`, comme `browser_session.register`). `lister(sub)` →
    list[Identity] ; `selector(sub, identity_id)` → Identity (ValueError si l'id
    n'est pas joignable par le credential — anti-binding). Sync ou async."""
    _LISTERS[connector] = lister
    _SELECTORS[connector] = selector


SCOPES = ("member", "org", "group")


def list_identities(sub: str, connector: str, scope: str = "member"):
    """Identités joignables par le credential résolu du `sub` pour `connector`.
    [] si non supporté (ou rien à choisir, ex. clé plateforme unipile). Peut
    renvoyer un awaitable (backend async enregistré via `register`).
    `scope` (Phase 2) : `org`/`group` listent les comptes nommés du palier partagé
    — backend keyed générique seulement ; les backends spécifiques (google,
    unipile) sont par-membre, un autre scope y répond []."""
    fn = _LISTERS.get(connector)
    if not fn:
        return []
    if scope != "member":
        return fn(sub, scope) if connector in _KEYED else []
    return fn(sub)


def select_identity(sub: str, connector: str, identity_id: str, scope: str = "member"):
    """Choisit l'identité `identity_id`. Lève `ValueError` si non supporté ou si
    l'id n'existe pas pour ce credential (anti-binding arbitraire). Peut renvoyer
    un awaitable (backend async enregistré via `register`). `scope` : cf.
    `list_identities` ; le contrôle d'accès du palier partagé (admin d'org /
    d'équipe) vit dans la capacité, pas ici."""
    fn = _SELECTORS.get(connector)
    if not fn:
        raise ValueError(f"Le connecteur `{connector}` ne gère pas le choix de compte.")
    if scope != "member":
        if connector not in _KEYED:
            raise ValueError(f"Le connecteur `{connector}` n'a de comptes qu'au palier membre.")
        return fn(sub, identity_id, scope)
    return fn(sub, identity_id)


# --- Google : N credentials du coffre (account=email) -----------------------

def _google_list(sub: str) -> list[dict]:
    from ..auth import google as google_oauth
    return [{"id": a["google_email"], "label": a["google_email"], "status": "ok",
             "is_default": a["is_default"], "channel": None}
            for a in google_oauth.list_accounts(sub) if a.get("google_email")]


def _google_select(sub: str, identity_id: str) -> dict:
    from .. import access, db
    org = access.current_org(sub)
    if org is None or not db.set_default_google_account(sub, org, identity_id):
        raise ValueError(f"Compte Google inconnu : {identity_id}")
    return {"id": identity_id, "is_default": True, "channel": None}


# --- Unipile : 1 clé → N identités distantes (BYO-only) ---------------------
# + comptes ACCORDÉS par leur propriétaire (#55, tout mode — y compris revente).

def _own_unipile_account_id(sub: str, provider: str) -> str | None:
    """Compte Unipile connecté PROPRE de `sub` sur ce canal — le binding VIVANT de
    l'org de contexte (le binding est un ACTE par org, modèle explicite : un siège
    plateforme connecté ailleurs se propose à l'ADOPTION au connect, jamais en
    fallback silencieux ici — l'ex-#221 auto a été retiré, il rendait le disconnect
    incohérent). Seul cross-org restant : **BYO** (#172) — la clé membre me suit dans
    une autre org → compte pris dans la MÊME org que la clé (`personal_instance_org`),
    clé et compte appariés. None si aucun."""
    from .. import access, providers, db
    org = access.current_org(sub)
    acc = db.get_unipile_account_id(sub, org, provider)
    if acc:
        return acc
    if providers.is_personal_cross_org("unipile"):
        pio = access.personal_instance_org(sub, "unipile", exclude_org=org)
        if pio is not None:
            return db.get_unipile_account_id(sub, pio, provider)
    return None


def _own_account_ids(sub: str, provider: str) -> set[str]:
    """Tous les `account_id` VIVANTS propres au sub sur ce canal (toutes orgs) — set
    de garde du pin `_account=` (le sien, en plus des comptes accordés)."""
    from .. import db
    p = provider.upper()
    return {a["account_id"] for a in db.list_unipile_accounts(sub)
            if (a.get("provider") or "").upper() == p}


def resolve_operated_account_id(sub: str, provider: str) -> str | None:
    """Compte Unipile opéré par `sub` sur ce canal (LE point de résolution #55/0051).

    **Pin d'appel `_account=` (ADR 0051)** : identité opérée épinglée POUR
    CET APPEL — prime sur le pointeur maison, ÉPHÉMÈRE (aucun état écrit). Gardé :
    compte ACCORDÉ (#55 vivant) OU compte PROPRE du sub ; un pin non opérable LÈVE
    (jamais de repli muet sur une autre identité).

    Sinon, pointeur « identité maison » posé → REVALIDÉ contre les grants VIVANTS à
    chaque appel (révocation ou déconnexion du owner = effet immédiat, backstop
    dur). Pointeur invalide → `ValueError` EXPLICITE, jamais de repli silencieux
    sur le compte propre : l'agent croirait agir comme le owner et agirait comme
    soi (un message parti sous la mauvaise identité est irréversible).
    Pas de pin ni de pointeur → compte connecté propre (org de contexte OU instance
    perso cross-org, #172)."""
    from .. import db, session_org
    pin = session_org.current_call_account()
    if pin:
        if pin in db.granted_accounts_for(sub, provider) or pin in _own_account_ids(sub, provider):
            return pin
        raise ValueError(
            f"Le compte {provider.title()} épinglé (`_account=`) n'est ni le "
            "tien ni un compte qui t'est accordé — ou il n'est plus opérable. Liste les "
            "identités opérables avec oto_identity(op='list').")
    op = db.get_operated_account(sub, provider)
    if op:
        if op["account_id"] in db.granted_accounts_for(sub, provider):
            return op["account_id"]
        raise ValueError(
            f"Le compte {provider.title()} qui t'était accordé n'est plus opérable "
            "(autorisation révoquée ou compte déconnecté par son propriétaire). "
            "Resélectionne ton identité (oto_identity(op='set') ou "
            "https://manage.oto.cx/console/connectors).")
    return _own_unipile_account_id(sub, provider)


def _unipile_chosen(sub: str, provider: str) -> str | None:
    """Compte effectivement opéré pour l'affichage `is_default` (pointeur valide
    sinon compte propre) — version fail-soft de `resolve_operated_account_id`
    (une liste d'identités ne doit pas lever sur un pointeur orphelin)."""
    from .. import db
    op = db.get_operated_account(sub, provider)
    if op and op["account_id"] in db.granted_accounts_for(sub, provider):
        return op["account_id"]
    return _own_unipile_account_id(sub, provider)


def _unipile_client(sub: str):
    """(client, byo) — résout clé+DSN du credential BYO ; None si non-BYO/absent."""
    from .. import access
    if access.credential_mode_for(sub, "unipile") not in access.BYO_MODES:
        return None  # revente (clé plateforme) → hosted-auth, pas de sélecteur
    rc = access.resolve_credential("unipile", want="byo", sub=sub)
    from oto.tools.unipile import make_unipile_client
    # dsn apparié à la clé (défaut api.unipile.com côté oto-core) — une clé qui vit
    # sur un tenant distinct porte son dsn dans la config du credential.
    return make_unipile_client(api_key=rc.key, dsn=rc.config.get("dsn"))


def _unipile_live_status_map(sub: str) -> dict:
    """Statut LIVE des comptes hébergés, lu sur la clé PLATEFORME Unipile :
    `{account_id: status}`.

    Le mode revente / hosted-auth persiste les comptes en DB et n'interroge PAS
    Unipile → un compte réellement mort (checkpoint, credentials expirés, révoqué
    par l'utilisateur) affichait « ok » à tort (#201). Le vrai statut n'est lisible
    qu'en listant les comptes de l'abonnement (`list_accounts().sources[].status`).

    ⚠️ Mais ce `sources[].status` de compte peut LUI AUSSI rester « OK » alors que
    la SESSION est morte (checkpoint / cookie li_at tourné) → un vrai appel se prend
    un 401 mais la carte disait « connecté » (#236). On confirme donc la liveness
    par une sonde `account_alive` (GET users/me → 401 = mort) et on rétrograde en
    'disconnected'. Chemin PICKER d'identités SEUL (hors boucle /api/me chaude —
    budget assumé, un appel users/me par compte hébergé, au clic sur le sélecteur).
    Fail-soft : `{}` si indisponible (l'appelant retombe sur « ok », comportement
    d'avant) ; sonde best-effort PAR compte (un incident garde le status de compte)."""
    from .. import access
    try:
        rc = access.resolve_credential("unipile", want="auto", sub=sub)
        from oto.tools.unipile import make_unipile_client
        cli = make_unipile_client(api_key=rc.key, dsn=rc.config.get("dsn"))
        out: dict = {}
        for a in cli.list_accounts():
            aid = a.get("id")
            if not aid:
                continue
            status = (a.get("sources") or [{}])[0].get("status")
            try:  # sonde de vraie liveness (#236) : users/me 401 = session morte
                if not cli.account_alive(aid):
                    status = "disconnected"
            # noqa: SILENT — best-effort : garde le statut de compte sur incident de sonde
            except Exception:
                pass  # best-effort : garde le status de compte sur incident sonde
            out[aid] = status
        return out
    # noqa: SILENT — sonde de statut live indisponible ⇒ statut stocké conservé
    except Exception:
        return {}


def _unipile_list(sub: str, canal: str | None = None) -> list[dict]:
    """Identités hébergées joignables par `sub`. `canal` (LINKEDIN/WHATSAPP/…) =
    ne rendre que celles de CE canal — ce que voit la carte d'un connecteur de canal
    depuis le split du 2026-08-28. None = tous les canaux (chemin d'appel qui ne
    connaît pas de canal)."""
    from .. import db
    granted = [g for g in db.list_account_grants_to(sub) if g.get("active")]
    out = []
    cli = _unipile_client(sub)
    # Statut live des comptes hébergés (clé plateforme), résolu au plus une fois et
    # seulement si un compte non-BYO le requiert (#201). Fail-soft → "ok".
    _live: dict = {}
    def _live_status(account_id: str) -> str:
        if "map" not in _live:
            _live["map"] = _unipile_live_status_map(sub)
        return _live["map"].get(account_id) or "ok"
    if cli is not None:  # BYO : les comptes de la clé (liste existante)
        try:
            accounts = cli.list_accounts()
        # noqa: SILENT — sonde de statut live indisponible ⇒ statut stocké conservé
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
    else:
        # Revente (clé plateforme / hosted-auth) : les comptes PROPRES connectés
        # DANS L'ORG DE CONTEXTE. Toujours listés — même sans grant et sans
        # « choix » à faire, un compte connecté DOIT apparaître (feedback #132 :
        # `identities: []` alors qu'un LinkedIn hébergé était connecté = faux
        # négatif, l'agent concluait à tort « aucun compte » et renvoyait
        # l'utilisateur au dashboard). Filtre org = scope membre ADR 0033 B4,
        # aligné sur `status_for` et la résolution d'appel (`get_unipile_account_id`) :
        # un compte d'une AUTRE org n'est pas opérable ici → le lister serait un
        # faux positif (bouton « Use this account » inerte, vécu 2026-07-08).
        accounts = db.list_unipile_accounts(sub)
        if accounts:  # org résolue seulement s'il y a quelque chose à filtrer
            from .. import access
            org = access.current_org(sub)
            accounts = [a for a in accounts if a.get("org_id") == org]
        for a in accounts:
            out.append({
                "id": a["account_id"],
                "label": a.get("account_name") or a["account_id"],
                "status": _live_status(a["account_id"]),
                "is_default": a["account_id"] == _unipile_chosen(sub, a["provider"]),
                "channel": a["provider"],
            })
    # Comptes ACCORDÉS (#55), tout mode. Une clé BYO partagée liste déjà le compte
    # du owner → on ANNOTE l'entrée existante plutôt que de la dupliquer.
    seen = {i["id"]: i for i in out}
    for g in granted:
        owner = {"sub": g["owner_sub"], "email": g.get("owner_email"),
                 "name": g.get("owner_name"),
                 "org": g.get("owner_org_id"), "org_name": g.get("owner_org_name")}
        existing = seen.get(g["account_id"])
        if existing is not None:
            existing["granted"] = True
            existing["owner"] = owner
            continue
        who = g.get("owner_name") or g.get("owner_email") or g["owner_sub"]
        out.append({
            "id": g["account_id"],
            "label": f"{g.get('account_name') or g['account_id']} — compte de {who}",
            "status": _live_status(g["account_id"]),
            "is_default": g["account_id"] == _unipile_chosen(sub, g["provider"]),
            "channel": g["provider"],
            "granted": True,
            "owner": owner,
        })
    if canal:
        # Filtre APRÈS l'annotation des comptes accordés : un compte accordé du bon
        # canal doit rester listé (c'est la seule identité que certains grantees
        # ont). Un compte dont le canal est INCONNU (`channel=None` — vu en BYO quand
        # Unipile ne renvoie pas `type`) ne se rattache à aucune carte de canal : le
        # taire ici vaut mieux que de le faire apparaître sous les six.
        _c = canal.upper()
        out = [i for i in out if (i.get("channel") or "").upper() == _c]
    return out


def _unipile_select(sub: str, identity_id: str, canal: str | None = None) -> dict:
    """Choisit l'identité opérée. `canal` (carte d'un connecteur de canal) = garde :
    on ne bascule pas son identité Telegram depuis la carte WhatsApp. Le canal RÉEL
    est celui du COMPTE, jamais celui qu'on suppose — d'où une garde sur le résultat
    plutôt qu'un filtre sur l'entrée : les trois chemins de sélection (compte accordé,
    retour à soi, bascule BYO) le découvrent chacun à leur façon, et un seul endroit
    doit trancher."""
    from .. import db

    def _exige_canal(trouve: str | None) -> None:
        """Refuse AVANT d'écrire si le compte n'est pas du canal de la carte.

        Après l'écriture il serait trop tard : poser le pointeur puis lever
        laisserait l'identité opérée changée par un appel qui a rendu une erreur."""
        if canal and (trouve or "").upper() != canal.upper():
            raise ValueError(
                f"Ce compte est un compte {(trouve or 'inconnu').title()} : il ne se "
                f"choisit pas depuis la carte {canal.title()}. Passe par la carte de "
                "son canal.")
    # 1) Compte ACCORDÉ (#55) : pose le POINTEUR « identité opérée » — ne touche
    #    JAMAIS la ligne de connexion `unipile_accounts` du grantee. La validation
    #    = le grant vivant (deny-by-default), pas la clé.
    g = next((r for r in db.list_account_grants_to(sub)
              if r.get("active") and r["account_id"] == identity_id), None)
    if g:
        _exige_canal(g["provider"])
        db.set_operated_account(sub, g["provider"], identity_id, g["owner_sub"])
        return {"id": identity_id, "channel": g["provider"], "is_default": True,
                "granted": True}
    # 2) Retour à SOI (tout mode, y compris revente) : efface le pointeur du canal.
    own = next((a for a in db.list_unipile_accounts(sub)
                if a["account_id"] == identity_id), None)
    if own:
        _exige_canal(own["provider"])
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
    _exige_canal(ch)
    # Scope membre (ADR 0033 B4) : le binding vaut dans l'org de contexte. BYO →
    # pas un siège plateforme (platform_seat=False), cohérent avec unipile_connect.
    # Bascule de connexion = retour-à-soi sur ce canal → efface le pointeur opéré (#55).
    from .. import access
    org = access.current_org(sub)
    if org is None:
        raise ValueError("Aucune org de contexte — impossible de rattacher le compte.")
    db.set_unipile_account(sub, identity_id, match.get("name"), org_id=org,
                           provider=ch, platform_seat=False)
    db.clear_operated_account(sub, ch)
    return {"id": identity_id, "channel": ch, "is_default": True}


# --- Backend keyed GÉNÉRIQUE : N credentials du coffre (account=label libre) ---
# Pour tout connecteur multi-compte (`Connector.auth_multi_account`) SANS backend
# spécifique (google en a un) : les comptes = les lignes du coffre au scope MEMBRE de
# l'org de contexte, le défaut = `meta.is_default`. Ex. « 2 Zoho » (self-clients FR/US).

def keyed_entity(sub: str, scope: str) -> "tuple[str, str] | None":
    """L'entité du coffre visée par un `scope` (member | org | group) pour `sub`, ou
    None sans org/équipe de contexte. Phase 2 (2026-08-25) : les comptes nommés
    existent aussi aux paliers partagés."""
    from .. import access, credentials_store
    org = access.current_org(sub)
    if org is None:
        return None
    if scope == "org":
        return ("org", str(org))
    if scope == "group":
        gid = access.current_group(sub)
        return None if gid is None else ("group", str(gid))
    return (credentials_store.MEMBER, credentials_store.member_id(org, sub))


def _keyed_list(sub: str, connector: str, scope: str = "member") -> list[dict]:
    from .. import credentials_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        return []
    out = []
    for row in credentials_store.list_accounts(ent[0], ent[1], connector):
        acct = row["account"]
        meta = row.get("meta") or {}
        out.append({
            "id": acct,
            "label": meta.get("label") or acct or "(défaut)",
            "status": "ok",
            "is_default": bool(meta.get("is_default")),
            "channel": None,
        })
    return out


def _keyed_select(sub: str, connector: str, identity_id: str, scope: str = "member") -> dict:
    from .. import credentials_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        raise ValueError("Aucune org/équipe de contexte — impossible de choisir un compte.")
    accounts = [r["account"] for r in credentials_store.list_accounts(ent[0], ent[1], connector)]
    if identity_id not in accounts:
        raise ValueError(f"Compte `{identity_id}` inconnu pour {connector}.")
    # Défaut UNIQUE : pose is_default sur la ligne choisie, le retire des autres.
    for acct in accounts:
        credentials_store.update_meta(ent[0], ent[1], connector, acct,
                                      {"is_default": acct == identity_id})
    return {"id": identity_id, "label": identity_id, "is_default": True, "channel": None}


_LISTERS = {"google": _google_list, "unipile": _unipile_list}
_SELECTORS = {"google": _google_select, "unipile": _unipile_select}
# Connecteurs servis par le backend keyed GÉNÉRIQUE (seul à connaître les paliers
# partagés) — rempli par `_register_keyed_multi_account`.
_KEYED: set[str] = set()


def _register_keyed_multi_account() -> None:
    """Enregistre le backend keyed générique pour tout connecteur multi-compte
    (`Connector.auth_multi_account` — depuis 2026-08-25, toute clé d'API l'est par
    défaut, plus seulement la liste curée `MULTI_ACCOUNT_PROVIDERS`) qui n'a pas
    déjà un backend spécifique (google, unipile). Closures liant le nom du
    connecteur (défaut d'arg = capture par valeur)."""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        name = con.name
        if not con.auth_multi_account or name in _LISTERS:
            continue
        _KEYED.add(name)
        _LISTERS[name] = lambda sub, scope="member", c=name: _keyed_list(sub, c, scope)
        _SELECTORS[name] = lambda sub, iid, scope="member", c=name: _keyed_select(sub, c, iid, scope)


def _register_hosted_channels() -> None:
    """Un backend d'identités par CANAL hébergé (split du 2026-08-28).

    `oto_identity(connector='whatsapp')` doit rendre les comptes WhatsApp, pas les
    six canaux : depuis que chaque canal a sa carte, une liste non filtrée y ferait
    apparaître un LinkedIn qu'aucun bouton de cette carte ne peut opérer. Même corps
    (`_unipile_list`/`_unipile_select`) avec un canal en plus — la résolution, les
    grants et le statut live restent UN seul chemin.

    (Ces connecteurs ne passent pas par le backend keyed générique : `hosted` ⟹
    `auth_multi_account` faux — leurs comptes ne sont pas des lignes du coffre.)"""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        if not con.hosted_channel:
            continue
        # `_ch` capturé par valeur : sans le défaut d'argument, les six backends
        # fermeraient sur la même variable de boucle (donc sur le dernier canal).
        _LISTERS[con.name] = (
            lambda sub, scope="member", _ch=con.hosted_channel: _unipile_list(sub, _ch))
        _SELECTORS[con.name] = (
            lambda sub, iid, scope="member", _ch=con.hosted_channel:
            _unipile_select(sub, iid, _ch))


_register_keyed_multi_account()
_register_hosted_channels()
