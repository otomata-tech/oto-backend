"""Datastore v2 — schéma structuré : validation d'écriture + cycle de vie (ADR 0046).

Module PUR (aucun I/O) : le schéma d'un datastore (colonne `user_datastores.schema`)
s'étend au-delà du rendu (0016) avec quatre couches OPT-IN :

- **types imbriqués** : `type: "object"` (+ `fields: [...]`) et `type: "list"`
  (+ `of: <field-def>` — scalaire ou sous-record) décrivent une *fiche* (occupant,
  `contacts[]`, `signaux[]`) que le blob JSONB porte déjà ;
- **référentiel fermé sous `strict`** : dans un composite DÉCLARÉ (`object.fields`,
  `list.of.fields`), un attribut que la déclaration ne nomme pas est REFUSÉ, en
  nommant l'élément (`contacts[1].email_pattern`). Le premier niveau, lui, reste
  ouvert : une clé inconnue y crée une colonne libre (contrat 0016) et n'est que
  SIGNALÉE (`hors_schema`, #294) ;
- **validation à l'écriture** : `field.required`, conformité de type,
  `field.required_when: {<champ>: <valeur>}` (le guard-rail : livrables requis
  quand `status = "qualified"`) et `field.max_length` (borne de longueur — un
  intitulé de poste n'est pas un paragraphe de raisonnement) — active si
  `schema.strict` OU si un field déclare required/required_when/max_length ;
- **cycle de vie** : `lifecycle: {states, transitions, terminal?}` sur le field
  `role="status"` — état inconnu ou transition non déclarée = refus ;
- **états terminaux** : `terminal` explicite, sinon dérivés (état sans transition
  sortante) — le store libère le claim de file de travail en y entrant ;
- **plafond de reprises** : `lifecycle.max_claims` + `lifecycle.abandon_state` —
  une ligne réservée N fois sans qu'une écriture n'aboutisse quitte la file ;
- **périmètre de réservation** : `lifecycle.claimable: {col: val}` — ce que la file
  SERT, quel que soit le `filter` de l'appelant (décision dans `claimable.py`).

Défaut (aucune de ces clés) = comportement 0016 inchangé : schéma de rendu SOFT.
Les erreurs sont des *listes de messages actionnables* — le store les joint dans
une ValueError, jamais un refus muet.

## Ce fichier est une FAÇADE, et il n'a plus de corps

Le domaine vit dans douze modules à côté, un par métier ; ce fichier ne fait que les
ré-exporter. Une cinquantaine de sites importent `datastore.schema` — `dsv2.validate_row`,
`dsv2.LAYER_KEYS`, `S.unwrap`… — et aucun n'a changé : la façade tient ce contrat, et
c'est ce qui rend la coupe relisable (un fichier, pas cinquante appelants).

**Où écrire, désormais** — la question à se poser est *quel métier*, pas *quel fichier* :

| module | ce qu'il tient |
|---|---|
| `couches.py` | le vocabulaire des couches d'une cellule et leurs formes |
| `motifs.py` | le COÛT d'un `pattern` — la garde qui empêche un motif de figer le serveur |
| `declaration.py` | LIRE une déclaration : « que déclare ce schéma ? » |
| `cycle_de_vie.py` | états, transitions, terminaux, plafond de reprises, périmètre |
| `hors_schema.py` | une clé que la déclaration ne nomme pas — signalée en haut, refusée dessous |
| `champs_reserves.py` | `readonly` / `origine: system` / `agent_access`, et le préavis daté |
| `definition.py` | valider le SCHÉMA lui-même, à la pose |
| `couches_exigees.py` | `required_layers` — ce que la valeur doit porter avec elle |
| `validation.py` | VALIDER une ligne à l'écriture, et ses textes de refus |
| `effacements.py` | fusionner / retirer un format, et le relevé de ce qui a disparu |
| `vocabulaire.py` | ce que CETTE version lit et fait respecter, dérivé du code |
| `non_applique.py` | ce qu'un tableau déclare et que la plateforme laisse inerte |

⚠️ **Un nouveau module qui LIT un attribut de colonne doit être ajouté à la liste de
fichiers de `vocabulaire._read_keys`** — sinon le vocabulaire dérivé le déclare mort et
l'avertissement accuse une clé parfaitement lue.
"""
from __future__ import annotations

# Ces imports ne servent plus ici : ils font PARTIE de la surface ré-exportée — un
# appelant qui lisait `schema.Optional` ou `schema.aga` continue de la voir. Les
# retirer serait un changement de contrat déguisé en ménage.
from datetime import date as _date, datetime as _datetime, timezone as _timezone

