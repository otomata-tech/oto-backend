"""L'empreinte du CLIENT d'une requête — son IP réelle et son user-agent.

Deux faits de TRANSPORT dont un handler de capacité a parfois besoin, alors qu'il
ne voit jamais la requête (ADR 0004, sens unique : le core n'importe pas
l'adaptateur). Même patron que `session_org._CALL_ORG` : l'adaptateur REST pose la
valeur autour de l'appel, le core la lit — ici `capabilities/me_legal`, qui SITUE
une acceptation de document légal.

Pourquoi une acceptation a besoin de ça : un consentement n'est opposable que daté
**et** situé. « Il a accepté » ne prouve rien tout seul ; « telle version, à telle
date, depuis telle adresse, avec tel navigateur » se défend.

L'adresse ne se lit dans un en-tête que derrière un proxy déclaré de confiance
(`OTO_PROXIES_DE_CONFIANCE`, cf. `pick_ip`) ; sans ce réglage, c'est le pair TCP.

Hors requête REST (MCP, boot, runner, tests) la trace vaut `None` de bout en bout.
Une trace absente reste absente : rien n'y est deviné, et surtout pas l'IP du
serveur lui-même.
"""
from __future__ import annotations

import contextvars
import ipaddress
import logging
import os
from typing import Optional

# Longueur maximale conservée d'un user-agent. L'en-tête est écrit par le client,
# donc de taille non bornée : sans écrêtage, une ligne de consentement peut porter
# un mégaoctet de chaîne arbitraire. 512 caractères couvrent tous les user-agents de
# navigateur réels avec de la marge — on écrête, on ne refuse pas : la trace vaut
# mieux tronquée qu'absente.
MAX_USER_AGENT = 512

logger = logging.getLogger(__name__)

_CLIENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "oto_client_trace", default=None)

VIDE = {"ip": None, "user_agent": None, "origin": None, "fetch_dest": None}

# Ce qu'un en-tête `Origin` ou `Sec-Fetch-Dest` porte au plus : une origine est un
# schéma, un hôte et un port. Au-delà, ce n'est pas une origine (et on ne la garde pas).
MAX_ORIGIN = 256


#: Les réseaux des proxies dont on croit les en-têtes d'adresse. Lus UNE fois, au
#: démarrage (`verifier_configuration`) ; `None` = pas encore lus.
_RESEAUX: Optional[tuple] = None

#: Le pair en boucle locale sans proxy déclaré a-t-il déjà été signalé ? Une fois par
#: processus : c'est un défaut de déploiement, pas un événement par requête.
_BOUCLE_SIGNALEE = False


class ProxiesDeConfianceInvalides(RuntimeError):
    """`OTO_PROXIES_DE_CONFIANCE` illisible : levé au démarrage. Une entrée qu'on ne
    comprend pas n'est ni ignorée ni remplacée par un défaut deviné — l'instance
    croirait (ou ne croirait plus) des en-têtes sans que personne l'ait décidé."""


def proxies_de_confiance_depuis_env() -> tuple:
    """Les réseaux de `OTO_PROXIES_DE_CONFIANCE` (adresses ou CIDR séparés par des
    virgules). Absente ou vide : aucun proxy de confiance — on lit le pair TCP et on
    ignore les en-têtes. Une entrée illisible LÈVE."""
    reseaux = []
    for brut in (os.environ.get("OTO_PROXIES_DE_CONFIANCE") or "").split(","):
        brut = brut.strip()
        if not brut:
            continue
        try:
            reseaux.append(ipaddress.ip_network(brut, strict=False))
        except ValueError:
            raise ProxiesDeConfianceInvalides(
                f"OTO_PROXIES_DE_CONFIANCE : {brut!r} n'est ni une adresse ni un "
                "CIDR.") from None
    return tuple(reseaux)


def verifier_configuration() -> None:
    """Au démarrage : lit le réglage une fois pour toutes, et refuse de démarrer sur
    un réglage illisible en le nommant."""
    global _RESEAUX
    _RESEAUX = proxies_de_confiance_depuis_env()


def _reseaux() -> tuple:
    global _RESEAUX
    if _RESEAUX is None:
        _RESEAUX = proxies_de_confiance_depuis_env()
    return _RESEAUX


def _de_confiance(adresse: str, reseaux: tuple) -> bool:
    """`adresse` appartient-elle à un réseau de confiance ? Une valeur qui n'est pas
    une adresse (`testclient`, un saut d'en-tête mal formé) ne l'est jamais."""
    try:
        ip = ipaddress.ip_address(adresse)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped          # `::ffff:127.0.0.1` d'une écoute double pile
    return any(ip in r for r in reseaux)


