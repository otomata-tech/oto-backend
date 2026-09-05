"""La grammaire des requêtes — construire une clause, jamais l'exécuter.

Extrait de `db/datastore.py` sans un changement de comportement (#325). La couture est
franche : **ici tout est PUR**. Ces fonctions rendent `(sql, params)` et ne touchent
jamais une connexion — ce qui les rend testables sans base, et c'est ce qui a permis de
figer la plupart des pièges du filtrage par des tests rapides.

Elles s'appuient sur `paths` pour désigner une valeur, et les fonctions qui EXÉCUTENT
(`datastore_list_rows`, `datastore_aggregate`…) restent dans le module de données.

⚠️ L'invariant qui vaut pour tout ce fichier : **le nom d'une colonne est TOUJOURS un
paramètre**, jamais interpolé dans le SQL. La seule exception est le chemin de la clé
métier, qui doit matcher son index d'expression à la chaîne près — et elle est traitée
dans `paths`, avec un littéral échappé.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .paths import (
    FIELD_VALUE_PARAM_SQL,
    LAYER_VALUE_PARAM_SQL,
    ROW_VALUES_TEXT_SQL,
    field_read_sql,
    leaf_read_sql,
    split_layer,
    split_list_path,
)

__all__ = ["group_key"]


_DS_FILTER_OPS = {"contains", "eq", "ne", "in", "gt", "gte", "lt", "lte", "empty", "not_empty"}


_DS_CMP_SQL = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


_DS_NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")  # numérique strict (pas de nan/1e5)


def _ds_text(val: Any) -> str:
    """Valeur de filtre → sa forme TEXTE telle que `data ->> champ` la rendrait (#306).

    `->>` extrait le JSON **en texte**, avec les conventions du JSON : un booléen y
    ressort `"true"`/`"false"` en minuscules. `str(True)` rend `"True"` — majuscule,
    convention Python — donc la comparaison était `"true" = "True"`, fausse pour
    chaque ligne. Zéro résultat, **sans erreur** : SQL compare deux chaînes valides
    qui ne coïncident jamais, et « aucune correspondance » est une réponse honnête à
    une question qui n'était pas celle qu'on posait. Mesuré : 0 ligne contre 29.

    Même famille pour un flottant entier — `str(1.0)` rend `"1.0"` là où un entier
    stocké ressort `"1"`.

    ⚠️ Une CHAÎNE passe telle quelle, et c'est délibéré : des appelants contournent
    aujourd'hui en envoyant `"true"` (le seul moyen d'obtenir le bon résultat). Le
    correctif ne doit pas transformer un piège silencieux en régression chez ceux
    qui avaient trouvé la parade — les deux formes matchent le même booléen stocké.
    """
    if isinstance(val, bool):        # AVANT le test int : en Python, bool ⊂ int
        return "true" if val else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    if val is None:
        # `data ->> champ` rend SQL NULL aussi bien pour un JSON `null` que pour une
        # clé absente : aucune comparaison textuelle ne peut les distinguer. On le
        # dit plutôt que de rendre un zéro que l'appelant lirait comme « aucune
        # ligne ne correspond ».
        raise ValueError(
            "valeur de filtre `null` : `data ->> champ` ne distingue pas un JSON "
            "`null` d'une clé absente, donc `eq`/`ne` ne peuvent pas y répondre — "
            "utiliser l'opérateur `empty` (ou `not_empty`).")
    return str(val)


# Colonnes MÉTA filtrables. Elles ne vivent PAS dans `data` : sans ce routage, un
# filtre « modifié depuis le 1er » partait en `data ->> '_updated_at'` = NULL et
# rendait ZÉRO ligne, sans la moindre erreur — un filtre muet est pire qu'un filtre
# absent. `order_by` les connaissait déjà (cf. `datastore_list_rows`), pas le WHERE.
_DS_META_TS_COLS = {"_updated_at": "updated_at", "_created_at": "created_at"}
_DS_META_TEXT_COLS = {"_id": "row_id"}
# Ops qui ont un sens sur une colonne NOT NULL : ni `empty`/`not_empty` (réponse
# connue d'avance), ni `contains` sur un instant. On REFUSE plutôt que de servir un
# résultat vide inexplicable.
_DS_META_TS_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}
_DS_META_TEXT_OPS = {"eq", "ne", "contains", "in"}
# Date seule (`2026-08-05`) vs instant (`2026-08-05T14:30`, suffixe tz optionnel).
_DS_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DS_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


_DS_MAX_FILTERS = 30


def ds_filter_specs(filter: Optional[dict]) -> list[dict]:
    """`{col: valeur}` ou `{col: {op: valeur}}` → la liste `{field, op, value}` du
    moteur SQL (ops whitelistés par `_ds_filter_clauses`, qui lève sur inconnu).

    Une valeur scalaire reste une égalité (contrat historique) ; un dict ouvre les
    opérateurs déjà servis au dashboard — `contains`, `ne`, `in`, `gt/gte/lt/lte`,
    `empty`/`not_empty`. Sans ça, une question triviale (« quel post a une autrice
    prénommée Sylvie ? ») obligeait à dumper tout le namespace et à filtrer en
    local, alors que le SQL savait le faire.

    Vivait dans `datastore/core.py` (`_filter_specs`) jusqu'au 29/08/2026 (#517) :
    le périmètre de réservation déclaré au schéma se valide par la MÊME grammaire, et
    `schema.py` ne peut pas remonter vers le store. Déplacement pur, `core` garde le
    nom.
    """
    out: list[dict] = []
    for k, v in (filter or {}).items():
        if isinstance(v, dict):
            if len(v) != 1:
                raise ValueError(
                    f"filtre `{k}` : un seul opérateur par colonne "
                    f"(reçu {sorted(v)!r})")
            op, value = next(iter(v.items()))
            out.append({"field": k, "op": str(op), "value": value})
        else:
            out.append({"field": k, "op": "eq", "value": v})
    return out


def _ds_meta_ts_clause(col: str, op: str, val: str) -> tuple[str, list]:
    """Fragment WHERE d'un filtre sur une colonne timestamptz méta.

    Une valeur DATE SEULE désigne la journée entière : « jusqu'au 5 » inclut le 5
    (sinon `<= '2026-08-05'` = minuit, et la journée saisie disparaît du résultat —
    le piège classique d'un filtre de date sur un timestamp). Une valeur avec heure
    se compare telle quelle. `col` vient de nos propres dicts (jamais de la saisie),
    la valeur reste paramétrée.

    ⚠️ « La journée » est celle du fuseau de la session PG (UTC en prod) : une ligne
    touchée à 23h30 à Paris compte pour le lendemain. Assumé tant qu'aucun fuseau
    utilisateur n'est déclaré nulle part — le jour où il l'est, c'est ICI que ça se
    règle (une borne, pas un décalage éparpillé dans les appelants)."""
    day = bool(_DS_DATE_ONLY_RE.match(val))
    if not day and not _DS_TS_RE.match(val):
        raise ValueError(
            f"valeur de date invalide `{val}` — attendu `AAAA-MM-JJ` "
            f"ou `AAAA-MM-JJTHH:MM`")
    if not day:
        sym = {"eq": "=", "ne": "<>"}.get(op) or _DS_CMP_SQL[op]
        return f"{col} {sym} %s::timestamptz", [val]
    lo, hi = "%s::timestamptz", "(%s::date + 1)::timestamptz"
    if op == "gte":
        return f"{col} >= {lo}", [val]
    if op == "lt":
        return f"{col} < {lo}", [val]
    if op == "gt":                       # après CE jour = à partir du lendemain
        return f"{col} >= {hi}", [val]
    if op == "lte":                      # jusqu'à CE jour inclus
        return f"{col} < {hi}", [val]
    window = f"({col} >= {lo} AND {col} < {hi})"
    return (window if op == "eq" else f"NOT {window}"), [val, val]


_DS_MAX_FIELDS_PER_FILTER = 50
_DS_MATCHES = ("any", "all")


def _ds_named_fields(value, quoi: str) -> list[str]:
    """Valide une liste de colonnes DÉCLARÉES par l'appelant (oto#22 barreau 1).

    Une notion vit souvent sur des colonnes numérotées (`contact1_fonction`,
    `contact2_fonction`…) : les interroger ensemble suppose de savoir lesquelles.
    L'appelant les NOMME — le serveur ne reconnaît aucune « famille » à l'orthographe
    d'un nom. Deviner `contact*` réintroduirait la convention de nommage qu'on vient
    de sortir des rôles, et ferait dépendre un résultat de l'orthographe des colonnes :
    une colonne renommée changerait un chiffre, sans que rien ne le signale.

    Une liste VIDE est refusée plutôt qu'ignorée : elle ne porterait sur rien, donc
    rendrait toutes les lignes — une réponse qui a l'air d'en être une."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(
            f"`{quoi}` doit être une LISTE NON VIDE de noms de colonnes — vide, "
            f"le filtre ne porterait sur rien et rendrait toutes les lignes")
    if len(value) > _DS_MAX_FIELDS_PER_FILTER:
        raise ValueError(
            f"`{quoi}` : {len(value)} colonnes déclarées, maximum "
            f"{_DS_MAX_FIELDS_PER_FILTER}")
    for k in value:
        if not isinstance(k, str) or not k:
            raise ValueError(
                f"`{quoi}` contient une entrée non textuelle ({k!r}) — chaque "
                f"membre est un nom de colonne")
    return list(value)


