"""Pousser les lignes d'un tableau vers un connecteur, PAR RÉFÉRENCE.

Un outil qui crée un lead ou un contact prenait la donnée de la personne en
ARGUMENTS : nom, email, téléphone, profil LinkedIn traversaient l'appel d'outil,
une fois par ligne. Le client MCP les voit passer et peut les refuser — et un agent
qui pousse deux cents lignes les recopie deux cents fois dans son contexte. Ici
l'agent désigne des LIGNES (un tableau, des `row_ids` ou un filtre) ; le serveur les
lit, appelle le connecteur, écrit en retour l'identifiant et l'état sur chaque ligne,
et ne rend que des COMPTES et des CODES — jamais une valeur lue.

Ce module ne connaît aucun connecteur : il ouvre le lot, juge le bail de chaque
ligne, lit ses valeurs, écrit en retour et tient le reçu. Chaque outil
(`lemlist_push_rows`, `hubspot_push_rows`) ne porte que son appel au connecteur.

Les règles de la file de travail sont celles de `data_write`, par construction : la
lecture et l'écriture passent par le MÊME store (`make_store(sub)`, org de l'appel,
`_run_id` lu dans le contexte). Une ligne tenue par un autre run est ÉCARTÉE AVANT
l'appel au connecteur — sinon le lead serait créé chez lui et l'écriture en retour
refusée ici, et la ligne resterait « à pousser » pour un doublon au prochain tour.
Pour la même raison, le droit d'ÉCRIRE sur le tableau est vérifié avant tout appel.

⚠️ **Une ligne se choisit une fois.** Par filtre, le lot ne retient que les lignes
dont la colonne d'état est VIDE : un nouvel appel avec le même filtre prend la suite,
et une ligne déjà traitée — poussée, doublon ou en échec — n'est pas reprise. Pour
reprendre une ligne en échec, la nommer dans `row_ids` (ou vider son état).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError
from . import couches, identite, jetons
from .core import (
    DatastoreNotFound,
    DatastoreReadOnly,
    RowLocked,
    RowNotFound,
    RowValidationError,
    make_store,
)
from .errors import RevisionConflict, TableauParNom
from .outils import _current_run

logger = logging.getLogger(__name__)

#: Lignes par appel. Chaque ligne coûte au moins un appel au connecteur ; au-delà,
#: le reçu arriverait après le plafond d'un appel REST (45 s, `capabilities/tools_me.py`).
MAX_LIGNES = 50
#: Budget d'horloge d'un lot, vérifié AVANT chaque ligne : passé ce délai le reçu est
#: rendu PARTIEL (`stopped: "time_budget"`) plutôt que coupé sans reçu — l'agent
#: rejouerait tout, et le connecteur recevrait des doublons. Même raisonnement que
#: `tools/clay.py` : plafond REST − le pire appel amont − une marge.
BUDGET_S = 30.0


def refus(code: str, message: str, **data) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=f"Refus `{code}` : {message}",
                              data={"code": code, "retryable": False, **data}))


def valider_correspondance(mapping: Any, *, nom: str = "field_mapping") -> dict[str, str]:
    """`{champ du connecteur: colonne du tableau}`, non vide, chaînes seulement."""
    if not isinstance(mapping, dict) or not mapping:
        raise refus("push_rows_mapping", f"`{nom}` doit être un objet non vide "
                    "{champ du connecteur: colonne du tableau}.")
    mauvais = [k for k, v in mapping.items()
               if not (isinstance(k, str) and k and isinstance(v, str) and v)]
    if mauvais:
        raise refus("push_rows_mapping", f"`{nom}` : clés et valeurs doivent être des "
                    f"noms non vides ({len(mauvais)} entrée(s) fautive(s)).")
    return dict(mapping)


@dataclass
class Lot:
    """Les lignes choisies, et de quoi écrire sur elles."""
    store: Any
    adresse: str
    lignes: list[dict]
    #: `row_ids` demandés que le tableau n'a pas rendus.
    absentes: list[str]
    #: Le filtre complet du lot (celui de l'appelant + « état vide »), None en `row_ids`.
    filtre: Optional[dict]
    filtres: Optional[list]

    def identite(self) -> dict:
        return identite.numero(self.store.dernier_tableau)

    def restantes(self) -> Optional[int]:
        """Par filtre : les lignes qui attendent encore (état vide), celles de ce lot
        comprises si elles n'ont pas été écrites. None en mode `row_ids`."""
        if self.filtres is None:
            return None
        return self.store.count_rows(self.adresse, filter=self.filtre,
                                     filters=self.filtres)


