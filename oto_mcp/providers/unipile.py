"""Déclaration de registre du connecteur `unipile` — le COMPTE FOURNISSEUR.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.

⚠️ Depuis le **split du 2026-08-28**, `unipile` n'est plus la carte de la
messagerie : il est le compte CHEZ le fournisseur (la clé, le quota, la clé
plateforme, l'option couche-3, les sièges). Les six **connexions** — LinkedIn,
WhatsApp, Telegram, Instagram, Messenger, X/Twitter — sont des connecteurs à part
entière (`providers/{linkedin,whatsapp,…}.py`) qui DÉLÈGUENT leur
credential ici (`credential_of="unipile"`). Cf. `docs/unipile.md` §Split.
"""
from __future__ import annotations

from ._model import _c

# unipile : le COMPTE Unipile — une clé d'abonnement qui ouvre N sessions hébergées
# (vrai Chrome + proxy résidentiel chez le fournisseur → contourne empreinte TLS et
# isolation de session du browser local, #5). Keyed api_key (résolu via
# resolve_api_key, cascade user > groupe > org > clé plateforme). byo_user (BYO) OU
# byo_org (l'org pose l'abonnement Otomata, ses membres connectent leurs comptes par
# hosted-auth). Hors socle (comme tout le catalogue, 16/07) ; l'option payante
# (couche 3) gate l'usage plateforme, le BYO reste libre. Le **dsn** (API v2 :
# gateway `api.unipile.com`) est résolu côté client (env `UNIPILE_DSN`, défaut
# api.unipile.com = celui d'Otomata) — PAS un champ de credential tant qu'un BYO
# sur un autre endpoint n'existe pas (déféré ; single-field = compatible avec le
# stockage org-secret existant, mono-valeur).
#
# ⚠️ Il ne lui reste QU'UN namespace, `unipile`, pour `unipile_connect_start`
# (multi-canal : linkedin|whatsapp|… — il n'appartient à aucune capacité, sa place
# cible est `oto_connector op=connect`, cf. oto-backend#279). Les namespaces des
# canaux (`linkedin`, `whatsapp`, `telegram`, `instagram`, `messenger`,
# `twitter`) sont partis avec leurs connecteurs : c'est ce qui donne à chaque canal
# un gate d'activation, une ACL, une sélection et une visibilité PROPRES.
#
# ⚠️ `hosted_auth` est retombé à False : la connexion hébergée est l'affaire des
# cartes de canal. Ici, `auth_method` vaut donc "secret" → la carte rend le
# formulaire de clé (BYO) et l'état de la clé plateforme. C'est ce que cette carte
# EST depuis le split : le compte fournisseur, pas une connexion.
CONNECTOR = _c(
    "unipile", ["unipile"],
    auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", personal_cross_org=True,
    # free-tier : clé plateforme OUVERTE à tous, gardée par l'OPTION couche-3 (has_option),
    # PAS par un allowlist de clé. Sans ce flag, un grant plateforme (onboarding d'un user
    # à unipile via le dashboard) fait passer la clé `open`→`closed`+share_down=[ce user]
    # et coupe TOUS les autres (panne all-users vécue 2× — org 194, puis un user). Avec le
    # flag, `platform_grant` ne pose QUE le quota, ne ferme jamais la clé (cf. oto-backend#245).
    platform_key_open=True,
    label="Compte Unipile",
    help="Le compte (clé d'abonnement) qui porte les connexions hébergées — "
         "LinkedIn, WhatsApp, Telegram, Instagram, Messenger, X/Twitter",
    href="https://www.unipile.com",
    modules=("unipile",),
)

CATEGORY = "Prospection"
PUBLISHER = "Unipile"
LOGO_DOMAIN = "unipile.com"
DESCRIPTION = (
    "Le compte Unipile qui porte tes connexions hébergées. Une seule clé "
    "d'abonnement ouvre les six canaux — LinkedIn, WhatsApp, Telegram, Instagram, "
    "Messenger et X/Twitter — que tu connectes ensuite un par un, chacun sur sa "
    "propre carte. Pose ta clé ici (BYO) ou utilise celle de la plateforme."
)


# --- la FORME d'une connexion hébergée (le porteur la décrit) ----------------

def channel(name: str, namespace: str, *, hosted_channel: str, label: str,
            help: str, href: str, modules: tuple[str, ...] = ()):
    """Entrée de registre d'UN canal hébergé — six connecteurs, une seule forme.

    Chaque canal a son domicile (`providers/<nom>.py`) et y déclare ce qui le
    DISTINGUE : son nom, son namespace de tools, son canal Unipile, son libellé,
    sa marque. Ce qu'il partage avec les cinq autres — les modes d'auth, la
    délégation de credential, le caractère par-personne, l'ouverture de la clé
    plateforme — est décrit ICI, chez le porteur de la clé, parce que c'est une
    propriété du COMPTE Unipile et pas du canal. Recopier ces cinq drapeaux six
    fois, c'est se donner cinq occasions de les faire diverger ; et le canal qui
    divergerait sur `platform_key_open` rejouerait la panne all-users de #245.

    ⚠️ `platform_key_open` n'est PAS recopié ici, et c'est délibéré : il gouverne
    le PARTAGE d'une clé plateforme (`credentials_store`), donc il se lit sur le
    connecteur qui la porte — la cascade a déjà normalisé quand on l'atteint. Le
    poser sur un canal serait de la configuration morte, que le prochain lecteur
    croirait vivante et pourrait faire diverger de celle du compte : exactement la
    forme de la panne all-users de #245.

    ⚠️ `personal_cross_org`, LUI, est recopié — et ce n'est pas une incohérence avec
    la ligne du dessus. Les deux drapeaux sont lus à des moments différents :
    `platform_key_open` sur le chemin du CREDENTIAL (après normalisation vers le
    porteur, donc jamais sur le canal), `personal_cross_org` sur le chemin du TOOL —
    `call_axes` résout par NAMESPACE, et c'est ce drapeau qui fait exister l'axe
    `_account=` sur `{canal}_chat`. Sans lui, on perdrait le pin d'identité opérée,
    donc les comptes accordés (#55) sur les six canaux. Ne pas « uniformiser » les
    deux par symétrie : ils ne répondent pas à la même question.

⚠️ `href` est celui de la MARQUE du canal (linkedin.com, whatsapp.com…), jamais
    celui du fournisseur : ce que la personne connecte, c'est son compte LinkedIn ou
    son WhatsApp. Unipile est notre plomberie — la nommer sur la carte demanderait à
    l'utilisateur de connaître un tiers avec qui il n'a aucune relation.

    Le canal ne DÉTIENT rien : `credential_of="unipile"` renvoie coffre, quota,
    clé plateforme et option couche-3 sur le compte. Ce qu'il possède en propre,
    c'est ce qui se gouverne par canal — activation, ACL, sélection, visibilité
    des tools, et sa connexion hébergée.
    """
    return _c(
        name, [namespace],
        auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
        secret_kind="api_key", hosted_auth=True, personal_cross_org=True,
        credential_of="unipile", hosted_channel=hosted_channel,
        label=label, help=help, href=href,
        modules=modules or (name,),
    )
