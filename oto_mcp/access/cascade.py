"""Le WALKER unique de la cascade de credentials (ADR 0024/0044).

La cascade `perso > cross-org > équipe active > org > plateforme` était écrite à
la main à six endroits ; elle vit ici, et nulle part ailleurs. Le walker est
paramétré par une SONDE (`CascadeProbe`) : présence (aucun déchiffrement),
fetch (ne déchiffre que le gagnant), ou préchargée (les mêmes réponses en
quelques lectures). Ajouter un barreau, c'est éditer `walk_cascade` — jamais un
appelant.

Ce module ne dépend que de `scope` (appartenance à un scope de partage, pour le
palier plateforme). Il ne connaît ni les quotas, ni le RBAC, ni la résolution
réelle : ce sont eux qui l'appellent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import (providers, credentials_store, db, grants_chain, group_store, org_store,
                tenant_vault)
from ..connectors import cardinality
from . import scope, secret_repr

# DÉRIVÉ du registre source unique (package `providers/`) : providers dont le
# secret peut être POSSÉDÉ par une org et partagé (auth_mode byo_org) — exclut
# slack (xoxp = identité perso) et les sessions per-user (linkedin/google/
# whatsapp/crunchbase). Gate les barreaux groupe/org du walker.
ORG_SHAREABLE_PROVIDERS = providers.ORG_SHAREABLE_PROVIDERS

# Liste FERMÉE (#876) — PAS `connectors.link.entries()` (google, déjà migré, y est aussi).
LEGACY_USER_SCOPE_PROVIDERS = ("atlassian", "folkmcp")


def _is_multi_account(provider: str, org: "int | None" = None) -> bool:
    """Le connecteur porte-t-il plusieurs comptes dans le coffre (segment `account`) ?
    Gate le chemin de sélection de compte ; un connecteur mono-compte garde la
    résolution historique (account='').

    ⚠️ **Passe par `connectors.cardinality`, jamais par la propriété du registre**, et
    ce n'est pas une indirection de style : une org peut avoir SURCHARGÉ la cardinalité
    en base (L6 pièce 2 c2), et la surcharge doit être lue ici comme elle l'est par la
    garde d'écriture. Lue d'un seul côté, elle accepterait un deuxième compte que
    personne n'irait jamais lire — le défaut exact d'oto-backend#409. Zéro requête :
    les surcharges vivent en mémoire, chargées au boot et rechargées à la main.

    `org` = l'org de CONTEXTE du requérant. None ⟹ seule la surcharge plateforme joue."""
    return cardinality.is_multi_account(provider, org)


def account_noun(provider: str) -> str:
    """Le MOT du fournisseur pour un compte de ce connecteur — « workspace » chez Slack,
    « organisation » chez Zoho, « site » pour le navigateur connecté, « compte » par
    défaut (`Connector.account_noun`). Sert les messages que l'AGENT lit au moment où il
    est bloqué : « plusieurs comptes slack » l'oblige à traduire, « plusieurs
    workspaces » lui dit ce qu'il cherche. Jamais vide."""
    con = providers.connector_for_provider(provider)
    return (getattr(con, "account_noun", "") or "compte") if con else "compte"


def _shared_auto_account(entity_type: str, entity_id: str, provider: str,
                         where: str, scope: Optional[str] = None) -> str:
    """Compte AUTOMATIQUE d'un palier multi-compte, quand l'appelant n'en a nommé
    aucun : compte unique auto > défaut posé (`oto_identity(op='set')`,
    meta.is_default) > '' si aucun compte > McpError d'ambiguïté. Module-level parce
    que partagé entre la résolution réelle (`_pick_account`) et la résolution ANONYME
    (`_resolve_credential_anon`) — l'endpoint `<slug>.mcp.oto.cx` doit sélectionner
    le compte d'org comme le barreau org du chemin réel, jamais lire `''` en dur
    (`ensure_named_coexistence` migre la ligne `''` vers « principal » au premier
    compte nommé — review #399 F3). `scope` non-None ⇒ les refs `oto_identity` du
    message d'ambiguïté portent le scope du palier (org/group)."""
    accts = credentials_store.list_accounts(entity_type, entity_id, provider)
    if len(accts) == 1:
        return accts[0]["account"]
    if not accts:
        return ""
    defaults = [a for a in accts if (a.get("meta") or {}).get("is_default")]
    if len(defaults) == 1:
        return defaults[0]["account"]
    sc = f", scope='{scope}'" if scope else ""
    noun = account_noun(provider)
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Plusieurs {noun}s `{provider}` configurés {where}, aucun (ou "
            f"plusieurs) marqué par défaut — précise lequel avec `_account=\"<nom>\"` "
            f"sur cet appel (oto_identity(op='list'{sc}) pour les lister, "
            f"oto_identity(op='set'{sc}) pour en fixer un par défaut)."
        )))


