"""Datastore — substrat natif PostgreSQL (ADR 0016).

Un datastore = une ligne `user_datastores` + ses rows dans `datastore_rows`
(une row = un dict JSONB). Schéma libre : aucune colonne à provisionner, les
champs apparaissent dans `data`. Trois champs auto-managés, exposés à plat dans
la row renvoyée :

- `_id` : identifiant uuid7-like (col `row_id`).
- `_created_at` / `_updated_at` : timestamps (colonnes dédiées).
- `_claims` / `_abandon` / `_abandon_run` : ce que la file de travail sait de la
  ligne — réservations sans écriture, motif si le plafond l'en a sortie (#433), et le
  run de la dernière tentative (#491).

Plus de dépendance Google : la vérité est en base, types préservés nativement
par JSONB (fin de la sentinelle `__j:` de l'ère Sheets). La propriété et le partage
passent par la primitive générique `ownership` (ADR 0030) : un datastore est possédé
par `(owner_type, owner_id)` (user/org/group) et accessible via owner-match ∪ grants
(`resource_grants`). L'export vers un provider tiers (Sheets/Notion…) est une
projection optionnelle, déférée à otomata#29.
"""
from __future__ import annotations

import copy

import base64
import binascii
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.errors import UniqueViolation

from . import acces_agent as aga
from . import ecartes as dsec
from . import layers as dsl
from . import versions as dsver
from . import schema as dsv2
from . import claimable, hors_org
# Les refus : extraits dans `errors` (#325), ré-importés ici pour que tout appelant
# (`from .datastore.core import RowNotFound`) reste inchangé.
from .claimable import RowOutsideClaimable  # noqa: F401
from .schema_ops import SchemaOpsMixin
from .forcage import Forcage
from .reserves import (
    iso_utc,
    poser_origine_systeme,
    refuser_champs_reserves,
)
from .errors import (  # noqa: F401
    BusinessKeyExists,
    BusinessKeyRequired,
    ColonneNonDeclaree,
    InvalidCursor,
    DatastoreAmbigu,
    DatastoreExists,
    DatastoreForbidden,
    DatastoreNotFound,
    DatastoreReadOnly,
    RowClaimed,
    RowLocked,
    RowNotFound,
    RowValidationError,
    TableauParNom,
    est_un_nom_de_tableau,
)
from .. import db, ownership, session_org
from .. import config
from ..db.query import ds_filter_specs as _filter_specs

logger = logging.getLogger(__name__)


# La colonne et ses couches : extraites dans `columns` (#325), ré-importées
# ici pour que les appelants (et les tests qui les exercent) restent inchangés.
from .columns import (  # noqa: E402,F401
    _META_COLS,
    _existing_layers,
    _merge_column,
    _refuse_group_by_compose,
    _refuse_mixed_layers,
    _writes_layers,
    arbitrer_les_vides,
    refuser_geste_sans_effet,
    effacements_report,
    ignores_report,
)
# Les noms POINTÉS (#684/#687) : `ranger_les_couches` referme l'aller-retour —
# ce qu'on sert doit pouvoir être réécrit tel quel — et `_refuse_dotted_names`,
# rétréci, ne tranche plus que ce qui reste sans adresse.
from .points import (  # noqa: E402,F401
    _refuse_dotted_names,
    ranger_les_couches,
)

# Les GREFFONS (07/09/2026) : le store était un fichier de 2 139 lignes, il est
# maintenant un noyau d'identité qui COMPOSE six mixins, sur le modèle de
# `SchemaOpsMixin`. Déplacement pur — tout ce qui s'importait de `datastore.core`
# s'y importe encore, ré-exporté ci-dessous.
from .outils import (  # noqa: E402,F401
    _OFFSET_CURSOR_PREFIX,
    _backquote,
    _current_run,
    _decode_cursor,
    _decode_offset_cursor,
    _encode_cursor,
    _encode_offset_cursor,
    _filter_clauses,
    _new_id,
    _now_iso,
    _ns_url,
    _refus_de_creation,
    indice_de_liberation,
)
from .controles import ControlesMixin  # noqa: E402
from .ecriture import EcritureMixin  # noqa: E402
from .ecriture_par_id import EcritureParIdMixin  # noqa: E402
from .file_de_travail import FileDeTravailMixin  # noqa: E402
from .lecture import LectureMixin  # noqa: E402
from .lots import LotsMixin  # noqa: E402
from .par_filtre import ParFiltreMixin  # noqa: E402
from .registre import RegistreMixin  # noqa: E402