def _ds_filter_targets(f: dict) -> list[str]:
    """Les colonnes qu'un filtre VISE : une (`field`) ou plusieurs (`fields`)."""
    field, fields = f.get("field"), f.get("fields")
    if fields is not None and field is not None:
        raise ValueError(
            "filtre : `field` et `fields` sont exclusifs — nommer UNE colonne, ou "
            "déclarer la liste des colonnes membres, jamais les deux")
    if fields is not None:
        return _ds_named_fields(fields, "fields")
    if not isinstance(field, str) or not field:
        raise ValueError("invalid filter: `field` manquant ou non textuel")
    return [field]


def _ds_filter_joiner(f: dict) -> str:
    """`any` (défaut) : une colonne qui satisfait suffit. `all` : toutes.

    Les deux sont nécessaires, et `all` n'est pas la négation d'`any` : « aucun des
    trois rangs n'a de contact » (`empty`+`all`) ne s'obtient pas en niant « au moins
    un rang a un contact ». Sans `all`, le complément d'une mesure serait inexprimable
    — et c'est en général la moitié qu'on cherche."""
    m = f.get("match") or "any"
    if m not in _DS_MATCHES:
        raise ValueError(
            f"filtre : `match` inconnu `{m}` — `any` (une colonne suffit, défaut) "
            f"ou `all` (toutes les colonnes déclarées)")
    return " OR " if m == "any" else " AND "


