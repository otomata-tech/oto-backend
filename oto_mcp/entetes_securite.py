"""En-têtes de sécurité des pages HTML que le backend rend LUI-MÊME (oto-backend#565).

**Partage des rôles, décidé une fois** : ce qui vaut pour toute réponse d'un domaine
(HSTS en tête) se pose au reverse-proxy, hors de ce dépôt ; ce qui dépend de la PAGE
— la Content-Security-Policy, qui doit dire ce que la page charge — se pose ici, à côté
du code qui écrit cette page. Une CSP écrite loin de sa page casse la page au premier
ajout d'une ressource, sans que rien ne le signale à celui qui l'ajoute.

Chaque politique ci-dessous est la LISTE de ce que sa page charge, relevée sur son
rendu (`tests/test_entetes_securite_565.py` la recoupe avec le HTML servi) :

- `default-src 'none'` partout : tout ce qui n'est pas nommé est refusé ;
- `frame-ancestors 'none'` partout : aucune de ces pages ne s'embarque (déjà
  `X-Frame-Options: DENY` là où il est posé ; la directive CSP est sa forme moderne) ;
- `base-uri 'none'` : un `<base>` injecté ne redirige aucun lien relatif ;
- `style-src 'unsafe-inline'` : les trois pages portent leur feuille dans un `<style>`
  en ligne (et la page de projet des attributs `style=`). La CSP n'y vise pas le style.
"""
from __future__ import annotations

import base64
import hashlib


def _politique(**directives: str) -> str:
    """`default-src 'none'` + les directives données (`_` → `-` dans le nom)."""
    base = {"default_src": "'none'", "base_uri": "'none'", "frame_ancestors": "'none'"}
    return "; ".join(f"{k.replace('_', '-')} {v}" for k, v in {**base, **directives}.items())


def empreinte_script(source: str) -> str:
    """La source CSP `'sha256-…'` d'un `<script>` en ligne — son contenu EXACT, entre
    les balises. Un octet changé dans le script sans recalcul ⟹ le navigateur le bloque ;
    d'où le calcul ici, sur la constante même que la page insère."""
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


# Polices Google (feuille + fichiers) et images du contenu : le markdown d'un doc ou
# d'une page de projet peut citer une image par URL absolue, les logos de connecteurs
# viennent d'ailleurs, et le favicon est une URI `data:`.
_POLICES = {"style_src": "'unsafe-inline' https://fonts.googleapis.com",
            "font_src": "https://fonts.gstatic.com"}
_IMAGES = "'self' https: data:"

# Page de partage d'un doc (`/p/d/<jeton>`, `public_doc_page`) : AUCUN script — le
# markdown est rendu en mode sûr, et cette politique en fait une seconde ligne : un
# script qui passerait l'échappement ne s'exécuterait pas.
CSP_DOC_PUBLIC = _politique(**_POLICES, img_src=_IMAGES, form_action="'none'")

# UI navigable d'un projet publié (`share_ui`, sous-domaine) : un script de tableau en
# ligne ET des gestionnaires en attribut (`onclick`, `oninput`, `onerror`), que seule
# `'unsafe-inline'` autorise. ⚠️ La CSP n'y est donc PAS une défense contre un script
# injecté en ligne — elle borne le reste : aucun script ni requête vers un tiers
# (`connect-src` absent ⟹ `'none'`), aucun formulaire, aucun cadre.
CSP_PROJET_PUBLIE = _politique(**_POLICES, img_src=_IMAGES,
                               script_src="'unsafe-inline'", form_action="'none'")


def csp_upload_large(script: str, origines: list[str]) -> str:
    """Formulaire d'un GROS fichier : comme `csp_upload`, plus les origines du stockage
    où le script envoie chaque partie (URL présignées)."""
    return _politique(script_src=empreinte_script(script), style_src="'unsafe-inline'",
                      connect_src=" ".join(["'self'", *origines]), form_action="'self'")


def csp_upload(script: str) -> str:
    """Formulaire d'upload signé (`api/uploads`) : UN script en ligne, autorisé par son
    empreinte ; il poste le fichier en `fetch` vers la même URL (`connect-src 'self'`) ;
    le `<form>` n'est jamais soumis nativement, mais il reste sur l'origine s'il l'était."""
    return _politique(script_src=empreinte_script(script), style_src="'unsafe-inline'",
                      connect_src="'self'", form_action="'self'")
