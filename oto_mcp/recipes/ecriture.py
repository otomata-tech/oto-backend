"""Écrire une page de lignes dans le tableau d'une recette — SYNCHRONE, hors boucle.

Le moteur l'appelle par `run_in_threadpool`. Même store que `data_write`
(`make_store(sub)`, org de l'appel, `_run_id` lu du contexte) : propriétaire,
historique et bail sont ceux de toute écriture d'agent.

**Une ligne existante n'est pas touchée par défaut** (`on_existing="skip"`) : une
recette qui repasse sur une société ne doit pas ramener à `sourced` une ligne déjà
validée. `update` n'écrit que les colonnes de la correspondance — jamais les valeurs
fixes, jamais un statut.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..datastore import par_reference as pr
from ..datastore.core import DatastoreReadOnly
from ..db import lecture_bornee

logger = logging.getLogger(__name__)

#: Lignes par écriture groupée (`par_reference.MAX_LIGNES`). Un lot n'est pas atomique
#: et s'arrête à la première ligne refusée : au-delà de l'échec, on rejoue ligne à ligne.
LOT = pr.MAX_LIGNES
#: Borne d'une lecture de tableau d'une recette (parents en attente, listes de
#: correspondance) : celle des lectures d'agrégat — au-delà, la lecture tient une
#: connexion d'une réserve partagée pour une réponse que personne n'attend plus.
DUREE_LECTURE_MS = lecture_bornee.DUREE_MAX_MS


class TableauIndisponible(Exception):
    """Le tableau ne peut pas recevoir la recette (introuvable, lecture seule, clé
    déclarée différente). `code` est le refus nommé rendu à l'appelant."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Tableau:
    store: Any
    adresse: str
    cle: str
    colonnes: set = field(default_factory=set)
    declare: bool = False


def ouvrir(datastore: Any, colonne_cle: str, *, ecrire: bool) -> Tableau:
    """Le tableau, le droit vérifié AVANT tout appel au connecteur, et sa clé.

    Une clé métier DÉCLARÉE différente de celle de la recette est refusée : l'écriture
    groupée dédoublonne sur la clé déclarée, et deux clés diraient deux choses."""
    # Un agent passe le NUMÉRO en entier (`284`) : la résolution attend du texte.
    datastore = str(datastore)
    store, adresse, schema = _ouvrir_tableau(datastore, ecrire=ecrire)
    declaree = store.declared_key(adresse)
    if declaree and declaree != colonne_cle:
        raise TableauIndisponible(
            "key_mismatch",
            f"The table's declared key is `{declaree}`, the recipe's key is "
            f"`{colonne_cle}`: they must be the same column. Nothing was called.")
    colonnes = {f["key"] for f in (schema.get("fields") or []) if f.get("key")}
    if not declaree and ecrire:
        colonnes |= _declarer_la_cle(store, adresse, datastore, colonne_cle, colonnes)
    # oto#124 : un tableau LIBRE déclare aussi ses colonnes — une colonne non déclarée
    # est refusée à l'écriture sur tous les tableaux, sans schéma compris.
    return Tableau(store, adresse, colonne_cle, colonnes, declare=ecrire)


def _declarer_la_cle(store: Any, adresse: str, datastore: str, colonne_cle: str,
                     colonnes: set) -> set:
    """Un tableau SANS clé métier déclarée reçoit celle de la recette, avant la première
    écriture. Sans elle, l'existant se lit puis s'écrit en deux temps : deux exécutions
    concurrentes écriraient deux fois la même ligne. Déclarée, l'unicité est tenue par la
    base (index unique de `set_schema`). Rend les colonnes ajoutées au schéma.

    Un tableau dont des lignes portent DÉJÀ deux fois la même valeur de clé ne peut pas la
    recevoir : refus nommé, rien n'est appelé."""
    champ = [] if colonne_cle in colonnes or not colonnes \
        else [{"key": colonne_cle, "type": "text"}]
    try:
        store.patch_schema(adresse, fields=champ or None, key=colonne_cle)
    except ValueError as e:  # `SchemaDefinitionError` : doublons, clé refusée
        logger.warning("recette : clé métier non déclarable sur %s : %s", adresse,
                       type(e).__name__)
        raise TableauIndisponible(
            "key_not_declarable",
            f"Table `{datastore}` has no declared key and `{colonne_cle}` cannot become "
            "it (existing rows repeat a value, or the schema refuses it): declare it "
            "yourself, or clean the duplicates. Nothing was called.")
    return {colonne_cle} if champ else set()


