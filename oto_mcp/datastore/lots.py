"""Écrire un LOT de lignes : le chemin des imports, et ses refus qui NOMMENT la ligne.

Extrait de `core.py` (déplacement pur, 07/09/2026) — un mixin que `DatastorePg`
compose, sur le modèle de `SchemaOpsMixin`. Séparé de `ecriture.py` parce que le lot
a ses propres règles — il n'est pas atomique, il dédouble sur une clé, et chacun de
ses refus doit dire OÙ il s'est arrêté (#412).

⚠️ Ce module LIT des attributs de colonne et de tableau (`key`, `schema`…) : il est
listé dans `vocabulaire._read_keys`.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.errors import UniqueViolation

from .. import db
from . import acces_agent as aga
from . import schema as dsv2
from .columns import _META_COLS, sans_les_nulls_sans_effet
from .controles import _relever_origine_module
from .errors import BusinessKeyRequired, RowLocked, RowValidationError
from .outils import _new_id, _refus_de_creation
from .points import _refuse_dotted_names, ranger_les_couches
from . import fin_du_null as fdn
from .donnees_d_origine import poser_les_deux_versions
from .reserves import refuser_champs_reserves


class LotsMixin:
    """L'écriture par lot du store. Composé par `DatastorePg`."""

    @staticmethod
    def _designation_de_lot(rang: int, total: int, key: Optional[str],
                            data: Any, faites: int) -> str:
        """COMMENT retrouver la ligne fautive d'un lot, et OÙ le lot s'est arrêté (#412).

        Le refus nommait le champ et la valeur, jamais la ligne : sur un import de
        8 910 lignes par lots de 200, retrouver la fautive coûtait plus cher que les
        199 lignes perdues avec elle. Le store valide ligne par ligne — il SAIT
        laquelle échoue, l'information existait et ne sortait pas.

        ⚠️ On y ajoute ce que le signal croyait acquis et qui est FAUX : **le lot
        n'est pas atomique**. Les lignes qui précèdent la fautive sont écrites et le
        restent. C'est ce qui décide de la reprise — rejouer le lot entier
        re-fusionnerait les premières (ou les dupliquerait, sans clé métier)."""
        ref = ""
        if key and isinstance(data, dict) and data.get(key) is not None:
            ref = f" ({key}={data[key]})"
        etat = (f"{faites} ligne{'s' if faites > 1 else ''} déjà "
                f"écrite{'s' if faites > 1 else ''} avant l'arrêt, aucune après "
                f"— reprends le lot à la ligne {rang}" if faites
                else "aucune ligne écrite avant l'arrêt")
        return f"ligne {rang}/{total} du lot{ref} · {etat}"

    def _write_rows_to_ns(self, ns_id: int, rows: list, *, key: Optional[str],
                          readonly_override: bool = False,
                          origine_override: bool = False,
                          donnees_d_origine: bool = False,
                          force: Optional[frozenset] = None) -> dict:
        """Cœur du batch, keyé par `ns_id` déjà résolu (réutilisable hors contexte
        d'org — matérialisation d'un upload signé, où l'org de session est absente).
        Le schéma v2 (validation/lifecycle, ADR 0046) s'applique à CHAQUE row du
        lot, sur son résultat mergé — une row fautive fait échouer le lot en NOMMANT
        la ligne autant que le champ (#412), et en disant ce qui est déjà écrit."""
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        nom_ns = ns.get("datastore") or f"#{ns_id}"
        # #658 : UN palier pour le lot entier, lu une seule fois — pas une lecture
        # d'ownership par ligne sur un import de huit mille.
        # `force` implique la demande : nommer une cible EST le geste.
        forcage = self._forcage_readonly(
            ns_id, schema, readonly_override or bool(force), force)
        inserted, updated, ids = 0, 0, []
        total = len(rows)
        for rang, data in enumerate(rows, 1):
            try:
                if not isinstance(data, dict):
                    raise ValueError("chaque row doit être un objet")
                self._reject_misplaced_id(data, None, batch=True)
                user_data = {k: v for k, v in data.items() if k not in _META_COLS}
                # ⚠️ #329 volet 2, appliqué au QUATRIÈME chemin — il y manquait.
                # `append_row`, `upsert_row` et la fusion refusent une clé littérale
                # pointée ; le LOT, non. Or c'est LUI qui porte les imports : la garde
                # était posée sur les trois chemins où l'on écrit une ligne, et absente
                # de celui où l'on en écrit huit mille.
                #
                # Ce que ça a produit, mesuré le 31/08 sur un fichier de production :
                # une fiche porte `contact2_nom.comment` et `contact2_email.comment`
                # comme COLONNES littérales de premier niveau, à côté d'une base
                # `contact2_nom` qui, elle, a été retirée depuis. Elles ont donc survécu
                # au retrait — *une couche imbriquée part avec sa colonne, une colonne
                # littérale du même nom ne part pas* — et se relisent ensuite comme des
                # « couches orphelines », un objet qui n'existe pas dans le modèle.
                # Deux sessions ont cherché le geste pendant une demi-journée.
                # MÊME ordre que les quatre autres portes : on range, puis on refuse.
                # C'est par ici que passent les imports — donc par ici que passe un
                # export du tableau de bord réimporté, qui porte `champ.comment` par
                # construction (#687).
                user_data = ranger_les_couches(
                    schema, user_data,
                    colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                        ns_id, schema, user_data, key))
                # oto#182 : un `null` qui n'efface rien ne s'écrit pas (cf. `append_row`).
                user_data = sans_les_nulls_sans_effet(
                    user_data, lambda: self._donnees_de_la_ligne_visee(
                        ns_id, schema, user_data, key), schema)
                # oto#140 : préavis de `null`, sur le chemin des imports aussi —
                # union sur le lot, donc une phrase et non cinq cents.
                vises = fdn.nulls_nommes(user_data)
                if vises:
                    if fdn.refus_arme():
                        raise ValueError(fdn.refus(vises))
                    self.off_notices.add(fdn.avertissement(vises))
                _refuse_dotted_names(user_data)
                # ⚠️ DÉBALLÉ : une clé métier ANNOTÉE désigne la même ligne qu'une clé nue.
                # `{"code": {"valeur": "A", "comment": "fichier source"}}` et
                # `{"code": "A"}` sont la MÊME identité — enrichir la provenance ne
                # change pas ce qu'une donnée EST. Sans ce déballage, le lookup
                # cherchait l'objet entier : ligne « introuvable », puis insertion,
                # puis `UniqueViolation` sur l'index de clé. Mesuré le 08/09/2026.
                kv = dsv2.unwrap(user_data.get(key)) if key else None
                existing_id = None
                if key and kv is not None and str(kv) != "":
                    existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
                # #516 : le LOT est le second chemin de création, et le plus
                # volumineux — c'est par lui que passent les imports. La garde s'y
                # juge sur la clé DÉCLARÉE, celle qui porte l'index UNIQUE, même
                # quand le lot dédouble sur une AUTRE (`key=` explicite) : sinon un
                # tableau fermé refuserait une ligne qu'il porte déjà.
                if existing_id is None and dsv2.key_required_of(schema):
                    dk = self._declared_key_of(schema)
                    dkv = dsv2.unwrap(user_data.get(dk))
                    if dk != key and dkv is not None and str(dkv) != "":
                        existing_id = db.datastore_find_row_id_by_key(ns_id, dk, dkv)
                    if existing_id is None:
                        raise _refus_de_creation(nom_ns, dk, dkv)
                if existing_id is not None:
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine,
                                         lot=True)
                    updated += 1
                    ids.append(existing_id)
                    continue
                # #586 : la création dans le LOT (même chemin que l'upload signé) —
                # la couche d'origine d'un champ système ne s'écrit pas.
                refuser_champs_reserves(schema, user_data,
                                        agent=aga.appel_d_agent())
                _relever_origine_module(self, ns_id, user_data, schema=schema,
                                        declare=origine_override)
                # CRÉATION : pas de ligne en base, donc rien à préserver — mais la
                # règle « une origine déjà posée ne se réécrit pas » vaut quand même,
                # car l'appelant peut avoir écrit `origine` lui-même (chemin déclaré).
                if donnees_d_origine:
                    poser_les_deux_versions(user_data, schema=schema)
                # `lot=True` : le refus de l'`id` nu doit nommer un geste qui
                # ABOUTIT en mode lot — cf. #72, 22 cas sur 29 suivaient le conseil
                # du refus précédent, lequel échouait ici.
                self._check_row(schema, user_data, lot=True, creation=True)
                try:
                    row = db.datastore_insert_row(ns_id, _new_id(), user_data)
                except UniqueViolation:
                    # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un
                    # write concurrent vient d'insérer la même clé entre le lookup et
                    # l'insert — c'est PRÉCISÉMENT le doublon que la contrainte empêche.
                    # On converge en update (même merge que le chemin nominal). La clé
                    # violée est la clé DÉCLARÉE du datastore (l'index ne porte qu'elle),
                    # qui peut différer d'un `key` explicite passé à l'appel.
                    dk = ((db.get_datastore_by_id(ns_id) or {}).get("schema")
                          or {}).get("key")
                    dkv = dsv2.unwrap(user_data.get(dk)) if dk else None
                    existing_id = (db.datastore_find_row_id_by_key(ns_id, dk, dkv)
                                   if dk and dkv is not None else None)
                    if existing_id is None:
                        raise  # violation inexpliquée → erreur franche, pas de repli muet
                    # ⚠️ `donnees_d_origine` voyage ICI aussi (oto#72) : ce chemin est
                    # la COURSE PERDUE sous l'index de clé métier, qui converge en
                    # update — « même merge que le chemin nominal », disait le
                    # commentaire, mais il laissait tomber ce paramètre. Une ligne
                    # d'import qui perdait sa course perdait sa version d'origine.
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine,
                                         lot=True)
                    updated += 1
                    ids.append(existing_id)
                    continue
            except RowLocked as e:
                # ⚠️ MÊME parti que les deux clauses suivantes, et pour la même
                # raison : le refus garde sa CLASSE, seule sa désignation change.
                # `RowLocked` dérive de `ValueError` depuis le 05/09/2026 ; sans
                # cette clause, elle tomberait dans le `except ValueError` du bas et
                # ressortirait en refus d'entrée invalide — perdant son code 409 et
                # le message du bail, exactement le défaut qu'on vient de fermer.
                raise RowLocked(
                    e.row_id, e.claimed_by, e.claimed_until, e.claimed_run,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except BusinessKeyRequired as e:
                # MÊME parti que ci-dessous : le refus garde sa classe (la face REST
                # en dérive son code `business_key_required`), seule sa désignation
                # change. Cette clause DOIT précéder `except ValueError` — dont
                # `BusinessKeyRequired` dérive, pour être actionnable côté MCP.
                raise BusinessKeyRequired(
                    e.motif, key=e.key, datastore=e.datastore, value=e.value,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except RowValidationError as e:
                # Le refus GARDE sa classe : les surfaces s'en servent pour choisir
                # leur code (`capabilities/datastore/rows`), et un refus de schéma
                # dans un lot reste un refus de schéma. Seule sa désignation change —
                # `details` suit, sinon le refus structuré (#545) se perdrait
                # exactement là où le lot rend la reprise la plus coûteuse.
                raise RowValidationError(
                    e.errors, details=e.details,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except ValueError as e:
                # Les autres refus de row (`id` égaré, `_id` dans un lot, row qui
                # n'est pas un objet) nomment déjà LEUR faute, jamais la ligne.
                raise ValueError(
                    f"{self._designation_de_lot(rang, total, key, data, inserted + updated)}"
                    f" : {e}") from None
            inserted += 1
            ids.append(row["row_id"])
        return {"inserted": inserted, "updated": updated, "count": inserted + updated,
                "key": key, "ids": ids}
