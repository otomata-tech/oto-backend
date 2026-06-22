"""Infosec — empreinte numérique d'un domaine (recon **passif** / OSINT).

Complète le volet « identité légale/financière » (`fr_*`) par l'empreinte
technique d'une entreprise quand on part d'un site/domaine : whois (RDAP), DNS,
posture e-mail (SPF/DMARC), sous-domaines (Certificate Transparency), TLS et
en-têtes HTTP de sécurité.

**Passif uniquement** : RDAP, DNS-over-HTTPS, logs CT publics (crt.sh), handshake
TLS, un GET HTTP. PAS de scan de ports ni de probing de vulnérabilités — recon
OSINT d'un prospect, rien d'intrusif. Aucune autorisation de la cible requise car
on ne consulte que des sources publiques / le service exposé lui-même.

Connecteur open-data : pas de credential, pas de clé. Exposé seulement si activé
en DB (cran d'activation, ADR 0010) — `register_all` gate sur `connector_activation`.
"""
from __future__ import annotations

import asyncio
import socket
import ssl
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastmcp import FastMCP

_UA = "oto-infosec/1.0 (+https://oto.ninja)"
_DOH = "https://cloudflare-dns.com/dns-query"


def _norm_domain(value: str) -> str:
    """Réduit une URL/e-mail/hostname à un domaine nu (sans schéma, port, chemin)."""
    v = (value or "").strip().lower()
    if "@" in v:
        v = v.split("@", 1)[1]
    if "://" not in v:
        v = "//" + v
    host = urlsplit(v).hostname or ""
    return host.strip(".")


async def _doh(name: str, rtype: str) -> list[str]:
    """Résout un type d'enregistrement via DNS-over-HTTPS Cloudflare (JSON)."""
    async with httpx.AsyncClient(timeout=15, headers={"accept": "application/dns-json"}) as c:
        r = await c.get(_DOH, params={"name": name, "type": rtype})
        r.raise_for_status()
        data = r.json()
    out = []
    for ans in data.get("Answer", []) or []:
        d = (ans.get("data") or "").strip()
        if rtype == "TXT":
            # concatène les chunks et retire les guillemets d'échappement
            d = d.replace('" "', "").strip('"')
        out.append(d)
    return out


