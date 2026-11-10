"""Les SECRETS parmi les variables inventoriées — ce qui ne s'écrit jamais dans un `.env`.

oto-backend#967. L'inventaire (`env_inventory.py`) dit ce qu'une instance LIT ; ce module
dit lesquelles de ces lectures sont des secrets, pour deux lecteurs qui ne doivent pas
diverger : le lanceur générique (`deploy/lanceur_secrets.py`), qui les tire du
gestionnaire de secrets de l'instance au démarrage, et la déclaration d'une instance
tierce (`deploy/cible/declaration.py`), qui refuse d'en voir un dans son `.env`.

Pur (aucune dépendance hors bibliothèque standard, via l'inventaire) : il est importé sur
la machine cible par le Python du système, avant qu'aucun venv n'existe.
"""
from __future__ import annotations

from oto_mcp import env_inventory as inv

# Une variable est un secret quand sa valeur OUVRE quelque chose : une base, un bucket,
# le coffre, un compte chez un tiers. Le lanceur générique (`deploy/lanceur_secrets.py`)
# tire ces valeurs du gestionnaire de secrets de l'instance au démarrage, et refuse d'en
# trouver une déjà dans l'environnement : le `.env` d'une instance ne porte que le
# non-secret. La liste est ICI, à côté de l'inventaire dont elle ne nomme que des
# entrées (`tests/test_env_secrets_967.py`), pour qu'il n'en existe pas une
# seconde dans un script de déploiement qui divergerait au premier secret ajouté.
#
# - REQUISE ∩ SECRETS : tirés à chaque démarrage, un manquant refuse le démarrage ;
# - REGLAGE ∩ SECRETS : facultatifs — une instance DÉCLARE ceux qu'elle porte, et un
#   secret déclaré mais introuvable refuse aussi (jamais d'absence muette).
SECRETS: frozenset[str] = frozenset({
    "DATABASE_URL",
    "OTO_MCP_S3_ACCESS_KEY",
    "OTO_MCP_S3_SECRET_KEY",
    "OTO_MCP_MASTER_KEY",
    "OTO_MCP_OAUTH_STATE_SECRET",
    "GOOGLE_WORKSPACE_CLIENT_SECRET",
    "FOD_API_TOKEN",
    "OTO_FERME_TOKEN",
    "LOGODEV_TOKEN",
    "BLS_API_KEY",
    "BROWSERBASE_API_KEY",
    "MISTRAL_API_KEY",
    "OTO_ANTHROPIC_ADMIN_KEY",
    "OTO_SCRUB_MODEL_KEY",
    "OTO_MAILER_SEND_BEARER",
    "OTO_TEM_SECRET_KEY",
    "MOLLIE_API_KEY",
    "OTO_LOGTO_WEBHOOK_SIGNING_KEY",
    "OTO_SENTRY_DSN",
    "OTO_EXPORT_CLE_CIBLE",
})

# Familles dynamiques dont chaque membre est un secret (la moitié `_SECRET` d'un
# credential de management d'annuaire, `OTO_MCP_LOGTO_M2M_SECRET` pour le primaire).
FAMILLES_SECRETES: frozenset[str] = frozenset({"credential_management_annuaire_secret"})

# Secrets qui n'entrent JAMAIS dans l'environnement du serveur : la clé de la cible d'un
# export ne vit que le temps d'une commande (`oto-mcp perimetre export`).
HORS_SERVEUR: frozenset[str] = frozenset({"OTO_EXPORT_CLE_CIBLE"})


def est_secret(nom: str) -> bool:
    """`nom` est-il un secret — nommément, ou comme membre d'une famille secrète ?"""
    if nom in SECRETS:
        return True
    return any(f.nom in FAMILLES_SECRETES and nom.endswith(f.suffixe)
               and nom.startswith(f.prefixe)
               and len(nom) > len(f.prefixe) + len(f.suffixe)
               for f in inv.FAMILLES_DYNAMIQUES)


def secrets_requis() -> tuple[str, ...]:
    """Les secrets sans lesquels une instance ne démarre pas, dans l'ordre de
    l'inventaire — ce que le lanceur tire toujours."""
    return tuple(v.nom for v in inv.NOMS_FIXES
                 if v.classe is inv.Classe.REQUISE and v.nom in SECRETS)