from . import schema_keys

import re
from collections.abc import Iterator
from functools import lru_cache
from datetime import datetime
from typing import Any, Optional

from . import acces_agent as aga
from . import claimable
from . import forcage as fcg

from .couches import (
    VALUE_LAYER, ORIGIN_LAYER, LAYER_KEYS, ALL_LAYER_KEYS, VALUE_BOUND_LAYERS,
    SYSTEM_ORIGIN, ORIGINE_INCONNUE, same_value, names_layers, unknown_layers, unwrap,
    split_layer, layer_value, flat_layers, layer_address, served_value, _served_item,
    _is_empty, est_vide,
)
from .motifs import (
    PATTERN_MAX_SRC, PATTERN_MAX_SUBJECT, PATTERN_BUDGET, _re_parser, _PATTERN_REFUSES,
    _PATTERN_FEUILLES, _MotifTropCher, _op_name, _sub_budget, _node_budget,
    pattern_refusal, _pattern_re,
)
from .declaration import (
    SCALAR_TYPES, COMPOSITE_TYPES, _fields, declares_field, _walk_fields, max_length_of,
    pattern_of, top_level_bounds, top_level_keys, cles_declarees, top_level_options, order_spec,
    champ_declare,
    status_field, DISPLAY_TITLE, title_field, validation_active,
    key_required_of, readonly_fields, system_origin_fields, top_level_patterns,
)
from .cycle_de_vie import (
    lifecycle_of, terminal_states, is_terminal_status, max_claims_of, abandon_state_of,
    claimable_of, refus_de_transition, merge_transitions, merge_lifecycle,
    queue_release_warning,
)
from .hors_schema import (
    off_schema_keys, _unknown_subkeys, _unknown_subkey_refusal, _off_schema,
    off_schema_warning, types_geles_warning, UNKNOWN_FIELDS_MODES, _REFERENTIEL_CITE,
    unknown_fields_mode, couche_mal_ecrite, enveloppe_probable, off_schema_refusal,
)
from .champs_reserves import (
    PARAMETRE_ORIGINE, ORIGINE_REFUS_LE, ENV_ORIGINE_REFUS_LE, _MOIS_FR, date_refus,
    date_refus_fr, description_parametre_origine, _en_francais, refus_arme,
    _les_deux_gestes, avertissement_origine, refus_origine, origine_posee,
    reserved_refusals, _origine_attendue, marqueurs_poses_warning,
)
from .definition import (
    validate_schema_def, _erreurs_unknown_fields, _COLUMN_ONLY_KEYS,
    _validate_reserved_def, _validate_fields_def,
)
from .couches_exigees import (
    _A_QUOI_SERT_LA_COUCHE, required_layers_of, _refus_de_couche,
    _couches_exigees_errors, _couches_exigees_sous, couches_manquantes,
)
from .phrases_de_refus import (
    _forme_attendue, _gated_by, _cause_required_when, _clause_aiguillage,
)
from .validation import (
    _NUM_RE, _type_error, _row_errors, validate_row,
)
from .effacements import (
    merge_fields, remove_fields, remove_field_attrs, _DECL_STRUCTURELLES, _DECL_NOMMEES,
    _DECL_VALEUR_MAX, _field_index, _sous_un_retire, declarations_effacees,
    _decl_rendue, declarations_effacees_report,
)
from .vocabulaire import (
    _ENFORCEMENT_PROBES, _ENFORCED, reset_enforced_keys, enforced_keys, _read_keys,
    _READ_KEYS, interpreted_keys, vocabulaire_vivant, _NEAR_MISS,
    unknown_declaration_keys, unknown_keys_warning, unknown_keys_read_warning,
)
from .donnees_d_origine import (
    PARAMETRE as PARAMETRE_DONNEES_D_ORIGINE,
    poser_les_deux_versions,
    description_parametre as description_donnees_d_origine,
)
from .cles_jumelles import (
    sont_jumelles, cles_libres_jumelles, cles_jumelles_warning,
)
from .non_applique import (
    _options_already_enforced, unenforced_options, unenforced_options_warning,
    options_not_enforced, options_not_enforced_warning, json_fields_depth,
    json_depth_warning, lifecycle_hors_statut, lifecycle_hors_statut_warning,
    motif_sans_obligation, motif_sans_obligation_warning,
    couche_exigee_sans_forme, couche_exigee_sans_forme_warning,
)
