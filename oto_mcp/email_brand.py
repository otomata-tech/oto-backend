"""Le DESSIN d'un email — la marque sous laquelle il part, et le gabarit qui l'habille.

`email.py` porte le TRANSPORT, `email_templates.py` le TEXTE ; ce module porte ce
que le destinataire VOIT. Trois choses le justifient, et aucune n'est cosmétique.

**1. La marque n'était qu'un mot.** Depuis 7d10a798 les six gabarits écrivent le
nom du produit du destinataire (`orgs.front_brand` / `config.front_for`) — mais la
couleur, elle, restait celle d'oto pour tout le monde. Un client du partenaire lisait
« sur <sa marque> » en brun otomata, puis cliquait vers une application blanc-et-ardoise :
le mot suivait le tenant, le dessin non. Une marque est ici un **jeu de jetons**
(`Marque`), indexé par le MÊME slug que le texte, et il n'y a plus d'endroit où
l'un puisse suivre le tenant sans l'autre.

**2. Un slug inconnu ne doit rien casser.** `front_brand` est une colonne, pas une
énum : un tenant déclaré demain y écrira son slug avant que ce fichier le connaisse.
`marque()` rend alors le gabarit NEUTRE **portant son nom** — jamais une couleur
devinée, jamais le nom d'un autre produit. C'est la même règle que `links.py` : on
préfère l'absence d'affirmation à une affirmation fausse.

**3. Un email n'est pas une page web.** Le gabarit d'avant était un `<div>` avec un
`max-width` — Outlook ignore les deux, et l'email s'y étalait sur toute la fenêtre.
D'où ce qui suit, qui n'est pas un goût mais une contrainte de client :

- **tables imbriquées** pour la colonne et pour le bouton (Outlook/Word ne met en
  page que ça) ;
- **styles en ligne uniquement** — pas de `<style>` (Gmail le garde, mais pas dans
  l'aperçu ni chez tous les webmails), pas de variables CSS, pas de flex ni de grid ;
- **police système** : une webfont ne se charge pas dans la plupart des clients, donc
  `Inter` n'est qu'un premier choix devant une vraie pile de replis ;
- **preheader caché** : sans lui, la ligne d'aperçu de la boîte de réception affiche
  le premier texte venu (« ou collez ce lien »). C'est la deuxième chose qu'on lit
  d'un email, avant même de l'ouvrir ;
- **`color-scheme: light` déclaré** : les couleurs ci-dessous sont des valeurs claires
  littérales ; sans la déclaration, Outlook mobile et Apple Mail repeignent le fond en
  sombre et laissent le texte en place — c'est exactement l'accident qu'a connu la
  page de connexion Logto du partenaire (cf. `logto/custom.css` côté front).

**4. La charte du tenant primaire se DÉCLARE (15/11/2026).** Ses sept teintes, le rayon
du bouton et un logo viennent de `OTO_BRAND_CHARTE` quand l'instance la pose
(`_charte_declaree`) ; sans elle, le rendu du code, inchangé. Posée mais illisible,
elle LÈVE (`CharteInvalide`) et l'instance refuse de démarrer (`verifier_reglages`). Un logo déclaré prend la
place du nom en tête, au-dessus de la carte, avec le nom en `alt`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

# ⚠️ Import de MODULE, jamais `from .email import _esc` — même règle, et pour la même
# raison, que l'en-tête d'`email_templates.py` : un nom importé à plat serait une
# copie figée que `monkeypatch.setattr(email, …)` ne toucherait jamais. C'est aussi
# ce qui rend le cycle inoffensif dans les deux sens : `email.py` importe ce module
# de la même façon (`from . import email_brand as _charte`) et personne ne dépend,
# au moment de l'import, d'un attribut de l'autre — seulement à l'appel.
from . import email as _email
from .config import require_env
from .tenancy import primary_slug

logger = logging.getLogger(__name__)

# Une webfont ne se charge pas dans un client mail : `Inter` est le premier choix
# (il s'affiche chez qui l'a installée, et sur les clients web des deux produits),
# les suivantes sont ce que le reste du monde a réellement.
POLICE = ("Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
          "Helvetica,Arial,sans-serif")

# Largeur de la colonne. 600px est le plancher historique des clients de bureau ;
# le `max-width` la réduit sur mobile, l'attribut `width` sert Outlook qui l'ignore.
LARGEUR = 600
# Gouttière de 32px de chaque côté ⟹ ce qu'il reste pour une image pleine largeur.
LARGEUR_UTILE = LARGEUR - 64


@dataclass(frozen=True)
class Marque:
    """Ce qu'il faut savoir d'un produit pour lui écrire un email à SA tête.

    Les couleurs sont des littéraux clairs : aucun jeton, aucune variable — voir
    l'en-tête du module. `nom` est ce qui s'imprime (le slug est un identifiant, pas
    un mot de marque) ; `site` s'affiche en pied et n'est jamais un lien cliquable —
    un lien de pied vers le produit ferait doublon avec le bouton et donne un signal
    de spam de plus pour rien.
    """
    slug: str
    nom: str
    site: str
    fond: str        # derrière la carte
    surface: str     # la carte
    encre: str       # le texte
    discret: str     # le texte secondaire (dates, adresses, pied)
    filet: str       # bordure de carte et séparateur
    bouton_fond: str
    bouton_encre: str
    # Déclarés par le tenant (`tenants.brand`), vides sinon. `expediteur` = l'en-tête
    # From (`Nom <adresse@domaine>`) — vide ⟹ `OTO_MAIL_FROM`, celui de l'instance.
    # ⚠️ Son domaine doit figurer dans `MAILER_FROM_DOMAINS` du mailer, sinon 403 :
    # rien ne part, et `email._send` le journalise en erreur et le signale. `langue` = la langue d'un destinataire sans
    # `users.locale` (un invité sans compte) — vide ⟹ français, comme avant.
    expediteur: str = ""
    langue: str = ""
    # Déclarés par l'instance pour son tenant PRIMAIRE (`OTO_BRAND_CHARTE`), sinon le
    # rendu d'avant : un bouton en pilule (999) et le NOM en tête de carte. `logo` =
    # `(url https, largeur, hauteur)` ; vide ⟹ pas d'image.
    bouton_rayon: int = 999
    logo: tuple = ()


# La charte du tenant PRIMAIRE — nos teintes, à l'octet. Les palettes de partenaires
# vivaient à côté jusqu'au 03/09/2026 ; elles se DÉCLARENT désormais en base
# (`tenants.brand`, servi par `_declaree` ci-dessous), pour la même raison que leur
# adresse de tableau de bord : accueillir le partenaire suivant demandait d'éditer ce
# fichier et de redéployer pour lui.
# ⚠️ **Ce n'est PAS un registre de marques et ça ne doit pas le redevenir.**
_TEINTES_PRIMAIRES = dict(
    fond="#faf6ec", surface="#fffdf7", encre="#2c2112", discret="#7a6c50",
    filet="#ece4d0", bouton_fond="#2c2112", bouton_encre="#fefcf5",
)


#: La charte que l'instance DÉCLARE pour son tenant primaire (15/11/2026). Les teintes
#: ci-dessus sont celles du code, donc celles de toute instance qui ne dit rien — et le
#: produit d'une instance peut ne pas leur ressembler du tout : ses emails partaient
#: alors dans une charte que son application ne porte pas. Comme le nom et le site
#: (#968), c'est l'instance qui la déclare, pas ce fichier : écrire ici les teintes ou
#: le logo d'un produit les imposerait à toutes les autres instances.
_ENV_CHARTE = "OTO_BRAND_CHARTE"
_RAYON_MAX = 999
_LOGO_MAX = 600


class CharteInvalide(ValueError):
    """`OTO_BRAND_CHARTE` est posée mais illisible : pas un objet JSON, une teinte
    absente ou qui n'est pas une couleur, un rayon ou un logo invalide."""