def personal_instance_org(sub: str, provider: str,
                          exclude_org: Optional[int] = None) -> Optional[int]:
    """Org portant l'instance PERSONNELLE cross-org de `sub` pour un connecteur
    par-personne (issue #172, piste A ; `Connector.personal_cross_org`), ou None.

    Déterministe (jamais de choix muet entre deux identités du MÊME humain) : l'org
    PERSO d'abord (une seule par sub, ADR 0030) si elle porte une clé membre, sinon
    la plus RÉCEMMENT posée. `exclude_org` écarte l'org de contexte (déjà testée en
    amont par le palier membre local). Sûr par construction : même `sub` ⟹ zéro
    usurpation — on ne fait que retrouver SA propre clé posée ailleurs.

    `provider` est normalisé vers le porteur du credential (délégation) : la clé
    perso d'un canal unipile est celle du COMPTE, et l'org à retenir est celle où
    cette clé vit. Le compte hébergé sera cherché dans la MÊME org
    (`connectors/identities._own_unipile_account_id`) — clé et compte appariés,
    jamais la clé d'ici avec le compte de là-bas."""
    provider = providers.credential_provider(provider)
    orgs = [o for o in credentials_store.list_member_orgs_for(sub, provider)
            if o != exclude_org]
    if not orgs:
        return None
    personal = org_store.get_personal_org(sub)
    if personal is not None and personal in orgs:
        return personal
    return orgs[0]  # set_at DESC → la plus récente


def _platform_grantee_scope(sub, active_org, scopes) -> "str | None":
    """Le scope de `scopes` qui vise `sub` sur une instance PLATEFORME, ou None (ADR 0044
    §F). `user:<sub>` prime (le plus spécifique) ; `org:<id>` gaté sur l'org **ACTIVE**
    (mirroir EXACT de l'ancien `get_active_org_grant(active_org)` — un grant d'org est métré
    per-contexte-d'org, pas per-appartenance : un membre de l'org X actif dans Y n'en profite
    pas). Sert l'accès (closed) ET le quota (rate_limit_by)."""
    if not scopes:
        return None
    if f"user:{sub}" in scopes:
        return f"user:{sub}"
    if active_org is not None and f"org:{active_org}" in scopes:
        return f"org:{active_org}"
    return None


def _platform_instance_usable(sub, active_org, inst: dict) -> bool:
    """Instance plateforme utilisable par `sub` ? (ADR 0044 §F, mode-aware). Un prêt
    `share_side` autorise (membership, comme un prêt BYO). Sinon selon `share_mode` :
    'open' = `share_down` vide (free-tier, ouvert à tous) OU `sub` grantee ; 'closed' =
    `sub` grantee (défaut fermé)."""
    down, side = inst.get("share_down") or [], inst.get("share_side") or []
    if scope._sub_matches_scopes(sub, side):
        return True
    granted = _platform_grantee_scope(sub, active_org, down) is not None
    if inst.get("share_mode") == "closed":
        return bool(down) and granted
    return (not down) or granted


def _platform_quota(sub, active_org, meta: dict) -> "int | None":
    """Quota/jour du bénéficiaire sur une instance plateforme : `rate_limit_by[scope de sub]`
    (user prime > org active), sinon le défaut `rate_limit` de l'instance."""
    rlb = (meta or {}).get("rate_limit_by") or {}
    scope = _platform_grantee_scope(sub, active_org, list(rlb.keys()))
    if scope is not None and scope in rlb:
        return rlb[scope]
    return (meta or {}).get("rate_limit")