def _ds_filter_clauses(filters: Optional[list]) -> tuple[list[str], list]:
    """Construit les fragments WHERE (combinés en AND) pour des filtres par colonne
    JSONB — **ou par colonne méta** (`_updated_at`/`_created_at`, dates système ;
    `_id`), routées vers la vraie colonne au lieu de `data ->>` (cf.
    `_DS_META_TS_COLS`). Champ paramétré + op whitelisté → pas d'injection. Les comparaisons
    ordonnées (`gt/gte/lt/lte`) sont numériques si la valeur EST numérique (cast
    gardé `::numeric`, les rows non numériques sont écartées), sinon textuelles
    (l'ISO `YYYY-MM-DD` se compare correctement en lexicographique). Lève
    `ValueError` sur un filtre malformé (→ 400 côté route).

    Un filtre vise UNE colonne (`field`) ou PLUSIEURS déclarées (`fields` + `match`,
    oto#22) : le prédicat est alors évalué sur chaque membre et les résultats joints
    en OR (`any`) ou en AND (`all`). Le filtre reste UN filtre — il se croise en AND
    avec les autres, comme n'importe lequel."""
    clauses: list[str] = []
    params: list = []
    if not filters:
        return clauses, params
    if len(filters) > _DS_MAX_FILTERS:
        raise ValueError("too many filters")
    for f in filters:
        if not isinstance(f, dict):
            raise ValueError("invalid filter")
        targets = _ds_filter_targets(f)
        op, val = f.get("op"), f.get("value")
        if op not in _DS_FILTER_OPS:
            # Dire QUELS opérateurs existent : « invalid filter » nu obligeait à
            # deviner (ou à renoncer et tout rapatrier pour filtrer en local).
            raise ValueError(
                f"opérateur de filtre inconnu `{op}` sur la colonne "
                f"`{'`, `'.join(targets)}` — "
                f"disponibles : {', '.join(sorted(_DS_FILTER_OPS))}")
        joiner = _ds_filter_joiner(f)
        subs: list[str] = []
        subparams: list = []
        for field in targets:
            clause, cparams = _ds_one_field_clause(field, op, val)
            if clause is None:      # filtre inerte (ex. `in` sur une liste vide)
                continue
            subs.append(clause)
            subparams.extend(cparams)
        if not subs:
            continue
        # Une cible unique ne se parenthèse pas : la forme `field` porte tout
        # l'existant, et son fragment doit rester identique au caractère près.
        clauses.append(subs[0] if len(subs) == 1 else "(" + joiner.join(subs) + ")")
        params.extend(subparams)
    return clauses, params