def _charte_declaree() -> dict:
    """Ce que `OTO_BRAND_CHARTE` déclare, prêt à passer à `Marque`, ou `{}` si elle
    n'est pas posée.

    JSON : les sept teintes (`fond`, `surface`, `encre`, `discret`, `filet`,
    `bouton_fond`, `bouton_encre`, en hexadécimal), et en option `bouton_rayon` (entier,
    en px) et `logo` (`{"url": "https://…", "largeur": n, "hauteur": n}`).

    ⚠️ **Validée comme une palette de tenant (`_declaree`), et LÈVE (`CharteInvalide`)**
    au lieu de retomber sur la charte du code : une déclaration illisible servie en
    silence ferait partir les emails dans une charte que l'instance croit avoir
    remplacée. `verifier_reglages` la lit au démarrage, qui refuse alors de partir en la
    nommant — comme `OTO_DASHBOARD_LINK_PATHS`. Rien d'elle ne finit dans un attribut
    `style` sans être validé."""
    brut = (os.environ.get(_ENV_CHARTE) or "").strip()
    if not brut:
        return {}
    try:
        decl = json.loads(brut)
    except ValueError as exc:
        raise CharteInvalide(f"{_ENV_CHARTE} n'est pas du JSON : {exc}") from exc
    if not isinstance(decl, dict):
        raise CharteInvalide(f"{_ENV_CHARTE} : objet JSON attendu")
    out: dict = {}
    for champ in _CHAMPS_COULEUR:
        val = decl.get(champ)
        if not isinstance(val, str) or not _COULEUR_RE.match(val.strip()):
            raise CharteInvalide(
                f"{_ENV_CHARTE} : « {champ} » absent ou pas une couleur ({val!r})")
        out[champ] = val.strip()
    if "bouton_rayon" in decl:
        r = decl["bouton_rayon"]
        if isinstance(r, bool) or not isinstance(r, int) or not 0 <= r <= _RAYON_MAX:
            raise CharteInvalide(
                f"{_ENV_CHARTE} : bouton_rayon {r!r} invalide (entier de 0 à {_RAYON_MAX})")
        out["bouton_rayon"] = r
    if decl.get("logo") is not None:
        logo = decl["logo"]
        url = logo.get("url") if isinstance(logo, dict) else None
        dims = [logo.get(k) for k in ("largeur", "hauteur")] if isinstance(logo, dict) else []
        if (not isinstance(url, str) or not url.startswith("https://")
                or any(c in url for c in "\"'<> \r\n")
                or len(dims) != 2
                or not all(isinstance(d, int) and not isinstance(d, bool)
                           and 0 < d <= _LOGO_MAX for d in dims)):
            raise CharteInvalide(
                f"{_ENV_CHARTE} : logo invalide (url https et largeur/hauteur entières "
                f"de 1 à {_LOGO_MAX} attendues)")
        out["logo"] = (url, dims[0], dims[1])
    return out


