"""Capacités « les lignes » : page, fiche, écriture, suppression, file, agrégat (#302).

Huit chemins qui vivaient en routes écrites à la main (`api/datastore.py`).
C'est le cœur de l'écran datastore, et c'était la plus grosse zone d'ombre du contrat
d'API : ni entrée ni sortie déclarées, donc rien à générer chez un intégrateur.

`mcp=None` partout : les tools `data_*` tiennent déjà la face agent, ce lot ne migre
que la face REST. Autz `SUB_ONLY` au seuil ; le vrai gate est le droit de lecture ou
d'ÉCRITURE sur le tableau, résolu par le store (org active + ownership ADR 0030) —
jamais par le nom passé en path. Un tableau hors périmètre répond 404, comme partout
ailleurs dans le datastore.

**Deux corps LIBRES** (`RestBinding.body_field`) : ajouter et modifier une ligne
envoient les colonnes du tableau au premier niveau du corps. Ce sont des DONNÉES, pas
des champs d'API — la garde de champ inconnu les aurait toutes refusées. Elle continue
de couvrir la query string et les params de chemin, ce que le test vérifie.

**Les paramètres JSON restent des chaînes** (`filters`, `metrics`, `filter`) : ils
arrivent par la query string, où ils sont du JSON encodé en texte. Les typer `list`/
`dict` ferait rendre à pydantic un `invalid_input` générique là où ces routes rendent
`invalid_filters`/`invalid_metrics`/`invalid_filter` — trois refus distincts que le
cockpit distingue. Le décodage vit donc dans le handler, comme avant.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ... import access
from ...auth import token_scopes
from ...datastore import journal as datastore_journal
from ...datastore import jetons
from ...datastore import layers as dsl
from ...datastore.errors import ClaimedRefUnresolved
from ...datastore.core import (
    BusinessKeyRequired,
    NamespaceNotFound,
    NamespaceReadOnly,
    RowNotFound,
    RowValidationError,
    make_store,
)
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import HORODATAGE, ns_not_found
from .lot import refuser_un_lot
from ..registry import CAPABILITIES


def _tolerant_int(v):
    """`?limit=beaucoup` retombe sur le défaut, il ne casse pas la requête.

    C'est le comportement des routes d'avant (`_int(name, default)` avalait
    `TypeError`/`ValueError`). Le typer `int` sec ferait 400 là où le serveur rendait
    une page — un écart invisible en test et visible en prod.
    """
    if v is None or isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# oto#53 : la forme des cellules à couches. Typé `str` et non `Literal` pour que la
# mauvaise valeur rende un refus qui NOMME le paramètre (`invalid_layers`, par
# `_layers`), pas l'`invalid_input` nu que l'adaptateur rend sur une `ValidationError`.
_LAYERS = Field(default=dsl.DEFAUT, description=(
    "Forme des cellules à couches. On ÉCRIT imbriqué (`champ` = `{valeur, origine, "
    "comment, link}`) et, par défaut, on relit À PLAT : ce paramètre lève cette "
    "asymétrie. `flat` (défaut) sert `champ` = la valeur et `champ.origine`/`.comment`/"
    "`.link` à plat à côté ; `nested` sert `champ` = `{valeur, origine, comment, link}` "
    "(la valeur toujours, les couches renseignées seulement), la forme dans laquelle on "
    "écrit ; une cellule sans couche est le même scalaire dans les deux. Toute autre "
    "valeur est refusée. Le défaut basculera vers `nested`, avec préavis daté : un "
    "client qui dépend d'une forme la nomme dès maintenant."))


def _layers(raw) -> str:
    """`?layers=` validé, ou un 400 qui nomme le paramètre et les valeurs admises."""
    try:
        return dsl.check(raw)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_layers", str(e))


class ListRowsInput(BaseModel):
    namespace: str
    # `None` = le défaut du serveur (0 / 50) ; borné à [1, 500] pour `limit`.
    offset: Optional[int] = None
    limit: Optional[int] = None
    order_by: Optional[str] = None
    order_dir: str = "desc"
    q: Optional[str] = None
    # JSON encodé : filtre d'égalité exacte `{colonne: valeur}` — le MÊME paramètre
    # que la face MCP `data_rows`, et ce que la CLI envoie sur `--filter`. Il était
    # absent : la route l'ignorait en silence et rendait TOUTES les lignes en les
    # présentant comme filtrées (#303). Les deux faces d'un verbe doivent offrir
    # les mêmes paramètres — c'est « une capacité, deux faces » appliquée à elle-même.
    filter: Optional[str] = None
    # JSON encodé : liste de clauses `{field, op, value}`, combinées en ET.
    filters: Optional[str] = None
    layers: str = _LAYERS

    _coerce = field_validator("offset", "limit", mode="before")(_tolerant_int)


class AggregateInput(BaseModel):
    namespace: str
    group_by: Optional[str] = None
    # JSON encodé : `[{op: count|sum|avg|min|max, field?}]`.
    metrics: Optional[str] = None
    # JSON encodé : filtre d'égalité exacte `{colonne: valeur}` (chemin MCP).
    filter: Optional[str] = None
    q: Optional[str] = None
    # JSON encodé : mêmes clauses riches que `/rows` — les tuiles du cockpit agrègent
    # le jeu filtré affiché.
    filters: Optional[str] = None


class NamespaceRefInput(BaseModel):
    namespace: str


class RowRefInput(BaseModel):
    namespace: str
    row_id: str


class GetRowInput(RowRefInput):
    """La lecture d'une ligne seule porte `layers` ; `RowRefInput` reste partagé avec
    la suppression, qui n'a pas de forme à choisir."""
    layers: str = _LAYERS


