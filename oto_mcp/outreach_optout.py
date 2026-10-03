"""Le lien de désinscription d'une relance — et, depuis oto#150, du DIGEST de
signaux : signer, vérifier, adresser.

Un jeton signé (HMAC-SHA256) qui scelle un `sub`, et rien d'autre. Même patron que
`upload_tokens` — même secret d'instance (`OTO_MCP_OAUTH_STATE_SECRET`), même
encodage — avec **une différence assumée : pas d'expiration**.

Un lien de désinscription qui périme n'est pas un lien de désinscription. Le mail
qu'on relit six mois plus tard est précisément celui dont on ne veut plus, et un
« ce lien a expiré » à ce moment-là transforme un refus en corvée. Ce que le jeton
autorise borne le risque : cesser de recevoir NOS relances (ou NOS résumés de
signaux), pour un compte que l'appelant devait déjà connaître. Il n'ouvre aucune
lecture, aucune écriture d'org, et ne se rejoue pas en autre chose.

⚠️ **Le secret d'instance est ce qui le tient.** Sans `OTO_MCP_OAUTH_STATE_SECRET`,
on ne fabrique pas de lien : `lien()`/`lien_digest()` lèvent, et l'envoi refuse
plutôt que de partir avec un pied de page qui ne mène nulle part.

⚠️ **`verify()`/`verify_digest()` lèvent aussi dans ce cas — délibérément, et c'est
le seul endroit où ce module ne se tait pas.** Rendre `None` ferait afficher « lien
invalide » à quelqu'un dont le lien est parfaitement valide : son refus serait
perdu, et la faute lui serait attribuée. Un secret absent est une panne de serveur ;
elle doit se voir comme telle (500 bruyant, Sentry) et pas comme une erreur de
l'utilisateur. Toutes les AUTRES causes de rejet — signature fausse, forme
illisible, mauvais `typ` — rendent bien `None` : celles-là ne sont pas de notre fait.

⚠️ **Deux canaux, deux `typ`, jamais interchangeables (oto#150).** La relance
(`oto_admin_outreach`) et le digest de signaux (`send_signal_digest_email`) sont
deux refus distincts, sur deux tables distinctes (`db/outreach.py::desinscrire` vs
`db/usage.py::opt_out_signal_digest`) — décision d'Alexis : se désinscrire de l'un
ne désinscrit pas de l'autre. Ils PARTAGENT la primitive de signature (même
secret, même forme) mais chacun a son `typ` scellé (`optout` / `digest_optout`) et
sa route (`/o/u/<token>` / `/o/d/<token>`) : un jeton de relance ne vérifie jamais
comme un jeton de digest, et réciproquement — `verify()` ne rend un `sub` que pour
`_TYP`, `verify_digest()` que pour `_TYP_DIGEST`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Optional

from . import config

_TYP = "optout"                  # relance de plateforme (oto_admin_outreach) — inchangé
_TYP_DIGEST = "digest_optout"    # digest de signaux (send_signal_digest_email, oto#150)
_TYP_LECTEURS = "readers_digest_optout"  # résumé des lecteurs d'une procédure partagée


class OptOutSecretManquant(RuntimeError):
    """Pas de secret d'instance : aucun lien signable, donc aucun envoi."""