def _legacy_platform_grant_meta(sub, provider, active_org) -> "dict | None":
    """Palier plateforme (ADR 0044 §F R3) SANS secret : {label, daily_quota} de l'instance
    PLATEFORM utilisable par `sub` la plus récente, ou None. Base des miroirs `status_for`/
    `credential_mode_for` (présence + quota, jamais de déchiffrement).

    ⚠️ **L'ancien chemin, et il ne bouge pas d'un octet** (blueprint ADR 0053, lot L5) :
    il reste le seul pour les neuf connecteurs non basculés, et le repli EXACT pour un
    bénéficiaire que la chaîne ne connaît pas. Le préfixe `_legacy_` ne le déprécie pas —
    il nomme l'une des deux voies de la fenêtre de double lecture."""
    for inst in credentials_store.list_platform_instances(provider):
        if _platform_instance_usable(sub, active_org, inst):
            return {"label": inst["label"],
                    "daily_quota": _platform_quota(sub, active_org, inst.get("meta"))}
    return None


def _platform_grant_meta(sub, provider, active_org) -> "dict | None":
    """Le palier plateforme, **chaîne de grants d'abord** (blueprint ADR 0053, lot L5).

    Trois issues, et la troisième est ce qui rend la fenêtre sûre :

    - la chaîne ACCORDE → son verdict (clé + quota portés par l'arête) ;
    - la chaîne REFUSE (des arêtes existent, toutes révoquées) → refus **sans repli** :
      sinon révoquer une arête ne couperait rien, l'ancien chemin free-tier
      re-accordant aussitôt ;
    - la chaîne est MUETTE (connecteur non basculé, ou aucune arête n'a jamais visé cet
      appelant) → l'ancien chemin, à l'identique.

    Les deux voies sont lues pour un connecteur basculé — c'est le prix assumé de la
    fenêtre (une lecture indexée de plus) et c'est ce qui produit le journal d'écart,
    matière du verdict de fin de fenêtre."""
    verdict = grants_chain.platform_rung(sub, provider, active_org)
    if verdict is None:
        return _legacy_platform_grant_meta(sub, provider, active_org)
    legacy = _legacy_platform_grant_meta(sub, provider, active_org)
    grants_chain.journal_resolution(provider, sub, active_org, verdict, legacy)
    if not verdict.granted:
        return None
    return {"label": verdict.label, "daily_quota": verdict.quota}


def _resolve_platform_grant(sub, provider, active_org) -> "dict | None":
    """Palier plateforme AVEC secret : {label, secret, daily_quota} ou None. Remplace les 3
    lectures legacy (get_active_grant/get_active_org_grant/get_platform_api_key). Le secret
    n'est déchiffré QUE pour l'instance gagnante (chemin chaud)."""
    g = _platform_grant_meta(sub, provider, active_org)
    if not g:
        return None
    secret = credentials_store.get_credential(credentials_store.PLATFORM, g["label"], provider)
    if secret is None:
        return None
    return {**g, "secret": secret}


# ── Walker de cascade unique ───────────────────────────────────────────────────
# La cascade `perso > cross-org > équipe active > org > plateforme` était écrite à
# la main à 6 endroits (résolution, mode, status ×2, anonyme, sonde de publication)
# — chaque barreau nouveau devait être reporté N fois, et chaque oubli faisait
# MENTIR une surface (vécu 2026-07-16 : boucle fields de status_for restée
# user-only ; 2026-07-07 : règle option recopiée 3×, divergée). Ici : UNE marche,
# paramétrée par la SONDE — `presence` (pas de déchiffrement, batchable /api/me)
# ou `fetch` (ne déchiffre que le gagnant). Toute évolution de cascade se fait ici
# et nulle part ailleurs.

