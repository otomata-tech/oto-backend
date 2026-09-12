"""Ce que CETTE version lit et fait respecter — dérivé du code, jamais recopié.

Un client ne peut pas opposer une documentation au serveur qui lui répond. Ce module
lui rend deux relevés, et **aucun des deux n'est une liste** :

- `enforced_keys` — les clés de validation que ce déploiement EXÉCUTE, établies en
  faisant tourner le validateur sur des sondes (`_ENFORCEMENT_PROBES`) : un schéma
  minimal qui doit être refusé, et parfois un témoin qui doit passer. Une clé est
  annoncée si, et seulement si, elle mord ici et maintenant ;
- `interpreted_keys` / `vocabulaire_vivant` — les clés que le code LIT, dérivées de
  son propre source par AST (`_read_keys`), d'où `unknown_declaration_keys` tire le
  signalement d'un attribut inconnu et `_NEAR_MISS` la suggestion de correction.

⚠️ **`_read_keys` scanne une liste de FICHIERS.** Un module du paquet qui se met à
lire un attribut de colonne doit y être ajouté, sinon le dérivé le déclare mort et
l'avertissement accuse une clé parfaitement lue. C'est la seule dépendance de ce
fichier envers la DISPOSITION du code, et elle est explicite pour cette raison.

⚠️ **« Oto ne les interprète pas », jamais « personne ne les lit »** : la plateforme ne
sait pas qui lit en aval — un consommateur affiche `label`, `help`, `hint` qu'elle ne
regarde pas. Le message n'a le droit de parler que d'elle.

Ce qu'il ne tient pas :
- **la liste DÉCLARÉE des attributs légitimes** (celle qu'on maintient à la main, et
  ce qu'elle a coûté) → `schema_keys.py` et `cles_inconnues.py` ;
- **ce qu'un tableau déclare et que le moteur laisse inerte** → `non_applique.py` ;
- **les clés hors référentiel d'une LIGNE** → `hors_schema.py` : ici, un format.
"""
from __future__ import annotations

from typing import Optional

from . import claimable
from . import schema_keys

from .couches import ORIGIN_LAYER, SYSTEM_ORIGIN
from .declaration import _fields, key_required_of
from .hors_schema import off_schema_refusal
from .champs_reserves import reserved_refusals
from .validation import validate_row

# ── Ce que CETTE version fait respecter (#389) ───────────────────────────────
#
# Le signal qui rendait les autres dangereux : il ne demandait pas une contrainte de
# plus, il demandait de savoir lesquelles MORDENT. Deux cas vécus le même jour, et le
# second est le vrai sujet — l'écart n'était pas dans le vocabulaire mais dans le
# DÉPLOIEMENT. `max_length: 60` posé sur quatre colonnes d'un tableau de production,
# code de validation écrit le jour même, version déployée qui ne l'exécutait pas
# encore : un PATCH idempotent rendait 200, et avec le code à jour 75 lignes sur 600
# devenaient inécritables. Effet DIFFÉRÉ au prochain déploiement, MASSIF, SIMULTANÉ,
# et de cause vieille de plusieurs semaines — personne ne relie « les agents
# n'écrivent plus sur ces lignes » à « quelqu'un a posé une borne un mardi ».
#
# `unknown_declaration_keys` (#316) dit déjà la moitié NÉGATIVE — « cette clé, je ne
# la lis pas ». Il manquait la moitié POSITIVE, la seule qu'un client puisse vérifier
# contre le serveur qui lui répond plutôt que contre une documentation.
#
# ⚠️ **Le relevé s'établit en FAISANT TOURNER le validateur**, jamais en recopiant une
# liste. Une liste parallèle diverge le jour où quelqu'un exécute une clé de plus (ou
# cesse d'en exécuter une), et elle se met alors à mentir dans les deux sens — ce que
# le signal reproche au silence. Chaque sonde est un schéma minimal + une ligne qui le
# viole : la clé est annoncée si, et seulement si, `validate_row` refuse ici et
# maintenant. C'est le même parti que `interpreted_keys` (dérivé du code), poussé d'un
# cran : dérivé du COMPORTEMENT, donc insensible à la façon dont le code est écrit.

