"""Garde d'egress des connecteurs — une destination de credential ne vise pas l'intérieur.

Sept connecteurs prennent leur hôte dans le credential : `http` (`base_url` et
`token_url`), `n8n`, `make`, `github` Enterprise, `lighton`, `posthog`,
`salesforce`. Cette valeur est déclarée par un **administrateur d'organisation
cliente**, et le process qui l'appelle tourne en root, sans conteneur : sa boucle
locale porte l'administration du proxy web, le pilotage du navigateur et le reste
des services de la machine. Sans garde, une `base_url` suffit à faire parler le
serveur à lui-même.

Le filtrage réseau du service ne couvre qu'**une** plage (le lien-local, donc les
métadonnées d'infrastructure) ; les plages privées et la boucle locale restaient
joignables. La garde vit donc dans le code, ici.

⚠️ **Elle contredit l'ADR 0037 §4**, qui décidait « zéro code SSRF dans le
connecteur » et renvoyait le blocage des plages privées à un egress proxy
« plus tard… si le besoin naît ». Ce proxy n'a jamais existé, et l'argument qui
justifiait l'absence de garde reposait sur lui. La décision a été reprise ;
l'ADR est à amender.

**Le refus se fait à la RÉSOLUTION, pas sur la forme du texte.** Un nom de
domaine parfaitement public peut pointer vers l'intérieur, et une adresse peut
s'écrire autrement qu'en quatre octets pointés (`127.1`, `2130706433`,
`[::ffff:127.0.0.1]`). Toutes ces formes passent un contrôle textuel ; aucune ne
passe `getaddrinfo`. Fail-closed sur l'ENSEMBLE des adresses résolues : un hôte
qui résout à la fois public et interne est refusé — c'est le montage type du
contournement.

**Les exceptions sont NOMMÉES et déclarées**, jamais devinées : une destination
interne légitime (un pont hébergé en boucle locale, un service sur le réseau
privé) doit figurer dans la variable d'environnement `OTO_EGRESS_ALLOW`, au
format `nom=adresse:port`, séparé par des virgules ou des retours à la ligne. La
clé est une **adresse littérale et un port** — jamais un nom d'hôte : on autorise
une destination, pas une étiquette qui peut se déplacer sous nous ; et jamais une
adresse seule, sinon une exception pour un pont ouvrirait toute la machine.
Le défaut est **vide** : ce fichier est public, les destinations réelles se
déclarent au déploiement. Une destination absente de la liste échoue avec un
message qui nomme la variable et le format — jamais en silence.

⚠️ **Limite assumée.** Entre le contrôle et la connexion, le client HTTP résout
une seconde fois : un DNS hostile qui répond « public » puis « interne » (DNS
rebinding) n'est pas couvert, et l'ADR 0037 avait raison sur ce point précis.
Une redirection 3xx vers l'intérieur ne l'est pas non plus : la garde tient au
moment où le backend construit le client, pas dans le transport d'oto-core.
Fermer ces deux fenêtres demande d'épingler l'adresse résolue dans le transport,
côté lib — autre lot, et un bump de pin.

Deux gardes plus anciennes et plus étroites font un travail voisin sans être
fusionnées ici : `tools/web.py:check_url_public` et
`file_source._assert_public_host`. Elles appliquent une politique DIFFÉRENTE —
URL fournie par l'agent, aucune exception possible, tout ce qui n'est pas public
est refusé. Ici l'URL vient d'un administrateur d'organisation et des exceptions
existent. Les rapprocher supposerait de trancher ces deux politiques ; ce n'est
pas ce lot.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

#: Variable d'environnement qui porte les exceptions nommées.
ALLOW_VAR = "OTO_EGRESS_ALLOW"


class EgressRefused(ValueError):
    """Une destination de credential vise une adresse interne non déclarée.

    Hérite de `ValueError` : les adaptateurs de connecteurs traduisent déjà un
    `ValueError` de config en refus actionnable pour l'appelant."""


# ── ce qui est « interne » ────────────────────────────────────────────────────

def _unwrap(ip):
    """Déballe les encapsulations v4-dans-v6 (`::ffff:127.0.0.1`, 6to4, Teredo).

    ⚠️ **Ne change pas la DÉCISION** — mesuré : `is_global` est déjà faux pour
    ces cinq formes, elles sont refusées avec ou sans ce déballage. Ce qu'il
    change est la RAISON servie : sans lui, un 6to4 qui joint la boucle locale
    (`2002:7f00:0001::`) est annoncé « réseau privé », et l'opérateur cherche la
    mauvaise chose. Le garder pour la précision du refus, pas croire qu'il ferme
    une porte."""
    if isinstance(ip, ipaddress.IPv6Address):
        for encapsule in (ip.ipv4_mapped, ip.sixtofour):
            if encapsule is not None:
                return encapsule
        if ip.teredo is not None:
            return ip.teredo[1]
    return ip


