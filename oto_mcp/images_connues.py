"""Les images CONNUES : les adresses d'image que la plateforme a apprises AVANT qu'une
app ne soit ouverte, recopiées dans notre stockage média pour qu'une app les montre
sans jamais appeler un tiers (`docs/apps.md` §Images).

## Le problème

Une app tourne dans un cadre isolé, sans réseau (`connect-src 'none'`). Mais une image
est une requête : si le cadre peut charger n'importe quelle image (`img-src https:`),
le script de l'app compose `<img src="https://hôte-de-l-auteur/?d=<lignes>">` au moment
où le lecteur l'ouvre, et les données du lecteur partent chez l'auteur. Fermer
`img-src` sur notre seul stockage ferme la sortie, mais aucun logo ni photo LinkedIn ne
s'affiche plus.

## La règle

**Une image s'affiche si son adresse était connue AVANT la lecture**, jamais si elle a
été composée pendant. Connue = écrite côté serveur, à un moment qui ne dépend d'aucun
lecteur :

- dans le HTML ou le manifeste ENREGISTRÉ d'une app (`db/apps.py`, `db/app_versions.py`
  — création, mise à jour, patch, restauration, publication, brouillon) ;
- dans une ligne de tableau écrite côté serveur (agent, recette, connecteur, API,
  import) — **jamais par la session d'un lecteur** (`console`, d'où écrit l'écran hôte
  d'une page) ni par un appel d'app (`geste_d_app`), sans quoi la page écrirait une
  ligne puis l'afficherait.

Chaque adresse connue entre au registre (`images_connues`, empreinte SHA-256 de la
chaîne EXACTE) ; une boucle de fond (`images_connues_worker.py`) la télécharge UNE fois,
sous la garde d'egress de `web_image`, et la dépose, publique, sous
`app-images/<empreinte>` dans le stockage média. Le front réécrit chaque adresse d'image
externe de la page vers cette clé, qu'il calcule lui-même (même empreinte) — ce qui
n'a jamais été copié répond 404, et `img-src` revient au seul stockage.

⚠️ **Rien n'est jamais téléchargé À CAUSE d'une lecture** : ni copie à la demande, ni
rafraîchissement. Sinon l'auteur enregistre mille adresses et la page CHOISIT, à la
lecture, lesquelles demander : le choix porterait les données.

⚠️ **Une valeur déjà présente dans la ligne n'est pas « apprise » par l'écriture
suivante** (`urls_des_lignes` ne retient que ce qui n'y était pas) : une valeur posée par
un geste d'app, puis recopiée par une formule ou ré-écrite telle quelle par un agent qui
modifie une autre colonne, ne devient pas connue pour autant.

Ce module est PUR (aucune base, aucun réseau) : extraire, empreinter, dire si le geste
en cours a le droit d'apprendre. L'écriture au registre vit dans
`db/images_connues.py`.
"""
from __future__ import annotations

import contextvars
import hashlib
import html as _html
import re
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlsplit

from . import geste

#: Le préfixe des copies dans le stockage média (`media_store.PREFIXES_PUBLICS`).
PREFIXE = "app-images"
#: Une adresse plus longue n'est pas retenue (une adresse d'image signée tient en
#: quelques centaines de caractères ; au-delà, c'est une donnée, pas une adresse).
MAX_URL = 2048
#: Au plus ce nombre d'adresses apprises par écriture d'app, et par ligne écrite.
MAX_PAR_APP = 200
MAX_PAR_LIGNE = 20

# Une adresse absolue dans un texte : HTML, CSS, JavaScript ou JSON. Elle s'arrête aux
# délimiteurs de ces quatre langages (guillemets, chevrons, parenthèses, espace,
# accolades et accent grave d'un gabarit JS, barre oblique inverse d'un échappement).
_URL_DANS_TEXTE = re.compile(r"""https?://[^\s"'<>()\\`{}]+""", re.IGNORECASE)
# Là où un navigateur charge une image : l'attribut (`src`, `srcset`, `poster`, le
# `href` d'une `<image>` SVG) et sa valeur entre guillemets doubles, simples, ou nue.
_ATTRIBUT_D_IMAGE = re.compile(
    r"""(?:<image\b[^>]*?\s(?:xlink:)?(href)|\s(src|srcset|poster))\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""",
    re.IGNORECASE)
