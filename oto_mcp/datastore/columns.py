"""La COLONNE côté Python : ce qu'une écriture touche, et sous quel nom on la désigne.

Extrait du store (#325), déplacement pur. Le pendant Python de `db/paths` : là-bas on
traduit un nom en SQL, ici on décide ce qu'une écriture modifie et ce qu'un ancien nom
désigne encore.

La règle que ce module porte tient en une phrase — **une écriture ne touche QUE ce
qu'elle nomme** — et elle a coûté deux défauts symétriques, l'un après l'autre :

- écrire une VALEUR effaçait l'origine : le patch par identifiant, le geste le plus
  courant d'un agent ;
- écrire une ORIGINE seule effaçait la valeur : le geste nominal du rattrapage de
  socle, quand un tableau adopte les couches après coup.

Deux correctifs symétriques auraient laissé passer le troisième. Une règle unique dont
les deux découlent, non.

S'y ajoute la traduction des anciens noms plats pendant une migration : elle vit ici
parce qu'elle répond à la même question — de quelle colonne parle-t-on ?
"""
from __future__ import annotations

from typing import Any, Optional

from . import schema as dsv2
from .errors import RowValidationError

# Les colonnes de la PLATEFORME : elles vivent dans la ligne sans être des
# données de l'utilisateur — ni purgeables, ni écrasables par une écriture.
_META_COLS = ("_id", "_created_at", "_updated_at", "_claimed_by", "_claimed_until",
              "_claimed_run", "_claims", "_abandon")


def _writes_layers(new: Any) -> bool:
    """L'écriture NOMME-t-elle des couches ? (`{"origine": …}`, `{"valeur": …}`…)

    Strict, comme tout écrivain : un dict fait UNIQUEMENT de couches connues. Un
    `{"a": 1, "origine": "x"}` reste une donnée `json` métier qui se trouve avoir un
    champ nommé « origine » — on ne le réinterprète pas."""
    return dsv2.names_layers(new)


def _existing_layers(existing: Any) -> dict:
    """Le contenu ACTUEL d'une colonne, vu comme ses couches.

    Tolérant, comme tout lecteur : un dict qui porte `valeur` est une colonne à
    couches même s'il en porte une qu'on ne connaît pas — écrite par une version plus
    récente, elle traverse intacte au lieu d'être perdue à la première réécriture par
    un nœud plus ancien. Un scalaire est une valeur sans couches ; `None` est le vide."""
    if isinstance(existing, dict) and existing and (
            dsv2.VALUE_LAYER in existing
            or all(k in dsv2.ALL_LAYER_KEYS for k in existing)):
        return dict(existing)
    return {} if existing is None else {dsv2.VALUE_LAYER: existing}


def _to_path(schema: Optional[dict], nom):
    """Un ancien nom plat → son chemin réel ; tout le reste, inchangé."""
    if not isinstance(nom, str):
        return nom
    cible = dsv2.resolve_flat_name(schema, nom)
    if cible is None:
        return nom
    colonne, rang, attr = cible
    return f"{colonne}[{rang}].{attr}"


def _resolve_filters(schema: Optional[dict], filters):
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            out.append(f)
            continue
        g = dict(f)
        if g.get("field"):
            g["field"] = _to_path(schema, g["field"])
        if isinstance(g.get("fields"), list):
            g["fields"] = [_to_path(schema, k) for k in g["fields"]]
        if isinstance(g.get("where"), list):
            g["where"] = _resolve_filters(schema, g["where"])
        out.append(g)
    return out


def _resolve_metrics(schema: Optional[dict], metrics):
    out = []
    for m in metrics or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        g = dict(m)
        if g.get("field"):
            g["field"] = _to_path(schema, g["field"])
        if isinstance(g.get("where"), list):
            g["where"] = _resolve_filters(schema, g["where"])
        out.append(g)
    return out


