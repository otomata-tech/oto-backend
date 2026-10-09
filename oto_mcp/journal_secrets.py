"""Les deux empreintes courtes d'un secret — celle du journal, celle du coffre.

Un secret n'apparaît hors du serveur que sous une forme courte et non inversible.
Ce module en porte les DEUX, et la clé unique qui les signe :

- `mask()` — ce que le **journal** écrit à la place d'un jeton (`tool`, `args`).
  Volontairement **corrélable** : deux lignes portant le même masque disent « le même
  jeton, rejoué », sans jamais dire lequel.
- `fingerprint()` — ce que la **lecture d'un credential** rend à la place de la valeur
  (oto-backend#671, 2026-08-31). Volontairement **NON corrélable** : elle est liée à
  la ligne du coffre, donc la même clé posée à deux endroits n'y donne pas la même
  empreinte. C'est ce qui empêche un lecteur d'empreintes de confirmer une clé devinée
  par ailleurs en la posant sur une ligne qu'il contrôle.

Une seule clé pour les deux : un second secret à gérer n'apporterait rien, et les deux
propriétés se lisent mieux côte à côte que dans deux modules.

## Ce que le journal d'appels n'écrit jamais en clair — et pourquoi par PROPRIÉTÉ

Le journal (`tool_calls`, ADR 0017) porte deux colonnes alimentées par des données
d'appelant : `tool` (pour un geste REST : `MÉTHODE /route`) et `args`. Jusqu'au
2026-08-29, la réduction de `tool` (`api/routes._normalize_route`) était une
**allowlist de FORMES** — numérique ou UUID → `:id`, tout le reste passe. Or les
routes servies portaient leur secret DANS le chemin (`/api/upload/{token}`,
`/api/public/docs/{token}`, `/api/invitations/{token}`, et jusqu'au 15/09/2026
`/api/invitations/code/{code}` — retirée depuis avec le code court d'invitation
lui-même, oto-backend#560), et aucun de ces secrets n'a la forme d'un identifiant :
ils partaient donc en clair dans une table lue par les surfaces de supervision
(#558). Pour l'invitation, le modèle de données refuse explicitement de persister
le jeton en clair (`org_store/invitations.py` n'enregistre que son empreinte) — un
middleware transverse défaisait cette précaution.

**La propriété qui remplace la forme** : un segment lié à un PARAMÈTRE DE ROUTE dont
le nom est déclaré secret ici est réduit, quelle que soit son allure. La liste des
routes concernées n'est écrite nulle part : elle est DÉRIVÉE de la table servie
(`api/routes.make_routes` appelle `declare_routes`), donc une route future qui
déclare `{token}` est couverte le jour où elle est montée.

La même propriété vaut sur l'autre face : le jeton d'invitation passe aussi par
l'outil `oto_org op=accept_invite`, dont l'`Input` déclare les mêmes noms. Un
argument de CAPACITÉ portant un de ces noms est masqué ; un argument de CONNECTEUR
qui s'appelle pareil ne l'est pas (`droit_article(code='CT')` n'est pas un secret,
et un journal qui le cache coûte une lecture sans rien protéger).

⚠️ **Le masque est un HMAC, pas « les 8 derniers » ni un sha256 nu.** Garder les 8
derniers caractères d'un secret court le rendrait en partie ENTIER, et un sha256 nu
se retrouve par force brute en quelques secondes pour qui lit le journal — même un
token long (256 bits) ne protège rien si sa réduction, elle, est courte et devinable.
La clé est celle qui signe déjà les jetons d'upload (`OTO_MCP_OAUTH_STATE_SECRET`) —
le masque reste donc stable d'un boot à l'autre, ce qui est tout son intérêt : deux
lignes portant le même masque disent « le même jeton, rejoué », sans jamais dire
lequel.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets as _secrets
from typing import Iterable, Optional
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)

# Les NOMS de paramètre qui portent un secret. C'est la seule liste écrite à la
# main de ce module, et elle est volontairement courte : tout le reste (quelles
# routes, quels outils) en est dérivé.
SECRET_PARAM_NAMES = frozenset({"token", "code"})

# Un paramètre dont le NOM n'est pas secret partout peut l'être sur UNE route : la
# route le DÉCLARE alors elle-même, sur son point d'entrée (`parametres_secrets`), et
# `declare_routes` le lit comme le reste — jamais une liste de chemins tenue ici.
# Premier cas : `/api/hooks/{trigger_id}`, dont le segment est l'adresse privée `h_…`
# d'un agent (128 bits, ce qui le rend introuvable à qui ne l'a pas reçue) ou l'id
# numérique d'un agent sans adresse privée, masqué pareil — la route ne sait lequel
# elle sert qu'en lisant la base. `trigger_id` n'est pas un secret ailleurs (les
# capacités de flotte le portent) : d'où la déclaration par route, pas par nom.
_ATTR_SECRETS = "parametres_secrets_du_chemin"


def parametres_secrets(*noms: str):
    """Décorateur d'un point d'entrée : ces paramètres de SON chemin sont secrets."""
    def poser(fn):
        setattr(fn, _ATTR_SECRETS, frozenset(noms))
        return fn
    return poser

# Les CONNECTEURS échappent à la dérivation ci-dessus (elle ne lit que le registre
# de capacités), et masquer leurs arguments par le NOM seul serait faux : un
# `code` métier n'est pas un secret. Un connecteur qui reçoit une VRAIE
# credential la déclare donc ici, outil par outil. Volontairement nominatif : ce
# n'est pas une heuristique sur « password », c'est une liste qu'on relit.
#
# `lemlist_mailbox` : `op="connect"` porte les mots de passe SMTP et IMAP d'une
# boîte mail — dans un sous-dictionnaire `smtp_imap`, que `truncated_args` sait
# traverser dès lors que le nom de la clé est déclaré. Sans cette ligne ils
# partaient EN CLAIR dans `tool_calls`, table lue par les surfaces de supervision.
# `lemlist_webhook` : `secret` signe les callbacks — qui l'a peut les forger.
# `typeform_webhooks` : idem, `secret` signe (HMAC) les réponses envoyées au webhook.
SECRET_TOOL_ARGS: dict[str, frozenset] = {
    "lemlist_mailbox": frozenset({"smtp_password", "imap_password"}),
    "lemlist_webhook": frozenset({"secret"}),
    "typeform_webhooks": frozenset({"secret"}),
    # A signed download link carries its credential in the query string.
    "oto_import": frozenset({"url"}),
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Forme d'une route déclarant un secret : (gabarit, {index → nom du paramètre}).
# Le gabarit est la découpe du patron, un paramètre valant None (joker).
_SECRET_ROUTES: list[tuple[tuple[Optional[str], ...], dict[int, str]]] = []

_KEY: Optional[bytes] = None


# --------------------------------------------------------------------------- #
# Le masque
# --------------------------------------------------------------------------- #

def _key() -> bytes:
    """Clé du masque. La même que celle des jetons signés — un secret de plus à
    gérer n'ajouterait rien, et un masque non clé serait inversible (cf. module).
    Sans aucun secret d'environnement (dev, tests), une clé de processus : le
    masque reste correct, il cesse seulement d'être corrélable entre deux boots."""
    global _KEY
    if _KEY is None:
        brut = (os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
                or os.environ.get("OTO_MCP_MASTER_KEY") or "")
        _KEY = brut.encode() if brut else _secrets.token_bytes(32)
    return _KEY