_URL_CSS = re.compile(r"""url\(\s*(['"]?)(https?://[^'")\s]+)\1\s*\)""", re.IGNORECASE)
# Ce qu'aucune image n'est : script, feuille de style, police — servis par les CDN que
# la CSP ouvre déjà, et qu'il serait vain de télécharger.
_PAS_UNE_IMAGE = (".js", ".mjs", ".css", ".map", ".woff", ".woff2", ".ttf", ".otf", ".eot")

# Une valeur de ligne n'est retenue que si elle a l'AIR d'une image : par le nom de sa
# colonne, l'extension de son chemin, ou l'hôte d'un service d'images connu. Sans ce
# filtre, chaque site web, chaque profil, chaque lien de suivi d'un tableau serait
# téléchargé.
_NOM_D_IMAGE = re.compile(
    r"image|img|photo|picture|pic|avatar|logo|thumb|icon|banner|cover|visuel|vignette",
    re.IGNORECASE)
_EXTENSIONS_D_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_HOTES_D_IMAGES = ("media.licdn.com", "static.licdn.com", "googleusercontent.com",
                   "img.logo.dev", "logo.clearbit.com", "gravatar.com", "pbs.twimg.com",
                   "fbcdn.net", "cdninstagram.com", "images.unsplash.com", "unavatar.io")

# Les sources d'écriture de ligne qui APPRENNENT une adresse (`geste.SOURCES`). Une
# liste qui autorise, jamais une liste qui exclut : une source neuve n'apprend rien tant
# qu'on ne l'a pas décidé.
# - `console` n'y est PAS : c'est la session du lecteur, et l'écran hôte d'une page écrit
#   par elle hors de `me.app.call` — une page dans un document écrit par les routes de
#   tableau, l'annulation d'un geste d'app aussi. Une page n'agit que par la session de
#   son lecteur : refuser la console ferme tous ces chemins, ceux de demain compris. Le
#   prix : une adresse tapée à la main dans un tableau n'est pas apprise.
# - `system` n'y est pas : la file de travail, le recalcul des formules, la maintenance
#   écrivent ce qu'ils DÉRIVENT de la ligne — sauf le travail de fond qui rejoue un outil
#   nommé (`jev_rows` en tâche de fond), qui écrit ce qu'il a trouvé dehors.
_SOURCES_QUI_APPRENNENT = frozenset({geste.AGENT, geste.API, geste.UPLOAD, geste.IMPORT})

_GESTE_D_APP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oto_images_geste_d_app", default=False)


