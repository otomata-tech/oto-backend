"""L'avertissement : ces clés de schéma ne sont lues par personne (oto#56, signal 658).

Le validateur acceptait n'importe quel attribut sur une colonne. `readonly` passe,
`editable` passe, `zorglub` passe — aucune n'est refusée, aucune n'est signalée.

Le cas fondateur est un agent qui pose `readonly: true` **et** `editable: true` en
espérant que le second rouvre le premier pour un humain, et qui le découvre en comparant
deux messages de refus strictement identiques. ⚠️ **Le cas grave est l'autre** : qui
écrit `read_only` au lieu de `readonly` croit avoir verrouillé sa colonne et n'a rien
verrouillé — la faute de frappe est silencieuse **et** elle désarme le cran.

## Avertissement, jamais refus

Refuser durcirait un contrat déjà servi (ADR 0019/0050 : *un contrat servi ne se durcit
pas en place, il se double*) et casserait à la première écriture tous les schémas
existants qui portent déjà des clés mortes. Même régime que `digest_warning`,
`diagram_warning` et `retrait_warning` : la pose réussit, l'auteur reçoit le signal.

## La référence est la DÉCLARATION, pas ce que le validateur lit

Première forme de ce lot, écartée par la mesure : dériver la liste en observant le
validateur. Le schéma n'est pas seulement validé, il est **servi** — cinq attributs
vivants n'existent que pour le front (`label`, lu 40 fois côté dashboard, `help`,
`placeholder`, `hint`, `description`). L'avertissement aurait crié « `label` n'est lue
par personne » sur presque tous les tableaux. **Un faux positif dans un signal de
qualité est pire que pas de signal** : on apprend à l'ignorer, et il ne sert plus le jour
où il a raison.

La référence est donc `schema_keys.RECONNUES`, où chaque clé porte son lecteur.
"""
from __future__ import annotations

from typing import Any, Iterable

from .schema_keys import RECONNUES


def inconnues(fields: Iterable) -> dict[str, list[str]]:
    """`{colonne: [clés que personne ne lit]}`. Vide = rien à signaler.

    Les couches (`colonne.comment`) portent les mêmes attributs que leur colonne : on
    les traite pareil, et c'est le validateur qui refuse celles qui n'ont de sens que
    sur une colonne."""
    out: dict[str, list[str]] = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        mortes = sorted(k for k in f if isinstance(k, str) and k not in RECONNUES)
        if mortes:
            out[str(f.get("key") or "?")] = mortes
    return out


def check(schema: Any) -> dict:
    """Check croisé à la pose, dans la forme des autres (`digest_check`,
    `retrait_check`) : la clé est TOUJOURS présente, `None` = rien à signaler.
    Best-effort — un check ne casse jamais une écriture."""
    try:
        if not isinstance(schema, dict):
            return {"unknown_keys_warning": None}
        trouvees = inconnues(schema.get("fields") or [])
        if not trouvees:
            return {"unknown_keys_warning": None}
        detail = " ; ".join(
            f"`{col}` : {', '.join('`' + k + '`' for k in cles)}"
            for col, cles in sorted(trouvees.items()))
        return {"unknown_keys_warning": (
            f"ces clés ne sont lues par PERSONNE et n'ont donc aucun effet — {detail}. "
            "Le schéma est bien posé, mais ce qu'elles devaient garder n'est pas gardé : "
            "une faute de frappe sur un attribut (`read_only` pour `readonly`) désarme "
            "le cran sans rien dire. La liste des attributs reconnus, avec qui lit "
            "chacun, est servie sur `GET /api/datastore/schema/keys`.")}
    # noqa: SILENT — contrôle de forme optionnel : pas d'avertissement plutôt qu'un faux
    except Exception:  # noqa: BLE001 — cf. `digest_check`
        return {"unknown_keys_warning": None}