def mask(value) -> str:
    """Empreinte courte et NON INVERSIBLE d'un secret — corrélable, jamais lisible."""
    digest = hmac.new(_key(), str(value).encode("utf-8", "replace"),
                      hashlib.sha256).hexdigest()
    return "#" + digest[:12]


def fingerprint(*parts: object) -> str:
    """Empreinte de RECONNAISSANCE d'un secret posé : quatre caractères hexadécimaux,
    non inversibles, et **liés à l'endroit** où le secret est rangé.

    Elle répond à « est-ce toujours la même clé qu'hier ? » et « celle-ci ou l'autre ? »
    sans rendre un seul caractère du secret. Le front l'affiche `•••• 3f7a`.

    ⚠️ **Ce ne sont pas les derniers caractères de la clé.** Un suffixe DE LA CLÉ est un
    morceau de secret : il identifie un compte chez le fournisseur, et il confirme une
    clé devinée par ailleurs. Ici, quatre caractères d'un HMAC dont la clé ne sort pas
    du serveur — indevinable hors ligne.

    ⚠️ **Passer la LIGNE du coffre en premier, la valeur en dernier.** Sans les
    coordonnées de la ligne, la même clé donnerait la même empreinte partout : qui lit
    l'empreinte d'un palier pourrait poser un candidat sur une ligne à lui et comparer —
    un oracle de confirmation à 1/65536. Liée à sa ligne, la seule façon de comparer est
    d'écraser la clé qu'on cherchait à confirmer.

    Résiduel assumé et borné : sur une MÊME ligne, quatre caractères laissent une chance
    sur 65536 de collision — un lecteur peut donc croire inchangée une clé qui a été
    rotée. C'est un défaut d'affichage, pas de confidentialité ; la source de vérité de
    « quand a-t-elle changé » reste la date de pose servie à côté.
    """
    charge = "\x1f".join(str(p) for p in parts)
    return hmac.new(_key(), charge.encode("utf-8", "replace"),
                    hashlib.sha256).hexdigest()[-4:]