def _refus_in_vide(field: str, val) -> ValueError:
    """Le refus d'un filtre `in` dont la liste ne porte aucune valeur utilisable
    (oto-backend#353).

    ⚠️ Ce filtre DISPARAISSAIT — `if not vals: return None, []` — et la requête rendait
    alors TOUT au lieu de rien : l'inverse exact de ce que `IN ()` veut dire. Une
    moisson de tableau entier a été vécue les 15-16/08 par ce chemin.

    ⚠️ **Pourquoi refuser plutôt que rendre zéro ligne**, alors que zéro serait la
    sémantique SQL juste : une liste vide est presque toujours un accident de
    l'appelant — une variable non remplie, une liste filtrée à zéro juste avant. Rendre
    zéro le laisserait conclure « cette donnée n'existe pas », ce qui est faux et
    silencieux ; refuser l'arrête là où il peut encore corriger. Le refus NOMME la
    colonne et ce qu'il a reçu, pour qu'il regarde sa variable plutôt que ses données.
    """
    return ValueError(
        f"filtre `{field}` : la liste de `in` ne porte aucune valeur "
        f"(reçu {val!r}). Rien n'a été lu — un `in` vide n'est pas une demande "
        f"lisible : selon la lecture, il rendrait TOUT le tableau ou rien. "
        f"Vérifie la liste que tu passes ; pour viser les lignes SANS valeur, "
        f"c'est `{{\"{field}\": {{\"empty\": true}}}}`.")


def _ds_one_field_clause(field: str, op: str, val) -> tuple[Optional[str], list]:
    """Le prédicat sur UNE colonne — `(fragment, params)`. Depuis #353 il n'y a plus
    de forme « inerte » : un filtre qui ne peut pas restreindre LÈVE, il ne s'évapore
    inerte. Point unique : `fields` boucle dessus, il n'en existe pas de copie."""
    if field in _DS_META_TS_COLS:
        if op not in _DS_META_TS_OPS:
            raise ValueError(
                f"opérateur `{op}` non applicable à `{field}` (date système, "
                f"toujours renseignée) — disponibles : "
                f"{', '.join(sorted(_DS_META_TS_OPS))}")
        return _ds_meta_ts_clause(_DS_META_TS_COLS[field], op, str(val))
    if field in _DS_META_TEXT_COLS:
        col = _DS_META_TEXT_COLS[field]
        if op not in _DS_META_TEXT_OPS:
            raise ValueError(
                f"opérateur `{op}` non applicable à `{field}` — disponibles : "
                f"{', '.join(sorted(_DS_META_TEXT_OPS))}")
        if op == "in":
            vals = [str(v) for v in (val if isinstance(val, list) else [val])
                    if v is not None and str(v) != ""]
            if not vals:
                raise _refus_in_vide(field, val)
            return f"{col} = ANY(%s)", [vals]
        if op == "contains":
            return f"{col} ILIKE %s", [f"%{val}%"]
        return f"{col} {'=' if op == 'eq' else '<>'} %s", [str(val)]
    # À partir d'ici, la colonne est lue par l'expression POLYMORPHE (#318) : elle
    # rend la valeur qu'elle soit plate ou à couches. Le champ y passe DEUX fois
    # (un `%s` par branche du COALESCE) — d'où `fp` plutôt que `field` répété à
    # la main, qui est l'endroit exact où un décalage de paramètres se glisse.
    #
    # Nom nu → la valeur ; `champ.source` → la couche. Une seule décision, ici, dont
    # toutes les branches héritent.
    chemin = split_list_path(field)
    if chemin is not None and chemin[1] is None:
        # `contacts[].email` — l'EXISTENCE est intrinsèque à la notation (oto#22 §12) :
        # « il existe un contact dont l'e-mail… ». `match` ne descend jamais ici, il
        # joint les cibles déclarées. C'est le SEUL chemin qui ne rend pas une valeur
        # scalaire, donc le seul qui ne peut pas passer par `field_read_sql`.
        colonne, _, reste = chemin
        V, fp = leaf_read_sql("_i.v", [], reste)
        # #353 : plus de `clause is None` à relayer — `_ds_leaf_predicate` rend
        # toujours un fragment depuis que le `in` vide LÈVE au lieu de disparaître.
        # Garder la branche laisserait croire qu'une clause peut encore s'évaporer.
        clause, cparams = _ds_leaf_predicate(V, fp, op, val, field)
        # La garde de type est OBLIGATOIRE : `jsonb_array_elements` LÈVE sur une
        # valeur qui n'est pas un tableau, et pendant une conversion une partie
        # des lignes ne l'est pas encore — l'état NORMAL, pas un cas limite.
        return (f"EXISTS (SELECT 1 FROM jsonb_array_elements("
                f"CASE WHEN jsonb_typeof(data->%s) = 'array' THEN data->%s "
                f"ELSE '[]'::jsonb END) AS _i(v) WHERE {clause})",
                [colonne, colonne] + cparams)
    V, fp = field_read_sql(field)
    return _ds_leaf_predicate(V, fp, op, val, field)


