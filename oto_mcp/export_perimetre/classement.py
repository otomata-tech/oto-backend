"""Le classement DÉCLARÉ de chaque table du schéma, face au périmètre d'un propriétaire.

Chaque table du schéma réel (celui que monte `init_db`, pas une reconstitution) a
exactement une entrée ici, dans l'une des quatre classes :

- **possédée** : une règle directe dit quelles lignes appartiennent au périmètre ;
- **indirecte** : la ligne hérite de son parent (`Via` seulement) ;
- **instance** : l'état propre de l'instance — la cible naît avec le sien, rien ne part ;
- **exclue** : la ligne appartient au propriétaire mais NE PART PAS, pour une raison
  écrite (notre commerce, ADR 0070 §3.1). La règle reste déclarée : le manifeste
  COMPTE ce qui n'est pas parti, il ne le tait pas.

Une entrée peut nommer une VUE SIMPLE (une seule table sous-jacente) plutôt que sa
table : `decouverte.verifier_classement` la résout en la table physique. C'est le cas
de la bibliothèque publique, que le code ne désigne que par sa vue.

⚠️ Une table absente d'ici est un REFUS (`decouverte.verifier_classement`), jamais un
oubli : c'est ce qui empêche l'export de rater la table ajoutée demain. Ajouter une
table au schéma, c'est la classer ici dans le même commit — le test
`tests/export_perimetre/test_classement_couvre_schema.py` rougit sinon.

`secrets` nomme les colonnes chiffrées avec NOTRE clé maîtresse (inutilisables par la
cible, et dont l'AAD contient l'identité du propriétaire) ; `hors_base` celles qui
désignent un objet de l'Object Storage, à copier à part.

`JOURNAL` nomme le JOURNAL d'appels et sa colonne d'horodatage : l'essentiel des
lignes d'un périmètre. Il peut voyager HORS de la fenêtre de coupure, par tranches de
dates (`sans_journal`, `journal`) ; l'export par défaut l'emporte avec le reste.

`comptes` nomme les colonnes-COMPTE d'une table possédée par org : la ligne est celle
d'UN compte dans l'org (sa préférence, son journal, son abonnement), et la colonne dit
lequel. Ce compte peut être un ANCIEN membre, hors périmètre : la ligne est alors
rattachée à son jumeau ou omise (`comptes`). Une colonne qui trace seulement l'AUTEUR
d'un geste sur un objet de l'org (`created_by`, `actor_sub`, `edited_by`…) n'en est pas :
l'objet est à l'org, pas à son auteur. Les règles ajoutent les leurs (`comptes_de`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..ownership import TYPE_RESSOURCE_DATASTORE, TYPE_RESSOURCE_PROCEDURE
from .regles import (Compte, Ou, ParEntite, ParGroupe, ParOrg, ParSub, ParSubSansOrg,
                     ParTenant, Regle, Via)

POSSEDEE = "possedee"
INDIRECTE = "indirecte"
INSTANCE = "instance"
EXCLUE = "exclue"
EXPORTEES = (POSSEDEE, INDIRECTE)


@dataclass(frozen=True)
class Table:
    classe: str
    regle: Regle | None = None
    raison: str = ""
    secrets: tuple[str, ...] = ()
    hors_base: tuple[str, ...] = ()
    # Un PARTAGE : la ligne ne part que si son destinataire est aussi du périmètre ;
    # sinon elle est omise, et comptée au manifeste (décision du 28/09/2026).
    destinataire: Regle | None = None
    comptes: tuple[str, ...] = ()


def possedee(regle: Regle, raison: str = "", *, secrets: tuple[str, ...] = (),
             hors_base: tuple[str, ...] = (), destinataire: Regle | None = None,
             comptes: tuple[str, ...] = ()) -> Table:
    return Table(POSSEDEE, regle, raison, secrets, hors_base, destinataire, comptes)


def indirecte(regle: Regle, raison: str = "", *, secrets: tuple[str, ...] = (),
              hors_base: tuple[str, ...] = (), destinataire: Regle | None = None,
              comptes: tuple[str, ...] = ()) -> Table:
    return Table(INDIRECTE, regle, raison, secrets, hors_base, destinataire, comptes)


def comptes_de(entree: Table) -> tuple[Compte, ...]:
    """Les colonnes-compte d'une table exportée : les déclarées, puis celles que ses
    règles connaissent (`Regle.comptes`), une fois chacune."""
    if entree.classe not in EXPORTEES:
        return ()
    tous = (*(Compte.colonne_simple(c) for c in entree.comptes), *entree.regle.comptes(),
            *(entree.destinataire.comptes() if entree.destinataire else ()))
    uniques: dict[str, Compte] = {}
    for k in tous:
        uniques.setdefault(k.colonne, k)
    return tuple(uniques.values())


def instance(raison: str) -> Table:
    return Table(INSTANCE, None, raison)


def exclue(regle: Regle, raison: str) -> Table:
    return Table(EXCLUE, regle, raison)


# `org_id` NULLABLE : la ligne sans org est celle de son compte.
_ORG_OU_COMPTE = Ou((ParOrg(), ParSubSansOrg()))
_COMMERCE = "notre commerce (ADR 0070 §3.1) : repris par oto-commerce, jamais embarqué"
_DOCS = Via("docs", ("doc_id",))
_PROJETS = Via("projects", ("project_id",))
_TABLEAUX = Via("user_datastores", ("ns_id",))

CLASSEMENT: dict[str, Table] = {
    # ── identité, orgs, équipes ────────────────────────────────────────────────
    "users": possedee(ParSub(), hors_base=("avatar_url",)),
    "user_account_profile": possedee(ParSub()),
    "user_api_tokens": possedee(ParSub(), "jetons HACHÉS : ils restent valides si la "
                                "cible accepte le même format de jeton"),
    "sub_aliases": possedee(ParSub("new_sub")),
    "orgs": possedee(ParOrg("id"), hors_base=("logo_url",)),
    "org_members": possedee(ParOrg()),
    "org_member_events": possedee(ParOrg(), comptes=("sub",)),
    "org_invitations": possedee(ParOrg()),
    "org_groups": possedee(ParOrg()),
    "org_group_members": indirecte(Via("org_groups", ("group_id",))),
    "org_disabled_tools": possedee(ParOrg()),
    "group_disabled_tools": possedee(ParGroupe()),
    "user_disabled_tools": possedee(ParOrg(), comptes=("sub",)),
    "user_enabled_tools": possedee(ParOrg(), comptes=("sub",)),
    # Le tenant du périmètre PART (décision du 28/09/2026) : il devient la ligne 1 de
    # la cible, les clés qui le désignent y sont remappées (`importation`). Ses admins
    # partent s'ils sont des comptes du périmètre ; les nôtres restent.
    "tenants": possedee(ParTenant()),
    "tenant_admins": possedee(ParSub()),
    "tenant_legal_docs": indirecte(Via("tenants", ("tenant_slug",), ("slug",), fk=False)),
    # ── contenu : projets, pages, tableaux, nœuds, procédures, fonctions ───────
    "projects": possedee(ParEntite()),
    "project_activity": indirecte(_PROJETS),
    "project_links": indirecte(_PROJETS),
    "project_files": indirecte(_PROJETS, hors_base=("s3_key", "public_url")),
    "project_file_texts": indirecte(Via("project_files", ("file_id",))),
    "docs": indirecte(_PROJETS),
    "doc_revisions": indirecte(_DOCS),
    "doc_links": indirecte(Via("docs", ("from_doc",))),
    "doc_change_requests": indirecte(_DOCS),
    "doc_embeddings": indirecte(_DOCS),
    "doc_chunk_embeddings": indirecte(_DOCS),
    "aux_embeddings": indirecte(
        Ou((Via("projects", ("ref",), fk=False, quand=("kind", "brief")),
            Via("nodes", ("ref",), fk=False, quand=("kind", "node")))),
        "les lignes `kind='guide'` portent un id de l'ancienne table `guides`, retirée : "
        "inattribuables, elles ne partent pas"),
    "user_datastores": possedee(ParEntite()),
    "datastore_rows": indirecte(_TABLEAUX),
    "datastore_row_revisions": indirecte(_TABLEAUX),
    "datastore_row_embeddings": indirecte(
        Via("datastore_rows", ("ns_id", "row_id"), ("ns_id", "row_id"))),
    "nodes": possedee(ParEntite(), "les nœuds `platform`/`tenant` (guides semés au "
                      "démarrage) sont ceux de l'instance"),
    "blocks": indirecte(Via("nodes", ("node_id",))),
    "org_instructions": possedee(Ou((ParOrg(), ParEntite()))),
    "org_instruction_revisions": possedee(Ou((ParOrg(), ParEntite()))),
    # La bibliothèque publique se nomme par sa VUE, comme partout dans le code (#526 :
    # son renommage physique ne doit être qu'un DDL) ; la découverte la résout en sa table.
    "guide_library": possedee(
        Ou((ParOrg("author_org_id"), ParOrg("source_org_id"))),
        "une entrée publiée part avec l'org qui l'a écrite ; celles de la plateforme restent"),
    "functions": possedee(ParEntite()),
    "function_versions": indirecte(Via("functions", ("function_id",))),
    "recipes": possedee(ParEntite()),
    "recipe_versions": indirecte(Via("recipes", ("recipe_id",))),
    "resource_grants": indirecte(
        Ou((Via("user_datastores", ("resource_id",), fk=False, texte=True,
                quand=("resource_type", TYPE_RESSOURCE_DATASTORE)),
            Via("projects", ("resource_id",), fk=False, texte=True,
                quand=("resource_type", "project")),
            Via("org_instructions", ("resource_id",), fk=False, texte=True,
                quand=("resource_type", TYPE_RESSOURCE_PROCEDURE)),
            Via("docs", ("resource_id",), fk=False, texte=True,
                quand=("resource_type", "doc")))),
        "un partage part avec sa RESSOURCE, si son principal est du périmètre",
        destinataire=ParEntite("principal_type", "principal_id")),
    # ── connecteurs et coffre ──────────────────────────────────────────────────
    "connector_credentials": possedee(ParEntite("entity_type", "entity_id"),
                                      secrets=("secret_enc",)),
    "connector_instances": possedee(ParEntite()),
    "connector_availability": possedee(ParEntite("scope_type", "scope_id"),
                                       "les lignes `platform` (semées au démarrage) et "
                                       "`tenant` sont celles de l'instance"),
    "connector_settings": possedee(ParEntite("scope_type", "scope_id")),
    "connector_selection_removed": possedee(ParOrg(), comptes=("sub",)),
    "connector_selection_seeded": possedee(ParOrg(), comptes=("sub",)),
    "user_selected_connectors": possedee(ParOrg(), comptes=("sub",)),
    "connector_account_grants": possedee(ParSub("owner_sub")),
    "connector_account_group_grants": possedee(ParSub("owner_sub")),
    "credential_disparitions": possedee(ParOrg()),
    "grants": possedee(ParEntite("grantor_kind", "grantor_id"),
                       "une arête part avec qui l'accorde, si son bénéficiaire est du "
                       "périmètre ; celles que la plateforme accorde sont notre offre "
                       "(ADR 0070 §6)",
                       destinataire=ParEntite("grantee_kind", "grantee_id")),
    "grant_counters": indirecte(Via("grants", ("grant_id",))),
    "connector_acl": exclue(ParEntite("scope_type", "scope_id"),
                            "plus lue depuis le 24/09/2026 (ADR 0053 D1)"),
    "connector_schemas": instance("cache des schémas d'outils, reconstruit par l'instance"),
    "apollo_phone_reveals": exclue(_ORG_OU_COMPTE,
                                   "commandes de reveal Apollo de trente jours, adressées "
                                   "à l'URL de réception de CETTE instance : la cible ne "
                                   "recevrait jamais leurs livraisons, et le sondage "
                                   "d'Apollo les relit pendant la même fenêtre"),
    # ── messagerie hébergée ────────────────────────────────────────────────────
    "unipile_accounts": possedee(ParOrg(), comptes=("sub",)),
    "unipile_operated_accounts": possedee(ParSub()),
    "unipile_pending": exclue(ParSub(), "connexion en cours : un état éphémère, qui ne "
                              "survit pas à une bascule"),
    # ── runner, abonnements de modèle, transcription ───────────────────────────
    "runs": possedee(_ORG_OU_COMPTE),
    "run_messages": indirecte(Via("runs", ("run_id",), ("run_id",))),
    "runner_jobs": possedee(ParOrg(), comptes=("sub",)),
    "runner_fleets": possedee(ParOrg(), comptes=("sub",)),
    "runner_triggers": possedee(ParOrg(), secrets=("hook_signing_secret_enc",),
                                comptes=("sub",)),
    "runner_hook_deliveries": possedee(ParOrg()),
    "runner_workers": possedee(ParOrg()),
    "runner_platform_workers": instance("les exécutants de plateforme : l'infrastructure "
                                        "de l'instance"),
    "runner_platform_depots": instance("les dépôts vus par les exécutants de plateforme"),
    "user_model_subscriptions": possedee(ParSub(), "`sandbox_id` désigne un bac à sable "
                                         "hors base"),
    "user_model_subscription_loans": possedee(ParOrg(), comptes=("sub",)),
    "org_model_subscription_limits": possedee(ParOrg()),
    "org_model_subscription_modes": possedee(ParOrg()),
    "transcription_jobs": indirecte(_PROJETS, secrets=("api_key_enc",),
                                    hors_base=("audio_key",)),
    # ── journal, usage, signaux ────────────────────────────────────────────────
    "tool_calls": possedee(_ORG_OU_COMPTE, "tout l'historique (décision du 28/09/2026) ; "
                           "ses mois archivés au froid ne partent pas ; au jour J, il "
                           "voyage à part, par tranches (`JOURNAL`)"),
    "journal_archives": instance("registre des mois archivés au froid : les archives "
                                 "mêlent tous les propriétaires, hors base"),
    "usage": possedee(ParSub(), "compteurs par compte, sans org"),
    "usage_signals": possedee(_ORG_OU_COMPTE),
    "access_shadow_l7": possedee(ParOrg()),
    "origine_ecritures": possedee(_ORG_OU_COMPTE),
    "portee_elargissements": possedee(Ou((ParOrg(), ParSubSansOrg("acteur_sub")))),
    "signal_digest_optouts": possedee(ParSub()),
    "scheduled_emails": possedee(Ou((ParOrg(), ParSubSansOrg("created_by")))),
    # ── commerce, légal, relances : les nôtres ─────────────────────────────────
    "org_subscriptions": exclue(ParOrg(), _COMMERCE),
    "billing_payments": exclue(ParOrg(), _COMMERCE),
    "billing_invoices": exclue(ParOrg(), _COMMERCE),
    "billing_identities": exclue(ParOrg(), _COMMERCE),
    "billing_contracts": exclue(ParOrg(), _COMMERCE),
    "option_comps": exclue(ParEntite("entity_type", "entity_id"), _COMMERCE),
    # Trois portées (#1089) : l'org, une personne dans l'org, une personne PARTOUT
    # (`org_id` NULL) — la dernière ne se lit que par le compte.
    "org_entitlements": exclue(_ORG_OU_COMPTE,
                               "droits déclarés posés par notre commerce ; "
                               "l'instance cible déclare les siens (décision du 28/09/2026)"),
    "legal_acceptances": exclue(ParSub(), "preuve d'acceptation de NOS documents légaux "
                                "(décision du 28/09/2026)"),
    "legal_acceptance_events": exclue(ParSub(), "preuve d'acceptation de NOS documents "
                                      "légaux"),
    "outreach_sends": exclue(ParSub(), "nos relances de plateforme"),
    "outreach_optouts": exclue(ParSub(), "nos relances de plateforme"),
    # ── l'instance ─────────────────────────────────────────────────────────────
    "alembic_version": instance("la version du schéma : la cible pose la sienne à sa "
                                "naissance (#969)"),
    "platform_instructions": instance("le socle d'instructions de la plateforme"),
    "upload_tokens_used": instance("anti-rejeu des jetons d'upload signés par NOTRE clé"),
}


# ── le journal hors fenêtre (#1088) ────────────────────────────────────────────────
# Mesuré sur une vraie copie : 4,33 M des 4,47 M lignes d'un périmètre sont des appels
# du journal, et l'essentiel des ~99 min d'export et d'import. Il se transfère à part.
JOURNAL = {"tool_calls": "created_at"}          # table du journal → colonne d'horodatage
# Les faits qui RECONSTRUISENT un run (`deploy/archive_tool_calls.py`, `RUN_FACTS`) : la
# source de vérité des runs. Une tranche peut les emporter TOUS, quelle que soit leur date
# (`--faits-de-run-complets`) ; le reste du journal ne part que sur sa fenêtre.
FAITS_DE_RUN = {"tool_calls": ("tool", ("run_start", "run_finish"))}   # colonne, valeurs
RAISON_JOURNAL = ("journal hors fenêtre : il se verse à part, par tranches de dates, "
                  "dans l'instance déjà importée (`oto-mcp perimetre journal`)")


def sans_journal(classement: dict[str, Table]) -> dict[str, Table]:
    """Le classement de l'export SANS journal : le journal y devient `exclue` — il ne part
    pas avec le reste, le manifeste le compte, et une table qui y renverrait (clé ou
    `Via`) est refusée comme toute référence vers ce qui ne part pas."""
    return {t: exclue(e.regle, RAISON_JOURNAL) if t in JOURNAL else e
            for t, e in classement.items()}