# #658 : le corps EST la ligne (`RestBinding.body_field`) — le forçage ne peut donc
# pas y vivre sans se confondre avec une colonne du tableau. Il passe en QUERY, ce que
# l'adaptateur REST fait déjà de tout champ d'`Input` hors du corps libre, et ce que
# l'OpenAPI publie tel quel. Additif : un client qui l'ignore garde le refus.
_FORCAGE = Field(default=False, description=(
    "Remplacer les colonnes verrouillées (`readonly`) que cet appel écrit, au lieu "
    "d'être refusé. Réservé au propriétaire du tableau ou à qui le gouverne ; ne vaut "
    "que pour cet appel ; journalisé (ligne, colonne, valeur remplacée)."))


class AppendRowInput(BaseModel):
    namespace: str
    # Le corps ENTIER (cf. `RestBinding.body_field`) : les colonnes du tableau.
    row: dict = Field(default_factory=dict)
    readonly_override: bool = _FORCAGE


class UpdateRowInput(BaseModel):
    namespace: str
    row_id: str
    # Le corps ENTIER : les colonnes à écrire (patch partiel, jamais un remplacement).
    patch: dict = Field(default_factory=dict)
    readonly_override: bool = _FORCAGE


class ReleaseInput(BaseModel):
    namespace: str
    row_id: str
    # Vide = libération FORCÉE (supervision humaine) ; renseigné = libération GARDÉE.
    worker: str = ""


