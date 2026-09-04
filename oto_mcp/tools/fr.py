"""Données entreprise France — identité, finances, événements légaux, appels d'offres.

Sources open data (pas de clé) : API Recherche Entreprises, INPI/BCE, BODACC, BOAMP.
Source payante (clé SIRENE) : INSEE SIRENE (SIRET, siège).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access
# Hors de `tools/` : ce module ne sert AUCUN outil, il porte la lecture du
# registre des personnes. `tools/<m>.py` est réservé aux modules montés depuis
# le registre de connecteurs (garde-fou `test_capabilities_drift`).
from .. import fr_registre

# Les annotations du bloc `finances` (0 = non déclaré, valeur illisible, montant
# invraisemblable) sont posées par **FOD**, pas ici : elles sont vraies quel que soit
# le consommateur, donc elles vivent au seul point que tous traversent (ADR 0028
# amendée le 12/08 — « FOD dit ce qu'il SAIT, jamais ce qu'il CROIT »). Le backend
# les fait passer, sans les recalculer : deux détections divergeraient.
#
# CE qui reste ici est propre à la SURFACE AGENT — l'avertissement sur les paramètres
# de filtre, qui n'existent que dans ce tool.
_FILTRE_CA_AVERTISSEMENT = (
    "⚠️ `ca_min`/`ca_max` filtrent en amont sur un montant dont l'unité est INCONNUE "
    "(euros pour les uns, milliers pour les autres, parfois d'une année à l'autre chez "
    "la même entreprise) et dont le 0 signifie « non déclaré ». Conséquences mesurées : "
    "la plage laisse passer les entreprises SANS CA connu (elles valent 0, donc ≤ toute "
    "borne haute) et rate celles qui ont déposé en milliers. Sur "
    "`tranche_effectif_salarie=51,52,53 & ca_max=400000`, les 12 résultats sont des "
    "grandes entreprises — 11 n'y sont que par leur 0, et la 12ᵉ est une banque à "
    "392 M€ lue comme 392 k€. Pour qualifier par taille, préférer "
    "`tranche_effectif_salarie` ou `categorie_entreprise`, et ne conclure sur un CA "
    "qu'après lecture du dépôt (`fr_bilans`)."
)

# Formes juridiques couramment ÉNONCÉES devant le nom (« la SCI Untel »), et les
# catégories juridiques INSEE correspondantes. Le répertoire, lui, n'inscrit
# presque jamais la forme dans la dénomination : « SCI ASC » ne ramène que des
# sociétés littéralement nommées ainsi, et jamais la SCI immatriculée « ASC ».
# La forme appartient donc à un FILTRE, pas au texte cherché (feedback #325).
# Codes validés contre la liste que l'API renvoie sur valeur invalide (30/07/2026).
_LEGAL_FORM_CODES: dict[str, tuple[str, ...]] = {
    "SCI": ("6540", "6541", "6542", "6543", "6544"),   # famille 654x = sociétés civiles immobilières
    "SCCV": ("6540", "6541"),
    "SCM": ("6533",),
    "SCP": ("6532",),
    "SARL": ("5499", "5485", "5410"),
    "EURL": ("5499",),                                  # SARL à associé unique — même catégorie
    "SAS": ("5710", "5785"),
    "SASU": ("5710",),
    "SA": ("5599", "5699"),
    "SNC": ("5202", "5203"),
}


def _split_legal_form(query: Optional[str]) -> Optional[tuple[str, str]]:
    """(forme, reste) si `query` commence par une forme juridique suivie d'un nom.

    « SCI ASC » → ("SCI", "ASC"). « SCI » seul → None (pas de nom à chercher),
    « ASCENSEURS » → None (préfixe non isolé)."""
    if not query:
        return None
    parts = query.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    form = parts[0].upper().strip(".")
    return (form, parts[1].strip()) if form in _LEGAL_FORM_CODES and parts[1].strip() else None


def register(mcp: FastMCP) -> None:
    from ..fod import fr as fod_fr  # données entreprise + INSEE keyé (passthrough) + index BOAMP/ACCO → service FOD

    # Données entreprise open-data servies par le service FOD dédié (ADR 0028) — le
    # backend n'exécute plus ces appels (dont l'INPI DuckDB, workload lourd) in-process.
    # Objets proxy à surface identique aux clients france_opendata → seuls ces
    # bindings changent, les corps des tools restent inchangés.
    entreprises = fod_fr.entreprises
    inpi = fod_fr.inpi
    bodacc = fod_fr.bodacc
    egapro = fod_fr.egapro

    # --- Identité (API Recherche Entreprises, open data) ---

    @mcp.tool(meta={"exhaustive_via": "fr_stock_search"})
    def fr_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        departement: Optional[str] = None,
        code_postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        categorie_entreprise: Optional[str] = None,
        ca_min: Optional[int] = None,
        ca_max: Optional[int] = None,
        idcc: Optional[str] = None,
        nature_juridique: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Search French companies — returns identity, HQ, NAF, employees,
        directors, finances, matched establishments. At least one filter required.

        ⚠️ **Énumération plafonnée ~10 000** (`page × per_page`, per_page ≤ 25) :
        l'API tronque **sans erreur** au-delà. Pour ÉNUMÉRER exhaustivement un grand
        ensemble (« toutes les boîtes du secteur X en région Y » quand il y en a
        des dizaines de milliers), bascule sur **`fr_stock_search`** (parquet SIRENE,
        pas de plafond). Ce tool reste le bon choix pour chercher/qualifier (indexé,
        rapide, filtres riches) tant que le résultat tient sous ~10k.

        ⚠️ Geographic filters (departement, code_postal, commune) match ANY
        establishment, NOT only the head office (siège). To target companies whose
        SIÈGE is in a département, use `fr_stock_search(departement=…,
        sieges_only=True)`.

        ⚠️ **`ca_min`/`ca_max` ne qualifient PAS une taille d'entreprise.** Le
        filtre porte sur un montant dont l'unité varie selon le dépôt (euros ou
        milliers, parfois d'une année à l'autre chez la même entreprise) et dont le
        0 signifie « non déclaré » — donc toute borne haute ramène en masse des
        entreprises dont le CA est inconnu, ce qui biaise vers les PLUS GROSSES.
        Mesuré : `tranche_effectif_salarie=51,52,53 & ca_max=400000` rend 12
        résultats, tous grandes entreprises, aucun vrai positif. Pour cibler par
        taille, utiliser `tranche_effectif_salarie` ou `categorie_entreprise`.
        Le bloc `finances` des résultats porte les mêmes réserves, marquées
        ligne à ligne (`alerte`) — cf. `finances_avertissement`.

        Spoken legal forms: people say "the SCI Untel", but the register rarely writes
        the form into the name — so a query like "SCI ASC" only matches companies
        literally NAMED that. On page 1, this tool detects such a prefix and ALSO runs
        the name alone filtered on that form, appending the extra hits (flagged
        `matched_by="legal_form"`) and reporting what it did under `legal_form_retry`.

        Args:
            query: Full-text search (company name, SIREN, brand…).
            naf: NAF activity codes, comma-separated (e.g. "62.01Z,62.02A").
            departement: Department code (e.g. "75").
            code_postal: Postal code (e.g. "75001").
            commune: INSEE commune code (COG, 5 digits — e.g. "67482" for
                Strasbourg). NOT a city name (a name raises "valeur non valide").
                For a place, pass `code_postal`, or use `fr_stock_search` (which
                resolves enseigne/commune by code too).
            employees: Employee-range codes (INSEE TEFEN) of the unité légale, comma-separated.
            categorie_entreprise: INSEE size category — "PME", "ETI" or "GE".
            ca_min: Minimum turnover — ⚠️ NOT reliably in euros, see below.
            ca_max: Maximum turnover — ⚠️ NOT reliably in euros, see below.
            idcc: IDCC codes (conventions collectives), comma-separated.
            nature_juridique: INSEE legal-form codes, comma-separated (e.g. "6540" for
                SCI, "5710" for SAS). Exact 4-digit codes only — an invalid value makes
                the API answer with the full list of valid ones.
            page: 1-based page number.
            per_page: Page size (max 25).
        """
        def _search(q, nj, pg):
            return entreprises.search(
                query=q,
                naf=[s.strip() for s in naf.split(",")] if naf else None,
                departement=departement,
                code_postal=code_postal,
                commune=commune,
                employees=[s.strip() for s in employees.split(",")] if employees else None,
                categorie_entreprise=categorie_entreprise,
                ca_min=ca_min, ca_max=ca_max,
                idcc=[s.strip() for s in idcc.split(",")] if idcc else None,
                nature_juridique=nj,
                page=pg, per_page=per_page,
            )

        explicit_nj = [s.strip() for s in nature_juridique.split(",")] if nature_juridique else None
        res = _search(query, explicit_nj, page)
        # Repêchage par forme juridique — seulement page 1 (c'est là qu'on conclut
        # « pas trouvée ») et seulement si l'appelant n'a pas déjà tranché la forme.
        # La recherche littérale reste en tête : les sociétés vraiment nommées
        # « SCI ASC » existent et sont des réponses légitimes.
        form = _split_legal_form(query) if page == 1 and not explicit_nj else None
        # Même compactage que fr_get : le payload brut (sièges 30+ champs,
        # matching_etablissements géo intégrale) explose vite (vu 48k chars).
        # Les établissements compactés restent là — test de co-localisation.
        # Compacter AVANT de marquer : la projection ne garde que des clés connues,
        # un flag posé plus tôt serait silencieusement perdu.
        res["results"] = [_compact_identity(r) for r in res.get("results", [])]
        if form:
            label, name = form
            codes = list(_LEGAL_FORM_CODES[label])
            extra = _search(name, codes, 1)
            seen = {r.get("siren") for r in res["results"]}
            added = [_compact_identity(r) for r in extra.get("results", [])
                     if r.get("siren") not in seen]
            for r in added:
                r["matched_by"] = "legal_form"
            res["results"] += added
            res["legal_form_retry"] = {
                "form": label, "query": name, "nature_juridique": codes,
                "total_results": extra.get("total_results"), "added": len(added),
            }
        # Qui filtre sur le CA a besoin de savoir sur QUOI il vient de filtrer :
        # l'amont compare une borne en euros à un nombre sans unité dont le 0 vaut
        # « non déclaré ». Dit ici, au moment où la question se pose (#399).
        if ca_min is not None or ca_max is not None:
            res["filtre_ca_avertissement"] = _FILTRE_CA_AVERTISSEMENT
        return res

    # 7 ratios top B2B + métadonnées d'exercice. Le reste (marge_brute, ebit,
    # capacite_de_remboursement, couverture_des_interets, caf_sur_ca,
    # ratio_de_vetuste) reste accessible via fr_bilan(siren, date).
    _LATEST_BILAN_KEYS = (
        "date_cloture_exercice", "type_bilan",
        "chiffre_d_affaires", "resultat_net", "ebe",
        "marge_ebe", "autonomie_financiere", "taux_d_endettement",
        "ratio_de_liquidite",
        # Les AVERTISSEMENTS de l'amont, jamais projetés hors de la réponse : un
        # `chiffre_d_affaires: None` accompagné de `valeur_indisponible` dit « le
        # dépôt porte un montant qu'on ne sait pas lire » ; le même None seul dit
        # « pas de dépôt ». Les jeter rendrait au consommateur exactement
        # l'ambiguïté que FOD vient de lever (ADR 0028 amendée).
        "alerte", "postes_indisponibles",
    )

    # fr_get compact : le payload brut recherche-entreprises pèse jusqu'à 40k chars
    # (matching_etablissements intégraux avec géo, compléments, sièges 30+ champs).
    # On garde tout ce qu'un agent de prospection consomme — identité, NAF,
    # effectifs, dirigeants, finances, et la LISTE des établissements (compactée :
    # nécessaire au test de co-localisation commune INSEE / établissement actif).
    _ETAB_KEEP = (
        "siret", "adresse", "code_postal", "commune", "libelle_commune",
        "etat_administratif", "est_siege", "activite_principale",
        "liste_enseignes", "nom_commercial", "date_creation",
    )
    _DIRIGEANT_KEEP = (
        "nom", "prenoms", "denomination", "siren", "qualite",
        "annee_de_naissance", "type_dirigeant",
    )
    _IDENTITY_KEEP = (
        "siren", "nom_complet", "nom_raison_sociale", "sigle",
        "etat_administratif", "nature_juridique", "activite_principale",
        "section_activite_principale", "tranche_effectif_salarie",
        "annee_tranche_effectif_salarie", "categorie_entreprise",
        "date_creation", "date_fermeture", "site_internet",
        "nombre_etablissements", "nombre_etablissements_ouverts", "finances",
        # Frère de `finances`, posé par FOD : sans lui dans cette liste, la
        # projection le mangerait en silence (elle ne garde que des clés connues).
        "finances_avertissement",
    )
    # Convention(s) collective(s) — l'amont la porte sous `complements.liste_idcc`.
    # Elle était perdue au mapping alors que `fr_search` ACCEPTE l'IDCC en FILTRE :
    # on pouvait chercher par convention sans jamais lire celle d'une entreprise
    # qu'on tenait déjà. L'asymétrie est le piège — pouvoir filtrer laisse croire que
    # la donnée est accessible (signal : champ « IDCC vérifié » resté à 0 % sur 500
    # lignes, alors que le client l'avait demandé explicitement).
    # Remontée À PLAT plutôt que sous `complements` : c'est la seule clé de ce bloc
    # qui porte une donnée métier ; exposer le bloc entier ramènerait ~30 booléens
    # d'annuaire (est_bio, est_qualiopi…) que personne n'a demandés.
    _COMPLEMENT_KEEP = ("liste_idcc",)
    _EVENT_KEEP = (
        "id", "dateparution", "familleavis", "familleavis_lib", "typeavis",
        "typeavis_lib", "tribunal", "commercant", "jugement", "registre",
        # Le permalien officiel DILA (#341, dossier liens #335 : pleine confiance,
        # à recopier jamais reconstruire) — il traversait fr_events mais était
        # mangé ici : la classe « projection qui ment par omission » (ADR 0028).
        "url_complete",
        # Le CONTENU de l'avis pour deux familles (#341) : le descriptif d'une
        # modification et celui d'un dépôt de comptes — même nature que
        # `jugement` (gardé depuis toujours pour les procédures collectives).
        "modificationsgenerales", "depot",
    )

    def _pick(d: dict, keys: tuple) -> dict:
        return {k: d[k] for k in keys if k in d and d[k] is not None}

    def _compact_identity(identity: dict) -> dict:
        out = _pick(identity, _IDENTITY_KEEP)
        out.update(_pick(identity.get("complements") or {}, _COMPLEMENT_KEEP))
        siege = identity.get("siege")
        if isinstance(siege, dict):
            out["siege"] = _pick(siege, _ETAB_KEEP)
        dirigeants = identity.get("dirigeants") or []
        out["dirigeants"] = [_pick(d, _DIRIGEANT_KEEP) for d in dirigeants[:10]]
        etabs = identity.get("matching_etablissements") or []
        out["etablissements"] = [_pick(e, _ETAB_KEEP) for e in etabs[:25]]
        if len(etabs) > 25:
            out["_etablissements_truncated"] = len(etabs)
        return out

    # Nombre max de SIREN par appel batch : borne le fan-out sur les API amont
    # (recherche-entreprises/INPI/BODACC, rate-limitées) ET la taille de réponse.
    _FR_GET_BATCH_MAX = 20

    def _fr_profile(siren: str) -> dict:
        """Corps de `fr_get` pour UN siren — factorisé pour le mode batch."""
        from concurrent.futures import ThreadPoolExecutor

        partial_errors: dict[str, str] = {}

        def _safe(label, fn, *fn_args):
            try:
                return fn(*fn_args)
            # noqa: SILENT — l'échec par source est rendu dans partial_errors
            except Exception as exc:  # dégradation gracieuse par sous-source
                partial_errors[label] = f"{type(exc).__name__}: {exc}"
                return None

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_identity = pool.submit(_safe, "identity", entreprises.get_by_siren, siren)
            f_bilans = pool.submit(_safe, "latest_bilan", inpi.list_exercises, siren)
            f_events = pool.submit(_safe, "recent_events", bodacc.search_by_siren, siren, None, 10)

        identity = f_identity.result()
        if not identity:
            # L'identité est la pièce maîtresse : sans elle, pas de fiche.
            if "identity" in partial_errors:
                return {"error": "identity_unavailable", "siren": siren,
                        "partial_errors": partial_errors}
            return {"error": "not_found", "siren": siren}

        exercises = f_bilans.result()
        latest_bilan = None
        finances_note = None
        latest_confidentiality = None
        if exercises:  # liste non vide = au moins un dépôt exploitable (BdF)
            latest_ex = exercises[0]
            latest_confidentiality = latest_ex.get("confidentiality")
            full = _safe("latest_bilan", inpi.get_bilan, siren,
                         latest_ex["date_cloture_exercice"])
            if full:
                latest_bilan = {k: full.get(k) for k in _LATEST_BILAN_KEYS}
            if latest_confidentiality and latest_confidentiality != "Public":
                finances_note = (
                    f"comptes « {latest_confidentiality.lower()} » (art. L.232-25) — "
                    "certains ratios sont absents par déclaration de confidentialité"
                )
        elif exercises == []:  # succès mais 0 dépôt exploitable au dataset BdF
            finances_note = (
                "aucun compte exploitable au dataset Banque de France : jamais déposé "
                "OU déposé en confidentialité totale (les micro/petites entreprises "
                "peuvent rendre leurs comptes confidentiels). Vérifier l'existence d'un "
                "dépôt confidentiel via les actes RNE sur data.inpi.fr."
            )

        events_data = f_events.result() or {}

        out = {
            "siren": siren,
            "identity": _compact_identity(identity),
            "latest_bilan": latest_bilan,
            "latest_bilan_confidentiality": latest_confidentiality,
            "recent_events": [
                _pick(e, _EVENT_KEEP) for e in events_data.get("results", [])
            ],
            "events_total": events_data.get("total_count", 0),
        }
        if finances_note:
            out["finances_note"] = finances_note
        if partial_errors:
            out["partial_errors"] = partial_errors
        return out

    @mcp.tool()
    def fr_get(siren: str | None = None, sirens: list | None = None) -> dict:
        """Full company profile by SIREN: identity (siège, directors, NAF,
        employees) + 7 top financial ratios from the latest INPI/BCE filing
        + recent BODACC legal events. Aggregates 3 open data sources in parallel.
        Use this as first call when investigating a company.

        BATCH: pass `sirens=[…]` (max 20 per call, chunk beyond) to qualify a
        LIST in one call — returns `{profiles: […], count}`, one profile per
        SIREN in input order; per-SIREN failures degrade to `{error, siren}`
        without failing the batch. For bulk HQ addresses only (no financials),
        `fr_stock_enrich` is cheaper.

        `latest_bilan` is trimmed to 7 B2B-relevant ratios (CA, résultat net,
        EBE, marge EBE, autonomie financière, taux d'endettement, liquidité).
        For the full ratio set, call `fr_bilan(siren, date_cloture)`.

        ⚠️ **`latest_bilan` = LITTÉRALEMENT le dernier exercice déposé, qui ne porte
        pas nécessairement de chiffre d'affaires** — un bilan simplifié n'a pas de
        case « CA total ». Pour un CA, remonter les exercices : `fr_bilans(siren)`
        les rend du plus récent au plus ancien avec leur `chiffre_d_affaires`, il
        faut prendre le premier qui en porte un. Vu sur Norauto : dernier exercice
        (simplifié) muet, 974 718 176 € à l'exercice précédent. Ne pas conclure
        « pas de chiffre d'affaires » sur le seul `latest_bilan`.

        Resilient to per-source failures: a timeout or error on INPI (bilan) or
        BODACC (events) degrades gracefully — the available blocks are returned
        and the failing sources are listed under `partial_errors`. Only an
        identity failure (the keystone source) fails the whole call.

        Args:
            siren: SIREN number (9 digits) — single-company mode.
            sirens: list of SIREN numbers (max 20) — batch mode. Give one OR
                the other, not both.
        """
        from concurrent.futures import ThreadPoolExecutor

        if (siren is None) == (sirens is None):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="donner `siren` (unitaire) OU `sirens` (batch), pas les deux"))
        if sirens is None:
            return _fr_profile(str(siren).strip())
        cleaned = [str(s).strip() for s in sirens if str(s).strip()]
        if not cleaned:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`sirens` est vide"))
        if len(cleaned) > _FR_GET_BATCH_MAX:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`sirens` est limité à {_FR_GET_BATCH_MAX} par appel "
                        f"(reçu {len(cleaned)}) — découpe en lots"))
        def _one(s: str) -> dict:
            try:
                return _fr_profile(s)
            # noqa: SILENT — l'échec par siren est rendu dans la ligne de résultat
            except Exception as exc:  # un SIREN en échec ne fait pas tomber le lot
                return {"error": f"{type(exc).__name__}: {exc}", "siren": s}

        # 4 profils en vol max (chacun ouvre 3-4 appels amont) : reste sous les
        # rate limits des API publiques tout en parallélisant le lot.
        with ThreadPoolExecutor(max_workers=4) as pool:
            profiles = list(pool.map(_one, cleaned))
        return {"profiles": profiles, "count": len(profiles)}

    # Borne du lot `fr_directors` : 5× celle de `fr_get`, parce qu'une fiche y
    # coûte UN appel amont (l'identité, dont on tire dirigeants ET forme
    # juridique) là où un profil `fr_get` en ouvre trois à quatre. Le terrain
    # qualifie par tranches de cent (#612).
    _FR_DIRECTORS_BATCH_MAX = 100
    # Espacement minimal entre deux départs vers l'amont (5 par seconde). Voir le
    # commentaire du lot : choix prudent, le quota amont n'étant pas publié.
    _FR_DIRECTORS_CADENCE_S = float(os.environ.get("FR_DIRECTORS_CADENCE_S", "0.2"))

    @mcp.tool()
    def fr_directors(siren: str | None = None, sirens: list | None = None) -> dict:
        """Directors declared at the French registry (RNE), for one company or a
        LIST — `sirens=[…]` (max 100) returns `{entreprises, count, obtenues,
        en_echec, erreurs, not_found, synthese}`, one entry per SIREN in input
        order.

        ⚠️ **`count` counts the records OBTAINED, not the lines returned.** A batch
        of 50 where 21 calls failed returns `count: 29`, `en_echec: 21` and names
        those 21 SIRENs in `erreurs` — at the top level, like `not_found`, because
        a hundred-line list is not re-read to find them. Reading `count` and
        `not_found` alone used to say "50 records, none missing" while 21 companies
        silently dropped out of the deliverable.

        ⚠️ **An `error` other than `not_found` is an upstream failure, not a fact
        about the company** — and it is RETRYABLE. The batch paces itself and
        retries the upstream quota (429, honouring `Retry-After`), so what reaches
        you has already been given a second chance; a SIREN still in `erreurs` says
        the upstream is busy, never that the company has no director. Ask for those
        SIRENs again, later or in a smaller batch.

        ⚠️ An empty `dirigeants` has THREE meanings, told apart by `registre`:
        the SIREN is unknown (`error: "not_found"`), the legal form is not
        registered so it declares nobody (`hors_registre` — association,
        commune: the emptiness says nothing about the company), or the company
        is registered with nobody on file (`attendu`). Never read an empty list
        as "no director" without reading `registre`. `personnes_physiques`
        counts the NAMED natural persons — a company whose only director is
        another company scores 0.

        Args:
            siren: SIREN number (9 digits) — single-company mode.
            sirens: list of SIREN numbers (max 100) — batch mode. Give one OR
                the other, not both.
        """
        from concurrent.futures import ThreadPoolExecutor

        if (siren is None) == (sirens is None):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="donner `siren` (unitaire) OU `sirens` (batch), pas les deux"))
        if sirens is None:
            un = str(siren).strip()
            return fr_registre.fiche(un, entreprises.get_by_siren(un))
        cleaned = [str(s).strip() for s in sirens if str(s).strip()]
        if not cleaned:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`sirens` est vide"))
        if len(cleaned) > _FR_DIRECTORS_BATCH_MAX:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`sirens` est limité à {_FR_DIRECTORS_BATCH_MAX} par appel "
                        f"(reçu {len(cleaned)}) — découpe en lots"))

        # Un PLAFOND en vol ne borne pas le DÉBIT : quatre requêtes qui se relaient
        # dès qu'un slot se libère envoient aussi vite que l'amont répond. Le quota
        # de Recherche Entreprises est par IP — celle de FOD, partagée par toute la
        # plateforme — donc un lot de 50 déclenchait son propre 429, quand les mêmes
        # SIREN redemandés en deux fois passaient sans erreur (otomata-tech/oto#44).
        #
        # ⚠️ La valeur ci-dessous est un choix PRUDENT, pas une mesure : le quota
        # n'est pas publié. Repère observé — un lot de 50 échouait, des lots de 10
        # et 11 passaient ; 5 départs par seconde restent nettement en deçà. À
        # ajuster si quelqu'un mesure le vrai seuil, pas au ressenti.
        cadence = _FR_DIRECTORS_CADENCE_S
        verrou, dernier_depart = threading.Lock(), [0.0]

        def _attendre_son_tour() -> None:
            with verrou:
                reste = cadence - (time.monotonic() - dernier_depart[0])
                if reste > 0:
                    time.sleep(reste)
                dernier_depart[0] = time.monotonic()

        def _one(s: str) -> dict:
            _attendre_son_tour()
            try:
                return fr_registre.fiche(s, entreprises.get_by_siren(s))
            # noqa: SILENT — l'échec par siren est rendu dans la ligne de résultat
            except Exception as exc:  # un SIREN en échec ne fait pas tomber le lot
                return {"error": f"{type(exc).__name__}: {exc}", "siren": s}

        # 4 en vol, comme le lot de `fr_get` — mais étalés (cf. ci-dessus).
        with ThreadPoolExecutor(max_workers=4) as pool:
            fiches = list(pool.map(_one, cleaned))
        # Un échec amont n'est PAS une fiche. `count` valait le nombre de lignes
        # rendues, échecs compris : une réponse à 50 dont 21 avaient échoué se
        # lisait « 50 fiches, aucune introuvable », et les 21 entreprises
        # disparaissaient du livrable ou passaient pour « sans dirigeant »
        # (otomata-tech/oto#44). Le partage est maintenant explicite, et les SIREN
        # en échec sont nommés DEUX fois — dans leur ligne et ici — au même titre
        # que les introuvables : une liste de cent fiches ne se relit pas pour les
        # retrouver.
        en_echec = [f["siren"] for f in fiches
                    if f.get("error") and f.get("error") != "not_found"]
        obtenues = [f for f in fiches if not f.get("error")]
        return {
            "entreprises": fiches,
            "count": len(obtenues),
            "obtenues": len(obtenues),
            "en_echec": len(en_echec),
            "erreurs": en_echec,
            "not_found": [f["siren"] for f in fiches if f.get("error") == "not_found"],
            "synthese": fr_registre.synthese(fiches),
        }

    # --- INSEE SIRENE (clé payante — passthrough via FOD) ---
    # Le backend résout la clé (vault : BYO membre/org → clé plateforme) + track le
    # quota, et la PASSE à FOD par-appel (ADR 0028/0037). L'appel INSEE tourne sur FOD ;
    # le credential reste maître dans le coffre backend, jamais stocké côté FOD.

    def _sirene_key() -> tuple[str, bool]:
        return access.resolve_api_key("sirene")  # (clé, is_platform)

    @mcp.tool()
    def fr_siret(siret: str) -> dict:
        """Fetch a French establishment by SIRET (14 digits) from INSEE SIRENE.

        Args:
            siret: SIRET number (14 digits).
        """
        key, is_platform = _sirene_key()
        result = fod_fr.insee_siret(siret, key)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    @mcp.tool()
    def fr_avis_sirene(siret: str) -> dict:
        """Official INSEE « Avis de situation au répertoire SIRENE » PDF of an
        establishment — the signed 1-page administrative document, for a dossier.

        Unlike `fr_siret` (JSON identity, needs the SIRENE key), this wraps INSEE's
        PUBLIC avis endpoint (no key) and returns a directly-fetchable URL to the PDF
        (availability is checked). The caller downloads the URL to get the file.

        Args:
            siret: SIRET number (14 digits ; spaces/dots are ignored).
        """
        import requests
        digits = "".join(c for c in str(siret) if c.isdigit())
        if len(digits) != 14:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"SIRET invalide : {siret!r} — 14 chiffres attendus.")))
        url = f"https://api-avis-situation-sirene.insee.fr/identification/pdf/{digits}"
        try:
            resp = requests.head(url, timeout=20)
        except requests.RequestException as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"endpoint INSEE avis-situation injoignable : {e}")))
        if resp.status_code == 404:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"aucun avis SIRENE pour le SIRET {digits} — établissement inconnu "
                "au répertoire (SIRET erroné ?).")))
        if resp.status_code != 200 or "pdf" not in resp.headers.get("Content-Type", "").lower():
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"INSEE avis-situation a répondu HTTP {resp.status_code} "
                f"({resp.headers.get('Content-Type', '?')}).")))
        return {"siret": digits, "url": url, "format": "pdf"}

    @mcp.tool()
    def fr_headquarters(siren: str) -> Optional[dict]:
        """Fetch the headquarters (siège) of a company from INSEE SIRENE.

        Args:
            siren: SIREN number (9 digits).
        """
        key, is_platform = _sirene_key()
        result = fod_fr.insee_headquarters(siren, key)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    # --- Finances (INPI/BCE, open data) ---

    @mcp.tool()
    def fr_bilans(siren: str) -> dict:
        """List available INPI/BCE annual filings for a SIREN.

        Returns exercise dates, bilan type (C=complet, S=simplifié, K=consolidé),
        confidentiality status, and turnover. Typically 3-9 years of history.

        Args:
            siren: SIREN number (9 digits).
        """
        items = inpi.list_exercises(siren)
        return {"siren": siren, "items": items, "total": len(items)}

    @mcp.tool()
    def fr_bilan(siren: str, date_cloture: str) -> dict:
        """Fetch one INPI/BCE annual filing with full financial ratios.

        Returns: CA, EBE, EBIT, résultat net, marge EBE, autonomie financière,
        taux d'endettement, liquidité, vétusté, BFR, rotation stocks,
        crédit clients/fournisseurs, couverture intérêts.
        Use fr_bilans first to discover available dates.

        Args:
            siren: SIREN number (9 digits).
            date_cloture: Exercise closing date (YYYY-MM-DD, e.g. "2024-12-31").
        """
        result = inpi.get_bilan(siren, date_cloture)
        if result is None:
            return {"error": "exercise_not_found", "siren": siren, "date_cloture": date_cloture}
        return result

    # --- Événements légaux (BODACC, open data) ---

    @mcp.tool()
    def fr_events(
        siren: str,
        famille: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """List BODACC legal events for a company: creations, modifications,
        sales, collective proceedings, annual filings.

        Args:
            siren: SIREN number (9 digits).
            famille: Filter by type — creation, modification, radiation, vente,
                procedure_collective, dpc (dépôt des comptes).
            limit: Max results (default 20).
        """
        return bodacc.search_by_siren(siren, famille=famille, limit=limit)

    @mcp.tool()
    def fr_events_batch(
        sirens: list[str],
        famille: Optional[str] = "collective",
    ) -> dict:
        """Check BODACC legal events for MANY companies at once (e.g. screen 700
        SIRENs for collective proceedings) — batched into a few upstream requests.

        Deterministic: returns a flat `annonces` list (one row per announcement,
        table-friendly) plus a `synthese` block of aggregate counts
        (sirens_avec_annonce, par_jugement_nature, …). It does NOT decide whether
        a company is currently *in* proceedings — that requires reading each
        annonce's `texte` (the jugement wording: "Ouvre la procédure…" vs
        "Clôture pour…"). Read `texte` and judge per SIREN.

        Args:
            sirens: list of SIRENs (9 digits).
            famille: BODACC family filter. Default "collective" (procédures
                collectives). Pass None for all families (creations, sales…).
        """
        return bodacc.search_batch(sirens, famille=famille)

    # --- Appels d'offres (BOAMP, open data) ---

    @mcp.tool()
    def fr_tenders_search(
        query: Optional[str] = None,
        descripteur: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        type_marche: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search French public procurement tenders (BOAMP).

        Args:
            query: Full-text search in the notice subject.
            descripteur: BOAMP descriptor (e.g. "Photovoltaïque", "Informatique").
            departement: Department code (e.g. "75").
            date_from: Publication date start (YYYY-MM-DD).
            date_to: Publication date end (YYYY-MM-DD).
            type_marche: Market type (TRAVAUX, FOURNITURES, SERVICES).
            limit: Max results (default 20, max 100).
        """
        return fod_fr.search_boamp(
            query=query, descripteur=descripteur, departement=departement,
            date_from=date_from, date_to=date_to, type_marche=type_marche,
            limit=limit,
        )

    @mcp.tool()
    def fr_tenders_get(idweb: str) -> dict:
        """Fetch a single BOAMP tender by its ID.

        Args:
            idweb: BOAMP notice identifier (e.g. "26-50647").
        """
        result = fod_fr.get_boamp(idweb)
        if result is None:
            return {"error": "not_found", "idweb": idweb}
        return result

    # --- Aides publiques aux entreprises (data.aides-entreprises.fr, open data) ---

    @mcp.tool()
    def fr_aides_search(
        insee: Optional[str] = None,
        code_postal: Optional[str] = None,
        effectif: Optional[int] = None,
        nature: Optional[str] = None,
        echeance_avant: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Shortlist d'aides publiques FR (subventions, prêts, garanties, AAP) pour
        une entreprise/un projet — base data.aides-entreprises.fr (réf. État, ~2 400
        aides actives, màj quotidienne, la base élague les périmées).

        Renvoie le filtre DÉTERMINISTE (géo par hiérarchie commune→dept→région→
        France/UE + tranche d'effectif + nature + échéance) avec l'entonnoir mesuré
        (`funnel`). ⚠️ La pertinence SECTORIELLE ne peut PAS venir de la base (son
        tagging profils est sur-inclusif à 99 %) ni d'un scoring lexical : c'est À
        TOI de re-ranker la shortlist en lisant nom/objet. Règle anti-hallucination :
        ne retiens que des `id`, puis re-rends chaque fiche via `fr_aides_get(id)` —
        ne JAMAIS reformuler nom/objet de mémoire, citer littéralement.

        Args:
            insee: code INSEE de la commune (préféré — ex. "31555" Toulouse).
            code_postal: à défaut d'INSEE (résolution best-effort).
            effectif: nombre de salariés (filtre les tranches ; les aides sans
                restriction restent).
            nature: sous-chaîne du type d'aide ("subvention", "prêt", "garantie",
                "avance", "exonération", "prestation"...).
            echeance_avant: YYYY-MM-DD — aides À échéance clôturant avant la date
                (veille AAP ; exclut les aides permanentes).
            q: filtre lexical AND (pré-filtre grossier, PAS un tri de pertinence).
            limit: fiches renvoyées (défaut 50 ; `count` = total filtré).
            offset: pagination.
        """
        try:
            return fod_fr.search_aides(
                insee=insee, code_postal=code_postal, effectif=effectif, nature=nature,
                echeance_avant=echeance_avant, q=q, limit=limit, offset=offset,
            )
        except ValueError as e:  # commune/CP inconnu du référentiel territoires
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def fr_aides_get(id_aid: str, raw: bool = False) -> dict:
        """Fiche COMPLÈTE d'une aide (source de vérité après re-rank de
        `fr_aides_search` — objet/conditions/montant intégraux, financeurs,
        contacts, sources officielles). Texte décodé (entités HTML nettoyées) et
        `cache_indexation` réduit à ses extraits utiles (natures, financeurs,
        territoires, contacts, sources).

        Args:
            id_aid: identifiant de l'aide (champ `id` de fr_aides_search).
            raw: True = enregistrement brut de la base (non décodé, volumineux ;
                pour un consommateur qui en dépend).
        """
        result = fod_fr.get_aide(id_aid, raw=raw)
        if result is None:
            return {"error": "not_found", "id_aid": id_aid}
        return result

    # --- Accords d'entreprise (ACCO, open data) ---
    # Base nationale des accords collectifs (DILA), accords conclus depuis le
    # 01/09/2017. Métadonnées : qui (SIRET, raison sociale, IDCC = convention
    # collective), quoi (thèmes codés), quand (date_texte), nature (ACCORD initial
    # vs AVENANT = renégociation). Le texte intégral n'est pas toujours publié
    # (conforme_version_integrale), mais le « qui a négocié quoi et quand » l'est.

    @mcp.tool()
    def fr_accords_search(
        query: Optional[str] = None,
        themes: Optional[list[str]] = None,
        nature: Optional[str] = None,
        siren: Optional[str] = None,
        siret: Optional[str] = None,
        idcc: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        latest_per_siret: bool = False,
        sort_by: str = "date",
        sort_dir: str = "desc",
        limit: int = 20,
        offset: int = 0,
        tranche_effectifs: Optional[list[str]] = None,
        categories_entreprise: Optional[list[str]] = None,
        exclude_categories: Optional[list[str]] = None,
        scan_cap: Optional[int] = None,
    ) -> dict:
        """Search French company collective agreements (accords d'entreprise, ACCO).

        Neutral primitive returning raw rows — compose your own need via filters,
        sort and per-company reduction.

        ⚠️ `themes` PROVES PRESENCE, NEVER ABSENCE. Theme codes come from DILA's
        own indexing of the filing: declarative, uneven, and demonstrably
        incomplete. Measured case — CAFES BIBAL VENDING (SIREN 345255087) filed
        the SAME kind of agreement twice: the 2019 one is coded 111/112, the
        2025 one is coded 081-084 only, yet its article 5 ("Régime de
        remboursement complémentaire de frais de santé et de prévoyance")
        re-institutes health AND pension for 4 years. Filtering on 111/112 there
        returns the 2019 act alone — reading "no recent act" off that is a FALSE
        NEGATIVE produced by the source, not by this tool. Health clauses
        routinely travel inside an agreement titled "égalité professionnelle" or
        "NAO", and get coded accordingly.

        Consequence for prospecting: to assert a company's scheme is DORMANT,
        theme codes are not enough — search by `siren` WITHOUT `themes`, then
        read the recent acts with `fr_accords_text` (health sections carry very
        stable headings: "frais de santé", "régime de prévoyance",
        "complémentaire santé"). Use `themes` to FIND candidates cheaply, the
        text to CONFIRM the ones you are about to act on.

        Common recipes:
        - Who just renegotiated their health/pension scheme (candidates, not an
          exhaustive set): themes=["111","112"], nature="AVENANT", sort_dir="desc".
        - Does THIS company have a health/pension agreement, and when: search by
          siren WITHOUT themes, sort_dir="desc", then read the recent acts.
        - PROSPECTING AUTONOMOUS SMEs (skip group subsidiaries, whose insurance is
          decided at HQ): exclude_categories=["GE"]. 26% of the companies filing a
          health/pension agreement are GE — filtering here is one query instead of
          a per-company qualification pass.

        Args:
            query: Substring in the agreement TITLE (ILIKE) — not in its text. The
                local index holds metadata only; body text is fetched per act by
                fr_accords_text, so a clause cannot be searched across the corpus.
            themes: Theme codes (OR). Health/pension: "111" (complémentaire santé),
                "112" (prévoyance), "113" (retraite supplémentaire). Use
                fr_accords_themes to discover codes. Presence-only — see the
                warning above before concluding anything from their ABSENCE.
            nature: ACCORD (initial) | AVENANT (amendment = renegotiation) | …
            siren: Company SIREN (9 digits) — matches ALL its establishments.
                PREFER this over siret to check a company: ACCO files an agreement
                under the DEPOSITING establishment's SIRET, often not the siège, so a
                siège-SIRET lookup misses agreements.
            siret: Exact establishment SIRET (14 digits).
            idcc: Exact branch code (convention collective).
            departement: Postal code prefix (2 digits).
            date_from / date_to: Bounds on the signature date (YYYY-MM-DD).
            latest_per_siret: Keep only one row per company — its most recent act —
                BEFORE applying date_from/date_to (so date bounds then filter the
                company's LAST act → dormant-contract detection).
            sort_by: date | date_depot | date_diffusion | date_maj (default date).
            sort_dir: asc (oldest first) | desc (newest first).
            limit: Max results (default 20, max 100).
            offset: Skip that many rows — page N = offset=(N-1)*limit. THE way to
                exhaust a result set bigger than `limit`: `total_count` tells you
                the volume, walk it with offset (do NOT slide `date_from`, which
                loses rows silently when more than `limit` share the same date).
            tranche_effectifs: INSEE employee-range codes (TEFEN) of the filing
                establishment, e.g. ["11","12","21"] — ACCO carries no company
                size, so this is resolved against the local SIRENE stock. Use it to
                keep SMEs only instead of post-filtering by hand.
            exclude_categories: drop these INSEE size categories ("PME"|"ETI"|"GE").
                THE way to skip group subsidiaries: the category is computed by
                INSEE over the GROUP perimeter, so a subsidiary that is small by
                its own headcount still reads "GE" (GTIE Rennes, €8M and a 20-49
                band, is a GE because it belongs to VINCI). No other field carries
                that. Resolved against the SIRENE legal-unit stock.
            categories_entreprise: keep ONLY these categories. ⚠️ NOT the mirror of
                exclude_categories: 4% of the companies filing an agreement have no
                category on record — an inclusion drops them (you cannot assert a
                company is an SME without knowing), an exclusion keeps them. To
                target autonomous SMEs, prefer exclude_categories=["GE"].
            scan_cap: how many agreements the SIRENE cross-check examines before
                paginating (default 5000, max 25000). `effectifs_filter.truncated`
                =true in the response means the pool is LARGER than this cap — the
                answer is then NOT exhaustive. Raise it to cover a whole pool
                (health/pension nationally is ~9700 agreements).
        """
        return fod_fr.search_acco(
            query=query, themes=themes, nature=nature, siren=siren, siret=siret,
            idcc=idcc, departement=departement, date_from=date_from, date_to=date_to,
            latest_per_siret=latest_per_siret, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset, tranche_effectifs=tranche_effectifs,
            categories_entreprise=categories_entreprise,
            exclude_categories=exclude_categories, scan_cap=scan_cap,
        )

    @mcp.tool()
    def fr_accords_get(id_or_numero: str, include_text: bool = False) -> dict:
        """Fetch a single company agreement by its DILA id (ACCOTEXT…) or numero (T…).

        Returns METADATA (who, when, themes, branch…). The body text is NOT in the
        local index — it is fetched per act from Légifrance, so ask for it with
        `include_text=True` (or call `fr_accords_text`, which also paginates long
        agreements). Without that, this tool cannot tell you what an agreement
        SAYS — and what it says is often the point: theme codes are declarative
        and incomplete (cf. fr_accords_search), so a health clause is regularly
        found only by reading.

        Args:
            id_or_numero: DILA identifier (ACCOTEXT000…) or deposit number (T…).
            include_text: also fetch the full text (one Légifrance call). Long
                agreements come back truncated here — `texte_tronque`=true means
                use `fr_accords_text(acco_id, offset=…)` to walk the rest.
        """
        result = fod_fr.get_acco(id_or_numero)
        if result is None:
            return {"error": "not_found", "id_or_numero": id_or_numero}
        if not include_text:
            return result
        from ..fod import ccn as fod_ccn
        # Le texte se demande par ID DILA : l'appelant a pu nommer l'acte par son
        # numéro de dépôt (T…), que Légifrance ne connaît pas.
        text = fod_ccn.accords_text(result.get("id") or id_or_numero)
        return {**result, "texte": text.get("texte"),
                "texte_chars": text.get("texte_chars"),
                "texte_tronque": text.get("tronque"),
                "next_offset": text.get("next_offset"),
                # Breaking FOD #335 (relayé #343) : `permalien` (vérifiable, 404
                # franc) + `lien_construit` (Légifrance, best-effort) remplacent
                # `source_url`, disparu de ce fond.
                "permalien": text.get("permalien"),
                "lien_construit": text.get("lien_construit")}

    @mcp.tool()
    def fr_accords_themes() -> list[dict]:
        """List the agreement theme codes present in the database (code → label →
        count). Discovery helper so you can pick `themes` for fr_accords_search.

        The counts say how often DILA APPLIED a code, not how many agreements
        cover the topic — the indexing is declarative and misses clauses (see
        the warning on fr_accords_search)."""
        return fod_fr.acco_themes()

    @mcp.tool()
    def fr_accords_text(acco_id: str, offset: int = 0) -> dict:
        """Full text of a company agreement (accord d'entreprise) by its DILA
        id — fetched on demand from Légifrance (the local ACCO index only has
        metadata). Chain after fr_accords_search / fr_accords_get.

        Returns metadata + `texte` (extracted from the filed docx; may be empty
        when no integral version was published) + `texte_chars`/`offset`/
        `next_offset` + `permalien` (verifiable link, 404s honestly when the
        text is absent) + `lien_construit` (best-effort Légifrance pattern,
        not guaranteed to resolve).

        Args:
            acco_id: DILA id (ACCOTEXT000…) from fr_accords_search results.
            offset: start position in the text. Long agreements come back in
                chunks: when `tronque` is true, call again with
                offset=`next_offset` to get the rest (health/pension clauses and
                final provisions usually sit at the END of a merger agreement).
        """
        from ..fod import ccn as fod_ccn
        return fod_ccn.accords_text(acco_id, offset=offset)

    # Jurisprudence / codes / conventions collectives (juris_*/loi_*/ccn_*) ont
    # été extraits vers le connecteur `droit` (tools/droit.py) — carte « Info
    # légale FR », ils n'étaient pas de l'INSEE. `fr_accords_*` reste ici (scopé
    # entreprise par SIREN).

    # --- Index égalité F-H (Egapro, open data) -------------------------------

    @mcp.tool()
    def fr_egapro_declaration(siren: str, year: Optional[int] = None) -> dict:
        """Gender-equality index (Egapro) declaration of a company, by SIREN.

        Every French company with 50+ employees must file an annual Egapro index.
        The payoff here is the **exact headcount** (`entreprise.effectif.total`) —
        where SIRENE only gives a bracket — plus the NAF code and per-indicator
        scores. Useful to qualify a lead's real size and confirm it is a 50+
        employer subject to social obligations.

        `year` omitted = most recent filing found (the API does not list a SIREN's
        years, so it scans back from the current year). Returns `{found: false}`
        when the company has no Egapro declaration (under 50 employees, or not filed).
        """
        decl = egapro.declaration(siren, year) if year else egapro.latest_declaration(siren)
        if decl is None:
            return {"found": False, "siren": siren,
                    "message": "Aucune déclaration Egapro (entreprise <50 salariés ou non déposée)."}
        return decl