class DatastorePg(SchemaOpsMixin, RegistreMixin, LectureMixin, EcritureMixin,
                  EcritureParIdMixin, ParFiltreMixin, LotsMixin, FileDeTravailMixin,
                  ControlesMixin):
    """Store tabulaire adossé à PostgreSQL.

    State-less, instancié par requête. Normalement à partir du `sub` (l'acteur user) ;
    ou, pour un endpoint MCP agissant sous une org (`acting_org`, secret opt-in), avec
    `sub=None` — l'autorité est alors l'org propriétaire. Résout chaque datastore en
    `ns_id` (possédé OU partagé) et opère sur `datastore_rows`.
    """

    def __init__(self, sub: Optional[str], *, acting_org: Optional[int] = None,
                 allowed_ns_ids: Optional[set] = None, read_only: bool = False):
        self.sub = sub
        self.acting_org = acting_org
        # oto#164 : ce qu'un geste `donnees_d_origine=true` a posé et sauté, cumulé
        # sur le geste (`donnees_d_origine.relever`) — `{colonne: lignes}` pour les
        # posées, `{raison: {colonne: lignes}}` pour les sautées, et les phrases
        # servies dans `notices`, recalculées sur le cumul.
        self.off_origines: dict = {"posees": {}, "sautees": {}, "servies": set()}
        # Scope dur (endpoint partagé) : None = pas de restriction ; set = ces ns_ids seuls.
        self.allowed_ns_ids: Optional[set] = (None if allowed_ns_ids is None
                                              else {int(x) for x in allowed_ns_ids})
        self.read_only = bool(read_only)
        self._active_scope_cache: Optional[tuple[list[int], list[int]]] = None
        # Le DERNIER tableau résolu par ce store : `{"ns_id", "datastore"}` — le
        # numéro et le nom CANONIQUE, pris dans la ligne `user_datastores` que
        # `_resolve` vient de lire, donc sans une requête de plus. C'est ce qui permet
        # à une remise de porter l'IDENTITÉ du tableau au lieu de l'ÉCHO de l'adresse
        # reçue (cf. `identite.py`) : `data_rows("600")` répondait `datastore: "600"`.
        # Même portée que les relevés ci-dessous — un store par requête — et un geste
        # du datastore résout UN tableau : le « dernier » est donc le sien. À lire
        # juste après l'appel au store, jamais gardé d'un geste à l'autre.
        self.dernier_tableau: Optional[dict] = None
        # Les relevés du geste, union sur un lot : remplis par `_check_row`, lus par
        # les surfaces via `off_schema_report()`. Le store est instancié par requête,
        # donc la portée est celle du geste.
        # Colonnes dont la valeur EN BASE ne passe plus le type déclaré, rencontrées
        # en écrivant AILLEURS sur la même ligne. `{champ: refus}` — le refus qu'on
        # aurait rendu, gardé pour que l'agent sache quoi y écrire s'il veut réparer.
        self.off_geles: dict = {}
        self.off_notices: set = set()
        # ⚠️ **Un fait qui change la NATURE du résultat, pas un relevé de routine.**
        # `{colonne: refus}` — la clé métier qu'une ligne CRÉÉE ne porte pas, donc que
        # rien ne pourra jamais rapprocher.
        #
        # Il vivait dans `off_notices`, et le message y était exact : il nommait la
        # colonne et le geste de remplacement. Mesuré le 09/09/2026 : servi DIX FOIS en
        # une soirée, il n'a rien empêché — le consommateur lisait le statut et l'`_id`,
        # comme n'importe quel consommateur raisonnable, et `notices` sonne comme
        # « informations diverses ». **172 500 jetons pour un texte juste, rangé à un
        # endroit que personne n'ouvre.**
        #
        # Le critère qui décide de la place, et il vaut pour la suite : *un consommateur
        # qui ne lit que le statut et l'identifiant serait-il trompé ?* Ici oui — la
        # réponse annonce un succès avec un `_id`, et cette ligne-là est orpheline. Les
        # relevés de routine (`hors_type`, `valeurs_ecartees`…) ne trompent
        # personne sur la nature du résultat : ils restent en annexe.
        self.off_non_rapprochables: dict = {}
        # Ce que ce geste a VIDÉ (#407/#408/#409) : les colonnes qu'il nomme avec un
        # `null` alors qu'elles portaient quelque chose, et la valeur perdue.
        # Même portée que les relevés ci-dessus (un store par requête), même union
        # sur un lot — mais une LISTE : la ligne fait partie de l'information.
        self.off_erased: list = []
        # #667 : les valeurs que le geste a posées et que le schéma REFUSE, écartées
        # pour que le reste de la ligne s'écrive. Sixième liste, distincte des cinq
        # autres — celles-ci ne sont NI en base NI perdues en silence.
        self.off_rejected: list = []
        # Et ce qu'il aurait vidé sans la règle de #608 : les colonnes qu'il nomme
        # avec un vide non-`null` par-dessus une valeur en place. Liste SÉPARÉE
        # d'`off_erased` — l'une nomme ce qui n'est plus, l'autre ce qui est resté.
        self.off_ignored: list = []
        # #658 : les colonnes VERROUILLÉES que ce geste a remplacées de force, avec
        # la ligne et la valeur d'avant. Même portée que les relevés ci-dessus (un
        # store par requête). Deux lecteurs, parce qu'il y a deux journaux : la face
        # MCP le verse au relevé d'appel (`note_call_trace`, no-op ailleurs), la face
        # REST le lit ici pour sa propre ligne (`datastore_journal.record`).
        self.off_forced: list = []

    # --- résolution datastore -> ns_id ---------------------------------------

    def _org_de_l_appel(self) -> Optional[int]:
        """L'org active de l'appel (oto#160) — celle qu'un tableau PERSONNEL créé par ce
        geste retient (`user_datastores.context_org_id`), où il se liste pour son
        propriétaire (`ownership.mes_tableaux_ici`, 29/09/2026).
        Même source que `_active_scope`, qui scope la liste et la résolution par nom."""
        if self.acting_org is not None:
            return int(self.acting_org)
        from .. import access
        return access.current_org(self.sub)

    def _active_scope(self) -> tuple[list[int], list[int]]:
        """Contexte de l'ORG ACTIVE (ADR 0023) : `([org active], [mes groupes dans cette
        org])`. La résolution par NOM scope là-dessus — comme `list_datastores` — de sorte
        qu'un datastore d'une AUTRE de mes orgs ne se résout plus hors de son org (fuite
        cross-org, symétrique au fix projets). L'ownership PERSO (`owner=user`) et les
        grants perso (`principal user`) suivent l'acteur : ils n'appartiennent à aucune
        org, donc ne sont pas une fuite d'org — `resolve_datastore_ns` les garde via `sub`."""
        if self._active_scope_cache is None:
            if self.acting_org is not None:
                # Endpoint agissant-org (sub-less) : contexte = l'org propriétaire seule,
                # aucun groupe (pas de membre → pas de scope de groupe).
                self._active_scope_cache = ([int(self.acting_org)], [])
                return self._active_scope_cache
            from .. import access, group_store
            oid = access.current_org(self.sub)
            if oid is None:
                self._active_scope_cache = ([], [])
            else:
                org = int(oid)
                # ADR 0049 (cadrage 10/07) : les groupes du contexte = mes équipes dans
                # l'org active — ou TOUS les groupes de l'org pour un org_admin (même
                # escalade que `roles.can_read_group`, alignée sur `oto_project op=list`).
                from .. import roles
                if roles.is_org_admin(self.sub, org):
                    groups = [int(g["id"]) for g in group_store.list_groups(org)]
                else:
                    groups = [int(g["group_id"])
                              for g in group_store.list_groups_for_user(self.sub, org)]
                # Un bénéficiaire HORS de l'org (projet partagé, `_project=`) n'a pas
                # l'org pour portée : ni ses tableaux par nom ou numéro, ni ce qui lui
                # est accordé (`ownership.owner_de_portee`, même seam que les listes).
                portee = [org] if ownership.owner_de_portee(self.sub, org) else []
                self._active_scope_cache = (portee, groups)
        return self._active_scope_cache

    def _resolve(self, datastore: str, *, write: bool = False) -> int:
        """ns_id d'un datastore VISIBLE DANS L'ORG ACTIVE (possédé par elle, perso, ou
        accordé à son contexte). `write=True` exige le droit d'écriture via
        `ownership.can_access`. L'adresse de l'APPEL : le journal et `dernier_tableau` la
        retiennent (`_trouver` seul ne retient rien)."""
        ns = self._trouver(datastore, write=write)
        ns_id = int(ns["id"])
        # Le journal cite l'ENTITÉ, pas la chaîne tapée : `data_write("160")` et
        # `data_write("slot:vivier")` visent le même tableau.
        # Consigné APRÈS les gardes (un datastore refusé ne laisse pas de trace) ;
        # no-op hors appel MCP — la face REST tient déjà son propre relevé.
        session_org.note_call_trace(ns_id=ns_id, ns_name=ns.get("datastore"))
        # Le journal garde la FORME de la désignation (`ref_kind`), jamais le nom : ici
        # toujours un numéro (un nom est refusé par `_trouver`, `slot:` noté à sa résolution).
        session_org.noter_forme_tableau(session_org.REF_NUMBER)
        # Le MÊME couple, gardé sur le store, pour les REMISES : le relevé d'appel
        # ci-dessus est muet hors MCP (REST, stdio, tests) et n'alimente que le
        # journal. Les deux valeurs sont dans la ligne déjà lue — l'identité ne coûte
        # donc rien de plus que la résolution elle-même.
        self.dernier_tableau = {"ns_id": ns_id, "datastore": ns.get("datastore")}
        return ns_id

    def _trouver(self, datastore: str, *, write: bool = False) -> dict:
        """La ligne du tableau que `_resolve` résout, sous les MÊMES gardes, sans rien
        retenir de l'appel : ni journal, ni avis, ni `dernier_tableau`. Pour regarder
        un AUTRE tableau que celui de l'appel (la cible d'un lien, `schema_ops`) sans
        que la remise ni le journal ne le prennent pour lui.

        Un NOM de tableau est refusé avant toute lecture (`TableauParNom`, retrait du
        08/11/2026) : seuls le numéro et `slot:<nom>`, déjà résolu en numéro, désignent."""
        if est_un_nom_de_tableau(datastore):
            raise TableauParNom(str(datastore))
        par_app = self._par_l_app(datastore, write)
        if par_app is not None:
            return par_app
        org_ids, group_ids = self._active_scope()
        try:
            ns = db.resolve_datastore_ns(
                datastore, sub=self.sub, org_ids=org_ids, group_ids=group_ids)
        except db.AdresseAmbigue as e:
            raise DatastoreAmbigu(e.adresse, par_id=e.par_id, par_nom=e.par_nom) from None
        # Vue bornée (oto#270) : ni le tableau tenu par un run, ni l'indice d'une autre
        # org — les deux sortent de O par construction.
        borne = ownership.vue_bornee() if self.acting_org is None else None
        if not ns and borne is None:
            # #631 : le run sait où il travaille — sa réservation porte le tableau.
            ns = hors_org.tenu_par_le_run(self.sub, datastore)
        if not ns:
            raise DatastoreNotFound(datastore, indice=None if borne is not None else (
                hors_org.indice_autre_org(self.sub, datastore,
                                          org_ids[0] if org_ids else None)))
        ns_id = int(ns["id"])
        if borne is not None and not ownership.visible_in_org(
                self.sub, borne, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id)):
            raise DatastoreNotFound(datastore)
        # Scope dur d'endpoint partagé : hors des tableaux liés au projet ⇒ invisible
        # (anti-fuite #193 ; DatastoreNotFound plutôt que Forbidden — on ne divulgue pas
        # l'existence d'un datastore hors périmètre).
        if self.allowed_ns_ids is not None and ns_id not in self.allowed_ns_ids:
            raise DatastoreNotFound(datastore)
        if write and self.read_only:
            raise DatastoreReadOnly(datastore)
        if write:
            ok = (ownership.org_can_access(self.acting_org, ownership.TYPE_RESSOURCE_DATASTORE,
                                           str(ns_id), "write")
                  if self.acting_org is not None
                  else ownership.can_access(self.sub, ownership.TYPE_RESSOURCE_DATASTORE,
                                            str(ns_id), "write"))
            if not ok:
                raise DatastoreReadOnly(datastore)
        return ns

    def _par_l_app(self, datastore: str, write: bool) -> Optional[dict]:
        """Le tableau tenu À TRAVERS l'app ouverte (`datastore/par_l_app.py`), quand le
        lecteur ne le tient pas lui-même assez : dans la portée d'un appel d'app
        seulement, pour une personne (jamais un store d'org ni un périmètre
        d'endpoint), pour un numéro. None = la résolution ordinaire décide."""
        from . import par_l_app

        if (self.sub is None or self.acting_org is not None or self.allowed_ns_ids is not None
                or not str(datastore).isdigit()):
            return None
        # Hors d'un appel d'app ou d'un run : rien à servir, rien de lu (le cas courant).
        if not par_l_app.peut_servir(self.sub):
            return None
        # Le lecteur tient-il déjà ce qu'il faut ? La résolution ordinaire le sert.
        if ownership.can_access(self.sub, ownership.TYPE_RESSOURCE_DATASTORE,
                                str(datastore), "write" if write else "read"):
            return None
        d = par_l_app.droit(self.sub, str(datastore))
        if d is None or (write and d != "write"):
            return None
        if write and self.read_only:
            raise DatastoreReadOnly(datastore)
        ns = db.get_datastore_by_id(int(datastore))
        if ns is not None:
            par_l_app.servi(self.sub, str(datastore))
        return ns

    @staticmethod
    def _row_to_dict(row: dict, schema: Optional[dict] = None, *,
                     bail_echu: str = "taire", layers: str = dsl.DEFAUT,
                     versions: tuple = dsver.DEFAUT,
                     empties: str = dsl.EMPTIES_DEFAUT,
                     fields: Optional[frozenset] = None,
                     declarees: Optional[list] = None) -> dict:
        """Ligne `datastore_rows` → row API (`_id`/`_created_at`/`_updated_at` à
        plat + champs user). Le bail de claim (ADR 0046 D) n'apparaît que s'il est
        posé (une ligne libre n'a aucune des trois clés `_claimed_*` → absentes,
        pas None).

        `fields` (oto-backend#980, lot 2) : projection descendue ICI depuis le
        seul point qui fabrique les couches aplaties (`dsv2.flat_layers`) — la
        payer pour une colonne qu'on va jeter juste après était le coût mesuré
        (~38 000 appels sur une page de 500 lignes × ~90 colonnes déclarées,
        pour un `fields` qui n'en demandait que quelques-unes). `None` = pas de
        projection, comportement inchangé (tous les appelants existants). Sinon,
        `_id` reste TOUJOURS servi (adressage de la ligne) ; toute autre clé —
        colonnes déclarées, couches aplaties, métadonnées `_created_at`/
        `_revision`/`_claimed_*`/`_claims`/`_abandon` — n'est produite QUE si son
        nom exact figure dans `fields`, exactement le calcul que faisait
        l'ancien filtrage a posteriori (`_project_row`), jamais un filtre plus
        permissif : une couche (`champ.origine`) ne survit à la projection que
        nommée elle-même, pas parce que `champ` l'est.

        `layers` (oto#53) : la forme des cellules à couches — `flat` (défaut) les
        aplatit à côté du nom nu, `nested` les rend comme elles s'écrivent. Le défaut
        vit dans `layers.DEFAUT`, pas ici.

        `versions` (oto#140) : les VERSIONS servies — `current` (ce qu'on a établi) et
        `origine` (ce que la cliente a remis). Le défaut vit dans `versions.DEFAUT`.

        `empties` (oto#204) : la forme d'une case au vide ASSUMÉ — `plain` (défaut) la
        sert `""`, `sentinel` la sert `"@empty"`, le mot qui la réécrit. Le défaut vit
        dans `layers.EMPTIES_DEFAUT`.

        ⚠️ **Le NOM NU rend toujours la version courante, quelle que soit la demande.**
        Faire porter deux sens à `champ` selon un paramètre serait exactement le piège
        qu'on retire ailleurs du produit : un mot, deux choses. `versions` décide donc
        de ce qui S'AJOUTE — demander `origine` fait apparaître `champ.origine` et ses
        sous-champs, ne pas la demander les fait disparaître, et `champ` ne bouge
        jamais.

        **Masquage agent (oto#83)** : les colonnes `agent_access: "none"` sont retirées
        ICI, au seul endroit par lequel passe TOUTE ligne servie par le store — douze
        sites d'appel, neuf méthodes (`append_row`, `get_row`, `list_rows`,
        `cursor_rows`, `page_rows`, `update_row`, `claim_next`, `claim_row`, `queue`).
        Retirées **avant** la projection, pas après : leurs couches (`champ.origine`) et
        leurs alias plats (`contact1_nom`) ne sont donc jamais fabriqués, plutôt que
        fabriqués puis rattrapés par un filtre de noms qu'on oublierait d'étendre.
        Inerte hors face agent — le tableau de bord de son propriétaire voit tout.

        `declarees` : `dsv2.cles_declarees(schema)`, calculé UNE fois par page par
        l'appelant qui sert une page (16/10/2026, profil d'une page de 500 lignes à 260
        colonnes : recalculé à chaque ligne, il pesait ~20 % du temps de la page).
        `None` = calculé ici, comportement inchangé pour les lectures d'une ligne."""
        data = row.get("data") or {}
        # Lu une fois par ligne. Le prédicat court-circuite sur la face REST, où les
        # pages sont les plus grosses : rien n'est parcouru là où rien n'est masqué.
        cachees = aga.masquees(schema) if aga.appel_d_agent() else frozenset()
        projette = fields is not None
        out = {"_id": row["row_id"]}
        if not projette or "_created_at" in fields:
            out["_created_at"] = row["created_at"]
        if not projette or "_updated_at" in fields:
            out["_updated_at"] = row["updated_at"]
        # La RÉVISION (12/09/2026), en chaîne : ce que `expected_revision` recopie. Toute
        # requête de `db/` qui projette une ligne sélectionne `rev` — un cliquet le tient
        # (`tests/datastore/test_revision_de_ligne.py`). Servie si lue : une ligne sans
        # `rev` est un faux de banc, pas un chemin servi.
        if "rev" in row and (not projette or "_revision" in fields):
            out["_revision"] = str(row["rev"])
        # Toute colonne a des sous-champs (#318) — c'est le contrat du datastore, pas
        # une forme que certaines valeurs adoptent. Une colonne « plate » est une
        # colonne dont les sous-champs sont VIDES, et on ne rend pas du vide.
        #
        # Le NOM NU rend donc toujours la valeur : un lecteur qui fait `row["email"]`
        # reçoit un e-mail, qu'il y ait une provenance ou non. Les sous-champs
        # renseignés s'ajoutent à plat sous `champ.couche` — visibles sans être
        # imposés, et projetables par `fields` comme n'importe quelle colonne.
        # ⚠️ **Une relique littérale VIDE ne doit pas masquer la couche du même nom.**
        #
        # Une clé littérale pointée (`{"qualification.comment": …}` au premier niveau,
        # écrite avant la garde du 31/08/2026) porte EXACTEMENT le nom que `flat_layers`
        # fabrique pour la couche. Les deux atterrissent donc sur la même clé de `out`,
        # et la dernière écrite gagne — en JSONB les clés sont triées par LONGUEUR, donc
        # `qualification` est parcourue avant `qualification.comment` : la relique
        # écrase toujours la couche.
        #
        # Mesuré sur la production le 09/09/2026, et c'est le coût réel des reliques :
        # **732 valeurs invisibles à leurs lecteurs** sur un tableau de campagne — 497
        # `qualification.comment` et 235 `retraitement.comment`, ces dernières portant
        # des traces de retrait pour conformité (« adresse retirée, source hors
        # contrat »). Le texte est en base, servi `None`. Une trace qu'on ne peut pas
        # lire ne prouve rien.
        #
        # ⚠️ **Une relique qui porte une VALEUR continue de gagner**, et c'est délibéré :
        # c'est le comportement figé par le banc, et sur 23 cellules du parc la relique
        # est la SEULE à porter la donnée — la masquer la perdrait. On ne corrige que
        # le cas où l'écrasement remplace quelque chose par rien.
        #
        # Le pré-calcul ne coûte que là où le danger existe : deux tableaux du parc
        # portent des reliques, tous les autres sortent sur le test de présence.
        #
        # oto-backend#980 (suite, profil GIL de prod, 18/09/2026) : `any(f.startswith(k
        # + ".") ...)` était réévalué pour CHAQUE clé de la ligne, en reparcourant
        # `fields` à chaque fois — quadratique en (clés de la ligne × colonnes de
        # `fields`). Avec `fields` à 144 colonnes, c'était la première feuille de code
        # applicatif au profil GIL (~9%), devant `_row_to_dict` elle-même. Les BASES de
        # `fields` (`"email.origine"` → base `"email"`) ne changent pas d'une clé à
        # l'autre : les calculer UNE FOIS par ligne, puis tester l'appartenance à cet
        # ensemble, passe de O(clés × champs) à O(clés + champs).
        bases_demandees, bases_a_couches = (_bases_projetees(fields) if projette
                                            else (frozenset(), frozenset()))

        def _pertinente(k: str) -> bool:
            """`k` (ou une de ses couches, `k.origine`…) est demandée — seul cas où
            `flat_layers(k, …)` vaut la peine d'être calculée sous projection."""
            return not projette or k in bases_demandees

        couches_servies: set = set()
        if layers != dsl.NESTED and any(
                isinstance(k, str) and "." in k for k in data):
            for k, v in data.items():
                if k in _META_COLS or k in cachees or not _pertinente(k):
                    continue
                # `origine=True` : on relève les couches que la cellule PORTE, pour
                # qu'une relique vide ne les masque pas — servies ou non, ce relevé
                # ne sert rien lui-même (comportement d'avant oto#273, inchangé).
                couches_servies.update(dsv2.flat_layers(k, v, origine=True))

        sentinelle = empties == dsl.SENTINEL
        sert_origine = dsver.sert_l_origine(versions)
        for k, v in data.items():
            if k in _META_COLS or k in cachees or not _pertinente(k):
                continue
            # `layers="nested"` (oto#53) : la cellule revient comme elle s'écrit,
            # `{valeur, origine, comment, link}` — rien n'est aplati à côté. `versions`
            # s'y applique comme à plat (oto#273) : `origine` n'y est que demandée, sans
            # quoi `versions_servies` annoncerait `["current"]` sur une origine servie.
            if layers == dsl.NESTED:
                if not projette or k in fields:
                    out[k] = dsl.nested_value(v, origine=sert_origine,
                                              sentinelle=sentinelle)
                continue
            # `served_value` descend dans une colonne-tableau : chaque attribut d'item
            # est une feuille, rendue comme telle (oto#22 §1).
            #
            # `versions` (oto#273) se décide DANS `flat_layers`, le point unique qui
            # fabrique ces noms, et pas ici après coup : le filtre qui vivait ici ne
            # voyait que le premier niveau, et `item["email.origine"]` fuyait un cran
            # plus bas, sous `versions_servies: ["current"]`.
            servie = dsv2.served_value(v, origine=sert_origine, sentinelle=sentinelle)
            if (not projette or k in fields) and not (
                    k in couches_servies and dsv2.est_vide(servie)):
                out[k] = servie
            # Les couches s'exposent dès qu'il y en a — même sans `valeur` posée
            # (import de socle sur un champ pas encore renseigné).
            if projette and k not in bases_a_couches:
                # Aucune couche de `k` n'est demandée : rien à aplatir. C'est ce qui
                # rend une projection sur des noms NUS moins chère que la ligne
                # complète — `flat_layers` était payé pour chaque colonne puis jeté.
                continue
            plat = dsv2.flat_layers(k, v, origine=sert_origine)
            if projette:
                # Une couche ne survit que NOMMÉE elle-même — demander `email` ne
                # fait pas apparaître `email.origine` (même règle que l'ancien
                # filtrage a posteriori, `_project_row`).
                plat = {n: val for n, val in plat.items() if n in fields}
            out.update(plat)
        # oto#182 — toute colonne DÉCLARÉE est servie, à `null` quand aucune valeur n'est
        # en place : une clé absente se lisait « cette colonne n'existe pas » et l'agent
        # fabriquait la valeur. Rien n'est écrit ; une valeur présente (même `""`, `null`
        # ou une couche seule) n'est pas touchée ; une colonne masquée reste absente.
        for k in (dsv2.cles_declarees(schema) if declarees is None else declarees):
            if (k not in data and k not in out and k not in cachees
                    and not k.startswith("_") and (not projette or k in fields)):
                out[k] = None
        # ⚠️ Un bail EXPIRÉ n'est pas une réservation — mesuré le 01/09/2026 sur un
        # fichier de production : **495 lignes sur 8 910 portaient `_claimed_by`, et
        # les 495 étaient expirées**, la plus ancienne depuis dix-huit jours, au nom
        # de travailleurs d'une campagne close.
        #
        # La garde le savait déjà (`datastore_active_lease` : « expiré compte pour
        # libre ») ; la lecture, non — elle servait le nom d'un travailleur mort
        # comme une réservation en cours. **Deux lectures voisines de la même donnée,
        # une seule connaissait la règle.**
        #
        # **C'est POSTGRESQL qui tranche, et c'est le fond du correctif.** La
        # fraîcheur arrive en colonne calculée (`claim_active`), du même prédicat et
        # sur la même horloge que la garde : la lecture et la garde ne se ressemblent
        # pas, elles PARTAGENT la règle. Une comparaison refaite en Python serait une
        # SECONDE implémentation — et elle était fausse en germe : comparer les
        # horodatages en TEXTE n'est juste que tant que `_normalize_value` émet un
        # séparateur espace sans fuseau. Le jour où un chemin rendrait un `T`
        # (0x54 > 0x20), tout bail se serait lu ACTIF, en silence et sans rien rougir.
        #
        # Lu par CLÉ, comme `claimed_run` : un SELECT qui oublierait la fraîcheur doit
        # LEVER, jamais servir un bail mort comme s'il courait encore.
        if row.get("claimed_by") is not None:
            actif = row["claim_active"]
            # `bail_echu="servir"` — l'EXCEPTION, et elle est unique : la file de
            # supervision (`DatastorePg.queue`). Son contrat, écrit à trois endroits
            # et ANTÉRIEUR à ce lot, est de rendre le bail « actif OU expiré, le
            # consommateur tranche sur `_claimed_until` ». Neutraliser le bail ici
            # pour tout le monde lui retirerait ce sur quoi trancher : l'écran
            # compterait les lignes mortes « sous bail » avec un compteur d'échus à
            # zéro, et le bouton « Libérer » — gaté sur `_claimed_by` — disparaîtrait
            # sur les lignes qu'il faut justement libérer. Le défaut vaut mieux SÛR :
            # un chemin de lecture neuf tait le bail mort sans avoir à y penser.
            if bail_echu == "servir" or actif:
                if not projette or "_claimed_by" in fields:
                    out["_claimed_by"] = row["claimed_by"]
                if not projette or "_claimed_until" in fields:
                    out["_claimed_until"] = row.get("claimed_until")
                # LE RUN qui tient ce bail — ce qui lie un travail à la LIGNE qu'il
                # travaille. `null` = bail pris SANS run (une personne sur la file du
                # dashboard) : un fait, pas un trou.
                if not projette or "_claimed_run" in fields:
                    out["_claimed_run"] = row["claimed_run"]
        # Ce que la file sait de la ligne (#433). Rendus SEULEMENT s'ils portent
        # quelque chose : un `_claims: 0` sur chaque ligne de chaque tableau serait
        # du bruit dans toutes les lectures, pour une file que la plupart n'ouvrent
        # jamais. Voir « déjà tentée 2 fois » est ce qui change une décision.
        if row.get("claims") and (not projette or "_claims" in fields):
            out["_claims"] = row["claims"]
        if row.get("abandon_reason") and (not projette or "_abandon" in fields):
            out["_abandon"] = row["abandon_reason"]
        # Le run de la dernière tentative (#491) — à côté du motif, jamais dedans :
        # `_abandon` reste une phrase, ce champ-ci se rapproche du journal sans la lire.
        if row.get("abandon_run") and (not projette or "_abandon_run" in fields):
            out["_abandon_run"] = row["abandon_run"]
        return out

    # --- schéma v2 : validation d'écriture + cycle de vie (ADR 0046) ---------

    def _ns_of(self, ns_id: int) -> dict:
        """La ligne `user_datastores` (nom canonique + schéma + propriétaire)."""
        return db.get_datastore_by_id(ns_id) or {}

    def _schema_of(self, ns_id: int) -> Optional[dict]:
        return self._ns_of(ns_id).get("schema")

    def _trace(self, trace: Optional[dict], ns_id: int, ns: dict,
               *, prev_status: Any = None) -> None:
        """RELEVÉ du geste pour le journal (seam ADR 0046 b4) : ce que la surface REST
        doit savoir, pris DANS la mutation qui l'a déjà calculé.

        ⚠️ `prev_status` **doit** venir d'ici et pas d'une relecture séparée : c'est
        l'état sur lequel la transition a été VALIDÉE. Une relecture faite avant
        l'appel court avec un write concurrent (un agent qui bouge la ligne entre
        les deux) et ferait proposer au cockpit une annulation vers un état que la
        ligne n'a jamais eu. Bénéfice second : zéro requête ajoutée (D2)."""
        if trace is None:
            return
        schema = ns.get("schema")
        trace.update({
            "ns_id": int(ns_id),
            "datastore": ns.get("datastore"),
            "status_key": (dsv2.status_field(schema) or {}).get("key"),
            "title_key": (dsv2.title_field(schema) or {}).get("key"),
            "prev_status": prev_status,
        })

    @staticmethod
    def _declared_key_of(schema: Optional[dict]) -> Optional[str]:
        k = (schema or {}).get("key")
        return k if isinstance(k, str) and k else None