class Row(BaseModel):
    """Une ligne : trois colonnes de plateforme, plus les colonnes de l'utilisateur.

    `additionalProperties` est VRAI et c'est le fond du modèle — un tableau du
    datastore n'a pas de schéma imposé (le schéma typé d'ADR 0046 est optionnel et se
    lit sur le namespace, pas ici).
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    row_id: str = Field(alias="_id", serialization_alias="_id")
    created_at: Optional[str] = Field(default=None, alias="_created_at",
                                      serialization_alias="_created_at",
                                      description=HORODATAGE)
    updated_at: Optional[str] = Field(default=None, alias="_updated_at",
                                      serialization_alias="_updated_at",
                                      description=HORODATAGE)
    # Bail de la file de travail (ADR 0046 D) : ABSENTS quand la ligne n'est pas
    # réservée (les lectures ordinaires ne les sélectionnent pas) — pas nuls, absents.
    claimed_by: Optional[str] = Field(default=None, alias="_claimed_by",
                                      serialization_alias="_claimed_by")
    claimed_until: Optional[str] = Field(default=None, alias="_claimed_until",
                                         serialization_alias="_claimed_until",
                                         description=HORODATAGE)
    claimed_run: Optional[str] = Field(
        default=None, alias="_claimed_run", serialization_alias="_claimed_run",
        description=(
            "The run holding this lease — what links a piece of work to the ROW it "
            "is working on. Present whenever `_claimed_by` is (the three travel "
            "together); null when the lease was taken WITHOUT a run (a person on "
            "the dashboard queue, an agent that passed no `_run_id`). Cleared when "
            "the run gives its rows back (`run_finish`, or the runner concluding "
            "its job), so it answers \"which row is this run on NOW\", not \"which "
            "row did that run work\"."))


class WrittenRow(Row):
    """La ligne écrite, plus le relevé « hors schéma » du geste (#294).

    Les deux clés n'apparaissent QUE si l'écriture a posé des champs absents du schéma
    déclaré — leur présence est le signal, leur absence est le cas normal.
    """
    hors_schema: Optional[list[str]] = None
    hors_schema_hint: Optional[str] = None
    # #319 — valeurs écrites hors des `options` déclarées, sur un tableau qui n'est
    # PAS en format strict : elles passent (le régime est souple par déclaration),
    # mais la réponse le dit désormais. `{champ: valeur}`. Absent quand tout est dans
    # les listes — le cas normal, pas de clé parasite dans la réponse.
    hors_options: Optional[dict] = None
    hors_options_hint: Optional[str] = None
    # #317 : ce qui a CHANGÉ dans le comportement de la plateforme, dit à l'instant
    # où ça joue — aujourd'hui le retrait de la libération automatique sur état final.
    # Déclaré (et pas seulement toléré par `extra="allow"`) parce qu'un message de
    # migration dont tout l'objet est d'être LU doit exister au contrat publié : une
    # intégration qui lit l'OpenAPI ne saurait pas qu'il peut arriver. Absent quand
    # il n'y a rien à dire — le cas normal.
    notices: Optional[list[str]] = None


class RowPage(BaseModel):
    rows: list[Row]
    # Total du jeu FILTRÉ (pas du tableau) — c'est ce qui pagine le cockpit.
    total: int
    offset: int
    limit: int


class RowQueue(BaseModel):
    rows: list[Row]


class AggregateResult(BaseModel):
    # Un groupe = `{<group_by>: valeur, count: n, sum_<champ>: n, …}` — les clés
    # dépendent des métriques demandées, elles ne sont donc pas déclarables.
    groups: list[dict]


class DeletedRow(BaseModel):
    ok: bool
    id: str


class ReleasedRow(BaseModel):
    ok: bool
    # ⚠️ `False` couvrait DEUX situations opposées, et rien ne les distinguait (#517,
    # 29/08) : « aucun bail » (bénin) et « bail d'un autre travail » (échec réel).
    # `reason` les sépare en vocabulaire fermé, `hint` dit laquelle en toutes lettres.
    released: bool
    id: str
    reason: Optional[str] = None
    hint: Optional[str] = None


def _adresse(ctx: ResolvedCtx, namespace: str, row_id=None, *, ligne: bool = True):
    """La MÊME couture que la face agent (`oto_mcp/datastore/jetons.py`).

    ⚠️ Elle est ici parce que les deux faces avaient divergé, et en silence : les
    opérations de SCHÉMA de cette couche résolvaient `slot:<nom>` depuis toujours,
    celles de LIGNES le passaient brut au stockage, qui répondait « namespace inconnu »
    sur un jeton parfaitement valide. *Une divergence qui refuse est visible ; une
    divergence qui répond une cause fausse s'instruit pendant des jours.*"""
    try:
        return jetons.resoudre(make_store(ctx.sub), namespace, row_id, ligne=ligne,
                               resoudre_slot=access.resolve_namespace_ref)
    except (jetons.JetonMalPlace, ClaimedRefUnresolved) as e:
        raise AuthzDenied(400, "jeton_mal_place", str(e))


def _indice_de_liberation(issue: dict) -> str:
    """La MÊME phrase que la face agent — écrite une fois, servie deux.

    Les deux faces avaient chacune sa formule, et toutes deux mêlaient les deux
    situations dans un seul texte (#517, 29/08)."""
    from ...datastore.core import indice_de_liberation
    return indice_de_liberation(issue)


def _verifier_contenu(contenu) -> None:
    """Ce qui n'a aucun sens comme donnée est refusé AVANT d'atteindre le stockage."""
    try:
        jetons.verifier_contenu(contenu)
    except jetons.JetonMalPlace as e:
        raise AuthzDenied(400, "jeton_mal_place", str(e))


def _json_param(raw: Optional[str], code: str, *, expect=None):
    """Décode un paramètre JSON de query string, avec le refus NOMMÉ de la route."""
    if not raw:
        return None
    try:
        out = json.loads(raw)
    except ValueError:
        raise AuthzDenied(400, code)
    if expect is not None and not isinstance(out, expect):
        raise AuthzDenied(400, code)
    return out


def _list_rows(ctx: ResolvedCtx, inp: ListRowsInput) -> dict:
    ns, _ = _adresse(ctx, inp.namespace, ligne=False)
    offset = max(0, inp.offset if inp.offset is not None else 0)
    limit = min(500, max(1, inp.limit if inp.limit is not None else 50))
    filter_eq = _json_param(inp.filter, "invalid_filter", expect=dict)
    filters = _json_param(inp.filters, "invalid_filters", expect=list)
    layers = _layers(inp.layers)
    try:
        return make_store(ctx.sub).page_rows(
            ns, offset=offset, limit=limit,
            order_by=inp.order_by or None, order_dir=inp.order_dir,
            q=inp.q or None, filter=filter_eq, filters=filters, layers=layers)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except ValueError as e:
        # Le message du store arrive JUSQU'À l'appelant. Sans lui, un refus
        # SÉMANTIQUE (opérateur inconnu, `null` sur `eq`, date invalide) rend le
        # même `invalid_filters` nu qu'un JSON malformé — l'appelant relit sa
        # syntaxe alors que c'est le SENS de son filtre qui est refusé. Trouvé au
        # smoke prod de v1.87.0 : la garde `null` nommait `empty` et personne ne
        # le voyait.
        raise AuthzDenied(400, "invalid_filters", str(e))


def _aggregate(ctx: ResolvedCtx, inp: AggregateInput) -> dict:
    """Agrégat serveur (ADR 0046 b1 — compteurs du cockpit) : COUNT/SUM/AVG/…
    groupés par un champ JSONB, sans rapatrier les lignes."""
    ns, _ = _adresse(ctx, inp.namespace, ligne=False)
    metrics = _json_param(inp.metrics, "invalid_metrics")
    filter_eq = _json_param(inp.filter, "invalid_filter", expect=dict)
    filters = _json_param(inp.filters, "invalid_filters", expect=list)
    try:
        groups = make_store(ctx.sub).aggregate(
            ns, group_by=inp.group_by or None, metrics=metrics,
            filter=filter_eq, q=inp.q or None, filters=filters)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_aggregate", str(e))
    return {"groups": groups}


def _queue(ctx: ResolvedCtx, inp: NamespaceRefInput) -> dict:
    """File de travail (ADR 0046 D) — vue de supervision : les lignes sous bail
    (`_claimed_by`/`_claimed_until`/`_claimed_run`), actif ou expiré. Lecture
    seule. `_claimed_run` est ce qui rend la vue ACTIONNABLE : sans lui elle dit
    qu'un travail tient une ligne, jamais lequel tient laquelle."""
    ns, _ = _adresse(ctx, inp.namespace, ligne=False)
    try:
        return {"rows": make_store(ctx.sub).queue(ns)}
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)


