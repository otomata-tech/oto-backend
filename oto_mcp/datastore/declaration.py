"""LIRE une déclaration de colonne — « que déclare ce schéma ? », et rien d'autre.

Un seul type de fonction ici : elle prend un schéma (ou un field-def) et rend ce
qu'il DIT. Aucune ne juge une donnée, aucune ne refuse quoi que ce soit. C'est la
couche que tout le reste interroge pour savoir de quoi il parle :

- la liste des champs et sa traversée récursive (`_fields`, `_walk_fields`,
  `declares_field`) ;
- les contraintes portées par un champ (`max_length_of`, `pattern_of`) ;
- ce que le premier niveau expose en bloc (`top_level_bounds`, `top_level_keys`,
  `top_level_options`, `top_level_patterns`, `order_spec`) ;
- les champs désignés par leur STRUCTURE (`status_field` = qui porte le `lifecycle`,
  `title_field` = qui porte `display: "title"`) ;
- les crans qui décident d'un régime (`validation_active`, `key_required_of`,
  `readonly_fields`, `system_origin_fields`) ;
- le vocabulaire des types (`SCALAR_TYPES`, `COMPOSITE_TYPES`).

**La distinction avec `definition.py` est la seule qui compte ici** : ce module lit
un schéma qu'on suppose valide ; `definition.py` juge si un schéma EST valide. Les
deux regardent le même objet et ne répondent pas à la même question — les mélanger
est ce qui rendait leur code indistinguable.

Ce qu'il ne tient pas :
- **la validité du schéma lui-même** → `definition.py` ;
- **le refus d'une VALEUR** contre ce qui est ici déclaré → `validation.py` ;
- **la liste fermée des attributs qu'une colonne peut porter** → `schema_keys.py` ;
- **ce que la plateforme fait vraiment de ce qui est déclaré** → `vocabulaire.py`
  (ce qu'elle interprète) et `non_applique.py` (ce qu'elle laisse inerte).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

from .couches import split_layer, SYSTEM_ORIGIN
from .motifs import PATTERN_MAX_SUBJECT, pattern_refusal

# validation reste volontairement permissive — le schéma guide le rendu, il ne
# transforme pas le datastore en base contrainte.
SCALAR_TYPES = ("text", "number", "date", "datetime", "bool", "json",
                "url", "email", "enum")
COMPOSITE_TYPES = ("object", "list")


def _fields(schema: Optional[dict]) -> list[dict]:
    return [f for f in (schema or {}).get("fields") or [] if isinstance(f, dict)]


def champ_declare(schema: Optional[dict], key: str) -> Optional[dict]:
    """La DÉCLARATION d'une colonne de premier niveau, ou `None`.

    Jumelle de `declares_field`, qui rend un booléen : celle-ci rend l'objet, pour qui
    a besoin de LIRE ce que la colonne déclare — l'identité de ses éléments (`of.key`),
    son type, ses options. Écrite ici plutôt que chez l'appelant pour la raison
    habituelle : deux recherches de champ divergeraient au premier cas limite (un
    `key` non-chaîne, un `fields` qui n'est pas une liste)."""
    if not isinstance(key, str):
        return None
    for f in _fields(schema):
        if isinstance(f, dict) and f.get("key") == key:
            return f
    return None


def declares_field(schema: Optional[dict], key: str) -> bool:
    """Le schéma déclare-t-il un field top-level de cette clé ? La reconnaissance
    par DÉCLARATION (#354) : c'est elle qui distingue une colonne de données
    légitime (un CSV importé porte souvent une colonne `id`) d'un identifiant de
    ligne égaré dans le corps — jamais une devinette sur la forme de la valeur."""
    return any(f.get("key") == key for f in _fields(schema))


def _walk_fields(fields: list) -> Iterator[dict]:
    """Tous les fields, sous-records COMPRIS (`object.fields`, `list.of[.fields]`)."""
    for f in fields:
        if not isinstance(f, dict):
            continue
        yield f
        if isinstance(f.get("fields"), list):
            yield from _walk_fields(f["fields"])
        of = f.get("of")
        if isinstance(of, dict):
            yield from _walk_fields([of])


def max_length_of(field: dict) -> Optional[int]:
    """La borne de longueur déclarée sur un field, si elle est exploitable.

    Volontairement muette sur une déclaration mal formée (`max_length: "60"`, 0,
    négative) : c'est `_validate_fields_def` qui la REFUSE à la pose du schéma.
    Ici on ne fait qu'appliquer ce qui est valide — un schéma déjà en base, posé
    quand la clé était encore ignorée, ne doit pas faire exploser une écriture."""
    ml = field.get("max_length")
    if isinstance(ml, bool) or not isinstance(ml, int) or ml <= 0:
        return None
    # Une borne sur un composite n'a pas de sens (longueur de quoi ?) et la
    # définition la refuse ; si elle traîne dans un vieux schéma, on l'ignore.
    return None if field.get("type") in COMPOSITE_TYPES else ml


def pattern_of(field: dict) -> Optional[str]:
    """Le motif déclaré sur un field, S'IL est exploitable en sûreté — sinon None.

    Même parti pris que `max_length_of` : volontairement muette sur une déclaration
    qu'on ne sait pas exécuter, parce qu'un schéma déjà en base — posé quand la clé
    était encore ignorée — ne doit pas faire exploser une écriture. C'est
    `_validate_fields_def` qui REFUSE, à la pose, devant celui qui peut corriger.

    Trois conditions, chacune vérifiée à la pose : une chaîne, sur un champ scalaire,
    et sur un champ BORNÉ. La borne n'est pas un confort — c'est elle qui rend le
    coût du motif majorable (cf. `pattern_refusal`)."""
    src = field.get("pattern")
    if not isinstance(src, str) or not src:
        return None
    if field.get("type") in COMPOSITE_TYPES:
        return None
    ml = max_length_of(field)
    if not ml or ml > PATTERN_MAX_SUBJECT:
        return None
    return None if pattern_refusal(src, ml) else src


def top_level_bounds(schema: Optional[dict]) -> dict[str, int]:
    """`{clé: max_length}` des champs BORNÉS de premier niveau — ceux qu'une requête
    SQL sait mesurer (`data->>clé`). Sert l'avertissement « des lignes existantes
    dépassent déjà » à la pose du schéma.

    ⚠️ Les cibles de COUCHE (#377) en sont exclues : `data->>'q.comment'` mesurerait
    une colonne littérale qui n'existe pas, donc rendrait « aucune ligne hors borne »
    sur un tableau que personne n'a vérifié — un silence qui ferait croire la table
    conforme. La borne, elle, s'applique bien : `validate_row` la fait respecter sur
    la valeur de la couche. Ce qui manque ici est l'avertissement sur l'EXISTANT,
    et il manque franchement plutôt qu'en mentant."""
    out: dict[str, int] = {}
    for f in _fields(schema):
        key, ml = f.get("key"), max_length_of(f)
        if isinstance(key, str) and key and ml and not split_layer(key)[1]:
            out[key] = ml
    return out


def top_level_keys(schema: Optional[dict]) -> set:
    """Colonnes DÉCLARÉES au premier niveau — la réponse à « cette colonne
    existe-t-elle ? ».

    Le schéma est la seule source de vérité là-dessus, et c'est pour ça que ce
    helper existe : dans une row JSONB STOCKÉE, **une colonne vide n'existe pas** (il
    n'y a pas de case vide, il n'y a pas de case). La ligne SERVIE, elle, la complète
    à `null` depuis oto#182 (`cles_declarees`). Une colonne déclarée mais renseignée
    sur 12 lignes de 500 est donc ABSENTE d'une page où aucune des 12 ne figure —
    et un contrôle qui échantillonne les lignes rendues la déclare inconnue.
    """
    return {str(f["key"]) for f in _fields(schema) if f.get("key")}


def cles_declarees(schema: Optional[dict]) -> list[str]:
    """Les colonnes DÉCLARÉES au premier niveau, dans l'ORDRE du schéma, sans doublon.

    Sert la complétion de la ligne servie (oto#182) : toute colonne déclarée y figure,
    à `null` quand aucune valeur n'est en place. L'ordre compte — `top_level_keys` rend
    un ensemble, dont l'itération changerait d'un processus à l'autre et avec elle
    l'ordre des clés servies.
    """
    vues: dict[str, None] = {}
    for f in _fields(schema):
        cle = f.get("key")
        if isinstance(cle, str) and cle:
            vues.setdefault(cle, None)
    return list(vues)


def top_level_options(schema: Optional[dict]) -> dict:
    """`{champ: [options]}` des champs de premier niveau porteurs d'une liste de
    valeurs non vide — **quel que soit leur type scalaire** (#98), plus seulement
    `enum`.

    Jusqu'au 10/09/2026 elle s'appelait `top_level_enum_options` et ne rendait que les
    enums. Or `options` est l'attribut canonique de la liste fermée, et il était
    ACCEPTÉ sans un mot sur les dix autres types : un propriétaire qui restreignait une
    colonne texte à quatre valeurs, sur un tableau strict, obtenait une déclaration
    acceptée et aucune restriction — ni refus, ni signalement.

    Exclus : les composites (`object`/`list` — leurs éléments se jugent par la récursion
    de la validation) et les cibles de couche. Restreint au premier niveau comme
    `top_level_bounds` : c'est ce qu'une requête `data->>champ` sait interroger sur
    l'existant. Un enum sans `options` est un enum LIBRE (le client rend un select
    vide) — il ne condamne rien."""
    out: dict = {}
    for f in _fields(schema):
        key = f.get("key")
        # Même raison que `top_level_bounds` : une cible de couche n'est pas
        # interrogeable par `data->>champ`, l'annoncer ferait porter le réglage
        # d'un écran sur une colonne qui n'existe pas.
        if not key or f.get("type") in COMPOSITE_TYPES or split_layer(key)[1]:
            continue
        opts = [str(o) for o in (f.get("options") or [])]
        if opts:
            out[str(key)] = opts
    return out


def order_spec(schema: Optional[dict], key) -> tuple:
    """`(type, options)` qui rend le TRI typé pour ce champ — `(None, None)` sinon.

    Le tri honore le type DÉCLARÉ (#336) : `number` → cast numérique, `enum` →
    rang d'option, `date`/`datetime` → texte (ISO trie juste par l'alphabet) mais
    vides-en-queue. Tout le reste — text, non déclaré, composite, chemin
    `col[0].attr`, couche `champ.source` — garde le tri textuel historique : ce
    helper ne matche que la CLÉ EXACTE d'un champ de premier niveau, comme
    `top_level_options`, parce que c'est ce que `data->>champ` sait trier.
    Un enum sans `options` est un enum LIBRE : rien à ranger, tri textuel."""
    if not isinstance(key, str):
        return (None, None)
    for f in _fields(schema):
        if str(f.get("key") or "") != key:
            continue
        ftype = f.get("type")
        if ftype == "number":
            return ("number", None)
        if ftype in ("date", "datetime"):
            return ("date", None)
        if ftype == "enum":
            opts = [str(o) for o in (f.get("options") or [])]
            return ("enum", opts) if opts else (None, None)
        return (None, None)
    return (None, None)


# ⚠️ `field_by_role` est SUPPRIMÉE (08/09/2026). Elle n'avait plus aucun appelant —
# `status` est désigné par le bloc `lifecycle`, `title` par `display: "title"` — mais
# sa seule présence dans le source suffisait à faire déclarer `role` comme APPLIQUÉ
# par `interpreted_keys()`, qui dérive du code plutôt que d'une liste.
#
# La plateforme annonçait donc appliquer une clé que plus personne ne lisait. Le module
# l'avait prévu en toutes lettres : « c'est ce que `lifecycle` et `role` s'apprêtent à
# faire — les figer laisserait la clé dans le vocabulaire après que le code aura cessé
# de la lire ». Une fonction morte n'est pas neutre quand un inventaire se dérive du
# source : **elle ment pour le compte de ceux qui l'ont abandonnée.**


#: Ce qui fait d'un `lifecycle` une FILE plutôt qu'une simple suite d'états : un
#: périmètre de réservation, un plafond de reprises, un état d'abandon. Dérivé des
#: schémas de production, pas décrété — c'est la distinction qu'ils portaient déjà.
FILE_KEYS = ("claimable", "max_claims", "abandon_state", "lease", "claims")


def status_field(schema: Optional[dict]) -> Optional[dict]:
    """La colonne de FILE : celle dont le `lifecycle` déclare un périmètre de
    réservation (`claimable`, `max_claims`, `abandon_state`). À défaut, la seule qui
    porte un bloc.

    ⚠️ **Un tableau peut porter PLUSIEURS cycles de vie, et c'est légitime** — corrigé
    le 08/09/2026, avant la mise en production, sur signalement d'une campagne. J'avais
    posé « un seul par tableau », ce qui aurait rendu quatre tableaux de production non
    modifiables : ils portent **deux avancements pour deux acteurs** — `statut`, la
    file que drainent les agents, et `suivi`, les états commerciaux qu'un humain suit
    à l'écran. Ce n'est pas une ambiguïté, ce sont deux choses différentes sur la même
    ligne.

    **Ce qui les distingue est déjà dans les données** : mesuré sur le parc entier, les
    blocs de file déclarent `claimable`/`max_claims`/`abandon_state`, les blocs d'états
    humains ne portent que `states` et `terminal` — et **aucun tableau ne porte deux
    blocs de file**. La règle n'a donc rien à deviner : elle lit ce que les schémas
    disent déjà.

    ⚠️ **Ce que ça ne fait pas encore, et qu'il faut savoir** : seule la colonne rendue
    ici voit ses transitions validées. Un second bloc est stocké, servi, lu par son
    consommateur — mais oto ne contrôle pas ses états. Valider chaque colonne sur son
    propre bloc est la suite juste ; ce n'était pas le moment de l'improviser.

    ⚠️ Et le cas limite reste : un tableau avec deux blocs SANS file (un seul dans le
    parc). Le premier déclaré gagne — c'est le comportement d'avant, conservé pour ne
    rien casser, et c'est exactement la devinette silencieuse que ce lot voulait
    supprimer. Elle sera fermée par la validation par colonne, pas par un refus qui
    bloquerait un tableau vivant.
    """
    porteurs = [f for f in _fields(schema)
                if isinstance(f.get("lifecycle"), dict) and isinstance(f.get("key"), str)]
    if not porteurs:
        return None
    # La colonne de FILE d'abord : celle dont le bloc déclare un périmètre de
    # réservation, un plafond de reprises ou un état d'abandon. C'est elle que
    # `data_claim_next` réserve et que le bail libère.
    for f in porteurs:
        if any(k in f["lifecycle"] for k in FILE_KEYS):
            return f
    return porteurs[0]


# La PRÉSENTATION d'une colonne — ce que sa valeur sert à l'écran, par opposition à
# `type` qui dit ce qu'elle EST (#317, « voie Notion »). Les deux dimensions sont
# ORTHOGONALES, et c'est une mesure qui l'a établi : sur les 57 titres de production,
# **six ne sont pas du texte** (cinq `url`, une `date`). En faire une valeur de `type`
# aurait forcé à choisir — un titre qui est une URL aurait cessé d'être rendu en lien.
#
# Un champ, une présentation ; un tableau, un titre.
DISPLAY_TITLE = "title"


def title_field(schema: Optional[dict]) -> Optional[dict]:
    """La colonne qui NOMME une ligne — ce qu'un humain reconnaît dans un journal, à
    la place d'un uuid.

    ⚠️ **Plus de repli sur `role="title"`** (#317 étape C) : il a vécu le temps de la
    conversion des schémas en base, et il meurt ici. Un repli qui survit à sa raison
    devient le canal par lequel ce qu'on retire revient — un schéma neuf déclarant un
    rôle continuerait de marcher, et le rôle ne serait jamais parti.

    La conversion au boot est passée sur les 57 tableaux (additive, idempotente) :
    un schéma qui n'aurait QUE le rôle ne nomme plus sa ligne, et retombe sur la clé
    métier puis l'identifiant, comme un tableau sans titre."""
    for f in _fields(schema):
        if f.get("display") == DISPLAY_TITLE and f.get("key"):
            return f
    return None


def validation_active(schema: Optional[dict]) -> bool:
    """La validation d'écriture est OPT-IN : `schema.strict` truthy, OU au moins un
    field déclarant `required`/`required_when`/`max_length`. Sans ça, écriture
    soft (0016).

    `max_length` compte au même titre que `required` — sans quoi une borne posée
    sur un schéma qui n'a aucun requis serait INERTE, silencieusement (signal
    #383). Elle est cherchée en PROFONDEUR (sous-records inclus), là où
    required/required_when sont lus sur les seules entrées DÉCLARÉES ICI : élargir
    ces deux-là activerait rétroactivement la validation de schémas déjà posés,
    alors que déclarer une borne EST la demande de la faire respecter.

    ⚠️ « entrée déclarée ici » ≠ « colonne » depuis #377 : une cible de COUCHE
    (`qualification.comment`) est une entrée de cette liste comme une autre, et
    active donc bien la validation. Ce qui reste hors de portée, c'est la
    profondeur — un requis enfoui dans un sous-record."""
    if not isinstance(schema, dict):
        return False
    if schema.get("strict"):
        return True
    if any(f.get("required") or f.get("required_when") for f in _fields(schema)):
        return True
    return any(max_length_of(f) for f in _walk_fields(_fields(schema)))


def key_required_of(schema: Optional[dict]) -> bool:
    """Ce tableau n'accepte-t-il QUE des écritures visant une ligne existante ?

    `schema.key_required` (#516) — opt-in, à côté de la clé métier qu'il durcit. Sur
    un tableau qui le porte, une écriture qui ne désigne aucune ligne (ni par son
    identifiant, ni par une valeur de `key` que le tableau porte déjà) est REFUSÉE au
    lieu d'en créer une. Le défaut reste la création, signalée par un `notices`
    (#390) : un tableau se remplit souvent avant d'avoir sa clé, et le cran est une
    déclaration de son propriétaire, jamais une politique de plateforme.

    ⚠️ **Sans `key` déclarée, il ne s'arme pas.** La combinaison se refuse à la POSE
    (`validate_schema_def`) — mais un schéma déjà en base qui la porterait rendrait
    le tableau inécrivable, et un vieux schéma ne doit pas faire exploser une
    écriture (même parti pris que `max_length_of`/`pattern_of`)."""
    if not isinstance(schema, dict):
        return False
    cle = schema.get("key")
    if not (isinstance(cle, str) and cle):
        return False
    return bool(schema.get("key_required"))


# ── les champs que l'appelant n'écrit pas (#586, #606) ───────────────────────
#
# Deux crans de COLONNE (premier niveau), une garde, un refus qui nomme le champ, la
# raison et où va la chose. Ce qu'ils protègent : la donnée remise par le client,
# contre deux gestes mesurés sur la même campagne le 29/08/2026 — l'écraser (quatorze
# valeurs sur douze fiches par cent, onze sans copie) et détruire sa copie de secours
# (la couche `origine` écrite par l'agent, réécrite par lui une fois sur quarante et
# une). Le geste (lever, poser) est dans `reserves.py` ; la DÉCISION est ici, à côté
# des autres déclarations, et `enforced_keys` la sonde.

def readonly_fields(schema: Optional[dict]) -> set:
    """Les colonnes `readonly: true` (#606) : leur VALEUR ne change pas par une
    écriture. Leurs couches restent ouvertes — `comment` est la destination de ce
    que dit une autre source (« registre — 20 B AVENUE … »), attachée au champ,
    comptable, livrable. `None` = absence (c'est ainsi qu'un patch lève le cran)."""
    return {f["key"] for f in _fields(schema)
            if f.get("readonly") is True
            and isinstance(f.get("key"), str) and f["key"]}


def system_origin_fields(schema: Optional[dict]) -> set:
    """TOUJOURS VIDE — `origine: "system"` est SUPPRIMÉ (décision produit, 08/09/2026).

    Le cran armait une capture automatique : à la première écriture qui changeait une
    valeur, la plateforme figeait la précédente comme origine. Il est remplacé par
    `donnees_d_origine`, qui fige la version d'origine **au moment où la valeur entre**
    — un geste déclaré au lieu d'un filet qui dépend d'un ordre de gestes.

    ⚠️ **Ce que ce retrait change, et il faut le savoir** : plus rien ne capture
    automatiquement. Une valeur écrasée par un agent sur une colonne qui portait le
    cran n'est plus retenue par personne. Sur un tableau de campagne, ce filet avait
    encore retenu 39 valeurs la nuit du 07 au 08/09. La contrepartie est que l'origine
    cesse de dépendre de qui pense à déclarer un cran avant l'import.

    ⚠️ **Les 28 799 couches `origine` déjà en base ne bougent pas** : elles restent
    lues, servies et protégées. C'est le mécanisme qui part, pas la donnée.

    La fonction est GARDÉE et rendue vide plutôt que supprimée : ses six appelants
    s'éteignent alors d'eux-mêmes, au lieu de disparaître un par un au risque d'en
    oublier un — c'est la leçon des quatre chemins d'écriture dont j'en avais branché
    trois. Une déclaration `origine: "system"` qui subsiste dans un schéma devient une
    clé qu'oto n'interprète pas, et l'avertissement des clés non interprétées la
    signale déjà."""
    return set()


def top_level_patterns(schema: Optional[dict]) -> dict:
    """`{clé: motif}` des champs de premier niveau porteurs d'un motif EXPLOITABLE.

    Même restriction que `top_level_bounds` et `top_level_options` : ce que
    `data->>clé` sait relire sur l'existant. Sert l'avertissement « des lignes
    existantes ne suivent déjà pas ce motif » à la pose du schéma."""
    out: dict = {}
    for f in _fields(schema):
        cle, motif = f.get("key"), pattern_of(f)
        if isinstance(cle, str) and cle and motif and not split_layer(cle)[1]:
            out[cle] = motif
    return out
