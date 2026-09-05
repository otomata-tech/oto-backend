"""Le périmètre de réservation déclaré sur le TABLEAU — `lifecycle.claimable` (#517).

Sans lui, `data_claim_next` sert « la plus ancienne ligne dont le bail est libre ou
expiré » — TOUTE ligne du tableau. Mesuré le 29/08/2026 sur un fichier de 8 910
lignes : un jalon en cible 100 (`lot_test = jalon-100`) ; le harnais dicte le filtre
dans la prose de l'ordre et l'agent le recopie. À 5 % d'oubli, cinq fiches hors lot
par jalon — servies, traitées, écrites, payées. **Une contrainte demandée par la
prose n'est pas une contrainte.**

D'où une déclaration du tableau, à côté de `max_claims` :

    lifecycle: {…, claimable: {"lot_test": "jalon-100", "statut": "a_enrichir"}}

Même grammaire que `filter` (`{col: val}` ou `{col: {op: val}}`), validée à la POSE
par le même moteur que la lecture (`db.query`) — opérateurs whitelistés, colonnes
déclarées sous `strict`, refus nommé sinon. Effet, sur les deux réservations :

- le serveur ne sert JAMAIS une ligne hors de ce filtre, quel que soit le `filter`
  passé — celui de l'appelant s'y ajoute en ET : il resserre, jamais n'élargit ;
- la réservation CIBLÉE (REST `claim_row`) refuse une ligne hors périmètre en le
  nommant — sinon la porte de côté ;
- une réservation qui ne trouve rien le dit EN NOMMANT le périmètre : un `row: null`
  nu se lit comme une file vide, alors que c'est peut-être le filtre de l'appelant qui
  contredit la déclaration.

Ce module ne connaît que la grammaire des filtres. ⚠️ Il l'importe À L'APPEL :
`db.query` remonte vers `schema.py` (par `paths`), et `schema.py` importe ce module
pour décider — un import de tête ferait le cycle. `core.py` l'appelle pour servir,
les deux faces pour le dire.
"""
from __future__ import annotations

import re
from typing import Any, Optional

CLE = "claimable"

# Le premier segment d'un chemin de colonne : `contacts[0].email` → `contacts`.
_TETE = re.compile(r"[.\[]")


class RowOutsideClaimable(Exception):
    """Réservation CIBLÉE d'une ligne hors du périmètre déclaré (#517).

    Le périmètre borne ce que le tableau SERT : si `claim_next` ne le franchit
    jamais mais que `claim_row` le franchit sur demande, la déclaration n'est qu'une
    étiquette. Porte le périmètre, pour que la surface le nomme — et le geste qui
    l'élargit, qui est celui du schéma, pas celui de l'appel."""

    def __init__(self, row_id: Any, perimetre: dict):
        self.row_id = row_id
        self.perimetre = dict(perimetre)
        super().__init__(
            f"ligne « {row_id} » hors du périmètre de réservation déclaré "
            f"{perimetre_texte(perimetre)} — réservation refusée. Ce tableau ne sert "
            "que les lignes de ce périmètre (`lifecycle.claimable` de son schéma) ; "
            "l'élargir est un geste sur le schéma (data_patch_schema), jamais sur "
            "l'appel.")


def perimetre_of(lc: Optional[dict]) -> Optional[dict]:
    """Le périmètre déclaré sur un cycle de vie, ou None = non déclaré.

    Une valeur présente mais inutilisable LÈVE (même parti que `max_claims_of`) : la
    déclaration est refusée à la pose, donc une forme illisible ici ne peut venir que
    d'une écriture hors surface — et l'ignorer rouvrirait le tableau en silence,
    exactement le défaut que le périmètre existe pour fermer."""
    if not isinstance(lc, dict) or lc.get(CLE) is None:
        return None
    p = lc[CLE]
    if not isinstance(p, dict) or not p:
        raise ValueError(
            f"lifecycle.claimable doit être un objet non vide {{col: val}} "
            f"(déclaré : {p!r})")
    return dict(p)


def clauses(perimetre: Optional[dict]) -> list[dict]:
    """Le périmètre en clauses du moteur SQL — celles que le pick ajoute en ET
    DEVANT celles de l'appelant. `[]` sans déclaration : le pick reste ce qu'il
    était."""
    if not perimetre:
        return []
    from ..db.query import ds_filter_specs
    return ds_filter_specs(perimetre)