def verifier_reglages() -> None:
    """La garde de démarrage : `OTO_BRAND_CHARTE` est lue une fois, et une déclaration
    illisible refuse le démarrage en la nommant — jamais la charte du code en silence."""
    _charte_declaree()


def marque_instance() -> Marque:
    """La marque du tenant PRIMAIRE de cette instance : son NOM et son SITE sont
    DÉCLARÉS (`OTO_BRAND_NAME`, `OTO_BRAND_SITE` — décision du 28/09/2026, #968), les
    teintes sont celles du code. Le site était écrit ici en dur : toute autre instance
    signait donc ses emails et ses pages publiques de NOTRE adresse. Lève si l'une des
    deux manque, et `identite_instance.verifier` refuse le démarrage.

    `OTO_BRAND_SITE` est un hôte nu (`exemple.tld`) : il s'affiche en pied d'email et
    devient `https://<site>` dans le pied des pages publiques."""
    return Marque(slug=primary_slug(), nom=nom_instance(), site=site_instance(),
                  langue=langue_instance(),
                  **{**_TEINTES_PRIMAIRES, **_charte_declaree()})


def langue_instance() -> str:
    """La langue d'un destinataire sans préférence connue (un invité sans compte),
    sous la marque de l'instance : `OTO_BRAND_LANGUE` (`fr`|`en`). Le primaire ne lit
    pas `tenants.brand`, c'est donc ici qu'il la déclare. Vide ⟹ `""`, les gabarits
    servent FR comme avant ; toute autre valeur LÈVE (`LangueInconnue`) — une faute de
    frappe servirait le français en silence à des invités qu'on croit servir en anglais."""
    return _langue(os.environ.get("OTO_BRAND_LANGUE"), "OTO_BRAND_LANGUE")


