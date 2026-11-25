"""Le contrôle AUTOMATIQUE de ce qui part dans la communauté (bibliothèque publique).

Publier dans la communauté rend un contenu lisible par tous les comptes, et ses
métadonnées par le web entier (`/api/public/community/*`, construit au build du site).
Ce qui se publie par erreur ne se reprend pas : retirer une entrée n'efface pas ce qui a
été lu. Le contrôle REFUSE donc, avant toute écriture, un contenu qui porte ce qui
n'a rien à faire dans un modèle partagé :

- une adresse email (hors domaines réservés à l'exemple, RFC 2606 / RFC 6761) ;
- un numéro de téléphone ;
- un jeton ou une clé d'API (formes connues : `sk-…`, `xox…-`, `Bearer …`, `ghp_…`,
  `AKIA…`, `AIza…`, JWT, `oto_…`, et les longues chaînes hex/base64 de forme secrète) ;
- une adresse web qui porte un secret (identifiants `user:pass@`, ou un paramètre de
  requête nommé comme un secret : `token`, `key`, `signature`…).

Ce qui a la forme d'un secret sans en être un dans le HTML d'une app — une empreinte
d'intégrité (`integrity="sha384-…"`) ou une ressource `data:…;base64,…` — n'est pas
compté (relevé sur les modèles d'app du front : sans cette exception, une app qui charge
une bibliothèque de CDN avec son empreinte serait refusée).

Fonction PURE, sans I/O : elle ne voit que les textes qu'on lui donne et ne rend que des
CATÉGORIES — jamais la valeur trouvée, qui est précisément ce qu'on ne doit pas
recopier (ni dans un message d'erreur, ni dans un journal).

Ce n'est pas une modération : c'est un filet contre la fuite involontaire. La
modération (retirer une entrée après coup) appartient au personnel de la plateforme
(`library.moderate`). Un contenu qui passe ce contrôle n'est pas « approuvé ».
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

# Les catégories rendues, et leur libellé lisible (le message d'erreur les nomme).
EMAIL, TELEPHONE, SECRET, URL_SECRETE = "email", "phone", "secret", "url_with_secret"
LIBELLES = {
    EMAIL: "une adresse email",
    TELEPHONE: "un numéro de téléphone",
    SECRET: "une clé ou un jeton d'API",
    URL_SECRETE: "une adresse web qui porte un secret (identifiants ou jeton dans l'URL)",
}

_RE_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# Domaines réservés à l'exemple : `prenom@example.com` dans un modèle n'est la donnée
# de personne.
_DOMAINES_EXEMPLE = ("example.com", "example.org", "example.net", "example.fr")
_TLD_EXEMPLE = (".example", ".test", ".invalid", ".localhost")

# Téléphones : international (+33 6 12 34 56 78, +1-415-555-0100) et national français
# (06 12 34 56 78, 06.12.34.56.78, 0612345678). Bornés par des non-chiffres : un SIRET ou
# un identifiant long n'est pas un numéro.
_RE_TEL = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?(?:\(0\)[\s.-]?)?\d{1,4}(?:[\s.-]?\d{2,4}){2,4}"
    r"|0[1-9](?:[\s.-]?\d{2}){4})(?![\d])")

_RE_SECRETS = [
    re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\boto_[A-Za-z0-9_-]{20,}"),
    # Longue chaîne hexadécimale (clé, empreinte d'accès) : 32 caractères et plus.
    re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])"),
]
# Longue chaîne base64/base64url : un secret si elle MÉLANGE minuscules, majuscules et
# chiffres — un mot composé, un slug ou un chemin n'en ont pas la forme.
_RE_B64 = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}")

# Ce qui a la FORME d'un secret sans en être un, dans le HTML d'une app : une empreinte
# d'intégrité de sous-ressource (`integrity="sha384-…"`, publique par construction) et une
# ressource embarquée (`data:image/png;base64,…`). Retirés avant de chercher les secrets.
_RE_NEUTRES = re.compile(
    r"\b(?:sha256|sha384|sha512)-[A-Za-z0-9+/]{40,}={0,2}"
    r"|\bdata:[\w.+-]+/[\w.+-]+(?:;[\w.+-]+=[\w.+-]+)*;base64,[A-Za-z0-9+/]+={0,2}", re.I)

_RE_URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+", re.I)
_PARAMS_SECRETS = re.compile(
    r"^(?:.*[_-])?(?:token|access_token|api_?key|apikey|key|secret|client_secret|"
    r"password|passwd|pwd|auth|signature|sig|code|session|sid|x-amz-signature|"
    r"x-amz-credential)$", re.I)


def _email_reel(domaine: str) -> bool:
    d = domaine.lower()
    if d in _DOMAINES_EXEMPLE or any(d.endswith("." + x) for x in _DOMAINES_EXEMPLE):
        return False
    return not d.endswith(_TLD_EXEMPLE)


def _b64_secret(s: str) -> bool:
    if "/" in s and s.count("/") > 2:      # un chemin, pas une clé
        return False
    return (any(c.islower() for c in s) and any(c.isupper() for c in s)
            and sum(c.isdigit() for c in s) >= 4)


def _url_secrete(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.username or parts.password:
        return True
    for nom, valeur in parse_qsl(parts.query, keep_blank_values=False):
        if _PARAMS_SECRETS.match(nom) and len(valeur) >= 8:
            return True
    return False


def categories(texte: str) -> list[str]:
    """Les catégories de contenu refusé que porte `texte`, sans doublon, dans un ordre
    stable. `[]` = rien à signaler. Ne rend JAMAIS la valeur trouvée."""
    t = texte or ""
    trouve: list[str] = []

    def _ajout(cat):
        if cat not in trouve:
            trouve.append(cat)

    for u in _RE_URL.findall(t):
        if _url_secrete(u):
            _ajout(URL_SECRETE)
    # Les URL jugées à part : leur chemin ou leur requête ne doit pas être relu comme
    # un téléphone (`/v1/0612345678`) ni comme un email (`user:pass@host`).
    sans_url = _RE_URL.sub(" ", t)
    if any(_email_reel(m.group(1)) for m in _RE_EMAIL.finditer(sans_url)):
        _ajout(EMAIL)
    if _RE_TEL.search(sans_url):
        _ajout(TELEPHONE)
    neutre = _RE_NEUTRES.sub(" ", t)
    if (any(r.search(neutre) for r in _RE_SECRETS)
            or any(_b64_secret(m.group(0))
                   for m in _RE_B64.finditer(_RE_NEUTRES.sub(" ", sans_url)))):
        _ajout(SECRET)
    return [c for c in (URL_SECRETE, EMAIL, TELEPHONE, SECRET) if c in trouve]


def inspecter(champs: dict[str, str]) -> list[dict]:
    """Inspecte chaque champ nommé (`{"title": …, "body_md": …}`) : rend la liste des
    constats `{"field", "kind"}`, vide si tout passe. Ni valeur ni extrait."""
    return [{"field": nom, "kind": cat}
            for nom, texte in champs.items() for cat in categories(texte or "")]


def message_de_refus(constats: list[dict]) -> str:
    """La phrase du refus : CE QUI a été trouvé et OÙ, jamais la valeur."""
    par_champ: dict[str, list[str]] = {}
    for c in constats:
        par_champ.setdefault(c["field"], []).append(LIBELLES[c["kind"]])
    detail = " ; ".join(f"{champ} : {', '.join(cats)}" for champ, cats in par_champ.items())
    return ("Publication refusée — le contenu porte ce qui n'a rien à faire dans un "
            f"modèle partagé ({detail}). Retire-le ou remplace-le par un exemple "
            "(`prenom@example.com`, `<slot:…>`, une clé posée dans le coffre), puis "
            "republie.")
