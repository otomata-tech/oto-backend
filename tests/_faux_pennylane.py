"""Un Pennylane de bureau : le fournisseur, mais en mémoire (#488).

Il remplace `PennylaneClient` — **pas** les fonctions de
`billing_invoices/pennylane.py`. La différence est tout l'intérêt : le rapprochement
du client, l'ordre brouillon → contrôle → finalisation, la traduction d'un refus en
exception et le choix du code de TVA sont donc EXERCÉS, pas contournés. Seule la
résolution de la clé plateforme est court-circuitée (elle a ses propres tests).

Il calcule les totaux comme le ferait Pennylane — HT × (1 + taux du code de TVA) —
pour que le contrôle d'écart de montant ait quelque chose à contrôler. Sans ça, le
test qui refuse une facture fausse ne prouverait rien.

Le mailer est simulé au même endroit (`oto_mcp.billing_invoices.mail._send`) : rien
ne part sur le réseau depuis la suite.

Il porte aussi la fixture `live` (une base PostgreSQL jetable) et le gréement des
tests de facturation — org, identité, abonnement, ligne de journal encaissée. Ils
sont partagés par deux fichiers de test et n'appartiennent à aucun des deux ; les
laisser dans l'un ferait importer un module `test_*` depuis l'autre, ce que pytest
collecte deux fois.
"""
from __future__ import annotations

import uuid

import pytest

# Taux appliqués par le « fournisseur » selon le code de TVA reçu — miroir de ce que
# Pennylane facturerait. `crossborder` (autoliquidation) et `extracom` (export) sont
# à zéro : c'est ce qui rend le contrôle de montant capable d'attraper un code faux.
_TAUX = {"FR_200": 0.20, "FR_100": 0.10, "FR_55": 0.055, "FR_21": 0.021,
         "crossborder": 0.0, "extracom": 0.0, "exempt": 0.0}


class FauxPennylane:
    """Le client, avec sa mémoire et son journal d'appels."""

    def __init__(self):
        self.customers: dict[int, dict] = {}
        self.documents: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self._seq = 100
        # Leviers de panne, posés par un test : `refus` fait échouer le client
        # comme le VRAI le fait — en levant `UpstreamHTTPError` (oto-core#77 :
        # le transport lève, il ne rend plus un dict d'erreur) ; `ttc_faux`
        # fabrique un brouillon qui ne dit pas ce qui a été débité.
        #
        # Ce levier a menti pendant tout le temps où il rendait un dict : après
        # que le transport a changé, les tests de facturation sont restés VERTS
        # alors que le code de production ne captait plus rien. Un double qui
        # échoue autrement que l'original ne prouve pas ce qu'on croit.
        self.refus: dict | None = None
        self.ttc_faux: float | None = None

    def _lever(self):
        """Échoue comme le vrai client : une exception portant le statut HTTP."""
        from oto.tools.common.errors import UpstreamHTTPError

        refus = dict(self.refus or {})
        statut = refus.get("status_code") or refus.get("error")
        raise UpstreamHTTPError(int(statut) if str(statut).isdigit() else 500,
                                refus.get("details") or refus.get("error") or "refus",
                                service="pennylane")

    def _id(self) -> int:
        self._seq += 1
        return self._seq

    # --- clients -------------------------------------------------------------

    def find_customer_by_external_reference(self, external_reference: str):
        self.calls.append(("find_customer", external_reference))
        for c in self.customers.values():
            if c.get("external_reference") == external_reference:
                return c
        return None

    def create_customer(self, name, emails=None, address=None, postal_code=None,
                        city=None, country_alpha2="FR", external_reference=None):
        self.calls.append(("create_customer", name, country_alpha2, external_reference))
        if self.refus:
            self._lever()
        cid = self._id()
        self.customers[cid] = {
            "id": cid, "name": name, "emails": emails or [],
            "billing_address": {"address": address, "postal_code": postal_code,
                                "city": city, "country_alpha2": country_alpha2},
            "external_reference": external_reference}
        return self.customers[cid]

    def update_customer(self, customer_id: int, **fields):
        self.calls.append(("update_customer", customer_id, dict(fields)))
        self.customers.setdefault(customer_id, {"id": customer_id}).update(fields)
        return self.customers[customer_id]

    # --- factures et avoirs --------------------------------------------------

    def find_invoice_by_external_reference(self, external_reference: str):
        self.calls.append(("find_invoice", external_reference))
        for d in self.documents.values():
            if d.get("external_reference") == external_reference:
                return d
        return None

    def _document(self, *, customer_id, date, deadline, lines, external_reference,
                  pdf_free_text, currency, signe: int):
        if self.refus:
            self._lever()
        ligne = dict(lines[0])
        ht = float(ligne["raw_currency_unit_price"]) * abs(ligne["quantity"]) * signe
        taux = _TAUX[ligne["vat_rate"]]
        did = self._id()
        ttc = self.ttc_faux if self.ttc_faux is not None else round(ht * (1 + taux), 2)
        self.documents[did] = {
            "id": did, "customer_id": customer_id, "date": date, "deadline": deadline,
            "draft": True, "status": "draft", "currency": currency,
            "external_reference": external_reference,
            "pdf_invoice_free_text": pdf_free_text,
            "invoice_lines": [ligne],
            "currency_amount_before_tax": round(ht, 2),
            "currency_amount": ttc,
            "invoice_number": None, "public_file_url": None}
        return self.documents[did]

    def create_customer_invoice(self, customer_id, date, deadline, lines, draft=True,
                                external_reference=None, pdf_free_text=None,
                                customer_invoice_template_id=None, currency="EUR"):
        self.calls.append(("create_invoice", external_reference, lines[0]["vat_rate"],
                           lines[0]["label"], draft))
        return self._document(customer_id=customer_id, date=date, deadline=deadline,
                              lines=lines, external_reference=external_reference,
                              pdf_free_text=pdf_free_text, currency=currency, signe=1)

    def create_credit_note(self, customer_id, date, deadline, lines,
                           external_reference=None, pdf_free_text=None,
                           customer_invoice_template_id=None, draft=True,
                           currency="EUR"):
        # oto-core inverse le signe des quantités AVANT l'appel HTTP : la nature
        # « avoir » est structurelle côté client, on la reproduit ici pour que le
        # test voie ce que Pennylane verrait.
        self.calls.append(("create_credit_note", external_reference,
                           lines[0]["vat_rate"], lines[0]["label"], draft))
        return self._document(customer_id=customer_id, date=date, deadline=deadline,
                              lines=lines, external_reference=external_reference,
                              pdf_free_text=pdf_free_text, currency=currency, signe=-1)

    def finalize_invoice(self, invoice_id: int):
        self.calls.append(("finalize", invoice_id))
        if self.refus:
            self._lever()
        d = self.documents[invoice_id]
        d.update(draft=False, status="upcoming",
                 invoice_number=f"F2026{invoice_id:04d}",
                 public_file_url=f"https://faux.pennylane.test/{invoice_id}.pdf")
        return d

    def link_credit_note(self, invoice_id: int, credit_note_id: int):
        self.calls.append(("link_credit_note", invoice_id, credit_note_id))
        return {"ok": True}


