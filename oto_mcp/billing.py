"""Billing par org (ADR 0043) — abonnement unique, PSP Mollie.

Le cycle est piloté ICI (miroir local `org_subscriptions` = source de vérité,
PSP-agnostique par conception ADR 0043) :
- `subscribe` ouvre le PREMIER paiement sur la page de checkout hébergée Mollie
  (`sequenceType=first` — 3DS carte ou collecte IBAN + mandat SEPA gérés par eux,
  UN seul flux) et journalise le paiement ;
- `confirm` LIT le paiement au retour du payeur (et en réconciliation) : encaissé
  (`paid`) → journalise l'encaissement AUSSITÔT, puis récupère le mandat réutilisable
  né du checkout et pose le miroir `active` — c'est LUI qui ouvre l'entitlement,
  jamais le redirect brut. Le mandat met quelques minutes à apparaître chez Mollie :
  tant que la fenêtre court, son absence est une ATTENTE (`pending_mandate`), jamais
  un refus servi au payeur — voir #493 ;
- `cancel` marque la résiliation à fin de période (l'entitlement court jusqu'à
  `current_period_end` ; le billing_runner fera la bascule).

Bascule Stancer→Mollie (ADR 0043, amende 2026-07-24) : Mollie **unifie carte et
SEPA** derrière un customer + un mandat créé au premier paiement → plus de chemin
SEPA séparé (IBAN tokenisé + signature OTP + ICS créancier). Le rejeu MIT tire sur
`customerId`+`mandateId`. Webhooks natifs (barreau ultérieur) ; polling = socle.

Le plan (prix, options débloquées) vit dans `PLANS` — mapping en CODE (pas de
table) : la vérité produit est versionnée et relue par l'entitlement (has_option,
2e source). ⚠️ Valeurs actuelles = prix actés Alexis 2026-07-06, **HORS TAXES**.

⚠️ **Le montant DÉBITÉ est le TTC** depuis #486 : le prix du palier est un HT, et
le taux dépend du pays du payeur (`billing_vat`). D'où l'ordre imposé — identité
de facturation d'abord, paiement ensuite : `subscribe` refuse tant que l'org n'a
pas dit qui paie et depuis quel pays, parce que sans cela le montant à prendre
n'est pas connu. Le calcul est fait par UN seul seam (`tax_for_org`), partagé
avec l'échéance du `billing_runner`.

⚠️ **Souscrire demande un CONSENTEMENT, pas seulement un moyen de paiement**
(#487) : `subscribe` refuse aussi tant que l'appelant n'a pas accepté les
documents du contexte `purchase` (CGU + CGV + DPA) à leur version courante. Sans
acceptation horodatée, les CGV et le DPA ne sont opposables à personne. Les deux
préalables sont évalués ENSEMBLE et rendus d'un coup (`_purchase_preconditions`) :
un tunnel qui les découvre l'un après l'autre fait remplir un formulaire pour
opposer une case à cocher au clic suivant.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import billing_consent, billing_grants, billing_vat, mollie_client
from . import db
from .db import billing as db_billing
# Le format de date servi par l'API est défini UNE fois, dans la couche DB (le row
# factory normalise tout datetime relu). Une réponse qui construit sa date sans
# passer par là fabrique un second format pour le même champ — cf. #291.
from .db._conn import _normalize_value

logger = logging.getLogger(__name__)

# Le mandat réutilisable ne naît pas AVEC l'encaissement : chez Mollie il apparaît
# quelques minutes après (1,4 s après le paiement du 25/08, il n'existait pas ;
# visible à +5 min). Cette fenêtre est donc la durée pendant laquelle une
# souscription est « en vol » — pendant laquelle un mandat absent est une COURSE et
# non un refus, et pendant laquelle ouvrir un second checkout ne peut que débiter
# deux fois (#493).
PENDING_WINDOW = timedelta(minutes=30)
# Cadence de re-sonde suggérée au client tant que le mandat n'est pas visible.
MANDATE_RETRY_AFTER_S = 15
# Nom du paramètre qui porte l'identité du paiement sur l'URL de retour navigateur.
RETURN_REF_PARAM = "payment_ref"


def is_enabled() -> bool:
    """Feature flag global (ADR 0043, dark launch) : la surface billing (capacités
    REST/MCP + nav dashboard + runner) n'est exposée QUE si `OTO_BILLING_ENABLED=1`.
    Absent/0 = dormant. Piloté par-déploiement (prod off tant que le PSP n'est pas
    live, canari on) sans divergence de branche ni revert."""
    return os.environ.get("OTO_BILLING_ENABLED", "0") == "1"

# plan → prix (centimes), intervalle, options de connecteur débloquées (couche 3,
# lues par access.has_option). Prix HT mensuels : 19/99/249/499 (Alexis 2026-08-03,
# 2ᵉ palier 49 → 99 le 2026-08-29, #490 — personne n'y était abonné, pas de rétroactif).
# Chaque plan CONFIGURE l'org à l'activation → une seule action admin.
# **Modèle simplifié (2026-08-03)** : gratuit = pas d'Unipile (option bloquée, sauf
# `comp` admin = « offert ») ; payant = Unipile + clés plateforme SANS quota
# (`unmetered`). On NE facture PLUS au nombre de comptes messagerie → tous les
# paliers ont `unipile_accounts=None` (illimité). ⚠️ Les 4 paliers débloquent donc
# AUJOURD'HUI exactement la même chose et ne diffèrent QUE par le prix — la
# différenciation (« payant = Unipile, mais pas que ») viendra plus tard (options
# premium par palier). `unmetered=True` = fin des credits d'appel.
PLANS: dict[str, dict] = {
    "standard": {
        "label": "Standard", "amount": 1900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "premium": {
        "label": "Premium", "amount": 9900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "business": {
        "label": "Business", "amount": 24900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "enterprise": {
        "label": "Entreprise", "amount": 49900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
}


def plans() -> list[dict]:
    """Catalogue public (l'UI billing du dashboard boucle dessus)."""
    return [{"plan": k, "custom": v.get("custom", False),
             **{f: v[f] for f in ("label", "amount", "currency", "interval",
                                  "unipile_accounts")}}
            for k, v in PLANS.items()]


def plan_options(plan: str) -> frozenset[str]:
    """Options de connecteur débloquées par `plan` (consommé par access.has_option)."""
    meta = PLANS.get(plan)
    return frozenset(meta["options"]) if meta else frozenset()


def plan_is_unmetered(plan: str) -> bool:
    """Le plan lève-t-il les quotas des clés plateforme ? (fin des credits d'appel)."""
    meta = PLANS.get(plan)
    return bool(meta and meta.get("unmetered"))


def apply_plan_entitlements(org_id: int, plan: str) -> None:
    """Configure l'org d'après son plan à l'ACTIVATION — le geste qui remplace
    le micro-management admin (options + plafond messagerie posés d'un coup).
    Idempotent. `unipile_accounts=None` (devis) = plafond levé."""
    meta = PLANS.get(plan)
    if meta is None:
        return
    db.set_org_unipile_limit(org_id, meta.get("unipile_accounts"))


def _add_period(dt: datetime, interval: str) -> datetime:
    """Échéance suivante au mois/an CALENDAIRE (pas d'approximation 30 j) —
    borné au dernier jour du mois cible (31/01 + 1 mois → 28/02)."""
    if interval == "year":
        return _safe_replace(dt, year=dt.year + 1, month=dt.month)
    month = dt.month + 1
    year = dt.year + (1 if month > 12 else 0)
    return _safe_replace(dt, year=year, month=((month - 1) % 12) + 1)


def _safe_replace(dt: datetime, *, year: int, month: int) -> datetime:
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def webhook_url() -> str:
    """URL publique que Mollie rappelle à chaque changement d'état d'un paiement
    (base = `OTO_MCP_PUBLIC_URL`, cf. Logto/Google OAuth). Portée par chaque
    paiement créé → réconciliation événementielle en complément du polling."""
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/billing/webhook"


# ── TVA : le seam unique entre l'identité de l'org et un montant ─────────────

def tax_for_org(org_id: int, amount_ht: int) -> dict:
    """La décomposition fiscale d'un montant HT pour CETTE org, au moment du débit.

    Un seul seam pour les deux chemins qui débitent — `subscribe` (premier paiement)
    et `billing_runner._charge_one` (échéance). Deux calculs auraient divergé au
    premier changement de règle, et la divergence se verrait sur une facture, pas
    dans un test.

    Lève `billing_identity_required` (identité absente/incomplète) ou
    `vat_consumer_unsupported` (particulier de l'Union hors France) : dans les deux
    cas, il n'y a pas de montant correct à prendre, et prendre le HT « en attendant »
    est exactement ce que #486 répare."""
    return billing_vat.tax_for_identity(
        amount_ht, db_billing.get_billing_identity(org_id))


# ── les préalables de la souscription ────────────────────────────────────────

def _purchase_preconditions(org_id: int, sub: Optional[str],
                            amount_ht: int) -> tuple[Optional[dict], list[dict]]:
    """Les DEUX préalables d'un achat, évalués ensemble. Rend `(décomposition
    fiscale, manques)` — la décomposition est `None` dès que l'identité manque.

    **L'ordre est celui du tunnel : identité, puis légal.** Il n'est pas cosmétique.
    Le payeur accepte des CGV *pour un montant*, et le montant n'existe qu'une fois
    le pays connu (c'est lui qui décide de la TVA, #486). Faire consentir d'abord et
    chiffrer ensuite ferait accepter un prix qui n'a pas encore été annoncé — le
    consentement est le DERNIER geste avant la page de paiement.

    Mais ordonner n'est pas refuser un à la fois : les deux manques partent
    ENSEMBLE, et c'est ce qui permet au tunnel de peindre l'écran entier — le
    formulaire d'identité ET les trois cases — en un seul aller-retour.

    Aucun effet de bord : rien n'est créé chez le PSP tant que cette liste n'est pas
    vide. Un refus après création laisserait derrière lui un customer et une page
    payable."""
    manques: list[dict] = []
    tax: Optional[dict] = None
    try:
        tax = tax_for_org(org_id, amount_ht)
    except ValueError as e:
        message = str(e)
        manques.append({"code": message.split(":", 1)[0].strip(), "message": message})
    legal = billing_consent.legal_blocker(sub)
    if legal:
        manques.append(legal)
    return tax, manques


# ── souscription ─────────────────────────────────────────────────────────────

def _elapsed_since(value, now: datetime) -> Optional[timedelta]:
    """Temps écoulé depuis un horodatage, quelle que soit sa forme.

    Le journal est relu NORMALISÉ par le row factory (« YYYY-MM-DD HH:MM:SS », sans
    fuseau, donc UTC implicite) ; Mollie, lui, rend de l'ISO 8601 à offset (parfois
    suffixé `Z`, que `fromisoformat` ne lit pas avant 3.11). `None` = horodatage
    illisible ou absent — l'appelant décide, il ne devine pas.
    """
    if isinstance(value, datetime):
        return now - (value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # noqa: SILENT — horodatage illisible : on rend None, et l'appelant tranche
        except ValueError:
            return None
        return now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    return None


def _since_paid(payment: dict, row: dict, now: datetime) -> Optional[timedelta]:
    """Depuis combien de temps l'argent est-il PRIS ?

    C'est cette durée-là, pas l'âge du checkout, qui décide si un mandat manquant est
    une course ou un incident : une page de paiement peut rester ouverte une
    demi-heure avant d'être payée. `paidAt` du PSP fait donc foi ; à défaut on
    retombe sur l'ouverture du checkout, qui ne peut que MAJORER le délai réel (le
    paiement lui est forcément postérieur) — jamais l'inverse, donc jamais une
    attente écourtée sans le savoir."""
    since = _elapsed_since(payment.get("paidAt"), now)
    if since is not None:
        return since
    return _elapsed_since(row.get("created_at"), now)


def _age_label(age: Optional[timedelta]) -> str:
    if age is None:
        return "à l'instant"
    s = int(age.total_seconds())
    return f"{s} s" if s < 120 else f"{s // 60} min"


def _return_url_with_ref(return_url: str, payment_id: str) -> str:
    """Ajoute `?payment_ref=tr_…` à l'URL de retour du navigateur (en écrasant une
    valeur déjà posée), sans toucher au reste de la query string du dashboard."""
    parts = urlparse(return_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != RETURN_REF_PARAM]
    query.append((RETURN_REF_PARAM, payment_id))
    return urlunparse(parts._replace(query=urlencode(query)))


def _payment_pending_message(in_flight: dict, now: datetime) -> str:
    """Le refus dit QUI occupe la place, depuis quand, et ce qu'il reste à faire —
    le remède n'est pas le même selon que l'argent est déjà pris ou non."""
    ref = in_flight.get("payment_intent_id") or f"ligne {in_flight.get('id')}"
    statut = in_flight.get("status")
    age = _age_label(_elapsed_since(in_flight.get("created_at"), now))
    suite = (
        "il est DÉJÀ ENCAISSÉ : l'abonnement s'ouvre seul dès que le mandat est "
        "disponible chez le PSP, quelques minutes plus tard"
        if statut == "paid" else
        "sa page de paiement est encore payable : la terminer ou la laisser expirer"
    )
    return (f"payment_pending: une souscription est déjà en cours pour cette org "
            f"(paiement {ref}, statut {statut}, ouvert il y a {age}) — {suite}. "
            f"Ouvrir un second paiement débiterait deux fois.")


def _org_customer_id(org_id: int, existing: Optional[dict]) -> Optional[str]:
    """Le customer Mollie de l'org — un seul, pour toujours (#493).

    Il se lit sur le miroir quand il existe, SINON sur le journal : le miroir n'est
    posé qu'à `confirm`, donc au deuxième clic d'une souscription en cours il n'y a
    encore rien à relire. C'est là qu'un second customer naissait, avec son propre
    mandat — celui que le rejeu MIT ne tirera jamais."""
    if existing and existing.get("customer_id"):
        return existing["customer_id"]
    return db_billing.last_customer_id_for_org(org_id)


def subscribe(org_id: int, plan: str, return_url: str, *, sub: Optional[str],
              method: str = "card") -> dict:
    """Ouvre la souscription. UN seul flux (Mollie unifie carte et SEPA) : premier
    paiement `sequenceType=first` → l'URL renvoyée = la page de checkout hébergée
    Mollie où le payeur finit le geste (3DS carte, ou saisie IBAN + acceptation du
    mandat SEPA). `method` ∈ {card, sepa} restreint la page ; le mandat réutilisable
    naît à l'encaissement. Le miroir n'est PAS posé ici — il naît à `confirm`
    (paiement constaté), qui relit le plan de la `metadata` du paiement.

    **Une seule souscription en vol à la fois** (#493) : tant qu'un premier paiement
    de moins de `PENDING_WINDOW` n'a pas définitivement échoué, un second checkout
    est refusé (`payment_pending`). C'est le geste le plus banal du monde — payer,
    voir un échec, recliquer — et c'est lui qui a débité 38 € un abonnement à 19 €.
    Corollaire assumé : résilier puis re-souscrire dans la demi-heure est refusé le
    temps que la fenêtre s'écoule, le refus disant quel paiement l'occupe.

    `sub` = l'appelant, et il est OBLIGATOIRE : c'est une personne qui accepte des
    documents, pas une organisation. Sans lui rien n'est accepté et la souscription
    est refusée — le paramètre n'a pas de défaut pour que l'oubli soit une erreur de
    programmation, jamais un gate ouvert. (Un abonnement OFFERT par un admin
    `admin_set_plan` ne passe pas par ici : rien n'y est vendu ni débité, il n'y a
    donc pas de consentement d'achat à recueillir.)"""
    meta = PLANS.get(plan)
    if meta is None:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    if meta.get("custom"):
        raise ValueError("custom_plan: ce palier est sur devis — contacter "
                         "Otomata (un admin l'active en abonnement comp)")
    if method not in ("card", "sepa"):
        raise ValueError(f"unknown_method: {method!r} (card | sepa)")
    existing = db_billing.get_org_subscription(org_id)
    if existing and existing["status"] == "active" and not existing.get("canceled_at"):
        raise ValueError("already_subscribed: l'org a déjà un abonnement actif")

    now = datetime.now(timezone.utc)
    in_flight = db_billing.pending_initial_payment(org_id, since=now - PENDING_WINDOW)
    if in_flight:
        raise ValueError(_payment_pending_message(in_flight, now))

    # LES PRÉALABLES AVANT LE CHECKOUT (#486 pour l'identité, #487 pour le légal).
    # Le prix du palier est un HT ; ce qui part au PSP est le TTC, et le taux dépend
    # du pays du payeur. Et on ne vend pas sans consentement écrit. Les deux se
    # tranchent donc AVANT de créer quoi que ce soit chez Mollie : un refus après
    # création laisserait un customer et une page payable derrière lui.
    tax, manques = _purchase_preconditions(org_id, sub, meta["amount"])
    if manques:
        raise billing_consent.PurchaseBlocked(manques)

    customer_id = _org_customer_id(org_id, existing)
    if not customer_id:
        cust = mollie_client.create_customer(
            name=f"Otomata org {org_id}", metadata={"org_id": str(org_id)})
        customer_id = cust["id"]

    payment = mollie_client.create_first_payment(
        tax["amount_ttc"], customer_id=customer_id, currency=meta["currency"],
        redirect_url=return_url, description=f"Abonnement {meta['label']}",
        method=mollie_client.mollie_method(method), webhook_url=webhook_url(),
        # le plan voyage dans la metadata du paiement (pas d'état serveur pendant
        # le checkout : confirm le relit → survit à un restart).
        metadata={"org_id": str(org_id), "plan": plan})
    db_billing.insert_billing_payment(
        org_id, "initial", tax["amount_ttc"], currency=meta["currency"],
        payment_intent_id=payment["id"], status=payment.get("status", "open"),
        customer_id=customer_id, tax=tax)
    # Le retour navigateur doit DIRE quel paiement il vient de conclure. Mollie
    # n'ajoute rien à `redirectUrl`, et cette URL se fixe à la création — où l'id du
    # paiement n'existe pas encore : on la ré-écrit donc juste après, le paiement
    # étant encore ouvert. Un refus de Mollie ne casse pas un checkout payable pour
    # un confort : `confirm` retombe sur le plus récent non conclu et le webhook,
    # lui, connaît toujours l'identité du paiement — mais on le DIT.
    try:
        mollie_client.update_payment(
            payment["id"],
            redirect_url=_return_url_with_ref(return_url, payment["id"]))
    except mollie_client.MollieError as e:
        logger.warning("billing: URL de retour non datée du paiement %s (org %s) — "
                       "le retour navigateur devra deviner : %s",
                       payment["id"], org_id, e)
    return {"checkout_url": mollie_client.checkout_url(payment),
            "payment_intent_id": payment["id"], "plan": plan, "method": method,
            # La décomposition part AVEC la réponse : le tunnel doit pouvoir annoncer
            # « 19,00 € HT + 3,80 € de TVA = 22,80 € » avant d'envoyer sur la page de
            # checkout, sinon le payeur découvre le TTC chez Mollie.
            "amount_ht": tax["amount_ht"], "vat_rate_bps": tax["vat_rate_bps"],
            "vat_amount": tax["vat_amount"], "amount_ttc": tax["amount_ttc"],
            "vat_scheme": tax["vat_scheme"], "vat_mention": tax["vat_mention"]}


def confirm(org_id: int, payment_ref: Optional[str] = None) -> dict:
    """Fait avancer la souscription en cours : lit un premier paiement non conclu ;
    encaissé (`paid`) → journalise l'encaissement, récupère le mandat réutilisable né
    du checkout, pose le miroir `active` (carte comme SEPA — même chemin). Idempotent :
    re-confirmer un abonnement déjà actif est un no-op informatif.

    `payment_ref` = l'identifiant du paiement à traiter, quand l'appelant le
    connaît. Le **webhook** le connaît (c'est celui qu'il vient de recevoir) et
    DOIT le passer ; le **retour navigateur** le porte désormais aussi (#493, il est
    daté sur l'URL de retour) ; le **polling** ne le connaît pas et prend le plus
    récent, ce qui reste correct pour lui. Sans ce paramètre, l'identité du paiement
    encaissé se perdait entre le webhook et ce chemin (#291).

    Encaissé sans mandat encore visible = `pending_mandate`, PAS un refus : le mandat
    réutilisable apparaît quelques minutes après le paiement chez Mollie (#493)."""
    sub_row = db_billing.get_org_subscription(org_id)
    # Idempotence D'ABORD. Elle tenait jusqu'ici au fait qu'un paiement confirmé
    # sortait de la file (`paid` = terminal) ; depuis #493 un encaissement RESTE
    # candidat tant que le miroir n'est pas posé, donc sans ce garde-fou explicite
    # re-confirmer un abonnement ouvert repousserait `current_period_end` d'une
    # période à chaque appel. Un abonnement résilié (canceled_at) n'est pas concerné :
    # il peut légitimement re-souscrire, exactement comme dans `subscribe`.
    if sub_row and sub_row["status"] == "active" and not sub_row.get("canceled_at"):
        return {"status": "active", "plan": sub_row["plan"]}

    # Candidats = les premiers paiements qui n'ont pas DÉFINITIVEMENT échoué. `paid`
    # en fait partie (#493) : l'encaissement est journalisé dès son constat, avant le
    # contrôle de mandat, donc un paiement réussi dont l'abonnement reste à ouvrir se
    # présente ici avec un statut terminal. Seuls failed/canceled/expired sortent.
    failed = set(db_billing.TERMINAL_PAYMENT_STATUSES) - {"paid"}
    candidates = [
        # ⚠️ `limit` explicite : au défaut (20), un `initial` ouvert plus ancien
        # devenait carrément INVISIBLE dès qu'une org avait vingt lignes de paiement
        # — donc jamais confirmé, sans le moindre message.
        p for p in db_billing.list_billing_payments(org_id, limit=200)
        if p["kind"] == "initial"
        and p["status"] not in failed
        and p.get("payment_intent_id")
    ]
    if not candidates:
        if sub_row and sub_row["status"] == "active":
            return {"status": "active", "plan": sub_row["plan"]}
        raise ValueError("no_pending_subscription: aucun paiement initial en cours")

    # Rien n'interdit deux souscriptions ouvertes à la fois (retour arrière, page
    # rechargée, hésitation carte/SEPA) : il peut donc exister PLUSIEURS paiements
    # ouverts, chacun avec une page payable. Quand l'appelant sait lequel a été
    # encaissé — le webhook le sait, il vient de le recevoir — on traite CELUI-LÀ.
    # Sans ça, le payeur qui termine l'ANCIENNE page était débité pendant que
    # `confirm` regardait la plus récente, la trouvait non payée, et rendait
    # `pending` : encaissé, aucun droit ouvert, aucune erreur nulle part.
    if payment_ref:
        row = next((p for p in candidates if p["payment_intent_id"] == payment_ref), None)
        if row is None:
            # Le paiement visé n'est pas (ou plus) un initial en cours de cette org :
            # on ne se rabat PAS sur un autre, ce serait confirmer sur la foi d'un
            # encaissement qui concerne autre chose.
            raise ValueError(
                f"unknown_payment: le paiement {payment_ref} n'est pas un paiement "
                "initial en cours pour cette org")
    else:
        row = candidates[0]  # le plus récent (list_billing_payments trie DESC)
    payment = mollie_client.get_payment(row["payment_intent_id"])
    pstatus = str(payment.get("status") or "")

    if pstatus in ("failed", "canceled", "expired"):
        db_billing.update_billing_payment(row["id"], status=pstatus)
        return {"status": "failed", "payment_status": pstatus, **billing_vat.tax_view(row)}
    if pstatus != "paid":
        # pas encaissé : le payeur est peut-être encore sur la page de checkout.
        return {"status": "pending", "payment_status": pstatus, **billing_vat.tax_view(row)}

    # ENCAISSÉ. On le grave AVANT tout le reste (#493) : le journal doit dire ce que
    # le PSP a fait, pas ce que nous avons su en faire. Le statut n'était écrit
    # qu'après le mandat, le plan et la pose du miroir — un paiement réellement
    # débité restait donc `open` au journal dès que l'une de ces étapes échouait, et
    # `subscribe` ne se gardait sur rien.
    db_billing.update_billing_payment(row["id"], status="paid",
                                      payment_id=payment["id"])
    # La facture suit l'ENCAISSEMENT, pas l'ouverture des droits (#488) : elle est
    # due même si le mandat n'est pas né et que le miroir n'est pas encore posé —
    # sinon le cas du 25/08 (argent pris, abonnement pas ouvert) resterait sans
    # document. Le palier vient de la metadata du paiement, seul endroit où il vit
    # pendant le checkout ; le miroir, lui, n'existe pas encore au premier passage.
    from . import billing_invoices as factures     # import tardif : le paquet lit `billing`
    factures.facturer_encaissement(row["id"],
                                   plan=(payment.get("metadata") or {}).get("plan"))

    # encaissé → le mandat réutilisable naît sur le customer… quelques minutes plus
    # tard. À 1,4 s il n'existe pas encore.
    now = datetime.now(timezone.utc)
    customer_id = payment.get("customerId")
    mandate = mollie_client.valid_mandate(customer_id) if customer_id else None
    if not mandate:
        age = _since_paid(payment, row, now)
        if age is None or age < PENDING_WINDOW:
            # COURSE, pas échec : servir un refus au payeur ici, c'est lui annoncer
            # un échec sur un paiement réussi — et il repaie (incident du 25/08).
            # L'abonnement s'ouvrira au prochain passage : re-sonde du navigateur,
            # webhook, ou rattrapage du billing_runner.
            logger.info("billing: org %s encaissée (paiement %s), mandat pas encore "
                        "visible (%s) — en attente", org_id, row["payment_intent_id"],
                        _age_label(age))
            return {"status": "pending_mandate", "payment_status": "paid",
                    "retry_after": MANDATE_RETRY_AFTER_S, **billing_vat.tax_view(row)}
        # Passé la fenêtre, ce n'est plus une course : encaissé sans mandat
        # réutilisable = récurrence impossible. On ne pose PAS un abonnement qu'on ne
        # saura pas renouveler (ADR : jamais de fallback silencieux) — c'est le SEUL
        # cas où le refus `no_mandate` a jamais été vrai, il le reste.
        logger.error("billing: org %s encaissée (paiement %s) SANS mandat après %s — "
                     "récurrence impossible, reprise manuelle",
                     org_id, row["payment_intent_id"], _age_label(age))
        raise RuntimeError(
            "no_mandate: premier paiement encaissé sans mandat valide après "
            f"{int(PENDING_WINDOW.total_seconds() // 60)} min — récurrence "
            "impossible, vérifier le moyen de paiement de la page de checkout")

    plan = (payment.get("metadata") or {}).get("plan")
    if plan not in PLANS:
        raise RuntimeError(f"bad_metadata: plan illisible sur le paiement ({plan!r})")
    meta = PLANS[plan]
    method = mollie_client.method_from_mollie(payment.get("method"))

    period_end = _add_period(now, meta["interval"])
    db_billing.upsert_org_subscription(
        org_id, plan=plan, method=method, provider="mollie",
        customer_id=customer_id, mandate_id=mandate["id"],
        mandate_rum=mandate.get("mandateReference"),
        status="active", current_period_end=period_end, next_billing_at=period_end)
    apply_plan_entitlements(org_id, plan)
    logger.info("billing: org %s abonnée (plan %s, méthode %s, échéance %s)",
                org_id, plan, method, period_end.date())
    return {"status": "active", "plan": plan, "method": method, **billing_vat.tax_view(row),
            # MÊME format que `status`/`cancel`, qui rendent la valeur relue en base
            # (normalisée « YYYY-MM-DD HH:MM:SS » par le row factory). Cette réponse
            # sortait en ISO 8601 avec offset : le même champ, deux formes selon le
            # verbe, donc un client qui parse `confirm` cassait sur `status` (#291).
            # On importe le normaliseur plutôt que de recopier son expression — une
            # seule définition du format, celle de la couche DB.
            "current_period_end": _normalize_value(period_end)}


# ── état & résiliation ───────────────────────────────────────────────────────

def status(org_id: int, *, sub: Optional[str] = None) -> dict:
    """État d'abonnement de l'org — **et ce qui lui est offert sans abonnement**.

    `granted` porte les avantages payants OFFERTS (dons d'option, cf.
    `billing_grants`). Il est joint dans les DEUX branches, et c'est le point : la
    branche « aucun abonnement » est justement celle où un bénéficiaire se voyait
    vendre ce qu'il possédait déjà. Le catalogue `plans` y reste servi — un don n'est
    pas un abonnement, et la voie pour en prendre un ne doit pas se refermer.

    `sub` = l'appelant, pour ses dons PERSONNELS. Omis sur les faces qui décrivent
    une org à un tiers (fiche admin) : voir `billing_grants.granted_benefits`.
    """
    granted = billing_grants.granted_benefits(org_id, sub=sub)
    # L'usage est servi à TOUT LE MONDE, gratifié ou non, abonné ou non : c'est le
    # seul élément de cet écran qui vaut pour tous les comptes.
    usage = billing_grants.monthly_usage(org_id)
    row = db_billing.get_org_subscription(org_id)
    if not row:
        return {"subscribed": False, "plans": plans(),
                "granted": granted, "usage": usage}
    meta = PLANS.get(row["plan"], {})
    comp = row["provider"] == "comp"   # abonnement forcé par un admin (non payé)
    return {
        "subscribed": row["status"] in ("active", "past_due"),
        "plan": row["plan"], "label": meta.get("label"),
        "amount": meta.get("amount"), "currency": meta.get("currency"),
        "interval": meta.get("interval"),
        # Ce que coûtera la PROCHAINE échéance, TVA comprise (#486) : `amount` reste
        # le prix HT du catalogue, et le TTC en est dérivé par l'identité COURANTE de
        # l'org — donc il bouge si l'org déménage, ce qui est le comportement voulu
        # (c'est bien ce qui sera prélevé). Ce qui a DÉJÀ été pris se lit sur
        # billing.payments, qui a figé sa propre décomposition.
        #
        # ⚠️ SAUF sur un abonnement OFFERT : rien n'y sera jamais prélevé, donc il n'y
        # a pas de TTC à annoncer — et poser `vat_blocked` sur une org offerte sans
        # identité de facturation serait une FAUSSE alerte, sur un écran dont c'est
        # tout le rôle de signaler les échéances en danger.
        **(billing_vat.BLANK_PREVIEW if comp else billing_vat.tax_preview(
            meta.get("amount"), db_billing.get_billing_identity(org_id))),
        "status": row["status"], "method": row["method"],
        "comp": comp,
        "current_period_end": row.get("current_period_end"),
        "next_billing_at": row.get("next_billing_at"),
        "grace_until": row.get("grace_until"),
        "canceled_at": row.get("canceled_at"),
        # Ce que le RUNNER a CONSTATÉ, à distinguer de `vat_blocked` juste au-dessus.
        # `vat_blocked` est une prévision recalculée à chaque lecture (« au taux
        # d'aujourd'hui, on ne saurait pas quoi prélever ») ; `block_code` est un fait
        # daté (« l'échéance du 25 n'a PAS pu être tirée, et depuis on sert sans
        # encaisser »). Un blocage de TVA réparé une heure après l'échéance efface le
        # premier et laisse le second : c'est précisément la différence utile.
        "block_code": row.get("block_code"),
        "block_detail": row.get("block_detail"),
        "block_since": row.get("block_since"),
        # Servis dans les DEUX branches : un abonné a lui aussi des dons possibles
        # (une option offerte survit à une souscription) et un usage à voir.
        "granted": granted,
        "usage": usage,
    }


def cancel(org_id: int) -> dict:
    """Résiliation à fin de période : l'entitlement court jusqu'à
    `current_period_end`, plus aucune échéance n'est tirée (next_billing_at
    nettoyé) ; le billing_runner basculera le statut à l'échéance."""
    row = db_billing.get_org_subscription(org_id)
    if not row or row["status"] == "canceled":
        raise ValueError("not_subscribed: aucun abonnement à résilier")
    db_billing.mark_cancel_at_period_end(org_id)
    return status(org_id)


def resume(org_id: int) -> dict:
    """Annule une résiliation : l'abonnement repart sur son cycle, sans rien encaisser.

    Le geste manquait, et son absence coûtait (#845) : l'écran annonçait la date de
    bascule vers le palier gratuit sans offrir de revenir en arrière — **un clic de trop
    était définitif jusqu'à la fin de la période**.

    ⚠️ **Aucun appel au prestataire de paiement, aucun mouvement d'argent.** Résilier ne
    révoque pas le mandat : il est toujours là, et l'abonnement n'a jamais cessé d'être
    `active`. Reprendre, c'est donc défaire deux écritures locales — rien de plus, et
    surtout rien qui touche l'encaissement.

    Les refus NOMMENT ce qui bloque, parce que les trois appellent des gestes
    différents : s'abonner, ne rien faire, ou se réabonner."""
    row = db_billing.get_org_subscription(org_id)
    if not row:
        raise ValueError("not_subscribed: aucun abonnement sur cette org")
    if row["status"] == "canceled":
        # ⚠️ La période est ÉCHUE et le runner a basculé : reprendre ici rouvrirait
        # l'entitlement sans qu'aucune échéance ne soit tirée — un abonnement gratuit
        # créé par un bouton « annuler la résiliation ». C'est un réabonnement, il
        # passe par `subscribe`.
        raise ValueError(
            "already_ended: la période est terminée et l'abonnement est clos — "
            "reprends-le par une nouvelle souscription, pas par une reprise")
    if not row.get("canceled_at"):
        raise ValueError("not_canceled: cet abonnement n'est pas résilié")
    if not db_billing.resume_canceled(org_id):
        # Le `WHERE` n'a rien touché alors que la lecture disait le contraire : le
        # runner est passé entre les deux. On le DIT plutôt que de rendre un succès
        # qui n'a rien fait.
        raise ValueError(
            "already_ended: la résiliation s'est consommée pendant la reprise — "
            "relis l'état avant de rejouer")
    return status(org_id)


# ── admin : forcer / retirer un plan (non payé) ──────────────────────────────

def admin_set_plan(org_id: int, plan: str, *, granted_by: str) -> dict:
    """Force un plan sur une org SANS paiement (abonnement `comp`) — ADR 0043.
    Ouvre l'entitlement immédiatement (options + plafond messagerie du plan),
    jamais de PSP derrière, jamais d'échéance tirée. Sert les pilotes,
    partenaires et le palier « sur devis ». Écrase l'abonnement existant."""
    if plan not in PLANS:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    db_billing.set_comp_subscription(org_id, plan, granted_by=granted_by)
    apply_plan_entitlements(org_id, plan)
    logger.info("billing: plan %s FORCÉ (comp) sur l'org %s par %s",
                plan, org_id, granted_by)
    return status(org_id)


def admin_clear_plan(org_id: int) -> dict:
    """Retire un abonnement `comp` (forcé). Refuse de toucher un abonnement PAYÉ
    (passer par la résiliation) — anti-bévue admin."""
    row = db_billing.get_org_subscription(org_id)
    if not row:
        raise ValueError("not_subscribed: aucun abonnement sur cette org")
    if row["provider"] != "comp":
        raise ValueError("paid_subscription: abonnement payant — résilier via "
                         "cancel, pas admin_clear_plan")
    db_billing.delete_subscription(org_id)
    db.set_org_unipile_limit(org_id, None)   # retire le plafond posé par le plan
    logger.info("billing: plan comp retiré de l'org %s", org_id)
    return {"subscribed": False, "org_id": org_id}


# ── webhook Mollie (réconciliation événementielle) ───────────────────────────

def process_webhook(payment_id: str) -> str:
    """Traite un rappel webhook Mollie (le corps ne porte QUE l'id du paiement —
    on re-fetch l'objet avec NOTRE clé, jamais de confiance dans le POST). Retourne
    l'issue (log) : 'ignored' | 'confirmed' | 'awaiting_mandate' | 'not_confirmed'
    | 'updated' | 'unchanged' | 'refunded'.

    Sécurité : un id inconnu de notre journal est ignoré (un POST forgé ne
    déclenche rien) ; un premier paiement `paid` rejoue `confirm` (idempotent) ;
    sinon on aligne le statut journalisé. Complément du polling (billing_runner),
    pas un remplacement.

    'awaiting_mandate' = encaissement pris en compte, mandat pas encore né chez
    Mollie (#493). C'est le cas NOMINAL du webhook, qui arrive une seconde après le
    paiement : le compter comme un incident enverrait chercher un défaut là où il
    n'y en a pas."""
    row = db_billing.get_billing_payment_by_ref(payment_id)
    if not row:
        return "ignored"
    payment = mollie_client.get_payment(payment_id)
    status = str(payment.get("status") or "")

    # REMBOURSEMENT (#488). Mollie appelle le MÊME webhook qu'un changement de
    # statut quand un remboursement est créé ou change d'état — les remboursements
    # n'ont pas d'URL à eux (docs.mollie.com/docs/webhooks). Le paiement reste
    # `paid` : c'est `amountRefunded`, absent tant que rien n'est remboursé, qui
    # porte l'information. Traité AVANT le reste, et il conclut : la facture, elle,
    # a été émise quand le paiement est passé `paid`.
    rembourse = mollie_client.cents_from_amount(payment.get("amountRefunded"))
    if rembourse:
        from . import billing_invoices as factures  # import tardif : le paquet lit `billing`
        factures.avoir_remboursement(row["id"], rembourse)
        return "refunded"

    if row["kind"] == "initial" and status == "paid":
        # On passe l'identifiant du paiement ENCAISSÉ : sans lui, `confirm` repartait
        # du plus récent et pouvait confirmer un autre paiement — ou rien (#291).
        try:
            out = confirm(row["org_id"], payment_ref=payment_id)
        except (ValueError, RuntimeError) as e:
            # Un encaissement qu'on ne sait pas transformer en droits est un incident
            # à INVESTIGUER, pas une exception à propager : la laisser remonter ferait
            # répondre 500 au webhook, donc relancer Mollie en boucle sur un état que
            # le retry ne réparera pas. On trace fort et on absorbe.
            logger.error("webhook: paiement %s encaissé (org %s) mais NON confirmé — %s",
                         payment_id, row["org_id"], e)
            return "not_confirmed"
        # Et on rend l'issue RÉELLE : annoncer « confirmed » quoi qu'il arrive faisait
        # affirmer au journal le contraire de ce qui s'était passé, ce qui est pire
        # qu'un silence — on cherche l'incident ailleurs.
        if out.get("status") == "pending_mandate":
            # Le mandat naît quelques minutes après l'encaissement : à l'instant du
            # webhook il n'existe pas encore. Rien à investiguer — la reprise est déjà
            # câblée (re-sonde du navigateur, rattrapage du billing_runner).
            logger.info("webhook: paiement %s encaissé (org %s), mandat pas encore "
                        "visible — abonnement en attente", payment_id, row["org_id"])
            return "awaiting_mandate"
        if out.get("status") != "active":
            logger.error("webhook: paiement %s encaissé (org %s), abonnement toujours "
                         "%s — investiguer", payment_id, row["org_id"], out.get("status"))
            return "not_confirmed"
        return "confirmed"
    if status and status != row["status"]:
        db_billing.update_billing_payment(row["id"], status=status)
        if status == "paid":
            # Une ÉCHÉANCE encaissée : le premier paiement, lui, passe par `confirm`
            # (branche du dessus), qui facture déjà. Sans cette ligne, une échéance
            # attendrait le tick du runner pour être facturée.
            from . import billing_invoices as factures   # import tardif
            factures.facturer_encaissement(row["id"])
        return "updated"
    return "unchanged"
