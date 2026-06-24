"""Rapprochement DPE ↔ ventes DVF — orchestration honnête (croise 2 sources).

Pas d'appariement 1:1 fiable en copropriété (DVF = parcelle+prix, DPE = adresse
BAN+étiquette, sans clé de logement commune). Donc :
- **maison** (mono-logement) : meilleur DPE par proximité + surface → champ `dpe`
  avec `dpe_match` ("exact" si surface ±15 %, sinon "proximite").
- **appartement / autre** : pas d'appariement → `dpe_immeuble` = liste des DPE du
  bâtiment (contexte énergétique), jamais « le » DPE de la vente.

Vit ici (pas dans un client france-opendata) car ça joint deux sources : la place
d'un client est mono-source.
"""
from __future__ import annotations

import math
from typing import Any, Optional

_DPE_KEYS = ("etiquette_dpe", "etiquette_ges", "conso_ep_kwh_m2_an",
             "surface_habitable", "date_dpe")


def _dist_m(lat1, lon1, lat2, lon2) -> Optional[float]:
    """Distance approchée (m) entre 2 points proches (équirectangulaire)."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    dlat = (lat2 - lat1) * 111_320
    dlon = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def _slim(d: dict[str, Any]) -> dict[str, Any]:
    return {k: d.get(k) for k in _DPE_KEYS}


def attach_dpe_to_sales(
    mutations: list[dict[str, Any]],
    dpe_rows: list[dict[str, Any]],
    max_dist_m: float = 30,
) -> None:
    """Mute `mutations` en place : attache le DPE matché (maison) ou la liste DPE
    du bâtiment (appartement). Voir docstring module pour la doctrine d'appariement."""
    for m in mutations:
        lat, lon = m.get("latitude"), m.get("longitude")
        if lat is None or lon is None:
            continue
        near = []
        for d in dpe_rows:
            dist = _dist_m(lat, lon, d.get("latitude"), d.get("longitude"))
            if dist is not None and dist <= max_dist_m:
                near.append((dist, d))
        if not near:
            continue
        near.sort(key=lambda x: x[0])

        if m.get("type_local") == "Maison":
            surf = m.get("surface_reelle_bati")
            best = None
            if surf:
                cands = [(abs((d.get("surface_habitable") or 0) - surf), d)
                         for _, d in near if d.get("surface_habitable")]
                if cands:
                    cands.sort(key=lambda x: x[0])
                    diff, best = cands[0]
                    m["dpe_match"] = "exact" if diff <= 0.15 * surf else "proximite"
            if best is None:
                best = near[0][1]
                m["dpe_match"] = "proximite"
            m["dpe"] = _slim(best)
        else:
            m["dpe_immeuble"] = [_slim(d) for _, d in near[:10]]
