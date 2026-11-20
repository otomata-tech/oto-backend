"""Capacités du schéma d'un tableau : le relire, et le POSER (#302).

Un schéma se posait sans pouvoir se relire. Pour connaître l'existant il fallait
`data_list_datastores` puis filtrer soi-même sur l'id — une jointure imposée à
l'appelant, et toute la liste ramenée en contexte pour un seul tableau.

Ce n'est pas qu'une gêne : `set_schema` pose le schéma **entier**, il ne fusionne
pas. Ajouter un champ sans avoir lu l'existant efface le reste en silence — et la
partie la plus coûteuse à perdre est `schema.key`, la clé métier, qui porte un index
UNIQUE partiel : la re-poster absente lève la contrainte sans que rien ne le dise.
La lecture est donc la condition d'une modification sûre, pas un confort.

Née CAPACITÉ et non tool écrit à la main (ADR 0042 §Convergence des surfaces) : le
dashboard édite déjà les schémas, il lui faut la même lecture, et une seconde
implémentation REST est exactement ce que la convergence combat. Les deux faces
sortent d'un descripteur unique, avec une seule autz.

Autz `SUB_ONLY` au seuil : le vrai gate est le droit de LECTURE sur le tableau, résolu
par le store (org active + ownership), jamais par le nom passé en path — un tableau
hors périmètre répond 404, comme partout ailleurs dans le datastore.

**La POSE rejoint la lecture ici** (#302, ex-route écrite à la main) : même chemin
`PUT …/{datastore}/schema`, mêmes réponses. ⚠️ Une asymétrie la traverse et n'est PAS
corrigée dans ce lot : la lecture résout les références `slot:<nom>` (ADR 0035 B3), la
pose non — elle prend le nom littéral, comme avant. La corriger ferait passer un appel
qui rendait 404, ce qui est un changement de comportement déguisé en migration ; à
trancher pour ses propres raisons.
"""
from __future__ import annotations

from ...datastore.identite import Adresse
from ...datastore import cles_inconnues

import warnings
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ... import access
from ... import db
from ...datastore import formule as dsformule
from ...datastore import identite
from ...datastore import lecture_du_schema
from ...datastore import reglages
from ...datastore import validation_complete as dsvc
from ...datastore import schema as dsv2
from ...datastore.core import (DatastoreForbidden, DatastoreNotFound, DatastoreReadOnly,
                               make_store)
from ...datastore.errors import SchemaDefinitionError
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import EntreeDatastore, ns_not_found
from ..registry import CAPABILITIES


class GetSchemaInput(EntreeDatastore):
    datastore: Adresse
    # oto#35 : la forme d'INSPECTION. Deux paramètres et pas une seconde capacité : la
    # même lecture, les mêmes droits, les mêmes avertissements — seule la réduction
    # change, et une seconde route aurait doublé le reste.
    forme: Literal["complete", "compacte"] = Field(default="complete", description=(
        "`complete` (default): the schema as served, every key. `compacte`: only the "
        "keys that constrain — structure and validation (`type`, `options`, "
        "`required`, `lifecycle`…) — without `description`, `label`, `meta` or null "
        "keys. To INSPECT a schema only: NEVER post a compact schema back, "
        "`data_set_schema` would erase every description and label of the table."))
    # oto#94 : ce que le tableau PORTE, distinct de ce qu'un agent REÇOIT.
    tel_que: Literal["servi", "stocke"] = Field(default="servi", description=(
        "`servi` (default): the schema as served to YOU — through this tool, columns "
        "the owner keeps from agents (`agent_access: \"none\"`) are left out. "
        "`stocke`: the schema as STORED, whole, with `gardes` — what each column guard "
        "actually does on this table. Reserved to whoever owns or governs the table "
        "(403 otherwise)."))