def _ds_leaf_predicate(V: str, fp: list, op: str, val,
                       field: str = "?") -> tuple:
    """Le prédicat sur une FEUILLE déjà résolue — `(fragment, params)`.

    Séparé de la résolution du chemin pour que les deux vivent au même endroit quel
    que soit le niveau : une colonne, une couche, l'attribut d'un item de liste. Sans
    cette séparation, interroger une liste aurait demandé une seconde copie de toute
    la logique d'opérateurs, et les deux auraient divergé au premier ajout."""
    if op in ("empty", "not_empty"):
        # ⚠️ La VALEUR compte. `{"empty": false}` se lit « pas vide » — c'est
        # la seule lecture possible d'un booléen, et un agent l'écrira. Elle
        # était JETÉE : les deux sens rendaient le même jeu de lignes, donc
        # « quelles valeurs n'ont pas de provenance ? » et son contraire
        # répondaient pareil, sans erreur. Défaut antérieur aux couches — il
        # valait déjà sur une colonne plate.
        veut_vide = (op == "empty") == (val is not False)
        return (f"({V} IS NULL OR {V} = '')" if veut_vide
                else f"({V} IS NOT NULL AND {V} <> '')"), fp + fp
    if op == "in":
        vals = [_ds_text(v) for v in (val if isinstance(val, list) else [val])
                if v is not None and str(v) != ""]
        if not vals:
            raise _refus_in_vide(field, val)
        return f"{V} = ANY(%s)", fp + [vals]
    if op == "contains":
        return f"{V} ILIKE %s", fp + [f"%{_ds_text(val)}%"]
    if op == "eq":
        return f"{V} = %s", fp + [_ds_text(val)]
    if op == "ne":
        return f"({V} IS DISTINCT FROM %s)", fp + [_ds_text(val)]
    # gt/gte/lt/lte
    sym = _DS_CMP_SQL[op]
    sval = _ds_text(val)
    if _DS_NUM_RE.match(sval):
        return (f"({V} ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                f"AND ({V})::numeric {sym} %s::numeric)"), fp + fp + [sval]
    return f"{V} {sym} %s", fp + [sval]


