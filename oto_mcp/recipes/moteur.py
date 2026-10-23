"""Le moteur d'une recette : appeler l'outil page par page, filtrer, fabriquer les
lignes, écrire. Sans modèle.

**Chaque page est un appel ordinaire de l'outil** : `tools/meta.executer_cible`, le
corps d'`oto_call` — mêmes gardes (activation du connecteur, axes, org du run), même
journal sous le NOM DE L'OUTIL (donc même facturation, par sa `quantity`), même
rédaction. Les lignes sont fabriquées depuis le résultat RÉDIGÉ : un champ que l'org
cache aux agents n'atterrit pas dans un tableau que des agents lisent.

**Ce qu'une recette appelle** : un outil de connecteur DÉCLARÉ EN LECTURE
(`tools/lecture.LECTURE`) — jamais un outil qui écrit chez le tiers, ni un outil non
déclaré : le défaut est le refus (`recipe_tool_not_read_only`).

**Ce qui arrête une exécution, et ce qu'elle rend.** Le plafond de dépense
(`limits.max_units`, compté sur la `quantity` que l'outil FACTURE — les unités déclarées
par la recette seulement quand l'outil n'en rend pas : `units_basis` le dit), le nombre
de pages DE CET APPEL, le budget d'horloge (au-delà : reçu partiel et `resume`), la
dernière page, ou un refus — de l'outil (clé, crédits) ou du tableau. Le reçu porte des
COMPTES et des CODES, jamais une valeur lue.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Optional

from starlette.concurrency import run_in_threadpool

from .. import outils_du_travail, redaction
from ..datastore import par_reference as pr
from ..mcp_errors import McpError
from ..tool_visibility import namespace_of
from ..tools import lecture
from . import contrat
from . import correspondance as co
from . import ecriture

#: Budget d'horloge d'un appel, vérifié AVANT chaque page : passé ce délai le reçu est
#: rendu partiel avec `resume`, plutôt que coupé sans reçu. Celui de `par_reference`.
BUDGET_S = pr.BUDGET_S
#: Une clé de résultat qui n'a pas la forme d'un nom de champ est une VALEUR prise comme
#: clé (un dictionnaire indexé par e-mail, par nom de société) : elle ne remonte pas.
_NOM_DE_CHAMP = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


class RecetteRefusee(Exception):
    """Refus AVANT tout appel au connecteur. `code` nommé, message pour l'agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _jeton(etat: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(etat).encode()).decode()


def empreinte(corps: dict) -> str:
    """L'empreinte d'une recette, portée par ses jetons de reprise : le jeton d'une
    AUTRE recette (ou d'une autre version) ne se rejoue pas sur celle-ci."""
    brut = json.dumps(corps, sort_keys=True, default=str).encode()
    return hashlib.sha256(brut).hexdigest()[:16]


def lire_reprise(reprise: Optional[str], corps: Optional[dict] = None) -> dict:
    """L'état d'une reprise. `r` = la ligne parente en cours (`for_each`). Le jeton doit
    porter l'empreinte de CETTE recette, et la dépense faite n'est jamais négative : un
    jeton fabriqué ne rouvre pas le plafond."""
    vide = {"p": None, "c": None, "u": 0, "r": None, "h": empreinte(corps) if corps else None}
    if not reprise:
        return vide
    try:
        etat = json.loads(base64.urlsafe_b64decode(reprise.encode()).decode())
        lu = {"p": etat.get("p"), "c": etat.get("c"), "u": max(0, int(etat.get("u", 0))),
              "r": etat.get("r"), "h": etat.get("h")}
    except (ValueError, TypeError, AttributeError):
        lu = None
    if lu is None or (corps is not None and lu["h"] != vide["h"]):
        raise RecetteRefusee("invalid_resume", "`resume` is not a token returned by a "
                                               "previous run of this recipe (and version).")
    return lu


def _cles_sures(cles) -> list:
    return sorted(k for k in cles if isinstance(k, str) and _NOM_DE_CHAMP.match(k))


