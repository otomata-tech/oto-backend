"""SIRENE address-resolution — query StockEtablissement + StockUniteLegale via DuckDB.

Porté depuis `services/sirene_local.ts` de Générations Renouvelables. Sert
à résoudre "(commune, adresse, hint NAF)" → liste de candidats SIRET scorés.

Use case principal : cascade d'enrichissement où l'adresse Enedis/Sitadel
sert de pivot pour identifier l'entreprise propriétaire d'un site. L'API
recherche-entreprises ne couvre pas ce besoin (pas de filtre adresse-token).

Heuristique de matching :
  1. Filtre code_commune (INSEE), expand Paris/Lyon/Marseille arrondissements
  2. Filtre etat='A' (actif) + statut_diffusion='O' (open data)
  3. Match adresse via tokens (libelle voie) OU enseigne 1/2/3 OU denom usuelle
     (capture les chaînes commerciales : Carrefour vs raison sociale légale)
  4. Score : NAF2 cohérent (+0.5), is_siege (+0.1), exact numero match (+0.3),
     voie match (+0.3) OU enseigne match (+0.2), section C (+0.1)
  5. Exclut les sections K (finance), L (immo), O (admin pub), S (autres services)
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .sirene_duckdb import _connect, parquet_path, ul_parquet_path


# Sections NAF à exclure des candidats (finance/immo/admin pub/autres).
_EXCLUDED_SECTIONS = frozenset({"K", "L", "O", "S"})

# Type voies INSEE acceptés en tokenization.
_TYPE_VOIES = frozenset({
    "RUE", "AV", "AVENUE", "BD", "BOULEVARD", "ROUTE", "RTE", "ALLEE", "ALL",
    "IMPASSE", "IMP", "PLACE", "PL", "CHEMIN", "CH", "VOIE", "QUAI", "PASSAGE",
    "ZONE", "ZAC", "ZI", "ZA", "PARC", "RESIDENCE", "RES", "SQUARE", "SQ",
    "COURS", "FAUBOURG", "FBG", "ESPLANADE", "ROND-POINT", "RD-PT", "PROMENADE",
    "PROM", "SENTIER", "SENT", "VENELLE", "TRAVERSE", "TRA", "MONTEE",
})

_COMMUNE_RE = re.compile(r"^([0-9]{5}|2[AB][0-9]{3})$")

# Paris/Lyon/Marseille : code global ↔ arrondissements.
_ARR_EXPANSION = {
    "75056": [f"75{101 + i}" for i in range(20)],
    "69123": [f"6938{i + 1}" for i in range(9)],
    "13055": [f"132{i + 1:02d}" for i in range(16)],
}
_ARR_TO_GLOBAL = {arr: g for g, arrs in _ARR_EXPANSION.items() for arr in arrs}


def expand_code_commune(cc: str) -> list[str]:
    """Pour un code commune `cc`, retourne toutes les variantes équivalentes.

    - code global P/L/M → [global, ...arrondissements]
    - arrondissement P/L/M → [arr, global, ...autres arrondissements]
    - autre code → [cc]

    Garantit toujours `cc in result`.
    """
    if cc in _ARR_EXPANSION:
        return [cc, *_ARR_EXPANSION[cc]]
    g = _ARR_TO_GLOBAL.get(cc)
    if g:
        return [cc, g, *[x for x in _ARR_EXPANSION[g] if x != cc]]
    return [cc]


def naf_section(naf: Optional[str]) -> Optional[str]:
    """Section NAF (A-S) depuis les 2 premiers chiffres du code APE."""
    if not naf or len(naf) < 2:
        return None
    try:
        c = int(naf[:2])
    except ValueError:
        return None
    if c <= 3: return "A"
    if c <= 9: return "B"
    if c <= 33: return "C"
    if c == 35: return "D"
    if c <= 39: return "E"
    if c <= 43: return "F"
    if c <= 47: return "G"
    if c <= 53: return "H"
    if c <= 56: return "I"
    if c <= 63: return "J"
    if c <= 66: return "K"
    if c == 68: return "L"
    if c <= 75: return "M"
    if c <= 82: return "N"
    if c == 84: return "O"
    if c == 85: return "P"
    if c <= 88: return "Q"
    if c <= 93: return "R"
    if c <= 96: return "S"
    return None


def tokenize_adresse(adresse: str) -> tuple[Optional[str], Optional[str], str]:
    """Découpe "12 RUE DE LA PAIX" en (numero='12', type_voie='RUE', libelle='DE LA PAIX').

    Le numero peut comporter une lettre suffixe (12B). Le type voie est optionnel.
    """
    tokens = adresse.strip().upper().split()
    if not tokens:
        return (None, None, "")
    numero: Optional[str] = None
    type_voie: Optional[str] = None
    i = 0
    if re.match(r"^\d+[A-Z]?$", tokens[0]):
        numero = tokens[0]
        i += 1
    if i < len(tokens) and tokens[i] in _TYPE_VOIES:
        type_voie = tokens[i]
        i += 1
    return (numero, type_voie, " ".join(tokens[i:]))


def lookup_by_address(
    adresse: str,
    code_commune: str | list[str],
    naf2_hint: Optional[str] = None,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Cherche les candidats SIRENE pour une (adresse, commune) donnée, scorés.

    Args:
        adresse: ex. "12 RUE DE LA PAIX"
        code_commune: code INSEE simple ou liste (pour P/L/M, l'expansion est
                      auto si on passe une string seule).
        naf2_hint: 2 premiers chiffres du NAF attendu, ex. "10" pour agro.
        top_n: nombre de candidats à retourner (1..10).

    Returns:
        Liste de dicts {siret, siren, raison_sociale, enseigne, naf_code,
        categorie_entreprise, tranche_effectifs, numero_voie, type_voie,
        libelle_voie, code_postal, libelle_commune, code_commune, is_siege,
        confidence (0..1), reason}, triés par confidence desc.
    """
    top_n = max(1, min(int(top_n), 10))
    numero, _type_voie, libelle = tokenize_adresse(adresse)
    if not libelle and not numero:
        return []

    raw_codes = code_commune if isinstance(code_commune, list) else expand_code_commune(code_commune)
    codes = [c for c in raw_codes if _COMMUNE_RE.match(c)]
    if not codes:
        return []

    libelle_like = f"%{libelle.lower()}%"
    cc_placeholders = ",".join("?" for _ in codes)

    sql = f"""
    SELECT
      e.siret, e.siren,
      COALESCE(e."etablissementSiege", FALSE)                AS is_siege,
      e."numeroVoieEtablissement"                            AS numero_voie,
      e."typeVoieEtablissement"                              AS type_voie,
      e."libelleVoieEtablissement"                           AS libelle_voie,
      e."complementAdresseEtablissement"                     AS complement_adresse,
      e."codePostalEtablissement"                            AS code_postal,
      e."libelleCommuneEtablissement"                        AS libelle_commune,
      e."codeCommuneEtablissement"                           AS code_commune,
      e."enseigne1Etablissement"                             AS enseigne1,
      e."enseigne2Etablissement"                             AS enseigne2,
      e."enseigne3Etablissement"                             AS enseigne3,
      e."denominationUsuelleEtablissement"                   AS denom_usuelle_etab,
      e."activitePrincipaleEtablissement"                    AS activite_principale,
      ul."denominationUniteLegale"                           AS raison_sociale,
      ul."categorieEntreprise"                               AS categorie_entreprise,
      ul."trancheEffectifsUniteLegale"                       AS tranche_effectifs
    FROM read_parquet('{parquet_path()}') e
    LEFT JOIN read_parquet('{ul_parquet_path()}') ul ON ul.siren = e.siren
    WHERE e."codeCommuneEtablissement" IN ({cc_placeholders})
      AND e."etatAdministratifEtablissement" = 'A'
      AND e."statutDiffusionEtablissement" = 'O'
      AND (
        LOWER(e."libelleVoieEtablissement") LIKE ?
        OR LOWER(e."enseigne1Etablissement") LIKE ?
        OR LOWER(e."enseigne2Etablissement") LIKE ?
        OR LOWER(e."enseigne3Etablissement") LIKE ?
        OR LOWER(e."denominationUsuelleEtablissement") LIKE ?
      )
    LIMIT 200
    """
    params = [*codes, libelle_like, libelle_like, libelle_like, libelle_like, libelle_like]

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    cols = [
        "siret", "siren", "is_siege", "numero_voie", "type_voie", "libelle_voie",
        "complement_adresse", "code_postal", "libelle_commune", "code_commune",
        "enseigne1", "enseigne2", "enseigne3", "denom_usuelle_etab",
        "activite_principale", "raison_sociale", "categorie_entreprise",
        "tranche_effectifs",
    ]

    want = libelle.lower()
    scored: list[dict[str, Any]] = []
    for row in rows:
        r = dict(zip(cols, row))
        sec = naf_section(r["activite_principale"])
        if sec and sec in _EXCLUDED_SECTIONS:
            continue

        score = 0.0
        reasons = ["commune match"]
        enseigne_match: Optional[str] = None
        lib = (r["libelle_voie"] or "").lower()

        if want and want in lib:
            score += 0.3
            reasons.append("voie match")
        else:
            for cand in (r["enseigne1"], r["enseigne2"], r["enseigne3"], r["denom_usuelle_etab"]):
                if cand and want in cand.lower():
                    enseigne_match = cand
                    score += 0.2
                    reasons.append(f'enseigne="{cand}"')
                    break

        if naf2_hint and r["activite_principale"]:
            naf2 = r["activite_principale"][:2]
            if naf2 == naf2_hint:
                score += 0.5
                reasons.append(f"naf2={naf2}")
            else:
                score += 0.1
                reasons.append(f"naf2 mismatch {naf2}≠{naf2_hint}")
        else:
            score += 0.2

        if numero and r["numero_voie"] == numero:
            score += 0.3
            reasons.append(f"num={numero}")

        if r["is_siege"]:
            score += 0.1
            reasons.append("siege")

        if sec == "C":
            score += 0.1
            reasons.append("section C")

        scored.append({
            "siret": r["siret"],
            "siren": r["siren"],
            "raison_sociale": r["raison_sociale"],
            "enseigne": enseigne_match,
            "naf_code": r["activite_principale"],
            "categorie_entreprise": r["categorie_entreprise"],
            "tranche_effectifs": r["tranche_effectifs"],
            "numero_voie": r["numero_voie"],
            "type_voie": r["type_voie"],
            "libelle_voie": r["libelle_voie"],
            "code_postal": r["code_postal"],
            "libelle_commune": r["libelle_commune"],
            "code_commune": r["code_commune"],
            "is_siege": bool(r["is_siege"]),
            "confidence": min(score, 1.0),
            "reason": " + ".join(reasons),
        })

    scored.sort(key=lambda x: x["confidence"], reverse=True)
    return scored[:top_n]


def stats() -> dict[str, Any]:
    """Healthcheck : count des établissements/UL + chemins parquet."""
    sql = f"""
    SELECT
      (SELECT COUNT(*) FROM read_parquet('{ul_parquet_path()}'))                                AS unites,
      (SELECT COUNT(*) FROM read_parquet('{parquet_path()}'))                                    AS etabs,
      (SELECT COUNT(*) FROM read_parquet('{parquet_path()}') WHERE "etatAdministratifEtablissement" = 'A') AS etabs_actifs
    """
    with _connect() as conn:
        row = conn.execute(sql).fetchone()
    return {
        "unites": int(row[0]) if row else 0,
        "etabs": int(row[1]) if row else 0,
        "etabs_actifs": int(row[2]) if row else 0,
        "etab_parquet": parquet_path(),
        "ul_parquet": ul_parquet_path(),
    }