# `(clé, schéma qui doit REFUSER, ligne fautive, témoin qui doit PASSER ou None)`.
# Le témoin ne sert qu'aux clés dont l'effet est d'ARMER autre chose : `strict`
# n'interdit rien par lui-même, il rend la conformité de type opposable. Sans le
# témoin, on l'annoncerait dès que le type est vérifié, ce qui serait vrai par
# accident.
_ENFORCEMENT_PROBES = (
    ("required",
     {"fields": [{"key": "x", "required": True}]}, {}, None),
    ("required_when",
     {"fields": [{"key": "x", "required_when": {"y": "1"}}, {"key": "y"}]},
     {"y": "1"}, None),
    ("max_length",
     {"fields": [{"key": "x", "max_length": 1}]}, {"x": "ab"}, None),
    ("pattern",
     {"fields": [{"key": "x", "max_length": 8, "pattern": "^ok$"}]},
     {"x": "non"}, None),
    ("max_items",
     {"strict": True,
      "fields": [{"key": "x", "type": "list", "of": {"type": "text"},
                  "max_items": 1}]},
     {"x": ["a", "b"]}, None),
    # ⚠️ Sonde passée d'`enum` à `text` le 10/09/2026 (#98). Sur un enum, elle
    # annonçait `options` appliquée pendant qu'une liste posée sur un texte, un json ou
    # une colonne sans type ne refusait RIEN, tableau strict compris : le client qui
    # lisait `enforced` se croyait protégé. Elle éprouve désormais le cas général —
    # celui qui était cassé —, et l'annonce retombera si le trou se rouvre.
    ("options",
     {"strict": True,
      "fields": [{"key": "x", "type": "text", "options": ["a"]}]},
     {"x": "b"}, None),
    ("type",
     {"strict": True, "fields": [{"key": "x", "type": "number"}]},
     {"x": "abc"}, None),
    # ⚠️ Sonde CHANGÉE le 08/09/2026, et le motif importe. Elle opposait un schéma
    # strict à un schéma libre sur une valeur de mauvais TYPE — ce qui supposait que le
    # type ne soit pas vérifié sans `strict`. Depuis que le type déclaré s'arme
    # lui-même, les deux refusent, et la sonde concluait que `strict` n'était pas
    # appliqué. Elle mesurait une différence qui n'existe plus.
    # Le témoin repose désormais sur les `options`, qui restent inertes sans `strict`
    # (mesuré le 08/09 : 181 tableaux du parc en portent sans les faire respecter).
    ("strict",
     {"strict": True, "fields": [{"key": "x", "type": "enum", "options": ["a"]}]},
     {"x": "b"},
     ({"fields": [{"key": "x", "type": "enum", "options": ["a"]}]}, {"x": "b"})),
    ("lifecycle",
     {"fields": [{"key": "s", "role": "status",
                  "lifecycle": {"states": ["a", "b"]}}]},
     {"s": "z"}, None),
    # oto#75 : la clé qui a vécu trois schémas de production SANS lecteur. Sa
    # sonde est donc la première chose qu'un client peut opposer au serveur qui
    # lui répond — « ce déploiement l'exécute-t-il, ou est-ce encore une
    # déclaration qui ne contraint rien ? »
    ("required_layers",
     {"fields": [{"key": "x", "required_layers": ["comment"]}]},
     {"x": "une valeur nue"}, None),
)

_ENFORCED: Optional[tuple] = None


def reset_enforced_keys() -> None:
    """Oublie le relevé mémorisé — pour un banc qui désarme une règle et vérifie que
    l'annonce tombe avec elle."""
    global _ENFORCED
    _ENFORCED = None