def tableau(datastore: Any, *, ecrire: bool) -> tuple[Any, str]:
    """Le store de l'appelant et l'adresse du tableau, le droit vérifié AVANT tout appel
    au connecteur : ÉCRIRE si `ecrire` (la vérification même que fera `update_row` à
    l'écriture en retour), lire sinon.

    Lève `jetons.JetonMalPlace`, `DatastoreNotFound` et `DatastoreReadOnly` tels quels :
    chaque outil les dit à sa façon."""
    adresse, _ = jetons.resoudre(datastore, None,
                                 resoudre_slot=access.resolve_datastore_ref)
    store = make_store(access.current_user_sub_or_raise())
    store._resolve(adresse, write=ecrire)
    return store, adresse


def ouvrir(datastore: Any, *, row_ids: Optional[list], filter: Optional[dict],
           colonne_etat: Optional[str], limite: int) -> Lot:
    """Résout le tableau, vérifie le droit d'y ÉCRIRE, et lit le lot.

    Exactement un de `row_ids` / `filter`. `filter` a la grammaire de `data_rows`
    (`{}` = toutes les lignes) ; avec `colonne_etat`, le lot n'y retient que les
    lignes dont cette colonne est vide."""
    if (row_ids is None) == (filter is None):
        raise refus("push_rows_selection", "passe exactement un de `row_ids` (les lignes "
                    "nommées) ou `filter` (la grammaire de data_rows, `{}` = toutes).")
    if not isinstance(limite, int) or not 1 <= limite <= MAX_LIGNES:
        raise refus("push_rows_limit", f"`batch_size` va de 1 à {MAX_LIGNES}.")
    ids: list[str] = []
    if row_ids is not None:
        if not isinstance(row_ids, list) or not row_ids:
            raise refus("push_rows_selection", "`row_ids` doit être une liste non vide.")
        ids = [str(r) for r in row_ids]
        if len(ids) > MAX_LIGNES:
            raise refus("push_rows_limit", f"au plus {MAX_LIGNES} `row_ids` par appel "
                        f"({len(ids)} reçus) — découpe le lot.")
    if filter is not None and not isinstance(filter, dict):
        raise refus("push_rows_selection", "`filter` doit être un objet (grammaire de "
                    "data_rows).")

    filtre = filtres = None
    try:
        store, adresse = tableau(datastore, ecrire=True)
        if ids:
            page = store.cursor_rows(adresse, filter={"_id": {"in": ids}}, limit=len(ids))
        else:
            filtre = filter or None
            filtres = ([{"field": colonne_etat, "op": "empty", "value": True}]
                       if colonne_etat else [])
            page = store.cursor_rows(adresse, filter=filtre, filters=filtres or None,
                                     limit=limite)
    except jetons.JetonMalPlace as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    except TableauParNom as e:
        raise refus("datastore_by_name", f"{e.indice} Rien n'a été envoyé.")
    except DatastoreNotFound as e:
        indice = getattr(e, "indice", None)
        raise refus("datastore_not_found", f"tableau `{datastore}` inconnu"
                    + (f" — {indice}" if indice else "") + ". Rien n'a été envoyé.")
    except DatastoreReadOnly:
        raise refus("datastore_read_only", f"le tableau `{datastore}` t'est partagé en "
                    "lecture seule : l'identifiant et l'état ne pourraient pas y être "
                    "écrits en retour. Rien n'a été envoyé.")
    except ValueError as e:
        raise refus("push_rows_filter", f"{e} Rien n'a été envoyé.")
    lignes = list(page.get("rows") or [])
    if ids:
        # L'ordre de l'appelant, pas celui du tableau : un reçu se relit contre la
        # liste qu'on a envoyée.
        par_id = {str(r.get("_id")): r for r in lignes}
        lignes = [par_id[i] for i in ids if i in par_id]
        absentes = [i for i in ids if i not in par_id]
    else:
        absentes = []
    return Lot(store, adresse, lignes, absentes, filtre, filtres)


def colonnes_inconnues(lot: Lot, colonnes) -> list[str]:
    """Les colonnes nommées que le tableau ne connaît pas — une faute de frappe dans la
    correspondance, qui pousserait des fiches à moitié vides sans rien dire.

    Une ligne servie porte chaque colonne DÉCLARÉE (à `None` si vide) ; une colonne
    libre, elle, n'apparaît que sur les lignes qui ont une valeur. Donc : ce que le lot
    montre est connu ; le reste est demandé au tableau ENTIER (`datastore_has_column`) —
    une colonne libre vide sur tout ce lot n'est pas une faute. Jugé sur un lot vide,
    rien n'est inconnu : on ne refuse pas ce qu'on ne peut pas voir."""
    from .. import db
    if not lot.lignes:
        return []
    vues = set().union(*(set(ligne) for ligne in lot.lignes))
    ns_id = (lot.store.dernier_tableau or {}).get("ns_id")
    return sorted({c for c in colonnes if c not in vues
                   and not (ns_id is not None and db.datastore_has_column(int(ns_id), c))})


