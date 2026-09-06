"""Déclaration de registre du connecteur `http`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# Client HTTP multi-auth : contrairement au bridge, oto DÉTIENT le secret de
# l'API cible (coffre AES, byo_org) et tape l'API directement (pas de service
# distant). `auth_mode` discrimine le mode (bearer/header/query/basic/oauth2/
# none) ; les champs secrets requis dépendent du mode (validés au call-time par
# oto_http.build_auth).
# À DISTINGUER du bridge (credential hors plateforme) : ici la clé est confiée
# à oto — pas de custody côté client.
#
# ⚠️ Ce commentaire a annoncé « lecture seule (GET), garde-fou anti-SSRF sur
# l'hôte » jusqu'au 2026-08-27 — DEUX affirmations fausses (oto-backend#449) : le
# connecteur porte aussi `http_post`, et aucune garde SSRF n'existait. La seconde
# a ensuite été présentée comme voulue, au motif que le filtrage d'egress de la
# plateforme compensait ; il ne bloque qu'une plage (le lien-local), la boucle
# locale et les plages privées restaient joignables. La garde existe désormais
# dans le code — `oto_mcp/egress.py`, appelée par `tools/http.py:_client()` —
# et une destination interne légitime se déclare comme exception NOMMÉE.
# Jeu FERMÉ des modes d'auth. RECOPIÉ de `oto.tools.http.AUTH_MODES` — pas importé :
# le registre reste pur (aucune dépendance runtime au niveau module, sinon une dép
# absente retirerait le connecteur du catalogue au lieu de le dégrader). La copie est
# tenue par le tripwire `test_http_auth_modes.py`, qui la compare à oto-core ET vérifie
# que les champs déclarés requis par mode sont EXACTEMENT ceux que `build_auth` exige.
AUTH_MODES = ("bearer", "header", "query", "basic", "oauth2", "none")

CONNECTOR = _c(
    "http", ["http"], auth_modes={"byo_org"}, secret_kind="fields",
    label="HTTP",
    # `auth_mode` SÉLECTIONNE les autres champs (oto-backend#449) : un formulaire
    # `bearer` n'a pas à montrer les six champs d'oauth2 et de basic, et l'écriture
    # refuse un mode incohérent au lieu de l'accepter puis d'échouer au 1er appel.
    field_discriminator="auth_mode",
    help="connecte n'importe quelle API HTTP à oto : renseigne l'URL de base, "
         "le mode d'auth (bearer / clé en header ou query / basic / oauth2) et "
         "le secret correspondant. oto stocke le secret (coffre chiffré) et tape "
         "l'API directement, en lecture (GET) comme en écriture (POST).",
    # `when=` = les modes qui rendent le champ PERTINENT ; `required` s'applique alors
    # DANS ces modes-là. Les trois champs sans `when` valent quel que soit le mode.
    credential_fields=(
        CredentialField("base_url", "URL de base", secret=False,
                        help="racine de l'API (ex. https://api.acme.com). `http://` "
                             "est accepté. Une adresse INTERNE (boucle locale, "
                             "réseau privé) est refusée tant qu'elle n'a pas été "
                             "déclarée comme exception nommée par l'opérateur de "
                             "la plateforme — le refus dit comment"),
        CredentialField("auth_mode", "Mode d'auth", secret=False,
                        choices=AUTH_MODES,
                        help="ce que l'API attend pour t'authentifier — il décide des "
                             "champs à remplir ensuite"),
        CredentialField("label", "Nom affiché", secret=False,
                        required=False, help="ex. « API Acme » — visible de ta seule org"),
        CredentialField("doc_path", "Route de doc (optionnel)", secret=False,
                        required=False,
                        help="chemin relatif à base_url qui rend la documentation "
                             "de l'API (ex. /openapi.json) — sert le tool `http_doc`, "
                             "quand renseigné. Même auth que le reste, pas de route "
                             "publique à part"),
        CredentialField("token", "Token / clé API", secret=True,
                        when=("bearer", "header", "query"),
                        help="valeur du bearer, ou de la clé (modes header/query)"),
        CredentialField("header_name", "Nom du header", secret=False,
                        when=("header",), help="ex. x-api-key"),
        CredentialField("query_param", "Nom du param", secret=False,
                        when=("query",), help="ex. api_key"),
        CredentialField("username", "Utilisateur", secret=False,
                        when=("basic",), help="identifiant du couple basic"),
        CredentialField("password", "Mot de passe", secret=True, when=("basic",),
                        whitespace_significant=True, help="mot de passe du couple basic"),
        CredentialField("token_url", "URL du token", secret=False,
                        when=("oauth2",), help="endpoint client-credentials"),
        CredentialField("client_id", "Client ID", secret=False,
                        when=("oauth2",), help="identifiant de l'application cliente"),
        CredentialField("client_secret", "Client secret", secret=True,
                        when=("oauth2",), help="secret de l'application cliente"),
        CredentialField("scope", "Scope", secret=False,
                        when=("oauth2",), required=False,
                        help="scopes demandés au serveur de token (optionnel)"),
    ),
)

# Éditeur : le connecteur est le NÔTRE, et il n'y a pas d'intermédiaire à nommer — oto
# détient le secret et tape DIRECTEMENT l'API que l'org a configurée (pas de service
# distant). Le host appelé est celui du `base_url` de chaque credential : il n'existe
# pas au niveau du registre, et rien ne se fait passer pour rien. DÉCLARÉ, et pas dérivé
# d'un défaut : depuis le 2026-09-02 il n'y en a plus (`Connector.publisher_name`).
PUBLISHER = "Otomata"
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "Connecter n'importe quelle API HTTP à oto en confiant son secret au coffre "
    ": renseigne l'URL de base, choisis le mode d'authentification (bearer, "
    "header, query, basic, OAuth2 ou aucun), et l'agent l'appelle en GET ou en "
    "POST. Utile pour une API interne ou un service tiers sans connecteur dédié "
    "— la clé reste chez oto, contrairement à un bridge où le tiers garde la "
    "sienne."
)