@dataclass(frozen=True)
class CascadeRung:
    """Un barreau GAGNANT de la marche : niveau + entité + charge de la sonde
    (`payload` = secret/grant en fetch, True/meta en présence). `via` distingue la
    clé membre LOCALE (éditable ici) de l'instance personnelle cross-org (#172)."""
    mode: str                       # user | group | org | tenant | platform
    entity_type: Optional[str]      # credentials_store.MEMBER | 'group' | 'org' | TENANT | PLATFORM
    entity_id: Optional[str]
    payload: object
    account: str = ""
    via: str = "local"              # local | cross_org

    def __repr__(self) -> str:
        """Expurgé SANS CONDITION (#564) : `payload` porte le secret déchiffré en
        mode fetch, et rien à l'exécution ne distingue ce mode de la sonde de
        PRÉSENCE, dont le payload est anodin. Cf. `secret_repr`."""
        return secret_repr.expurge(self, "payload")


@dataclass(frozen=True)
class CascadeProbe:
    """Sonde d'un barreau — même interface pour présence et fetch. `member` renvoie
    `(payload, account)` ou None (le fetch de résolution y encapsule sa sélection
    multi-compte, McpErrors comprises) ; `member_cross` est toujours mono-compte ;
    `tenant` reçoit le SLUG (L-clés PR 1) et répond comme `org` ; `platform` renvoie
    le grant (meta ou résolu) ou None. Champ REQUIS pour chacun : une sonde qui
    oublierait un barreau le sauterait en silence — le défaut de #409."""
    member: Callable[[str, int, str], Optional[tuple]]
    member_cross: Callable[[str, int, str], Optional[object]]
    legacy_user: Callable[[str, str], Optional[object]]
    group: Callable[[int, str], Optional[object]]
    org: Callable[[int, str], Optional[object]]
    tenant: Callable[[str, str], Optional[object]]
    platform: Callable[[Optional[str], str, Optional[int]], Optional[dict]]


# Une instance membre SUSPENDUE (lot 2 / ADR 0044 §KeyStack) est repliée dans les
# sondes réelles : la clé existe au coffre mais la cascade la traite comme absente
# → la résolution ET le statut sautent le barreau membre (le niveau du dessous prend
# le relais). Elle reste listée par `oto_instance op=list` (KeyStack), réactivable.
# ⚠️ La sonde membre du chemin de RÉSOLUTION n'est PAS celle-ci : c'est `_member_fetch`
# (dans `_resolve_credential_impl`), qui porte la sélection multi-compte — elle doit
# rendre le MÊME verdict de suspension (vécu #401 : elle ne le lisait pas, une clé
# suspendue gagnait quand même pendant que le KeyStack annonçait le relais).
PRESENCE_PROBE = CascadeProbe(
    member=lambda s, o, p: ((True, "") if db.has_member_api_key(s, o, p)
                            and not db.member_instance_suspended(s, o, p) else None),
    member_cross=lambda s, o, p: (True if db.has_member_api_key(s, o, p) else None),
    legacy_user=lambda s, p: (True if credentials_store.has_credential(
        credentials_store.USER, s, p) else None),
    group=lambda g, p: (True if group_store.has_group_secret(g, p) else None),
    org=lambda o, p: (True if org_store.has_org_secret(o, p) else None),
    tenant=lambda t, p: (True if tenant_vault.has_tenant_secret(t, p) else None),
    platform=lambda s, p, o: _platform_grant_meta(s, p, o),
)

# ⚠️ Les sondes org/group de FETCH_PROBE lisent le compte MONO (`account=''`) : les
# chemins qui doivent voir les comptes NOMMÉS d'un palier partagé (résolution réelle
# `_org_fetch`/`_group_fetch`, résolution anonyme `_anon_org_fetch`) composent leur
# propre CascadeProbe par-dessus — ne pas brancher FETCH_PROBE tel quel sur un
# nouveau chemin de résolution d'un connecteur multi-compte (review #399 F3).
FETCH_PROBE = CascadeProbe(
    member=lambda s, o, p: ((lambda k: (k, "") if k
                             and not db.member_instance_suspended(s, o, p) else None)(
                                 db.get_member_api_key(s, o, p))),
    member_cross=lambda s, o, p: db.get_member_api_key(s, o, p),
    legacy_user=lambda s, p: credentials_store.get_credential(
        credentials_store.USER, s, p),
    group=lambda g, p: group_store.get_group_secret(g, p),
    org=lambda o, p: org_store.get_org_secret(o, p),
    tenant=lambda t, p: tenant_vault.get_tenant_secret(t, p),
    platform=lambda s, p, o: _resolve_platform_grant(s, p, o),
)