def _vcard_field(vcard: list, field: str) -> Optional[str]:
    """Extrait un champ d'un jCard RDAP (vcardArray[1] = liste de [name,_,_,value])."""
    try:
        for entry in vcard[1]:
            if entry[0] == field:
                val = entry[3]
                return val if isinstance(val, str) else " ".join(map(str, val))
    except (IndexError, TypeError):
        pass
    return None


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def infosec_whois(domain: str) -> dict:
        """Domain registration via RDAP (modern structured whois).

        Returns registrar, key dates (creation/expiration/last-changed), domain
        statuses, nameservers and registrant org when public (often redacted for
        privacy). RDAP replaces legacy whois with structured JSON.

        Args:
            domain: a domain or URL (scheme/path stripped automatically).
        """
        d = _norm_domain(domain)
        if not d:
            return {"error": "domaine invalide"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"user-agent": _UA}) as c:
            r = await c.get(f"https://rdap.org/domain/{d}")
            if r.status_code == 404:
                return {"domain": d, "found": False, "note": "non enregistré ou TLD non couvert par RDAP"}
            r.raise_for_status()
            j = r.json()
        events = {e.get("eventAction"): e.get("eventDate") for e in j.get("events", []) or []}
        registrar = registrant = None
        for ent in j.get("entities", []) or []:
            roles = ent.get("roles", []) or []
            name = _vcard_field(ent.get("vcardArray", []), "fn")
            if "registrar" in roles:
                registrar = name or ent.get("handle")
            if "registrant" in roles:
                registrant = name or ent.get("handle")
        return {
            "domain": j.get("ldhName", d),
            "found": True,
            "registrar": registrar,
            "registrant": registrant,
            "statuses": j.get("status", []),
            "created": events.get("registration"),
            "expires": events.get("expiration"),
            "last_changed": events.get("last changed"),
            "nameservers": [ns.get("ldhName") for ns in j.get("nameservers", []) or []],
        }

    @mcp.tool()
    async def infosec_dns(domain: str) -> dict:
        """DNS records (A/AAAA/MX/NS/TXT) via DNS-over-HTTPS, with stack hints.

        MX and TXT records leak the company's mail provider (Google/Microsoft…)
        and SaaS verification tokens (a fingerprint of the tech stack).

        Args:
            domain: a domain or URL.
        """
        d = _norm_domain(domain)
        if not d:
            return {"error": "domaine invalide"}
        a, aaaa, mx, ns, txt = await asyncio.gather(
            _doh(d, "A"), _doh(d, "AAAA"), _doh(d, "MX"), _doh(d, "NS"), _doh(d, "TXT"),
            return_exceptions=True,
        )
        def ok(x): return x if isinstance(x, list) else []
        mx_list, txt_list = ok(mx), ok(txt)
        hints = []
        joined = " ".join(mx_list + txt_list).lower()
        for needle, label in (("google", "Google Workspace"), ("outlook", "Microsoft 365"),
                              ("protonmail", "Proton"), ("zoho", "Zoho"),
                              ("mailgun", "Mailgun"), ("sendgrid", "SendGrid"),
                              ("amazonses", "Amazon SES"), ("ovh", "OVH"),
                              ("atlassian", "Atlassian"), ("hubspot", "HubSpot")):
            if needle in joined:
                hints.append(label)
        return {
            "domain": d,
            "A": ok(a), "AAAA": ok(aaaa), "MX": mx_list, "NS": ok(ns), "TXT": txt_list,
            "stack_hints": sorted(set(hints)),
        }

    @mcp.tool()
    async def infosec_email_security(domain: str) -> dict:
        """E-mail authentication posture: SPF, DMARC, MTA-STS, common DKIM selectors.

        A weak/absent posture (no DMARC, or p=none) means the domain is easy to
        spoof — a maturity signal on a prospect's IT hygiene.

        Args:
            domain: a domain or URL.
        """
        d = _norm_domain(domain)
        if not d:
            return {"error": "domaine invalide"}
        root_txt, dmarc_txt, mta = await asyncio.gather(
            _doh(d, "TXT"), _doh(f"_dmarc.{d}", "TXT"), _doh(f"_mta-sts.{d}", "TXT"),
            return_exceptions=True,
        )
        def ok(x): return x if isinstance(x, list) else []
        spf = next((t for t in ok(root_txt) if t.lower().startswith("v=spf1")), None)
        dmarc = next((t for t in ok(dmarc_txt) if t.lower().startswith("v=dmarc1")), None)
        dmarc_policy = None
        if dmarc:
            for part in dmarc.split(";"):
                part = part.strip()
                if part.lower().startswith("p="):
                    dmarc_policy = part.split("=", 1)[1].strip().lower()
        # DKIM : on ne peut pas énumérer les sélecteurs, on teste les courants
        selectors = ["default", "google", "selector1", "selector2", "k1", "dkim", "mail", "s1"]
        dkim_found = []
        results = await asyncio.gather(
            *[_doh(f"{s}._domainkey.{d}", "TXT") for s in selectors], return_exceptions=True
        )
        for sel, res in zip(selectors, results):
            if isinstance(res, list) and any("v=dkim1" in t.lower() or "p=" in t.lower() for t in res):
                dkim_found.append(sel)
        score = sum([bool(spf), dmarc_policy in ("quarantine", "reject"),
                     bool(mta if isinstance(mta, list) and mta else None), bool(dkim_found)])
        grade = ["faible", "faible", "moyenne", "bonne", "forte"][score]
        return {
            "domain": d,
            "spf": spf,
            "dmarc": dmarc,
            "dmarc_policy": dmarc_policy,
            "mta_sts": bool(ok(mta)),
            "dkim_selectors_found": dkim_found,
            "posture": grade,
            "note": "DKIM testé sur sélecteurs courants seulement (énumération impossible)",
        }

    @mcp.tool()
    async def infosec_subdomains(domain: str, limit: int = 100) -> dict:
        """Known subdomains via Certificate Transparency logs (crt.sh, passive).

        Reads issued-certificate names from public CT logs — no bruteforce, no
        active probing. Reveals the org's surface (api., vpn., staging., mail.…).

        Args:
            domain: apex domain (e.g. "example.com").
            limit: max distinct subdomains returned (default 100).
        """
        d = _norm_domain(domain)
        if not d:
            return {"error": "domaine invalide"}
        rows = None
        last_err = "inconnu"
        async with httpx.AsyncClient(timeout=40, headers={"user-agent": _UA}) as c:
            for attempt in range(3):  # crt.sh renvoie souvent des 5xx transitoires
                try:
                    r = await c.get("https://crt.sh/", params={"q": f"%.{d}", "output": "json"})
                    if r.status_code >= 500:
                        last_err = f"HTTP {r.status_code}"
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    rows = r.json()
                    break
                except Exception as e:
                    last_err = type(e).__name__
                    await asyncio.sleep(1.5 * (attempt + 1))
        if rows is None:
            return {"domain": d, "error": f"crt.sh indisponible ({last_err})", "subdomains": []}
        names: set[str] = set()
        for row in rows:
            for n in (row.get("name_value") or "").splitlines():
                n = n.strip().lower().lstrip("*.")
                if n.endswith(d) and n != d:
                    names.add(n)
        ordered = sorted(names)
        return {"domain": d, "count": len(ordered), "subdomains": ordered[:limit]}

    @mcp.tool()
    async def infosec_tls(domain: str, port: int = 443) -> dict:
        """TLS certificate of the host: issuer, validity, SANs, protocol version.

        SANs frequently reveal sibling domains/subdomains. A failed validation
        (expired/self-signed/hostname mismatch) is reported, not hidden.

        Args:
            domain: a domain or URL.
            port: TLS port (default 443).
        """
        host = _norm_domain(domain)
        if not host:
            return {"error": "domaine invalide"}

        def probe() -> dict:
            ctx = ssl.create_default_context()
            try:
                with socket.create_connection((host, port), timeout=15) as sock:
                    with ctx.wrap_socket(sock, server_hostname=host) as ss:
                        cert = ss.getpeercert() or {}
                        version, cipher = ss.version(), ss.cipher()
                validated, err = True, None
            except ssl.SSLCertVerificationError as e:
                # Outil d'INSPECTION : on reconnecte sans vérification UNIQUEMENT
                # pour lire le protocole/cipher d'un hôte au certif invalide
                # (expiré/auto-signé/mismatch) — diagnostic, aucune donnée échangée,
                # `validated=False` est remonté tel quel. Pas un canal de confiance.
                cert, validated, err = {}, False, str(e)
                uctx = ssl._create_unverified_context()
                with socket.create_connection((host, port), timeout=15) as sock:
                    with uctx.wrap_socket(sock, server_hostname=host) as ss:
                        version, cipher = ss.version(), ss.cipher()
            subject = {k: v for t in cert.get("subject", ()) for k, v in t}
            issuer = {k: v for t in cert.get("issuer", ()) for k, v in t}
            sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
            return {
                "host": host, "port": port, "validated": validated, "error": err,
                "protocol": version,
                "cipher": cipher[0] if cipher else None,
                "issuer": issuer.get("organizationName") or issuer.get("commonName"),
                "subject_cn": subject.get("commonName"),
                "valid_from": cert.get("notBefore"),
                "valid_until": cert.get("notAfter"),
                "subject_alt_names": sans,
            }

        try:
            return await asyncio.to_thread(probe)
        except Exception as e:
            return {"host": host, "port": port, "error": f"{type(e).__name__}: {e}"}

    @mcp.tool()
    async def infosec_headers(domain: str) -> dict:
        """HTTP security headers + server/tech fingerprint from a single GET.

        Checks HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
        and Permissions-Policy presence, plus Server/X-Powered-By disclosure.

        Args:
            domain: a domain or URL.
        """
        d = _norm_domain(domain)
        if not d:
            return {"error": "domaine invalide"}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                         headers={"user-agent": _UA}) as c:
                r = await c.get(f"https://{d}")
        except Exception as e:
            return {"domain": d, "error": f"{type(e).__name__}: {e}"}
        h = {k.lower(): v for k, v in r.headers.items()}
        checks = {
            "hsts": "strict-transport-security" in h,
            "csp": "content-security-policy" in h,
            "x_frame_options": "x-frame-options" in h,
            "x_content_type_options": "x-content-type-options" in h,
            "referrer_policy": "referrer-policy" in h,
            "permissions_policy": "permissions-policy" in h,
        }
        present = sum(checks.values())
        return {
            "domain": d,
            "final_url": str(r.url),
            "status": r.status_code,
            "security_headers": checks,
            "security_headers_score": f"{present}/{len(checks)}",
            "server": h.get("server"),
            "x_powered_by": h.get("x-powered-by"),
        }
