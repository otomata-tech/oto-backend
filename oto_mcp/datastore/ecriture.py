"""ÉCRIRE une ligne : l'ajouter, la fusionner, la remplacer, l'effacer.

Le PATCH par `id` vit à côté depuis le 12/09/2026 (`ecriture_par_id.py`), sorti dans le
lot qui l'a fait passer sous le verrou de ligne avec une précondition de révision.

Extrait de `core.py` (déplacement pur, 07/09/2026) — un mixin que `DatastorePg`
compose, sur le modèle de `SchemaOpsMixin`. Le LOT vit à côté (`lots.py`) : les deux
chemins d'écriture ont déjà divergé une fois sur une famille de règles (#322), ils
partagent donc les fonctions — `_check_row`, `arbitrer_les_vides`,
`refuser_champs_reserves` — pas seulement l'intention.

⚠️ Ce module LIT des attributs de colonne et de tableau (`key`, `schema`, `data`…) :
il est listé dans `vocabulaire._read_keys`.
"""
from __future__ import annotations

from typing import Optional

from psycopg.errors import UniqueViolation

from .. import db
from . import acces_agent as aga
from . import schema as dsv2
from .columns import (
    _META_COLS,
    _merge_column,
    _refuse_mixed_layers,
    arbitrer_les_vides,
    refuser_geste_sans_effet,
    sans_les_nulls_sans_effet,
)
from .controles import _relever_origine_module
from .errors import DatastoreNotFound, RowNotFound, RowValidationError
from . import fin_du_null as fdn
from . import reliques as rq
from .forcage import Forcage
from .outils import _new_id, _now_iso, _refus_de_creation
from .points import _refuse_dotted_names, ranger_les_couches
from .donnees_d_origine import poser_les_deux_versions
from .reserves import refuser_champs_reserves