def _recu() -> dict:
    return {"pages": 0, "items_seen": 0, "units": 0, "units_basis": None,
            "rows_built": 0, "written": 0,
            "updated": 0, "existing_left_untouched": 0, "skipped_where": 0,
            "skipped_no_key": 0, "duplicates_in_call": 0, "failed": {},
            "created_columns": [], "done": False, "stopped": None, "resume": None}


#: `test` / `publish` d'une recette `for_each` : la première ligne parente peut
#: légitimement ne rien rendre — l'épreuve en essaie jusqu'à trois, une page chacune.
ESSAI_PARENTS = 3
#: Une colonne remplie sur au moins cette part des lignes à la publication est SURVEILLÉE.
SEUIL_SURVEILLEE = 0.8
#: …et une page d'au moins tant de lignes où elle est vide partout arrête l'exécution.
PAGE_TEMOIN = 10
#: Les échecs qui tiennent à UNE ligne parente (son entrée) : elle est marquée
#: `failed:<code>` et l'exécution passe à la suivante. Tout autre échec (clé, crédits,
#: délai, limite de débit, panne) tient au compte ou au fournisseur : l'exécution s'arrête
#: et la ligne reste en attente.
ECHECS_DE_LIGNE = frozenset({"invalid_input", "not_found", "call_refused", "upstream_4xx"})
#: …dont celui-là ne dit RIEN de systémique (« ce SIREN n'existe pas ») : la ligne est
#: marquée aussitôt, hors disjoncteur — sinon trois sociétés inconnues d'affilée
#: arrêteraient chaque exécution sur les mêmes trois lignes.
HORS_DISJONCTEUR = frozenset({"not_found"})
#: …sauf quand plusieurs lignes d'affilée échouent pareil : c'est alors systémique (une
#: garde d'activation, un argument mal écrit). L'exécution s'arrête ; les lignes de la
#: série sont marquées `failed:<code>` et NOMMÉES au reçu (`failed_rows`) — sans quoi
#: chaque exécution buterait sur les mêmes, sans qu'on sache lesquelles.
DISJONCTEUR = 3


async def _outil(fastmcp, outil: str):
    """L'outil, s'il est appelable par une recette — refus nommé sinon. Jugé AVANT tout
    effet (ouverture du tableau, colonnes créées, clé déclarée)."""
    from ..tools import meta
    if namespace_of(outil) in contrat.NAMESPACES_INTERDITS:
        raise RecetteRefusee("tool_not_allowed",
                             f"`{outil}` is a platform tool: a recipe only calls connector tools.")
    if namespace_of(outil) in contrat.NAMESPACES_A_MODELE:
        raise RecetteRefusee("tool_not_allowed",
                             f"`{outil}` runs a model: a recipe never calls one.")
    tool = await meta.resoudre_outil(fastmcp, outil)
    if tool is None:
        raise RecetteRefusee("unknown_tool", f"Unknown tool `{outil}`.")
    # Lancée par un travail hébergé, une recette n'appelle que les outils de sa liste —
    # jugé ici, avant tout effet ; `executer_cible` le rejoue à chaque page.
    hors_liste = await run_in_threadpool(outils_du_travail.refus, outil)
    if hors_liste:
        raise RecetteRefusee(outils_du_travail.CODE, f"{hors_liste} Nothing was called.")
    if not lecture.en_lecture(tool):
        raise RecetteRefusee("recipe_tool_not_read_only",
                             f"`{outil}` is not declared read-only: a recipe only calls tools "
                             "that read from their provider, never one that could write "
                             "(send, post, create…). Nothing was called.")
    return tool


async def _appeler(tool, sub: Optional[str], outil: str, args: dict):
    from ..tools import meta
    return await meta.executer_cible(tool, sub, outil, outil, args)


