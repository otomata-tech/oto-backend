"""Envoi d'email transactionnel (invitations d'org) via **otomata-mailer**.

Standard Otomata : on n'utilise plus Resend per-app — l'endpoint générique
`POST mailer.oto.zone/api/send` (Scaleway TEM, brand Otomata, domaines from
vérifiés DKIM/SPF) sert les emails métier de toutes les apps. Bearer
`OTO_MAILER_SEND_BEARER`. **Best-effort** : sans bearer configuré ou en cas
d'échec, on ne lève pas — on renvoie False et l'appelant expose l'`invite_url`
pour un partage manuel.
"""
from __future__ import annotations

import html as _html
import logging
import os

from .config import require_env

log = logging.getLogger("oto_mcp.email")


def _mailer_url() -> str:
    return require_env("OTO_MAILER_URL")


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


def _send(to: str, subject: str, html: str, reply_to: str | None = None,
          from_email: str | None = None, cc: list[str] | None = None,
          mailer_url: str | None = None, bearer: str | None = None) -> bool:
    """Envoi via mailer.oto.zone (Scaleway TEM). `from_email` = adresse expéditrice
    (défaut marque `_mail_from()`) — le service refuse (403) un domaine hors allowlist
    `MAILER_FROM_DOMAINS`. Best-effort (False si pas de bearer ou échec) — mais
    `OTO_MAILER_URL`/`OTO_MAIL_FROM` sont REQUISES dès qu'un envoi est tenté (#968) :
    sans elles, un envoi partirait en silence sous NOTRE relais et NOTRE adresse.

    `cc` = copies VISIBLES (le service lit `cc`). `mailer_url`/`bearer` = un relais
    déclaré par l'appelant (l'email d'activation d'un tenant part par SON instance du
    mailer, dont le domaine expéditeur est vérifié chez elle) — les deux ensemble, sinon
    le relais de l'instance."""
    if mailer_url and not bearer:
        return False
    bearer = bearer or os.environ.get("OTO_MAILER_SEND_BEARER")
    if not bearer:
        return False
    # Résolues AVANT le bloc best-effort : une variable manquante est une erreur de
    # CONFIGURATION, pas un aléa réseau — elle ne doit pas se perdre dans le même
    # `except` qu'un timeout httpx (#968).
    url = mailer_url or _mailer_url()
    depuis = from_email or _mail_from()
    try:
        import httpx
        # Anti-injection d'en-tête : neutralise CR/LF sur TOUS les champs d'en-tête
        # (choke-point unique → couvre tous les templates). Le corps `html` n'est pas
        # un en-tête (et déjà échappé par les templates via _esc).
        payload = {"from": _no_crlf(depuis), "to": _no_crlf(to),
                   "subject": _no_crlf(subject), "html": html}
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
        log.warning("mailer %s → %s %s", url, r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("email to %s not sent (%s)", to, e)
        return False


def send_via_resend(to: str, subject: str, html: str, *, api_key: str,
                    from_email: str, reply_to: str | None = None) -> bool:
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
                          region: str = "fr-par", reply_to: str | None = None) -> bool:
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


def _bouton(app_url: str | None, libelle: str, brand: str = "oto") -> str:
    """Le bouton d'ouverture à la marque du DESTINATAIRE, ou RIEN.

    Le lien d'un projet dépend d'un patron déclaré par le tenant : le produit du
    partenaire n'a pas forcément cette vue, et coller NOTRE chemin sous SON domaine
    fabriquerait un lien mort — pire qu'une absence, parce qu'un lien mort ne se
    diagnostique pas, il se subit (cf. `links.py`). Un email est un lien AFFICHÉ, pas
    une redirection : on n'écrit rien plutôt que d'envoyer quelque part."""
    return _charte.bouton(_charte.marque(brand), app_url, libelle)


# Les 4 gabarits transactionnels (texte + locale FR/EN) vivent dans
# `email_templates.py` — extraits pour tenir sous 500 lignes une fois la
# version anglaise ajoutée (oto-backend#700). Réexposés ICI pour que
# `email.send_invite_email` etc. restent des attributs du module `email`
# (c'est ce que les tests monkeypatchent) — import placé APRÈS `_send`,
# `_esc` et `_bouton` ci-dessus, dont `email_templates` dépend.
from .email_templates import (  # noqa: E402,F401 — réexport intentionnel
    send_invite_email,
    send_resource_shared_email,
    send_resource_transferred_email,
    send_signal_digest_email,
)


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
    brand: str = "oto",
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
    brand: str = "oto",
    locale: str | None = None,
    unsubscribe_url: str | None = None,
) -> bool:
    """Envoie un email à contenu libre (fourni par l'agent), rendu à la charte de
    `brand`, via le mailer Otomata (Scaleway TEM).

    `from_email`/`from_name` = adresse expéditrice (défaut = marque `_MAIL_FROM`) ;
    le domaine doit être dans l'allowlist du service. `reply_to` défaut = la boîte
    du studio (`OTO_CONTACT_TO`). `image_url`/`image_alt` = l'image de tête (cf.
    `render_composed_email`). True si envoyé, False sinon (best-effort)."""
    html = render_composed_email(body, cta_text=cta_text, cta_url=cta_url, footer=footer,
                                 image_url=image_url, image_alt=image_alt, brand=brand,
                                 locale=locale, unsubscribe_url=unsubscribe_url)
    # `require_env` seulement si `reply_to` est absent (court-circuit `or`) — sans
    # elle, le repli irait vers NOTRE boîte personnelle (#968).
    rt = reply_to or _contact_to()
    return _send(to, subject, html, reply_to=rt, from_email=format_from(from_email, from_name))