def enforced_keys() -> list[str]:
    """Les clés de validation que CETTE version EXÉCUTE, triées.

    Rendue à la pose ET à la lecture d'un schéma : un client peut donc vérifier que ce
    qu'il déclare sera appliqué par le serveur qui lui répond — c'est la seule parade
    au décalage entre le code écrit et la version servie."""
    global _ENFORCED
    if _ENFORCED is None:
        vues = []
        for cle, schema, row, temoin in _ENFORCEMENT_PROBES:
            if not validate_row(schema, row):
                continue                      # la règle n'existe pas ici
            if temoin and validate_row(temoin[0], temoin[1]):
                continue                      # elle refuse même sans la clé : pas elle
            vues.append(cle)
        # `key_required` (#516) ne se prouve pas sur une ROW : il se juge contre le
        # CONTENU du tableau (cette clé désigne-t-elle une ligne ?), que `validate_row`
        # ne voit pas. Sa sonde interroge donc la fonction qui DÉCIDE — dérivée du
        # code comme les autres, jamais une ligne de liste : le jour où le cran
        # disparaît, l'annonce tombe avec lui.
        if key_required_of({"key": "x", "key_required": True}):
            vues.append("key_required")
        # #586/#606 : les champs que l'appelant n'écrit pas se jugent sur le GESTE
        # (payload + ligne en place), pas sur une row seule — même sonde que
        # `key_required` : on interroge la fonction qui décide.
        if reserved_refusals({"fields": [{"key": "x", "readonly": True}]},
                             {"x": "b"}, {"x": "a"})[0]:
            vues.append("readonly")
        if reserved_refusals({"fields": [{"key": "x", "origine": SYSTEM_ORIGIN}]},
                             {"x": {ORIGIN_LAYER: "y"}})[0]:
            vues.append("origine")
        # oto#83 : le cran ne mord que sur la face agent — la sonde le dit donc
        # explicitement (`agent=True`), sinon elle mesurerait l'absence de contexte
        # d'appel et annoncerait « pas appliqué » sur un déploiement qui l'applique.
        if reserved_refusals({"fields": [{"key": "x", "agent_access": "none"}]},
                             {"x": "v"}, agent=True)[0]:
            vues.append("agent_access")
        # #614/#678 : le refus de la colonne non déclarée au premier niveau. Il ne se
        # prouve pas sur `validate_row` (le relevé vit hors d'elle, dans `_check_row`,
        # pour rester la source unique du « hors du référentiel ») — sa sonde
        # interroge donc la fonction qui décide, comme `key_required`.
        if off_schema_refusal({"strict": True, "unknown_fields": "reject",
                               "fields": [{"key": "x"}]}, {"inventée": "v"})[0]:
            vues.append("unknown_fields")
        # #517 : le périmètre de réservation se juge au PICK, pas sur une row — la
        # sonde interroge la fonction qui produit les clauses que le pick ajoute.
        if claimable.clauses(claimable.perimetre_of({"claimable": {"x": "1"}})):
            vues.append("claimable")
        _ENFORCED = tuple(sorted(vues))
    return list(_ENFORCED)


# ── Clés de déclaration non interprétées (#316) ──────────────────────────────
#
# Le cas réel : trois champs posés avec `enum: [...]` au lieu d'`options: [...]`.
# La clé a été stockée, rendue fidèlement, affichée — et jamais lue. Les trois
# énumérations étaient LIBRES sans que rien ne le dise, et 504 valeurs sont entrées
# sur un tableau qui se croyait contraint. Comportement conforme au contrat, et
# indistinguable d'un enum contraint À L'USAGE.
#
# ⚠️ **On ne ferme PAS le vocabulaire**, et c'est doctrinal : les consommateurs posent
# leurs propres déclarations (`role: qualif`, `dated_by`, `compare_by`, `initial_of`)
# que le datastore transporte sans les interpréter. Refuser l'inconnu casserait ce
# contrat. On SIGNALE — même patron que `hors_schema` à l'écriture d'une ligne : on
# n'empêche rien, on rend la chose visible et actionnable.