def _refuse_group_by_compose(group_by) -> None:
    """`group_by="a,b"` est REFUSÉ, nommé (oto#50).

    Le fait mesuré : une mission cliente appelle `data_aggregate` avec
    `group_by: "lot_test,statut"` et reçoit **200, un unique groupe de clé `null`
    contenant toutes les lignes**. La chaîne part telle quelle jusqu'au SQL, où
    `data->>'lot_test,statut'` vaut NULL sur chaque ligne — donc un seul groupe, et une
    réponse fausse rendue comme un résultat.

    ⚠️ C'est la forme la plus coûteuse de cette classe : l'agent lit « un groupe, clé
    nulle » et conclut que la donnée est vide ou mal remplie. Il part corriger un
    tableau qui n'a rien. Rien dans la réponse ne peut le détromper — un refus, si.

    **Pourquoi refuser plutôt que supporter.** Un vrai groupement à deux dimensions
    demande de trancher ce qu'est un groupe COMPOSITE dans la réponse servie : une clé
    jointe (`"a|b"`), un tuple, un objet imbriqué ? Chacune fige une forme qu'aucun
    consommateur ne pourra plus changer, et personne ne l'a demandée — c'est un lot de
    conception, pas un correctif. Le refus dit donc aussi ce qu'il faudra trancher le
    jour où quelqu'un le demandera vraiment : il ouvre la porte au lieu de la murer.

    ⚠️ Ne pas confondre avec la forme LISTE, qui existe et fait autre chose : elle met
    en commun les valeurs de plusieurs champs sous une même clé, ce n'est pas un
    groupement à deux dimensions. Le message le dit, parce que c'est l'erreur naturelle
    de qui vient d'écrire une virgule."""
    if not isinstance(group_by, str) or "," not in group_by:
        return
    champs = [c.strip() for c in group_by.split(",") if c.strip()]
    cite = ", ".join(f"`{c}`" for c in champs)
    raise ValueError(
        f"`group_by=\"{group_by}\"` n'est pas un groupement à deux dimensions — cette "
        "forme n'existe pas, et sans ce refus elle serait lue comme UN nom de champ "
        f"contenant une virgule : aucune ligne ne le porte, tu recevrais un unique "
        "groupe de clé `null` avec toutes tes lignes dedans, en 200. "
        f"Groupe sur UN champ à la fois ({cite} — un appel chacun), ou passe la LISTE "
        f"`{champs}` si tu voulais mettre leurs valeurs EN COMMUN sous une même clé "
        "(ce n'est pas la même chose : la liste fusionne, elle ne croise pas). "
        "Le groupement croisé n'est pas encore servi : le jour où il le sera, il "
        "faudra d'abord décider sous quelle forme un groupe composite est rendu — clé "
        "jointe, tuple, ou objet — parce que ce choix-là ne se défait plus.")


def _resolve_group_by(schema: Optional[dict], group_by):
    _refuse_group_by_compose(group_by)
    if isinstance(group_by, (list, tuple)):
        return [_to_path(schema, k) for k in group_by]
    return _to_path(schema, group_by)


def _refuse_flat_writes(schema: Optional[dict], user_data: dict) -> None:
    """Écrire sur un nom PROJETÉ est refusé, en nommant la cible neuve (oto#22 §6).

    Pendant la migration, `contact1_nom` est servi en LECTURE — calculé depuis la
    colonne-tableau, jamais stocké. L'accepter en écriture créerait une colonne libre
    du même nom : la lecture continuerait de rendre la valeur PROJETÉE, et ce qui vient
    d'être écrit serait invisible tout en ayant été accepté. C'est la forme exacte du
    défaut qu'on passe la journée à fermer — un accusé de réception pour un travail qui
    n'atteint rien.

    Le refus dit où écrire : un message qui dit seulement « non » fait deviner."""
    if not user_data:
        return
    for cle in user_data:
        cible = dsv2.resolve_flat_name(schema, cle)
        if cible is None:
            continue
        colonne, rang, attr = cible
        raise RowValidationError([
            f"{cle}: nom servi en lecture pendant la migration, il ne s'écrit pas "
            f"(il est CALCULÉ depuis `{colonne}`, jamais stocké) — écrire "
            f"`{colonne}[{rang}].{attr}`"])


