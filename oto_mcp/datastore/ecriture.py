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

from typing import Any, Optional

from psycopg.errors import UniqueViolation

from .. import db, geste
from . import acces_agent as aga
from . import schema as dsv2
from .columns import (
    _META_COLS,
    _refuse_mixed_layers,
    arbitrer_les_vides,
    mots_resolus_a_la_creation,
    refuser_cles_internes,
    refuser_geste_sans_effet,
    refuser_les_mots_mal_places,
    sans_les_nulls_sans_effet,
    sans_les_objets_vides,
    vides_assumes_perdus,
)
from .cle_metier import ligne_de_la_course_perdue, refuser_cle_metier_vide
from .controles import _relever_origine_module
from .errors import DatastoreNotFound, RowNotFound, RowValidationError
from . import mots_deprecies as mdp
from . import jetons
from . import upsert_implicite as upi
from . import vide_remplace as vr
from . import reliques as rq
from .forcage import Forcage
from .outils import _new_id, _now_iso, _refus_de_creation
from .points import _refuse_dotted_names, ranger_les_couches
from . import rangs as rg
from .precondition import revision_attendue
from . import donnees_d_origine as ddo
from . import layers as dsl
from . import versions as dsver
from .reserves import refuser_champs_reserves


class EcritureMixin:
    """Les écritures unitaires du store. Composé par `DatastorePg`."""

    # --- row ops -------------------------------------------------------------

    @geste.import_si_donnees_d_origine
    def append_row(self, datastore: str, data: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False,
                   force: Optional[frozenset] = None,
                   upsert: bool = False,
                   key: Optional[str] = None,
                   layers: str = dsl.DEFAUT,
                   versions: tuple = dsver.DEFAUT,
                   empties: str = dsl.EMPTIES_DEFAUT) -> dict:
        """Écrit UNE row. Si le datastore déclare une clé métier (`schema.key`) et
        qu'une row porte déjà cette valeur de clé (pas de doublon, l'index
        `ds_bkey_<ns>` la refuse) : l'écriture qui la DÉSIGNE (`key` = la clé
        déclarée, ou tableau fermé) la modifie ; celle qui AJOUTE la fusionne avec
        `upsert=True`, et sans lui est avertie puis REFUSÉE à la date de
        `upsert_implicite` (oto#141). Sinon append. Renvoie la row.

        `key` (oto#141) = l'appel NOMME la clé : elle doit être la clé déclarée, et la
        row en porter la valeur — sinon refus, jamais un paramètre ignoré (cf.
        `jetons.refus_de_key_sans_lot`).

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
                                       donnees_d_origine=donnees_d_origine,
                                       layers=layers, versions=versions, empties=empties)
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
        # oto#22 : l'écriture PAR RANG (`contacts[0].email`, `contacts[+]`) sort du
        # payload AVANT toute garde — laissée dedans, elle serait jugée comme un nom
        # pointé. Ses éléments passent les mêmes gardes, à leur vrai rang ; elle se
        # résout contre la ligne en place, sous le verrou de la fusion.
        user_data, rangs = rg.sortir_les_rangs(schema, user_data)
        if rangs is not None:
            rangs.preparer(schema, self._normaliser_les_dates)
        # CAS 1 avant le refus : une fiche relue et réémise entière porte
        # `site_web` ET `site_web.comment`, et c'est notre propre lecture. On range
        # l'annotation à sa place AVANT de juger quoi que ce soit — sinon les gardes
        # qui suivent (champs réservés, schéma) jugeraient une adresse au lieu d'une
        # colonne, et le geste dominant d'un agent se ferait refuser.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                ns_id, schema, user_data))
        # oto#182 : un `null` qui n'efface rien (l'écho d'une ligne lue) ne s'écrit pas.
        user_data = sans_les_nulls_sans_effet(
            user_data, lambda: self._donnees_de_la_ligne_visee(ns_id, schema, user_data),
            schema)
        # oto#165 : un `{}` sur une case vide n'est pas une valeur — écarté, et dit.
        user_data, objets_vides = sans_les_objets_vides(
            user_data, lambda: self._donnees_de_la_ligne_visee(ns_id, schema, user_data))
        self.off_rejected.extend(objets_vides)
        # oto#140 : `@keep` et `@clear` — avertis jusqu'à leur date, REFUSÉS à partir
        # d'elle (J3), dit à l'instant où l'appelant les emploie, le seul moment
        # actionnable.
        mdp.controler(self.off_notices, user_data, rangs.brut if rangs else None)
        _refuse_dotted_names(user_data)
        refuser_cles_internes(user_data)
        refuser_les_mots_mal_places(schema, user_data)
        _refuse_mixed_layers(schema, user_data)
        # #859 : les dates en une forme, AVANT la recherche par clé et la fusion.
        user_data = self._normaliser_les_dates(schema, user_data)
        # Signal feedback 994 : une clé métier vide entrerait dans l'index d'unicité
        # comme `""` — refusée ici, avant toute recherche et tout insert.
        refuser_cle_metier_vide(schema, user_data)
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
        cle_nommee, key = key, self._declared_key_of(schema)
        # oto#141 : `key` NOMMÉ par l'appel désigne la ligne par la clé — il doit nommer
        # la clé déclarée, la ligne en porter la valeur. Sinon il ne désignerait rien :
        # refusé, jamais ignoré (la même règle que la face MCP, `jetons`).
        if cle_nommee is not None and not jetons.key_unitaire_redondant(
                cle_nommee, key, data, None):
            raise ValueError(jetons.refus_de_key_sans_lot(cle_nommee, key))
        designation = upi.designe(schema, cle_nommee is not None)
        # ⚠️ DÉBALLÉ — une clé métier annotée est la MÊME identité qu'une clé nue
        # (cf. `lots.py`). Enrichir la provenance ne change pas ce qu'une donnée est.
        kv = dsv2.unwrap(user_data.get(key)) if key else None
        # oto#141 : `upsert=true` sans clé ne fusionnerait rien — refusé, pas ignoré.
        upi.refuser_upsert_sans_cle(upsert, key, ns.get("datastore") or datastore)
        if key and kv is not None:
            existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
            if existing_id is not None:
                # oto#141 : DÉSIGNÉE, la ligne se modifie ; AJOUTÉE, la fusion n'est
                # plus implicite — avertie jusqu'à sa date, refusée à partir d'elle.
                upi.controler_fusion(self.off_notices, designation=designation,
                                     upsert=upsert,
                                     datastore=ns.get("datastore") or datastore,
                                     key=key, kv=kv, row_id=existing_id)
                return self._row_to_dict(
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine,
                                         rangs=rangs),
                    schema, layers=layers, versions=versions, empties=empties)
        # #516 : sur un tableau FERMÉ, on ne crée pas — on vise. Le geste est arrivé
        # jusqu'ici sans désigner de ligne : ni par son `_id` (promu plus haut, et
        # refusé s'il ne matche rien), ni par une valeur de clé que le tableau porte.
        # Refuser AVANT `_check_row` : la validation de schéma parlerait des champs
        # d'une ligne qui ne doit pas naître.
        if dsv2.key_required_of(schema):
            raise _refus_de_creation(ns.get("datastore") or datastore, key, kv,
                                     schema=schema, ligne=user_data)
        # #390 (3ᵉ demande) : une ligne CRÉÉE sans la clé métier déclarée est non
        # rapprochable — aucune écriture ultérieure ne la retrouvera par sa clé, et
        # le batch qui dédouble passera à côté. C'est la forme résiduelle de
        # l'incident : une 501ᵉ ligne sans SIREN née avec tout l'enrichissement,
        # sans une erreur. Les deux autres portes (adresse égarée dans `row`, `id`
        # nu) sont désormais fermées ; celle-ci n'a pas d'adresse du tout, donc rien
        # à refuser — on NOMME, comme `hors_schema`. Mesuré avant de la poser :
        # 197 tableaux à clé déclarée, 50 024 lignes, 3 sans clé. Elle ne parlera
        # quasiment jamais, et c'est ce qui la rendra lisible.
        if key and kv is None:
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
        # oto#204 (et le trou de #183) : la CRÉATION ne passe pas par la fusion, donc les
        # mots réservés se résolvent ICI, AVANT la capture d'origine (qui prendrait
        # `"@empty"` pour une valeur remise) et avant la validation (qui le prendrait pour
        # un texte satisfaisant `required`). Sur une COPIE : si la course est perdue
        # ci-dessous, la fusion reçoit le geste d'origine et le résout elle-même — lui
        # passer un marqueur le ferait refuser comme clé interne.
        # oto#22 : la ligne naît — un rang n'y vise rien, `contacts[+]` y ajoute. Les
        # colonnes ainsi produites passent la garde des champs réservés, qui n'a vu
        # plus haut que le payload sans elles.
        cree = user_data
        if rangs is not None:
            par_rang = rangs.appliquer({}, schema, creation=True)
            refuser_champs_reserves(schema, par_rang, agent=aga.appel_d_agent())
            cree = {**user_data, **par_rang}
        a_creer = mots_resolus_a_la_creation(schema, cree)
        releve = ddo.poser_les_deux_versions(a_creer) if donnees_d_origine else None
        # `creation=True` : c'est ici qu'une colonne parasite NAÎT (#117). Un patch par
        # `id` vise une ligne existante et peut légitimement ne toucher qu'une colonne
        # libre — la garde n'y a rien à faire.
        self._check_row(schema, a_creer, creation=True)
        try:
            row = db.datastore_insert_row(ns_id, _new_id(), a_creer)
        except UniqueViolation as e:
            # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un write
            # concurrent a inséré la même clé entre le lookup et l'insert — le doublon
            # que la contrainte empêche. On converge en merge (même chemin que le batch).
            existing_id = ligne_de_la_course_perdue(ns_id, key, kv, e)
            # oto#141 : perdre la course, c'est découvrir que la clé EXISTE — même règle
            # que le lookup ci-dessus.
            upi.controler_fusion(self.off_notices, designation=designation,
                                 upsert=upsert,
                                 datastore=ns.get("datastore") or datastore,
                                 key=key, kv=kv, row_id=existing_id)
            # `donnees_d_origine` voyage ici : le geste d'origine n'a pas été touché
            # ci-dessus, la fusion pose donc elle-même les deux versions (cf. `lots.py`).
            return self._row_to_dict(
                self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                     forcage=forcage,
                                     origine_override=origine_override,
                                     donnees_d_origine=donnees_d_origine,
                                     rangs=rangs),
                schema, layers=layers, versions=versions, empties=empties)
        # oto#164 : relevé APRÈS l'insert — une course perdue ne compte pas deux fois,
        # la fusion ci-dessus relève elle-même ce qu'elle pose.
        if releve is not None:
            ddo.relever(self, releve)
        return self._row_to_dict(row, schema, layers=layers, versions=versions, empties=empties)

    def _merge_into_row(self, ns_id: int, row_id: str, user_data: dict,
                        *, schema: Optional[dict] = None,
                        forcage: Optional[Forcage] = None,
                        origine_override: bool = False,
                        donnees_d_origine: bool = False,
                        lot: bool = False,
                        rangs: Optional[rg.EcrituresParRang] = None) -> dict:
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
        et recevait donc le conseil qui échoue au tour suivant.

        `rangs` = l'écriture PAR RANG du geste (oto#22), sortie du payload par
        l'appelant : elle se résout ICI, contre la ligne lue sous le verrou."""
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
        refuser_cles_internes(user_data)
        refuser_les_mots_mal_places(schema, user_data)
        _refuse_mixed_layers(schema, user_data)
        # #859 : le lot arrive ici sans être passé par `append_row`.
        user_data = self._normaliser_les_dates(schema, user_data)
        sk = (dsv2.status_field(schema) or {}).get("key")

        def _apply(current: dict) -> dict:
            merged = dict(current or {})
            prev_status = merged.get(sk) if sk else None
            # oto#22 : les rangs se résolvent ICI, contre la ligne lue sous le verrou —
            # deux gestes concurrents sur deux éléments ne s'écrasent pas. Ensuite la
            # colonne-liste résultante suit la fusion comme toute colonne.
            ecrit = (user_data if rangs is None
                     else {**user_data, **rangs.appliquer(current, schema)})
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
            vises = rq.effacements_sur_relique(ecrit, current)
            if vises:
                raise RowValidationError([rq.refus(vises)])
            releve = (ddo.poser_les_deux_versions(ecrit, avant=current)
                      if donnees_d_origine else None)
            pose, vidages, ecartes = arbitrer_les_vides(current, ecrit, row_id)
            # #724 : préserver et le DIRE ne suffit pas quand l'écarté était TOUT ce
            # que l'écriture portait — l'appel n'a alors aucun effet et répond 200.
            # ⚠️ Par CE chemin le refus ne peut pas parler : on n'arrive ici (append
            # promu, lot) qu'avec une valeur de clé métier non vide, donc posée — ce
            # qui garantit qu'un LOT ne casse jamais dessus. Il y est quand même :
            # les deux chemins d'écriture ont déjà divergé une fois sur cette famille
            # de règles (#322), ils partagent la fonction, pas seulement l'intention.
            # oto#140 J2 : ce vide écarté REMPLACERA la valeur à une date annoncée — dit
            # dans la réponse comme dans le refus.
            annonce = vr.annonce(ecrit, ecartes)
            refuser_geste_sans_effet(pose, ecartes, annonce)
            # Colonne par colonne, pour que l'origine survive à une écriture
            # ordinaire. Un `update` en bloc l'emporterait avec le reste — et
            # silencieusement, puisque remplacer une valeur est le geste normal.
            # ⚠️ Le champ DÉCLARÉ passe avec la valeur : sans lui, une liste qui
            # nomme l'identité de ses éléments (`of.key`) se remplacerait quand même
            # en bloc, et la déclaration serait une clé de plus que rien ne lit.
            for _k, _v in pose.items():
                merged[_k] = rg.fusionner(rangs, _k, merged.get(_k), _v,
                                          dsv2.champ_declare(schema, _k))
            # oto#204 : un vide ASSUMÉ redevenu vide ordinaire par le remplacement d'une
            # liste se relève. Sur un requis, `_check_row` refuse juste après ; ailleurs
            # il tomberait sans un mot.
            for _k, _v in pose.items():
                vidages.extend(vides_assumes_perdus((current or {}).get(_k),
                                                    merged.get(_k), _k, row_id, _v))
            # #586/#606 : ce que l'appelant n'écrit pas — jugé sur le geste ENTIER
            # (payload, ligne en place, résultat), sous le verrou, avant que quoi
            # que ce soit ne parte. Puis la plateforme pose l'origine qu'elle doit.
            refuser_champs_reserves(schema, pose, avant=current or {},
                                    forcage=forcage, agent=aga.appel_d_agent())
            # Les origines que `donnees_d_origine` vient de poser sont déclarées par
            # le paramètre : elles ne sont pas « écrites sans le dire » (oto#70).
            _relever_origine_module(
                self, ns_id,
                ddo.sans_les_origines_posees(pose, releve) if releve else pose,
                current or {}, schema=schema, declare=origine_override)
            # ⚠️ `written` reste l'ensemble des clés que l'appelant a NOMMÉES, pas
            # celles qu'on a retenues : une borne de longueur ou un motif ne doit pas
            # se réarmer sur une colonne préservée, dont la valeur n'a pas bougé.
            self._check_row(schema, merged, prev_status=prev_status,
                            written=set(pose), en_place=current or {}, pose=pose,
                            lot=lot, ecrits_par_rang=rangs.ecrits if rangs else None)
            self.off_erased.extend(vidages)
            self.off_ignored.extend(ecartes)
            if annonce:
                self.off_notices.add(annonce)
            if releve is not None:
                ddo.relever(self, releve)
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
            db.create_datastore(_ot, _oid, datastore,
                                context_org_id=self._org_de_l_appel())
            self._active_scope_cache = None  # invalide le cache (le ns créé appartient à la PERSONNE (ADR 0068), pas à l'org active)
            ns_id = self._resolve(datastore, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        schema = self._schema_of(ns_id)
        # oto#22 : un REMPLACEMENT n'a pas d'élément en place à viser — un rang s'y
        # refuse en le disant, plutôt que de tomber dans le refus des noms pointés.
        _sans_rang, rangs = rg.sortir_les_rangs(schema, user_data)
        if rangs is not None:
            raise RowValidationError([
                f"{', '.join('`' + c + '`' for c in rangs.brut)} : ce chemin REMPLACE "
                f"la ligne entière, il n'y a pas d'élément en place à viser par rang. "
                f"Rien n'a été écrit. Écris chaque colonne-liste entière."])
        # ⚠️ Pas de `colonnes_en_place` ici, et c'est délibéré : l'upsert REMPLACE la
        # ligne. Ranger une annotation sur une colonne qui n'est que dans l'ancienne
        # ligne poserait une couche sur une valeur qui tombe dans le même geste.
        user_data = ranger_les_couches(schema, user_data)
        # oto#140 : le remplacement aussi — avertis, puis REFUSÉS à leur date (J3).
        mdp.controler(self.off_notices, user_data)
        _refuse_dotted_names(user_data)
        refuser_cles_internes(user_data)
        refuser_les_mots_mal_places(schema, user_data)
        _refuse_mixed_layers(schema, user_data)
        # #859 : le remplacement aussi — il ne passe par `_check_row` que sous validation.
        user_data = self._normaliser_les_dates(schema, user_data)
        refuser_cle_metier_vide(schema, user_data)
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
        # oto#204 : le REMPLACEMENT ne fusionne pas — les mots réservés se résolvent ici,
        # contre rien, avant la validation et l'écriture (cf. `append_row`).
        user_data = mots_resolus_a_la_creation(schema, user_data)
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
                   force: Optional[frozenset] = None,
                   upsert: bool = False) -> dict:
        """Écrit un LOT de rows en un appel. Si une clé métier est en vigueur (param
        `key` explicite, sinon `schema.key` déclarée), une row dont la valeur de clé
        existe déjà en base est MODIFIÉE si le lot la DÉSIGNE (`key` nommé, tableau
        fermé), et sinon — un AJOUT — fusionne avec `upsert=True`, avertie puis REFUSÉE
        sans lui à la date de `upsert_implicite` (oto#141). Deux rows du lot à la même
        clé suivent cette dernière règle dans les deux cas. Sinon append. Renvoie un récap {inserted, updated, count,
        key, ids, fusions?}. Résout le datastore UNE fois (write) pour tout le lot."""
        ns_id = self._resolve(datastore, write=True)
        return self._write_rows_to_ns(ns_id, rows, key=key or self.declared_key(datastore),
                                      readonly_override=readonly_override,
                                      origine_override=origine_override,
                                      donnees_d_origine=donnees_d_origine,
                                      force=force, upsert=upsert,
                                      # oto#141 : `key` NOMMÉ = le lot DÉSIGNE.
                                      cle_passee=key is not None)

    def delete_row(self, datastore: str, row_id: str, *,
                   trace: Optional[dict] = None,
                   expected_revision: Any = None) -> None:
        """Supprime une row, sous le VERROU de la ligne — bail, révision, suppression.

        `expected_revision` (oto#217) = la `_revision` de la ligne telle que l'appelant
        l'a lue, quand c'est sur cette lecture qu'il a décidé de supprimer. Différente
        de la révision en place ⇒ `RevisionConflict`, rien n'est supprimé.

        ⚠️ Le relevé de l'état d'avant (`trace`) vient de la ligne VERROUILLÉE, plus
        d'un `get_row` séparé : celui-ci courait avec une écriture concurrente, et la
        garde de bail, posée sur une lecture à part, laissait passer une réservation
        qui s'intercalait entre le contrôle et le delete."""
        attendue = revision_attendue(expected_revision)
        ns_id = self._resolve(datastore, write=True)
        supprimee = db.datastore_delete_row(ns_id, row_id,
                                            lease_guard=self._lease_guard(row_id),
                                            expected_revision=attendue)
        if supprimee is None:
            raise RowNotFound(row_id)
        if trace is not None:
            ns = self._ns_of(ns_id)                  # lu seulement si on relève
            sk = (dsv2.status_field(ns.get("schema")) or {}).get("key")
            self._trace(trace, ns_id, ns, prev_status=supprimee.get(sk) if sk else None)

    def delete_rows(self, datastore: str, items: list) -> dict:
        """Supprime un LOT de rows (#1268) — `items` = `[(row_id, revision_attendue)]`,
        révisions déjà lues par `revision_attendue`.

        Le tableau est résolu UNE fois ; chaque ligne garde SA transaction, son verrou,
        son bail et sa révision (`datastore_delete_row`) — un lot n'est que N gestes
        unitaires sans N allers-retours. Un refus sur une ligne n'arrête pas le lot :
        il est rendu avec sa ligne. ⚠️ Une ligne absente n'est pas un refus : rejouer
        un lot déjà passé rend `not_found`, pas une erreur."""
        ns_id = self._resolve(datastore, write=True)
        deleted, not_found, refused = [], [], []
        for row_id, attendue in items:
            try:
                supprimee = db.datastore_delete_row(
                    ns_id, row_id, lease_guard=self._lease_guard(row_id),
                    expected_revision=attendue)
            except ValueError as e:              # RowLocked, RevisionConflict
                refused.append((row_id, e))
                continue
            (not_found if supprimee is None else deleted).append(row_id)
        return {"deleted": deleted, "not_found": not_found, "refused": refused}
