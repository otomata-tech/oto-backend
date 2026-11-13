"""Ce que l'effacement d'une org fait de CHAQUE table du schéma — décidé, jamais deviné.

L'export par périmètre classe déjà chaque table (`export_perimetre.classement`) : ce
classement dit quelles lignes appartiennent à une org, et un test refuse une table non
classée. L'effacement s'appuie dessus, et ajoute UNE décision :

- une table **exportée** (`possedee`, `indirecte`) est **effacée** : ses lignes du
  périmètre sont supprimées — sauf décision contraire écrite dans `EXCEPTIONS` ;
- une table **non exportée** (`exclue`, `instance`) n'a PAS de décision par défaut :
  l'export la laisse pour une raison qui n'est pas celle de l'effacement (notre
  commerce, un cache, un état de l'instance), donc chacune est tranchée dans
  `NON_EXPORTEES`. Une table qui n'y figure pas REFUSE l'effacement
  (`verifier`) — c'est ce qui empêche l'effacement de rater la table ajoutée demain.

Les actions :

- `effacer` : supprimer les lignes du périmètre ;
- `anonymiser` : garder la ligne, vider (NULL) les colonnes nommées ;
- `pierre_tombale` : la ligne `orgs` elle-même — gardée, vidée de tout ce qui n'est
  pas dans `PIERRE_TOMBALE_GARDE` (les lignes comptables qu'on doit conserver la
  désignent par clé étrangère) ;
- `conserver` : une obligation légale l'exige, pour une durée écrite ;
- `expire` : la donnée disparaît seule, sous la durée écrite (état éphémère) ;
- `sans_objet` : la table ne porte rien d'une org (registre, cache de l'instance).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..export_perimetre.classement import CLASSEMENT, EXPORTEES, Table
from ..export_perimetre.regles import Ou, ParOrg, ParSubSansOrg, Regle

EFFACER = "effacer"
ANONYMISER = "anonymiser"
PIERRE_TOMBALE = "pierre_tombale"
CONSERVER = "conserver"
EXPIRE = "expire"
SANS_OBJET = "sans_objet"
# Ce qui ÉCRIT dans la base pendant la purge.
ECRIVENT = (EFFACER, ANONYMISER, PIERRE_TOMBALE)


@dataclass(frozen=True)
class Decision:
    action: str
    raison: str = ""
    # La règle d'appartenance quand le classement n'en porte pas (table `instance`).
    regle: Regle | None = None
    # `anonymiser` : les colonnes vidées.
    colonnes: tuple[str, ...] = field(default_factory=tuple)


def effacer(raison: str = "", regle: Regle | None = None) -> Decision:
    return Decision(EFFACER, raison, regle)


def anonymiser(colonnes: tuple[str, ...], raison: str) -> Decision:
    return Decision(ANONYMISER, raison, None, colonnes)


def conserver(raison: str) -> Decision:
    return Decision(CONSERVER, raison)


def expire(raison: str) -> Decision:
    return Decision(EXPIRE, raison)


def sans_objet(raison: str) -> Decision:
    return Decision(SANS_OBJET, raison)


_COMPTABLE = ("pièce comptable : conservée 10 ans (Code de commerce, art. L123-22), "
              "rattachée à la pierre tombale de l'org")
_ETAT_DE_TRAVAIL = "état de travail de l'org : il part avec elle"

# La ligne `orgs` : gardée (les pièces comptables la désignent), vidée de tout le reste.
# Toute colonne ABSENTE d'ici est remise à son défaut (`SET col = DEFAULT`, NULL sans
# défaut) : une colonne ajoutée demain est vidée sans que personne ait à y penser.
PIERRE_TOMBALE_GARDE = frozenset({
    "id", "created_at", "tenant_id", "archived_at", "auto_created",
    "suspended_at", "suspended_by", "suspended_reason", "suspended_tenant_id",
    "front_base_url", "front_brand",
})
PIERRE_TOMBALE_NOM = "org effacée"

# Les tables EXPORTÉES qui ne s'effacent pas simplement.
EXCEPTIONS: dict[str, Decision] = {
    "orgs": Decision(PIERRE_TOMBALE, "la ligne reste, sans rien de personnel : les pièces "
                     "comptables conservées la désignent"),
}

# Chaque table NON exportée, tranchée.
NON_EXPORTEES: dict[str, Decision] = {
    # ── exclues de l'export : à l'org, mais pas emportées ──────────────────────
    "org_service_accounts": effacer("le compte de service de l'org et ses clés"),
    "connector_selection_reconciled": effacer("marque de cache de l'org"),
    "apollo_phone_reveals": effacer("commandes de reveal de l'org ou de ses comptes"),
    "unipile_pending": effacer("connexion en cours d'un compte qui part"),
    "apify_runs": effacer("suivi des runs de l'org sur la clé plateforme"),
    "higgsfield_jobs": effacer("suivi des travaux de l'org sur la clé plateforme"),
    "agent_episodes": effacer("pannes et relances des agents de l'org"),
    "copilot_threads": effacer(_ETAT_DE_TRAVAIL),
    "copilot_undos": effacer("annulations offertes à un compte qui part"),
    "copilot_spend": effacer("dépense du jour d'un compte qui part"),
    "copilot_attachments": effacer("fichiers joints aux messages de l'agent intégré "
                                   "(l'objet stocké part avec la ligne)"),
    "copilot_slack_threads": effacer("où vit une conversation d'un compte qui part"),
    "org_subscriptions": conserver(_COMPTABLE),
    "billing_payments": conserver(_COMPTABLE),
    "billing_invoices": conserver(_COMPTABLE),
    "billing_identities": conserver(_COMPTABLE + " — l'identité de facturation figure "
                                    "sur les factures émises"),
    "billing_contracts": conserver(_COMPTABLE),
    "option_comps": effacer("options offertes à l'org ou à ses comptes : sans objet "
                            "une fois l'org effacée"),
    "org_entitlements": effacer("droits déclarés de l'org et de ses comptes"),
    "legal_acceptances": effacer("projection transitoire des acceptations : le journal "
                                 "`legal_acceptance_events` est la preuve"),
    "legal_acceptance_events": anonymiser(
        ("ip", "user_agent"),
        "preuve d'acceptation de NOS documents légaux, gardée le temps de la "
        "prescription (5 ans, Code civil art. 2224) sous le seul identifiant du compte, "
        "sans adresse IP ni navigateur"),
    "outreach_sends": effacer("nos relances envoyées à un compte qui part"),
    "outreach_optouts": effacer("refus de relance d'un compte qui part : sans objet, "
                                "le compte n'existe plus"),
    "credit_payer_modes": effacer("qui paie dans l'org"),
    "org_erasures": conserver("la preuve de l'effacement lui-même (RGPD art. 5 §2) : "
                              "elle ne garde, à la fin, que des comptes et des dates"),
    # ── l'instance : rien d'une org, ou un dérivé du journal ────────────────────
    "journal_totaux_jour": effacer("totaux par jour dérivés du journal de l'org",
                                   Ou((ParOrg(), ParSubSansOrg()))),
    "journal_jobs_jour": effacer("jobs par jour dérivés du journal de l'org", ParOrg()),
    "journal_archives": sans_objet("registre des mois archivés au froid : les ARCHIVES "
                                   "elles-mêmes sont réécrites sans l'org "
                                   "(`effacement.archives`)"),
    "journal_jours_consolides": sans_objet("registre des jours consolidés"),
    "connector_schemas": sans_objet("cache des schémas d'outils"),
    "copilot_slack_events": expire("déduplication des messages Slack, quelques jours, "
                                   "sans propriétaire"),
    "runner_platform_workers": sans_objet("les exécutants de plateforme"),
    "runner_platform_depots": sans_objet("les dépôts des exécutants de plateforme"),
    "platform_key_rate": expire("fenêtre de débit d'une minute"),
    "limite_refus_jour": expire("empreintes de refus des limiteurs, trente jours"),
    "credit_scales": sans_objet("barème du budget d'unités"),
    "credit_scale_lines": sans_objet("lignes du barème"),
    "credit_balances": conserver("solde du budget d'unités sous un identifiant de payeur "
                                 "opaque : écriture comptable de l'instance"),
    "credit_holds": expire("réservations en vol, closes à leur échéance"),
    "credit_movements": conserver("mouvements du budget d'unités : écritures comptables "
                                  "de l'instance, sous un identifiant de payeur opaque"),
    "alembic_version": sans_objet("version du schéma"),
    "platform_instructions": sans_objet("socle d'instructions de la plateforme"),
    "upload_tokens_used": expire("anti-rejeu de jetons signés, jusqu'à leur échéance"),
}

# Les tables qui se désignent ELLES-MÊMES (une clé étrangère vers leur propre table) :
# une ligne qui RESTE peut y descendre d'une ligne effacée, et sa clé bloquerait la purge
# (ou, en cascade, partirait sans décision). Chacune est tranchée :
# - `cascade`  : la clé est `ON DELETE CASCADE` et ses descendants sont de toute façon
#                du périmètre (une sous-page suit sa page) ;
# - `revoquer` : les descendants hors périmètre sont RÉVOQUÉS et détachés — un partage
#                dérivé d'un partage effacé ne doit jamais survivre actif (il élargirait
#                un accès), et un partage ne se supprime jamais : il s'archive
#                (`revoked_at`, 0053-D7), sa consommation reste ;
# - `detacher` : les descendants hors périmètre restent, leur lien vers le parent est
#                vidé (une instance de connecteur d'une autre org héritée d'une des siennes) ;
# - `set_null` : la base le fait elle-même (`ON DELETE SET NULL`) — une copie publiée
#                par une autre org reste, sans son « copiée de ».
AUTO_REFERENCES: dict[str, str] = {
    "docs": "cascade",
    "grants": "revoquer",
    "connector_instances": "detacher",
    "procedure_comments": "cascade",
    "doctrine_library": "set_null",     # la table derrière la vue `guide_library`
}

# Les colonnes de CLÉ d'objet que l'export ne connaît pas (il ne part pas avec ces
# tables), et que l'effacement doit retirer du stockage.
CLES_D_OBJET_EN_PLUS = frozenset({"copilot_attachments.s3_key"})


class DispositionIncomplete(RuntimeError):
    def __init__(self, anomalies: list[str]):
        self.anomalies = anomalies
        super().__init__("effacement d'org : disposition incomplète — "
                         f"{len(anomalies)} anomalie(s) :\n  - " + "\n  - ".join(anomalies))


def decision(table: str, entree: Table) -> Decision:
    if table in EXCEPTIONS:
        return EXCEPTIONS[table]
    if entree.classe in EXPORTEES:
        return effacer()
    return NON_EXPORTEES[table]


def regle_de(entree: Table, d: Decision) -> Regle | None:
    """La règle qui dit quelles lignes la décision vise."""
    return d.regle or entree.regle


def verifier(classement: dict[str, Table] = CLASSEMENT,
             colonnes: dict[str, tuple[str, ...]] | None = None,
             auto_references: set[str] | None = None) -> None:
    """Refuse, en les listant toutes, les anomalies de la disposition. `auto_references` :
    les tables du schéma réel qui portent une clé étrangère vers elles-mêmes."""
    anomalies: list[str] = []
    for t in sorted((auto_references or set()) - set(AUTO_REFERENCES)):
        anomalies.append(f"{t} : se désigne elle-même sans décision "
                         "(`effacement/disposition.py::AUTO_REFERENCES`)")
    for t, e in sorted(classement.items()):
        if e.classe in EXPORTEES:
            if t in NON_EXPORTEES:
                anomalies.append(f"{t} : exportée, sa décision va dans `EXCEPTIONS`")
            continue
        if t not in NON_EXPORTEES:
            anomalies.append(f"{t} ({e.classe}) : aucune décision d'effacement — la "
                             "trancher dans `effacement/disposition.py::NON_EXPORTEES`")
            continue
        d = NON_EXPORTEES[t]
        if d.action in (EFFACER, ANONYMISER) and regle_de(e, d) is None:
            anomalies.append(f"{t} : `{d.action}` sans règle d'appartenance")
        if d.action not in ECRIVENT and not d.raison:
            anomalies.append(f"{t} : `{d.action}` sans raison écrite")
        if colonnes is not None and t in colonnes:
            manquantes = [c for c in d.colonnes if c not in colonnes[t]]
            if manquantes:
                anomalies.append(f"{t} : colonnes à anonymiser absentes {manquantes}")
    for t in sorted(set(NON_EXPORTEES) | set(EXCEPTIONS)):
        if t not in classement:
            anomalies.append(f"{t} : décision pour une table inconnue du classement")
    if colonnes is not None and "orgs" in colonnes:
        for c in sorted(PIERRE_TOMBALE_GARDE - set(colonnes["orgs"])):
            anomalies.append(f"orgs.{c} : gardée par la pierre tombale, absente du schéma")
    if anomalies:
        raise DispositionIncomplete(anomalies)