def _scan_mixed(value: Any, path: str, errors: list) -> None:
    """Le balayage RÉCURSIF de la garde #329 — au grain feuille, parce que c'est
    à l'intérieur des items de colonne-liste (les attributs contacts) que
    passent les écritures réelles. Trois natures de dict, trois traitements :
    mixte (≥1 couche connue + ≥1 inconnue) → refus nommé ; pur-couches → on ne
    descend PAS dedans (la valeur d'une couche est opaque, contrat du lecteur
    tolérant) ; sans aucune couche → donnée libre, on descend (ses feuilles
    peuvent porter des couches)."""
    if isinstance(value, dict):
        cles = set(value)
        couches = cles & set(dsv2.ALL_LAYER_KEYS)
        if couches:
            inconnues = sorted(cles - set(dsv2.ALL_LAYER_KEYS))
            if inconnues:
                errors.append(
                    f"{path}: {', '.join(repr(k) for k in inconnues)} n'est pas une "
                    f"couche — les couches sont {', '.join(dsv2.ALL_LAYER_KEYS)}. "
                    "Rien n'a été écrit. Corrige la clé ; si c'est un objet métier "
                    "qui porte ce nom par coïncidence, déclare la colonne en type "
                    "`json` (data_set_schema) — elle devient exempte de cette garde.")
            return
        for k, v in value.items():
            _scan_mixed(v, f"{path}.{k}", errors)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _scan_mixed(item, f"{path}[{i}]", errors)


def _refuse_mixed_layers(schema: Optional[dict], user_data: Optional[dict]) -> None:
    """#329 : une couche mal orthographiée se REFUSE, elle n'écrase jamais.

    Un dict qui mêle une clé de couche connue et une inconnue était traité en
    donnée json ordinaire (`_writes_layers` strict + `unknown_layers`
    court-circuité sans `valeur`) : il ÉCRASAIT la valeur existante sans une
    erreur — une faute de frappe systématique dans une procédure effacerait un
    champ sur ~9 000 lignes. La garde vit ICI, à la validation d'entrée, sur le
    payload ÉCRIT : dans le merge elle raterait les items (grain colonne), sur
    le résultat mergé elle bloquerait rétroactivement les lignes porteuses d'un
    dict mixte historique.

    Exemption au grain où un type EST déclaré : une colonne de premier niveau
    déclarée `json` est un objet métier assumé — un contenu y porte `origine`
    sans être des couches."""
    if not user_data:
        return
    exemptes = {f.get("key") for f in dsv2._fields(schema)
                if f.get("type") == "json" and f.get("key")}
    errors: list = []
    for col, val in user_data.items():
        if col in exemptes or col in _META_COLS:
            continue
        _scan_mixed(val, col, errors)
    if errors:
        raise RowValidationError(errors)