def group_secret_map(groups: Optional[list] = None) -> dict:
    """`{group_id: {connecteurs dont l'équipe détient un secret}}` — UNE lecture par
    équipe, jamais une par (équipe × connecteur).

    Deux appelants posent CETTE question sur le chemin `/api/me` : le barreau `group`
    de la sonde préchargée, et le hint `team_key_group` de `status_for`. Le second
    interrogeait la base par connecteur (`has_group_secret`), et comme il ne se
    déclenche que sur les connecteurs `forbidden` — la majorité d'un compte réel — il
    coûtait à lui seul 67 allers-retours là où l'inventaire était DÉJÀ chargé à côté
    de lui. D'où l'extraction en fonction nommée : les deux la construisent chacun,
    par la même lecture.

    Chacun la sienne, et NON une carte passée de l'un à l'autre : `preloaded_presence_probe`
    est un seam de test (stubbé par lambda dans trois fichiers), et lui ajouter un
    paramètre casse ces stubs pour économiser une lecture PAR ÉQUIPE — une à trois,
    contre les soixante-sept que ce lot retire. Le partage coûterait plus qu'il ne rend.

    ⚠️ Même définition de « détient » que la sonde préchargée, à dessein : la présence
    d'une ligne dans `list_credentials`. `has_credential` y ajoute `secret_enc IS NOT
    NULL` (la colonne est nullable en schéma, quoi qu'en dise son commentaire). Sur ce
    point la carte n'invente rien — elle ALIGNE le hint sur le verdict que la cascade
    rend déjà, au lieu de le laisser répondre par un chemin qui pourrait diverger.
    """
    from .. import credentials_store as cs

    par_groupe: dict = {}
    for g in (groups or []):
        gid = int(g["group_id"] if isinstance(g, dict) else g)
        par_groupe[gid] = {r["connector"]
                           for r in cs.list_credentials("group", str(gid))}
    return par_groupe


def preloaded_presence_probe(sub: str, *, org: Optional[int],
                             groups: Optional[list] = None) -> CascadeProbe:
    """`PRESENCE_PROBE`, mais préchargée : les mêmes réponses, en quelques lectures.

    **C'est une TROISIÈME SONDE, pas un second chemin.** Le walker n'est pas touché
    d'une ligne : toute la cascade — gates byo_user, instance personnelle cross-org,
    ORG_SHAREABLE, éligibilité plateforme — reste où elle est. On ne change que la
    façon dont les cinq questions trouvent leur réponse : en mémoire, depuis un
    inventaire lu une fois, au lieu d'un aller-retour par connecteur.

    ⚠️ **Le prix d'une sonde préchargée est de rester ÉQUIVALENTE.** Elle ne peut pas
    dériver silencieusement : `tests/test_presence_batch.py` la confronte à
    `PRESENCE_PROBE` sur l'ensemble des connecteurs du registre, même contexte, et
    exige le MÊME verdict. Un barreau ajouté demain à la cascade casse ce différentiel
    au lieu de produire deux vérités.

    Mesuré (33 connecteurs installés, compte réel) : les cinq sondes coûtaient 425 ms
    en marchant une fois par connecteur. Ce qu'elle ne couvre PAS, et volontairement :
    - le barreau **plateforme** garde la callable d'origine — il lit une chaîne de
      grants avec journal d'écart (fenêtre de bascule 0053-L5), et le précharger
      demanderait de rejouer cette logique ailleurs. Mesuré à 9 ms l'appel sur les
      seuls connecteurs qui l'atteignent : le gain ne paie pas le risque ;
    - `personal_instance_org` est appelé par le WALKER, pas par la sonde (un appel,
      12 ms) — le précharger supposerait de toucher au walker, ce qu'on refuse.

    """
    from .. import credentials_store as cs

    membre: set = set()
    suspendues: set = set()
    if org is not None:
        for r in cs.list_credentials(cs.MEMBER, cs.member_id(org, sub)):
            membre.add(r["connector"])
            if (r.get("meta") or {}).get("suspended") in (True, "true"):
                # La suspension ne vaut que pour le compte MONO (account '') — c'est
                # ce que la sonde d'origine interroge (`account=""`).
                if not r.get("account"):
                    suspendues.add(r["connector"])

    par_groupe = group_secret_map(groups)

    org_secrets: set = set()
    if org is not None:
        org_secrets = {r["connector"] for r in cs.list_credentials("org", str(org))}

    # Barreau TENANT (L-clés PR 1) : une lecture, seulement pour un sub d'un tenant
    # tiers — `rung_tenant` rend None pour un sub nu, et l'inventaire n'est pas lu.
    tenant_secrets: set = set()
    slug = tenant_vault.rung_tenant(sub)
    if slug is not None:
        tenant_secrets = {r["connector"] for r in cs.list_credentials(cs.TENANT, slug)}

    return CascadeProbe(
        member=lambda s, o, p: ((True, "") if p in membre and p not in suspendues
                                else None),
        # Cross-org : l'inventaire ne porte QUE l'org active, donc on retombe sur la
        # lecture d'origine. C'est un appel, pas trente-trois : le walker n'y arrive
        # que pour les connecteurs `personal_cross_org` mono-compte.
        member_cross=PRESENCE_PROBE.member_cross,
        legacy_user=PRESENCE_PROBE.legacy_user,  # (#876) même exception que member_cross
        group=lambda g, p: (True if p in par_groupe.get(int(g), ()) else None),
        org=lambda o, p: (True if p in org_secrets else None),
        tenant=lambda t, p: (True if p in tenant_secrets else None),
        platform=PRESENCE_PROBE.platform,
    )