def _ds_where(ns_id: int, q: Optional[str], filters: Optional[list]) -> tuple[str, list]:
    """Clause WHERE partagée par list/count (même filtrage → total cohérent)."""
    where = "WHERE ns_id = %s"
    params: list = [ns_id]
    if q:
        # Recherche plein-texte sur tout le JSON. ACCENT-INSENSIBLE (#67 V2.3) :
        # même repli d'accents `_fold` qu'`oto_search` → « café » trouve « cafe » et
        # inversement (fin de la divergence « sans accents repliés »). Reste un substring
        # (matching partiel conservé, choix de la file feed) — l'alignement en tsquery
        # tokenisée est un arbitrage distinct.
        from .projects import _fold  # lazy : projects importe datastore (évite le cycle)
        where += (f" AND {_fold(ROW_VALUES_TEXT_SQL)} ILIKE"
                  f" '%%' || {_fold('%s')} || '%%'")
        params.append(q)
    fclauses, fparams = _ds_filter_clauses(filters)
    for c in fclauses:
        where += f" AND {c}"
    params.extend(fparams)
    return where, params


_NUMERIC_RE = r'^\s*-?[0-9]+(\.[0-9]+)?\s*$'

# Un horodatage ISO 8601 tel que le datastore en reçoit : date seule, ou date +
# heure avec séparateur `T` ou espace, secondes et fraction optionnelles, décalage
# `Z` ou `±HH:MM` optionnel. Motif POSIX (pas de `\d` côté PostgreSQL).
#
# ⚠️ Il ne valide pas une date, il reconnaît une FORME castable — `2026-02-31`
# passe le motif et fait échouer le cast. Le bloc conforme le sait : la garde sert
# à ne caster que ce qui a une chance, pas à remplacer la validation d'écriture.
_ISO_DT_RE = (r'^[0-9]{4}-[0-9]{2}-[0-9]{2}'
              r'([T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?'
              r'(Z|[+-][0-9]{2}:?[0-9]{2})?)?$')


def _order_guards(value_sql: str, value_params: list, order_type: str,
                  options) -> tuple:
    """Les trois prédicats d'un tri typé (#336), fragments + params appariés :
    `(vide, vide_p, conforme, conforme_p, typé, typé_p)`.

    Partagés entre l'ORDER BY et le compteur d'écart — deux copies divergeraient
    exactement comme le filtre et le tri avaient divergé (le même champ juste à
    une question, faux à l'autre)."""
    vide = f"({value_sql} IS NULL OR {value_sql} = '')"
    vide_p = list(value_params) * 2
    if order_type == "number":
        # La même garde que le filtre : caster une valeur non numérique ferait
        # échouer la requête ENTIÈRE — le CASE ne caste que ce qui est conforme.
        conforme = f"{value_sql} ~ %s"
        conforme_p = list(value_params) + [_NUMERIC_RE]
        typed = f"({value_sql})::numeric"
        typed_p = list(value_params)
    elif order_type == "enum":
        conforme = f"{value_sql} = ANY(%s::text[])"
        conforme_p = list(value_params) + [list(options or [])]
        typed = f"array_position(%s::text[], {value_sql})"
        typed_p = [list(options or [])] + list(value_params)
    else:
        # date/datetime — le tri était TEXTUEL, sur l'idée qu'« ISO trie juste par
        # l'alphabet ». C'est vrai d'un seul format dans un seul fuseau, et faux
        # dès qu'une colonne en mélange deux, ce qu'aucune validation d'écriture
        # n'empêche (oto-backend#859). Deux ruptures mesurées :
        #   • un DÉCALAGE horaire range à l'envers — `…T23:00:00+02:00` (21 h UTC)
        #     passe après `…T22:00:00Z` alphabétiquement, avant temporellement ;
        #   • `conforme = TRUE` déclarait TOUTE valeur rangeable, donc le compteur
        #     d'écart du tri restait à zéro par construction et une valeur qui
        #     n'est pas une date du tout se rangeait comme si elle en était une.
        #     La garde ne gardait rien : elle affirmait.
        # On caste donc en `timestamptz`, qui ramène tout à un instant — et on ne
        # caste QUE ce qui a la forme, sans quoi une seule valeur libre ferait
        # échouer la requête entière (la raison d'être de la garde du bloc number).
        # ⚠️ Une date SEULE est prise à minuit dans le fuseau de la base : deux
        # valeurs du même jour à quelques heures d'écart peuvent donc se ranger
        # autrement qu'attendu. C'est le prix d'une colonne qui mélange les formats,
        # et le vrai remède est la normalisation à l'écriture.
        conforme = f"{value_sql} ~ %s"
        conforme_p = list(value_params) + [_ISO_DT_RE]
        typed = f"({value_sql})::timestamptz"
        typed_p = list(value_params)
    return vide, vide_p, conforme, conforme_p, typed, typed_p