def _read_keys() -> frozenset:
    """Les clés que le code LIT réellement, dérivées de son source.

    ⚠️ **Dérivées, pas listées** — et ce n'est pas du zèle : une liste parallèle du
    vocabulaire diverge le jour où quelqu'un lit une clé de plus (ou cesse d'en lire
    une), et le signal se met alors à mentir dans les deux sens — taire une vraie
    faute de frappe, ou accuser une clé parfaitement lue. C'est exactement ce que
    `lifecycle` et `role` s'apprêtent à faire : ils sont en cours de recadrage
    (#315/#317), et les figer ici en dur les laisserait dans le vocabulaire après
    que le code aura cessé de les lire.

    La dérivation surestime (elle ramasse aussi des clés de ligne ou de datastore,
    `data`, `owner_id`…) et c'est le BON côté de l'erreur : on signale moins, jamais
    à tort. Un faux positif — accuser une clé qui marche — est ce qui ferait ignorer
    l'avertissement, donc le rendrait inutile.
    """
    import ast
    import pathlib

    keys: set = set()
    ici = pathlib.Path(__file__).parent
    # ⚠️ La liste est celle des FICHIERS, jamais celle des clés : un module qui se met
    # à lire un attribut de colonne doit être ajouté ici, sinon le dérivé le déclare
    # mort et l'avertissement accuse une clé parfaitement lue. `acces_agent.py`
    # (oto#83) lit `agent_access` par sa constante — c'est le cas qui l'a imposée.
    #
    # ⚠️ **Le second cas est la COUPE de `schema.py`** : les douze modules ci-dessous en
    # sont issus, et le jour où ils ont été écrits `schema.py` a cessé de contenir la
    # moindre lecture d'attribut. Le dérivé n'y voyait plus rien : c'est exactement le
    # défaut que le paragraphe au-dessus annonce, déclenché par un déplacement qui ne
    # changeait aucun comportement. `schema.py` reste listé — il ne coûte rien et il
    # redeviendrait porteur si quoi que ce soit y revenait.
    #
    # ⚠️ **Le troisième cas est la COUPE de `core.py`** (07/09/2026) : les sept
    # greffons ci-dessous en sont issus, et le noyau qui reste n'y a gardé qu'une
    # poignée de lectures. Le même déplacement pur, le même dérivé qui rétrécit —
    # d'où les sept noms ajoutés le jour même de la coupe.
    for nom in ("schema.py", "core.py", "acces_agent.py",
                "couches.py", "motifs.py", "declaration.py", "cycle_de_vie.py",
                "hors_schema.py", "champs_reserves.py", "definition.py",
                "couches_exigees.py", "validation.py", "phrases_de_refus.py",
                "effacements.py",
                "outils.py", "controles.py", "registre.py", "lecture.py",
                "ecriture.py", "ecriture_par_id.py", "lots.py", "file_de_travail.py",
                "vocabulaire.py", "non_applique.py"):
        try:
            arbre = ast.parse((ici / nom).read_text(encoding="utf-8"))
        # noqa: SILENT — clés de schéma illisibles ⇒ ensemble vide, la lecture continue
        except Exception:      # source illisible (zip, .pyc seul) : on n'invente pas
            return frozenset()
        # Les constantes de MODULE (`CLE = "agent_access"`), pour résoudre
        # `f.get(CLE)` comme `f[CLE]` : sans elles, une clé lue par sa constante — la
        # forme qu'on encourage justement pour ne pas répéter un littéral — passe pour
        # jamais lue, et une garde bâtie dessus l'accuserait d'être morte. C'est ce
        # qui est arrivé à `flat_alias` (retirée depuis) avant que les deux formes ne
        # soient résolues ici ; les DEUX comptent, n'en résoudre qu'une laisse le trou
        # ouvert sur l'autre.
        constantes = {
            n.targets[0].id: n.value.value
            for n in arbre.body
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
        for n in ast.walk(arbre):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and n.args):
                arg = n.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in constantes:
                    keys.add(constantes[arg.id])
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Name) \
                    and n.slice.id in constantes:
                keys.add(constantes[n.slice.id])
    return frozenset(keys)