def _get_row(ctx: ResolvedCtx, inp: GetRowInput) -> dict:
    ns, rid = _adresse(ctx, inp.namespace, inp.row_id)
    layers = _layers(inp.layers)
    try:
        return make_store(ctx.sub).get_row(ns, rid, layers=layers)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except RowNotFound:
        raise AuthzDenied(404, "row_not_found")


def _write_refusal(e: Exception) -> AuthzDenied:
    """Un refus d'écriture est ACTIONNABLE, jamais un 500 opaque.

    `RowValidationError` (schéma strict, transition de cycle de vie non déclarée) et
    `ValueError` (`_id` posé dans le corps #390, clé métier déjà portée) portent le
    message qui dit quoi corriger — le front l'affiche tel quel. Sans cette
    traduction, la face MCP répondait proprement pendant que REST rendait « Internal
    Server Error », et Sentry comptait une faute d'appel comme un bug backend.
    ⚠️ `RowValidationError` DÉRIVE de `ValueError` : l'ordre des branches est le
    contrat, pas un détail de style.

    ⚠️ `BusinessKeyRequired` (#516) est un troisième cas, et il mérite son propre code :
    un front qui reçoit `invalid_row_input` ne peut que réafficher une phrase, alors
    que `business_key_required` lui dit QUOI proposer — viser une ligne existante.
    Elle dérive de `ValueError` elle aussi : l'ordre des branches reste le contrat.

    `details` (#545) passe avec, quand le refus en porte : `expected_column` dit au
    front QUEL champ pointer, sans reparser la phrase française du message — la
    reconstituer serait un contrat déguisé. Le CODE, lui, ne bouge pas : un refus de
    schéma reste `row_invalid`, et un code neuf ferait traiter comme nouveau ce que
    les clients gèrent déjà.
    """
    if isinstance(e, BusinessKeyRequired):
        return AuthzDenied(400, "business_key_required", str(e))
    if isinstance(e, RowValidationError):
        return AuthzDenied(400, "row_invalid", str(e), e.details)
    return AuthzDenied(400, "invalid_row_input", str(e))


