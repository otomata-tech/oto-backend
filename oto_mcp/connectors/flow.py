"""Le geste « connecter » d'un connecteur — déclaré par son module, dérivé partout.

**Le problème que ça ferme.** Certains connecteurs ne s'obtiennent pas en collant des
champs : il faut un geste hors formulaire (consentement OAuth, session navigateur…).
Rien ne le DÉCLARAIT, alors chaque surface a compensé à sa façon — et toujours par le
NOM du connecteur. Le dashboard montait le widget de consentement derrière un
`['zoho','zohodesk','zohoanalytics'].includes(name)` ; Salesforce, qui a pourtant
exactement la même forme côté backend (capacité de démarrage, callback, les deux hooks
`status_hints`, la fabrique `oauth_flow`), n'y était simplement pas — donc pas de bouton,
et un client ne pouvait pas finir sa connexion. Ajouter un nom de plus aurait marché
cinq minutes et fait grossir la seule chose qu'il fallait supprimer.

**Ce que le seam garantit.** Un connecteur déclare son flux ICI, dans son propre module
(patron `connector_verify` / `status_hints`). Le catalogue en dérive un descripteur de
FORME — quels paramètres l'utilisateur doit fournir, comment s'appelle le geste — et le
front rend un formulaire générique + un bouton, sans jamais connaître un nom.

**Ce que le descripteur ne porte PAS, délibérément** : aucune URL, aucune clé de
capacité, aucun nom d'outil. `/api/connectors` est servi sans authentification ; un
descripteur qui publierait ses chemins internes ferait de la surface d'attaque un effet
de bord de la documentation. Le chemin est FIXE et connu du client
(`POST /api/me/connectors/{name}/connect`), le nom voyage en paramètre de chemin.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowParam:
    """Une valeur que l'utilisateur doit fournir pour démarrer le flux.

    `options` non vide ⟹ liste fermée (le front rend un select). C'est le DOMICILE
    UNIQUE de ces valeurs : la région Zoho était jusqu'ici recopiée quatre fois, dont
    une version fausse dans le libellé du registre (un `sa` que le code rejette)."""
    name: str
    label: str
    options: tuple[tuple[str, str], ...] = ()      # (valeur, libellé)
    default: str = ""
    required: bool = True
    help: str = ""

    def describe(self) -> dict:
        return {
            "name": self.name, "label": self.label, "required": self.required,
            "default": self.default, "help": self.help,
            "options": [{"value": v, "label": lbl} for v, lbl in self.options],
        }


@dataclass(frozen=True)
class FlowStart:
    """Ce que rend un flux — la MÊME forme pour tous, quel que soit le connecteur.

    `me.connector_connect` est le seam qui permet au front de brancher un connecteur
    sans savoir lequel. Sa sortie ne l'a pas suivi : Zoho échotait `{auth_url,
    connector}`, Salesforce `{auth_url, scope}`, et la garantie commune n'était qu'un
    commentaire de type (`-> {"auth_url": …}`) que rien ne faisait respecter — un
    troisième flux aurait inventé sa troisième clé. Le contrat publié a dû être
    déclaré ouvert avec deux champs optionnels : il documentait l'incohérence.

    **Le premier niveau est FERMÉ, et c'est le seul que l'appelant peut écrire** :
    `auth_url` à ouvrir dans un navigateur, rien d'autre. Ce qu'un connecteur veut
    échoter en plus descend dans `details`, un champ NOMMÉ dont le contenu est sa
    propriété — même règle qu'en entrée (la spécificité d'un connecteur vit dans son
    module) et même choix que `ResolvedCredential.config`. Figer l'union des clés
    aurait fait grossir le contrat commun à chaque flux ajouté ; ici il ne bouge plus.

    `details` n'est JAMAIS requis pour agir : un client qui le lit accepte de
    connaître le connecteur qu'il branche, ce que le seam ne lui demande pas."""
    auth_url: str
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"auth_url": self.auth_url, "details": dict(self.details)}


@dataclass(frozen=True)
class Flow:
    connector: str
    start: Callable[..., FlowStart]  # (ctx, values) -> FlowStart
    params: tuple[FlowParam, ...] = field(default_factory=tuple)
    label: str = "Connecter"
    # Chemin du retour de consentement. L'URL COMPLÈTE en est dérivée à la lecture
    # (`callback_url`), jamais écrite en dur : elle dépend de l'environnement, et une
    # URL de prose dans une doc ment dès qu'on la lit depuis la preprod.
    callback_path: str = ""
    # « Une app OAuth est-elle déjà à disposition de cet utilisateur ? » — la sienne,
    # celle de son org, ou celle de l'ÉDITEUR (oto). Sans cette réponse, le front ne
    # peut que promettre le pire cas : il demandait « pose d'abord les identifiants de
    # l'application », y compris à qui n'a plus rien à poser. `None` = le connecteur
    # ne déclare pas la question, le front ne promet alors rien.
    app_ready: Optional[Callable[[str], bool]] = None


_FLOWS: dict[str, Flow] = {}


def declare(connector: str, *, start: Callable[..., FlowStart],
            params: tuple[FlowParam, ...] = (), label: str = "Connecter",
            callback_path: str = "",
            app_ready: Optional[Callable[[str], bool]] = None) -> None:
    """Déclare le flux de connexion de ce connecteur. Appelé au niveau MODULE (comme
    `status_hints.register_state`) : c'est une déclaration pure, elle doit être lisible
    dès l'import, sans attendre le montage FastMCP."""
    for p in params:
        if not p.options and p.required and not p.default:
            # Un choix fermé sans options est indémarrable côté front : il rendrait un
            # select vide. Mieux vaut le refuser à la déclaration qu'au clic.
            raise ValueError(
                f"{connector}.{p.name} : paramètre requis sans options ni défaut.")
    _FLOWS[connector] = Flow(connector=connector, start=start,
                             params=tuple(params), label=label,
                             callback_path=callback_path, app_ready=app_ready)


def supports(connector: str) -> bool:
    return connector in _FLOWS


def entries() -> dict[str, Flow]:
    return dict(_FLOWS)


def describe(connector: str) -> Optional[dict]:
    """Le champ `connect` du catalogue : la FORME du geste, rien d'autre.

    `None` pour les ~56 connecteurs qui n'ont pas de flux — le front lit alors son
    formulaire de champs habituel, comme avant."""
    f = _FLOWS.get(connector)
    if f is None:
        return None
    return {"label": f.label, "params": [p.describe() for p in f.params]}


async def start(connector: str, ctx, values: dict) -> FlowStart:
    """Démarre le flux déclaré et rend la forme commune.

    Le type de retour est vérifié ICI, à l'unique point de passage : une annotation
    Python ne s'applique pas toute seule, et c'est précisément parce que la garantie
    ne vivait qu'en commentaire que deux flux ont pu diverger sans que rien ne
    proteste. Un flux qui rend autre chose casse au premier appel, pas au premier
    front qui s'y fie."""
    fabrique = _FLOWS[connector].start
    if inspect.iscoroutinefunction(fabrique):
        # Un flux peut être ASYNCHRONE — celui d'une messagerie hébergée interroge le
        # fournisseur avant de rendre son lien. Le serveur est mono-loop : ce chemin
        # réseau doit être attendu, jamais exécuté en bloquant.
        out = await fabrique(ctx, values or {})
    else:
        # Un flux SYNCHRONE n'est pas inoffensif pour autant : deux d'entre eux
        # enregistrent dynamiquement un client OAuth chez le fournisseur, en HTTP
        # bloquant (chemin froid, la première fois seulement). Appelés nûment depuis
        # cet `async def`, ils figeaient tout le processus le temps de la réponse
        # (oto-backend#867). Les traiter ICI vaut pour les cinq flux d'un coup —
        # aucun n'a besoin de le savoir, et le prochain non plus.
        out = await asyncio.to_thread(fabrique, ctx, values or {})
        if inspect.isawaitable(out):
            out = await out
    if not isinstance(out, FlowStart):
        raise TypeError(
            f"le flux « {connector} » doit rendre un FlowStart (reçu {type(out).__name__}) : "
            "la forme rendue à l'appelant est commune à tous les connecteurs, ce qui "
            "t'est propre va dans `details`.")
    return out


def callback_url(connector: str) -> Optional[str]:
    """URL de retour à enregistrer chez le fournisseur, DÉRIVÉE de l'environnement.

    Elle n'est PAS dans `describe()` : ce descripteur-là part dans `/api/connectors`,
    servie sans authentification. Celle-ci n'est ajoutée que sur la projection
    authentifiée — c'est une valeur que le client doit connaître pour configurer son
    app, pas une donnée de catalogue public.

    Dérivée, et c'est le point : jusqu'ici elle vivait en PROSE dans la doc du
    connecteur, avec le domaine de prod écrit à la main. Un utilisateur de preprod y
    lisait donc une URL que son backend n'utilise pas — et le consentement échouait sur
    un `redirect_uri_mismatch` incompréhensible."""
    f = _FLOWS.get(connector)
    if not f or not f.callback_path:
        return None
    from ..auth import flow as oauth_flow
    return oauth_flow.redirect_uri(f.callback_path)


def app_ready(connector: str, sub: str) -> Optional[bool]:
    """Cet utilisateur a-t-il déjà une app OAuth à disposition pour ce connecteur ?

    `None` = question non déclarée (ou hors service) : le front doit alors rester
    muet plutôt que d'affirmer. Comme `callback_url`, ça n'entre QUE dans la
    projection authentifiée — la réponse dépend de qui demande.

    Fail-open volontaire : une panne de lecture ne doit pas transformer un écran de
    connexion en écran d'erreur ; au pire l'utilisateur voit la consigne longue."""
    f = _FLOWS.get(connector)
    if not f or f.app_ready is None or not sub:
        return None
    try:
        return bool(f.app_ready(sub))
    except Exception:  # noqa: BLE001
        logger.debug("app_ready failed for %s", connector, exc_info=True)
        return None