# --------------------------------------------------------------------------- #
# Les routes : la déclaration, dérivée de la table servie
# --------------------------------------------------------------------------- #

def declare_routes(routes: Iterable) -> int:
    """Recense les routes dont un paramètre est déclaré secret. Appelée par
    `api/routes.make_routes` sur la table qu'elle sert — jamais sur une liste
    tenue à la main. Rend le nombre de routes retenues.

    Accepte des routes Starlette (attribut `path`) ou des patrons bruts.
    """
    trouve: list[tuple[tuple[Optional[str], ...], dict[int, str]]] = []
    vus: set[tuple] = set()
    for route in routes:
        patron = getattr(route, "path", route)
        if not isinstance(patron, str):
            continue
        gabarit: list[Optional[str]] = []
        secrets_a: dict[int, str] = {}
        declares = getattr(getattr(route, "endpoint", None), _ATTR_SECRETS, frozenset())
        for i, seg in enumerate(patron.split("/")):
            if seg.startswith("{") and seg.endswith("}"):
                nom = seg[1:-1].split(":", 1)[0]
                gabarit.append(None)
                if nom in SECRET_PARAM_NAMES or nom in declares:
                    secrets_a[i] = nom
            else:
                gabarit.append(seg)
        if secrets_a and tuple(gabarit) not in vus:
            vus.add(tuple(gabarit))
            trouve.append((tuple(gabarit), secrets_a))
    # Le plus SPÉCIFIQUE d'abord : si deux routes à secret partagent un préfixe
    # (ex. `/api/invitations/{token}` et `/api/invitations/code/{code}`, jusqu'au
    # 15/09/2026 — cette paire n'existe plus, oto-backend#560), le gabarit le plus
    # court (plus générique) ne doit jamais matcher en premier un chemin qui
    # appartient en réalité au plus long : la route la plus spécifique perdrait
    # son nom dans l'agrégation.
    trouve.sort(key=lambda e: len(e[0]), reverse=True)
    _SECRET_ROUTES[:] = trouve
    return len(trouve)


def declared_secret_routes() -> list[tuple[tuple[Optional[str], ...], dict[int, str]]]:
    """Ce qui est déclaré, pour les tests et la maintenance."""
    return list(_SECRET_ROUTES)


def _secret_indices(segments: list[str]) -> dict[int, str]:
    """Indices de segments porteurs d'un secret, d'après la route la plus spécifique.

    Le gabarit peut être PLUS COURT que le chemin : un 404 sur
    `/api/upload/<jeton>/x` n'atteint aucun handler mais est journalisé comme tout
    `/api/*` — son jeton doit tomber aussi."""
    for gabarit, secrets_a in _SECRET_ROUTES:   # déjà triés du plus long au plus court
        if len(segments) < len(gabarit):
            continue
        if all(g is None or g == segments[i] for i, g in enumerate(gabarit)):
            return secrets_a
    return {}


def route_and_secrets(path: str) -> tuple[str, Optional[dict[str, str]]]:
    """`(route réduite, {nom: masque})` — ce que le journal écrit d'un chemin REST.

    La route réduite ne porte JAMAIS le masque : `tool` sert l'agrégation du
    monitoring (un `GROUP BY tool`), et y mettre une empreinte par jeton ferait
    exploser sa cardinalité. Le masque part dans `args`, où il répond à la seule
    question qu'on se pose vraiment : « le même jeton a-t-il été rejoué ? »"""
    segments = path.split("/")
    secrets_a = _secret_indices(segments)
    reduit, masques = [], {}
    for i, seg in enumerate(segments):
        nom = secrets_a.get(i)
        if nom:
            reduit.append(":" + nom)
            if seg:
                masques[nom] = mask(seg)
        elif seg.isdigit() or _UUID_RE.match(seg):
            # La réduction PAR FORME d'avant #558 : conservée telle quelle, sans
            # quoi l'agrégation du monitoring changerait de vocabulaire et les
            # séries seraient coupées en deux.
            reduit.append(":id")
        else:
            reduit.append(seg)
    # Un chemin plus long que le gabarit : la queue est de la donnée d'appelant
    # sans route, on la coupe plutôt que de la recopier.
    if secrets_a:
        fin = max(secrets_a) + 1
        reduit = reduit[:fin]
    return "/".join(reduit), (masques or None)