class SchemaOut(BaseModel):
    # Le champ s'appelle `schema` sur le fil — c'est le nom de la colonne, du paramètre
    # de `data_set_schema` et de ce que lisent les consommateurs. Mais `schema` masque
    # une méthode héritée de `BaseModel` (l'ancienne API v1), ce que pydantic signale à
    # la définition de la classe. D'où le nom python décalé + alias : le schéma OpenAPI
    # est généré `by_alias`, donc la face publique reste bien `schema`.
    model_config = ConfigDict(populate_by_name=True)

    # ⚠️ Le NOM CANONIQUE du tableau, plus l'écho de l'adresse reçue : lire le schéma
    # de `600` répondait `datastore: "600"` (cf. `datastore/identite.py`).
    datastore: Adresse
    # Le NUMÉRO du tableau — la forme d'adresse à employer, le nom partant en retrait.
    ns_id: Optional[int] = Field(default=None, description=identite.DESCRIPTION)
    # `None` = aucun schéma déclaré. C'est l'état NORMAL d'un datastore (le datastore
    # est schema-free par défaut) — d'où un champ nullable plutôt qu'un 404, qui ne
    # saurait pas distinguer « pas de schéma » de « tableau inconnu ».
    declared_schema: Optional[dict] = Field(default=None, alias="schema",
                                            serialization_alias="schema")
    # #389 : les clés de validation que cette version applique — la seule parade au
    # décalage entre le code écrit et la version servie.
    enforced: list = []
    # oto#127 puis oto#124 : le réglage de tête tel que la plateforme l'APPLIQUE, défaut
    # compris — `{new_rows}` (`unknown_columns` est retiré le 05/10/2026). Toujours
    # présent : un réglage absent du schéma n'est pas « inconnu », il est à son défaut.
    reglages: dict = Field(default_factory=dict, description=(
        "The head setting AS APPLIED, default included: `new_rows` (`create` | "
        "`reject` — whether a write that designates no existing row may create one). "
        "`unknown_columns` was REMOVED on 2026-10-05: no setting decides what is "
        "checked any more."))
    # #416 : ce que le schéma SERVI contient et qu'aucun niveau n'admet (stocké avant
    # la fermeture du vocabulaire, 01/10/2026). Absent (None) dans le cas normal — un
    # champ toujours présent finirait ignoré comme un ornement.
    warning: Optional[str] = None
    # oto-backend#1008 v2 : le statut CONSULTABLE du backfill de formule, posé par
    # `set_schema` puis drainé en fond (`formula_backfill_worker.py`). Absent quand
    # le tableau n'a jamais eu de formule à recalculer — un champ à `0` en
    # permanence serait aussi peu lu qu'un `warning` toujours présent.
    formules_a_recalculer: Optional[int] = None
    # oto#94 : QUELLE lecture vient d'être rendue — toujours présent, c'est le seul
    # moyen de distinguer deux réponses de même forme. `servi` = ce que reçoit
    # l'appelant (amputé des colonnes masquées sur la face outil) ; `stocke` = entier.
    tel_que: Literal["servi", "stocke"] = "servi"
    # oto#35 : présent seulement en forme compacte — une réduction qui ne se dirait pas
    # serait relue, un jour, comme le schéma entier, et reposée.
    forme: Optional[Literal["compacte"]] = None
    # oto#94 : combien de colonnes la lecture SERVIE a retirées (face outil, colonnes
    # `agent_access: "none"`). Le compte, jamais le nom. Absent quand rien n'est retiré.
    colonnes_masquees: Optional[int] = None
    # oto#94 : lecture `stocke` seulement — ce que les gardes de colonne FONT ici :
    # `verrouillees`, `masquees_a_l_agent`, `lecture_seule_agent` (listes de colonnes)
    # et `sans_effet` (`[{chemin, cle, raison}]` : déclaré, et que rien n'applique).
    # Une liste vide est omise ; `{}` = rien de déclaré, rien d'inerte.
    gardes: Optional[dict] = None