_READ_KEYS: Optional[frozenset] = None


def interpreted_keys() -> frozenset:
    """Le vocabulaire effectivement interprété — calculé une fois, dérivé du code."""
    global _READ_KEYS
    if _READ_KEYS is None:
        _READ_KEYS = _read_keys()
    return _READ_KEYS


def vocabulaire_vivant() -> frozenset:
    """Toute clé qu'un lecteur consulte : le validateur OU le front. **La** référence.

    ⚠️ Il y avait DEUX inventaires, et ils se trompaient sur des ensembles disjoints —
    dans la même réponse. Le dérivé ci-dessus ne voit que le validateur : il dénonçait
    `label`, `help`, `hint`, `placeholder`, `description`, cinq attributs vivants que
    seul le front lit, donc presque tous les tableaux existants. La déclaration écrite
    à la main (`schema_keys`) ne voyait pas ce que le validateur applique : elle
    dénonçait `options`, `required` et `max_items`. Chacun était aveugle exactement là
    où l'autre voyait, et un agent qui posait un schéma recevait deux verdicts
    contradictoires sur le sien.

    Un faux positif dans un signal de qualité est pire que pas de signal : on apprend à
    l'ignorer, et il ne sert plus le jour où il a raison. Deux signaux qui se
    contredisent apprennent la même chose deux fois plus vite.

    La moitié `front` ne peut pas être dérivée d'ici — elle est lue dans un autre
    dépôt. Elle reste donc déclarée, et c'est une dette assumée que `schema_keys`
    documente. La moitié `validateur`, elle, est dérivée ET confrontée dans les DEUX
    sens par `tests/test_schema_keys_oto56.py`."""
    return interpreted_keys() | schema_keys.LUES_PAR_LE_FRONT


# Fautes de frappe qui MÉRITENT d'être nommées : une clé inconnue proche d'une clé
# lue n'est presque jamais une déclaration tierce délibérée. Dérivé lui aussi — les
# variantes pointent vers la clé réelle, qui doit exister dans le vocabulaire lu.
_NEAR_MISS = {
    "enum": "options", "enums": "options", "option": "options",
    "choices": "options", "choix": "options", "values": "options",
    "valeurs": "options", "allowed": "options",
    "maxlength": "max_length", "max_len": "max_length", "maxLength": "max_length",
    "requiredWhen": "required_when", "required_if": "required_when",
    "mandatory": "required", "obligatoire": "required",
    "champs": "fields", "columns": "fields",
    "cle": "key", "name": "key", "nom": "key",
    "read_only": "readonly", "readOnly": "readonly", "writable_by": "readonly",
    "origin": "origine",
}


def unknown_declaration_keys(schema: Optional[dict]) -> list[dict]:
    """Par champ, les clés de déclaration qu'oto n'interprète pas.

    Rend `[{field, keys: [...], near_miss: {clé: clé_réelle}}]` — vide quand tout est
    lu. Le near-miss est ce qui rend l'avertissement ACTIONNABLE : « `enum` n'est pas
    lue par oto ; si tu voulais contraindre les valeurs, la clé est `options` » vaut
    infiniment mieux que « clé inconnue ».
    """
    if not isinstance(schema, dict):
        return []
    lues = vocabulaire_vivant()
    if not interpreted_keys():         # dérivation indisponible : ne rien affirmer
        return []
    out: list[dict] = []

    def _visiter(fields: list, prefixe: str = "") -> None:
        for f in fields:
            if not isinstance(f, dict):
                continue
            nom = f"{prefixe}{f.get('key') or '?'}"
            inconnues = sorted(k for k in f if k not in lues)
            if inconnues:
                near = {k: _NEAR_MISS[k] for k in inconnues
                        if k in _NEAR_MISS and _NEAR_MISS[k] in lues}
                out.append({"field": nom, "keys": inconnues, "near_miss": near})
            if isinstance(f.get("fields"), list):
                _visiter(f["fields"], f"{nom}.")
            of = f.get("of")
            if isinstance(of, dict) and isinstance(of.get("fields"), list):
                _visiter(of["fields"], f"{nom}[].")

    _visiter(_fields(schema))
    return out