# --------------------------------------------------------------------------- #
# Le journal d'ACCÈS d'uvicorn : la même propriété, sur le troisième canal
# --------------------------------------------------------------------------- #

def routes_a_requete_secrete() -> tuple[str, ...]:
    """Les routes qui REÇOIVENT un secret dans leur query — protocole d'un tiers,
    pas un choix : le retour d'autorisation WordPress porte `password=`, celui
    d'Ubersuggest (client public) porte le code ET, dans le state, le vérificateur
    PKCE, qui ensemble valent un jeton. Leur query entière tombe, au journal d'accès
    (ici) comme dans Sentry (`sentry_setup`)."""
    from .auth import ubersuggest, wordpress
    return (wordpress.CALLBACK_PATH, ubersuggest.CALLBACK_PATH)


def requete_secrete(chemin: str) -> bool:
    """`chemin` (sans query) est-il une route dont la query porte un secret ? La
    variante à `/` final est la même route."""
    return (chemin.rstrip("/") or "/") in routes_a_requete_secrete()


# Les CLÉS de query dont la valeur ne s'écrit jamais au journal — d'accès (requêtes
# ENTRANTES, `uvicorn.access`) comme des clients HTTP (requêtes SORTANTES, `httpx`…) —
# sur TOUTE route : le retour d'un fournisseur OAuth (`code`, `state`, `session_state`)
# arrive en query sur la route de chaque connecteur et sur celle du relais ; une API
# tierce prend sa clé en query (`?key=`, `?api_key=`). Une clé est secrète
# par son NOM, jamais par la route qui la reçoit — une route future est couverte
# d'office. `error`/`error_description` restent lisibles : c'est le diagnostic.
CLES_DE_REQUETE_SECRETES = frozenset({
    "code", "state", "session_state", "id_token", "access_token", "refresh_token",
    "client_secret", "token", "key", "apikey", "api_key", "access_key",
})
# … et toute clé dont le nom CONTIENT l'un de ces fragments (`api_token`, `password`,
# `app_secret`…).
_FRAGMENTS_DE_CLE_SECRETE = ("secret", "token", "password")
MASQUE_DE_REQUETE = "***"


def cle_de_requete_secrete(cle: str) -> bool:
    """La valeur de la clé de query `cle` (brute, encodée ou non) est-elle un secret ?"""
    nom = unquote_plus(cle).strip().lower()
    return nom in CLES_DE_REQUETE_SECRETES or any(f in nom for f in _FRAGMENTS_DE_CLE_SECRETE)


def requete_pour_journal_acces(requete: str) -> str:
    """La query brute, chaque valeur d'une clé secrète remplacée par `***` ; l'ordre,
    les clés et les autres valeurs sont recopiés tels quels."""
    morceaux = requete.split("&")
    for i, morceau in enumerate(morceaux):
        cle, egal, _ = morceau.partition("=")
        if egal and cle_de_requete_secrete(cle):
            morceaux[i] = f"{cle}={MASQUE_DE_REQUETE}"
    return "&".join(morceaux)


def chemin_pour_journal_acces(cible: str) -> str:
    """La cible d'une requête (`chemin?requête`) telle que le journal d'accès l'écrit :
    chaque segment lié à un paramètre de route secret devient son masque, la query
    d'une route qui reçoit un secret en query devient `[redacted]`, la valeur de toute
    clé de query secrète (`cle_de_requete_secrete`) devient `***` sur toute autre
    route, tout le reste est recopié tel quel.

    Pas la réduction de `route_and_secrets` : le journal d'accès sert à lire UNE
    requête (quel tableau, quel numéro), pas à agréger — les identifiants y restent
    lisibles. Seul le secret tombe, et sous le MÊME masque que `tool_calls.args` :
    une ligne d'accès et une ligne d'appel se recoupent (« ce jeton a été rejoué »)
    sans que l'une ou l'autre dise lequel."""
    chemin, sep, requete = cible.partition("?")
    if sep and requete_secrete(chemin):
        requete = "[redacted]"
    elif sep:
        requete = requete_pour_journal_acces(requete)
    segments = chemin.split("/")
    secrets_a = _secret_indices(segments)
    if not secrets_a:
        return chemin + sep + requete
    for i in secrets_a:
        if i < len(segments) and segments[i]:
            segments[i] = mask(segments[i])
    return "/".join(segments) + sep + requete