def _get_schema(ctx: ResolvedCtx, inp: GetSchemaInput) -> dict:
    # `slot:<nom>` accepté comme partout dans le datastore (ADR 0035 B3) : sans cette
    # résolution la référence passait pour un nom littéral et rendait 404 — une lecture
    # refusée là où tous les tools `data_*` l'acceptent. Le nom RÉSOLU est renvoyé :
    # l'appelant doit voir sur quel tableau il vient de lire.
    datastore = access.resolve_datastore_ref(inp.datastore)
    store = make_store(ctx.sub)
    masquees = 0
    try:
        if inp.tel_que == "stocke":
            schema = store.schema_stocke(datastore)
        else:
            schema, masquees = store.schema_servi_et_masquees(datastore)
    except DatastoreNotFound:
        raise AuthzDenied(404, "datastore_not_found")
    except DatastoreForbidden:
        raise AuthzDenied(403, "forbidden", (
            "the schema as STORED can be read by whoever owns this table or "
            "governs it; shared access is not enough. `tel_que=servi` (the default) "
            "returns what is served to you."))
    # #389 : la liste des clés de validation que CETTE version exécute. Servie ICI
    # autant qu'à la pose — sans quoi il faudrait ÉCRIRE un schéma pour savoir ce que
    # le serveur applique, c'est-à-dire produire un effet de bord pour poser une
    # question.
    # ⚠️ « Le nom RÉSOLU est renvoyé » ci-dessus ne valait que pour `slot:<nom>` : un
    # numéro restait un numéro. C'est désormais l'IDENTITÉ du tableau — son nom
    # canonique ET son numéro — quelle que soit la forme de l'adresse reçue.
    out = {**identite.de_releve(store.dernier_tableau, datastore),
           "schema": (lecture_du_schema.compacte(schema) if inp.forme == "compacte"
                      else schema),
           "enforced": dsv2.enforced_keys(), "tel_que": inp.tel_que,
           "reglages": reglages.effectifs(schema)}
    if inp.forme == "compacte":
        out["forme"] = "compacte"
    if masquees:
        out["colonnes_masquees"] = masquees
    # Les gardes se lisent sur le schéma ENTIER, jamais sur la réduction : `compacte`
    # garde bien les clés de garde, mais une garde ne se juge pas sur une copie.
    if inp.tel_que == "stocke":
        out["gardes"] = lecture_du_schema.gardes(schema)
    # oto-backend#1008 v2 : combien de rows restent `formula_dirty` — lu via l'index
    # partiel, jamais un balayage, et SEULEMENT si le schéma déclare au moins une
    # colonne formule (la lecture la plus fréquente n'en a aucune : sans cette
    # garde, chaque `get_schema` paierait une requête pour rien). `None` (absent
    # du fil) plutôt que `0` quand rien ne reste à recalculer : distinguer « rien à
    # recalculer » de « jamais eu de formule » évite de faire chercher un backfill
    # qui n'existe pas.
    if dsformule.colonnes_formule(schema):
        if ns_id := (store.dernier_tableau or {}).get("ns_id"):
            restantes = db.datastore_formula_dirty_count(ns_id)
            if restantes:
                out["formules_a_recalculer"] = restantes
    # #416 : ce qu'un schéma STOCKÉ porte encore d'inconnu se dit à chaque LECTURE.
    # Depuis le 01/10/2026 une clé inconnue est refusée à la pose, mais celles posées
    # avant restent (le refus ne porte que sur ce qu'un geste pose) jusqu'à la
    # migration — et c'est le LECTEUR qui consomme la contradiction : un `enum`
    # résiduel à côté de l'`options` qui fait foi se lit comme la liste admise.
    averts = [cles_inconnues.residus_warning(schema),
              # 07/09/2026 — un `lifecycle` que la file ne lit pas. Seule la colonne
              # de FILE (`declaration.status_field` : celle dont le bloc déclare
              # `claimable`, `max_claims` ou `abandon_state`, à défaut la première qui
              # porte un bloc) voit ses transitions validées ; un second bloc est
              # stocké, servi… et sans effet pour oto. Dit à la lecture, parce qu'un
              # schéma en base ne se repose pas.
              dsv2.lifecycle_hors_statut_warning(
                  dsv2.lifecycle_hors_statut(schema), schema),
              # 08/09/2026 — deux gardes qui ont l'air de mordre. Dites ICI autant
              # qu'à la pose, et pour la même raison qu'au-dessus : un tableau de
              # production ne repose pas son schéma, donc l'avertissement de la pose
              # ne parlera jamais à celui qui en a le plus besoin — celui dont la
              # garde est déjà posée et déjà trompeuse.
              dsv2.motif_sans_obligation_warning(dsv2.motif_sans_obligation(schema)),
              dsv2.couche_exigee_sans_forme_warning(
                  dsv2.couche_exigee_sans_forme(schema))]
    averts = [a for a in averts if a]
    if averts:
        out["warning"] = "\n".join(averts)
    return out


# ⚠️ Le champ d'ENTRÉE doit s'appeler `schema` — c'est le nom sur le fil, et la garde
# de champ inconnu compare des noms PYTHON, pas des alias (un alias ferait refuser le
# corps que le dashboard envoie depuis toujours). Or `schema` masque une méthode
# héritée de `BaseModel`, que pydantic signale à la définition de la classe : le
# warning est éteint ICI, sur ces trois lignes, plutôt que subi au boot du serveur.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)

    class SetSchemaInput(EntreeDatastore):
        datastore: Adresse
        # `null` (ou absent) = RETIRER le schéma, retour en table libre. Les deux se
        # confondent, et c'est le comportement de la route d'avant : `body.get("schema")`.
        schema: Optional[dict] = None


