"""Données entreprise Grèce — identité via registres publics (open data, sans clé).

- **GEMI** (Γ.Ε.ΜΗ., registre général du commerce) : recherche universelle via
  l'autocomplete du portail de publicité — gratuit, sans clé, sans reCAPTCHA →
  liste d'entités (raison sociale, n° GEMI, n° TVA/ΑΦΜ, statut actif/inactif).
- **VIES** (UE) : enrichissement pour un résultat unique → validité du n° de TVA
  intracommunautaire + adresse.

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
Profil approfondi (dirigeants, capital, codes ΚΑΔ) : nécessiterait l'API GEMI
officielle (clé gratuite) — hors périmètre ici.

Porté de 321agents (`gr_lookup`) : la journalisation d'usage est assurée par le
CallMonitoringMiddleware d'oto, on ne la recâble pas ici.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

_AUTOCOMPLETE = "https://publicity.businessportal.gr/api/autocomplete/{term}"
_VIES = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/EL/vat/{n}"
# Le WAF du registre GEMI rejette les User-Agents non-navigateur (httpx → 429),
# on présente donc un UA navigateur. Obligatoire, pas optionnel.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


async def _autocomplete(term: str) -> list[dict]:
    url = _AUTOCOMPLETE.format(term=quote(term, safe=""))
    async with httpx.AsyncClient(timeout=20) as c:
        for attempt in range(3):
            r = await c.get(url, headers=_HEADERS)
            if r.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))  # throttling registre — backoff bref
                continue
            break
    if r.status_code == 429:
        raise RuntimeError("Registre GEMI temporairement indisponible — réessayer.")
    r.raise_for_status()
    return ((r.json() or {}).get("payload") or {}).get("autocomplete") or []


async def _vies(vat9: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_VIES.format(n=vat9), headers=_HEADERS)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None
    return {
        "valid": bool(d.get("isValid")),
        "address": (d.get("address") or "").replace("\n", " ").strip() or None,
    }


def _norm(m: dict) -> dict:
    afm = (m.get("afm") or "").strip() or None
    return {
        "name": m.get("co_name") or m.get("title"),
        "also_known_as": m.get("title"),
        "gemi": str(m["arGemi"]) if m.get("arGemi") else None,
        "vat": f"EL{afm}" if afm else None,
        "afm": afm,
        "active": m.get("companyStatusId") == 3,
        "status": m.get("companyStatus"),
        "type": m.get("type"),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def gr_lookup(query: str) -> dict:
        """Look up a Greek entity in open data (GEMI registry + VIES).

        `query` accepts a **company name**, a **GEMI no.** or a Greek **VAT no.**
        (ΑΦΜ, with or without the `EL` prefix). Returns the matching companies
        (name, GEMI no., VAT, active/inactive status). For a single result, adds
        the **address** and the VAT validity via VIES.
        """
        q = (query or "").strip()
        if not q:
            raise ValueError("empty `query`.")
        # ΑΦΜ : retirer un préfixe EL optionnel pour la recherche.
        term = q[2:].strip() if q[:2].upper() == "EL" and q[2:].strip().isdigit() else q

        matches = await _autocomplete(term)
        if not matches:
            return {"query": q, "count": 0, "results": [], "note": "No company found."}

        results = [_norm(m) for m in matches[:10]]
        # Enrichissement VIES si un seul résultat porteur d'un n° de TVA.
        if len(results) == 1 and results[0]["afm"]:
            vies = await _vies(results[0]["afm"])
            if vies:
                results[0]["address"] = vies["address"]
                results[0]["vat_valid"] = vies["valid"]
        return {
            "source": "GEMI registry (autocomplete) + VIES",
            "query": q,
            "count": len(matches),
            "results": results,
        }