class MasqueCheminAcces(logging.Filter):
    """Filtre de `uvicorn.access` : le chemin de chaque ligne passe par
    `chemin_pour_journal_acces`. Sans lui, `/api/receivers/apollo/phones/<jeton>`,
    `/api/upload/<jeton>`, `/api/invitations/<jeton>`… s'écrivaient EN CLAIR dans
    journald (le journal d'accès d'uvicorn ne connaît pas la table des routes) —
    la même fuite que #558, sur le canal que #558 n'avait pas vu. Et la query de
    chaque retour OAuth (`/api/<fournisseur>/oauth/callback?code=…&state=…`,
    `/oauth/callback` du relais) écrivait le code d'autorisation en clair.

    Une ligne qui n'a pas la forme d'uvicorn (`client, méthode, cible, version,
    statut`) passe inchangée : un filtre de journal ne lève jamais."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            masque = chemin_pour_journal_acces(args[2])
            if masque != args[2]:
                record.args = args[:2] + (masque,) + args[3:]
        return True


# --------------------------------------------------------------------------- #
# Les requêtes SORTANTES : la même règle de clés, sur les clients HTTP
# --------------------------------------------------------------------------- #

# Une query dans un texte libre : de `?` jusqu'au premier blanc, guillemet ou chevron.
_REQUETE_DANS_UN_TEXTE = re.compile(r"\?([^\s\"'<>]+)")


def masquer_requetes(texte: str) -> str:
    """Chaque query de `texte` (une URL complète, une ligne de journal) passée par
    `requete_pour_journal_acces` — la même règle que le journal d'accès."""
    return _REQUETE_DANS_UN_TEXTE.sub(
        lambda m: "?" + requete_pour_journal_acces(m.group(1)), texte)


def _argument_masque(arg):
    """`arg` tel que le journal l'écrit, sa query masquée ; inchangé (même objet) s'il
    n'y a rien à masquer — un `%d` reçoit toujours son entier."""
    if arg is None or isinstance(arg, (int, float, bytes)):
        return arg
    texte = arg if isinstance(arg, str) else str(arg)     # `httpx.URL`…
    if "?" not in texte:
        return arg
    masque = masquer_requetes(texte)
    return arg if masque == texte else masque