_ECRITURE_DETRUIT = (' ⚠️ Une écriture DÉTRUIT ce qui est dans la colonne : sur une colonne ouverte il n\'y a ni annulation ni historique, la valeur précédente disparaît au moment où la vôtre arrive. Le seul filet est le format `origine: "system"`, et il est plus étroit qu\'il n\'y paraît — la plateforme garde la valeur précédente UNE fois, à la première écriture qui CHANGE la valeur APRÈS la déclaration de ce format. Une colonne sans ce format ne garde rien ; un format déclaré tard capture ce que le dernier écrivain a laissé, qui peut être la valeur d\'un autre agent, présentée sous le nom `origine`. La face d\'appel n\'y change rien : une ligne créée ici et une ligne créée par l\'outil agent se comportent à l\'identique.')


def _append_row(ctx: ResolvedCtx, inp: AppendRowInput) -> dict:
    ns, _ = _adresse(ctx, inp.namespace, ligne=False)
    _verifier_contenu(inp.row)
    trace: dict = {}
    store = make_store(ctx.sub)
    try:
        refuser_un_lot(store, ns, inp.row)  # oto#48 : un lot enveloppé n'est pas une ligne
        created = store.append_row(ns, inp.row, trace=trace,
                                   readonly_override=inp.readonly_override)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    except ValueError as e:
        raise _write_refusal(e)
    nsctx = datastore_journal.from_trace(trace, ns)
    datastore_journal.record(
        datastore_journal.TOOL_WRITE, sub=ctx.sub, ctx=nsctx, row_id=created.get("_id"),
        fields=list(inp.row.keys()), forced=store.off_forced,
        to_status=datastore_journal.status_of(created, nsctx))
    # Même relevé « hors schéma » que la face MCP (#294) : les deux faces ne doivent
    # pas diverger sur ce qu'elles signalent d'une écriture.
    return {**created, **store.off_schema_report()}


