"""Capacités billing (ADR 0043, B2) — abonnement par org, REST-only.

Pas de face MCP par choix d'ADR : payer est un acte humain (dashboard), on ne
fait pas transiter d'URL de paiement dans un contexte LLM. Souscrire/confirmer/
résilier = org_admin ; consulter = tout membre de l'org active.

L'**identité de facturation** (#486) est le PRÉALABLE de ce cycle — le pays décide
du montant réellement débité, et `billing.subscribe` refuse tant qu'elle n'est pas
là (409 `billing_identity_required`). Elle vit à côté, dans
`capabilities/billing_identity.py` : l'abonnement est un cycle, l'identité une
fiche qu'on remplit une fois. Même régime REST-only, même gate de dark launch.

Le **consentement** est le second préalable (#487, `billing_consent`) : 409
`legal_required` tant que l'appelant n'a pas accepté CGU + CGV + DPA à leur version
courante, via `me.legal.accept {context: "purchase"}`. Les deux préalables sont
rendus ENSEMBLE dans `details.blockers` — le tunnel les affiche d'un coup au lieu
de les découvrir un par un.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .. import billing, billing_consent
from ..mollie_client import MollieError
from ._authz import ORG_ADMIN, ORG_MEMBER, SUB_ONLY, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class NoInput(BaseModel):
    pass


class SubscribeInput(BaseModel):
    plan: str
    return_url: str          # URL de retour du dashboard (page billing)
    method: str = "card"     # 'card' | 'sepa' (prélèvement) — restreint la page Mollie
    # Pas de champs IBAN/mobile : le flux Mollie unifié collecte le moyen de
    # paiement (carte ou IBAN + mandat) sur la page de checkout hébergée.


class ConfirmInput(BaseModel):
    # Le retour de la page hébergée porte `?payment_ref=tr_…` (posé sur l'URL de
    # retour par billing.subscribe) : le navigateur DIT quel paiement il vient de
    # conclure, au lieu de laisser le serveur prendre « le plus récent ». Optionnel :
    # un client qui ne l'a pas (polling, vieux front) garde le comportement d'avant.
    payment_ref: Optional[str] = None


class PaymentsInput(BaseModel):
    limit: int = 20

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        # Patron `SearchInput._cap` : la valeur part telle quelle en `LIMIT %s`. Sans
        # borne, un `limit` énorme sérialise tout l'historique de l'org et un NÉGATIF
        # fait échouer Postgres (« LIMIT must not be negative ») en 500 opaque.
        return max(1, min(int(v), 100))


class AdminPlanInput(BaseModel):
    org_id: int
    plan: str | None = None   # None / omis = retirer le plan comp forcé


# ── formes de réponse (ADR 0009 : `Output` DÉCRIT la 200, ne la valide pas) ───
# Le vocabulaire de la facturation est piégeux : un montant, une date de fin et un
# état d'abonnement ne veulent pas dire la même chose selon la branche. Les
# subtilités vivent dans les `description=` — c'est ce que lit l'intégrateur dans
# `/api/openapi.json`, et ce qui lui évite de les découvrir en production.

class Plan(BaseModel):
    """Un palier du catalogue. Le catalogue est un mapping en CODE (`billing.PLANS`),
    pas une table : il n'a pas d'id de base et peut changer entre deux déploiements."""
    plan: str = Field(description="Identifiant technique du palier ('standard', "
                                  "'premium', 'business', 'enterprise') — c'est CETTE "
                                  "valeur qu'attend billing.subscribe, pas le label.")
    label: str = Field(description="Libellé d'affichage ('Standard', 'Entreprise'…).")
    amount: int = Field(description="Prix HT mensuel en CENTIMES (1900 = 19,00 €). "
                                    "Jamais un décimal, jamais TTC.")
    currency: str = Field(description="Code devise ISO en minuscules ('eur').")
    interval: str = Field(description="Période facturée : 'month' | 'year'. "
                                      "L'échéance suivante est calendaire (31/01 + 1 "
                                      "mois → 28/02), jamais +30 jours.")
    unipile_accounts: Optional[int] = Field(
        default=None,
        description="Plafond de comptes de messagerie ouvert par le palier. "
                    "`null` = ILLIMITÉ (surtout pas « zéro compte ») — aujourd'hui "
                    "TOUS les paliers valent null : on ne facture plus au nombre de "
                    "comptes, les 4 paliers débloquent la même chose et ne diffèrent "
                    "que par le prix.")
    custom: bool = Field(description="Palier « sur devis ». billing.subscribe le REFUSE "
                                     "(400 `custom_plan`) : il s'ouvre par un admin "
                                     "plateforme en abonnement offert (comp).")


class PlansView(BaseModel):
    """Catalogue public des paliers — état de l'org NON inclus (c'est billing.status)."""
    plans: list[Plan]


class MonthlyUsage(BaseModel):
    """Ce que l'org a CONSOMMÉ ce mois-ci, et ce qui est inclus.

    ⚠️ **Rien ici ne refuse quoi que ce soit.** Le journal qui porte ce chiffre est
    best-effort et non transactionnel : il mesure et il prévient, il ne facture pas
    et ne coupe pas. Un dépassement s'affiche, il ne bloque aucun appel.

    ⚠️ **Aucun ratio n'est servi, et c'est délibéré.** L'usage médian d'une org active
    est de 25 appels par mois pour 1000 inclus : une barre de progression ou un
    « 2,5 % » y dirait « c'est gratuit et sans fin », soit l'inverse de ce que ce
    bloc existe pour faire comprendre. Afficher le NOMBRE et mentionner le plafond ;
    ne pas les diviser."""
    calls: int = Field(description="Appels d'OUTIL D'AGENT émis sous cette org depuis "
                                   "le 1er du mois (UTC). N'inclut ni la navigation "
                                   "dans le tableau de bord, ni les handshakes : trois "
                                   "volumes sans rapport, dont le mélange ne mesurerait "
                                   "que le fait d'avoir un onglet ouvert.")
    included: int = Field(description="Appels inclus par mois et par organisation.")
    period_start: str = Field(description="Début de la période comptée — le 1er du mois "
                                          "courant, UTC. ⚠️ Le MOIS EN COURS est la "
                                          "seule fenêtre disponible : la purge du "
                                          "journal ne garde qu'environ 35 jours, donc "
                                          "aucune comparaison avec le mois précédent "
                                          "n'est calculable sur cette source.")
    over: bool = Field(description="Le compte dépasse-t-il l'inclus ? C'est CE booléen "
                                   "qui décide d'un message, jamais un pourcentage "
                                   "calculé côté écran — et il n'entraîne aucun refus.")


class GrantedBenefit(BaseModel):
    """Un avantage payant OFFERT — un droit ouvert sans qu'aucun euro ne circule.

    ⚠️ Ce n'est PAS un abonnement offert (`comp=true` plus haut, qui suppose une
    ligne d'abonnement et un palier). C'est un **don d'option** : l'org n'a pas
    d'abonnement du tout, et reçoit quand même un avantage du catalogue. Les deux
    chemins coexistent et ne se lisent pas au même endroit — d'où ce bloc, servi
    justement dans la branche `subscribed:false` où rien ne le disait."""
    option: str = Field(description="Identifiant technique de l'avantage ('unipile'). "
                                    "Seules les options VENDUES dans un palier "
                                    "apparaissent ici : un drapeau de population "
                                    "('beta') n'est pas un cadeau et n'y figure pas.")
    label: str = Field(description="L'avantage NOMMÉ, tel qu'on l'affiche "
                                   "('Messagerie hébergée (Unipile)'). Il ne se "
                                   "présume pas : plusieurs avantages peuvent "
                                   "s'offrir, ce champ dit lequel.")
    detail: Optional[str] = Field(default=None, description="Ce que l'avantage permet, "
                                                            "en une phrase.")
    scope: str = Field(description="'org' = offert à l'espace (tous ses membres) | "
                                   "'user' = offert à CE compte, et il le suit dans "
                                   "tous ses espaces. Offert des deux façons, "
                                   "l'avantage n'apparaît qu'une fois — avec "
                                   "l'échéance la plus lointaine des deux.")
    granted_at: Optional[str] = Field(default=None, description="Date de la mise à "
                                                                "disposition.")
    expires_at: Optional[str] = Field(
        default=None,
        description="Fin de la mise à disposition. `null` = SANS terme (l'état de "
                    "tous les dons antérieurs au 2026-09-02) — surtout pas « expire "
                    "bientôt ». Passée cette date, l'avantage cesse d'être accordé : "
                    "le droit se referme, les données restent.")
    days_left: Optional[int] = Field(
        default=None,
        description="Jours restants avant `expires_at`. `null` si sans terme. "
                    "**NÉGATIF si l'échéance est passée** — un don échu se lit échu, "
                    "il n'est pas ramené à zéro.")
    value_amount: Optional[int] = Field(
        default=None,
        description="Ce que cet avantage coûterait, en CENTIMES **hors taxes** : le "
                    "prix du palier le MOINS cher qui l'inclut. Rien n'a été facturé "
                    "— c'est la valeur du cadeau, pas une dette.")
    currency: Optional[str] = Field(default=None, description="Devise ('eur').")
    interval: Optional[str] = Field(default=None, description="'month' | 'year'.")


class BillingStatus(BaseModel):
    """État d'abonnement de l'org active — servi aussi bien par billing.status que
    par billing.cancel (qui rend l'état APRÈS la demande de résiliation).

    ⚠️ DEUX formes selon qu'une ligne d'abonnement existe : sans abonnement, la
    réponse se réduit à `{subscribed: false, plans: […]}` et TOUS les autres champs
    sont absents (pas nuls) ; avec abonnement, `plans` disparaît. Tester `subscribed`,
    jamais la présence de `plan`."""
    subscribed: bool = Field(
        description="L'org a-t-elle un droit d'accès ouvert ? True pour un abonnement "
                    "`active` MAIS AUSSI `past_due` (impayé en cours de relance : "
                    "l'accès court encore). Donc subscribed=True n'implique ni « à "
                    "jour de paiement », ni « payé » (cf. `comp`).")
    plans: Optional[list[Plan]] = Field(
        default=None,
        description="Le catalogue, joint UNIQUEMENT quand l'org n'a aucun abonnement "
                    "— de quoi peindre la page de souscription sans second appel. "
                    "Absent dès qu'un abonnement existe : son absence n'est pas une "
                    "erreur.")
    plan: Optional[str] = Field(default=None, description="Palier souscrit (clé de "
                                                          "catalogue).")
    label: Optional[str] = Field(
        default=None,
        description="Libellé du palier, relu du CATALOGUE COURANT — donc `null` si le "
                    "palier stocké a disparu du code depuis la souscription (idem "
                    "amount/currency/interval).")
    amount: Optional[int] = Field(
        default=None,
        description="Prix courant du palier au catalogue, en CENTIMES **HORS TAXES**. "
                    "Ce n'est PAS un montant facturé : un abonnement offert (comp) "
                    "affiche le prix du palier alors que rien n'a jamais été encaissé, "
                    "et depuis #486 ce qui est débité est le TTC (`amount_ttc`). Les "
                    "montants réellement passés au PSP se lisent sur billing.payments.")
    vat_rate_bps: Optional[int] = Field(
        default=None,
        description="Taux appliqué à la PROCHAINE échéance, en points de base "
                    "(2000 = 20,00 %). `null` si aucun régime n'est calculable — "
                    "`vat_blocked` dit pourquoi.")
    vat_amount: Optional[int] = Field(
        default=None, description="TVA de la prochaine échéance, en centimes.")
    amount_ttc: Optional[int] = Field(
        default=None,
        description="Ce qui sera RÉELLEMENT prélevé à la prochaine échéance, en "
                    "centimes. Dérivé de l'identité de facturation COURANTE : il bouge "
                    "si l'org change de pays, ce qui est voulu — ce qui a déjà été pris "
                    "ne bouge pas, lui, et se lit sur billing.payments.")
    vat_scheme: Optional[str] = Field(
        default=None,
        description="'fr_ttc' | 'reverse_charge' | 'export'. `null` si non calculable.")
    vat_blocked: Optional[str] = Field(
        default=None,
        description="Pourquoi le TTC est inconnu : 'billing_identity_required' ou "
                    "'vat_consumer_unsupported'. `null` = rien ne bloque. Un "
                    "abonnement ACTIF avec un `vat_blocked` posé signale une échéance "
                    "que le runner ne pourra pas prélever — à réparer. ⚠️ Sur un "
                    "abonnement OFFERT (comp=true), les quatre champs de TVA valent "
                    "TOUJOURS `null`, `vat_blocked` compris : rien n'y sera jamais "
                    "prélevé, donc il n'y a ni TTC à annoncer ni alerte à lever.")
    currency: Optional[str] = Field(default=None, description="Devise du palier ('eur').")
    interval: Optional[str] = Field(default=None, description="'month' | 'year'.")
    status: Optional[str] = Field(
        default=None,
        description="État du miroir local : 'incomplete' (souscription ouverte, jamais "
                    "de droit), 'active', 'past_due' (impayé, droit maintenu pendant la "
                    "relance), 'canceled' (fini). C'est LA source de vérité du cycle, "
                    "PSP-agnostique.")
    method: Optional[str] = Field(
        default=None,
        description="Moyen de paiement du mandat : 'card' | 'sepa' | 'comp' (aucun — "
                    "abonnement offert par un admin).")
    comp: bool = Field(
        default=False,
        description="Abonnement OFFERT, forcé par un admin plateforme : accès ouvert, "
                    "aucun PSP derrière, aucune échéance tirée, `amount` purement "
                    "indicatif. Un comp=True + subscribed=True ne signifie donc aucun "
                    "encaissement (billing.payments sera vide).")
    current_period_end: Optional[str] = Field(
        default=None,
        description="Borne de l'accès : fin de la période couverte. Format 'YYYY-MM-DD "
                    "HH:MM:SS' UTC (normalisé par la couche DB) — pas de l'ISO 8601, à "
                    "la différence de billing.confirm qui rend un horodatage à offset. "
                    "`null` sur un abonnement offert (aucune période).")
    next_billing_at: Optional[str] = Field(
        default=None,
        description="Prochaine échéance à tirer. `null` = plus RIEN ne sera tiré — "
                    "abonnement offert, ou résilié (canceled_at posé) — surtout pas "
                    "« pas encore programmé ».")
    grace_until: Optional[str] = Field(
        default=None,
        description="Fin du délai de grâce d'un impayé (`past_due`) : au-delà, la "
                    "relance cesse et l'abonnement bascule. `null` hors impayé.")
    canceled_at: Optional[str] = Field(
        default=None,
        description="Horodatage de la DEMANDE de résiliation, pas de la fin d'accès : "
                    "le statut reste 'active' et subscribed=True jusqu'à "
                    "current_period_end. Une résiliation se lit donc ici, JAMAIS sur "
                    "`status`.")
    block_code: Optional[str] = Field(
        default=None,
        description="Ce que le runner a CONSTATÉ à la dernière échéance qu'il n'a PAS "
                    "pu tirer : 'billing_identity_required', "
                    "'vat_consumer_unsupported', 'plan_unknown' ou 'no_mandate'. "
                    "`null` = rien n'a échoué. ⚠️ À ne pas confondre avec `vat_blocked`, "
                    "qui est une PRÉVISION recalculée à chaque lecture : `block_code` "
                    "est un fait daté, et tant qu'il est posé le service est rendu SANS "
                    "être encaissé — le cycle n'avance pas et le droit ne se ferme pas.")
    block_detail: Optional[str] = Field(
        default=None,
        description="Le message de diagnostic qui accompagne `block_code`. Destiné à "
                    "l'exploitation, pas au payeur.")
    block_since: Optional[str] = Field(
        default=None,
        description="Depuis QUAND l'échéance ne passe plus — donc depuis quand le "
                    "service est rendu gratuitement. Ne bouge pas d'un tick à l'autre : "
                    "c'est la date du PREMIER constat, pas du dernier.")
    granted: list["GrantedBenefit"] = Field(
        default_factory=list,
        description="Avantages payants OFFERTS à l'org (et, sur /api/me/billing, au "
                    "compte appelant) — servis dans les DEUX branches, y compris "
                    "`subscribed:false`. Liste vide = rien d'offert **ou** org hors "
                    "du périmètre du dispositif (une org hébergée par un tenant "
                    "tiers n'en reçoit jamais : ses clients ne sont pas les nôtres). "
                    "L'absence ne prouve donc pas l'absence de don.")
    usage: Optional[MonthlyUsage] = Field(
        default=None,
        description="Consommation du mois en cours face à ce qui est inclus, servie "
                    "dans les DEUX branches et à tous les comptes — c'est le seul bloc "
                    "de cet écran qui vaut pour tout le monde. `null` = rien à "
                    "montrer : org hors périmètre du dispositif, ou journal illisible. "
                    "Un compteur qui n'a pas su lire se TAIT plutôt que d'afficher un "
                    "« 0 » qu'aucun lecteur ne peut recouper.")


class SubscribeStarted(BaseModel):
    """Souscription OUVERTE, pas conclue : à ce stade rien n'est débité, aucun miroir
    d'abonnement n'existe et l'org n'a aucun droit ouvert. Le geste se finit sur la
    page de checkout hébergée, puis billing.confirm constate l'encaissement."""
    checkout_url: str = Field(
        description="Page de checkout HÉBERGÉE Mollie où le payeur finit (3DS carte, "
                    "ou saisie IBAN + acceptation du mandat SEPA). À ouvrir dans un "
                    "navigateur : rien ne se passe côté serveur tant qu'elle n'est pas "
                    "parcourue, et elle expire.")
    payment_intent_id: str = Field(description="Identifiant Mollie du PREMIER paiement "
                                               "('tr_…') — la trace à rapprocher de "
                                               "billing.payments (kind='initial').")
    plan: str = Field(description="Palier demandé (écho de l'entrée). Il voyage dans la "
                                  "metadata du paiement : c'est de là que confirm le "
                                  "relit, aucun état serveur pendant le checkout.")
    method: str = Field(description="'card' | 'sepa' — écho de l'entrée, il RESTREINT la "
                                    "page de checkout. Ne présume pas du moyen "
                                    "finalement enregistré : le `method` réel se lit sur "
                                    "confirm/status.")
    amount_ht: int = Field(description="Prix du palier en centimes, HORS TAXES.")
    vat_rate_bps: int = Field(description="Taux retenu, en points de base "
                                          "(2000 = 20,00 %, 0 = exonéré).")
    vat_amount: int = Field(description="TVA en centimes.")
    amount_ttc: int = Field(
        description="Ce que la page de checkout va RÉELLEMENT débiter, en centimes. "
                    "C'est ce montant-là qu'il faut annoncer au payeur avant de "
                    "l'envoyer sur la page hébergée — sinon il découvre le TTC chez "
                    "le PSP.")
    vat_scheme: str = Field(
        description="'fr_ttc' (TVA française 20 %) | 'reverse_charge' "
                    "(autoliquidation intracommunautaire) | 'export' (hors UE).")
    vat_mention: Optional[str] = Field(
        default=None,
        description="Mention légale à porter sur la facture (art. 196 de la directive "
                    "2006/112/CE en autoliquidation, art. 259-1 du CGI en export). "
                    "`null` en régime français : une facture avec TVA n'a rien à "
                    "justifier.")


class ConfirmResult(BaseModel):
    """Avancement du premier paiement (POLLING au retour du payeur, et en
    réconciliation). Enveloppe commune à quatre branches, discriminées par `status` ;
    les champs propres à une branche sont simplement absents des autres.

    ⚠️ `status` décrit la SOUSCRIPTION, pas le paiement — le brut du PSP est
    `payment_status`. Appel idempotent : re-confirmer un abonnement déjà actif rend
    `{status:'active', plan}` seul, sans method ni current_period_end ; leur absence
    n'est donc pas une anomalie.

    Toutes les branches sont des **200** : `confirm` décrit l'avancement d'une
    souscription, il ne signale une erreur que lorsque l'appel lui-même est fautif
    (paiement inconnu, aucune souscription en cours). Un paiement RÉUSSI ne produit
    donc jamais de code d'erreur — c'est exactement ce qui a fait repayer un client
    le 25/08 (#493)."""
    status: str = Field(
        description="'active' = encaissé, mandat récupéré, miroir posé, accès OUVERT. "
                    "'pending' = pas encore encaissé (le payeur est peut-être encore "
                    "sur la page) : re-poller. 'pending_mandate' = ENCAISSÉ, mais le "
                    "mandat réutilisable n'existe pas encore chez le PSP (il naît "
                    "quelques minutes après le paiement) : l'argent est pris, l'accès "
                    "s'ouvrira seul — re-poller après `retry_after`, et surtout ne PAS "
                    "reproposer de payer. 'failed' = paiement failed/canceled/"
                    "expired, l'org n'est PAS abonnée et il faut re-souscrire (aucune "
                    "reprise possible sur ce paiement).")
    retry_after: Optional[int] = Field(
        default=None,
        description="Délai conseillé avant la re-sonde, en SECONDES. Porté par la "
                    "branche 'pending_mandate' uniquement.")
    plan: Optional[str] = Field(default=None, description="Palier activé — relu de la "
                                                          "metadata du paiement. Présent "
                                                          "sur 'active' seulement.")
    method: Optional[str] = Field(
        default=None,
        description="Moyen RÉELLEMENT enregistré ('card' | 'sepa'), déduit du paiement "
                    "Mollie et non de ce qui avait été demandé. Absent sur le no-op "
                    "idempotent.")
    current_period_end: Optional[str] = Field(
        default=None,
        description="Fin de la première période, ISO 8601 avec offset (⚠️ format "
                    "DIFFÉRENT de billing.status, qui rend 'YYYY-MM-DD HH:MM:SS'). "
                    "Absent sur le no-op idempotent.")
    payment_status: Optional[str] = Field(
        default=None,
        description="État BRUT du paiement chez Mollie (open, pending, authorized, "
                    "paid, failed, canceled, expired). Porté par les branches 'pending', "
                    "'pending_mandate' (où il vaut toujours 'paid') et 'failed'.")
    amount: Optional[int] = Field(
        default=None,
        description="Montant RÉELLEMENT passé au PSP pour ce paiement, en centimes — "
                    "TTC depuis #486. Relu du journal, pas du catalogue : c'est ce que "
                    "le client a été débité, même si le prix du palier a changé depuis. "
                    "Absent sur le no-op idempotent (aucun paiement n'y est lu).")
    amount_ht: Optional[int] = Field(
        default=None,
        description="Part hors taxes du montant, en centimes. ⚠️ `null` sur les "
                    "encaissements ANTÉRIEURS au 28/08/2026 : ils ont réellement été "
                    "débités du HT sans TVA et ne sont pas réécrits — un `null` ici "
                    "veut dire « ligne d'avant la règle », jamais « zéro ».")
    vat_rate_bps: Optional[int] = Field(
        default=None, description="Taux appliqué, en points de base (2000 = 20,00 %).")
    vat_amount: Optional[int] = Field(
        default=None, description="TVA effectivement facturée, en centimes.")
    vat_scheme: Optional[str] = Field(
        default=None,
        description="'fr_ttc' | 'reverse_charge' | 'export' — le régime figé au moment "
                    "du débit, qui ne suit PAS un changement d'identité ultérieur.")


class Payment(BaseModel):
    """Une TENTATIVE de paiement journalisée, pas une facture ni un reçu : une ligne
    peut n'avoir rien encaissé. Seul `status='paid'` atteste un encaissement."""
    id: int = Field(description="Identifiant de la ligne de journal LOCALE (séquence), "
                                "pas l'identifiant Mollie.")
    kind: str = Field(description="'initial' (premier paiement d'une souscription, celui "
                                  "qui crée le mandat) | 'renewal' (échéance rejouée sur "
                                  "le mandat existant).")
    amount: int = Field(description="Montant de la tentative en CENTIMES, figé au moment "
                                    "de la tentative : il peut différer du prix courant "
                                    "du palier rendu par billing.status. C'est ce qui a "
                                    "été passé au PSP, donc le **TTC** depuis #486.")
    amount_ht: Optional[int] = Field(
        default=None,
        description="Part hors taxes, en centimes. ⚠️ `null` sur les DEUX "
                    "encaissements antérieurs au 28/08/2026, débités du HT sans TVA et "
                    "délibérément NON réécrits : `null` = « ligne d'avant la règle », "
                    "surtout pas « zéro ». C'est ce champ qui les distingue.")
    vat_rate_bps: Optional[int] = Field(
        default=None, description="Taux appliqué, en points de base (2000 = 20,00 %).")
    vat_amount: Optional[int] = Field(
        default=None, description="TVA facturée, en centimes (amount − amount_ht).")
    country_code: Optional[str] = Field(
        default=None,
        description="Pays de facturation retenu au moment du débit (ISO-3166-1 "
                    "alpha-2) — il ne suit pas un déménagement ultérieur de l'org.")
    vat_scheme: Optional[str] = Field(
        default=None, description="'fr_ttc' | 'reverse_charge' | 'export'.")
    currency: str = Field(description="Code devise ISO en minuscules ('eur').")
    status: str = Field(
        description="État repris du PSP : 'processing'/'open'/'pending'/'authorized' = "
                    "en vol (re-pollé) ; 'paid' | 'failed' | 'canceled' | 'expired' = "
                    "terminal. Une ligne non terminale ne se conclura pas d'elle-même "
                    "dans cette réponse : c'est le runner qui la fera bouger.")
    attempt: int = Field(description="Rang de la tentative pour une MÊME échéance "
                                     "(relance d'impayé) : plusieurs lignes de même "
                                     "`kind` et même montant ne sont pas des doubles "
                                     "débits, elles se distinguent par ce rang.")
    created_at: str = Field(description="Création de la TENTATIVE ('YYYY-MM-DD HH:MM:SS' "
                                        "UTC), pas la date d'encaissement — qui n'est "
                                        "pas exposée ici.")


class PaymentsView(BaseModel):
    """Journal des tentatives, plus récentes d'abord, borné par `limit`. Une liste vide
    est normale sur un abonnement offert (comp) : rien n'y transite par le PSP."""
    payments: list[Payment]


def _domain(fn, *args):
    """Traduit les erreurs domaine/PSP en refus neutres (jamais un 500 nu) :
    ValueError = état/entrée (`code: détail`), MollieError = amont PSP (502),
    RuntimeError = config/invariant (MOLLIE_API_KEY absente, mandat manquant)."""
    try:
        return fn(*args)
    except billing_consent.PurchaseBlocked as e:
        # #487 : les préalables d'un achat (identité de facturation, consentement)
        # sont rendus ENSEMBLE. Le code de tête est le PREMIER manque dans l'ordre du
        # tunnel — donc inchangé quand un seul manque, ce que les clients existants
        # attendaient déjà — et `details.blockers` porte la liste complète, chaque
        # entrée avec son propre code, son message, et pour le légal les documents à
        # présenter (slug, libellé, version, URL).
        #
        # ⚠️ C'est `blockers` qu'un client doit lire, jamais le code de tête seul :
        # avec deux manques, il n'en nomme qu'un.
        raise AuthzDenied(409, e.code, str(e), details={"blockers": e.blockers})
    except ValueError as e:
        msg = str(e)
        code = msg.split(":", 1)[0].strip() if ":" in msg else "billing_error"
        # `payment_pending` = un premier paiement de cette org est encore en vol
        # (#493). C'est un CONFLIT d'état, pas une entrée invalide : le client n'a
        # rien à corriger, il a à attendre — et surtout pas à ouvrir un second
        # paiement, ce qui débiterait deux fois.
        # `billing_identity_required` et `vat_consumer_unsupported` (#486) sont eux
        # aussi des CONFLITS d'état, pas des entrées invalides : le corps de l'appel
        # est correct, c'est l'org qui n'est pas en état d'être débitée (identité
        # manquante, ou pays que le guichet OSS ne couvre pas encore). Un 400
        # enverrait chercher le défaut dans la requête, où il n'est pas.
        raise AuthzDenied(
            409 if code in ("already_subscribed", "payment_pending",
                            "billing_identity_required",
                            "vat_consumer_unsupported") else 400,
            code, msg)
    except MollieError as e:
        raise AuthzDenied(502, "psp_error", e.detail)
    except RuntimeError as e:
        msg = str(e)
        code = msg.split(":", 1)[0].strip() if ":" in msg else ""
        # Tout n'est pas transitoire. `no_mandate` (encaissé sans mandat réutilisable)
        # et `bad_metadata` sont DÉFINITIFS : les annoncer « facturation indisponible »
        # invite à retenter — sur un état où le client a DÉJÀ été débité et où il faut
        # une investigation manuelle. Un 409 dit la vérité : l'état empêche d'aboutir,
        # réessayer n'y changera rien.
        if code in ("no_mandate", "bad_metadata"):
            raise AuthzDenied(409, code, msg)
        raise AuthzDenied(503, "billing_unavailable", msg)


def _plans(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return {"plans": billing.plans()}


def _status(ctx: ResolvedCtx, inp: NoInput) -> dict:
    # `sub` passé ICI et nulle part ailleurs : /api/me/billing est l'écran de
    # l'appelant, et un don sur douze est posé sur un COMPTE — sans ce grain, son
    # porteur voit un catalogue qui lui vend ce qu'il a déjà. La fiche d'org servie à
    # un admin plateforme (`orgs.reads._org_detail`) ne le passe pas : elle décrit
    # l'org, pas son lecteur.
    return _domain(lambda: billing.status(ctx.org_id, sub=ctx.sub))


def _subscribe(ctx: ResolvedCtx, inp: SubscribeInput) -> dict:
    def call():
        # `sub` = l'appelant : accepter des documents est un acte de PERSONNE, pas
        # d'organisation (ADR 0043 fait payer l'org ; c'est un humain qui signe).
        return billing.subscribe(ctx.org_id, inp.plan, inp.return_url,
                                 sub=ctx.sub, method=inp.method)

    return _domain(call)


def _confirm(ctx: ResolvedCtx, inp: ConfirmInput) -> dict:
    return _domain(billing.confirm, ctx.org_id, inp.payment_ref)


def _cancel(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return _domain(billing.cancel, ctx.org_id)


def _resume(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return _domain(billing.resume, ctx.org_id)


def _admin_set_plan(ctx: ResolvedCtx, inp: AdminPlanInput) -> dict:
    if inp.plan:
        return _domain(lambda: billing.admin_set_plan(
            inp.org_id, inp.plan, granted_by=ctx.sub))
    return _domain(lambda: billing.admin_clear_plan(inp.org_id))


def _payments(ctx: ResolvedCtx, inp: PaymentsInput) -> dict:
    from ..db import billing as db_billing

    rows = db_billing.list_billing_payments(ctx.org_id, inp.limit)
    return {"payments": [
        {k: r.get(k) for k in ("id", "kind", "amount", "amount_ht", "vat_rate_bps",
                               "vat_amount", "country_code", "vat_scheme",
                               "currency", "status", "attempt", "created_at")}
        for r in rows
    ]}


_BILLING_CAPS = [
    Capability(
        key="billing.plans", handler=_plans, Input=NoInput, authz=SUB_ONLY,
        Output=PlansView,
        rest=RestBinding("GET", "/api/billing/plans"),
    ),
    Capability(
        key="billing.status", handler=_status, Input=NoInput, authz=ORG_MEMBER,
        Output=BillingStatus,
        rest=RestBinding("GET", "/api/me/billing"),
    ),
    Capability(
        key="billing.subscribe", handler=_subscribe, Input=SubscribeInput,
        authz=ORG_ADMIN, Output=SubscribeStarted,
        description="Open a subscription: returns a hosted Mollie checkout URL. "
                    "Refuses with 409 while a precondition is unmet — "
                    "`billing_identity_required` (no billing identity on the org), "
                    "`vat_consumer_unsupported` (EU consumer outside France), "
                    "`legal_required` (caller has not accepted CGU/CGV/DPA at their "
                    "current version), `already_subscribed`, `payment_pending`. ALL "
                    "unmet preconditions are listed in `details.blockers`; the "
                    "top-level code names only the first.",
        rest=RestBinding("POST", "/api/me/billing/subscribe"),
    ),
    Capability(
        key="billing.confirm", handler=_confirm, Input=ConfirmInput,
        authz=ORG_ADMIN, Output=ConfirmResult,
        rest=RestBinding("POST", "/api/me/billing/confirm"),
    ),
    # Résiliation à fin de période : rend le MÊME état que billing.status (avec
    # canceled_at posé, statut encore 'active') — d'où l'Output partagé.
    Capability(
        key="billing.cancel", handler=_cancel, Input=NoInput,
        authz=ORG_ADMIN, Output=BillingStatus,
        rest=RestBinding("POST", "/api/me/billing/cancel"),
    ),
    # L'inverse de la résiliation, qui n'existait pas : l'écran annonçait la date de
    # bascule sans offrir de revenir en arrière (#845). Purement local — résilier ne
    # révoque pas le mandat, donc reprendre n'encaisse rien et n'appelle personne.
    Capability(
        key="billing.resume", handler=_resume, Input=NoInput,
        authz=ORG_ADMIN, Output=BillingStatus,
        rest=RestBinding("POST", "/api/me/billing/resume"),
    ),
    Capability(
        key="billing.payments", handler=_payments, Input=PaymentsInput,
        authz=ORG_MEMBER, Output=PaymentsView,
        rest=RestBinding("GET", "/api/me/billing/payments"),
    ),
    # Admin : forcer un plan sur une org SANS paiement (abonnement comp) ou le
    # retirer (plan=null). Ouvre l'entitlement (options + plafond messagerie du
    # plan) immédiatement. Sert pilotes/partenaires + palier « sur devis ».
    Capability(
        key="billing.admin_set_plan", handler=_admin_set_plan, Input=AdminPlanInput,
        authz=SUPER_ADMIN,
        description="[super admin] Force a plan on an org WITHOUT payment (comp "
                    "subscription): unlocks the plan's options + messaging seat cap "
                    "immediately, no PSP, never charged. Pass plan=null to remove a "
                    "comp plan (refuses to touch a PAID subscription). For pilots, "
                    "partners and the custom 'enterprise' tier.",
        mcp="oto_admin_set_plan",
        rest=RestBinding("POST", "/api/admin/orgs/{org_id}/plan", {"org_id": "org_id"}),
    ),
]

# Feature flag (ADR 0043, dark launch) : billing dormant tant que OTO_BILLING_ENABLED
# n'est pas posé (prod off / canari on). Les descripteurs restent au registre
# (introspection, tests, catalogue admin) ; SEULE la surface est gatée au montage.
CAPABILITIES += [replace(_cap, gate=billing.is_enabled) for _cap in _BILLING_CAPS]