def typed_order_sql(value_sql: str, value_params: list, order_type: str,
                    options, direction: str) -> tuple[str, list]:
    """L'ORDER BY d'un tri typé (#336) : trois blocs, dans cet ordre quel que
    soit le sens — conformes (triés selon le type, ASC/DESC demandé), puis non
    conformes (bloc alphabétique : une valeur qu'on ne sait pas ranger ne
    s'intercale pas et ne prend jamais la tête), puis vides (une absence n'est
    pas une donnée mal rangée)."""
    vide, vide_p, conforme, conforme_p, typed, typed_p = _order_guards(
        value_sql, value_params, order_type, options)
    sql = (f"CASE WHEN {vide} THEN 2 WHEN {conforme} THEN 0 ELSE 1 END, "
           f"CASE WHEN NOT {vide} AND {conforme} THEN {typed} END {direction}, "
           f"{value_sql} ASC, row_id {direction}")
    params = (vide_p + conforme_p                      # bloc sélecteur
              + vide_p + conforme_p + typed_p          # valeur typée
              + list(value_params))                    # bloc alphabétique
    return sql, params


def order_health_sql(value_sql: str, value_params: list, order_type: str,
                     options) -> tuple[str, list]:
    """Les deux compteurs d'écart du tri typé — `off_type` et `empty` — en une
    projection `COUNT(*) FILTER`, sur le même WHERE que la page (le compteur
    décrit le JEU filtré entier, jamais la page — sinon il ment dès la page 2)."""
    vide, vide_p, conforme, conforme_p, _t, _tp = _order_guards(
        value_sql, value_params, order_type, options)
    sql = (f"COUNT(*) FILTER (WHERE NOT {vide} AND NOT {conforme}) AS off_type, "
           f"COUNT(*) FILTER (WHERE {vide}) AS empty")
    return sql, vide_p + conforme_p + vide_p


def _metric_filter_sql(m: dict) -> tuple[str, list]:
    """Le `FILTER (WHERE …)` d'une métrique conditionnelle (oto#22 barreau 1).

    Une métrique porte sa propre condition, dans la MÊME grammaire que `filters`
    (`fields` multi-champs compris). C'est ce qui permet de compter DEUX populations
    dans une seule requête — le total et le sous-ensemble — donc d'obtenir un taux
    sans recouper deux appels dont les périmètres peuvent diverger sans le dire."""
    spec = m.get("where")
    if not spec:
        return "", []
    if not isinstance(spec, (list, tuple)):
        raise ValueError(
            "agrégat : `where` d'une métrique = une LISTE de filtres, même "
            "grammaire que `filters`")
    fc, fp = _ds_filter_clauses(list(spec))
    if not fc:
        return "", []
    return " FILTER (WHERE " + " AND ".join(fc) + ")", fp


def _metric_label(m: dict, defaut: str, pris: set) -> str:
    """Le nom sous lequel la métrique ressort. `label` explicite, sinon dérivé.

    Le dérivé est DÉDOUBLONNÉ : deux `count` (le total et le conditionnel) portaient
    sinon la même clé, et la seconde écrasait la première à la construction du dict —
    un résultat qui a l'air complet, avec une métrique en moins."""
    nom = m.get("label") or defaut
    if not isinstance(nom, str) or not nom:
        raise ValueError("agrégat : `label` doit être un nom non vide")
    base, out, i = nom, nom, 2
    while out in pris:
        out = f"{base}_{i}"
        i += 1
    pris.add(out)
    return out


def _group_fields(group_by) -> Optional[list]:
    """Les colonnes d'un regroupement en UNION, ou None pour un group_by ordinaire."""
    if isinstance(group_by, (list, tuple)):
        return _ds_named_fields(group_by, "group_by")
    return None


def group_key(group_by) -> Optional[str]:
    """La clé sous laquelle la valeur du groupe ressort. Pour une union, elle nomme
    les colonnes mises en commun — la valeur vient de l'une d'elles, et laquelle n'a
    pas de sens : c'est le principe même du « tous rangs confondus »."""
    if isinstance(group_by, (list, tuple)):
        return "|".join(group_by)
    return group_by


