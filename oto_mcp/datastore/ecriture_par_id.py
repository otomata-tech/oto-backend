"""PATCHER une ligne par son `id` — sous le verrou de ligne, avec précondition de révision.

Sorti d'`ecriture.py` le 12/09/2026 (le fichier frôlait la limite de taille), dans le
lot qui a changé son corps. Un mixin que `DatastorePg` compose, comme ses voisins.

**Le défaut fermé, mesuré sur l'arbre servi avec une base jetable.** Le patch par `id`
— REST `PATCH …/rows/{id}`, `data_write(id=…)`, et `append_row` avec `_id` qui s'y
promeut — lisait la ligne dans une connexion du pool, fusionnait en Python, puis
réécrivait le JSON ENTIER dans une autre connexion, sans verrou ni condition. Deux
patchs partis ensemble sur deux colonnes DIFFÉRENTES : l'un des deux perdu dans 100 %
des cas, 9 à 15 % avec un décalage de 0 à 50 ms. La voie par clé métier, verrouillée
depuis #197, perdait 0 %.

**Ce qu'il fait désormais.** Tout son travail sur la ligne tourne dans `apply_fn` de
`db.datastore_merge_row_locked`, sous `FOR UPDATE`, dans cet ordre : le BAIL, puis la
RÉVISION attendue, puis la fusion et l'UPDATE. Le motif qui avait tenu ce chemin hors du
verrou — « remplacer n'est pas fusionner » — vaut pour le remplacement (`upsert_row`),
pas pour un patch, qui EST une fusion.

⚠️ Ce module LIT des attributs de colonne et de tableau (`key`, `schema`…) : il est
listé dans `vocabulaire._read_keys`.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.errors import UniqueViolation

from .. import db
from . import acces_agent as aga
from . import fin_du_null as fdn
from . import reliques as rq
from . import schema as dsv2
from .columns import (
    _META_COLS,
    _merge_column,
    _refuse_mixed_layers,
    arbitrer_les_vides,
    refuser_geste_sans_effet,
)
from .controles import _relever_origine_module
from .donnees_d_origine import poser_les_deux_versions
from .errors import RowNotFound, RowValidationError
from .outils import _now_iso
from .points import _refuse_dotted_names, ranger_les_couches
from .reserves import refuser_champs_reserves


def revision_attendue(valeur: Any) -> Optional[int]:
    """`expected_revision` tel que reçu → l'entier comparé à `rev`, None s'il est omis.

    La révision est SERVIE en chaîne (`_revision`), c'est la forme attendue ; un entier
    désigne la même révision et passe aussi. Tout le reste est REFUSÉ en nommant la
    forme : une précondition illisible ne s'ignore pas, sinon l'écriture partirait sans
    la protection que l'appelant a demandée."""
    if valeur is None:
        return None
    texte = str(valeur).strip()
    if isinstance(valeur, bool) or not (texte.isascii() and texte.isdigit()):
        raise ValueError(
            f"`expected_revision` = la `_revision` servie avec la ligne que tu as lue "
            f"(une chaîne de chiffres, ex. \"3\") — reçu {valeur!r}. Rien n'est écrit.")
    return int(texte)