def internal_reason(adresse: str) -> str | None:
    """Pourquoi cette adresse est interne, en clair — ou `None` si elle est publique.

    La décision est le OU de TOUTES les branches ci-dessous, `not is_global`
    compris en dernier ressort : une plage spéciale qu'aucune branche ne nomme est
    refusée quand même, avec une raison générique. L'ordre ne sert qu'à choisir le
    mot juste — un refus qui ne nomme pas sa raison se fait contourner au lieu
    d'être corrigé.

    ⚠️ **Ne pas réduire ce corps à `not is_global`.** La box et la CI tournent
    sous Python 3.10, le banc sous 3.13, et les prédicats d'`ipaddress` ont bougé
    entre ces versions sur les formes encapsulées. Vérification faite sur les 20
    adresses du banc, **les deux versions décident et motivent identiquement** —
    donc rien n'est corrigé ici, c'est une précaution : la décision ne repose pas
    sur un prédicat unique dont la définition a déjà changé une fois."""
    ip = _unwrap(ipaddress.ip_address(adresse))
    if ip.is_unspecified:
        return "adresse non spécifiée — elle désigne la machine elle-même"
    if ip.is_loopback:
        return "boucle locale de la machine"
    if ip.is_link_local:
        return "lien-local — c'est la plage des métadonnées d'infrastructure"
    if ip.is_private:
        return "réseau privé (RFC 1918 en IPv4, ULA en IPv6)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "plage réservée"
    if not ip.is_global:
        return "adresse non publique (plage à usage spécial)"
    return None


# ── les exceptions nommées ────────────────────────────────────────────────────

def declared_exceptions(brut: str | None = None) -> dict[tuple[str, int], str]:
    """Les exceptions déclarées, `(adresse, port) → nom`.

    Lit `OTO_EGRESS_ALLOW` quand `brut` est absent. Une entrée illisible LÈVE :
    une liste d'exceptions qu'on ne sait pas lire ne s'interprète pas « comme
    vide », elle se signale. Cette lecture n'a lieu qu'au moment de refuser, donc
    une faute de frappe dans la variable n'atteint que les destinations internes
    — jamais le trafic public."""
    if brut is None:
        brut = os.environ.get(ALLOW_VAR, "")
    exceptions: dict[tuple[str, int], str] = {}
    for entree in brut.replace("\n", ",").split(","):
        entree = entree.strip()
        if not entree:
            continue
        nom, _, destination = entree.partition("=")
        nom, destination = nom.strip(), destination.strip()
        if not nom or not destination:
            raise ValueError(
                f"{ALLOW_VAR} : entrée illisible {entree!r} — format attendu "
                "« nom=adresse:port », p. ex. « pont-local=127.0.0.1:9000 ».")
        adresse, _, port = destination.rpartition(":")
        adresse = adresse.strip().strip("[]")
        try:
            ip = ipaddress.ip_address(adresse)
            numero = int(port)
        except ValueError:
            raise ValueError(
                f"{ALLOW_VAR} : destination illisible {destination!r} pour "
                f"« {nom} » — une exception se déclare par une ADRESSE littérale "
                "ET un port (« 127.0.0.1:9000 ») : jamais par un nom d'hôte, qui "
                "peut se déplacer sous nous, et jamais par une adresse seule, qui "
                "ouvrirait toute la machine.") from None
        exceptions[(str(ip), numero)] = nom
    return exceptions


# ── le contrôle ───────────────────────────────────────────────────────────────

def resolved_addresses(hote: str, port: int) -> set[str]:
    """Toutes les adresses vers lesquelles cet hôte résout, sans scope IPv6.

    Fonction publique et séparée : c'est le point que les épreuves remplacent
    pour éprouver la garde sans dépendre du DNS de la machine qui les joue."""
    infos = socket.getaddrinfo(hote, port, proto=socket.IPPROTO_TCP)
    return {info[4][0].split("%", 1)[0] for info in infos}


def check_url(url: str, *, connector: str, field: str = "base_url") -> None:
    """Refuse `url` si son hôte résout vers une adresse interne non déclarée.

    Ne rend rien quand la destination est publique ou déclarée. `connector` et
    `field` ne servent qu'au message : un opérateur doit savoir QUELLE carte
    corriger, pas seulement que « quelque chose » a été refusé."""
    morceaux = urlsplit((url or "").strip())
    if morceaux.scheme not in ("http", "https"):
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` doit être une URL http(s) — "
            f"schéma `{morceaux.scheme or '∅'}` refusé.")
    hote = morceaux.hostname
    if not hote:
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` est une URL sans hôte.")
    port = morceaux.port or (443 if morceaux.scheme == "https" else 80)

    try:
        adresses = resolved_addresses(hote, port)
    except OSError as e:
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` — l'hôte `{hote}` ne résout "
            f"pas ({e}). La destination ne peut pas être contrôlée, donc elle "
            "n'est pas appelée.") from None

    for adresse in sorted(adresses):
        raison = internal_reason(adresse)
        if raison is None:
            continue
        if declared_exceptions().get((adresse, port)) is not None:
            continue
        # Le déguisement : dire les DEUX. Un refus qui ne montre que l'adresse
        # résolue paraît absurde à qui a saisi un nom de domaine public, et un
        # refus qui ne montre que le nom saisi ne dit pas ce qui cloche.
        vu = (f"`{hote}`" if hote == adresse
              else f"`{hote}`, qui résout vers {adresse},")
        raise EgressRefused(
            f"connecteur `{connector}` : destination refusée. {vu} est une "
            f"adresse interne — {raison}. Un connecteur ne sort pas vers le "
            "réseau interne de la plateforme : cette adresse porte des services "
            "d'administration qu'un credential d'organisation ne doit pas "
            "pouvoir atteindre.\n"
            f"Si c'est une destination légitime (un pont hébergé), elle doit "
            f"être déclarée comme exception NOMMÉE dans la variable "
            f"{ALLOW_VAR} du service, au format « nom=adresse:port » — ici "
            f"« mon-pont={adresse}:{port} ». Tant qu'elle n'y figure pas "
            f"l'appel est refusé : la liste ne se devine pas, elle se déclare. "
            f"(Champ `{field}` de la carte {connector}.)")