def _bases_projetees(fields: frozenset) -> tuple[frozenset, frozenset]:
    """`(bases, bases à couches)` d'une projection : les colonnes que `fields` vise
    (`"email.origine"` → `"email"`), et celles dont une COUCHE est nommée — en UN
    parcours de `fields` par ligne, jamais un par clé de la ligne (oto-backend#980)."""
    bases, a_couches = set(), set()
    for f in fields:
        base, point, _ = f.partition(".")
        bases.add(base)
        if point:
            a_couches.add(base)
    return frozenset(bases), frozenset(a_couches)


def make_store(sub: str) -> "DatastorePg":
    """Construit un store PG pour `sub`. Plus aucune dépendance externe (ADR 0016)
    — datastore est une surface plateforme self-contained."""
    return DatastorePg(sub)


def make_org_store(org_id: int, *, allowed_ns_ids: Optional[set] = None,
                   read_only: bool = False) -> "DatastorePg":
    """Store agissant SOUS L'AUTORITÉ d'une ORG, sans user (`sub=None`). Sert un
    endpoint MCP `secret` opt-in datastore (ADR 0032) : la résolution de datastore et
    le droit d'écriture se décident sur le principal ORG (owner-match / grant d'org),
    jamais sur un membre. N'expose PAS la gouvernance (create/delete/rename/share) —
    ces actes restent réservés à un user identifié (tools sub-only).

    `allowed_ns_ids` (non None) = **scope dur** : seuls ces datastores sont listables/
    résolvables — les tableaux LIÉS au projet partagé (anti-fuite #193 : sans ce scope
    l'endpoint exposerait TOUT le datastore de l'org). Set vide ⇒ rien d'exposé.
    `read_only=True` ⇒ l'écriture (`data_write`/`data_set_schema`) lève `DatastoreReadOnly`."""
    return DatastorePg(None, acting_org=int(org_id),
                       allowed_ns_ids=allowed_ns_ids, read_only=read_only)