def _arguments(corps: dict, params: dict, etat: dict, restant: int,
               row: Optional[dict] = None, reste_ligne: Optional[int] = None) -> dict:
    args = co.rendre(corps.get("arguments") or {}, co._portees(params, row))
    if row is not None:
        # Sous `for_each`, une case vide de la ligne n'envoie rien (jamais `null`).
        args = co.sans_vides(args)
    pag = corps["source"]["pagination"]
    if pag["type"] == "page":
        args[pag["param"]] = etat["p"] if etat["p"] is not None else pag.get("start", 0)
    elif pag["type"] == "cursor" and etat["c"]:
        args[pag["param"]] = etat["c"]
    if pag.get("size"):
        taille = pag["size"]
        # Réduire la DERNIÈRE page au reste du plafond n'est juste qu'au curseur : en
        # pagination par numéro, une taille réduite décale la fenêtre (page 1 de taille
        # 1 = l'élément 1, déjà lu) — la boucle s'arrête plutôt avant la page.
        if corps["units"] == "items" and pag["type"] == "cursor":
            taille = max(1, min(taille, restant))
        # `max_items_per_row` : ne pas payer une page entière pour n'en garder qu'une
        # partie. Même règle de fenêtre : au numéro, seule la PREMIÈRE page se réduit.
        if reste_ligne is not None and (pag["type"] == "cursor" or etat["p"] is None):
            taille = max(1, min(taille, reste_ligne))
        args[pag["size_param"]] = taille
    return args


def _page_entiere_hors_plafond(corps: dict, restant: int) -> bool:
    """En pagination par numéro, une page n'est jamais coupée : si elle peut dépasser le
    reste du plafond, on ne l'appelle pas."""
    pag = corps["source"]["pagination"]
    return corps["units"] == "items" and pag["type"] == "page" \
        and bool(pag.get("size")) and restant < pag["size"]


def _compter(recu: dict, corps: dict, issue, elements: list) -> None:
    """Le coût de la page : ce que l'outil a FACTURÉ (`quantity`), sinon les unités que
    la recette déclare. `units_basis` dit laquelle (`mixed` si les deux ont servi)."""
    if issue.quantity is not None:
        cout, base = issue.quantity, "billed"
    else:
        cout, base = (len(elements) if corps["units"] == "items" else 1), "declared"
    recu["units"] += cout
    recu["units_basis"] = base if recu["units_basis"] in (None, base) else "mixed"


def _compter_echec(recu: dict, issue) -> None:
    """Un appel REFUSÉ par le fournisseur compte dans la dépense : ce qu'il a facturé
    s'il le dit (`quantity`), sinon l'appel lui-même — un fournisseur qui facture la
    recherche ratée n'est pas plafonné autrement."""
    if issue.quantity is not None:
        cout, base = issue.quantity, "billed"
    else:
        cout, base = 1, "declared"
    recu["units"] += cout
    recu["units_basis"] = base if recu["units_basis"] in (None, base) else "mixed"


def _suite(corps: dict, payload: Any, elements: list, args: dict, etat: dict) -> bool:
    """Avance l'état vers la page suivante ; False = c'était la dernière."""
    pag = corps["source"]["pagination"]
    if pag["type"] == "none":
        return False
    if pag["type"] == "cursor":
        etat["c"] = co.lire(payload, pag["next"])
        return bool(etat["c"])
    if not elements or (pag.get("last") and co.lire(payload, pag["last"]) is True):
        return False
    if pag.get("size_param") and len(elements) < int(args.get(pag["size_param"]) or 0):
        return False
    etat["p"] = args[pag["param"]] + 1
    return True


def _arreter(recu: dict, code: str, **details) -> dict:
    recu["stopped"] = code
    recu.update(details)
    return recu


def surveillees(temoin: Optional[dict]) -> list[str]:
    """Les colonnes que la publication a vues remplies (`test_report.fill`) : celles-là
    ne doivent pas revenir vides sur toute une page — un fournisseur qui change la forme
    de sa réponse remplirait sinon le tableau de lignes creuses, sans rien dire."""
    temoin = temoin or {}
    # Une épreuve trop courte ne prouve rien : une colonne remplie sur 1 ligne sur 1
    # n'est pas « pleine », et la surveiller bloquerait le premier parent qui en manque.
    if int(temoin.get("rows_built") or 0) < PAGE_TEMOIN:
        return []
    fill = temoin.get("fill") or {}
    return sorted(c for c, v in fill.items()
                  if isinstance(v, (int, float)) and v >= SEUIL_SURVEILLEE)


def _derive(lignes: dict, colonnes: list[str]) -> list[str]:
    if len(lignes) < PAGE_TEMOIN:
        return []
    return [c for c in colonnes if all(co._vide(l.get(c)) for l in lignes.values())]


