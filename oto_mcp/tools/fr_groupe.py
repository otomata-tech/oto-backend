"""French ownership chain — go up to the group, down to the subsidiaries.

Signal #337. The need is to qualify a company's INDEPENDENCE, and it comes from a
costly false negative: in one campaign, 4 leads out of 5 were discarded because
INSEE classed them as "large company" — they were SUBSIDIARIES, small in their own right.
`categorie_entreprise` is computed by INSEE at the GROUP level, not at the level of the
entity; without the chain, it makes any size-based targeting lie.

## What the upstream can actually return (verified on 2026-08-28, not inferred)

- **Child → parent**: the RNE publishes the LEGAL-ENTITY officers with their
  SIREN. It is the same `dirigeants` block that `fr_get` already returns — the edge
  existed, it just wasn't assembled.
- **Parent → children**: the recherche-entreprises full-text index **indexes the
  officers**. Its OpenAPI says so ("q: terms for a text search
  (name and/or address, officers, elected members)") and the differential proves it:
  `q=LEFEBVRE SARRUT` returns FLS IMMOBILIER, EDITIONS LEGISTATIVES and SOCIETE CIVILE
  ARVIL, none of whose names shares a token with the query. It is the inverted index the
  signal asked us to build server-side: it already existed upstream.
- **What the upstream CANNOT do**: search by an officer's SIREN.
  `q=602060147` returns only Hachette Livre itself, and `nom_personne` targets only
  NATURAL persons (0 results on "LEFEBVRE SARRUT", verified). The index therefore
  returns CANDIDATES by name; we are the ones who prove the link, via the officer's
  SIREN. On `q=HACHETTE LIVRE`, 5 candidates out of 29 have no link to it
  (L'ESPRIT LIVRE, MATRA HACHETTE…): without this check, they would pass for
  subsidiaries.
- **Hard upstream limits**: `per_page` ≤ 25 (26 → HTTP 400) and `page × per_page`
  ≤ 10,000 (401 → HTTP 400). On a large group (BOUYGUES = 1,476 candidates),
  exhaustive downward traversal is therefore out of reach — and we SAY so.

## What this module refuses to guess

The beneficial-owners register has been closed to the public since 2026-07-31:
the real shareholding of a SAS is no longer accessible. A SAS with no legal-entity
officer is therefore NOT "independent", it is **undetermined** — and saying so is the
core of the tool, not its flaw. Observed on EDITIONS PAYOT ET RIVAGES: no
legal-entity officer other than the statutory auditor, whereas Actes Sud owns it
100%.

No writes, no credits: three open-data sources, read-only.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from .lecture import LECTURE
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

# --- Taxonomy of officer capacities ------------------------------------------
# Survey of 2026-08-28 on Hachette Livre, Lefebvre Sarrut, Frojal, Calmann-Lévy,
# Payot & Rivages. The classification is a CONTRACT returned to the caller (the client
# requires enforceable sources), not a convenience heuristic.

# An auditor is neither a shareholder nor an officer: without this exclusion, KPMG,
# Deloitte, RSM and Salustro Reydel become the group head of half their
# clients and contaminate the whole chain (the signal cites the CERALP / Bamboo case).
# ⚠️ TWO mentions, not one: the "contrôleur des comptes" is the equivalent of the
# commissaire among associations and mutual societies. Found by counting the REAL
# capacities in the register (1,101 legal-entity officers across 18 groups, 2026-08-28) —
# 7 occurrences that, with only the "commissaire" mention, passed for subsidiaries.
# Counting is the method: the list written from memory had missed one.
_CONTROLE_DES_COMPTES = ("commissaire aux comptes", "contrôleur des comptes",
                         "controleur des comptes")

# `forte` — the capacity IMPLIES ownership: these mentions are only held by a
# partner holding capital (SNC, SCS, civil partnerships).
_FORTE = ("associé commandité", "associée commanditée", "associé indéfiniment",
          "associée indéfiniment", "associé unique", "associée unique")
# `moyenne` — corporate office: governance is PROVEN, control only suggested
# (a SAS can be chaired by a company that does not hold a single share of it).
_MOYENNE = ("président", "présidente", "administrateur", "administratrice",
            "gérant", "gérante", "directeur général", "directrice générale",
            "directoire", "conseil de surveillance", "représentant permanent")
# `faible` — neither ownership nor control. Being a MEMBER of a professional GIE (Hachette
# Livre is one of the GIE PROLIVRE, the CENTRALE DE L'ÉDITION, the CELF) or LIQUIDATOR
# of a shell company is not a group membership.
_FAIBLE = ("membre", "liquidateur", "liquidatrice", "autre")

# Only these two bands are TRAVERSED: following a weak link would climb from a
# member to its GIE, then from the GIE to all its other members — a false group.
_TRAVERSABLES = ("forte", "moyenne")
_RANG = {"forte": 3, "moyenne": 2, "faible": 1, "inconnue": 0}

_CAVEAT = (
    "The beneficial-owners register has been closed to the public since 31/07/2024: "
    "the shareholding of a SAS is no longer published. The absence of a legal-entity officer "
    "therefore does NOT prove independence — it is UNDETERMINED. Never read "
    "`confiance=\"indeterminee\"` as \"independent company\"."
)
_METHODE = (
    "legal-entity officers from the RNE (recherche-entreprises); statutory audit "
    "excluded (commissaire aux comptes, contrôleur des comptes); "
    "`faible`/`inconnue` links returned but not traversed"
)

# Fan-out guardrails: a breadth-first walk over a conglomerate blows up fast, and
# every node is an upstream call. High limits, never silent (`tronque`).
_MAX_DEPTH_DUR = 6
_MAX_NOEUDS = 60
_MAX_PAGES_DUR = 20
_PER_PAGE = 25  # upstream maximum, verified: 26 → HTTP 400


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def confiance_du_lien(qualite: Optional[str]) -> str:
    """Confidence band of a legal-entity officer's capacity.

    The order of the tests IS the rule: "Gérant et associé indéfiniment et solidairement
    responsable" carries both vocabularies, and the OWNERSHIP mention wins. An unlisted
    capacity comes out `inconnue` — never promoted by default, because an invented link
    propagates to the whole chain.
    """
    q = (qualite or "").casefold()
    if any(m in q for m in _FORTE):
        return "forte"
    if any(m in q for m in _MOYENNE):
        return "moyenne"
    if any(m in q for m in _FAIBLE):
        return "faible"
    return "inconnue"


def _mandataires(fiche: dict) -> tuple[list[dict], int, list[dict]]:
    """(usable officers, statutory auditors set aside, no-SIREN officers set aside).

    A legal-entity officer WITHOUT a SIREN — typically a foreign company, outside the
    RNE — cannot be followed, but it is not nothing: the source gives its
    name and capacity, and it is often the only hint of a foreign group.
    Reducing it to a counter made readers conclude "no group" (oto#209). It is therefore
    returned as the source carries it, with nothing inferred: no country, no legal form
    (the source has none), and no "foreign" read into a name — the agent decides.
    """
    gardes, cac, sans_siren = [], 0, []
    for d in fiche.get("dirigeants") or []:
        if d.get("type_dirigeant") != "personne morale":
            continue
        qual = (d.get("qualite") or "").casefold()
        if any(m in qual for m in _CONTROLE_DES_COMPTES):
            cac += 1
            continue
        if not d.get("siren"):
            sans_siren.append({"denomination": d.get("denomination"),
                               "qualite": d.get("qualite")})
            continue
        gardes.append(d)
    return gardes, cac, sans_siren


def _motifs_indetermination(racine: str, fiche: dict, liens: list[dict]) -> list[str]:
    """Why no parent was reached, read mechanically from the root.

    `indeterminee` covers situations the agent must be able to tell apart without
    re-reading the register: "no legal-entity officer" is not "legal-entity
    officers exist but without a SIREN" (oto#209). Several reasons can coexist;
    `aucun_mandataire_personne_morale` stands alone by construction.
    """
    pms, cac, sans_siren = _mandataires(fiche)
    motifs = []
    if cac:
        motifs.append("controle_des_comptes")
    if sans_siren:
        motifs.append("sans_siren")
    if any(l["de"] == racine and not l["traverse"] for l in liens):
        motifs.append("liens_non_traverses")
    if any(l["de"] == racine and l["traverse"] and l["vers"] == racine for l in liens):
        motifs.append("lien_vers_elle_meme")
    if not pms and not cac and not sans_siren:
        motifs.append("aucun_mandataire_personne_morale")
    return motifs


def register(mcp: FastMCP) -> None:
    from ..fod import fr as fod_fr  # same FOD proxy as `fr_get` (ADR 0028)

    entreprises = fod_fr.entreprises

    def _fiche(siren: str) -> Optional[dict]:
        """Upstream identity record, or None if the directory does not recognise it.

        ⚠️ The upstream client has a fallback that returns the FIRST result when the exact
        SIREN is absent from the page — a namesake would pass for the requested company
        and the whole chain would go down the wrong branch. So we re-check the SIREN
        here, on the caller's side, rather than hope for an upstream fix.
        """
        fiche = entreprises.get_by_siren(siren)
        if not isinstance(fiche, dict) or fiche.get("siren") != siren:
            return None
        return fiche

    def _fiche_ou_refus(siren: str) -> dict:
        fiche = _fiche(siren)
        if fiche is None:
            raise _bad(f"SIREN {siren} unknown to the company directory.")
        if not fiche.get("nom_complet"):
            # Empty shell returned by the upstream (seen on 999999999): reserved SIREN,
            # struck off without a name, or non-disclosable unit. Guessing a name here
            # would send the downward search to an unrelated company.
            raise _bad(
                f"SIREN {siren}: the directory returns no name for it "
                "(unknown SIREN, or non-disclosable unit).")
        return fiche

    def _ascendant(racine: str, fiche: dict, max_depth: int) -> dict:
        from concurrent.futures import ThreadPoolExecutor

        liens: list[dict] = []
        tetes: list[dict] = []
        fiches = {racine: fiche}
        vus = {racine: 0}
        parent: dict[str, tuple[str, str]] = {}
        non_resolus: list[str] = []
        non_classees: set[str] = set()
        cac = 0
        sans_siren: list[dict] = []
        cycle = tronque = False
        frontiere = [racine]
        profondeur = 0

        while frontiere and profondeur < max_depth:
            profondeur += 1
            suivants: list[str] = []
            for src in frontiere:
                pms, n_cac, sans = _mandataires(fiches[src])
                cac += n_cac
                sans_siren.extend({"de": src, **d} for d in sans)
                traversables = 0
                for d in pms:
                    conf = confiance_du_lien(d.get("qualite"))
                    if conf == "inconnue":
                        non_classees.add(d.get("qualite") or "")
                    traverse = conf in _TRAVERSABLES
                    cible = d["siren"]
                    liens.append({
                        "de": src, "vers": cible,
                        "denomination_vers": d.get("denomination"),
                        "qualite": d.get("qualite"), "confiance": conf,
                        "traverse": traverse, "profondeur": profondeur,
                    })
                    if not traverse:
                        continue
                    traversables += 1
                    if cible in vus:
                        cycle = True          # the loop is returned, not followed
                    elif len(vus) >= _MAX_NOEUDS:
                        tronque = True
                    else:
                        vus[cible] = profondeur
                        parent[cible] = (src, conf)
                        suivants.append(cible)
                # A head = an EXPLORED node, other than the root, with no traversable link.
                # The root is never one: with no parent, it is undetermined.
                if traversables == 0 and src != racine:
                    tetes.append(src)
            # A level is read IN PARALLEL: each node is an upstream round trip, and
            # a holding company readily has three per level. In series, the budget of
            # 60 nodes would mean a minute of waiting — the tool would be correct, and
            # unusable. Same remedy as `france_opendata.compact.profil_entreprise`.
            with ThreadPoolExecutor(max_workers=8) as pool:
                lues = list(zip(suivants, pool.map(_fiche, suivants)))
            for cible, f in lues:
                if f is None:
                    non_resolus.append(cible)   # e.g. foreign company, outside the RNE
                else:
                    fiches[cible] = f
            frontiere = [c for c in suivants if c in fiches]
            if suivants and not frontiere:
                break
        if frontiere:
            tronque = True   # nodes were still left to explore at the budget limit

        def _chemin(node: str) -> tuple[list[str], str]:
            chemin, conf = [node], "forte"
            while node in parent:
                node, c = parent[node]
                chemin.append(node)
                conf = c if _RANG[c] < _RANG[conf] else conf
            return list(reversed(chemin)), conf

        rendus = []
        for t in tetes:
            chemin, conf = _chemin(t)
            rendus.append({
                "siren": t,
                "denomination": (fiches.get(t) or {}).get("nom_complet"),
                "profondeur": vus[t], "confiance": conf, "chemin": chemin,
            })
        # The GLOBAL confidence is computed over all reached nodes, not over the
        # heads alone: a walk stopped by `max_depth` (or closed by a cycle)
        # has no head, and returning `indeterminee` in that case would make readers see "no
        # shareholder published" where we found three. Observed live on
        # Calmann-Lévy at max_depth=1 (3 parents, 0 heads).
        atteints = [n for n in vus if n != racine]
        globale = (max((_chemin(n)[1] for n in atteints), key=lambda c: _RANG[c])
                   if atteints else "indeterminee")
        out = {
            "op": "ascendant", "siren": racine,
            "denomination": fiche.get("nom_complet"),
            "categorie_entreprise": fiche.get("categorie_entreprise"),
            "tetes": rendus, "liens": liens, "confiance": globale,
            "cycle": cycle, "tronque": tronque,
            "exclus": {"controle_des_comptes": cac, "sans_siren": sans_siren},
            "appels_amont": len(fiches) + len(non_resolus),
            "methode": _METHODE, "caveat": _CAVEAT,
        }
        if globale == "indeterminee":
            out["motifs_indetermination"] = _motifs_indetermination(racine, fiche, liens)
        if non_resolus:
            out["parents_hors_repertoire"] = non_resolus
        if non_classees:
            out["qualites_non_classees"] = sorted(non_classees)
        return out

    def _descendant(racine: str, fiche: dict, max_pages: int) -> dict:
        denomination = fiche["nom_complet"]
        requete = fiche.get("nom_raison_sociale") or denomination
        filiales: list[dict] = []
        examines = 0
        total_amont = None
        for page in range(1, max_pages + 1):
            res = entreprises.search(query=requete, page=page, per_page=_PER_PAGE)
            lot = res.get("results") or []
            total_amont = res.get("total_results")
            examines += len(lot)
            for cand in lot:
                if cand.get("siren") == racine:
                    continue
                pms, _cac, _sans = _mandataires(cand)
                # The link is PROVEN only by the officer's SIREN; the candidate's
                # name proves nothing (the index is full-text and fuzzy).
                liens = [d for d in pms if d.get("siren") == racine]
                if not liens:
                    continue
                meilleur = max(liens,
                               key=lambda d: _RANG[confiance_du_lien(d.get("qualite"))])
                filiales.append({
                    "siren": cand.get("siren"),
                    "denomination": cand.get("nom_complet"),
                    "qualite": meilleur.get("qualite"),
                    "confiance": confiance_du_lien(meilleur.get("qualite")),
                    "categorie_entreprise": cand.get("categorie_entreprise"),
                    "etat_administratif": cand.get("etat_administratif"),
                })
            if len(lot) < _PER_PAGE:
                break
        filiales.sort(key=lambda f: (-_RANG[f["confiance"]], f["siren"]))
        tronques = bool(total_amont is not None and examines < total_amont)
        return {
            "op": "descendant", "siren": racine, "denomination": denomination,
            "requete": requete,
            "filiales": filiales, "total": len(filiales),
            "candidats_examines": examines, "candidats_total_amont": total_amont,
            "candidats_tronques": tronques,
            "methode": (
                f"candidates from the upstream full-text index on \"{requete}\" "
                "(which indexes officers), kept ONLY if a legal-entity "
                f"officer carries SIREN {racine}; " + _METHODE),
            "caveat": (
                (f"{examines} candidates examined out of {total_amont}: the inventory is "
                 "a SAMPLE, not an exhaustive list — rerun with a higher "
                 "max_pages. ") if tronques else "") + _CAVEAT,
        }

    @mcp.tool(annotations=LECTURE)
    def fr_groupe(
        siren: str,
        op: Literal["ascendant", "descendant"] = "ascendant",
        max_depth: int = 4,
        max_pages: int = 4,
    ) -> dict:
        """Ownership chain of a French company — qualify its
        INDEPENDENCE, or inventory a group.

        ⚠️ `confiance="indeterminee"` (no parent reached) does NOT mean
        "independent": the beneficial-owners register has been closed to the public
        since 31/07/2024. `motifs_indetermination` says why — including
        `sans_siren`: legal-entity officer without a SIREN (often foreign), returned
        in `exclus.sans_siren` (name, capacity). No parent is an ANSWER.
        ⚠️ Going down, the upstream caps at 25 results/page and 10,000 in total:
        `candidats_tronques=true` signals a sample, not an inventory.

        `op`:
        - **"ascendant"** (default): goes up the legal-entity officers, BREADTH-first
          (a company often has several parents at the same level). Returns
          `{tetes, liens, confiance, cycle, tronque}`.
        - **"descendant"**: entities controlled by this SIREN — one call instead of N.

        Why: `categorie_entreprise` (PME/ETI/GE) is computed by INSEE at the GROUP
        level, never at the entity level — a tiny subsidiary comes out as "GE".

        `confiance` per link: **forte** = the capacity implies ownership (limited
        partner / unlimited liability) · **moyenne** = corporate office (chair,
        director, manager): governance proven, control suggested · **faible** =
        neither (GIE member, liquidator) · **inconnue** = unlisted
        capacity. Only forte and moyenne are traversed. Statutory audit is
        excluded (commissaire OR contrôleur, counted in `exclus`).

        Args:
            siren: Company SIREN (9 digits).
            op: ascendant (default) | descendant.
            max_depth: op="ascendant" — levels climbed (default 4, max 6).
            max_pages: op="descendant" — pages of 25 candidates examined (default 4,
                max 20).
        """
        digits = "".join(c for c in str(siren) if c.isdigit())
        if len(digits) != 9:
            raise _bad(f"Invalid SIREN: {siren!r} — 9 digits expected.")
        if op not in ("ascendant", "descendant"):
            raise _bad("op must be 'ascendant' or 'descendant'")
        fiche = _fiche_ou_refus(digits)
        if op == "ascendant":
            return _ascendant(digits, fiche, max(1, min(max_depth, _MAX_DEPTH_DUR)))
        return _descendant(digits, fiche, max(1, min(max_pages, _MAX_PAGES_DUR)))


__all__ = ["register", "confiance_du_lien"]