def walk_cascade(sub: Optional[str], provider: str, *, org: Optional[int],
                 group: "Optional[int] | Callable[[], Optional[int]]",
                 probe: CascadeProbe, want: str = "auto"):
    """Générateur des barreaux gagnants, DANS L'ORDRE de la cascade. Consommer le
    premier = résolution (`cascade_winner`) ; tout consommer = statut (les niveaux
    configurés au-delà du gagnant restent affichables). Chaque gate (byo_user,
    ORG_SHAREABLE, personal_cross_org, éligibilité plateforme, `want='byo'`)
    vit ICI — plus jamais dans un call-site.

    **Délégation de credential** (`Connector.credential_of`) : `provider` est d'abord
    normalisé vers le connecteur qui PORTE la clé (les six canaux unipile → `unipile`).
    Le faire ICI et nulle part ailleurs est la raison d'être de ce walker : la
    résolution (`_resolve_credential_impl`), le miroir de mode (`credential_mode_for`)
    et le statut (`status_for`) le traversent tous les trois — ils ne peuvent donc pas
    répondre trois choses différentes à « quelle clé ? ». Normaliser dans chacun d'eux
    rouvrirait exactement la divergence du 2026-07-07 (carte « clé d'org » verte à côté
    d'un « Bloqué » rouge). Ce que le walker ne décide PAS, en revanche, c'est le DROIT
    d'appeler : activation, ACL et sélection restent gatées sur le nom NU, chez
    l'appelant."""
    provider = providers.credential_provider(provider)
    if sub is not None and org is not None and providers.is_byo_user(provider):
        hit = probe.member(sub, org, provider)
        if hit is not None:
            payload, account = hit
            yield CascadeRung("user", credentials_store.MEMBER,
                              credentials_store.member_id(org, sub), payload, account)
    # Instance personnelle cross-org (#172, amende ADR 0033) : connecteur par-personne
    # (unipile) → ma clé posée dans une AUTRE org me suit (même sub, zéro usurpation).
    # Mono-compte seulement. Prime sur les paliers partagés, comme la clé locale.
    if (sub is not None and providers.is_personal_cross_org(provider)
            and not _is_multi_account(provider, org)):
        pio = personal_instance_org(sub, provider, exclude_org=org)
        if pio is not None:
            payload = probe.member_cross(sub, pio, provider)
            if payload is not None:
                yield CascadeRung("user", credentials_store.MEMBER,
                                  credentials_store.member_id(pio, sub), payload,
                                  via="cross_org")
    # Scope LEGACY (#876) : LA ligne du sub DEMANDEUR seul, liste fermée.
    if sub is not None and provider in LEGACY_USER_SCOPE_PROVIDERS:
        payload = probe.legacy_user(sub, provider)
        if payload is not None:
            yield CascadeRung("user", credentials_store.USER, sub, payload)
    if provider in ORG_SHAREABLE_PROVIDERS:
        # `group` accepte un callable zéro-arg (résolution PARESSEUSE de l'équipe
        # active) : le générateur s'arrête au premier barreau gagnant, donc une
        # clé membre trouvée ne coûte jamais le lookup DB de `current_group`
        # (iso-comportement avec l'ancien chemin ; vécu test_call_axes_account).
        g = group() if callable(group) else group
        if g is not None:
            hit = probe.group(g, provider)
            if hit is not None:
                # Sonde de fetch multi-compte : (payload, account) — la sonde de
                # présence répond un booléen, le barreau reste mono ('').
                payload, account = hit if isinstance(hit, tuple) else (hit, "")
                yield CascadeRung("group", "group", str(g), payload, account)
        if org is not None:
            hit = probe.org(org, provider)
            if hit is not None:
                payload, account = hit if isinstance(hit, tuple) else (hit, "")
                yield CascadeRung("org", "org", str(org), payload, account)
        # Étage TENANT (L-clés PR 1, ADR 0052) : la clé partagée du tenant de
        # l'APPELANT — lu sur son sub qualifié, jamais sur le rattachement de l'org
        # (lot L1). `rung_tenant` rend None pour un sub nu (tenant primaire : ses clés
        # partagées sont les instances plateforme) et pour l'anonyme — le barreau
        # n'est alors pas sondé du tout, donc il ne coûte rien là où il ne peut rien
        # trouver. Sous le gate ORG_SHAREABLE comme l'équipe et l'org : c'est une clé
        # partagée. Servi AVANT la plateforme : plus proche de l'appelant.
        slug = tenant_vault.rung_tenant(sub)
        if slug is None and sub is None and org is not None:
            # ANONYME (ADR 0032, L-clés PR 2) : pas d'identité ⟹ le tenant ne se lit
            # que sur une arête VIVANTE tenant→org — jamais sur le rattachement de
            # l'org (lot L1). Sans arête, l'anonyme garde sa cascade `org > plateforme`.
            slug = grants_chain.tenant_for_org(org, provider)
        if slug is not None:
            hit = probe.tenant(slug, provider)
            if hit is not None:
                # L'arête tenant→org (0053, PR 2) — lue APRÈS la sonde, donc jamais
                # sans clé : MUETTE ⟹ la clé sert (PR 1) ; ACCORDE ⟹ elle sert, le
                # budget se règle à la résolution (`tenant_budget`) ; REFUSE ⟹ le
                # barreau se SAUTE et l'org retombe sur la plateforme.
                verdict = grants_chain.tenant_rung(slug, provider, org)
                if verdict is None or verdict.granted:
                    payload, account = hit if isinstance(hit, tuple) else (hit, "")
                    yield CascadeRung("tenant", credentials_store.TENANT, slug, payload,
                                      account, via="grant" if verdict else "local")
    if want != "byo":
        con = providers.connector_for_provider(provider)
        if con is not None and "platform" in con.auth_modes:
            grant = probe.platform(sub, provider, org)
            if grant:
                yield CascadeRung("platform", credentials_store.PLATFORM,
                                  grant.get("label"), grant)


def cascade_winner(sub: Optional[str], provider: str, *, org: Optional[int],
                   group: "Optional[int] | Callable[[], Optional[int]]",
                   probe: CascadeProbe,
                   want: str = "auto") -> Optional[CascadeRung]:
    """Premier barreau gagnant, ou None si rien ne résout."""
    return next(walk_cascade(sub, provider, org=org, group=group,
                             probe=probe, want=want), None)