def colonnes_a_creer(t: Tableau, colonnes: list[str]) -> list[str]:
    """Les colonnes que la recette écrit et que le schéma DÉCLARÉ ne connaît pas —
    tableau libre compris (oto#124 : une colonne non déclarée est refusée partout).
    Rien hors écriture (l'épreuve n'écrit pas)."""
    if not t.declare:
        return []
    return [c for c in dict.fromkeys(colonnes) if c not in t.colonnes]


def creer_colonnes(t: Tableau, colonnes: list[str]) -> list[str]:
    """Ajoute les colonnes manquantes, en texte. Les existantes ne sont jamais
    retouchées (`patch_schema` fusionne par clé)."""
    neuves = colonnes_a_creer(t, colonnes)
    if neuves:
        t.store.patch_schema(t.adresse, fields=[{"key": c, "type": "text"} for c in neuves])
        t.colonnes.update(neuves)
    return neuves


def _code(e: Exception) -> str:
    """Le classement de `par_reference` ; ce qu'il ne connaît pas est `write_failed`."""
    return pr.code_du_refus(e) or "write_failed"


def _existantes(t: Tableau, cles: list[str]) -> dict[str, str]:
    """`{valeur de clé: _id}` des lignes déjà là, en une lecture."""
    if not cles:
        return {}
    page = t.store.cursor_rows(t.adresse, filter={t.cle: {"in": cles}},
                               fields=[t.cle], limit=len(cles))
    return {str(pr.valeur(r, t.cle)): str(r["_id"]) for r in page.get("rows") or []
            if pr.valeur(r, t.cle) is not None}


def ecrire_page(t: Tableau, lignes: dict[str, dict], *, on_existing: str,
                colonnes_mappees: list[str], recu: dict) -> None:
    """Écrit les lignes d'une page, indexées par leur valeur de clé. Compte dans
    `recu` (`written`, `updated`, `existing_left_untouched`, `failed`)."""
    deja = _existantes(t, list(lignes))
    neuves = [l for k, l in lignes.items() if k not in deja]
    for k, rid in deja.items():
        if on_existing != "update":
            recu["existing_left_untouched"] += 1
            continue
        patch = {c: lignes[k].get(c) for c in colonnes_mappees
                 if lignes[k].get(c) is not None and c != t.cle}
        code = pr.ecrire_ligne(t.store, t.adresse, rid, patch) if patch else None
        if code:
            recu["failed"][code] = recu["failed"].get(code, 0) + 1
        else:
            recu["updated"] += 1
    for i in range(0, len(neuves), LOT):
        lot = neuves[i:i + LOT]
        try:
            t.store.write_rows(t.adresse, lot)
            recu["written"] += len(lot)
            continue
        except DatastoreReadOnly:
            raise TableauIndisponible("datastore_read_only",
                                      "The table became read-only during the run.")
        except Exception as e:  # le lot s'arrête à la 1re refusée : rejouer une à une
            logger.warning("recette : lot refusé sur %s (%s), reprise ligne à ligne",
                           t.adresse, type(e).__name__)
        # Les lignes d'AVANT le refus sont écrites : les relire, pour ne rejouer que
        # les autres — rejouer une ligne déjà là la fusionnerait avec elle-même.
        passees = _existantes(t, [str(l.get(t.cle)) for l in lot])
        recu["written"] += sum(1 for l in lot if str(l.get(t.cle)) in passees)
        for ligne in (l for l in lot if str(l.get(t.cle)) not in passees):
            code = _une(t, ligne)
            if code:
                recu["failed"][code] = recu["failed"].get(code, 0) + 1
            else:
                recu["written"] += 1


def _une(t: Tableau, ligne: dict) -> Optional[str]:
    """Écrit UNE ligne neuve. Rend None ou le code du refus — jamais son texte, qui
    pourrait citer une valeur de la ligne."""
    try:
        t.store.write_rows(t.adresse, [ligne])
        return None
    except Exception as e:  # journalisé sans le message ; le code va au reçu
        logger.warning("recette : ligne refusée sur %s : %s", t.adresse, type(e).__name__)
        return _code(e)


# ── `for_each` : les lignes PARENTES qui déclenchent les appels ─────────────────
#: Lignes parentes lues par requête : la boucle relit les EN ATTENTE après chaque lot,
#: leur état écrit les en retire.
LOT_PARENTS = 50
#: Au plus tant de lignes lues par requête : les lignes déjà vues dans l'appel (état non
#: écrit) s'y ajoutent, sans quoi la sélection rendrait vide et l'exécution se dirait
#: finie, des lignes encore en attente.
MAX_LUES = 1_000