def nom_instance() -> str:
    return require_env("OTO_BRAND_NAME").strip()


def site_instance() -> str:
    site = require_env("OTO_BRAND_SITE").strip()
    if "/" in site or ":" in site:
        raise RuntimeError(
            f"OTO_BRAND_SITE vaut {site!r} : attendu un hôte nu (exemple.tld), sans "
            "schéma ni chemin.")
    return site

# Le gabarit d'un slug qu'on ne connaît pas : les gris neutres du système partagé
# par les deux fronts, et le NOM qu'on nous a donné. Pas de site (on ne l'invente
# pas), donc pas de ligne de pied à sa marque.
_NEUTRE = Marque(
    slug="", nom="", site="",
    fond="#f9f9fb", surface="#ffffff", encre="#1c2024", discret="#60646c",
    filet="#d9d9e0", bouton_fond="#1c2024", bouton_encre="#ffffff",
)


_CHAMPS_COULEUR = ("fond", "surface", "encre", "discret", "filet",
                   "bouton_fond", "bouton_encre")
_COULEUR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# `adresse@domaine.tld` ou `Nom <adresse@domaine.tld>` — rien d'autre : la valeur
# finit dans un en-tête, un CR/LF ou un `<` de trop y écrirait autre chose, et une
# `,` ou un `;` dans le nom affiché en ferait une LISTE d'adresses.
_EXPEDITEUR_RE = re.compile(
    r"^(?:[^<>\r\n\x00@,;]{1,64} )?<?[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}>?$")
_LANGUES = ("fr", "en")


class LangueInconnue(ValueError):
    """Une langue déclarée hors de `_LANGUES` : jamais ignorée, jamais repliée sur FR."""


def _langue(val, source: str) -> str:
    """`fr`|`en`, ou `""` si rien n'est déclaré. Une valeur qui n'est ni vide ni une
    langue servie lève en nommant sa SOURCE : c'est la déclaration qui est fausse, et
    c'est là qu'il faut aller la corriger."""
    v = val.strip().lower() if isinstance(val, str) else ""
    if not v:
        return ""
    if v not in _LANGUES:
        raise LangueInconnue(
            f"{source} vaut {val!r} : langue attendue parmi {', '.join(_LANGUES)} "
            "(ou vide pour le français).")
    return v


def _expediteur(slug: str, val) -> str:
    v = val.strip() if isinstance(val, str) else ""
    if not v:
        return ""
    if not _EXPEDITEUR_RE.match(v) or v.count("<") != v.count(">"):
        logger.warning("marque du tenant %r : expéditeur %r invalide — ignoré", slug, v)
        return ""
    return v


def _declaree(slug: str) -> Optional[Marque]:
    """La palette que le TENANT déclare (`tenants.brand`), ou None.

    ⚠️ **Validée ici, pas au transport.** Le registre porte ce qui est écrit en base ;
    c'est à l'usage de juger, parce que c'est l'usage qui sait ce qu'il consomme —
    sept teintes nommées, en notation hexadécimale. Une clé inconnue est ignorée, une
    valeur qui n'est pas une couleur aussi : une configuration à moitié remplie ne
    doit jamais empêcher un email de partir, ni laisser passer une valeur qui finirait
    telle quelle dans un attribut `style` (une palette est du texte injecté dans du
    HTML — la valider est aussi ce qui l'empêche d'y écrire autre chose).

    ⚠️ **Sauf la langue** : une langue qui n'est ni vide ni servie LÈVE (`_langue`).
    Ignorée, elle servait le français en silence à ceux qu'on croyait servir en anglais
    — le seul champ dont le défaut se voit à l'arrivée sans que personne le remarque.

    Une palette PARTIELLE est refusée entière plutôt que complétée par la nôtre :
    mélanger sept teintes venues de deux chartes produit un dessin que personne n'a
    dessiné, et le défaut ne se verrait qu'à l'arrivée, chez le destinataire.
    """
    from . import tenancy  # tardif : `tenancy` n'a pas à connaître le dessin
    entree = tenancy.current().entry_for_slug(slug)
    declaree = getattr(entree, "brand", None) if entree is not None else None
    if not isinstance(declaree, dict) or not declaree:
        return None
    teintes = {}
    for champ in _CHAMPS_COULEUR:
        val = declaree.get(champ)
        if not isinstance(val, str) or not _COULEUR_RE.match(val.strip()):
            logger.warning(
                "marque du tenant %r : « %s » absent ou pas une couleur (%r) — palette "
                "déclarée ignorée en entier, on ne mélange pas deux chartes",
                slug, champ, val)
            return None
        teintes[champ] = val.strip()
    nom = str(declaree.get("nom") or slug)
    return Marque(slug=slug, nom=nom, site=str(declaree.get("site") or ""),
                  expediteur=_expediteur(slug, declaree.get("expediteur")),
                  langue=_langue(declaree.get("langue"),
                                 f"tenants.brand.langue du tenant {slug!r}"),
                  **teintes)