# ── ce qu'une écriture VIDE (#407/#408/#409), et ce qui n'est PAS un vide (#608) ─
#
# Le pendant de la règle du merge. « Une écriture ne touche que ce qu'elle nomme »
# dit ce qui SURVIT ; il restait à dire ce qui TOMBE. Nommer un champ avec `null`
# l'efface — c'est le seul geste qui vide une valeur fausse, donc il reste permis —
# mais il est indiscernable, dans un payload, d'un `None` de sérialisation : une
# variable non peuplée, un gabarit à demi rempli, un aller-retour de lecture.
#
# Vécu le 13/08/2026 (org 226, tableau `edition-essais`) : une session a écrit
# `row={'moteur': None, 'siren': …}` ligne par ligne, a reçu des succès ordinaires,
# et a découvert le champ vidé huit minutes plus tard — en l'imputant à l'écriture
# d'enrichissement suivante, qui ne nommait pas `moteur` et ne l'avait pas touché
# (trois signaux, #407/#408/#409, sur une cause qui n'était pas la leur). L'écriture
# a fait ce qu'on lui demandait ; c'est ce qu'elle en a DIT qui manquait.
#
# Même patron que `hors_schema` (#294) et `hors_options` (#319) : on n'empêche rien,
# on nomme. Et on nomme la VALEUR PERDUE — sans elle il n'y a rien à rétablir.
#
# ══ #608 (28/08/2026) : LA CHAÎNE VIDE N'EST PAS UNE VALEUR ═══════════════════
#
# Annoncer une perte n'est pas l'éviter. Un client (org 270, `koncile-accounts`) a
# perdu un signal de recrutement daté parce que son lot de sourcing portait
# `best_signal: ""` dans son GABARIT de ligne — la forme NORMALE d'un lot : un
# gabarit écrit une fois, réutilisé sur toutes les lignes. Il a été rétabli grâce à
# `valeurs_effacees` ci-dessus ; c'est le seul hasard qui a sauvé la donnée.
#
# **Une chaîne vide est-elle une valeur, ou une absence de valeur ?** Le serveur
# répondait les DEUX, dans le même appel :
#
#   - `_is_empty` (le validateur) répond ABSENCE : une chaîne vide ne subit aucun
#     contrôle de type, et sur un champ requis elle produit « champ requis
#     manquant » — c'est-à-dire, mot pour mot, « tu n'as rien fourni » ;
#   - `_merge_column` répondait VALEUR : elle écrasait ce qui était en place.
#
# Deux réponses contradictoires sur la même donnée. On tranche pour l'ABSENCE, et
# ce n'est pas un arbitrage de goût : une chaîne vide (comme une liste ou un objet
# vides) est ce que produit une SOURCE MUETTE — un enrichissement qui n'a rien
# trouvé, un gabarit à demi peuplé, un champ de formulaire jamais rempli. Aucun de
# ces gestes ne veut dire « oublie ce que tu savais ». `null`, lui, ne se fabrique
# pas tout seul dans un gabarit de lot : il reste LE geste qui vide.
#
# D'où la règle, qui tient en une phrase :
#
#   **un vide non-`null` ne DÉPLACE jamais une valeur — il ne peut s'écrire que là
#   où il n'y a rien.**
#
# Elle est volontairement plus étroite que « la chaîne vide est ignorée » : là où la
# colonne était déjà vide (ou absente), le geste passe tel quel, donc CRÉER une
# ligne à partir d'un gabarit ne change pas de comportement. On ne se protège que
# de la DESTRUCTION, qui est le seul dégât irréversible.
#
# ⚠️ Et on le DIT (`valeurs_ignorees`) : ignorer en silence serait le défaut de #608
# retourné — un appelant qui voulait vraiment vider croirait avoir vidé. Le relevé
# nomme le champ, la valeur qui a SURVÉCU, et le geste à employer pour vider pour de
# bon. Un refus dur a été écarté : un lot de 500 lignes qui casse sur un gabarit
# imparfait coûte plus cher que la valeur qu'on préserve, et le tableau qui écrit
# `""` depuis des mois n'a rien demandé (8 897 cellules vides mesurées en production
# le 28/08, sur 59 tableaux — les refuser rétroactivement casserait 59 clients).

# Deux bornes, pour qu'un relevé reste lisible par un agent : le nombre
# d'effacements nommés, et la taille d'une valeur rendue. Au-delà, on dit la TAILLE
# plutôt qu'un extrait — un extrait ferait croire qu'on tient la valeur.
_EFFACEMENTS_NOMMES = 20
_VALEUR_RENDUE_MAX = 300


def _valeur_posee(new: Any) -> tuple:
    """`(l'écriture touche-t-elle la VALEUR ?, la valeur qu'elle pose)`.

    Écrire `{"origine": …}` seul ne touche pas la valeur (c'est toute la règle de
    `_merge_column`) : ce n'est donc jamais un effacement, même si la colonne
    finissait vide pour une autre raison."""
    if not _writes_layers(new):
        return True, new
    if dsv2.VALUE_LAYER in new:
        return True, new[dsv2.VALUE_LAYER]
    return False, None


def _sans_la_valeur(neuf: Any) -> Any:
    """Ce qui reste de l'écriture d'une colonne quand on lui RETIRE sa valeur vide.

    `None` = plus rien à écrire, la colonne n'est pas touchée du tout. Une écriture
    en couches qui pose AUSSI une origine (`{"valeur": "", "origine": "apollo"}`)
    garde son origine : « une écriture ne touche que ce qu'elle nomme » vaut dans ce
    sens-là aussi — écarter la valeur vide ne doit pas emporter ce qui l'accompagne."""
    if not _writes_layers(neuf):
        return None
    reste = {k: v for k, v in neuf.items() if k != dsv2.VALUE_LAYER}
    return reste or None