def unknown_keys_warning(inconnues: list[dict]) -> str:
    """Le message rendu à l'appelant — une phrase, pas un dump.

    Il dit la CONSÉQUENCE (« stockée et rendue, mais jamais lue ») avant la
    correction : sans elle, un lecteur pressé prend l'avertissement pour un détail de
    style, alors qu'il signale une contrainte qui n'existe pas."""
    if not inconnues:
        return ""
    corrections = [f"{k} → {v}" for e in inconnues
                   for k, v in (e.get("near_miss") or {}).items()]
    champs = ", ".join(f"{e['field']} ({', '.join(e['keys'])})" for e in inconnues[:5])
    msg = (f"Clés non interprétées par oto : {champs}"
           + (" …" if len(inconnues) > 5 else "")
           + ". Elles sont stockées et rendues telles quelles, mais AUCUNE ne "
             "contraint quoi que ce soit ici. ⚠️ « Non interprétée par oto » ne veut "
             "pas dire « inutile » : un consommateur peut la lire en aval, et la "
             "plateforme ne sait pas qui lit quoi.")
    if corrections:
        msg += " Vouliez-vous écrire : " + ", ".join(sorted(set(corrections))) + " ?"
    return msg


def unknown_keys_read_warning(inconnues: list[dict]) -> str:
    """Le MÊME relevé, dit au LECTEUR d'un schéma plutôt qu'à son auteur (#416).

    ⚠️ Ce n'est pas une variante de style : l'avertissement de pose demande « vouliez-
    vous écrire `options` ? », question qui n'a aucun sens pour qui lit le schéma d'un
    tableau qu'il n'a pas déclaré — il n'a rien voulu écrire, il cherche à savoir à
    quoi s'en tenir. Ce qu'il lui faut, c'est **laquelle des deux clés fait foi**.

    Le défaut mesuré : un champ portant à la fois `enum` (jamais lue) et `options`
    (qui contraint) donne deux réponses contradictoires à « quelles valeurs sont
    admises ». Un agent se fie au plus court, `enum` — qui a l'air le plus officiel —
    et se restreint à tort, ou attend un rejet qui n'arrivera jamais.

    La liste vient de `unknown_declaration_keys`, comme à la pose : une seule
    dérivation, deux formulations. Le jour où une clé entre dans le vocabulaire lu,
    les deux messages s'éteignent ensemble."""
    if not inconnues:
        return ""
    champs = ", ".join(f"{e['field']} ({', '.join(e['keys'])})" for e in inconnues[:5])
    # Ce qui FAIT FOI, quand la clé morte a une cousine vivante : c'est la seule
    # information qui permette d'écrire juste sans reposer le schéma.
    autorite = sorted({v for e in inconnues for v in (e.get("near_miss") or {}).values()})
    msg = (f"Ce schéma porte des clés qu'oto n'INTERPRÈTE PAS : {champs}"
           + (" …" if len(inconnues) > 5 else "")
           + ". Elles sont stockées et rendues fidèlement, mais aucun contrôle de la "
             "plateforme ne s'appuie dessus — ne t'y fie pas pour savoir ce qui est "
             "admis ICI. ⚠️ Cela ne dit PAS qu'elles sont inutiles : un consommateur "
             "peut parfaitement les lire (un front les affiche), et oto ne sait pas "
             "qui lit quoi en aval. Ne retire rien sur la seule foi de ce message.")
    if autorite:
        msg += (" Ce qui fait foi : " + ", ".join(f"`{k}`" for k in autorite)
                + ". En cas de contradiction entre les deux, c'est cette clé-là qui "
                  "décide, et l'autre est un résidu.")
    return msg
