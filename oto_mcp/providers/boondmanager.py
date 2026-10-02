"""Déclaration de registre du connecteur `boondmanager`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# boondmanager : le CRM/ERP des ESN et sociétés de conseil. Surface ÉTROITE :
# recherche, lecture et création de contacts, sociétés, opportunités et actions —
# ni mise à jour, ni suppression.
#
# Auth `X-Jwt-Client-BoondManager` à TROIS champs (`secret_kind="fields"`,
# résolus par `access.resolve_credential_fields`) : le client signe un JWT par
# appel avec la `client_key`. `client_token` est déclaré NON secret : il identifie
# le compte Boond sans rien signer. L'accès à l'API REST doit être autorisé dans
# le compte Boond, sinon tout appel est refusé.
#
# BYOK strict (`byo_user` + `byo_org`) : c'est le CRM du client, et chaque appel
# consomme son quota MENSUEL d'API Boond.
CONNECTOR = _c(
    "boondmanager", ["boondmanager"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "client_token", "Client token", secret=False,
            help="Boond → interface administrateur → tableau de bord (espace "
                 "développeur / API)."),
        CredentialField(
            "client_key", "Client key", secret=True,
            help="Même écran que le client token : la clé qui signe les appels."),
        CredentialField(
            "user_token", "User token", secret=True,
            help="Boond → paramètres de l'utilisateur → sécurité. Les appels "
                 "agissent avec les droits de cet utilisateur ; l'accès à l'API "
                 "REST doit y être autorisé."),
    ),
    label="BoondManager",
    help="CRM des ESN : contacts, sociétés, opportunités, actions — recherche, "
         "lecture, création",
    href="https://www.boondmanager.com",
)

CATEGORY = "Prospection"
PUBLISHER = "BoondManager"
LOGO_DOMAIN = "boondmanager.com"

DESCRIPTION = (
    "Le CRM d'une ESN ou d'une société de conseil dans BoondManager : chercher "
    "et lire contacts, sociétés, opportunités et actions, et en créer — jamais "
    "de modification ni de suppression. Trois jetons pris dans le compte Boond, "
    "qui doit autoriser l'accès à l'API."
)
