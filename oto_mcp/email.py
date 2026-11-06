"""Envoi d'email transactionnel : le TRANSPORT des mails de plateforme et des envois BYO.

Les mails de plateforme (invitations, notifications, digests, contact, `email_send`
sous l'adresse de l'instance) passent tous par `_send`, sur le transport que
l'instance DÉCLARE (`transport_plateforme`) — l'un ou l'autre, jamais les deux :

- **un relais mailer** : `OTO_MAILER_URL` (endpoint `/api/send`) + bearer
  `OTO_MAILER_SEND_BEARER` ;
- **l'API Scaleway TEM de l'instance, en direct** : `OTO_TEM_SECRET_KEY`,
  `OTO_TEM_PROJECT_ID`, `OTO_TEM_REGION`, les trois ensemble — le même transport
  (`send_via_scaleway_tem`) que celui d'une org qui apporte sa clé.

**Best-effort** : un échec d'envoi (réseau, refus du service) rend False et l'appelant
expose l'`invite_url` pour un partage manuel. Une configuration absente, ambiguë ou
incomplète LÈVE, elle (#968).
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from html.parser import HTMLParser

from .config import require_env

log = logging.getLogger("oto_mcp.email")

_TEM_INSTANCE = ("OTO_TEM_SECRET_KEY", "OTO_TEM_PROJECT_ID", "OTO_TEM_REGION")


@dataclass(frozen=True)
class TemInstance:
    """Les identifiants Scaleway TEM de l'instance — jamais ceux d'une org."""
    secret_key: str = field(repr=False)
    project_id: str
    region: str


def transport_plateforme() -> "str | TemInstance":
    """Le transport des mails de plateforme de CETTE instance : l'URL du relais mailer,
    ou les identifiants TEM de l'instance. Un choix de configuration, jamais deviné :
    LÈVE (`RuntimeError`, comme `require_env`) si aucun n'est déclaré, si les deux le
    sont, ou si TEM l'est à moitié. Relu à chaque envoi ; rejoué au démarrage par
    `identite_instance.verifier`.

    Avec TEM, l'expéditeur reste `OTO_MAIL_FROM`, sur un domaine vérifié dans le projet
    TEM de l'instance : Scaleway refuse sinon — aucune logique de domaine ici."""
    url = os.environ.get("OTO_MAILER_URL") or None
    tem = (os.environ.get("OTO_TEM_SECRET_KEY") or None,
           os.environ.get("OTO_TEM_PROJECT_ID") or None,
           os.environ.get("OTO_TEM_REGION") or None)
    if url and any(tem):
        raise RuntimeError(
            "transport des emails de plateforme ambigu : OTO_MAILER_URL et "
            f"{', '.join(n for n, v in zip(_TEM_INSTANCE, tem) if v)} sont déclarées "
            "ensemble — l'instance envoie par son relais mailer OU par son API Scaleway "
            "TEM, n'en déclarer qu'un.")
    if any(tem):
        manquent = [n for n, v in zip(_TEM_INSTANCE, tem) if not v]
        if manquent:
            raise RuntimeError(
                f"transport Scaleway TEM de l'instance déclaré à moitié : il manque "
                f"{', '.join(manquent)} ({', '.join(_TEM_INSTANCE)} vont ensemble).")
        return TemInstance(*tem)
    if url:
        return url
    raise RuntimeError(
        "aucun transport des emails de plateforme : déclarer OTO_MAILER_URL (relais "
        f"mailer) ou {', '.join(_TEM_INSTANCE)} (API Scaleway TEM de l'instance).")


def _mail_from() -> str:
    return require_env("OTO_MAIL_FROM")


def _contact_to() -> str:
    """La boîte qui reçoit les réponses d'un email composé sans `reply_to` explicite."""
    return require_env("OTO_CONTACT_TO")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    """Échappement pour une VALEUR D'ATTRIBUT : `_esc` laisse passer les guillemets,
    et un `"` dans un `alt=` refermerait l'attribut — la balise suivante serait celle
    de l'auteur du texte, pas la nôtre."""
    return _html.escape(s or "", quote=True)