def arbitrer_les_vides(existing: Optional[dict], user_data: Optional[dict],
                       row_id: Optional[str] = None) -> tuple:
    """`(ce que l'écriture pose VRAIMENT, ce qu'elle efface, ce qu'on a écarté)`.

    UN seul parcours pour les deux relevés et pour la correction du payload : les
    trois répondent à la même question — « ce geste fait-il tomber une valeur en
    place ? » — et les faire diverger, c'est exactement le défaut de #608 (le
    validateur et la fusion ne s'accordaient pas sur ce qu'est un vide).

    Le vide se juge DÉBALLÉ (`unwrap`) des deux côtés, comme tout ce qui juge une
    valeur : une colonne à couches dont la `valeur` tombe est vidée au même titre
    qu'un scalaire, et une colonne qui ne portait que son `origine` n'avait déjà
    pas de valeur à perdre.

    ⚠️ Ce parcours ne décide QUE de la valeur : le sort du GESTE — quand il n'a plus
    rien à poser — se juge après, sur ses trois sorties (`refuser_geste_sans_effet`)."""
    pose: dict = {}
    effaces: list[dict] = []
    ignores: list[dict] = []
    for cle, neuf in (user_data or {}).items():
        touche, posee = _valeur_posee(neuf)
        if (cle in _META_COLS or not touche or not dsv2._is_empty(posee)):
            pose[cle] = neuf
            continue
        ancienne = dsv2.unwrap((existing or {}).get(cle))
        if dsv2._is_empty(ancienne):
            pose[cle] = neuf              # rien à perdre : on ne fait pas de bruit
            continue
        if posee is None:
            # `null` NOMMÉ : le geste explicite d'effacement. Il s'exécute — vider
            # une valeur fausse n'a pas d'autre porte — et il se dit.
            effaces.append({"ligne": row_id, "champ": cle, "valeur": ancienne})
            pose[cle] = neuf
            continue
        # Vide non-`null` sur une valeur en place : la valeur survit (#608).
        ignores.append({"ligne": row_id, "champ": cle, "valeur": ancienne})
        reste = _sans_la_valeur(neuf)
        if reste is not None:
            pose[cle] = reste
    return pose, effaces, ignores


def refuser_geste_sans_effet(pose: Optional[dict], ecartes: list) -> None:
    """REFUSE une écriture qui, après arbitrage, ne pose plus RIEN (#724).

    #608 préserve une valeur en place contre un vide non-`null` et le DIT
    (`valeurs_ignorees`). Il reste un cas où le dire ne suffit pas : quand l'écarté
    était **tout** ce que l'écriture portait. L'appel n'a alors aucun effet et répond
    `200` — un succès qui n'a rien fait, dont le seul témoin est une clé de la réponse.

    **Ce n'est pas une conjecture, c'est ce qui s'est passé** (2026-09-01, 04:16-04:20) :
    dix `row={'contacts': []}` sur des fiches clientes, dix `200`, zéro retrait. La
    porte `null` existait, et le relevé la nommait déjà mot pour mot — elle n'a pas été
    empruntée : une seule écriture `null` ce jour-là, sur une table d'ESSAI, jamais sur
    les fiches ratées, dont l'une porte encore le contact qu'on voulait retirer.

    **Pourquoi refuser plutôt que faire effacer.** Faire effacer le vide SEUL ferait
    dépendre un geste DESTRUCTEUR de ses voisines : « selon le contexte ta donnée
    disparaît » est une perte silencieuse, quand « selon le contexte ton appel échoue »
    est un désagrément qui enseigne. Le refus arrive au moment où l'appelant peut
    encore corriger, et il **nomme exactement quoi écrire** — c'est ce qui le distingue
    d'un relevé qu'on peut ne pas lire.

    La ligne de partage est l'EFFET du geste, pas le type de la valeur : une écriture
    qui pose autre chose (la fiche entière réémise, 98 % de la population mesurée) est
    inchangée. Conséquence structurelle : une row de LOT porte toujours sa clé métier,
    donc elle pose — un import de 500 lignes ne peut pas casser ici. Chiffres, fenêtre
    et réserves : `docs/datastore.md`.
    """
    if not ecartes:
        return                       # rien n'a été écarté : rien à refuser
    if any(cle not in _META_COLS for cle in (pose or {})):
        return                       # le geste pose autre chose : il agit
    champs = sorted({str(r.get("champ")) for r in ecartes})
    cite = ", ".join(f"`{c}`" for c in champs)
    porte = ", ".join(f'"{c}": null' for c in champs)
    raise ValueError(
        f"écriture sans effet : {cite} porte une valeur VIDE non-`null` (liste vide, "
        "chaîne vide, objet vide) sur une valeur déjà en place, et ton écriture ne "
        "pose rien d'autre — elle ne changerait donc RIEN, et te répondrait comme un "
        "succès. Un vide non-`null` ne déplace jamais une valeur : c'est ce que rend "
        "une source muette ou un gabarit à demi peuplé, pas une demande d'effacement. "
        f"POUR VIDER POUR DE BON, écris exactement : {{{porte}}}. Pour laisser la "
        "valeur intacte, retire ce champ de ton corps.")