def _build_aggregate(ns_id: int, group_by, metrics: Optional[list],
                     q: Optional[str], filters: Optional[list],
                     limit: int) -> tuple[str, list, list]:
    """Construit `(sql, params, names)` de l'agrégat — PUR (aucun I/O), testable sans PG.
    `names` = `[(alias_sql, nom_lisible)]`. Ordre des `%s` : colonnes SELECT (group +
    métriques, filtres de métrique inclus) puis LATERAL puis WHERE puis LIMIT —
    l'ordre de `params` doit suivre EXACTEMENT.
    Les noms de champs passent en PARAMÈTRES, jamais interpolés (anti-injection) —
    DEUX fois chacun depuis #318, un par branche du COALESCE qui lit une colonne
    plate ou à couches. L'ordre de `sparams` en dépend.

    `group_by` accepte une LISTE de colonnes (oto#22) : leurs valeurs sont alors mises
    en commun, une ligne contribuant une occurrence par colonne renseignée — la
    « répartition tous rangs confondus ». Le dégroupement passe par un `LATERAL
    (VALUES …)`, dont les paramètres s'insèrent entre ceux du SELECT et ceux du WHERE."""
    metrics = metrics or [{"op": "count"}]
    pooled = _group_fields(group_by)
    select, sparams, names = [], [], []  # noms lisibles alignés sur les alias mN
    lateral, lparams = "", []
    if pooled:
        vals = []
        for k in pooled:
            _v, _vp = field_read_sql(k)
            vals.append(f"({_v})")
            lparams.extend(_vp)
        lateral = " , LATERAL (VALUES " + ", ".join(vals) + ") AS _u(v)"
        select.append("_u.v AS grp")
    elif group_by:
        _v, _vp = field_read_sql(group_by)
        select.append(f"{_v} AS grp")
        sparams.extend(_vp)
    pris: set = set()
    for i, m in enumerate(metrics):
        op = str(m.get("op", "")).lower()
        field = m.get("field")
        alias = f"m{i}"
        fsql, fparams = _metric_filter_sql(m)
        if op == "count" and not field:
            select.append(f"COUNT(*){fsql} AS {alias}")
            sparams.extend(fparams)
            names.append((alias, _metric_label(m, "count", pris)))
        elif op == "count_rows":
            # Sous une union, `count` compte les OCCURRENCES (deux contacts sur la
            # même fiche font deux) : le nombre de FICHES est une autre question, et
            # les confondre donne un chiffre plausible et faux. Hors union, les deux
            # coïncident — `row_id` est unique par ligne.
            select.append(f"COUNT(DISTINCT row_id){fsql} AS {alias}")
            sparams.extend(fparams)
            names.append((alias, _metric_label(m, "count_rows", pris)))
        elif op == "count":
            _v, _vp = field_read_sql(field)
            select.append(f"COUNT({_v}){fsql} AS {alias}")
            sparams.extend(_vp + fparams)
            names.append((alias, _metric_label(m, f"count_{field}", pris)))
        elif op in ("sum", "avg", "min", "max"):
            if not field:
                raise ValueError(f"agrégat: op '{op}' exige un `field`")
            _v, _vp = field_read_sql(field)
            select.append(
                f"{op.upper()}(CASE WHEN {_v} ~ %s "
                f"THEN ({_v})::numeric END){fsql} AS {alias}")
            sparams.extend(_vp + [_NUMERIC_RE] + _vp + fparams)
            names.append((alias, _metric_label(m, f"{op}_{field}", pris)))
        else:
            raise ValueError(
                f"agrégat: op inconnu {op!r} (count|count_rows|sum|avg|min|max)")
    where, wparams = _ds_where(ns_id, q, filters)
    if pooled:
        # Un rang vide ou absent n'est pas un contact : il ne fabrique pas un groupe.
        where += " AND _u.v IS NOT NULL AND _u.v <> ''"
    sql = f"SELECT {', '.join(select)} FROM datastore_rows{lateral} {where}"
    params = sparams + lparams + wparams
    if group_by:
        sql += " GROUP BY grp ORDER BY m0 DESC NULLS LAST, grp ASC"
    sql += " LIMIT %s"
    params.append(limit)
    return sql, params, names