async def executer(corps: dict, params: dict, *, fastmcp, sub: Optional[str],
                   datastore: Any = None, reprise: Optional[str] = None,
                   ecrire: bool = True, pages_max: Optional[int] = None,
                   budget_s: float = BUDGET_S, temoin: Optional[dict] = None) -> dict:
    """Exécute une recette VALIDÉE (`contrat.valider`). `ecrire=False` = l'épreuve :
    les pages sont appelées (et facturées) mais rien n'est écrit ; le reçu porte le
    remplissage par colonne (`fill`). `temoin` = le rapport d'épreuve de la version
    publiée : ses colonnes pleines sont surveillées (`mapping_drift`)."""
    recu = _recu()
    etat = lire_reprise(reprise, corps)
    recu["units"] = etat["u"]
    outil = await _outil(fastmcp, corps["tool"])
    col_cle = corps["key"]["column"]
    mappees = list(corps["map"])
    remplissage = {c: 0 for c in mappees}
    fe = corps.get("for_each")
    tableau = parents = None
    # L'horloge court dès l'entrée : les lectures préalables (parents, listes de
    # correspondance) comptent dans le budget de l'appel.
    fin = time.monotonic() + budget_s
    try:
        # Comparées RÉSOLUES : un numéro et un nom désignent le même tableau.
        if fe and datastore is not None and await run_in_threadpool(
                ecriture.meme_tableau, datastore, fe["datastore"]):
            raise RecetteRefusee("for_each_same_table",
                                 "`for_each.datastore` is the table the recipe writes into: "
                                 "the parent rows must live in another table. Nothing was "
                                 "called.")
        if datastore is not None:
            tableau = await run_in_threadpool(ecriture.ouvrir, datastore, col_cle,
                                              ecrire=ecrire)
        elif ecrire:
            raise RecetteRefusee("missing_datastore", "`datastore` (the table number) is "
                                                      "required to run a recipe.")
        # Les listes de correspondance AVANT le tableau parent : `match_table_too_large`
        # tombe avant que la colonne d'état ne soit déclarée.
        ensembles = await run_in_threadpool(ecriture.charger_ensembles, corps["where"])
        if fe:
            parents = await run_in_threadpool(
                ecriture.ouvrir_parents, fe["datastore"], fe["status_column"], ecrire=ecrire,
                filtre=fe.get("filter"),
                requises=co.colonnes_citees(corps.get("arguments"), "row"))
        if ecrire:
            recu["created_columns"] = await run_in_threadpool(
                ecriture.creer_colonnes, tableau, mappees + list(corps["values"]) + [col_cle])
    except ecriture.TableauIndisponible as e:
        raise RecetteRefusee(e.code, str(e))
    a_surveiller = surveillees(temoin)
    exigees = co.colonnes_citees(corps.get("arguments"), "row") if fe else set()
    lim = corps["limits"]
    # `max_pages` borne CET appel (une reprise repart à zéro page) : le plafond de la
    # chaîne d'appels est celui de la dépense, porté par le jeton.
    bornes = [b for b in (pages_max, lim.get("max_pages")) if b]
    pages_max = min(bornes) if bornes else None
    if fe and not ecrire and pages_max is not None:
        pages_max = max(pages_max, ESSAI_PARENTS)
    # Les appels faits dans CET appel, réussis ou non : l'horloge se vérifie après le
    # premier, quel qu'il soit — dix parents refusés en 0,3 s chacun restent bornés.
    appels = {"n": 0}

    def _reprise() -> str:
        return _jeton({**etat, "u": recu["units"]})

    async def _tirer(row: Optional[dict]) -> tuple[Optional[str], int]:
        """Les pages d'UNE portée (la recette seule, ou une ligne parente). Rend `(arrêt,
        éléments retenus)` — `arrêt` None quand la portée est épuisée."""
        pris = 0
        pages_ici = 0
        cap = (fe or {}).get("max_items_per_row")
        while True:
            restant = lim["max_units"] - recu["units"]
            if restant <= 0 or _page_entiere_hors_plafond(corps, restant):
                _arreter(recu, "spend_cap")
                return "spend_cap", pris
            if pages_max is not None and recu["pages"] >= pages_max:
                _arreter(recu, "max_pages", resume=_reprise())
                return "max_pages", pris
            if appels["n"] and time.monotonic() >= fin:
                _arreter(recu, "time_budget", resume=_reprise())
                return "time_budget", pris
            if row is not None and co.exigees_vides(corps.get("arguments"),
                                                    co._portees(params, row), exigees):
                return "ligne:invalid_input", pris
            args = _arguments(corps, params, etat, restant, row,
                              reste_ligne=None if cap is None else cap - pris)
            appels["n"] += 1
            try:
                issue = await _appeler(outil, sub, corps["tool"], dict(args))
            except McpError as e:
                # Arguments refusés par le schéma de l'outil, garde d'activation : rien n'a
                # été appelé pour cette page.
                if fe:
                    return "ligne:call_refused", pris
                _arreter(recu, "call_refused", error=str(e.error.message)[:500])
                return "call_refused", pris
            if not issue.ok:
                _compter_echec(recu, issue)
            if not issue.ok and fe and not issue.retryable \
                    and issue.code in ECHECS_DE_LIGNE:
                return f"ligne:{issue.code}", pris
            if not issue.ok:
                # Sous `for_each`, le message du fournisseur peut citer l'argument refusé —
                # une valeur de la ligne : le reçu n'en porte que le code.
                _arreter(recu, issue.code or "tool_failed",
                         error=None if fe else issue.message,
                         retryable=issue.retryable, resume=_reprise())
                return recu["stopped"], pris
            if issue.retenu:
                _arreter(recu, "redaction_withheld",
                         error="The org's redaction policy withheld this tool's output.")
                return "redaction_withheld", pris
            payload = redaction.extract_payload(issue.result)
            elements = co.lire(payload, corps["source"]["items"]) if corps["source"]["items"] \
                else payload
            if not isinstance(elements, list):
                cles = _cles_sures(payload)[:20] if isinstance(payload, dict) else []
                _arreter(recu, "items_not_found",
                         error=f"`source.items` does not point to a list. Top-level keys "
                               f"of the result: {cles}")
                return "items_not_found", pris
            recu["pages"] += 1
            pages_ici += 1
            recu["items_seen"] += len(elements)
            _compter(recu, corps, issue, elements)
            plein = False
            if cap is not None and pris + len(elements) >= cap:
                recu["items_over_row_cap"] = recu.get("items_over_row_cap", 0) \
                    + pris + len(elements) - cap
                elements, plein = elements[:cap - pris], True
            pris += len(elements)
            lignes: dict[str, dict] = {}
            try:
                for el in elements:
                    if not co.garde(el, corps["where"], params, row=row,
                                    ensembles=ensembles):
                        recu["skipped_where"] += 1
                        continue
                    rangee = co.ligne(el, corps["map"], corps["values"], params, row=row)
                    cle = co.cle(el, corps["key"], rangee, params, row=row)
                    if cle is None:
                        recu["skipped_no_key"] += 1
                        continue
                    rangee[col_cle] = cle
                    if cle in lignes:
                        recu["duplicates_in_call"] += 1
                        continue
                    lignes[cle] = rangee
            except co.ValeurNonScalaire as e:
                # La recette pointe une liste là où elle attend une valeur : à corriger
                # dans la recette, pas ligne par ligne. Rien de la page n'est écrit.
                _arreter(recu, co.ValeurNonScalaire.code, error=str(e))
                return co.ValeurNonScalaire.code, pris
            derive = _derive(lignes, a_surveiller)
            if derive and fe:
                # Sous `for_each`, la dérive d'UN parent le marque (`failed:mapping_drift`)
                # et l'exécution passe au suivant ; une série d'affilée relève du
                # disjoncteur (systémique : la forme a changé pour tous).
                recu["drifted_columns"] = sorted(set(recu.get("drifted_columns") or [])
                                                 | set(derive))
                return "ligne:mapping_drift", pris
            if derive:
                # La page n'est PAS écrite : des lignes creuses ne valent pas mieux
                # qu'aucune, et la recette est à reprendre, pas l'appel.
                _arreter(recu, "mapping_drift", drifted_columns=derive,
                         error=f"Column(s) {derive} came back empty on all {len(lignes)} "
                               "rows of a page, though the published test filled them: "
                               "the tool's output changed shape. Fix the recipe's `map`.")
                return "mapping_drift", pris
            for rangee in lignes.values():
                for c in mappees:
                    if not co._vide(rangee.get(c)):
                        remplissage[c] += 1
            recu["rows_built"] += len(lignes)
            if ecrire and lignes:
                try:
                    await run_in_threadpool(ecriture.ecrire_page, tableau, lignes,
                                            on_existing=corps["on_existing"],
                                            colonnes_mappees=mappees, recu=recu)
                except ecriture.TableauIndisponible as e:
                    _arreter(recu, e.code, error=str(e))
                    return e.code, pris
            if plein or not _suite(corps, payload, elements, args, etat):
                etat["p"] = etat["c"] = None
                return None, pris
            if fe and not ecrire and pages_ici >= 1:
                etat["p"] = etat["c"] = None
                return None, pris

    # `finally` : l'épreuve rend son remplissage quelle que soit la sortie de la boucle.
    try:
        if not fe:
            arret, _ = await _tirer(None)
            recu["done"] = arret is None
            return recu
        await _parcourir(fe, parents, ecrire, etat, recu, _tirer, fin=fin, appels=appels)
    finally:
        if not ecrire:
            n = recu["rows_built"] or 1
            recu["fill"] = {c: round(v / n, 3) for c, v in remplissage.items()}
    return recu