def valeur(ligne: dict, colonne: str) -> Any:
    """La valeur d'une case, `None` si elle est vide — `"@empty"` (« cherché, rien
    trouvé ») compris : ce n'est pas une donnée à pousser."""
    v = couches.unwrap(ligne.get(colonne))
    if v is None or v == couches.VIDE_DELIBERE:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    if isinstance(v, (list, dict)) and not v:
        return None
    return v


def tenue_ailleurs(ligne: dict) -> bool:
    """La ligne est-elle réservée par un AUTRE que cet appel ?

    Même règle que la garde d'écriture (`file_de_travail._lease_guard`) : un bail
    n'est servi que s'il est actif, et il ne laisse passer que le run qui le tient.
    Un bail posé sans run bloque tout le monde jusqu'à son échéance."""
    if not (ligne.get("_claimed_by") or ligne.get("_claimed_until")):
        return False
    run = _current_run()
    return not (run and ligne.get("_claimed_run") == run)


def ecrire(lot: Lot, row_id: str, patch: dict) -> Optional[str]:
    """Écrit `patch` sur une ligne du lot. Rend None, ou le CODE du refus."""
    return ecrire_ligne(lot.store, lot.adresse, row_id, patch)


def code_du_refus(e: Exception) -> Optional[str]:
    """Le CODE d'un refus d'écriture sur une ligne, ou None si l'exception n'en est pas
    un (elle doit alors monter). Le code seulement, jamais le texte : il peut citer une
    valeur de la ligne. Seul classement des refus d'écriture par ligne — `recipes/
    ecriture` le lit aussi."""
    if isinstance(e, RowLocked):
        return "row_locked"
    if isinstance(e, RevisionConflict):
        return "row_changed"
    if isinstance(e, RowNotFound):
        return "row_not_found"
    if isinstance(e, DatastoreReadOnly):
        return "datastore_read_only"
    if isinstance(e, (RowValidationError, ValueError)):
        return "writeback_refused"
    return None


def ecrire_ligne(store: Any, adresse: str, row_id: str, patch: dict, *,
                 expected_revision: Any = None) -> Optional[str]:
    """Écrit `patch` sur la ligne. Rend None, ou le CODE du refus (`code_du_refus`).

    Avec `expected_revision`, une ligne changée depuis la lecture n'est pas écrasée
    (`row_changed`)."""
    try:
        if expected_revision is None:
            store.update_row(adresse, row_id, patch)
        else:
            store.update_row(adresse, row_id, patch, expected_revision=expected_revision)
        return None
    except Exception as e:  # noqa: BLE001 — classé ici ; un inconnu remonte tel quel
        code = code_du_refus(e)
        if code is None:
            raise
        if code == "writeback_refused":
            # Journalisé sans le message (il peut citer une valeur de la ligne).
            logger.warning("écriture en retour refusée sur une ligne de %s : %s",
                           adresse, type(e).__name__)
        return code


@dataclass
class Recu:
    """Le reçu d'un lot : des COMPTES et des CODES, jamais une valeur lue.

    `errors` porte `{row_id, code}` — l'identifiant de LIGNE du tableau, pas une
    donnée de la personne."""
    comptes: dict[str, int] = field(default_factory=dict)
    erreurs: list[dict] = field(default_factory=list)
    arret: Optional[str] = None
    _fin: float = field(default_factory=lambda: time.monotonic() + BUDGET_S)

    def compter(self, cle: str, n: int = 1) -> None:
        self.comptes[cle] = self.comptes.get(cle, 0) + n

    def echec(self, row_id: str, code: str) -> None:
        self.compter("failed")
        self.erreurs.append({"row_id": str(row_id), "code": code})

    def ecarter(self, row_id: str, code: str) -> None:
        """Une ligne laissée de côté SANS appel au connecteur (bail, identité
        manquante…) : comptée par motif, et nommée."""
        self.compter(f"skipped_{code}")
        self.erreurs.append({"row_id": str(row_id), "code": code})

    def budget_epuise(self) -> bool:
        return time.monotonic() >= self._fin

    def rendre(self, lot: Lot, *, selectionnees: int, dry_run: bool,
               traitees: int, **extra) -> dict:
        out: dict = {**lot.identite(), "dry_run": dry_run, "selected": selectionnees,
                     **dict(sorted(self.comptes.items())), "errors": self.erreurs}
        for i in lot.absentes:
            out["errors"].append({"row_id": i, "code": "row_not_found"})
        restantes = lot.restantes()
        if restantes is None:
            restantes = selectionnees - traitees
        out["remaining"] = restantes
        out["stopped"] = self.arret
        out.update(extra)
        return out