def _no_crlf(s: str | None) -> str | None:
    """Neutralise une injection d'en-tête email : retire CR/LF (et NUL) d'une valeur
    destinée à un champ d'en-tête (sujet, to, from, replyTo). Des données
    user-controlled (nom de projet, titre de page) transitent par le sujet ; un
    \\r\\n y injecterait un en-tête arbitraire côté service d'envoi."""
    if s is None:
        return None
    return s.replace("\r", "").replace("\n", " ").replace("\x00", "")


class _VersionTexte(HTMLParser):
    """Rend le texte lisible d'un gabarit HTML, pour la partie `text/plain`."""
    _SAUTS = {"p", "br", "div", "tr", "table", "h1", "h2", "h3", "li"}
    _MUETS = {"head", "style", "script", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.morceaux: list[str] = []
        self._pile_cachee: list[str] = []   # balises ouvertes sous un display:none
        self._muet = 0
        self._lien: list[tuple[str, int]] = []  # (href, position du début du libellé)
        self._sep = " : "   # typographie FR ; `<html lang="en">` ⟹ ": "

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and (a.get("lang") or "").lower().startswith("en"):
            self._sep = ": "
        if self._pile_cachee:
            self._pile_cachee.append(tag)
            return
        if "display:none" in (a.get("style") or "").replace(" ", ""):
            self._pile_cachee.append(tag)   # le preheader : invisible, hors du texte
            return
        if tag in self._MUETS:
            self._muet += 1
        if tag in self._SAUTS:
            self.morceaux.append("\n")
        if tag == "a":
            self._lien.append((a.get("href") or "", len(self.morceaux)))

    def handle_endtag(self, tag):
        if self._pile_cachee:
            self._pile_cachee.pop()
            return
        if tag in self._MUETS:
            self._muet = max(0, self._muet - 1)
        if tag == "a" and self._lien:
            href, debut = self._lien.pop()
            libelle = "".join(self.morceaux[debut:]).strip()
            if href and not href.startswith("mailto:") and href != libelle:
                self.morceaux.append(f"{self._sep}{href}" if libelle else href)
        if tag in self._SAUTS:
            self.morceaux.append("\n")

    def handle_data(self, data):
        if not self._pile_cachee and not self._muet:
            self.morceaux.append(data)


_INVISIBLES = re.compile("[\u200b\u200c\u200d\u034f\u2060\ufeff\xad]")


def version_texte(html: str) -> str:
    """La partie `text/plain` d'un email de plateforme, tirée de son HTML : un email en
    HTML SEUL est un signal classique des filtres anti-spam (invitations rangées en
    spam, 06/11/2026). Liens gardés en clair, preheader caché et caractères invisibles
    retirés, un paragraphe par bloc."""
    p = _VersionTexte()
    try:
        p.feed(html or "")
        p.close()
    # noqa: SILENT — un HTML illisible part sans texte plutôt que de bloquer l'envoi
    except Exception:
        return ""
    texte = _INVISIBLES.sub("", "".join(p.morceaux)).replace("\xa0", " ")
    lignes = [" ".join(l.split()) for l in texte.split("\n")]
    out: list[str] = []
    for l in lignes:
        precedente = next((x for x in reversed(out) if x), "")
        if l and l.startswith("http") and precedente.endswith(l):
            continue   # l'URL que le gabarit répète en clair sous son bouton
        if l or (out and out[-1]):
            out.append(l)
    return "\n".join(out).strip()


def _relais_refuse(url: str, depuis: str, detail: str) -> None:
    """Un 403 du relais n'est pas un aléa : l'expéditeur (celui d'un tenant,
    `tenants.brand.expediteur`, ou celui de l'instance) a un domaine hors de son
    allowlist `MAILER_FROM_DOMAINS`, et AUCUN envoi sous lui ne partira tant qu'on ne
    l'y ajoute pas. Journalisé en ERREUR et signalé au suivi d'erreurs (même canal et
    même forme que `tenancy._refus`), jamais rangé avec un timeout. Fail-open sur le
    suivi seulement : l'appelant reçoit False comme avant."""
    message = ("relais %s : expéditeur %r refusé (403) — domaine hors "
               "MAILER_FROM_DOMAINS du relais, aucun envoi sous lui ne part (%s)")
    args = (url, depuis, detail)
    log.error(message, *args)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("oto.mailer", "expediteur_refuse")
            sentry_sdk.capture_message(message % args, level="error")
    # noqa: SILENT — le suivi d'erreurs indisponible n'empêche pas de rendre le refus
    except Exception:
        pass


def _send(to: str, subject: str, html: str, reply_to: str | None = None,
          from_email: str | None = None, cc: list[str] | None = None,
          mailer_url: str | None = None, bearer: str | None = None) -> bool:
    """Le choke-point des mails de plateforme : part sur le transport de l'instance
    (`transport_plateforme`, qui LÈVE sans transport déclaré, #968). `from_email` =
    adresse expéditrice, `Nom <adresse>` accepté (défaut `_mail_from()`) : le relais la
    refuse (403) hors de son allowlist `MAILER_FROM_DOMAINS`, Scaleway hors des domaines
    vérifiés du projet TEM. Best-effort : False si pas de bearer (relais) ou si l'envoi
    échoue.

    `cc` = copies VISIBLES. `mailer_url`/`bearer` = un relais déclaré par l'appelant
    (l'email d'activation d'un tenant part par SON instance du mailer, dont le domaine
    expéditeur est vérifié chez elle) — les deux ensemble, et alors le transport de
    l'instance n'intervient pas. CR/LF neutralisés sur chaque en-tête, quel que soit le
    transport (le corps `html` n'est pas un en-tête, et les gabarits l'échappent)."""
    if mailer_url:
        if not bearer:
            return False
        return _par_relais(mailer_url, bearer, to, subject, html,
                           from_email or _mail_from(), reply_to, cc)
    # Résolu AVANT tout bloc best-effort : un transport manquant est une erreur de
    # CONFIGURATION, pas un aléa réseau — elle ne doit pas se perdre dans le même
    # `except` qu'un timeout httpx (#968).
    transport = transport_plateforme()
    if isinstance(transport, TemInstance):
        return _par_tem(transport, to, subject, html, from_email or _mail_from(),
                        reply_to, cc)
    bearer = bearer or os.environ.get("OTO_MAILER_SEND_BEARER")
    if not bearer:
        return False
    return _par_relais(transport, bearer, to, subject, html,
                       from_email or _mail_from(), reply_to, cc)


def _par_tem(tem: TemInstance, to: str, subject: str, html: str, depuis: str,
             reply_to: str | None, cc: list[str] | None) -> bool:
    """Envoi par l'API Scaleway TEM de l'instance — `send_via_scaleway_tem`, le transport
    d'une org qui apporte sa clé, avec les identifiants de l'instance. TEM prend le nom
    et l'adresse de l'expéditeur séparément : `Nom <adresse>` est décomposé ici, et une
    valeur sans adresse lisible LÈVE (c'est la configuration, pas un aléa)."""
    nom, adresse = parseaddr(_no_crlf(depuis))
    if "@" not in adresse:
        raise RuntimeError(f"expéditeur {depuis!r} illisible pour Scaleway TEM : "
                           "« adresse » ou « Nom <adresse> » attendu (OTO_MAIL_FROM).")
    return send_via_scaleway_tem(
        _no_crlf(to), _no_crlf(subject), html, secret_key=tem.secret_key,
        project_id=tem.project_id, region=tem.region, from_email=adresse,
        from_name=nom or None, reply_to=_no_crlf(reply_to),
        cc=[_no_crlf(a) for a in cc] if cc else None, text=version_texte(html))


def _par_relais(url: str, bearer: str, to: str, subject: str, html: str, depuis: str,
                reply_to: str | None, cc: list[str] | None) -> bool:
    """Envoi par un relais mailer (`/api/send`, Scaleway TEM derrière lui)."""
    try:
        import httpx
        payload = {"from": _no_crlf(depuis), "to": _no_crlf(to),
                   "subject": _no_crlf(subject), "html": html}
        texte = version_texte(html)
        if texte:
            # Relu par le relais (`text`, oto-auth-mailer) ; un relais plus ancien
            # l'ignore sans erreur et l'email part en HTML seul, comme avant.
            payload["text"] = texte
        if reply_to:
            # Le service lit `replyTo` (camelCase) et IGNORE en silence toute autre
            # clé : `reply_to` y a fait perdre l'adresse de réponse sans erreur (oto#148).
            payload["replyTo"] = _no_crlf(reply_to)
        if cc:
            payload["cc"] = [_no_crlf(a) for a in cc]
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            json=payload,
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 403:
            _relais_refuse(url, depuis, r.text[:200])
            return False
        log.warning("mailer %s → %s %s", url, r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("email to %s not sent (%s)", to, e)
        return False


def send_via_resend(to: str, subject: str, html: str, *, api_key: str,
                    from_email: str, reply_to: str | None = None,
                    cc: list[str] | None = None) -> bool:
    """Envoi direct via l'API Resend, avec la clé BYOK de l'org. `from_email` =
    adresse sur un domaine vérifié côté Resend par l'org. Best-effort (False si
    échec), même contrat que `_send`. PAS d'usage du client oto-core (interdiction
    de résolution de secret côté serveur)."""
    if not api_key or not from_email:
        return False
    try:
        import httpx
        payload = {"from": from_email, "to": [to], "subject": subject, "html": html}
        if reply_to:
            payload["reply_to"] = reply_to
        if cc:
            payload["cc"] = list(cc)
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=10.0,
        )
        if r.status_code in (200, 201):
            return True
        log.warning("resend → %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("resend email to %s not sent (%s)", to, e)
        return False


def send_via_scaleway_tem(to: str, subject: str, html: str, *, secret_key: str,
                          project_id: str, from_email: str, from_name: str | None = None,
                          region: str = "fr-par", reply_to: str | None = None,
                          cc: list[str] | None = None, text: str | None = None) -> bool:
    """Envoi direct via l'API Scaleway TEM, avec la clé BYO de l'org (secret_key +
    project_id). `from_email` = adresse sur un domaine VÉRIFIÉ dans le compte Scaleway
    de l'org — l'API TEM refuse les domaines non vérifiés (propriété du domaine garantie
    par Scaleway, zéro logique domaine côté oto). Best-effort (False si échec), même
    contrat que `send_via_resend`. PAS de résolution de secret côté serveur."""
    if not secret_key or not project_id or not from_email:
        return False
    region = region or "fr-par"
    try:
        import httpx
        frm: dict = {"email": from_email}
        if from_name:
            frm["name"] = from_name
        payload: dict = {
            "from": frm,
            "to": [{"email": to}],
            "subject": subject,
            "html": html,
            "project_id": project_id,
        }
        if text:
            payload["text"] = text
        if cc:
            payload["cc"] = [{"email": a} for a in cc]
        if reply_to:
            payload["additional_headers"] = [{"key": "Reply-To", "value": reply_to}]
        r = httpx.post(
            f"https://api.scaleway.com/transactional-email/v1alpha1/regions/{region}/emails",
            headers={"X-Auth-Token": secret_key},
            json=payload,
            timeout=10.0,
        )
        if r.status_code in (200, 201):
            return True
        log.warning("scaleway tem → %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, domaine non vérifié → best-effort
        log.warning("scaleway tem email to %s not sent (%s)", to, e)
        return False


# Le DESSIN (marques, palettes, gabarit de page, bouton) vit dans `email_brand.py`
# — ce module-ci garde le TRANSPORT. Import de MODULE, jamais `from .email_brand
# import page` : le cycle est mutuel (email_brand appelle `_esc`/`_esc_attr` d'ici)
# et seul un import de module le rend inoffensif dans les deux sens, aucun des deux
# ne touchant un attribut de l'autre au moment de l'import.
from . import email_brand as _charte  # noqa: E402


def _bouton(app_url: str | None, libelle: str, brand: str | None = None) -> str:
    """Le bouton d'ouverture à la marque du DESTINATAIRE, ou RIEN.

    Le lien d'un projet dépend d'un patron déclaré par le tenant : le produit du
    partenaire n'a pas forcément cette vue, et coller NOTRE chemin sous SON domaine
    fabriquerait un lien mort — pire qu'une absence, parce qu'un lien mort ne se
    diagnostique pas, il se subit (cf. `links.py`). Un email est un lien AFFICHÉ, pas
    une redirection : on n'écrit rien plutôt que d'envoyer quelque part."""
    return _charte.bouton(_charte.marque(brand), app_url, libelle)


# Les gabarits vivent dans `email_templates.py` (oto-backend#700), qui importe ce module :
# import de MODULE, et `email.send_invite_email` etc. (que les tests monkeypatchent) lus À
# L'APPEL (PEP 562) — `from .email_templates import <nom>` cassait l'entrée par ce module-là.
from . import email_templates as _gabarits  # noqa: E402
_GABARITS = ("send_invite_email", "send_resource_shared_email",
             "send_resource_transferred_email", "send_signal_digest_email",
             "send_process_readers_digest_email")


def __getattr__(nom: str):
    if nom in _GABARITS:
        return getattr(_gabarits, nom)
    raise AttributeError(f"module {__name__!r} has no attribute {nom!r}")


# L'image se contraint à la largeur utile de la colonne (`email_brand.LARGEUR_UTILE`)
# par l'attribut `width` (lu par les clients qui ignorent le CSS) ET par
# `max-width:100%` (affichage réduit : l'image suit la colonne au lieu de la déborder).
#
# ⚠️ Lu À L'APPEL, jamais au niveau module : `email_brand` importe `email` en retour,
# donc lire un attribut de l'un pendant l'exécution du corps de l'autre casse l'import
# dès qu'on entre par `email_brand` (vécu ici même). Le cycle n'est inoffensif QUE
# tant que les deux modules ne se touchent qu'au moment de l'appel.
_IMG_STYLE = "max-width:100%;height:auto;display:block;border:0"


def _image_html(image_url: str | None, image_alt: str | None) -> str:
    """L'image de tête, ou RIEN. Lève `ValueError` — jamais de repli :

    - **`alt` obligatoire** : beaucoup de clients bloquent les images, le mail doit
      garder son sens sans elle. Pas de valeur par défaut, qui ne dirait rien.
    - **`https://` seul** : un `http://` est bloqué ou marqué « non sécurisé » par les
      clients, et un `data:`/`cid:` n'est pas une URL publique stable.
    - URL et alt échappés en ATTRIBUT (guillemets compris)."""
    url = (image_url or "").strip()
    alt = (image_alt or "").strip()
    if not url and not alt:
        return ""
    if url and not alt:
        raise ValueError("`image_alt` est requis avec `image_url` : le texte de "
                         "remplacement porte le sens du visuel quand l'image est bloquée.")
    if alt and not url:
        raise ValueError("`image_alt` sans `image_url` : rien à décrire.")
    if not url.startswith("https://"):
        raise ValueError(f"`image_url` doit commencer par https:// (reçu : {url[:24]!r}).")
    return (f'<p style="{_charte.PARA}"><img src="{_esc_attr(url)}" alt="{_esc_attr(alt)}" '
            f'width="{_charte.LARGEUR_UTILE}" style="{_IMG_STYLE}"></p>')


def _pied_de_l_org(org_footer: dict, en: bool) -> tuple[str, tuple | None]:
    """La phrase et le lien du pied qu'une org DÉCLARE pour ses envois avec sa propre
    clé — à la place du nôtre, jamais à côté (décision d'Alexis du 12/09/2026).

    Ni signature de marque ni « vous avez un compte » : le destinataire est un
    prospect de l'org, il n'a pas de compte chez nous et ne nous connaît pas. Le pied
    dit le moyen de ne plus rien recevoir que l'org a déclaré — une adresse, un lien,
    ou les deux. Sans aucun des deux, il n'y a pas de pied de l'org : on lève, on ne
    rend jamais un pied privé de désabonnement (la capacité de réglage refuse déjà ce
    cas en amont ; ceci est la garantie du gabarit, pas le message servi)."""
    url = str(org_footer.get("unsubscribe_url") or "").strip()
    adresse = str(org_footer.get("unsubscribe_email") or "").strip()
    if not url and not adresse:
        raise ValueError("pied de l'org sans désabonnement : `unsubscribe_url` ou "
                         "`unsubscribe_email` requis.")
    if en:
        tete, libelle = "to stop receiving our messages", "unsubscribe"
        ecrire = f", write to {adresse}" if adresse else ""
        lien = (" or use this link:" if adresse else ", use this link:") if url else "."
    else:
        tete, libelle = "pour ne plus recevoir nos messages", "se désabonner"
        ecrire = f", écrivez à {adresse}" if adresse else ""
        lien = (" ou utilisez ce lien :" if adresse else ", utilisez ce lien :") if url else "."
    return tete + ecrire + lien, ((url, libelle) if url else None)


def render_composed_email(
    body: str,
    *,
    cta_text: str | None = None,
    cta_url: str | None = None,
    footer: bool = True,
    image_url: str | None = None,
    image_alt: str | None = None,
    brand: str | None = None,
    locale: str | None = None,
    unsubscribe_url: str | None = None,
    org_footer: dict | None = None,
) -> str:
    """Rend le HTML, à la charte de `brand`, d'un email dont le **contenu est fourni
    par l'agent** (prose brute + CTA optionnel + UNE image de tête).

    `body` = texte brut : les lignes vides séparent des paragraphes, les sauts de
    ligne simples deviennent des `<br>`. Échappé (jamais de HTML injecté par
    l'agent). `footer` ajoute la signature de marque + l'opt-out par réponse.
    `image_url` + `image_alt` (les deux, ou aucun) placent une image AVANT le corps ;
    voir `_image_html` pour ce qui est refusé (`ValueError`).

    La ligne d'aperçu de la boîte de réception est le PREMIER PARAGRAPHE, pas le
    sujet : Gmail affiche « sujet — aperçu » côte à côte, et y répéter le sujet ne
    dit rien de plus. C'est aussi ce qui évite l'aperçu d'avant, où la boîte allait
    chercher le premier texte venu (« ou collez ce lien »).

    `org_footer` = le pied que l'org a déclaré (`{unsubscribe_url?, unsubscribe_email?}`)
    pour un envoi fait avec SA clé : il REMPLACE le nôtre (cf. `_pied_de_l_org`). C'est à
    l'appelant de ne le passer que sur ce chemin-là — `send_composed_email`, qui part
    avec la clé commune du mailer, ne l'accepte pas, et c'est délibéré."""
    m = _charte.marque(brand)
    image_html = _image_html(image_url, image_alt)
    paras = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
    body_html = "".join(
        f'<p style="{_charte.PARA}">{_esc(p).replace(chr(10), "<br>")}</p>'
        for p in paras
    )
    cta_html = _charte.bouton(m, cta_url, cta_text) if (cta_text and cta_url) else ""
    # Le pied MARKETING (pourquoi vous recevez ça, comment ne plus le recevoir) —
    # celui d'un transactionnel dit autre chose, cf. `email_templates`.
    #
    # ⚠️ La phrase CHANGE quand un lien de désinscription accompagne le pied : sans
    # lien, « répondez pour ne plus en recevoir » est le seul refus possible et il
    # faut le dire ; avec lien, le laisser proposerait deux chemins dont un seul est
    # enregistré quelque part (une réponse humaine ne persiste aucun refus).
    locale = locale or m.langue or None
    en = locale == "en"
    apercu = paras[0] if paras else m.nom
    if footer and org_footer:
        if unsubscribe_url:
            raise ValueError("`org_footer` et `unsubscribe_url` s'excluent : le pied de "
                             "l'org porte SON désabonnement, pas le nôtre.")
        mention_org, lien_org = _pied_de_l_org(org_footer, en)
        return _charte.page(m, image_html + body_html + cta_html,
                            preheader=apercu, mention=mention_org, locale=locale,
                            desinscription=lien_org, signature=False)
    if not footer:
        mention = None
    elif unsubscribe_url:
        mention = (f"you're receiving this because you have a {m.nom} account — "
                   "reply to this email to talk to us."
                   if en else
                   f"vous recevez ce message car vous avez un compte {m.nom} — "
                   "répondez à cet email pour nous parler.")
    else:
        mention = (f"you're receiving this because you have a {m.nom} account — "
                   "reply to this email to talk to us, or to stop receiving them."
                   if en else
                   f"vous recevez ce message car vous avez un compte {m.nom} — "
                   "répondez à cet email pour nous parler, ou pour ne plus en recevoir.")
    desinscription = ((unsubscribe_url, "unsubscribe" if en else
                       "ne plus recevoir ces messages")
                      if (footer and unsubscribe_url) else None)
    return _charte.page(m, image_html + body_html + cta_html,
                        preheader=apercu, mention=mention, locale=locale,
                        desinscription=desinscription)


def format_from(from_email: str | None, from_name: str | None = None) -> str | None:
    """En-tête `from` au format « Name <addr> » (ou l'adresse seule). None si pas
    d'adresse → l'appelant retombe sur la marque par défaut."""
    if not from_email:
        return None
    return f"{from_name} <{from_email}>" if from_name else from_email


def send_composed_email(
    to: str,
    subject: str,
    body: str,
    *,
    cta_text: str | None = None,
    cta_url: str | None = None,
    reply_to: str | None = None,
    footer: bool = True,
    from_email: str | None = None,
    from_name: str | None = None,
    image_url: str | None = None,
    image_alt: str | None = None,
    brand: str | None = None,
    locale: str | None = None,
    unsubscribe_url: str | None = None,
    cc: list[str] | None = None,
) -> bool:
    """Envoie un email à contenu libre (fourni par l'agent), rendu à la charte de
    `brand`, via le mailer Otomata (Scaleway TEM).

    `from_email`/`from_name` = adresse expéditrice (défaut = marque `_MAIL_FROM`) ;
    le domaine doit être dans l'allowlist du service. `reply_to` défaut = la boîte
    du studio (`OTO_CONTACT_TO`). `image_url`/`image_alt` = l'image de tête (cf.
    `render_composed_email`). True si envoyé, False sinon (best-effort).

    `cc` et `unsubscribe_url` s'excluent : le lien de désabonnement est celui du seul
    destinataire `to` (une relance nominative) — en copie, chacun recevrait le lien
    qui désabonne quelqu'un d'autre. Les deux ensemble LÈVENT."""
    if cc and unsubscribe_url:
        raise ValueError("send_composed_email : `cc` et `unsubscribe_url` s'excluent — le "
                         "lien de désabonnement nominatif partirait aux copies.")
    html = render_composed_email(body, cta_text=cta_text, cta_url=cta_url, footer=footer,
                                 image_url=image_url, image_alt=image_alt, brand=brand,
                                 locale=locale, unsubscribe_url=unsubscribe_url)
    # `require_env` seulement si `reply_to` est absent (court-circuit `or`) — sans
    # elle, le repli irait vers NOTRE boîte personnelle (#968).
    rt = reply_to or _contact_to()
    return _send(to, subject, html, reply_to=rt, from_email=format_from(from_email, from_name),
                 cc=cc)