class EcritureMixin:
    """Les écritures unitaires du store. Composé par `DatastorePg`."""

    # --- row ops -------------------------------------------------------------

    def append_row(self, datastore: str, data: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False,
                   force: Optional[frozenset] = None) -> dict:
        """Écrit UNE row. Si le datastore déclare une clé métier (`schema.key`),
        applique la MÊME dédup upsert que le batch `write_rows` : une row de même
        valeur de clé est MERGÉE (pas de doublon, l'index `ds_bkey_<ns>` la refuse) ;
        sinon append. Renvoie la row (nouvelle ou mise à jour).

        ⚠️ Sur un tableau qui déclare `key_required` (#516), l'append n'existe plus :
        une écriture qui ne désigne aucune ligne existante est REFUSÉE
        (`BusinessKeyRequired`) au lieu d'en créer une.

        `trace` (dict mutable, optionnel) = relevé pour le journal, cf. `_trace`.
        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`."""
        if isinstance(data, dict) and "_id" in data:
            # PROMOTION (#354, amende le refus #390) : `_id` dans `row` EST
            # l'adresse de la ligne — réécrire la ligne telle que
            # `data_claim_next`/`data_rows` l'a servie devient le geste juste,
            # symétrique du claim. Garde-fou indissociable : un `_id` qui ne
            # matche AUCUNE ligne rend une erreur nommée, jamais une création —
            # sinon la promotion re-fabrique le fantôme par une porte de côté.
            cible = str(data["_id"])
            reste = {k: v for k, v in data.items() if k != "_id"}
            try:
                return self.update_row(datastore, cible, reste, trace=trace,
                                       readonly_override=readonly_override,
                                       donnees_d_origine=donnees_d_origine)
            except RowNotFound:
                raise ValueError(
                    f"`_id` ({cible!r}) ne correspond à aucune ligne de "
                    f"`{datastore}` — rien n'est créé. L'identifiant est peut-être "
                    "tronqué ou la ligne purgée : relis-la (data_rows, "
                    "data_claim_next) et réécris avec son `_id` exact.")
        ns_id = self._resolve(datastore, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        # CAS 1 avant le refus : une fiche relue et réémise entière porte
        # `site_web` ET `site_web.comment`, et c'est notre propre lecture. On range
        # l'annotation à sa place AVANT de juger quoi que ce soit — sinon les gardes
        # qui suivent (champs réservés, schéma) jugeraient une adresse au lieu d'une
        # colonne, et le geste dominant d'un agent se ferait refuser.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                ns_id, schema, user_data))
        # oto#182 : un `null` qui n'efface rien (l'écho d'une ligne lue) ne s'écrit pas,
        # et ne compte donc pas pour le préavis ci-dessous.
        user_data = sans_les_nulls_sans_effet(
            user_data, lambda: self._donnees_de_la_ligne_visee(ns_id, schema, user_data),
            schema)
        # oto#140 : `null` efface ENCORE, mais il est en préavis. Dit à l'instant où
        # l'ancien comportement joue — le seul moment actionnable, et le lecteur est
        # celui qui peut agir. ⚠️ Refusé à la date, JAMAIS interprété en silence : un
        # `null` traduit en `@empty` « pour rendre service » effacerait la valeur d'un
        # agent qui voulait dire « cherché, rien trouvé » — le dégât même que ce lot
        # existe pour empêcher, commis par la correction.
        vises = fdn.nulls_nommes(user_data)
        if vises:
            if fdn.refus_arme():
                raise ValueError(fdn.refus(vises))
            self.off_notices.add(fdn.avertissement(vises))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        # #586 : la couche d'origine d'un champ système ne s'écrit pas, création
        # comprise — jugée sur le payload seul (le readonly, lui, se juge contre la
        # ligne en place, donc dans la fusion). Refusé AVANT le lookup de clé.
        # #658 : tranché AVANT la fusion — c'est elle qui ouvre le verrou de ligne.
        # `force` implique la demande : nommer une cible EST le geste.
        forcage = self._forcage_readonly(
            ns_id, schema, readonly_override or bool(force), force)
        refuser_champs_reserves(schema, user_data, agent=aga.appel_d_agent())
        _relever_origine_module(self, ns_id, user_data, schema=schema,
                                declare=origine_override)
        self._trace(trace, ns_id, ns)
        # La clé métier sort du MÊME schéma que ci-dessus (`declared_key` re-résolvait
        # le datastore et relisait la ligne pour le même résultat).
        key = self._declared_key_of(schema)
        # ⚠️ DÉBALLÉ — une clé métier annotée est la MÊME identité qu'une clé nue
        # (cf. `lots.py`). Enrichir la provenance ne change pas ce qu'une donnée est.
        kv = dsv2.unwrap(user_data.get(key)) if key else None
        if key and kv is not None and str(kv) != "":
            existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
            if existing_id is not None:
                return self._row_to_dict(
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine),
                    schema)
        # #516 : sur un tableau FERMÉ, on ne crée pas — on vise. Le geste est arrivé
        # jusqu'ici sans désigner de ligne : ni par son `_id` (promu plus haut, et
        # refusé s'il ne matche rien), ni par une valeur de clé que le tableau porte.
        # Refuser AVANT `_check_row` : la validation de schéma parlerait des champs
        # d'une ligne qui ne doit pas naître.
        if dsv2.key_required_of(schema):
            raise _refus_de_creation(ns.get("datastore") or datastore, key, kv)
        # #390 (3ᵉ demande) : une ligne CRÉÉE sans la clé métier déclarée est non
        # rapprochable — aucune écriture ultérieure ne la retrouvera par sa clé, et
        # le batch qui dédouble passera à côté. C'est la forme résiduelle de
        # l'incident : une 501ᵉ ligne sans SIREN née avec tout l'enrichissement,
        # sans une erreur. Les deux autres portes (adresse égarée dans `row`, `id`
        # nu) sont désormais fermées ; celle-ci n'a pas d'adresse du tout, donc rien
        # à refuser — on NOMME, comme `hors_schema`. Mesuré avant de la poser :
        # 197 tableaux à clé déclarée, 50 024 lignes, 3 sans clé. Elle ne parlera
        # quasiment jamais, et c'est ce qui la rendra lisible.
        if key and (kv is None or str(kv) == ""):
            # ⚠️ **Remonté au premier niveau depuis le 09/09/2026**, et pas ajouté :
            # ce message existait, exact, dans `notices` — et il n'a rien empêché,
            # servi dix fois en une soirée. Ce n'est pas un relevé de routine, c'est
            # une réserve qui contredit le succès annoncé juste à côté.
            self.off_non_rapprochables[str(key)] = (
                f"ligne créée SANS `{key}`, la clé métier de ce tableau : elle ne "
                f"sera rapprochée par personne — ni une réécriture, ni un lot qui "
                f"dédouble sur cette clé. Si elle visait une ligne existante, c'est "
                f"data_write(id=…) ; sinon renseigne `{key}`.")
        # ⚠️ QUATRIÈME chemin, et celui que j'avais oublié — trouvé par le banc, pas
        # par relecture. Les trois autres (lot, fusion, patch par `id`) étaient
        # branchés ; la création unitaire, non. C'est exactement le défaut que ce
        # fichier dénonce ailleurs sur la même famille de règles : une garde posée sur
        # les chemins auxquels on pense, absente de celui qu'on croyait couvert parce
        # qu'il ressemble aux autres.
        if donnees_d_origine:
            poser_les_deux_versions(user_data, schema=schema)
        # `creation=True` : c'est ici qu'une colonne parasite NAÎT (#117). Un patch par
        # `id` vise une ligne existante et peut légitimement ne toucher qu'une colonne
        # libre — la garde n'y a rien à faire.
        self._check_row(schema, user_data, creation=True)
        try:
            row = db.datastore_insert_row(ns_id, _new_id(), user_data)
        except UniqueViolation:
            # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un write
            # concurrent a inséré la même clé entre le lookup et l'insert — le doublon
            # que la contrainte empêche. On converge en merge (même chemin que le batch).
            existing_id = (db.datastore_find_row_id_by_key(ns_id, key, kv)
                           if key and kv is not None else None)
            if existing_id is None:
                raise  # violation inexpliquée → erreur franche, pas de repli muet
            return self._row_to_dict(
                self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                     forcage=forcage,
                                     origine_override=origine_override),
                schema)
        return self._row_to_dict(row, schema)

    def _merge_into_row(self, ns_id: int, row_id: str, user_data: dict,
                        *, schema: Optional[dict] = None,
                        forcage: Optional[Forcage] = None,
                        origine_override: bool = False,
                        donnees_d_origine: bool = False,
                        lot: bool = False) -> dict:
        """MERGE `user_data` dans la row existante (dernier écrit gagne par champ),
        en appliquant le schéma v2 (ADR 0046) au résultat mergé : validation avec
        `prev_status` (transition de lifecycle) puis release du claim si l'état
        devient terminal. Renvoie la row brute persistée. Corps commun à l'append
        unitaire et au batch.

        Le read-merge-write est ATOMIQUE (verrou de ligne, #197) : le get + le
        merge + l'update tournent dans une seule transaction `FOR UPDATE`, sinon
        deux writes concurrents de la même clé (même row_id) s'écrasaient
        mutuellement (last-writer-wins) et perdaient des champs silencieusement.

        `lot` = ce geste vient d'un LOT (oto#72). Il ne change rien à la fusion, il
        change le REFUS : hors lot, celui d'un `id` nu conseille deux gestes que le mode
        lot refuse ailleurs. La ligne de lot qui retrouve une ligne EXISTANTE passe ici,
        et recevait donc le conseil qui échoue au tour suivant."""
        if schema is None:
            schema = self._schema_of(ns_id)
        # La ligne visée est connue ICI : ses colonnes comptent pour « colonne réelle »,
        # ce qui rend `{"site_web.comment": …}` seul écrivable sur un tableau souple.
        # Lue paresseusement — le chemin nominal ne la demande jamais.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: set(
                (db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        sk = (dsv2.status_field(schema) or {}).get("key")

        def _apply(current: dict) -> dict:
            merged = dict(current or {})
            prev_status = merged.get(sk) if sk else None
            # Arbitrage AVANT la fusion : après, l'ancienne valeur n'existe plus
            # nulle part. Il rend d'un coup ce que l'écriture pose VRAIMENT (les
            # vides non-`null` qui auraient déplacé une valeur en sont retirés,
            # #608) et les deux relevés. Posés sur le store seulement une fois la
            # validation passée — un refus n'a rien effacé, l'annoncer ferait
            # chercher un dégât imaginaire.
            # `donnees_d_origine` : l'appel apporte la donnée telle qu'elle a été
            # REMISE. On fige sa version d'origine AVANT l'arbitrage des vides, pour
            # que ce qui est écarté le soit sur la forme définitive. Sous le verrou de
            # ligne, donc `current` est la ligne vraie — indispensable ici : c'est LUI
            # qui dit si une origine est déjà posée, et une origine posée ne se
            # réécrit jamais. Muter en place est sans risque, le geste est idempotent.
            # ⚠️ Un effacement qui ne détruit RIEN, annoncé comme un succès : le
            # geste vise la couche imbriquée, mais la donnée est dans une relique
            # littérale (`data->>"c.link"`) que rien ici ne touche. Un lecteur croit
            # avoir retiré une donnée personnelle. Un zéro se met en doute ; un succès
            # ne se met pas en doute — d'où un REFUS, et seulement sur l'effacement :
            # l'écriture ordinaire vise l'imbriqué à juste titre.
            vises = rq.effacements_sur_relique(user_data, current)
            if vises:
                raise RowValidationError([rq.refus(vises)])
            if donnees_d_origine:
                poser_les_deux_versions(user_data, avant=current, schema=schema)
            pose, vidages, ecartes = arbitrer_les_vides(current, user_data, row_id)
            # #724 : préserver et le DIRE ne suffit pas quand l'écarté était TOUT ce
            # que l'écriture portait — l'appel n'a alors aucun effet et répond 200.
            # ⚠️ Par CE chemin le refus ne peut pas parler : on n'arrive ici (append
            # promu, lot) qu'avec une valeur de clé métier non vide, donc posée — ce
            # qui garantit qu'un LOT ne casse jamais dessus. Il y est quand même :
            # les deux chemins d'écriture ont déjà divergé une fois sur cette famille
            # de règles (#322), ils partagent la fonction, pas seulement l'intention.
            refuser_geste_sans_effet(pose, ecartes)
            # Colonne par colonne, pour que l'origine survive à une écriture
            # ordinaire. Un `update` en bloc l'emporterait avec le reste — et
            # silencieusement, puisque remplacer une valeur est le geste normal.
            # ⚠️ Le champ DÉCLARÉ passe avec la valeur : sans lui, une liste qui
            # nomme l'identité de ses éléments (`of.key`) se remplacerait quand même
            # en bloc, et la déclaration serait une clé de plus que rien ne lit.
            for _k, _v in pose.items():
                merged[_k] = _merge_column(merged.get(_k), _v,
                                           dsv2.champ_declare(schema, _k))
            # #586/#606 : ce que l'appelant n'écrit pas — jugé sur le geste ENTIER
            # (payload, ligne en place, résultat), sous le verrou, avant que quoi
            # que ce soit ne parte. Puis la plateforme pose l'origine qu'elle doit.
            refuser_champs_reserves(schema, pose, avant=current or {},
                                    forcage=forcage, agent=aga.appel_d_agent())
            _relever_origine_module(self, ns_id, pose, current or {}, schema=schema,
                                    declare=origine_override)
            # ⚠️ `written` reste l'ensemble des clés que l'appelant a NOMMÉES, pas
            # celles qu'on a retenues : une borne de longueur ou un motif ne doit pas
            # se réarmer sur une colonne préservée, dont la valeur n'a pas bougé.
            self._check_row(schema, merged, prev_status=prev_status,
                            written=set(pose), lot=lot)
            self.off_erased.extend(vidages)
            self.off_ignored.extend(ecartes)
            return merged

        result = db.datastore_merge_row_locked(ns_id, row_id, _apply, _now_iso(),
                                               lease_guard=self._lease_guard(row_id))
        if result is None:
            raise RowNotFound(row_id)  # supprimée entre le lookup et le verrou (course)
        row, merged = result
        # #658 : après le verrou — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, merged)
        return row

    def upsert_row(self, datastore: str, row_id: str, data: dict, *,
                   origine_override: bool = False) -> tuple[dict, bool]:
        """Écrit une row à une clé `row_id` EXPLICITE (≠ append_row qui génère un
        id), en remplaçant si elle existe. Crée le datastore au besoin. Sert le
        stockage dédupliqué par clé stable (ex. urn LinkedIn). Renvoie
        `(row, inserted)` — `inserted` False = la row existait déjà."""
        self._reject_misplaced_id(data, row_id)
        try:
            ns_id = self._resolve(datastore, write=True)
        except DatastoreNotFound:
            _ot, _oid = self._default_owner()
            db.create_datastore(_ot, _oid, datastore)
            self._active_scope_cache = None  # invalide le cache (le ns créé appartient à la PERSONNE (ADR 0068), pas à l'org active)
            ns_id = self._resolve(datastore, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        schema = self._schema_of(ns_id)
        # ⚠️ Pas de `colonnes_en_place` ici, et c'est délibéré : l'upsert REMPLACE la
        # ligne. Ranger une annotation sur une colonne qui n'est que dans l'ancienne
        # ligne poserait une couche sur une valeur qui tombe dans le même geste.
        user_data = ranger_les_couches(schema, user_data)
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        valide = dsv2.validation_active(schema) or dsv2.lifecycle_of(schema)
        reserves = bool(dsv2.readonly_fields(schema)
                        or dsv2.system_origin_fields(schema))
        prev = db.datastore_get_row(ns_id, row_id) if (valide or reserves) else None
        prev_data = dict((prev or {}).get("data") or {}) if prev else None
        if reserves:
            # #586/#606 sur un REMPLACEMENT : une colonne readonly absente du corps
            # serait perdue par le remplacement — c'est une modification, jugée
            # comme telle (le payload est complété des colonnes qui tomberaient).
            complet = {**{k: None for k in (prev_data or {}) if k not in user_data},
                       **user_data}
            refuser_champs_reserves(schema, complet, avant=prev_data,
                                    agent=aga.appel_d_agent())
            _relever_origine_module(self, ns_id, complet, prev_data, schema=schema,
                                    declare=origine_override)
        if valide:
            sk = (dsv2.status_field(schema) or {}).get("key")
            prev_status = (prev_data or {}).get(sk) if sk else None
            self._check_row(schema, user_data, prev_status=prev_status)
        self._assert_writable(ns_id, row_id)
        row, inserted = db.datastore_upsert_row(ns_id, row_id, user_data)
        if not inserted:
            self._terminal_write_notice(schema, ns_id, row_id, user_data)
        return self._row_to_dict(row, schema), inserted

    def declared_key(self, datastore: str) -> Optional[str]:
        """Clé métier déclarée au schéma (`schema.key`) — sert la dédup au batch
        write. None si aucune (table libre / schéma sans clé).

        Lit le schéma SERVI, et c'est sans conséquence (oto#83) : le masquage ne touche
        jamais la clé métier — `acces_agent._cles_par_acces` l'écarte quoi qu'en dise la
        déclaration, précisément pour qu'aucune décision interne ne dépende du point de
        vue de l'appelant."""
        return self._declared_key_of(self.get_schema(datastore))

    def write_rows(self, datastore: str, rows: list, *, key: Optional[str] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False,
                   force: Optional[frozenset] = None) -> dict:
        """Écrit un LOT de rows en un appel. Si une clé métier est en vigueur (param
        `key` explicite, sinon `schema.key` déclarée), chaque row qui la porte fait un
        UPSERT (merge) sur la row existante de même valeur de clé — pas de doublon ;
        sinon append d'une nouvelle row. Renvoie un récap {inserted, updated, count,
        key, ids}. Résout le datastore UNE fois (write) pour tout le lot."""
        ns_id = self._resolve(datastore, write=True)
        return self._write_rows_to_ns(ns_id, rows, key=key or self.declared_key(datastore),
                                      readonly_override=readonly_override,
                                      origine_override=origine_override,
                                      donnees_d_origine=donnees_d_origine,
                                      force=force)

    def delete_row(self, datastore: str, row_id: str, *,
                   trace: Optional[dict] = None) -> None:
        ns_id = self._resolve(datastore, write=True)
        if trace is not None:
            # Relevé demandé : on lit l'état de la row DANS le chemin de suppression
            # (au plus près du delete), jamais par un `get_row` séparé côté route —
            # qui re-résoudrait le datastore et courrait avec un write concurrent.
            ns = self._ns_of(ns_id)
            sk = (dsv2.status_field(ns.get("schema")) or {}).get("key")
            prev = ((db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}) if sk else {}
            self._trace(trace, ns_id, ns, prev_status=prev.get(sk) if sk else None)
        self._assert_writable(ns_id, row_id)
        if not db.datastore_delete_row(ns_id, row_id):
            raise RowNotFound(row_id)