@dataclass
class Parents:
    store: Any
    adresse: str
    etat: str
    filtre: Optional[dict] = None
    #: Les colonnes que les arguments citent : une ligne où l'une est vide n'est pas
    #: sélectionnée — l'appel serait payé pour rien, et la ligne reviendrait en tête de
    #: chaque exécution. Elle reste en attente jusqu'à ce qu'on la remplisse.
    requises: tuple = ()

    def clauses(self) -> list[dict]:
        return [{"field": self.etat, "op": "empty", "value": True}] + [
            {"field": c, "op": "not_empty", "value": True} for c in self.requises]


def _ouvrir_tableau(datastore: Any, *, ecrire: bool) -> tuple[Any, str, dict]:
    """Le store, l'adresse et le schéma d'un tableau, le droit vérifié (écrire ou lire)
    AVANT tout appel au connecteur ; chaque refus nommé."""
    from ..datastore import jetons
    from ..datastore.core import DatastoreNotFound
    from ..datastore.errors import TableauParNom
    datastore = str(datastore)
    try:
        store, adresse = pr.tableau(datastore, ecrire=ecrire)
        return store, adresse, store.get_schema(adresse) or {}
    except jetons.JetonMalPlace as e:
        raise TableauIndisponible("invalid_datastore", str(e))
    except TableauParNom as e:
        raise TableauIndisponible("datastore_by_name", f"{e.indice} Nothing was called.")
    except DatastoreNotFound:
        raise TableauIndisponible("datastore_not_found",
                                  f"Table `{datastore}` not found. Nothing was called.")
    except DatastoreReadOnly:
        raise TableauIndisponible("datastore_read_only",
                                  f"Table `{datastore}` is shared with you read-only. "
                                  "Nothing was called.")


def meme_tableau(cible: Any, parent: Any) -> bool:
    """La cible et le tableau parent sont-ils le MÊME tableau, une fois résolus ? Un
    numéro et un nom désignent le même : comparer les chaînes laisserait passer l'un
    sous l'autre. Rien n'est écrit."""
    store, a, _ = _ouvrir_tableau(cible, ecrire=False)
    _, b, _ = _ouvrir_tableau(parent, ecrire=False)
    # L'adresse est l'écho de ce qu'on a passé (nom ou numéro) : on compare le NUMÉRO.
    return store._resolve(a) == store._resolve(b)


def _lu_borne(objet: str, lire):
    """Une lecture de tableau sous `statement_timeout` : dépassée, refus NOMMÉ plutôt
    qu'une connexion tenue des minutes."""
    try:
        with lecture_bornee.lectures_bornees(objet, DUREE_LECTURE_MS):
            return lire()
    except lecture_bornee.LectureTropLongue as e:
        raise TableauIndisponible("table_read_timeout", str(e))


def ouvrir_parents(datastore: Any, colonne_etat: str, *, ecrire: bool,
                   filtre: Optional[dict] = None, requises=()) -> Parents:
    """Le tableau parent, le droit d'y ÉCRIRE vérifié avant tout appel quand on écrit :
    l'état de chaque ligne y est écrit en retour, et c'est lui qui fait qu'une exécution
    suivante ne repaie pas une ligne faite. Le filtre est éprouvé ici, AVANT que la
    colonne d'état ne soit déclarée (si elle manque) : refusé, rien n'a changé."""
    store, adresse, schema = _ouvrir_tableau(datastore, ecrire=ecrire)
    colonnes = {f["key"] for f in (schema.get("fields") or []) if f.get("key")}
    p = Parents(store, adresse, colonne_etat, filtre or None, tuple(sorted(requises)))
    # Éprouvé sans la clause d'état : sa colonne n'est peut-être pas encore déclarée.
    clauses = [c for c in p.clauses() if c["field"] != colonne_etat]
    try:
        _lu_borne("lignes parentes d'une recette",
                  lambda: store.cursor_rows(adresse, filter=p.filtre, filters=clauses,
                                            limit=1))
    except ValueError as e:
        raise TableauIndisponible("invalid_filter", f"`for_each.filter`: {e} Nothing was "
                                                    "called.")
    if ecrire and colonne_etat not in colonnes:
        store.patch_schema(adresse, fields=[{"key": colonne_etat, "type": "text"}])
    return p


