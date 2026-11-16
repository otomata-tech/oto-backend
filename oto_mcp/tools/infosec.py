"""Infosec — digital footprint of a domain (**passive** recon / OSINT).

MCP envelope of `oto.tools.infosec.InfosecClient` (oto-core): whois (RDAP), DNS, email
posture (SPF/DMARC/DKIM…), DNS blocklists and the scored deliverability report,
subdomains (Certificate Transparency), TLS and HTTP security headers. Passive only:
RDAP, DNS-over-HTTPS, public CT logs, one TLS handshake, one HTTP GET — no port scan,
no vulnerability probing.

The checks, their bounds (one DNS probe per check, global deadline, a read past it named
in the coverage, never an absence) and the domain normalisation live in the lib. Here,
the host's share only: the User-Agent of the probes (`_ua`, this instance's contact
address, #968), a local refusal (`ValueError`: unknown op, invalid domain or IP) turned
into `INVALID_PARAMS`. An upstream refusal (`UpstreamHTTPError`) is left to the error
envelope.

Open-data connector: no credential, no key. Exposed only if enabled in the DB
(activation gate, ADR 0010) — `register_all` gates on `connector_activation`.

**Consolidated surface (ADR 0047 §Amendment)**: one business object (the domain), its
facets picked by `op`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.infosec import InfosecClient

from .. import config
from ..mcp_errors import McpError


def _ua() -> str:
    """User-Agent of the probes: the contact address is that of THIS instance, never
    ours hard-coded (#968) — resolved at call time, `public_base_url` raises without it."""
    return f"oto-infosec/1.0 (+{config.public_base_url()})"


def _client() -> InfosecClient:
    return InfosecClient(_ua())


def register(mcp: FastMCP) -> None:
    # `op` has NO default: no facet is "the" natural reading of a
    # domain, and a default would answer something other than what was asked.
    @mcp.tool()
    def infosec_domain(
        op: Literal["whois", "dns", "email_security", "subdomains", "tls", "headers",
                    "blocklist", "deliverability"],
        domain: str = "",
        ip: Optional[str] = None,
        dkim_selector: Optional[str] = None,
        limit: int = 100,
        port: int = 443,
    ) -> dict:
        """Check a domain: email deliverability, blocklists, DNS, whois, TLS — `op` picks the check.

        Read-only and passive: DNS-over-HTTPS, RDAP, public CT logs, a TLS handshake,
        one HTTP GET. No port scan, no vulnerability probing, no key needed.

        `op` :
        - **"deliverability"** : full email-deliverability report for YOUR sending
          domain — SPF/DMARC/DKIM/MTA-STS/TLS-RPT/BIMI + blocklists, a `score` /100,
          a `grade` and `recommendations` sorted by priority. Start here to answer
          « do our emails land in spam? ». What could not be read (DNS error, time
          budget) is listed in `coverage` and NOT scored — `coverage.partial` says so. Pass `ip` for a dedicated sending IP and
          `dkim_selector` if the domain signs with a custom selector.
        - **"blocklist"** : is the domain or a sending IPv4 on a spam blocklist (DNSBL)?
          Domain lists + IP lists (SpamCop, PSBL, NordSpam, s5h — lists whose terms
          allow automated commercial lookups), with reverse DNS of each IP. IPs come from `ip`, else from the
          domain's SPF `ip4:` entries. Each list answers `listed` / `clean` / `error` —
          an `error` is NOT a clean result. ⚠️ Spamhaus, SURBL, URIBL, Barracuda,
          Mailspike and UCEPROTECT are returned under `not_checked` (public resolvers
          refused, or terms that forbid automated commercial use), never as clean. A domain
          sending through Google/Microsoft/an emailing tool has no IP of its own to
          check — the response says so in `notes`.
        - **"email_security"** : email authentication posture — SPF (record, `all`
          qualifier, DNS lookup count vs the 10 limit), DMARC (policy, sp, pct, rua,
          alignment), DKIM on common selectors (+ `dkim_selector`), MTA-STS, TLS-RPT,
          BIMI, MX. ⚠️ DKIM selectors cannot be enumerated: an empty
          `dkim_selectors_found` does not prove DKIM is absent.
        - **"whois"** : domain registration via RDAP — registrar, creation/expiration
          dates, statuses, nameservers, registrant org when public.
        - **"dns"** : A/AAAA/MX/NS/TXT records, with stack hints (mail provider, SaaS
          verification tokens).
        - **"subdomains"** : known subdomains from Certificate Transparency logs
          (crt.sh). `count` = all distinct found, `subdomains` = truncated to `limit`.
          crt.sh often fails transiently: the response then carries `error`.
        - **"tls"** : TLS certificate of the host (issuer, validity, SANs, protocol);
          a failed validation is reported (`validated=false` + `error`), not hidden.
        - **"headers"** : HTTP security headers (HSTS, CSP, …) + server fingerprint.

        Args:
            op: deliverability | blocklist | email_security | whois | dns | subdomains | tls | headers.
            domain: a domain, URL or email address (only the domain part is kept).
                Required except for op="blocklist" with `ip`.
            ip: op="blocklist"/"deliverability" — an IPv4 sending address to check.
            dkim_selector: op="email_security"/"deliverability" — DKIM selector to test
                in addition to the common ones.
            limit: op="subdomains" — max distinct subdomains returned (default 100).
            port: op="tls" — TLS port (default 443).
        """
        try:
            return _client().check(op, domain=domain, ip=ip, dkim_selector=dkim_selector,
                                   limit=limit, port=port)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
