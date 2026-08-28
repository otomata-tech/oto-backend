"""Les vues MINCES sur la résolution — un contrat par usage.

Un tool keyed veut une clé (`resolve_api_key`), un client multi-secrets veut ses
champs (`resolve_credential_fields`), un mount fédéré veut son token OAuth
(`resolve_mount_token`), le dashboard veut savoir SOUS QUELLE ORIGINE ça
résoudrait sans rien déchiffrer (`credential_mode_for`), et l'endpoint publié
veut savoir si une org peut résoudre seule (`connector_resolvable_for_org`).

Toutes dérivent de `resolve` ou du walker en sonde de présence : aucune ne
recopie la cascade — une divergence ferait MENTIR une surface (vécu 2026-07-07 :
la règle d'option recopiée trois fois, divergée). `option_open` est ici, et pas
dans `quotas`, parce qu'il croise l'entitlement avec le BYO — donc avec le mode
de credential, donc avec la cascade.
"""
from __future__ import annotations

from typing import Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import providers, credentials_store, db, org_store
from . import cascade, quotas, resolve, scope


BYO_MODES = ("user", "group", "org")


# (resolve_remote_credential retiré — ADR 0034 B4 : le connecteur `bridge`
# universel se résout par les champs standard, cf. resolve_credential_fields.)


def option_open(sub: str, connector: str, *, org: "int | None | object" = scope._UNSET,
                group: "int | None | object" = scope._UNSET) -> bool:
    """SOURCE UNIQUE de « l'option (couche 3) du connecteur est-elle levée pour `sub` ? ».
    Le statut carte (`connectors_selection.option_ok`) ET le gate « connecter » d'unipile
    (`status_for.subscribed`) l'appellent → ils ne peuvent plus DIVERGER (le BYO ouvrait
    l'option ici mais pas là → carte « clé d'org » + « Bloqué » incohérente, corrigé
    2026-07-07). Règle : pas d'option requise ⟹ ouvert ; sinon **BYO** (clé propre
    user/groupe/org — l'user gère sa propre instance) OU **has_option** (comp admin /
    abonnement). `org`/`group` explicites = calcul pour un tiers (fiche admin)."""
    opt = quotas.paid_option_for(connector)
    if opt is None:
        return True
    if credential_mode_for(sub, connector, org=org, group=group) in BYO_MODES:
        return True
    return quotas.has_option(sub, opt, org=org)


def resolve_api_key(provider: str, account: Optional[str] = None) -> tuple[str, bool]:
    """Renvoie `(api_key, is_platform)` ou lève McpError actionnable. Vue mince
    sur `resolve_credential` (contrat inchangé pour les ~15 tools keyed ; `account`
    optionnel sélectionne le compte en multi-compte)."""
    rc = resolve.resolve_credential(provider, want="auto", account=account)
    return rc.key, rc.is_platform


def resolve_credential_fields(provider: str, account: Optional[str] = None) -> dict:
    """Résout un credential **multi-champs** byo_user (modèle générique, ADR 0011)
    du sub courant → dict des champs déclarés (`Connector.secret_fields`).

    Pour les connecteurs in-process dont le client s'instancie avec plusieurs
    secrets (ex. Silae : client_id / client_secret / subscription_key, OAuth2
    client-credentials). **byo-only** : pas de clé plateforme ni de quota — le
    credential EST le grant, comme un mount. Vue mince sur `resolve_credential`
    (cascade user > groupe > org, sans palier plateforme ; `account` sélectionne
    le compte en multi-compte)."""
    return resolve.resolve_credential(provider, want="byo", account=account).fields


def resolve_mount_token(provider: str) -> str:
    """Résout le **token OAuth per-user** d'un connecteur fédéré `kind="mount"`
    (otomata#16) depuis le coffre — entité `user` = sub courant.

    Contrairement à un remote (credential d'ORG = token M2M du bridge), un mount
    fédère un MCP distant déjà authentifié par user (ex. atlassian, OAuth Rovo) :
    chaque user porte SON token, résolu par requête et injecté en bearer dans le
    proxy (cf. tools/mount.py). Lève une McpError actionnable si le user n'a pas
    connecté ce service — le proxy traduit ça en « tools non visibles » (le
    ProxyProvider warn+skip), pas en crash de session.
    """
    sub = scope.current_user_sub_or_raise()
    # OAuth fédéré : token avec refresh transparent.
    # Le résolveur connector-spécifique vit hors d'access (refresh = flow OAuth).
    if provider == "atlassian":
        from ..auth import atlassian as atlassian_oauth
        token = atlassian_oauth.access_token_for(sub)
    elif provider == "folkmcp":
        from ..auth import folk as folk_oauth
        token = folk_oauth.access_token_for(sub)
    else:
        # Mount non-oauth (basic_auth, ex. planity) : credential posé via la carte
        # api-keys → scope membre (ADR 0033), comme sa pose.
        org = scope.current_org(sub)
        token = (credentials_store.get_credential(
                     credentials_store.MEMBER,
                     credentials_store.member_id(org, sub), provider)
                 if org is not None else None)
    if token:
        return token
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Connecteur `{provider}` non connecté pour ton compte. "
            f"Connecte-le depuis ton dashboard (manage.oto.cx)."
        ),
    ))