def marque(slug: Optional[str]) -> Marque:
    """La marque de ce slug. Inconnu ⟹ gabarit neutre **portant son nom**.

    On ne replie PAS sur oto : écrire « oto » en pied de l'email d'un partenaire est
    le faux que 7d10a798 a corrigé côté texte, et il reviendrait ici par la porte du
    dessin. Sans nom du tout (`None`, `""`), c'est la marque que l'instance déclare
    (`marque_instance`) — le défaut de `front_brand`, dont NULL veut dire « la
    plateforme ».

    **Ordre depuis le 03/09/2026 : ce que le TENANT déclare d'abord**, jamais une
    palette de partenaire écrite dans notre code : elle obligerait à nous redéployer
    pour accueillir le suivant — c'est le même défaut que son adresse de tableau de
    bord, corrigé de la même façon et au même endroit. Un slug tiers sans déclaration
    prend le gabarit neutre à son nom — jamais les teintes du tenant primaire.
    """
    s = (slug or "").strip()
    if not s or s.lower() == primary_slug():
        # Le tenant primaire ne se surcharge pas en base : sa marque est celle que
        # l'instance déclare, et une ligne de registre ne doit pas pouvoir la repeindre.
        return marque_instance()
    declaree = _declaree(s)
    if declaree is not None:
        return declaree
    return Marque(**{**_NEUTRE.__dict__, "slug": s, "nom": s})


# --- Styles dérivés d'une marque -------------------------------------------
#
# Rendus par des fonctions plutôt que posés en constantes : une constante serait
# forcément celle d'UNE marque, et c'est précisément la faute qu'on répare.

PARA = "margin:0 0 16px"          # paragraphe courant (la cellule porte la typo)
PARA_FIN = "margin:0"             # dernier d'un bloc : pas de marge orpheline


def discret(m: Marque) -> str:
    """Texte secondaire : l'URL en clair sous un bouton, une date, le pied."""
    return f"color:{m.discret};font-size:13px;line-height:1.5"


def bouton(m: Marque, url: Optional[str], libelle: str) -> str:
    """Le bouton d'ouverture, ou RIEN — avec, dessous, l'adresse en clair.

    `url` vide rend la chaîne vide : le lien d'une vue dépend d'un patron déclaré par
    le tenant, et le produit du partenaire n'a pas forcément cette vue (`links.py`).
    Un email est un lien AFFICHÉ, pas une redirection : on n'écrit rien plutôt que
    d'envoyer quelque part.

    **Table, pas `<a>` seul** : Outlook (moteur Word) n'applique ni `padding` ni
    `border-radius` à un `<a>` en `inline-block` — le bouton s'y réduisait au texte.
    Le fond et l'arrondi vivent donc sur le `<td>`, le `<a>` ne porte que sa mise en
    page interne."""
    if not url:
        return ""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:4px 0 12px"><tr>'
        f'<td align="center" style="border-radius:{m.bouton_rayon}px;background:{m.bouton_fond}">'
        f'<a href="{_email._esc_attr(url)}" style="display:inline-block;padding:12px 24px;'
        f'font-family:{POLICE};font-size:15px;font-weight:600;line-height:1;'
        f'color:{m.bouton_encre};text-decoration:none;border-radius:{m.bouton_rayon}px">'
        f'{_email._esc(libelle)}</a></td></tr></table>'
        # L'adresse en clair sous le bouton est échappée en ATTRIBUT alors qu'elle
        # est du texte : `_esc` laisse passer les guillemets, et un `"` venu d'une
        # URL d'agent traverserait alors le rendu intact. `&quot;` s'affiche `"` —
        # la propriété « aucune donnée d'entrée ne ressort avec un guillemet nu »
        # coûte donc zéro et se vérifie d'un seul coup d'œil.
        f'<p style="{PARA};{discret(m)};word-break:break-all">'
        f'{_email._esc_attr(url)}</p>'
    )