def brancher(monkeypatch, faux: FauxPennylane | None = None) -> FauxPennylane:
    """Branche le faux fournisseur, le téléchargement du PDF et le mailer.

    Rend le faux, pour que le test lise son journal d'appels."""
    import httpx

    from oto_mcp.billing_invoices import mail
    from oto_mcp.billing_invoices import pennylane as seam

    faux = faux or FauxPennylane()
    monkeypatch.setattr(seam, "client", lambda: faux)

    class _Reponse:
        status_code = 200
        content = b"%PDF-1.4 faux document"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Reponse())
    envoyes: list[tuple] = []
    monkeypatch.setattr(mail, "_send",
                        lambda to, subject, html, **k: envoyes.append((to, subject, html))
                        or True)
    faux.emails = envoyes
    return faux

# ── gréement des tests de facturation ────────────────────────────────────────

IDENTITE_FR = dict(legal_name="ACME SAS", country_code="FR",
                   address_line="1 rue de la Paix", postal_code="13001",
                   city="Marseille", billing_email="compta@acme.test")


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base jetable, montée par le DDL réel (`init_db`) — le fragment
    `billing_invoices` compris."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_fact_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


# ── petits gréements ─────────────────────────────────────────────────────────

def _org(nom: str = "ACME") -> int:
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return conn.execute(
            "INSERT INTO orgs (name) VALUES (%s) RETURNING id", (nom,)
        ).fetchone()["id"]


def _identite(org: int, **champs) -> None:
    from oto_mcp.db import billing as db_billing

    db_billing.upsert_billing_identity(org, **{**IDENTITE_FR, **champs})


def _abonnement(org: int, plan: str = "standard") -> None:
    from oto_mcp.db import billing as db_billing

    db_billing.upsert_org_subscription(org, plan=plan, customer_id="cst_1",
                                       mandate_id="mdt_1", status="active",
                                       current_period_end="2026-09-28 10:00:00")


def _paiement(org: int, *, ht: int = 1900, pays: str = "FR",
              tva: str | None = None, kind: str = "initial",
              statut: str = "paid", sans_tva: bool = False) -> dict:
    """Une ligne de journal ENCAISSÉE, décomposition fiscale comprise.

    `sans_tva=True` reproduit les deux encaissements du 25/08 : débités du HT, sans
    `amount_ht` — ce que la règle (c) exclut de la facturation automatique."""
    from oto_mcp import billing_vat
    from oto_mcp.db import billing as db_billing
    from oto_mcp.db import billing_invoices as db_invoices

    if sans_tva:
        row_id = db_billing.insert_billing_payment(
            org, kind, ht, payment_intent_id=f"tr_{uuid.uuid4().hex[:8]}",
            status=statut, tax=None)
    else:
        tax = billing_vat.tax_for(ht, pays, tva)
        row_id = db_billing.insert_billing_payment(
            org, kind, tax["amount_ttc"],
            payment_intent_id=f"tr_{uuid.uuid4().hex[:8]}", status=statut, tax=tax)
    return db_invoices.billing_payment_row(row_id)