def erreurs(lc: dict, *, declared: set, strict: bool,
            status_key: Optional[str], states: set) -> list[str]:
    """Les refus à la POSE — une liste de messages actionnables, vide si tout va.

    Le périmètre est un filtre : il se valide comme un filtre, par le moteur qui le
    servira (`ds_filter_specs` + `_ds_filter_clauses`), jamais par une grammaire
    parallèle qui divergerait le jour où le moteur apprend un opérateur. S'y ajoutent
    ce qu'un filtre d'appel ne vérifie pas et qu'une déclaration doit : une clause
    INERTE (`in: []`) est refusée à la source depuis #353 ; une colonne inconnue sous `strict` ; un
    état du statut que le cycle de vie ne déclare pas — la file serait vide pour
    toujours, sans un mot."""
    if CLE not in lc or lc[CLE] is None:
        return []
    p = lc[CLE]
    if not isinstance(p, dict) or not p:
        return [f"lifecycle.claimable doit être un objet non vide {{col: val}} ou "
                f"{{col: {{op: val}}}} — la grammaire de `filter` (reçu {p!r})"]
    from ..db.query import _DS_META_TEXT_COLS, _DS_META_TS_COLS, _ds_filter_clauses
    out: list[str] = []
    try:
        specs = clauses(p)
        sql, _ = _ds_filter_clauses(specs)
    except ValueError as e:
        # ⚠️ #353 : c'est désormais PAR ICI que passe la clause inerte (`in: []`).
        # Elle était détectée en dessous, en comptant les fragments rendus — une
        # clause qui « s'évaporait » en laissait un de moins. Depuis que le filtre
        # vide LÈVE au lieu de disparaître, ce comptage ne peut plus rien voir, et
        # le refus qui remonte ici nomme la colonne et la sortie, ce que le
        # comptage ne pouvait pas faire.
        out.append(f"lifecycle.claimable: {e}")
    meta = set(_DS_META_TS_COLS) | set(_DS_META_TEXT_COLS)
    for col, val in p.items():
        tete = _TETE.split(str(col), 1)[0]
        if strict and tete not in declared and tete not in meta:
            out.append(
                f"lifecycle.claimable: colonne `{col}` non déclarée au schéma "
                "(strict) — déclare-la, ou retire-la du périmètre")
        if (status_key and col == status_key and states
                and not isinstance(val, dict) and str(val) not in states):
            out.append(
                f"lifecycle.claimable: `{col}` = {val!r} n'est pas un état déclaré "
                f"({', '.join(sorted(states))}) — la file serait vide pour toujours")
    return out


def perimetre_texte(perimetre: dict) -> str:
    """`{lot_test: jalon-100, statut: a_enrichir}` — le périmètre tel qu'une phrase
    le cite, opérateur compris quand il y en a un (`{posted_at: {gte: 2026-06}}`).
    Colonnes TRIÉES : le schéma relu de la base (jsonb) ne garde pas l'ordre de la
    pose, et une phrase qui change d'un appel à l'autre se lit comme deux périmètres."""
    def _val(v: Any) -> str:
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}: {_val(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(_val(x) for x in v) + "]"
        return str(v)
    return ("`{" + ", ".join(f"{k}: {_val(perimetre[k])}" for k in sorted(perimetre))
            + "}`")


def phrase_vide(perimetre: dict, filtre: Optional[dict]) -> str:
    """Rien servi, et il y a un périmètre : la phrase le NOMME (#517).

    Écrite ici, une fois, pour les deux faces — deux formules divergent. Quand
    l'appelant a passé un filtre, la phrase dit aussi comment il se combine : en ET.
    C'est le cas de la campagne qui cible un jalon et demande un autre lot — rien
    n'est servi, et sans cette phrase l'agent lirait « file vide »."""
    tete = f"aucune ligne libre dans le périmètre déclaré {perimetre_texte(perimetre)}"
    if filtre:
        return (f"{tete} — ton filtre {perimetre_texte(filtre)} s'y ajoute en ET : il "
                "resserre le périmètre, il ne l'élargit pas")
    return f"{tete} (`lifecycle.claimable` du tableau ; un `filter` s'y ajoute en ET)"
