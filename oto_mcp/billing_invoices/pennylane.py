"""Le seam Pennylane de la PLATEFORME (#488) — la compta d'Otomata, pas celle d'un client.

## Quelle clé, et pourquoi pas celle du coffre

Le connecteur `pennylane` du catalogue est **clé-par-utilisateur** (`auth_modes =
{byo_user, byo_org}`, cf. `providers/pennylane.py`) : chacun pose sa clé sur
`manage.oto.cx/api-keys` et ne voit que sa propre comptabilité. Cette clé-là ne
peut pas servir ici — facturer un abonnement Otomata se fait sur **la comptabilité
d'Otomata**, qui n'appartient à aucun utilisateur et n'est visible d'aucune org
cliente. Et `access.resolve_api_key` résout dans le contexte de l'appelant : le
webhook du PSP et la boucle de fond n'en ont pas.

Elle vient donc de l'**environnement du process**, `OTO_PENNYLANE_API_KEY`, comme
`MOLLIE_API_KEY` — les deux sont des comptes fournisseurs d'Otomata, résolus au
démarrage depuis Scaleway Secret Manager, jamais SOPS, jamais le coffre. La ranger
au coffre en scope `PLATFORM` aurait fait entrer la compta d'Otomata dans la
mécanique de partage du marketplace (`platform_grant`, `share_down`) : un
mécanisme conçu pour PRÊTER une clé, sur la seule clé qu'on ne prêtera jamais.

**Clé absente ⟹ l'émission échoue en le disant** (`pennylane_unconfigured`), et la
tentative reste journalisée `pending`. Jamais un encaissement sans trace.

## Ce que ce module ajoute au client oto-core

Le client vit dans oto-core (`oto.tools.pennylane.PennylaneClient`) — source unique,
on ne réécrit pas d'appels HTTP ici. Ce module pose ce que le client ne fait pas :

- **la clé plateforme** et son refus explicite quand elle manque ;
- **la traduction d'un échec en exception** : `post`/`put` du client RENDENT
  `{"error", "details", "status_code"}` au lieu de lever. Un appelant qui ne
  regarde pas trouverait `result.get("id") is None` et croirait à une facture sans
  numéro. Ici tout passe par `_ok`, qui lève ;
- **la rédaction désactivée** : `FieldFilter()` explicite. Le défaut lit
  `~/.otomata/config.yaml` (politique destinée aux réponses servies à un agent) —
  une règle qui masquerait `id` ou `invoice_number` casserait la facturation en
  silence, sur une machine et pas sur une autre.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from oto.tools.common.errors import UpstreamHTTPError

from .. import billing_vat

logger = logging.getLogger(__name__)

PLATFORM_KEY_ENV = "OTO_PENNYLANE_API_KEY"

# Délai de garde des appels Pennylane ET du téléchargement du PDF. Le chemin
# d'émission est appelé depuis `confirm` (retour navigateur) : sans borne, un
# fournisseur qui ne répond pas immobiliserait un thread de la face REST.
_PDF_TIMEOUT_S = 20.0

# Taux de TVA : notre `vat_rate_bps` → l'enum Pennylane. Le code français est le
# taux en DIXIÈMES de pourcent (FR_200 = 20,0 %, FR_100 = 10 %, FR_55 = 5,5 %,
# FR_21 = 2,1 %) — on le DÉRIVE plutôt que de le figer, pour qu'un changement de
# taux ne laisse pas une constante périmée derrière lui, et on le vérifie contre
# les quatre codes qui existent : dériver un code inexistant produirait un 400
# `NotAnyOf` opaque, qui ne nomme jamais le champ fautif.
_FR_VAT_CODES = frozenset({"FR_200", "FR_100", "FR_55", "FR_21"})

# ⚠️ **Classification fiscale des lignes exonérées — à confirmer avec le conseil.**
# L'enum Pennylane porte `crossborder` (échange transfrontalier) et `extracom`
# (hors Union), sans définition dans sa documentation. Le rapprochement retenu est
# celui des termes : autoliquidation intracommunautaire → `crossborder`, export
# hors UE → `extracom`. Les deux sont à 0 %, donc le TOTAL de la facture est juste
# dans les deux cas — c'est le compte de produit qui dépend du bon code, pas le
# montant. Un contrôle d'écart de montant ne peut donc pas attraper une erreur ici :
# seule la relecture du plan comptable le peut. Une ligne à changer si le conseil
# tranche autrement.
_ZERO_RATE_CODES = {
    billing_vat.SCHEME_REVERSE_CHARGE: "crossborder",
    billing_vat.SCHEME_EXPORT: "extracom",
}


class PennylaneUnavailable(RuntimeError):
    """Émission impossible — la cause est NOMMÉE (`code`) et journalisée telle quelle.

    `code` ∈ `pennylane_unconfigured` (clé plateforme absente),
    `pennylane_error` (le fournisseur a refusé ou n'a pas répondu),
    `pennylane_bad_response` (réponse sans l'information attendue)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def is_configured() -> bool:
    """La plateforme sait-elle facturer ? (clé de la compta d'Otomata posée)"""
    return bool(os.environ.get(PLATFORM_KEY_ENV))


def client():
    """Le client Pennylane d'Otomata. Lève si la clé n'est pas configurée."""
    key = os.environ.get(PLATFORM_KEY_ENV)
    if not key:
        raise PennylaneUnavailable(
            "pennylane_unconfigured",
            f"la facturation exige la clé Pennylane d'Otomata dans l'env du process "
            f"({PLATFORM_KEY_ENV}, posée au boot depuis Scaleway Secret Manager). "
            f"Ce n'est PAS la clé d'un utilisateur du connecteur `pennylane`.")
    from oto.tools.common.field_filter import FieldFilter
    from oto.tools.pennylane import PennylaneClient

    return PennylaneClient(api_key=key, field_filter=FieldFilter())


def _ok(appel: Callable[[], Any], what: str) -> dict:
    """EXÉCUTE un appel Pennylane et rend son retour, ou lève une exception NOMMÉE.

    Le passage obligé prend une fonction, pas un résultat : depuis oto-core#77 le
    client LÈVE sur refus amont, et une exception levée dans l'argument n'aurait
    jamais atteint un contrôle placé après l'appel. Le geste doit donc se produire
    ici, sous la garde.

    Tout ce qui remonte du fournisseur est traduit en `PennylaneUnavailable`, avec
    sa cause nommée : c'est ce que la chaîne d'émission attend, et rien d'autre ne
    l'arrête proprement."""
    try:
        result = appel()
    except PennylaneUnavailable:
        raise
    except UpstreamHTTPError as e:
        raise PennylaneUnavailable(
            "pennylane_error",
            f"{what} — HTTP {e.status_code} : {str(e.body)[:300]}") from e
    except RuntimeError as e:
        # Refus sans statut HTTP : réseau, débit limité, corps illisible.
        raise PennylaneUnavailable("pennylane_error", f"{what} — {e}") from e
    if not isinstance(result, dict):
        raise PennylaneUnavailable("pennylane_bad_response",
                                   f"{what} — réponse inattendue ({type(result).__name__})")
    return result


def vat_rate_code(scheme: Optional[str], rate_bps: Optional[int]) -> str:
    """Le code TVA Pennylane d'une ligne, depuis NOTRE régime (`billing_vat`).

    Refuse plutôt que de deviner : une facture émise avec un code de TVA faux est
    un document comptable faux, et il ne se corrige que par un avoir."""
    code = _ZERO_RATE_CODES.get(scheme or "")
    if code:
        return code
    if scheme != billing_vat.SCHEME_FR:
        raise PennylaneUnavailable("pennylane_bad_response",
                                   f"régime de TVA inconnu pour une facture : {scheme!r}")
    derive = f"FR_{int(rate_bps or 0) // 10}"
    if derive not in _FR_VAT_CODES:
        raise PennylaneUnavailable(
            "pennylane_bad_response",
            f"aucun code TVA Pennylane pour un taux de {rate_bps} points de base "
            f"(codes connus : {', '.join(sorted(_FR_VAT_CODES))})")
    return derive


# ── le client (l'org qui paie), rapproché par sa référence externe ───────────

def customer_external_reference(org_id: int) -> str:
    """La clé de rapprochement d'une org chez Pennylane. STABLE et unique : c'est
    elle qui empêche un second client d'être créé au deuxième paiement (Pennylane
    refuse d'ailleurs un doublon en 422 « External reference has already been
    taken »)."""
    return f"oto-org-{org_id}"


def _address(identity: dict) -> str:
    lignes = [identity.get("address_line"), identity.get("address_line2")]
    return ", ".join([str(x).strip() for x in lignes if x and str(x).strip()])


def sync_customer(org_id: int, identity: dict) -> int:
    """Retrouve — ou crée — le client Pennylane de cette org, et rend son id.

    Le rapprochement se fait sur `external_reference` via le filtre SERVEUR de
    Pennylane (un seul appel, exhaustif), jamais sur le nom : deux orgs peuvent
    porter la même raison sociale, et une org peut changer la sienne.

    Le numéro de TVA est posé APRÈS création (`update_customer`) : il n'est pas un
    paramètre de création côté oto-core, et il est indispensable — c'est lui qui
    figure sur une facture en autoliquidation."""
    c = client()
    ref = customer_external_reference(org_id)
    try:
        found = c.find_customer_by_external_reference(ref)
    except PennylaneUnavailable:
        raise
    except Exception as e:            # UpstreamHTTPError, réseau, etc.
        raise PennylaneUnavailable("pennylane_error",
                                   f"recherche du client {ref} : {e}") from e

    champs = {"name": (identity.get("legal_name") or "").strip(),
              "vat_number": identity.get("vat_number") or None}
    if found and found.get("id"):
        cid = int(found["id"])
        # Le client existe : on remet à jour ce qui a pu bouger depuis (raison
        # sociale, n° de TVA). Une facture doit porter l'identité COURANTE.
        _ok(lambda: c.update_customer(cid, **{k: v for k, v in champs.items() if v}),
            f"mise à jour du client Pennylane {cid}")
        return cid

    emails = [identity["billing_email"]] if identity.get("billing_email") else None
    cree = _ok(lambda: c.create_customer(
        name=champs["name"], emails=emails, address=_address(identity),
        postal_code=identity.get("postal_code"), city=identity.get("city"),
        country_alpha2=identity.get("country_code") or "FR",
        external_reference=ref), f"création du client Pennylane {ref}")
    cid = cree.get("id")
    if not cid:
        raise PennylaneUnavailable("pennylane_bad_response",
                                   f"client {ref} créé sans id")
    cid = int(cid)
    if champs["vat_number"]:
        _ok(lambda: c.update_customer(cid, vat_number=champs["vat_number"]),
            f"n° de TVA du client Pennylane {cid}")
    return cid


# ── le document ──────────────────────────────────────────────────────────────

def invoice_external_reference(payment_ref: str, kind: str) -> str:
    """La clé d'idempotence CÔTÉ PENNYLANE, dérivée du paiement.

    Notre contrainte d'unicité protège notre table ; celle-ci protège la
    comptabilité — une reprise qui repartirait après un crash retrouve le document
    déjà créé au lieu d'en émettre un second."""
    return (f"oto-payment-{payment_ref}" if kind == "invoice"
            else f"oto-refund-{payment_ref}")


def find_document(external_reference: str) -> Optional[dict]:
    """Le document déjà émis pour cette référence, ou None. Anti-doublon d'une
    reprise : un crash entre la création et la finalisation laisse un brouillon,
    qu'on retrouve ici plutôt que d'en créer un second."""
    try:
        return client().find_invoice_by_external_reference(external_reference)
    except PennylaneUnavailable:
        raise
    except Exception as e:
        raise PennylaneUnavailable(
            "pennylane_error",
            f"recherche du document {external_reference} : {e}") from e


def create_document(*, kind: str, customer_id: int, date: str, deadline: str,
                    label: str, amount_ht: int, vat_scheme: Optional[str],
                    vat_rate_bps: Optional[int], external_reference: str,
                    free_text: Optional[str], currency: str = "EUR") -> dict:
    """Crée le BROUILLON d'une facture (ou d'un avoir) à UNE ligne.

    Brouillon d'abord, exprès : c'est la seule fenêtre où le document peut encore
    être comparé à ce qui a réellement été débité avant d'être gravé. Une facture
    finalisée ne se supprime pas — elle ne se corrige que par un avoir.

    La ligne est de forme « libre » (label + prix + unité + taux), pas un produit
    du catalogue : le libellé porte la période, qui change à chaque échéance. ⚠️ Les
    deux formes sont exclusives chez Pennylane, tout mélange rend un 400 `NotAnyOf`
    qui ne dit pas quel champ pèche."""
    c = client()
    ligne = {
        "label": label,
        "quantity": 1,
        "unit": "piece",
        # Prix unitaire HT en STRING décimale — le montant interne est en centimes.
        "raw_currency_unit_price": f"{amount_ht / 100:.2f}",
        "vat_rate": vat_rate_code(vat_scheme, vat_rate_bps),
    }
    args = dict(customer_id=customer_id, date=date, deadline=deadline,
                lines=[ligne], external_reference=external_reference,
                pdf_free_text=free_text or None, draft=True, currency=currency)
    # `create_credit_note` inverse lui-même le signe des quantités : la nature
    # « avoir » est structurelle côté oto-core, jamais laissée à l'appelant.
    fn = c.create_credit_note if kind == "credit_note" else c.create_customer_invoice
    return _ok(lambda: fn(**args), f"création du document {external_reference}")


def finalize(invoice_id: int) -> dict:
    """Fige le document : c'est la finalisation qui lui donne son NUMÉRO (la
    numérotation continue appartient à Pennylane) et son PDF."""
    return _ok(lambda: client().finalize_invoice(invoice_id),
               f"finalisation du document {invoice_id}")


def link_credit_note(invoice_id: int, credit_note_id: int) -> None:
    """Rattache l'avoir à la facture qu'il annule. L'attribut `credited_invoice_id`
    de la création est cassé côté fournisseur (changelog Pennylane) — ce point de
    terminaison est le seul lien qui fonctionne. Un lien qui échoue ne perd pas
    l'avoir : il est émis, seule la liaison manque, et on le DIT."""
    try:
        _ok(lambda: client().link_credit_note(invoice_id, credit_note_id),
            f"liaison de l'avoir {credit_note_id} à la facture {invoice_id}")
    except PennylaneUnavailable as e:
        logger.error("facturation: avoir %s émis mais NON lié à la facture %s — %s",
                     credit_note_id, invoice_id, e)


def fetch_pdf(document: dict, external_reference: str) -> tuple[Optional[bytes], Optional[str]]:
    """Télécharge le PDF du document. Rend `(octets, url)`, `(None, None)` si le
    fournisseur n'a pas (encore) d'URL à donner.

    ⚠️ `public_file_url` **expire en 30 minutes** : on télécharge les octets tout
    de suite et on les range. Stocker l'URL comme « lien vers la facture » aurait
    donné un lien mort une demi-heure plus tard — dans un e-mail, on ne s'en
    apercevrait qu'en le voyant échouer chez le client.

    Un PDF absent n'est PAS une émission ratée : la facture existe, elle porte son
    numéro, et la reprise retéléchargera. C'est pour ça que l'échec est rendu
    plutôt que levé."""
    import httpx

    url = document.get("public_file_url")
    if not url:
        # Le brouillon n'a pas de fichier ; l'objet rendu par la finalisation ne le
        # porte pas toujours. On relit alors le document par sa référence.
        relu = find_document(external_reference) or {}
        url = relu.get("public_file_url")
        document = relu or document
    if not url:
        return None, None
    r = httpx.get(url, timeout=_PDF_TIMEOUT_S, follow_redirects=True)
    if r.status_code != 200 or not r.content:
        logger.warning("facturation: PDF du document %s non téléchargé (HTTP %s)",
                       external_reference, r.status_code)
        return None, url
    return r.content, url