def _signaler_boucle_sans_reglage(pair: str) -> None:
    """Un pair en boucle locale alors qu'aucun proxy n'est déclaré : le processus est
    presque sûrement derrière un proxy de la box, et TOUTES ses requêtes portent alors
    la même adresse — un seul seau pour chaque borne par adresse (relais
    d'autorisation, limite de débit, projets publiés). Le comportement reste le sûr
    (le pair TCP) ; on le dit fort, une fois."""
    global _BOUCLE_SIGNALEE
    if _BOUCLE_SIGNALEE:
        return
    try:
        boucle = ipaddress.ip_address(pair).is_loopback
    except ValueError:
        return
    if boucle:
        _BOUCLE_SIGNALEE = True
        logger.error(
            "OTO_PROXIES_DE_CONFIANCE est vide et le pair TCP est en boucle locale : "
            "toutes les requêtes partagent l'adresse du proxy, donc un seul seau par "
            "borne d'adresse. Déclarer le proxy (ex. 127.0.0.1/32,::1/128).")


def pick_ip(cf_connecting_ip: Optional[str], x_forwarded_for: Optional[str],
            peer: Optional[str]) -> Optional[str]:
    """L'IP réelle du client — la RÈGLE UNIQUE du dépôt (#1232).

    Un en-tête d'adresse est écrit par qui veut : le croire sans condition laisse un
    client se choisir son adresse (échapper à une borne par adresse, ou en accuser
    une autre). On ne le lit donc que s'il vient d'un proxy qu'on a DÉCLARÉ
    (`OTO_PROXIES_DE_CONFIANCE`) :

    - pair TCP hors des réseaux de confiance (ou réglage absent) : le pair TCP, les
      en-têtes sont ignorés ;
    - pair de confiance : `CF-Connecting-IP`, sinon l'adresse la plus à DROITE de
      `X-Forwarded-For` qui n'est pas elle-même de confiance — la liste se lit
      client → relais, chaque relais AJOUTANT en queue : ce qui est à gauche du
      dernier relais de confiance a été écrit par le client ; si toute la liste est
      de confiance, son premier saut ; sans en-tête, le pair TCP.

    Appelée par `ip_de_requete` (REST, relais d'autorisation) et
    `subdomain_project._client_ip` (en-têtes ASGI en octets ; seau des projets publiés
    et `limite_debit`). Deux implémentations divergeraient à la première évolution de
    la chaîne de relais, et la divergence se verrait sur une trace de consentement,
    c'est-à-dire trop tard.
    """
    pair = (peer or "").strip() or None
    reseaux = _reseaux()
    if pair is None or not _de_confiance(pair, reseaux):
        if not reseaux and pair is not None:
            _signaler_boucle_sans_reglage(pair)
        return pair
    if cf_connecting_ip and cf_connecting_ip.strip():
        return cf_connecting_ip.strip()
    sauts = [s.strip() for s in (x_forwarded_for or "").split(",") if s.strip()]
    for saut in reversed(sauts):
        if not _de_confiance(saut, reseaux):
            return saut
    return sauts[0] if sauts else pair


def ip_de_requete(request) -> Optional[str]:
    """`pick_ip` d'une requête Starlette (en-têtes insensibles à la casse)."""
    return pick_ip(request.headers.get("cf-connecting-ip"),
                   request.headers.get("x-forwarded-for"),
                   request.client.host if request.client else None)


def set_current(*, ip: Optional[str], user_agent: Optional[str],
                origin: Optional[str] = None,
                fetch_dest: Optional[str] = None) -> contextvars.Token:
    """Pose l'empreinte du client pour la durée de l'appel courant.

    `origin` (en-tête `Origin`) et `fetch_dest` (`Sec-Fetch-Dest`) : d'OÙ, dans le
    navigateur, la requête est partie — posés par le navigateur, pas par la page. Une
    seule capacité les lit aujourd'hui : l'émission d'un consentement de geste d'app
    (`capabilities/apps_appel`), qui n'est servie qu'à l'écran hôte, jamais à une app
    dans son cadre isolé (origine `null`). Un en-tête trop long n'est pas une origine :
    il vaut `None`, que la capacité refuse."""
    origin = (origin or "").strip()
    return _CLIENT.set({
        "ip": ip or None,
        "user_agent": (user_agent or "")[:MAX_USER_AGENT] or None,
        "origin": origin if 0 < len(origin) <= MAX_ORIGIN else None,
        "fetch_dest": (fetch_dest or "").strip().lower()[:32] or None,
    })


def reset(token: contextvars.Token) -> None:
    _CLIENT.reset(token)


def current() -> dict:
    """`{"ip", "user_agent", "origin", "fetch_dest"}` de la requête courante — tous à
    `None` hors requête REST. Rend toujours un dict : un appelant qui écrit une trace n'a pas à savoir
    par quelle surface il est arrivé."""
    return _CLIENT.get() or dict(VIDE)