def _lien_desinscription(m: Marque, desinscription: Optional[tuple]) -> str:
    """Le lien « ne plus recevoir », ou RIEN.

    Il ne passe PAS par `mention` : cette phrase-là est échappée (aucun appelant n'a
    le droit d'y glisser du HTML), donc un lien y arriverait en toutes lettres. Il a
    son propre paramètre, une paire `(url, libellé)`, et l'URL est échappée en
    ATTRIBUT — un guillemet refermerait le `href` et la balise suivante serait celle
    de l'auteur du texte.

    Un `https://` est exigé : un lien de désinscription en clair est bloqué ou marqué
    « non sécurisé », et un désabonnement qu'on ne peut pas cliquer n'en est pas un.
    """
    if not desinscription:
        return ""
    url, libelle = desinscription
    url, libelle = (url or "").strip(), (libelle or "").strip()
    if not url or not libelle:
        return ""
    if not url.startswith("https://"):
        raise ValueError(f"lien de désinscription : https:// attendu (reçu {url[:24]!r}).")
    return (f'<br><a href="{_email._esc_attr(url)}" style="color:inherit">'
            f'{_email._esc(libelle)}</a>')


def _pied(m: Marque, mention: Optional[str],
          desinscription: Optional[tuple] = None, signature: bool = True) -> str:
    """Le pied : la signature de marque, puis la raison de l'envoi.

    Trois régimes, et la distinction porte du sens :
    `None` = **pas de pied du tout** (la carte s'arrête au contenu — ce que demande
    `footer=False`) ; `""` = la signature seule ; un texte = signature + raison.

    Sans `site` (marque inconnue), la ligne de signature disparaît plutôt que de
    porter un nom sans adresse — on n'invente pas le domaine d'un partenaire ; et un
    pied qui n'aurait alors NI signature NI mention ne se rend pas du tout.

    `desinscription` = `(url, libellé)` d'un lien de désabonnement, réservé au pied
    MARKETING (cf. `mention_transactionnelle`, qui n'en propose délibérément pas).

    `signature=False` = le pied n'est pas le NÔTRE : c'est celui qu'une org déclare
    pour ses envois avec sa propre clé (décision du 12/09/2026). La ligne « marque ·
    site » disparaît — la garder signerait de notre nom un message qui ne vient pas
    de nous, adressé à quelqu'un qui ne nous connaît pas."""
    if mention is None:
        return ""
    ligne = f"{_email._esc(m.nom)} · {_email._esc(m.site)}<br>" if (m.site and signature) else ""
    if not ligne and not mention:
        return ""
    return (
        f'<tr><td style="padding:0 32px"><div style="border-top:1px solid {m.filet}">'
        f'</div></td></tr>'
        f'<tr><td style="padding:16px 32px 28px 32px;font-family:{POLICE};'
        f'{discret(m)}">{ligne}{_email._esc(mention)}'
        f'{_lien_desinscription(m, desinscription)}</td></tr>'
    )


def mention_transactionnelle(m: Marque, locale: Optional[str]) -> str:
    """La phrase de pied d'un email TRANSACTIONNEL : pourquoi il arrive, et quoi
    faire s'il n'aurait pas dû.

    Elle ne propose pas de désabonnement, et c'est délibéré : on ne se désabonne pas
    d'une invitation ni d'un partage — le jour où l'on n'en veut plus, c'est le
    compte ou le partage qu'on retire, pas une case à décocher. Le pied MARKETING
    (`email.render_composed_email`) dit, lui, l'inverse, parce qu'il le doit."""
    if locale == "en":
        return (f"you're receiving this because something involving you happened on "
                f"{m.nom}. reply to this message if it looks wrong.")
    return (f"vous recevez ce message parce qu'une action vous concerne sur {m.nom}. "
            f"répondez-y si quelque chose cloche.")


