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
from ..datastore.core import DatastoreReadOnly, RowLocked, RowValidationError
from ..datastore.errors import RevisionConflict

logger = logging.getLogger(__name__)

#: Lignes par écriture groupée. Un lot n'est pas atomique et s'arrête à la première
#: ligne refusée : au-delà de l'échec, on rejoue ligne à ligne.
LOT = 50


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
    from ..datastore import jetons
    from ..datastore.core import DatastoreNotFound
    # Un agent passe le NUMÉRO en entier (`284`) : la résolution attend du texte.
    datastore = str(datastore)
    try:
        store, adresse = pr.tableau(datastore, ecrire=ecrire)
        schema = store.get_schema(adresse) or {}
        declaree = store.declared_key(adresse)
    except jetons.JetonMalPlace as e:
        raise TableauIndisponible("invalid_datastore", str(e))
    except DatastoreNotFound:
        raise TableauIndisponible("datastore_not_found",
                                  f"Table `{datastore}` not found. Nothing was called.")
    except DatastoreReadOnly:
        raise TableauIndisponible("datastore_read_only",
                                  f"Table `{datastore}` is shared with you read-only. "
                                  "Nothing was called.")
    if declaree and declaree != colonne_cle:
        raise TableauIndisponible(
            "key_mismatch",
            f"The table's declared key is `{declaree}`, the recipe's key is "
            f"`{colonne_cle}`: they must be the same column. Nothing was called.")
    colonnes = {f["key"] for f in (schema.get("fields") or []) if f.get("key")}
    return Tableau(store, adresse, colonne_cle, colonnes, declare=bool(colonnes))


def colonnes_a_creer(t: Tableau, colonnes: list[str]) -> list[str]:
    """Les colonnes que la recette écrit et que le schéma DÉCLARÉ ne connaît pas. Un
    tableau sans schéma (libre) n'en déclare aucune : rien à créer."""
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
    if isinstance(e, RowLocked):
        return "row_locked"
    if isinstance(e, RevisionConflict):
        return "row_changed"
    if isinstance(e, DatastoreReadOnly):
        return "datastore_read_only"
    if isinstance(e, (RowValidationError, ValueError)):
        return "row_refused"
    return "write_failed"


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