def _update_row(ctx: ResolvedCtx, inp: UpdateRowInput) -> dict:
    # L'état AVANT vient du RELEVÉ de la mutation (`trace`) : c'est celui sur lequel la
    # transition a été validée. Le relire ici courrait avec un write concurrent → le
    # cockpit proposerait d'annuler vers un état que la ligne n'a jamais eu.
    ns, rid = _adresse(ctx, inp.namespace, inp.row_id)
    _verifier_contenu(inp.patch)
    trace: dict = {}
    store = make_store(ctx.sub)
    try:
        updated = store.update_row(ns, rid, inp.patch, trace=trace,
                                   readonly_override=inp.readonly_override)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    except RowNotFound:
        raise AuthzDenied(404, "row_not_found")
    except ValueError as e:
        raise _write_refusal(e)
    nsctx = datastore_journal.from_trace(trace, ns)
    datastore_journal.record(
        datastore_journal.TOOL_WRITE, sub=ctx.sub, ctx=nsctx, row_id=rid,
        fields=list(inp.patch.keys()), forced=store.off_forced,
        from_status=trace.get("prev_status"),
        to_status=datastore_journal.status_of(updated, nsctx))
    return {**updated, **store.off_schema_report()}


def _delete_row(ctx: ResolvedCtx, inp: RowRefInput) -> dict:
    ns, rid = _adresse(ctx, inp.namespace, inp.row_id)
    trace: dict = {}
    try:
        make_store(ctx.sub).delete_row(ns, rid, trace=trace)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    except RowNotFound:
        raise AuthzDenied(404, "row_not_found")
    nsctx = datastore_journal.from_trace(trace, ns)
    datastore_journal.record(datastore_journal.TOOL_DELETE, sub=ctx.sub, ctx=nsctx,
                             row_id=rid, from_status=trace.get("prev_status"))
    return {"ok": True, "id": rid}


def _release_claim(ctx: ResolvedCtx, inp: ReleaseInput) -> dict:
    """Libère le bail d'une ligne. Deux régimes, selon ce que l'appelant SAIT :

    - `worker` renseigné → libération GARDÉE (on ne libère pas le bail d'un autre) ;
    - `worker` absent → libération FORCÉE, supervision humaine (dashboard).

    Le forcé est refusé à un jeton PORTÉ : c'est le vecteur des intégrations
    multi-utilisateurs (un front d'équipe), où « n'importe qui libère la réservation
    de n'importe qui » est le défaut, pas la supervision. Une session interactive
    garde le geste, elle en a la légitimité. Exige l'écriture dans les deux cas.
    """
    ns, rid = _adresse(ctx, inp.namespace, inp.row_id)
    worker = (inp.worker or "").strip()
    if not worker and token_scopes.current() is not None:
        raise AuthzDenied(400, "worker_required",
                          "un jeton porté libère SON bail : passer le worker "
                          "utilisé au claim (la libération forcée est réservée "
                          "à la supervision, session interactive)")
    trace: dict = {}
    store = make_store(ctx.sub)
    try:
        # La libération FORCÉE reste un booléen : la supervision humaine agit sans
        # garde, il n'y a pas de « bail d'un autre » qui la concerne.
        issue = (store.release_claim(ns, rid, worker=worker, trace=trace) if worker
                 else {"released": store.force_release(ns, rid, trace=trace),
                       "reason": None, "lease": None})
        released = issue["released"]
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, ns)
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    if released:  # rien libéré = rien changé, donc rien à journaliser
        datastore_journal.record(
            datastore_journal.TOOL_RELEASE, sub=ctx.sub,
            ctx=datastore_journal.from_trace(trace, ns), row_id=rid)
    return {
        "ok": True, "released": released, "id": rid, "reason": issue["reason"],
        **({} if released else {"hint": _indice_de_liberation(issue)}),
    }