async def _parcourir(fe: dict, parents, ecrire: bool, etat: dict, recu: dict,
                     tirer, *, fin: float, appels: dict) -> None:
    """Les lignes parentes EN ATTENTE (colonne d'état vide, filtre, entrées remplies),
    une à une : leur appel, puis leur état — `done` (des éléments), `empty` (aucun),
    `failed:<code>` (son entrée refusée : `ECHECS_DE_LIGNE`, ou sa page en dérive). Une
    ligne dont une entrée citée est vide n'est pas sélectionnée : jamais appelée ni
    marquée, elle est comptée au reçu (`skipped_empty_input`) quand l'exécution s'achève. Une ligne coupée par un
    plafond ou un échec du compte reste en attente — le jeton `resume` la reprend à sa
    page, une exécution neuve du début (les lignes déjà écrites sont reconnues par leur
    clé). Une série d'échecs identiques (`DISJONCTEUR`) arrête tout : ses lignes sont
    marquées et nommées au reçu (`failed_rows`)."""
    recu["parents"] = {"done": 0, "empty": 0, "failed": 0}
    vues: set = set()
    premiere = etat.get("r")
    serie: list[str] = []          # les lignes de la série d'échecs en cours, à marquer
    code_serie: Optional[str] = None

    async def _marquer(rid: str, statut: str) -> None:
        if not ecrire:
            return
        code = await run_in_threadpool(ecriture.marquer_parent, parents, rid, statut)
        if code:
            recu["failed"][code] = recu["failed"].get(code, 0) + 1

    async def _solder() -> None:
        nonlocal code_serie
        for rid in serie:
            await _marquer(rid, f"failed:{code_serie}")
        serie.clear()
        code_serie = None

    try:
        while True:
            faits = sum(recu["parents"].values())
            if not ecrire:
                place = ESSAI_PARENTS - faits
            elif fe.get("max_parents"):
                place = fe["max_parents"] - faits
            else:
                place = contrat.LOT_PARENTS
            if place <= 0:
                if ecrire:
                    _arreter(recu, "max_parents", resume=_jeton(
                        {"p": None, "c": None, "u": recu["units"], "r": None,
                         "h": etat.get("h")}))
                return
            if appels["n"] and time.monotonic() >= fin:
                _arreter(recu, "time_budget", resume=_jeton(
                    {"p": None, "c": None, "u": recu["units"], "r": None,
                     "h": etat.get("h")}))
                return
            try:
                lot = await run_in_threadpool(ecriture.parents_en_attente, parents,
                                              limite=place, premiere=premiere, vues=vues)
            except ecriture.TableauIndisponible as e:
                _arreter(recu, e.code, error=str(e))
                return
            premiere = None
            if not lot:
                # `done` dit que plus AUCUNE ligne sélectionnable n'attend ; celles qu'une
                # entrée vide écarte attendent encore, et le reçu les compte.
                try:
                    recu["skipped_empty_input"] = await run_in_threadpool(
                        ecriture.parents_aux_entrees_vides, parents)
                except ecriture.TableauIndisponible as e:
                    _arreter(recu, e.code, error=str(e))
                    return
                recu["done"] = True
                return
            for ligne in lot:
                rid = str(ligne["_id"])
                vues.add(rid)
                if etat.get("r") != rid:
                    etat["p"] = etat["c"] = None
                etat["r"] = rid
                arret, pris = await tirer(ecriture.portee_ligne(ligne))
                if arret is not None and arret.startswith("ligne:") \
                        and arret.split(":", 1)[1] in HORS_DISJONCTEUR:
                    etat.update(r=None, p=None, c=None)
                    await _solder()
                    recu["parents"]["failed"] += 1
                    await _marquer(rid, f"failed:{arret.split(':', 1)[1]}")
                    continue
                if arret is not None and arret.startswith("ligne:"):
                    code = arret.split(":", 1)[1]
                    etat.update(r=None, p=None, c=None)
                    if code != code_serie:
                        await _solder()
                        code_serie = code
                    serie.append(rid)
                    recu["parents"]["failed"] += 1
                    if len(serie) >= DISJONCTEUR:
                        recu["failed_rows"] = list(serie)
                        _arreter(recu, "repeated_failure", error=(
                            f"{DISJONCTEUR} parent rows in a row failed with `{code}`: "
                            "that looks systemic (an argument, the connector), not a bad "
                            f"row. They are marked `failed:{code}` and listed in "
                            "`failed_rows`; fix the cause, clear their status column, and "
                            "run again."))
                        await _solder()
                        return
                    continue
                if arret is not None:
                    return
                await _solder()
                etat["r"] = None
                statut = "done" if pris else "empty"
                recu["parents"][statut] += 1
                await _marquer(rid, statut)
                if not ecrire and recu["rows_built"]:
                    return
            if not ecrire:
                recu["done"] = True
                return
    finally:
        await _solder()