def _secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise OptOutSecretManquant(
            "OTO_MCP_OAUTH_STATE_SECRET manquant : impossible de signer un lien de "
            "désinscription, donc impossible d'envoyer une relance.")
    return v.encode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(sub: str, typ: str) -> str:
    payload = json.dumps({"typ": typ, "sub": sub},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def sign(sub: str) -> str:
    """Le jeton de désinscription d'une RELANCE. **Stable** : le même sub rend
    toujours le même jeton — un renvoi ne fabrique pas un second lien à révoquer."""
    return _sign(sub, _TYP)


def _verify(token: str, typ: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    p_b64, sig_b64 = token.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    # noqa: SILENT — fail-closed : toute erreur de vérification ⇒ jeton refusé
    except Exception:
        return None
    if not hmac.compare_digest(sig, hmac.new(_secret(), payload, hashlib.sha256).digest()):
        return None
    try:
        data = json.loads(payload)
    # noqa: SILENT — fail-closed : toute erreur de vérification ⇒ jeton refusé
    except Exception:
        return None
    if data.get("typ") != typ:
        return None
    sub = data.get("sub")
    return str(sub) if sub else None


def verify(token: str) -> Optional[str]:
    """Le `sub` scellé si `token` est un jeton de désinscription de RELANCE valide,
    `None` sinon. Fail-closed sur toute erreur de forme (dont un `typ` d'un autre
    canal — un jeton de digest ne vérifie jamais ici, cf. `verify_digest`)."""
    return _verify(token, _TYP)


def verify_digest(token: str) -> Optional[str]:
    """Comme `verify`, pour la désinscription du DIGEST de signaux (oto#150) —
    jamais interchangeable : un jeton de relance ne vérifie jamais ici."""
    return _verify(token, _TYP_DIGEST)


def verify_lecteurs(token: str) -> Optional[str]:
    """Comme `verify`, pour la désinscription du RÉSUMÉ DES LECTEURS d'une procédure
    partagée — troisième canal, troisième `typ` : jamais interchangeable."""
    return _verify(token, _TYP_LECTEURS)


def lien(sub: str) -> str:
    """L'adresse complète servie dans le pied du mail de RELANCE.

    Sur le BACKEND (`OTO_MCP_PUBLIC_URL`), pas sur le dashboard : la désinscription
    doit fonctionner sans session, sans JavaScript et sans que le front soit déployé —
    c'est le même argument que la page publique d'un doc partagé (`/p/d/<token>`).

    L'adresse se DÉCLARE et ne se devine pas : `config.public_base_url()` lève plutôt
    que de retomber sur un domaine. Un lien de désinscription envoyé mort ne se rattrape
    pas — le destinataire ne le reclique pas, il classe l'expéditeur.
    """
    return f"{config.public_base_url()}/o/u/{_sign(sub, _TYP)}"


def lien_digest(sub: str) -> str:
    """L'adresse complète servie dans le pied du DIGEST de signaux (oto#150) —
    même forme que `lien()`, route et `typ` distincts (cf. l'en-tête du module) :
    ce lien ne désinscrit jamais des relances, quoi qu'il arrive."""
    return f"{config.public_base_url()}/o/d/{_sign(sub, _TYP_DIGEST)}"


def lien_lecteurs(sub: str) -> str:
    """L'adresse servie dans le pied du RÉSUMÉ DES LECTEURS (`digest_lecteurs.py`) —
    même forme que `lien_digest()`, route `/o/r/` et `typ` à lui."""
    return f"{config.public_base_url()}/o/r/{_sign(sub, _TYP_LECTEURS)}"


# La page rendue au destinataire. Server-rendered, sans JS, sans marque tierce : elle
# confirme, elle ne propose rien d'autre. Idempotente — la recharger n'est pas une
# erreur, et le dire évite qu'on la reclique en pensant que ça n'a pas marché.
_PAGE = """<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titre}</title></head>
<body style="margin:0;background:#faf9f7;font-family:-apple-system,BlinkMacSystemFont,\
'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1917">
<div style="max-width:520px;margin:12vh auto;padding:28px 32px;background:#fff;\
border:1px solid #e7e5e4;border-radius:12px">
<p style="margin:0 0 12px;font-size:15px;font-weight:600">{titre}</p>
<p style="margin:0;font-size:15px;line-height:1.6">{corps}</p>
</div></body></html>"""

_TEXTES = {
    "relance": {
        "fr": ("C'est noté",
               "Vous ne recevrez plus nos messages de relance. Les emails liés à "
               "votre compte (invitations, partages) continuent d'arriver : ils ne "
               "sont pas de la relance."),
        "en": ("Done",
               "You will not receive our follow-up messages any more. Emails tied "
               "to your account (invitations, shares) still come through: those "
               "are not follow-ups."),
    },
    "digest": {
        "fr": ("C'est noté",
               "Vous ne recevrez plus le résumé des retours à vos signaux. Les "
               "emails liés à votre compte (invitations, partages) continuent "
               "d'arriver : ils ne sont pas ce résumé."),
        "en": ("Done",
               "You will not receive the summary of replies to your reported "
               "signals any more. Emails tied to your account (invitations, "
               "shares) still come through: those are not this summary."),
    },
    "lecteurs": {
        "fr": ("C'est noté",
               "Vous ne recevrez plus le résumé des personnes qui lisent vos "
               "procédures partagées. La liste reste visible dans l'onglet Readers "
               "de chaque procédure."),
        "en": ("Done",
               "You will not receive the summary of who reads your shared "
               "processes any more. The list stays visible in each process's "
               "Readers tab."),
    },
}
_REFUS = ("Lien invalide",
          "Ce lien de désinscription n'est pas valide. Répondez simplement à "
          "l'email que vous avez reçu, on s'en occupe.")


def page_confirmation(locale: Optional[str] = None, *, kind: str = "relance") -> str:
    """`kind` choisit la PROMESSE tenue par la page — `relance` (défaut, inchangé)
    ou `digest` (oto#150) : les deux canaux ne s'arrêtent pas ensemble, la page ne
    doit donc jamais dire l'inverse de ce que le lien vient de faire."""
    titre, corps = _TEXTES[kind]["en" if locale == "en" else "fr"]
    return _PAGE.format(lang="en" if locale == "en" else "fr", titre=titre, corps=corps)


def page_refus() -> str:
    titre, corps = _REFUS
    return _PAGE.format(lang="fr", titre=titre, corps=corps)