def parents_en_attente(p: Parents, *, limite: int, premiere: Optional[str],
                       vues: set) -> list[dict]:
    """Les prochaines lignes parentes dont l'état est vide — `premiere` (la ligne d'une
    reprise) d'abord, si elle attend encore. Une ligne déjà vue dans cet appel n'est pas
    rendue deux fois (son état n'a pas pu s'écrire) : sans cela, la boucle tournerait."""
    out: list[dict] = []
    if premiere:
        page = _lu_borne("lignes parentes d'une recette", lambda: p.store.cursor_rows(
            p.adresse, filter={**(p.filtre or {}), "_id": {"in": [premiere]}},
            filters=p.clauses(), limit=1))
        out += page.get("rows") or []
    page = _lu_borne("lignes parentes d'une recette", lambda: p.store.cursor_rows(
        p.adresse, filter=p.filtre, filters=p.clauses(),
        limit=min(MAX_LUES, LOT_PARENTS + len(vues) + len(out))))
    for r in page.get("rows") or []:
        if len(out) >= limite:
            break
        rid = str(r["_id"])
        if rid not in vues and all(str(o["_id"]) != rid for o in out):
            out.append(r)
    return out[:limite]


def parents_aux_entrees_vides(p: Parents) -> int:
    """Les lignes en attente (état vide, filtre) qu'une entrée vide écarte de la
    sélection (`requises`) : jamais appelées, jamais marquées. Comptées pour que le reçu
    le dise — sans cela une exécution `done` les laisse en attente sans un mot."""
    if not p.requises:
        return 0
    attente = [{"field": p.etat, "op": "empty", "value": True}]

    def _compter() -> int:
        toutes = p.store.count_rows(p.adresse, filter=p.filtre, filters=attente)
        pleines = p.store.count_rows(p.adresse, filter=p.filtre, filters=p.clauses())
        return max(0, toutes - pleines)
    return _lu_borne("lignes parentes d'une recette", _compter)


def portee_ligne(ligne: dict) -> dict:
    """La portée `row` d'une ligne parente : ses valeurs nues (couches retirées), et
    son `_id`."""
    out = {k: pr.valeur(ligne, k) for k in ligne if not str(k).startswith("_")}
    out["_id"] = str(ligne["_id"])
    return out


def marquer_parent(p: Parents, row_id: str, statut: str) -> Optional[str]:
    """Écrit l'état d'une ligne parente. Rend None ou le code du refus."""
    return pr.ecrire_ligne(p.store, p.adresse, row_id, {p.etat: statut})


# ── `where … in_table / not_in_table` ────────────────────────────────────────
#: Au-delà, ce n'est plus une liste d'exclusion qu'on charge en mémoire.
MAX_VALEURS_TABLE = 50_000
_PAGE_TABLE = 1_000


def charger_ensembles(clauses: list[dict]) -> dict[int, set]:
    """Pour chaque clause `in_table` / `not_in_table`, les valeurs NORMALISÉES de sa
    colonne — lues une fois par exécution, jamais une requête par élément, chaque page
    sous `statement_timeout`. Une case à plusieurs valeurs (liste) compte chacune."""
    from . import correspondance as co
    out: dict[int, set] = {}
    for i, c in enumerate(clauses or []):
        if c.get("op") not in ("in_table", "not_in_table"):
            continue
        store, adresse, _ = _ouvrir_tableau(c["table"], ecrire=False)
        valeurs: set = set()
        curseur, lues = None, 0
        while True:
            page = _lu_borne(f"liste de correspondance `{c['table']}`",
                             lambda: store.cursor_rows(
                                 adresse, fields=[c["column"]], limit=_PAGE_TABLE,
                                 cursor=curseur,
                                 filters=[{"field": c["column"], "op": "not_empty",
                                           "value": True}]))
            lignes = page.get("rows") or []
            lues += len(lignes)
            if lues > MAX_VALEURS_TABLE:
                raise TableauIndisponible(
                    "match_table_too_large",
                    f"Table `{c['table']}` holds more than {MAX_VALEURS_TABLE} values in "
                    f"`{c['column']}`: too many to match against. Nothing was called.")
            for r in lignes:
                brute = pr.valeur(r, c["column"])
                for x in (brute if isinstance(brute, list) else [brute]):
                    try:
                        v = co.normaliser(x, c.get("normalize"))
                    except co.ValeurNonScalaire as e:
                        raise TableauIndisponible(
                            co.ValeurNonScalaire.code,
                            f"Table `{c['table']}`, column `{c['column']}`: {e} Nothing "
                            "was called.")
                    if v is not None:
                        valeurs.add(v)
            curseur = page.get("next_cursor")
            if not curseur:
                break
        out[i] = valeurs
    return out