def empreinte(url: str) -> str:
    """L'empreinte d'une adresse : SHA-256 hexadécimal de la chaîne EXACTE (UTF-8),
    sans normalisation — le front calcule la même sur la valeur qu'il lit
    (`getAttribute('src')`, la valeur de la ligne), et deux écritures d'une même adresse
    sous deux formes sont deux adresses."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cle(url: str) -> str:
    """La clé de la copie dans le stockage média."""
    return f"{PREFIXE}/{empreinte(url)}"


def _recevable(url: str) -> bool:
    if not url or len(url) > MAX_URL or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def urls_du_texte(*textes: Optional[str], limite: int = MAX_PAR_APP) -> list[str]:
    """Les adresses d'IMAGE d'un HTML (et de son manifeste) enregistré, dans l'ordre,
    sans doublon, bornées à `limite`.

    Prises telles quelles là où le navigateur charge une image : `src`, `srcset`,
    `poster`, `href` d'une `<image>` SVG, `url(…)` du CSS (attribut `style`, `<style>`,
    chaîne de script). Ailleurs — une chaîne de script, un lien, le manifeste —
    seulement si l'adresse a l'air d'une image (extension, hôte d'images) : chaque
    `<a href>` d'une app ne doit pas devenir un téléchargement. Les entités des
    attributs sont décodées (`&amp;` → `&`), comme le navigateur les décode : c'est la
    valeur décodée que le front lira et empreintera ; la forme brute est retenue aussi
    quand elle diffère (une chaîne JavaScript n'est pas décodée)."""
    vues: dict[str, None] = {}

    def garder(u: str) -> bool:
        for v in dict.fromkeys((_html.unescape(u).strip(), u.strip())):
            if len(vues) >= limite:
                return False
            if _recevable(v) and not urlsplit(v).path.lower().endswith(_PAS_UNE_IMAGE):
                vues.setdefault(v, None)
        return True

    for texte in textes:
        if not texte:
            continue
        for m in _ATTRIBUT_D_IMAGE.finditer(texte):
            nom = (m.group(1) or m.group(2) or "").lower()
            valeur = m.group(3) or m.group(4) or m.group(5) or ""
            morceaux = valeur.split(",") if nom == "srcset" else [valeur]
            for morceau in morceaux:
                u = (morceau.split() or [""])[0]
                if u.lower().startswith(("http://", "https://")) and not garder(u):
                    return list(vues)
        # Dans un attribut `style`, les guillemets d'un `url()` sont souvent des entités.
        for m in (*_URL_CSS.finditer(texte), *_URL_CSS.finditer(_html.unescape(texte))):
            if not garder(m.group(2)):
                return list(vues)
        for m in _URL_DANS_TEXTE.finditer(texte):
            u = m.group(0)
            if a_l_air_d_une_image("", _html.unescape(u)) and not garder(u):
                return list(vues)
    return list(vues)


def a_l_air_d_une_image(cle_de_colonne: str, url: str) -> bool:
    """Une valeur de ligne qui ressemble à une image : nom de colonne, extension, hôte."""
    if _NOM_D_IMAGE.search(cle_de_colonne or ""):
        return True
    parts = urlsplit(url)
    if parts.path.lower().endswith(_EXTENSIONS_D_IMAGE):
        return True
    hote = (parts.hostname or "").lower()
    return any(hote == h or hote.endswith("." + h) for h in _HOTES_D_IMAGES)


def _feuilles(valeur: Any, cle_: str = "") -> Iterator[tuple[str, str]]:
    """Les chaînes d'une valeur de ligne, avec la clé qui les porte (la colonne, ou le
    sous-champ d'un objet ; une liste garde la clé de sa colonne). Une cellule en
    COUCHES (`{"valeur": …, "origine": …, "comment": …}`, `datastore/couches.py`) ne
    rend que sa valeur, sous le nom de sa colonne : la provenance n'est pas une image."""
    from .datastore.couches import unwrap
    if isinstance(valeur, dict):
        deballee = unwrap(valeur)
        if deballee is not valeur:
            yield from _feuilles(deballee, cle_)
            return
    if isinstance(valeur, str):
        yield cle_, valeur
    elif isinstance(valeur, dict):
        for k, v in valeur.items():
            yield from _feuilles(v, str(k))
    elif isinstance(valeur, (list, tuple)):
        for v in valeur:
            yield from _feuilles(v, cle_)


def urls_des_lignes(avant: Optional[dict], apres: Optional[dict],
                    limite: int = MAX_PAR_LIGNE) -> list[str]:
    """Les adresses d'image qu'une écriture de ligne APPREND : celles de `apres` qui ont
    l'air d'une image et qu'aucune chaîne de `avant` ne portait déjà.

    Ce qui était dans la ligne avant l'écriture n'est pas appris par elle — une valeur
    posée par un geste d'app, recopiée ensuite par une formule ou réécrite telle quelle
    autour d'une autre colonne, reste inconnue."""
    deja = {v for _, v in _feuilles(avant or {})}
    vues: dict[str, None] = {}
    for k, v in _feuilles(apres or {}):
        u = v.strip()
        if u in deja or v in deja or not _recevable(u) or not a_l_air_d_une_image(k, u):
            continue
        vues.setdefault(u, None)
        if len(vues) >= limite:
            break
    return list(vues)


@contextmanager
def geste_d_app() -> Iterator[None]:
    """Le temps d'un appel d'app ouverte (`capabilities/apps_appel.py`) : aucune
    écriture de ligne n'apprend d'adresse. Une `ContextVar`, comme le geste : posée à
    l'entrée, remise à la sortie ; elle suit `asyncio.to_thread`."""
    jeton = _GESTE_D_APP.set(True)
    try:
        yield
    finally:
        _GESTE_D_APP.reset(jeton)


def ecriture_apprend() -> bool:
    """L'écriture de ligne EN COURS apprend-elle des adresses ? Non pendant un geste
    d'app, non sans geste posé, non pour une source hors liste ; `system` seulement
    quand il rejoue un outil nommé."""
    if _GESTE_D_APP.get():
        return False
    g = geste.courant()
    if g is None:
        return False
    if g.source in _SOURCES_QUI_APPRENNENT:
        return True
    return g.source == geste.SYSTEM and bool(g.outil)


def retenir_avec(conn, urls: Iterable[str], origine: str) -> None:
    """Raccourci des points d'écriture : pose `urls` au registre dans la transaction de
    `conn`, sous un point de sauvegarde — l'échec du registre ne défait jamais
    l'écriture qui l'a appris (`db/images_connues.retenir`)."""
    urls = list(urls)
    if not urls:
        return
    from .db import images_connues as reg
    reg.retenir(conn, urls, origine)