class SchemaPosed(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    datastore: Adresse
    declared_schema: Optional[dict] = Field(default=None, alias="schema",
                                            serialization_alias="schema")
    # #389 : les clés de validation que cette version applique — la seule parade au
    # décalage entre le code écrit et la version servie.
    enforced: list = []
    # Défaut de configuration relevé à la pose (statut sans état terminal, bornes
    # posées sur des données déjà hors borne, colonnes orphelines) : présent seulement
    # quand il y a quelque chose à dire, et adressé à l'auteur du schéma.
    warning: Optional[str] = None

    # #388 : ce que cette pose vient de RETIRER, avec les valeurs perdues — la
    # réponse en est la seule copie. Clé distincte de `warning` : les autres décrivent
    # une configuration douteuse et réparable, celle-ci nomme ce qui n'est plus.
    declarations_effacees: list = []
    declarations_effacees_hint: Optional[str] = None
    # oto-backend#479 : ce que les lignes EN PLACE violent déjà du schéma posé, par
    # chemin (`contacts[].email`) — `{rows, blocking_rows?, sample_ids, consequence}`.
    # Absent = rien à juger (aucun champ déclaré, tableau vide) ; `{}` avec
    # `complete: true` = examiné, rien de fautif.
    existing_violations: Optional[dict] = None
    # Ce qui a été examiné : `{rows_examined, rows_total, complete}`. `complete: false`
    # = plafond de lignes atteint, les comptes sont des PLANCHERS.
    existing_violations_scope: Optional[dict] = None


def _set_schema(ctx: ResolvedCtx, inp: SetSchemaInput) -> dict:
    try:
        # Une clé inconnue est REFUSÉE par le store (`validate_schema_def`), sur les
        # deux faces : rien à ajouter ici.
        return make_store(ctx.sub).set_schema(inp.datastore, inp.schema)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, inp.datastore)
    except DatastoreReadOnly:
        raise AuthzDenied(403, "datastore_read_only")
    except SchemaDefinitionError as e:
        # ⚠️ **Le refus PARLE désormais, et c'était le défaut le plus cher de la
        # nuit du 07→08/09/2026.** La route rendait `{"error":"invalid_schema"}` —
        # vingt-six caractères — pour TOUT refus de pose. Une session a tâtonné sur
        # cinq essais, conclu que `pattern` ne fonctionnait pas, et s'apprêtait à
        # remonter une capacité manquante. Le message existait et disait exactement
        # quoi corriger : « pattern exige max_length sur le même champ — le coût d'un
        # motif se majore contre la longueur de ce qu'il lit ». Personne ne l'a vu.
        #
        # Le silence était délibéré, et sa raison était bonne : UN des refus de pose
        # cite des valeurs de données (un échantillon de doublons de clé métier).
        # Mais il a été appliqué à tous. `SchemaDefinitionError` marque ceux qui ne
        # parlent que du schéma POSÉ — l'appelant l'a écrit, le lui rendre ne lui
        # apprend rien qu'il n'ait déjà envoyé.
        raise AuthzDenied(400, "invalid_schema", str(e))
    except ValueError:
        # Les autres refus restent muets : leur message cite des valeurs de LIGNES,
        # et les ouvrir serait un choix de produit, pas une correction.
        raise AuthzDenied(400, "invalid_schema")


