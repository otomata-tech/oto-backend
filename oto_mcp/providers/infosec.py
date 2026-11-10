"""Registry declaration of the `infosec` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it doesn't
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# infosec: PASSIVE recon of a domain (RDAP/DNS/CT/TLS/headers, OSINT, no key).
# Complements fr_* (legal identity) with the digital footprint. No intrusive scan.
CONNECTOR = _c(
    "infosec", ["infosec"], secret_kind="none",
    label="Infosec", help="domain check: email deliverability (SPF/DMARC/DKIM, blocklists, score + recommendations), whois/RDAP, DNS, subdomains (CT), TLS, security headers (passive recon)",
)

CATEGORY = "Product & data"
PUBLISHER = "Otomata (OSINT)"
DESCRIPTION = (
    "Domain check, passive read-only: email deliverability "
    "(SPF/DMARC/DKIM, blocklists, score and recommendations), WHOIS/RDAP, DNS, "
    "subdomains via Certificate Transparency, TLS and security headers."
)
SANS_LOGO_DE_MARQUE = True