def _valeur_rendue(valeur: Any) -> Any:
    """La valeur perdue, ou sa TAILLE quand la rendre coûterait la réponse."""
    n = len(valeur) if isinstance(valeur, str) else len(str(valeur))
    if n <= _VALEUR_RENDUE_MAX:
        return valeur
    return (f"<{n} caractères — la valeur complète n'est plus lisible ici, "
            "elle n'est plus en base non plus>")


def _nommes(records: list) -> tuple:
    """Les entrées rendues (bornées, valeurs raccourcies) et le reste non nommé."""
    nommes = [{**r, "valeur": _valeur_rendue(r.get("valeur"))}
              for r in records[:_EFFACEMENTS_NOMMES]]
    return nommes, len(records) - len(nommes)


def effacements_report(records: list) -> dict:
    """Le relevé des effacements, prêt à fusionner dans une réponse d'écriture.

    `{}` quand rien n'a été vidé — le cas normal ne porte pas de clé parasite.

    ⚠️ Depuis #608, `null` est le SEUL vide qui arrive ici : la phrase ne cite donc
    pas les autres vides parmi les valeurs qui effacent, sous peine de prescrire un
    geste qui, lui, est REFUSÉ quand il est seul et ignoré quand il accompagne (#724)."""
    if not records:
        return {}
    nommes, reste = _nommes(records)
    hint = ("un `null` NOMMÉ dans le payload EFFACE la valeur en place — ce n'est "
            "PAS la même chose que ne pas nommer le champ, qui le laisse intact. Si "
            "l'effacement n'était pas voulu (variable non peuplée, gabarit à demi "
            "rempli), réécris les valeurs ci-dessus : elles ne sont plus en base.")
    if reste:
        hint += f" {len(records)} effacements au total, {len(nommes)} nommés ici."
    return {"valeurs_effacees": nommes, "valeurs_effacees_hint": hint}


def ignores_report(records: list) -> dict:
    """Le relevé des vides ÉCARTÉS (#608) : ce que le geste aurait détruit.

    Clé DISTINCTE de `valeurs_effacees`, et c'est le point : celle-là nomme une
    valeur qui N'EST PLUS, celle-ci nomme une valeur qui EST ENCORE LÀ. Les
    confondre ferait réécrire des valeurs déjà en place — ou pire, ferait croire à
    une perte."""
    if not records:
        return {}
    nommes, reste = _nommes(records)
    hint = ("une valeur VIDE non-`null` (chaîne vide, liste vide, objet vide) ne "
            "remplace pas une valeur déjà en place : c'est ce que rend une source "
            "muette ou un gabarit à demi peuplé, pas une demande d'effacement. Les "
            "valeurs ci-dessus sont INTACTES en base — il n'y a rien à rétablir. "
            "Pour vider un champ pour de bon, nomme-le avec `null`.")
    if reste:
        hint += f" {len(records)} champs préservés au total, {len(nommes)} nommés ici."
    return {"valeurs_ignorees": nommes, "valeurs_ignorees_hint": hint}