def _forme(obj: Any, prefixe: str, out: dict, profondeur: int) -> None:
    if profondeur > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            # Une clé qui est une VALEUR (e-mail, nom) ne remonte pas : `*` à sa place.
            k = k if isinstance(k, str) and _NOM_DE_CHAMP.match(k) else "*"
            _forme(v, f"{prefixe}.{k}" if prefixe else k, out, profondeur + 1)
        return
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        _forme(obj[0], f"{prefixe}[0]", out, profondeur + 1)
        return
    entree = out.setdefault(prefixe, {"type": type(obj).__name__, "filled": 0})
    if not co._vide(obj):
        entree["filled"] += 1
        entree["type"] = type(obj).__name__


async def echantillon(fastmcp, sub: Optional[str], outil: str, arguments: dict,
                      items: str = "") -> dict:
    """UN appel de l'outil, et la FORME de ses éléments : chaque chemin, son type,
    sur combien d'éléments il est rempli. Rien n'est écrit. L'appel est facturé comme
    tout appel de l'outil — passe la plus petite taille de page."""
    issue = await _appeler(await _outil(fastmcp, outil), sub, outil, dict(arguments or {}))
    if not issue.ok:
        raise RecetteRefusee(issue.code or "tool_failed", issue.message or "tool failed")
    if issue.retenu:
        raise RecetteRefusee("redaction_withheld",
                             "The org's redaction policy withheld this tool's output.")
    payload = redaction.extract_payload(issue.result)
    elements = co.lire(payload, items) if items else payload
    if not isinstance(elements, list):
        listes = _cles_sures(k for k, v in payload.items() if isinstance(v, list)) \
            if isinstance(payload, dict) else []
        return {"items_found": False, "list_paths": listes,
                "top_level_keys": _cles_sures(payload)[:30] if isinstance(payload, dict) else []}
    forme: dict = {}
    for el in elements:
        _forme(el, "", forme, 0)
    return {"items_found": True, "items": len(elements),
            "paths": [{"path": p, **v} for p, v in sorted(forme.items())]}