class MasqueRequeteSortante(logging.Filter):
    """Filtre des clients HTTP : la ligne `HTTP Request: POST https://…?key=… "HTTP/1.1
    200 OK"` d'httpx (INFO) écrivait la clé d'API d'un connecteur EN CLAIR dans
    journald. Masquer plutôt que remonter ces loggers en WARNING : la trace des appels
    sortants reste, sans leurs secrets. Chaque argument (et le message sans argument,
    forme d'httpcore) passe par `masquer_requetes` ; un filtre de journal ne lève jamais."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            masques = tuple(_argument_masque(a) for a in args)
            if any(m is not a for m, a in zip(masques, args)):
                record.args = masques
        elif not args and isinstance(record.msg, str) and "?" in record.msg:
            record.msg = masquer_requetes(record.msg)
        return True


JOURNAL_ACCES = "uvicorn.access"
# Les loggers qui écrivent l'URL d'une requête SORTANTE. Un filtre ne vaut que pour le
# logger NOMMÉ (pas ses enfants) : chaque logger d'httpcore est donc nommé. httpx écrit
# en INFO (servi en prod) ; urllib3 et httpcore en DEBUG seulement — couverts pareil,
# pour le jour où le niveau descend.
JOURNAUX_SORTANTS = ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                     "httpcore.http2", "httpcore.proxy", "httpcore.socks",
                     "urllib3.connectionpool")


def _poser(nom: str, classe: type) -> None:
    journal = logging.getLogger(nom)
    if not any(isinstance(f, classe) for f in journal.filters):
        journal.addFilter(classe())


def installer_masques_du_journal() -> None:
    """Pose `MasqueCheminAcces` sur `uvicorn.access` et `MasqueRequeteSortante` sur
    chaque logger de `JOURNAUX_SORTANTS` — une fois, même rappelé.

    Appelé par `server.main` AVANT `uvicorn.run` : la configuration de journal
    qu'uvicorn applique au démarrage (`dictConfig`, `disable_existing_loggers=False`)
    remplace les handlers des loggers, pas leurs filtres — les filtres tiennent donc
    quel que soit le lanceur, pourvu que le process passe par `server.main` (l'unité
    systemd lance `oto-mcp` → `cli.main` → `server.main`)."""
    _poser(JOURNAL_ACCES, MasqueCheminAcces)
    for nom in JOURNAUX_SORTANTS:
        _poser(nom, MasqueRequeteSortante)


# --------------------------------------------------------------------------- #
# Les arguments d'outil : la même propriété, sur l'autre face
# --------------------------------------------------------------------------- #

_ARG_NAMES: Optional[dict[str, frozenset]] = None


def _arg_names() -> dict[str, frozenset]:
    """`{outil MCP → noms d'arguments secrets}`, dérivé du registre de capacités.

    Seules les CAPACITÉS sont couvertes : ce sont les surfaces de la plateforme,
    et `{token}`/`{code}` y désignent toujours le même objet que dans les routes.
    Un connecteur qui expose un `code` métier (`droit_article`, `stripe_*`) n'est
    pas concerné — masquer par le nom seul y coûterait une lecture pour rien."""
    global _ARG_NAMES
    if _ARG_NAMES is None:
        # Les déclarations de CONNECTEUR d'abord, et hors du try : elles ne
        # dépendent d'aucun registre. Les poser après aurait rendu le masquage
        # des credentials tributaire d'un import de capacités — un registre
        # illisible aurait renvoyé les mots de passe SMTP en clair au journal,
        # avec pour seul signal un warning parlant d'autre chose.
        table: dict[str, frozenset] = dict(SECRET_TOOL_ARGS)
        try:
            import oto_mcp.capabilities  # noqa: F401 — peuple le registre
            from oto_mcp.capabilities.registry import caps_with_mcp
            for cap in caps_with_mcp():
                champs = set(getattr(cap.Input, "model_fields", {}) or {})
                secrets_ = champs & SECRET_PARAM_NAMES
                if secrets_:
                    table[cap.mcp] = table.get(cap.mcp, frozenset()) | secrets_

        except Exception:  # noqa: BLE001 — le journal ne casse jamais le service
            # Bruyant, et une seule fois : sans registre, le masquage des arguments
            # est INERTE. Un journal qui cesse de masquer sans le dire est
            # exactement le mode d'échec que ce module ferme.
            logger.warning("registre de capacités illisible : le masquage des "
                           "arguments de CAPACITÉ est inactif (les credentials "
                           "déclarées par connecteur restent masquées)",
                           exc_info=True)
            _ARG_NAMES = table
            return _ARG_NAMES
        _ARG_NAMES = table
    return _ARG_NAMES


def secret_arg_names_by_tool() -> dict:
    """La table complète `{outil → champs secrets}` — pour la purge rétroactive."""
    return dict(_arg_names())


def secret_arg_names(tool: Optional[str]) -> frozenset:
    """Noms d'arguments qu'un outil ne doit pas laisser passer en clair."""
    if not tool:
        return frozenset()
    return _arg_names().get(tool, frozenset())


# --------------------------------------------------------------------------- #
# La purge rétroactive (ADR 0065 — un travail de maintenance, pas un boot)
# --------------------------------------------------------------------------- #

def journal_purge_plans() -> list[tuple[str, str, list[str]]]:
    """Ce qu'il y a à réparer dans les lignes DÉJÀ écrites, une entrée par route :
    `(préfixe littéral, route réduite, préfixes plus spécifiques à exclure)`.

    Dérivé de la même déclaration que le masquage à l'écriture — pas d'une seconde
    liste qui divergerait. L'exclusion est ce qui empêche la passe d'une route
    générique d'écraser ce que la passe d'une route plus spécifique sous le même
    préfixe vient de réduire (exemple historique : `/api/invitations/` face à
    `/api/invitations/code/`, jusqu'au 15/09/2026 — oto-backend#560)."""
    plans = []
    prefixes = []
    for gabarit, secrets_a in _SECRET_ROUTES:
        premier = min(secrets_a)
        if any(g is None for g in gabarit[:premier]):
            continue  # secret précédé d'un joker : pas de préfixe littéral (aucun cas ce jour)
        prefixe = "/".join(gabarit[:premier]) + "/"
        reduit = prefixe + ":" + secrets_a[premier]
        prefixes.append(prefixe)
        plans.append((prefixe, reduit))
    return [(p, r, [q for q in prefixes if q != p and q.startswith(p)])
            for p, r in plans]