def _logo(m: Marque) -> str:
    """Le logo DÉCLARÉ, au-dessus de la carte sur le fond de page, ou RIEN.

    Il remplace le nom en tête de carte. `alt` = le nom de la marque : beaucoup de
    clients bloquent les images, l'email doit se lire sans elle — l'alt s'affiche alors
    à la place, dans la typo du texte. Largeur et hauteur explicites : sans elles,
    Outlook affiche l'image à sa taille native."""
    if not m.logo:
        return ""
    url, largeur, hauteur = m.logo
    return (
        f'<table role="presentation" width="{LARGEUR}" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;max-width:{LARGEUR}px"><tr>'
        f'<td style="padding:0 0 24px 2px"><img src="{_email._esc_attr(url)}" '
        f'width="{largeur}" height="{hauteur}" alt="{_email._esc_attr(m.nom)}" '
        f'style="display:block;border:0;outline:none;width:{largeur}px;height:{hauteur}px;'
        f'font-family:{POLICE};font-size:16px;font-weight:600;color:{m.encre}"></td>'
        f'</tr></table>'
    )


def page(m: Marque, contenu: str, *, preheader: str, mention: Optional[str],
         locale: Optional[str] = None, desinscription: Optional[tuple] = None,
         signature: bool = True) -> str:
    """Le document complet : `<head>`, fond, carte, en-tête de marque, pied.

    `contenu` = les `<p>` déjà rendus par le gabarit (la cellule porte la typo, donc
    un paragraphe n'a que sa marge à déclarer — cf. `PARA`). `preheader` = la ligne
    d'aperçu de la boîte de réception ; `mention` = la phrase de pied qui dit
    pourquoi cet email arrive (`None` = aucun pied, cf. `_pied`). Les deux sont
    ÉCHAPPÉS ici, comme tout le reste : aucun appelant n'a le droit d'y passer du
    HTML.

    Pas de bourrage après le preheader (retiré le 06/11/2026) : 120 caractères de
    largeur nulle dans un bloc caché sont un signal anti-spam, et des invitations
    arrivaient en spam. Gmail peut coller au preheader le début du corps ; chaque
    gabarit en fait donc une phrase complète, qui se lit seule.
    """
    lang = "en" if locale == "en" else "fr"
    return (
        '<!DOCTYPE html>'
        f'<html lang="{lang}"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light">'
        '<meta name="supported-color-schemes" content="light">'
        f'<title>{_email._esc(m.nom)}</title>'
        '</head>'
        f'<body style="margin:0;padding:0;background:{m.fond};'
        f'-webkit-text-size-adjust:100%">'
        f'<div style="display:none;max-height:0;max-width:0;opacity:0;overflow:hidden;'
        f'mso-hide:all;font-size:1px;line-height:1px;color:{m.fond}">'
        f'{_email._esc(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;background:{m.fond}">'
        f'<tr><td align="center" style="padding:32px 12px">'
        + _logo(m) +
        f'<table role="presentation" width="{LARGEUR}" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;max-width:{LARGEUR}px;background:{m.surface};'
        f'border:1px solid {m.filet};border-radius:12px">'
        + ("" if m.logo else
           f'<tr><td style="padding:28px 32px 0 32px;font-family:{POLICE};font-size:15px;'
           f'font-weight:600;letter-spacing:-0.01em;color:{m.encre}">{_email._esc(m.nom)}'
           f'</td></tr>') +
        f'<tr><td style="padding:{32 if m.logo else 20}px 32px 4px 32px;font-family:{POLICE};'
        f'font-size:16px;line-height:1.6;color:{m.encre}">{contenu}</td></tr>'
        + _pied(m, mention, desinscription, signature) +
        '</table></td></tr></table></body></html>'
    )