_NS = "/api/datastore/namespaces/{namespace}"

CAPABILITIES += [
    Capability(
        key="me.datastore.list_rows",
        handler=_list_rows,
        Input=ListRowsInput,
        Output=RowPage,
        authz=SUB_ONLY,
        mcp=None,  # `data_rows` tient déjà la face agent
        rest=RestBinding(verb="GET", path=_NS + "/rows"),
        description=("Page de lignes d’un tableau (tri, recherche, filtres serveur). "
                     "Pagination par `offset` + `limit` avec `total` du jeu filtré, "
                     "pas de curseur — la fin se calcule. Couches à plat par défaut, "
                     "`layers=nested` pour la forme d’écriture ; guide "
                     "`datastore-semantics`."),
    ),
    Capability(
        key="me.datastore.append_row",
        handler=_append_row,
        Input=AppendRowInput,
        Output=WrittenRow,
        authz=SUB_ONLY,
        mcp=None,
        # Corps LIBRE (les colonnes) + 201 : les deux contrats d'avant la migration.
        rest=RestBinding(verb="POST", path=_NS + "/rows", status=201, body_field="row"),
        description=("Ajoute UNE ligne à un tableau — le corps EST la ligne : un objet, "
                     "une clé par colonne. Pas de lot ici : un corps dont l'unique clé "
                     "porte une liste d'objets est refusé (`400 batch_body`) ; le lot "
                     "passe par `data_write(rows=[…])` côté agent, ou par un upload "
                     "signé NDJSON/CSV (`oto_upload_url` → `PUT /api/upload/{token}`) "
                     "pour les volumes. "
                     "`readonly_override=true` remplace les colonnes verrouillées "
                     "de cet appel — propriétaire ou gouvernant du tableau seulement, "
                     "et journalisé. Couches, `readonly`, clé métier : guide "
                     "`datastore-semantics`." + _ECRITURE_DETRUIT),
    ),
    Capability(
        key="me.datastore.get_row",
        handler=_get_row,
        Input=GetRowInput,
        Output=Row,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="GET", path=_NS + "/rows/{row_id}"),
        description="Lit une ligne par son `_id` (deep-link de fiche).",
    ),
    Capability(
        key="me.datastore.update_row",
        handler=_update_row,
        Input=UpdateRowInput,
        Output=WrittenRow,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="PATCH", path=_NS + "/rows/{row_id}", body_field="patch"),
        description=("Modifie une ligne (patch partiel ; le corps EST le patch). "
                     "`readonly_override=true` remplace les colonnes verrouillées "
                     "de cet appel — propriétaire ou gouvernant du tableau seulement, "
                     "et journalisé." + _ECRITURE_DETRUIT),
    ),
    Capability(
        key="me.datastore.delete_row",
        handler=_delete_row,
        Input=RowRefInput,
        Output=DeletedRow,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="DELETE", path=_NS + "/rows/{row_id}"),
        description="Supprime une ligne.",
    ),
    Capability(
        key="me.datastore.release_claim",
        handler=_release_claim,
        Input=ReleaseInput,
        Output=ReleasedRow,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="POST", path=_NS + "/rows/{row_id}/release"),
        description="Libère le bail d'une ligne (gardée avec `worker`, forcée sans).",
    ),
    Capability(
        key="me.datastore.queue",
        handler=_queue,
        Input=NamespaceRefInput,
        Output=RowQueue,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="GET", path=_NS + "/queue"),
        description="File de travail : les lignes sous bail (supervision, lecture seule).",
    ),
    Capability(
        key="me.datastore.aggregate",
        handler=_aggregate,
        Input=AggregateInput,
        Output=AggregateResult,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="GET", path=_NS + "/aggregate"),
        description="Agrégat serveur d'un tableau (COUNT/SUM/AVG/MIN/MAX, groupé).",
    ),
]