def unipile_api_key_for(sub: str) -> Optional[str]:
    """Clé API Unipile pour `sub`, en cascade (sans lever) : clé de l'user (BYO),
    secret de son org active (abonnement Otomata), puis **clé plateforme** si l'user
    a un grant (mode revente — partage de la clé sans la copier dans chaque org).
    None si aucune.

    Pris pour `sub` EXPLICITE → utilisable hors contexte MCP (route REST connect).
    Les tools MCP, eux, passent par `resolve_api_key("unipile")` (idiome keyed)."""
    active_org = scope.current_org(sub)
    key = db.get_member_api_key(sub, active_org, "unipile")
    if key:
        return key
    # Instance personnelle cross-org (issue #172) : ma clé unipile posée dans une
    # autre org me suit (miroir de resolve_credential — le connect ne doit pas croire
    # « pas de BYO » alors que la résolution trouve ma clé perso ailleurs).
    if providers.is_personal_cross_org("unipile"):
        pio = cascade.personal_instance_org(sub, "unipile", exclude_org=active_org)
        if pio is not None:
            personal_key = db.get_member_api_key(sub, pio, "unipile")
            if personal_key:
                return personal_key
    if active_org is not None:
        org_key = org_store.get_org_secret(active_org, "unipile")
        if org_key:
            return org_key
    # Mode plateforme (ADR 0044 §F R3) : instance PLATFORM utilisable par sub. Gate sur
    # l'éligibilité `platform` du registre (défense en profondeur, comme resolve_api_key).
    con = providers.connector_for_provider("unipile")
    if con and "platform" in con.auth_modes:
        grant = cascade._resolve_platform_grant(sub, "unipile", active_org)
        if grant:
            return grant["secret"]
    return None


def credential_mode_for(sub: str, provider: str, *,
                        org: "int | None | object" = scope._UNSET,
                        group: "int | None | object" = scope._UNSET,
                        probe: "Optional[CascadeProbe]" = None) -> str:
    """Origine de la clé `provider` pour `sub` (EXPLICITE, hors contexte MCP) :
    `user|group|org|platform|over_quota|forbidden`. PRÉSENCE seulement (pas de
    déchiffrement → sûr/léger pour un statut). **Miroir** de la cascade
    `resolve_credential` (incl. fallback grant org) — une divergence ferait mentir
    l'UI. « BYO » (clé propre, pas la plateforme) = mode ∈ {user, group, org}.
    `org`/`group` explicites (≠ _UNSET) = calcul pour un TIERS contre son propre
    contexte (fiche admin), sans current_org/current_group (anti-fuite du requérant).

    `probe` = sonde de présence ALTERNATIVE (défaut : `PRESENCE_PROBE`). Un appelant qui
    interroge BEAUCOUP de connecteurs d'affilée passe une sonde **préchargée**
    (`preloaded_presence_probe`) : mêmes réponses, en quelques lectures au lieu d'une
    marche par connecteur. Le paramètre existe pour que ce cas passe PAR cette fonction
    — le contrôle de quota du barreau plateforme, juste en dessous, ne se recopie pas
    chez l'appelant, et un appelant pressé n'a pas de raison de contourner le seam.

    ⚠️ Passer `org`/`group` explicitement N'EST PAS qu'un raccourci pour un tiers : c'est
    aussi ce qui évite de re-résoudre le contexte à CHAQUE appel. Mesuré sur 33
    connecteurs d'un compte réel — `current_org` y pesait 73 % du temps total, appelé
    trente-trois fois pour rendre trente-trois fois la même valeur."""
    o = scope.current_org(sub) if org is scope._UNSET else org
    g = scope.current_group(sub) if group is scope._UNSET else group
    # Marche unique (walker) en sonde PRÉSENCE — plus de cascade recopiée ici :
    # le miroir est structurel, il ne peut plus diverger de la résolution.
    win = cascade.cascade_winner(sub, provider, org=o, group=g,
                         probe=probe or cascade.PRESENCE_PROBE, want="auto")
    if win is None:
        return "forbidden"
    if win.mode != "platform":
        return win.mode
    grant = win.payload
    used = quotas.usage_today(sub, provider)
    limit = grant.get("daily_quota") or quotas.quota_for(provider)
    return "over_quota" if (limit and used >= limit) else "platform"


def connector_resolvable_for_org(provider: str, org_id: int) -> bool:
    """Un connecteur peut-il être résolu pour une ORG **sans user identifié** ?
    Vrai si : credential-less (`secret_kind='none'`), OU secret d'org configuré, OU
    clé plateforme accordée à l'org. Sonde pour publier un endpoint MCP **anonyme**
    (ADR 0032) servi par la clé de l'org propriétaire du projet : un endpoint sans
    login n'a pas de `user_key`/session per-user → oauth/cookie sont exclus de fait
    (pas de secret d'org pour eux). Miroir org-only de la cascade `resolve_credential`."""
    con = providers.connector_for_provider(provider)
    if con is None:
        return False
    if con.secret_kind == "none":
        return True
    # Walker en présence, sub=None → cascade réduite org > plateforme (ADR 0044
    # §F R3 : instance 'open' free-tier, ou 'closed' visant `org:<org_id>`).
    return cascade.cascade_winner(None, provider, org=org_id, group=None,
                          probe=cascade.PRESENCE_PROBE) is not None