def _merge_column(existing: Any, new: Any) -> Any:
    """Fusion d'UNE colonne. **Aucune couche ne s'écrit implicitement, dans aucun sens.**

    Une écriture ne touche QUE ce qu'elle nomme. C'est la protection contre
    l'ACCIDENT, pas contre l'intention — et surtout, c'est ce qui dispense l'agent d'y
    penser : il écrit ce qu'il veut poser, le reste demeure. Un geste explicite
    remplace ce qu'il vise ; il n'y a pas de verrou, donc rien à contourner.

    Les deux directions ont coûté un défaut chacune, et la seconde a failli coûter
    8 910 lignes :

      - écrire une VALEUR effaçait l'origine (#322) — le patch par `id`, le geste le
        plus courant d'un agent ;
      - écrire une ORIGINE seule effaçait la valeur (#326) — le geste nominal du
        RATTRAPAGE de socle, quand un tableau adopte les couches après coup. Aucune
        erreur, la valeur simplement disparue.

    D'où la règle unique dont les deux découlent, plutôt que deux correctifs
    symétriques : on part de l'existant, l'écriture y dépose ce qu'elle nomme.

    ⚠️ Deux conséquences qui ne se devinent pas :

    `comment` et `link` décrivent LA VALEUR : quand elle change sans qu'ils soient
    renommés, ils tombent avec elle — les garder ferait affirmer une provenance
    fausse, précisément le défaut qu'on élimine une couche plus haut. `origine`, elle,
    décrit le point de départ : elle survit.

    Une écriture ORDINAIRE (scalaire, `null`, ou donnée `json`) est une écriture de
    la valeur : elle laisse l'origine intacte. Effacer l'origine se demande —
    `{"origine": null}`. Et une colonne dont il ne reste que la valeur redevient un
    scalaire nu : les lignes sans couches ne doivent pas se mettre à porter une
    enveloppe.

    ⚠️ **Une valeur nue IDENTIQUE à celle en place est un NO-OP : toutes les couches
    restent** (29/08/2026, trou éprouvé en v1.165.0 sur une colonne `readonly`). La
    lecture sert la valeur nue et met les couches à côté (`flat_layers`), donc le
    round-trip relire → repousser (#390) repousse forcément la valeur nue — et
    « réécrire la valeur emporte `comment`/`link` » détruisait au passage la
    divergence qu'un agent venait d'écrire dans `adresse.comment`. Une valeur
    identique n'est pas une réécriture ; le jugement est au TYPE près (`0` n'est pas
    `False`). Vaut aussi en couches : `{"valeur": <identique>, "comment": …}` écrit le
    comment sans faire tomber le link — la valeur n'a pas changé, rien ne tombe."""
    if not _writes_layers(new):
        if dsv2.same_value(_existing_layers(existing).get(dsv2.VALUE_LAYER), new):
            return existing
        # Toute colonne A une origine ; quand elle est VIDE il n'y a rien à préserver,
        # et la colonne reste plate — le plat est un état, pas une nature.
        origine = _existing_layers(existing).get(dsv2.ORIGIN_LAYER)
        if origine is None:
            return new
        if new is None:
            # EFFACEMENT (signal #695). Une origine `""` est le marqueur « rien
            # n'avait été remis » (cf. `reserves.py`, qui la pose ainsi quand il n'y
            # avait pas de valeur d'avant) : elle QUALIFIE une valeur. Quand la valeur
            # s'en va, il ne reste qu'une enveloppe sans rien à qualifier —
            # `{"origine": ""}` — qui n'est plus une valeur d'énumération valide et
            # rend la ligne INVISIBLE au filtrage et aux facettes. Mesuré sur trois
            # lignes remises à zéro : quatre champs sur quatre, exactement ceux qui
            # portaient une couche `origine` ; les champs texte nullés au même appel
            # n'avaient pas ce résidu.
            # ⚠️ Une origine PLEINE, elle, survit : c'est le point de départ, parfois
            # l'unique copie de la valeur remise, et l'effacer serait une perte que
            # personne ne peut reconstituer.
            # ⚠️ Et le vide ne se lit ICI que — pas au cas général : à la RÉÉCRITURE
            # le marqueur `""` doit survivre, sinon la deuxième écriture capturerait
            # la première valeur de l'agent comme si elle venait du client
            # (`test_vide_a_l_origine_le_marqueur_tient_le_une_seule_fois`, qui a
            # attrapé une première correction trop large).
            return None if dsv2.est_vide(origine) else {dsv2.ORIGIN_LAYER: origine}
        return {dsv2.VALUE_LAYER: new, dsv2.ORIGIN_LAYER: origine}
    out = _existing_layers(existing)
    if dsv2.VALUE_LAYER in new and not dsv2.same_value(out.get(dsv2.VALUE_LAYER),
                                                       new[dsv2.VALUE_LAYER]):
        for couche in dsv2.VALUE_BOUND_LAYERS:
            out.pop(couche, None)
    out.update(new)
    out = {k: v for k, v in out.items() if v is not None}
    if not out:
        return None
    if set(out) == {dsv2.VALUE_LAYER}:
        return out[dsv2.VALUE_LAYER]
    return out
