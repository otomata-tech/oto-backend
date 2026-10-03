"""DDL du store : l'ASSEMBLAGE des fragments par domaine.

Le DDL lui-même vit dans `db/schema/<domaine>.py` (un module par domaine) ; ce
fichier ne fait qu'une chose, et elle est structurante : il les concatène dans un
**ordre explicite et figé**. PostgreSQL crée les tables dans l'ordre du DDL — une
FK vers une table pas encore créée échoue sur une base VIERGE (#151 sur `orgs`,
même panne guettée par `tests/test_tenant_l1_migration.py` pour `tenants`).
L'ordre ci-dessous n'est donc pas une mise en page, c'est une contrainte
d'exécution : y toucher se vérifie contre une base neuve.

`CREATE TABLE IF NOT EXISTS` seulement — les évolutions de colonnes sur tables
existantes vivent dans `_init.init_db`, jamais ici (`docs/live-migrations.md`).

⚠️ Réordonner, ajouter ou réécrire un fragment change la chaîne servie : chaque
fragment est figé dans `tests/schema_gele/<module>.<CONSTANTE>.sql` (sans ses
commentaires) et cet ordre dans `tests/schema_gele/ORDRE`, que
`tests/test_schema_assembly_frozen.py` compare au servi. Un tel changement est un acte
délibéré — `python -m scripts.schema_gele --regen` en local, et le diff des fichiers
figés dans le même commit — jamais l'effet de bord d'un déplacement de fichier.
"""
from __future__ import annotations

from . import schema

# Ordre de CRÉATION des tables — ne pas réordonner sans vérifier les FK.
ASSEMBLAGE: tuple[str, ...] = (
    schema.users.USERS,              # identité (table racine)
    schema.tenants.TENANTS,          # palier tenant (ADR 0052), créé avant `orgs`
    schema.orgs.ORGS,                # table racine `orgs`, créée avant ses référents
    schema.usage.USAGE,              # compteurs, journal d'appels, signaux d'usage
    schema.runs.RUNS,                # runs, fil de messages, jobs et déclencheurs
    schema.visibility.TOOL_TOGGLES,  # bascules de visibilité d'outils
    schema.users.PROFILE,            # fiche profil du compte
    schema.legal.LEGAL,              # acceptations et documents légaux
    schema.connectors.ACL,           # RBAC connecteur interne à l'org
    schema.datastore.DATASTORE,      # namespaces et lignes du datastore
    schema.datastore.REVISIONS,      # journal des révisions de ligne (oto#273)
    schema.projects.PROJECTS,        # projets, pages, révisions, liens
    schema.embeddings.EMBEDDINGS,    # vecteurs (pages, sources aux, chunks, lignes)
    schema.projects.PROJECT_FILES,   # activité et fichiers d'un projet
    schema.grants.GRANTS,            # partages de ressources possédées
    schema.tokens.TOKENS,            # jetons d'API et d'upload
    schema.unipile.UNIPILE,          # comptes messagerie hébergés et leurs prêts
    schema.orgs.MEMBERSHIP,          # appartenance et invitations
    schema.guides.GUIDES,            # instructions plateforme (#50) — les guides sont des nœuds
    schema.nodes.NODES,              # nœuds de contenu et blocs
    schema.procedures.PROCEDURES,    # procédures d'org, révisions, bibliothèque
    schema.orgs.GROUPS,              # équipes et leurs membres
    schema.unipile.UNIPILE_GROUP_GRANTS,  # grant de compte connecteur cible groupe (#55)
    schema.connectors.CREDENTIALS,   # coffre des credentials (ADR 0002/0033)
    schema.connectors.INSTANCES,     # instances de connecteur (blueprint 0053-D9, L6)
    schema.billing.OPTION_COMPS,     # options offertes
    schema.users.ALIASES,            # alias de sub (bascule de compte)
    schema.connectors.SCHEMAS,       # schémas d'outils mis en cache
    schema.emails.EMAILS,            # envois programmés
    schema.outreach.OUTREACH,        # relances de plateforme et refus de recevoir
    schema.portee.PORTEE,            # élargissements de portée par un agent (ADR 0068)
    schema.alertes.ALERTES,          # clés retirées sous des agents programmés (oto#59)
    schema.origine.ORIGINE,          # qui pose la couche origine (oto#70 lot 2)
    schema.billing.SUBSCRIPTIONS,    # abonnements et paiements (ADR 0043)
    schema.billing.IDENTITIES,       # identité de facturation par org (#486)
    schema.billing.INVOICES,         # factures et avoirs émis chez Pennylane (#488)
    schema.functions.FUNCTIONS,      # fonctions : code pur exécuté par Oto (ADR 0073)
    schema.transcription.TRANSCRIPTION_JOBS,  # file des travaux de transcription (ADR 0074)
    schema.entitlements.ORG_ENTITLEMENTS,  # droits déclarés par org (ADR 0070 §7)
    schema.usage.JOURNAL_ARCHIVES,   # mois du journal archivés au froid (#665)
    schema.billing.CONTRACTS,        # abonnements réglés hors plateforme (#806)
    schema.runs.MODEL_SUBSCRIPTION_LIMITS,  # plafond de consommation des abonnements, par org
    schema.runs.MODEL_SUBSCRIPTION_POOL,  # pool d'org des abonnements : mode et prêts
    schema.runs.MODEL_SUBSCRIPTION_REPLI,  # repli d'un abonnement épuisé sur la clé de l'org
    schema.orgs.MEMBER_EVENTS,       # journal des entrées et sorties de membres (oto#145)
    schema.connectors.APOLLO_PHONE_REVEALS,  # téléphones révélés par Apollo, reçus par oto
    schema.orgs.INVITATIONS_RESSOURCE,  # partage en attente d'un objet vers une adresse sans compte
    schema.procedures.PROCESS_SHARES,  # partage d'une procédure par lien, et ses lecteurs
)

_SCHEMA = "".join(ASSEMBLAGE)
