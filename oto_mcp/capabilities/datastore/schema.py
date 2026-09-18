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
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ... import access
from ... import db
from ...datastore import formule as dsformule
from ...datastore import identite
from ...datastore import schema as dsv2
from ...datastore.core import DatastoreNotFound, DatastoreReadOnly, make_store
from ...datastore.errors import SchemaDefinitionError
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import EntreeDatastore, ns_not_found
from ..registry import CAPABILITIES


class GetSchemaInput(EntreeDatastore):
    datastore: Adresse


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
    # #416 : ce que le schéma SERVI contient et qu'oto ne lit pas. Absent (None) dans
    # le cas normal — un champ toujours présent finirait ignoré comme un ornement.
    warning: Optional[str] = None
    # oto-backend#1008 v2 : le statut CONSULTABLE du backfill de formule, posé par
    # `set_schema` puis drainé en fond (`formula_backfill_worker.py`). Absent quand
    # le tableau n'a jamais eu de formule à recalculer — un champ à `0` en
    # permanence serait aussi peu lu qu'un `warning` toujours présent.
    formules_a_recalculer: Optional[int] = None


def _get_schema(ctx: ResolvedCtx, inp: GetSchemaInput) -> dict:
    # `slot:<nom>` accepté comme partout dans le datastore (ADR 0035 B3) : sans cette
    # résolution la référence passait pour un nom littéral et rendait 404 — une lecture
    # refusée là où tous les tools `data_*` l'acceptent. Le nom RÉSOLU est renvoyé :
    # l'appelant doit voir sur quel tableau il vient de lire.
    datastore = access.resolve_datastore_ref(inp.datastore)
    store = make_store(ctx.sub)
    try:
        schema = store.get_schema(datastore)
    except DatastoreNotFound:
        raise AuthzDenied(404, "datastore_not_found")
    # #389 : la liste des clés de validation que CETTE version exécute. Servie ICI
    # autant qu'à la pose — sans quoi il faudrait ÉCRIRE un schéma pour savoir ce que
    # le serveur applique, c'est-à-dire produire un effet de bord pour poser une
    # question.
    # ⚠️ « Le nom RÉSOLU est renvoyé » ci-dessus ne valait que pour `slot:<nom>` : un
    # numéro restait un numéro. C'est désormais l'IDENTITÉ du tableau — son nom
    # canonique ET son numéro — quelle que soit la forme de l'adresse reçue.
    out = {**identite.de_releve(store.dernier_tableau, datastore), "schema": schema,
           "enforced": dsv2.enforced_keys()}
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
    # #416 : le garde des clés non lues existait, mais UNIQUEMENT à la pose — et un
    # schéma déjà pollué ne se repose jamais. Mesuré en production le 28/08 : trois
    # tableaux (9 454 lignes) portent un attribut `enum` résiduel à côté de l'`options`
    # qui, elle, fait foi. Leur auteur a reçu l'avertissement il y a des semaines ou ne
    # l'a jamais reçu ; leurs LECTEURS, eux, en ont besoin à chaque lecture, parce que
    # c'est là que la contradiction se consomme.
    #
    # ⚠️ On AVERTIT, on ne nettoie pas. Réécrire le schéma d'un client pour en retirer
    # une clé serait détruire une déclaration qu'il a posée et que le datastore
    # s'engage à TRANSPORTER (les consommateurs y mettent les leurs) — et une
    # migration qui retouche des schémas se rejoue à chaque boot. Le résidu est
    # inerte : ce qui nuisait, c'était son silence.
    averts = [dsv2.unknown_keys_read_warning(dsv2.unknown_declaration_keys(schema)),
              # 08/09/2026 — deux orthographes libres d'une même clé dans ce schéma.
              # Servi au LECTEUR autant qu'à l'auteur : c'est le consommateur en aval
              # qui découvre qu'une colonne manque à son regroupement, et lui seul
              # sait laquelle des deux formes il lit vraiment.
              dsv2.cles_jumelles_warning(
                  dsv2.cles_libres_jumelles(dsv2.unknown_declaration_keys(schema))),
              # 07/09/2026 — MÊME défaut, autre clé, et celui-ci coûte une file de
              # travail entière. `lifecycle` n'est lu que sur le champ `role: "status"`
              # : posé ailleurs, il est stocké, servi… et sans le moindre effet. Plus
              # d'état terminal, plus de plafond de reprises, plus d'état d'abandon,
              # plus de périmètre de réservation — et le schéma affiche le contraire.
              #
              # ⚠️ **La pose le refuse déjà, et c'est justement pourquoi il faut le
              # dire ICI.** Le refus ne parle qu'à qui écrit un schéma NEUF ; celui
              # qui a posé le sien avant la garde ne l'entendra jamais, puisqu'un
              # schéma en base ne se repose pas. Cas trouvé sur un tableau de
              # production d'une campagne vivante — par un tiers comparant deux
              # schémas, pas par la plateforme.
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
    # Les attributs de colonne que PERSONNE ne lit (oto#56). `None` = rien à
    # signaler ; la clé est toujours là, pour distinguer « rien à dire » d'un serveur
    # trop vieux. Avertissement, jamais refus : refuser durcirait un contrat servi et
    # casserait les schémas qui portent déjà des clés mortes.
    unknown_keys_warning: Optional[str] = None

    # #388 : ce que cette pose vient de RETIRER, avec les valeurs perdues — la
    # réponse en est la seule copie. Clé distincte de `warning` : les autres décrivent
    # une configuration douteuse et réparable, celle-ci nomme ce qui n'est plus.
    declarations_effacees: list = []
    declarations_effacees_hint: Optional[str] = None


def _set_schema(ctx: ResolvedCtx, inp: SetSchemaInput) -> dict:
    try:
        # L'avertissement se calcule sur ce que l'appelant a ENVOYÉ, pas sur ce que le
        # store rend : c'est son texte à lui qui porte la faute de frappe, et le store
        # peut normaliser (oto#56).
        return {**make_store(ctx.sub).set_schema(inp.datastore, inp.schema),
                **cles_inconnues.check(inp.schema)}
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
            "Le schéma est posé ENTIER — relire avant d'amender. La réponse porte "
            "`enforced` (les clés de validation que CETTE version applique) et "
            "`declarations_effacees` (ce que la pose vient de RETIRER, valeurs "
            "comprises — elle en est la seule copie)."
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
            "Returns `{datastore, ns_id, schema, enforced}` — `schema` is null when none "
            "is declared, which is a normal state, not an error. "
            "`ns_id` is the table's NUMBER (e.g. 174) and `datastore` its canonical name, "
            "whatever form you addressed it by: pass the NUMBER as `datastore` from here "
            "on — a name still resolves, it is being retired, not broken. Read it BEFORE "
            "amending: "
            "`data_set_schema` posts the schema WHOLE, it does not merge, so adding one "
            "field means re-posting the existing definition plus that field. "
            "The work-queue rules live on the `role:\"status\"` field, under `lifecycle`: "
            "`states`/`transitions`/`terminal`, plus `max_claims` + `abandon_state` — the "
            "ceiling of claims WITHOUT a write past which a row leaves the queue. "
            "`enforced` lists the validation keys THIS deployment actually applies "
            "(required, max_length, pattern…): check what you are about to declare "
            "against it, rather than against documentation — a key posted but not "
            "enforced looks like a contract and is not one, and one enforced only after "
            "the next deploy freezes rows all at once, weeks after the cause. "
            "⚠️ It says what BITES, not what is USEFUL: a key absent from `enforced` "
            "is not dead — presentation keys are read by whoever renders the table, "
            "and oto cannot know who reads what downstream. Never drop a key on the "
            "strength of its absence here. "
            "`warning` appears only when the stored schema carries declaration keys oto "
            "does NOT read — typically a leftover `enum` sitting beside the `options` "
            "that actually constrains the field. When it does, trust the key the warning "
            "names: the unread one is a residue, whatever it says."
        ),
    ),
]