class EcritureParIdMixin:
    """Le patch par `id` du store. Composé par `DatastorePg`."""

    def update_row(self, datastore: str, row_id: str, patch: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False,
                   force: Optional[frozenset] = None,
                   expected_revision: Any = None) -> dict:
        """Patch partiel d'une row. `trace` (dict mutable, optionnel) = relevé pour
        le journal — dont l'état AVANT, celui-là même sur lequel la transition de
        cycle de vie est validée (cf. `_trace`) : lu SOUS le verrou, il est vrai.

        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`.

        `expected_revision` = la `_revision` de la ligne telle que l'appelant l'a lue,
        quand ce qu'il écrit a été CALCULÉ à partir d'elle. Différente de la révision
        en place ⇒ `RevisionConflict`, rien n'est écrit, aucune relance."""
        self._reject_misplaced_id(patch, row_id)
        attendue = revision_attendue(expected_revision)
        ns_id = self._resolve(datastore, write=True)
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        status_key = (dsv2.status_field(schema) or {}).get("key")
        # #658 : tranché AVANT la fusion, comme sur les autres chemins — c'est elle qui
        # ouvre le verrou de ligne, et ce jugement ne dépend pas de la ligne.
        # `force` implique la demande : nommer une cible EST le geste.
        forcage = self._forcage_readonly(
            ns_id, schema, readonly_override or bool(force), force)
        ecrit: dict = {}

        def _apply(en_place: dict) -> dict:
            data = dict(en_place or {})
            # La ligne est lue SOUS LE VERROU : ses colonnes sont « réelles » sans un
            # aller-retour de plus. C'est la porte du round-trip #390 — relire une
            # fiche et la repousser — donc celle où l'aller-retour DOIT se refermer.
            corps = ranger_les_couches(schema, patch, colonnes_en_place=lambda: set(data))
            # oto#140 : `null` efface ENCORE, mais il est en préavis. Refusé à la date,
            # JAMAIS interprété en silence (cf. `append_row`).
            vises = fdn.nulls_nommes(corps)
            if vises:
                if fdn.refus_arme():
                    raise ValueError(fdn.refus(vises))
                self.off_notices.add(fdn.avertissement(vises))
            _refuse_dotted_names(corps)
            _refuse_mixed_layers(schema, corps)
            prev_status = data.get(status_key) if status_key else None
            self._trace(trace, ns_id, ns, prev_status=prev_status)
            # MÊME garde que la fusion : un effacement qui ne détruit rien (la donnée
            # vit dans une relique littérale) serait annoncé comme un succès.
            vises = rq.effacements_sur_relique(corps, data)
            if vises:
                raise RowValidationError([rq.refus(vises)])
            if donnees_d_origine:
                poser_les_deux_versions(corps, avant=data, schema=schema)
            # MÊME arbitrage que la fusion : le patch par `id` est le geste qui a vidé
            # `moteur` en production le 13/08 (#407/#408/#409). Les deux chemins
            # d'écriture ont déjà divergé une fois sur cette famille de règles (#322) :
            # ils partagent la fonction, pas seulement l'intention.
            pose, vidages, ecartes = arbitrer_les_vides(data, corps, row_id)
            # #724 : un vide SEUL accepté sans effet, c'est le chemin des dix retraits
            # perdus du 01/09 — refusé avant tout relevé.
            refuser_geste_sans_effet(pose, ecartes)
            avant = dict(data)
            written = set()
            for k, v in pose.items():
                if k in _META_COLS:
                    continue
                # MÊME fusion que le batch : l'origine survit ici aussi.
                data[k] = _merge_column(data.get(k), v, dsv2.champ_declare(schema, k))
                written.add(k)
            # #586/#606 : MÊME garde que la fusion — le patch par `id` est le geste le
            # plus courant d'un agent, et celui qui a écrasé les quatorze valeurs.
            refuser_champs_reserves(schema, pose, avant=avant,
                                    forcage=forcage, agent=aga.appel_d_agent())
            # ⚠️ Ce chemin a déjà été oublié deux fois sur l'origine (sa survie, puis
            # son relevé) parce qu'il a son propre corps : les deux sont branchés ici.
            _relever_origine_module(self, ns_id, pose, avant, schema=schema,
                                    declare=origine_override)
            # Validation sur le RÉSULTAT mergé (un patch partiel ne doit pas échouer
            # sur un requis déjà présent) + transition de cycle de vie (ADR 0046 B/C).
            # Seule la borne de longueur se limite aux clés du patch (#383).
            self._check_row(schema, data, prev_status=prev_status, written=written)
            self.off_erased.extend(vidages)
            self.off_ignored.extend(ecartes)
            ecrit["data"] = data
            return data

        try:
            resultat = db.datastore_merge_row_locked(
                ns_id, row_id, _apply, _now_iso(),
                lease_guard=self._lease_guard(row_id),
                expected_revision=attendue, rafraichir_rang=True)
        except UniqueViolation:
            # Un AUTRE enregistrement porte déjà cette valeur de clé métier (index
            # UNIQUE ds_bkey_<ns_id>). Un update ciblé sur `row_id` ne peut pas basculer
            # silencieusement sur une autre row → erreur actionnable, jamais un 500.
            dk = (schema or {}).get("key")
            dkv = dsv2.unwrap((ecrit.get("data") or {}).get(dk)) if dk else None
            if dk and dkv is not None:
                raise ValueError(
                    f"un autre enregistrement porte déjà {dk}={dkv} "
                    "(clé métier unique) — impossible de dupliquer") from None
            raise  # violation inexpliquée → erreur franche, pas de repli muet
        if resultat is None:
            raise RowNotFound(row_id)
        row, data = resultat
        # #658 : après l'UPDATE — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, data)
        return self._row_to_dict(row, schema)