CAPABILITIES += [
    Capability(
        key="me.datastore.set_schema",
        handler=_set_schema,
        Input=SetSchemaInput,
        Output=SchemaPosed,
        authz=SUB_ONLY,
        mcp=None,  # `data_set_schema` tient déjà la face agent
        rest=RestBinding(verb="PUT",
                         path="/api/datastores/{datastore}/schema"),
        description=(
            "Pose (ou retire, avec `schema: null`) le schéma typé d'un tableau. "
            "Le schéma est posé ENTIER — relire avant d'amender. Un réglage de "
            "tête : `new_rows` (`create` par défaut | `reject` — le droit d'une ligne "
            "nouvelle de naître, exige `key`). `unknown_columns` est REFUSÉ depuis le "
            "05/10/2026 — plus aucun réglage : les colonnes et les valeurs sont "
            "toujours vérifiées (" + dsvc.description_schema() + ") ; `strict`, "
            "`unknown_fields` et `key_required` le sont depuis le 02/10/2026. Une clé "
            "qu'aucun "
            "niveau n'admet (tête, colonne, sous-champ, `of`, `lifecycle` — listes sur "
            "`GET /api/datastore/schema/keys`) est REFUSÉE (400), en nommant le chemin, "
            "la clé et la plus proche ; une clé inconnue DÉJÀ stockée et inchangée "
            "passe. Une annotation à soi va dans `meta` (un objet, à chaque niveau, "
            "transporté, jamais lu, borné en taille) ; un texte d'aide dans "
            "`description`. La réponse porte "
            "`enforced` (les clés de validation que CETTE version applique) et "
            "`declarations_effacees` (ce que la pose vient de RETIRER, valeurs "
            "comprises — elle en est la seule copie) et `existing_violations` (par "
            "chemin, les lignes EN PLACE que le schéma posé condamne : compte, "
            "échantillon d'identifiants, conséquence ; `existing_violations_scope` "
            "dit si le relevé est complet)."
        ),
    ),
    Capability(
        key="me.datastore.get_schema",
        handler=_get_schema,
        Input=GetSchemaInput,
        Output=SchemaOut,
        authz=SUB_ONLY,
        mcp="data_get_schema",
        rest=RestBinding(verb="GET", path="/api/datastores/{datastore}/schema"),
        description=(
            "Read a datastore's declared TYPED schema (the one `data_set_schema` posts). "
            "Returns `{datastore, ns_id, schema, enforced, reglages}` — `schema` is null "
            "when none is declared, which is a normal state, not an error. "
            "`reglages` gives the head setting AS APPLIED, default included: "
            "`new_rows` (`create` | `reject` — whether a write that designates no "
            "existing row may create one). `unknown_columns` was REMOVED on "
            "2026-10-05 (columns and values are always checked); a stored leftover of "
            "it, or of `strict`, `unknown_fields` and `key_required`, is named in "
            "`warning`. "
            "`ns_id` is the table's NUMBER (e.g. 174) and `datastore` its canonical name, "
            "whatever form you addressed it by: pass the NUMBER as `datastore` — a table "
            "name is refused. Read it BEFORE "
            "amending: "
            "`data_set_schema` posts the schema WHOLE, it does not merge, so adding one "
            "field means re-posting the existing definition plus that field. "
            "The work queue itself needs NOTHING declared: `data_claim_next` reserves "
            "rows on any table, with or without a schema. A `lifecycle` block only "
            "RESTRICTS it, on the column that carries the block: "
            "`states`/`transitions`/`terminal` — `transitions` maps each state to a "
            "LIST of reachable states, even for one (`{\"a\": [\"b\"]}`, never "
            "`{\"a\": \"b\"}`, which is refused) — plus `max_claims` + "
            "`abandon_state` — the "
            "ceiling of claims WITHOUT a write past which a row leaves the queue; "
            "`labels` (`{state: \"Displayed name\"}`) names each step for a screen and "
            "is never applied to a write. "
            "`enforced` lists the validation keys THIS deployment actually applies "
            "(required, max_length, pattern…): check what you are about to declare "
            "against it, rather than against documentation — a key posted but not "
            "enforced looks like a contract and is not one, and one enforced only after "
            "the next deploy freezes rows all at once, weeks after the cause. "
            "⚠️ It says what BITES, not what is USEFUL: a key absent from `enforced` "
            "is not dead — presentation keys are read by whoever renders the table, "
            "and oto cannot know who reads what downstream. Never drop a key on the "
            "strength of its absence here. "
            "`warning` appears only when the stored schema still carries keys no level "
            "admits — posted before the vocabulary was closed (2026-10-01), typically "
            "a leftover `enum` beside the `options` that actually constrains the field. "
            "When it does, trust the key the warning names: the other is a residue, "
            "whatever it says. Writing a NEW unknown key, or changing one, is refused. "
            "`tel_que` says which reading this is. `servi` (default) is the schema as "
            "served to you: through this tool, columns the owner keeps from agents "
            "are left out, and `colonnes_masquees` counts them. `stocke` — owner or "
            "governor only — is the schema as stored, whole, plus `gardes`: per "
            "column, what the guards actually do HERE (`verrouillees`, "
            "`masquees_a_l_agent`, `lecture_seule_agent`) and `sans_effet`, what is "
            "declared and applied by nothing, with the reason. `forme=compacte` keeps "
            "only the keys that constrain, without prose or null keys — for "
            "inspection, never to post back."
        ),
    ),
]
