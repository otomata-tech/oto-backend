"""La COLONNE côté Python : ce qu'une écriture touche, et sous quel nom on la désigne.

Extrait du store (#325), déplacement pur. Le pendant Python de `db/paths` : là-bas on
traduit un nom en SQL, ici on décide ce qu'une écriture modifie.

La règle que ce module porte tient en une phrase — **une écriture ne touche QUE ce
qu'elle nomme** — et elle a coûté deux défauts symétriques, l'un après l'autre :

- écrire une VALEUR effaçait l'origine : le patch par identifiant, le geste le plus
  courant d'un agent ;
- écrire une ORIGINE seule effaçait la valeur : le geste nominal du rattrapage de
  socle, quand un tableau adopte les couches après coup.

Deux correctifs symétriques auraient laissé passer le troisième. Une règle unique dont
les deux découlent, non.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import couches as dsl
from . import schema as dsv2
from .errors import RowValidationError

# Les colonnes de la PLATEFORME : elles vivent dans la ligne sans être des
# données de l'utilisateur — ni purgeables, ni écrasables par une écriture.
# `_revision` (12/09/2026) : la révision servie de la ligne. Réservée comme les
# autres — mesuré avant de la poser, aucune ligne en base ne porte de clé `_revision`.
_META_COLS = ("_id", "_created_at", "_updated_at", "_claimed_by", "_claimed_until",
              "_claimed_run", "_claims", "_abandon", "_revision")


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


def _porte_un_null(neuf: Any) -> bool:
    """L'écriture de CETTE colonne pose-t-elle `null` ? Même lecture que
    `fin_du_null.nulls_nommes` : un scalaire `null`, ou une `valeur` nulle en couches."""
    return neuf is None or (isinstance(neuf, dict) and dsv2.VALUE_LAYER in neuf
                            and neuf[dsv2.VALUE_LAYER] is None)


def sans_les_nulls_sans_effet(user_data: Optional[dict],
                              en_place: Callable[[], Optional[dict]],
                              schema: Optional[dict]) -> Optional[dict]:
    """Retire d'une écriture les `null` qui n'effacent RIEN (oto#182).

    La lecture sert à `null` toute colonne déclarée sans valeur en place. Un agent qui
    relit une ligne puis la réémet renvoie donc ces `null` : écrits, ils ajoutaient une
    clé, faisaient tourner la révision et déclenchaient le préavis de `null` (le refus à
    partir du 01/12) — sur un geste qui ne change rien. Mesuré par la suite complète :
    cinq bancs d'aller-retour rougissaient.

    Un `null` sur une colonne SANS valeur en place (absente, ou déjà vide) n'est donc pas
    écrit. Sur une valeur EN PLACE, il reste l'effacement nommé — préavis, puis refus —,
    inchangé : ce filtre ne décide rien de ce qu'efface un `null`, il retire seulement
    ceux qui n'ont rien à effacer.

    ⚠️ **Colonnes DÉCLARÉES seulement.** La lecture ne sert à `null` que le déclaré : un
    écho ne peut viser que lui. Une colonne HORS schéma écrite à `null` garde son
    comportement — elle naît en base et le relevé la nomme, ou le tableau la refuse —,
    sinon une faute de frappe écrite à `null` disparaîtrait sans refus ni relevé
    (`test_hors_schema_tous_chemins.py`, rouge sur la première version de ce filtre).

    `en_place` rend les données de la ligne visée (`{}` pour une création) et n'est
    appelé QUE si l'écriture porte un `null` : le chemin nominal ne paie aucune lecture.
    Une écriture en couches garde ce qui accompagne sa valeur nulle
    (`{"valeur": null, "comment": …}` → `{"comment": …}`).
    """
    declarees = set(dsv2.cles_declarees(schema))
    candidats = [cle for cle, neuf in (user_data or {}).items()
                 if cle in declarees and cle not in _META_COLS and _porte_un_null(neuf)]
    if not candidats:
        return user_data
    donnees = en_place() or {}
    out = dict(user_data)
    for cle in candidats:
        if not dsv2._is_empty(dsv2.unwrap(donnees.get(cle))):
            continue
        reste = _sans_la_valeur(out[cle])
        if reste is None:
            del out[cle]
        else:
            out[cle] = reste
    return out


# ── ce qu'une écriture de LISTE fait tomber d'un cran plus bas (oto#120) ────────
#
# La fusion se fait au grain de la COLONNE, jamais de l'élément : reposer une liste
# la remplace en bloc. Les couches portées par ses éléments tombent donc toutes —
# y compris celles des éléments dont la valeur n'a pas changé. **Ce comportement
# n'est pas le défaut** (une liste n'a ni clé ni identité d'élément : rien ne
# permettrait d'aligner l'ancien et le neuf sans inventer une convention) ; le
# défaut était le SILENCE. Les trois relevés ci-dessus ne nomment que des colonnes
# de premier niveau, et cette destruction-là n'apparaissait nulle part.
#
# Ampleur au 06/09/2026 : plus de mille couches vivent dans des éléments de listes
# sur les tableaux en production, et une équipe a failli en effacer soixante-dix
# en croyant enrichir — sauvée par le seul hasard d'un script qui réimbriquait
# chaque élément.
#
# ⚠️ Le parcours MIROITE le lecteur (`served_value` / `_served_item` / `flat_layers`)
# et pas la structure stockée : on ne descend que dans les LISTES, parce que c'est
# exactement là que le lecteur aplatit. Un dict ordinaire est servi tel quel, donc
# une réémission le repose tel quel et il n'y a rien à perdre. La conséquence est
# ce qui rend le conseil vrai : ce qu'on relève est exactement ce qu'un aller-retour
# « relire puis repousser » aurait conservé.


def _couches_imbriquees(valeur: Any, chemin: str, out: dict) -> None:
    """Les couches SERVIES à l'intérieur d'une valeur de liste, par adresse.

    `out[("contacts[0].nom", "comment")] = "registre"`. On passe l'adresse COMPLÈTE
    à `flat_layers` et on la recoupe : c'est elle qui décide ce qu'est une couche
    renseignée, et une seconde copie de ce jugement divergerait un jour — le module
    en a déjà payé le prix ailleurs."""
    if not isinstance(valeur, list):
        return
    for i, item in enumerate(valeur):
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            adresse = f"{chemin}[{i}].{k}"
            for plat, val in dsv2.flat_layers(adresse, v).items():
                champ, _, couche = plat.rpartition(".")
                out[(champ, couche)] = val
            _couches_imbriquees(dsv2.unwrap(v), adresse, out)


def _couches_perdues(ancienne: Any, posee: Any, cle: str,
                     row_id: Optional[str]) -> list[dict]:
    """Les couches d'éléments que cette écriture fait TOMBER, à leur adresse.

    Une couche tombe quand la valeur posée ne la porte plus à la MÊME adresse :
    c'est ce que fait le remplacement en bloc. Reposer la liste telle qu'elle a été
    servie ne relève donc rien — le no-op de `_merge_column` non plus, puisque deux
    valeurs identiques portent les mêmes adresses."""
    if not isinstance(ancienne, list):
        return []
    avant: dict = {}
    _couches_imbriquees(ancienne, cle, avant)
    if not avant:
        return []
    apres: dict = {}
    _couches_imbriquees(posee, cle, apres)
    return [{"ligne": row_id, "champ": champ, "couche": couche, "valeur": val}
            for (champ, couche), val in avant.items()
            if (champ, couche) not in apres]


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

    ⚠️ **« Ce qu'elle efface » porte DEUX natures de destruction** (oto#120) : une
    valeur de premier niveau nommée avec `null`, et les couches d'éléments qu'une
    écriture de liste remplace en bloc. Même question, un cran plus bas, donc même
    parcours — mais `effacements_report` les sépare en deux clés, parce que ce qu'on
    fait pour les rétablir n'est pas le même geste.

    ⚠️ Ce parcours ne décide QUE de la valeur : le sort du GESTE — quand il n'a plus
    rien à poser — se juge après, sur ses trois sorties (`refuser_geste_sans_effet`)."""
    pose: dict = {}
    effaces: list[dict] = []
    ignores: list[dict] = []
    for cle, neuf in (user_data or {}).items():
        touche, posee = _valeur_posee(neuf)
        if cle in _META_COLS or not touche:
            pose[cle] = neuf
            continue
        ancienne = dsv2.unwrap((existing or {}).get(cle))
        if not dsv2._is_empty(posee):
            # La valeur est posée : rien ne tombe au premier niveau. Un cran plus
            # bas, si — la liste est remplacée en bloc (oto#120).
            effaces.extend(_couches_perdues(ancienne, posee, cle, row_id))
            pose[cle] = neuf
            continue
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

    **DEUX clés, parce que deux gestes de rétablissement** (oto#120) : une valeur de
    premier niveau nommée avec `null` se réécrit à son nom ; une couche d'élément
    tombée avec le remplacement en bloc d'une liste se réimbrique dans l'élément.
    Les confondre ferait prescrire l'un pour l'autre — un aller-retour dépensé pour
    rien, exactement ce que la famille de relevés existe pour éviter.

    ⚠️ Depuis #608, `null` est le SEUL vide qui arrive ici : la phrase ne cite donc
    pas les autres vides parmi les valeurs qui effacent, sous peine de prescrire un
    geste qui, lui, est REFUSÉ quand il est seul et ignoré quand il accompagne (#724)."""
    out: dict = {}
    valeurs = [r for r in records or [] if "couche" not in r]
    if valeurs:
        nommes, reste = _nommes(valeurs)
        hint = ("un `null` NOMMÉ dans le payload EFFACE la valeur en place — ce n'est "
                "PAS la même chose que ne pas nommer le champ, qui le laisse intact. Si "
                "l'effacement n'était pas voulu (variable non peuplée, gabarit à demi "
                "rempli), réécris les valeurs ci-dessus : elles ne sont plus en base.")
        if reste:
            hint += f" {len(valeurs)} effacements au total, {len(nommes)} nommés ici."
        out["valeurs_effacees"] = nommes
        out["valeurs_effacees_hint"] = hint
    out.update(couches_effacees_report(
        [r for r in records or [] if "couche" in r]))
    return out


def couches_effacees_report(records: list) -> dict:
    """Le relevé des couches d'éléments tombées avec le remplacement d'une liste.

    Même forme que `valeurs_effacees` — la ligne, le champ, la valeur perdue — plus
    la couche, parce qu'ici l'adresse ne suffit pas à la désigner. `champ` porte le
    RANG (`contacts[0].nom`) : sans lui, « trois `comment` sont tombés » ne se
    rétablit pas.

    ⚠️ Le conseil est le seul qui marche aujourd'hui, et il est étroit : **relire,
    puis reposer la liste ENTIÈRE en réémettant les couches telles qu'elles ont été
    servies**. Il n'y a pas d'écriture au grain de l'élément — une liste n'a pas
    d'identité d'élément — donc pas de geste plus petit à prescrire."""
    if not records:
        return {}
    nommes, reste = _nommes(records)
    hint = ("reposer une colonne-liste la remplace EN BLOC : la fusion se fait au "
            "grain de la colonne, jamais de l'élément. Les couches ci-dessus étaient "
            "portées par des éléments de la liste et ne sont PLUS en base — y compris "
            "sur les éléments dont la valeur n'a pas changé. Il n'existe pas "
            "d'écriture d'un élément seul : pour écrire dans une liste qui porte des "
            "couches, relis la ligne et repose la liste entière en réémettant les "
            "couches telles qu'elles t'ont été servies (`nom.comment` à côté de `nom`, "
            "dans le même élément). Si la perte n'était pas voulue, réécris les "
            "valeurs ci-dessus de cette façon.")
    if reste:
        hint += f" {len(records)} couches au total, {len(nommes)} nommées ici."
    return {"couches_effacees": nommes, "couches_effacees_hint": hint}


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


def _cle_d_item(champ: Any) -> Optional[str]:
    """Le champ qui IDENTIFIE un élément d'une liste — `of.key`, ou `None`.

    Même mot que la clé métier d'une ligne (`schema.key`), un cran plus bas et pour la
    même raison : dire ce qui fait qu'un élément est « le même » d'une écriture à
    l'autre. Sans elle, une liste se remplace en bloc, comme depuis toujours.

    ⚠️ **C'est une identité de CRÉNEAU, pas de personne** (tranché le 08/09/2026). Le
    bon candidat est une catégorie stable et fermée — `contact_rh`, `contact_paie` —
    et surtout pas un email ou un nom. Que l'occupant d'un créneau change (Jane
    remplacée par Doe après une passe d'agent) est le geste NORMAL que ce mécanisme
    doit servir ; apparier des gens sur leur nom serait au contraire le mode d'échec
    qu'on refuse."""
    of = champ.get("of") if isinstance(champ, dict) else None
    cle = of.get("key") if isinstance(of, dict) else None
    return cle if isinstance(cle, str) and cle else None


def _index_par_cle(items: Any, cle: str) -> dict:
    """`{valeur d'identité: élément}` — et le premier gagne sur un doublon.

    Le doublon est REFUSÉ un cran plus haut (`_merge_items`) : on ne peut pas
    apparier deux éléments qui se disent le même, et choisir en silence serait
    exactement l'appariement muet qu'on s'interdit."""
    out: dict = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        v = dsv2.unwrap(it.get(cle))
        if v not in (None, "") and v not in out:
            out[v] = it
    return out


def _merge_items(avant: Any, nouveaux: list, cle: str,
                 nom: str = "liste") -> list:
    """Fusionne une liste ÉLÉMENT PAR ÉLÉMENT sur l'identité déclarée.

    Ce que ça répare : une liste se fusionnait en bloc, donc un agent qui réémettait
    ses contacts pour corriger UN email effaçait les couches de TOUS les éléments —
    provenance et version d'origine comprises. Sur une colonne de premier niveau
    l'origine survit à une écriture ; dans un élément de liste, elle ne survivait à
    rien.

    Trois règles, et les deux dernières existent pour que rien ne se fasse en silence :

    - un élément dont l'identité correspond est FUSIONNÉ attribut par attribut, avec
      la même règle que les colonnes — ses couches survivent ;
    - un élément **sans** valeur d'identité n'est apparié à rien : il entre tel quel.
      Deviner à quoi il correspond serait inventer ;
    - une identité **en double** dans l'une des deux listes LÈVE, en nommant la valeur.
      Deux éléments qui se disent le même ne sont pas départageables, et prendre le
      premier apparierait au hasard des données de personnes.
    """
    def _doublons(source) -> set:
        vues, doubles = set(), set()
        for it in source if isinstance(source, list) else []:
            if not isinstance(it, dict):
                continue
            v = dsv2.unwrap(it.get(cle))
            if v in (None, ""):
                continue
            if v in vues:
                doubles.add(v)
            vues.add(v)
        return doubles

    # La liste ENVOYÉE : un doublon y est refusé, au moment où l'appelant peut encore
    # le corriger — c'est SON geste, et deux éléments de même identité ne sont pas
    # appariables.
    doubles = _doublons(nouveaux)
    if doubles:
        raise RowValidationError(
            [f"`{cle}` en double dans la liste posée : "
             + ", ".join(repr(d) for d in sorted(doubles, key=str))
             + f" — deux éléments qui portent la même identité ne peuvent pas être "
             f"appariés. Donne à chacun une valeur de `{cle}` distincte."])

    # ⚠️ **La liste EN PLACE ne refuse plus rien, et c'est un correctif.**
    #
    # Elle levait aussi — donc une ligne qui portait déjà un doublon n'acceptait plus
    # AUCUNE écriture, **y compris celle qui l'aurait réparé** : la validation jugeait
    # l'état, pas le geste. Le refus proposait deux sorties et aucune ne marchait —
    # « donne des valeurs distinctes » (impossible, tout était refusé) et « retire
    # `of.key` » (qui ne se retire pas). La fiche était dans une impasse.
    #
    # C'est exactement le défaut que ce module dénonce ailleurs — *une garde qui
    # bloque le geste qui la lèverait* — et je l'ai posé en le citant. Signalé par la
    # campagne le 08/09/2026, sur un tableau jetable, avant que ça n'arrive en vrai.
    #
    # Quand l'état est ambigu, on ne peut pas apparier : on REMPLACE en bloc, ce qui
    # est le comportement d'une liste sans clé — et la liste envoyée, elle, est valide.
    # Le geste répare au lieu d'échouer.
    if _doublons(avant):
        return _sentinelles_dans_les_items(nouveaux, nom)

    index = _index_par_cle(avant, cle)
    out = []
    for it in nouveaux:
        if not isinstance(it, dict):
            out.append(it)
            continue
        v = dsv2.unwrap(it.get(cle))
        ancien = index.get(v) if v not in (None, "") else None
        if ancien is None:
            out.append(it)
            continue
        fusionne = dict(ancien)
        for k, val in it.items():
            fusionne[k] = _merge_column(ancien.get(k), val)
        out.append({k: v2 for k, v2 in fusionne.items() if v2 is not None})
    return out


def _sentinelles_dans_les_items(nouveaux: Any, chemin: str) -> Any:
    """Résout `@empty` et REFUSE `@keep` dans les éléments d'une liste remplacée.

    ⚠️ **Le trou que ça ferme a atteint la production**, et il a été trouvé sur de la
    donnée servie, pas dans un journal : `contacts[0].commentaire.comment` valait
    littéralement `"@keep"` sur une fiche de campagne. L'agent avait écrit le mot au
    bon endroit — c'est une couche d'un attribut d'élément, un endroit parfaitement
    légitime — et la plateforme l'a pris pour du texte. Une ligne de plus et la
    cliente lisait « @keep » dans le commentaire d'un contact.

    **Pourquoi ce chemin échappait à la résolution.** Une liste sans identité d'élément
    déclarée se REMPLACE en bloc : `_merge_column` ne descend pas dedans, donc rien n'y
    résolvait les deux mots. Avec `of.key`, `_merge_items` fusionne attribut par
    attribut et les résout — le défaut n'existe que sur le chemin du remplacement.

    **Les deux mots ne se traitent pas pareil ici, et la raison est structurelle :**

    - `@empty` **se résout** : « vide-le » ne demande aucun passé, donc il tient sans
      identité d'élément ;
    - `@keep` **se refuse** : « garde ce qui est là » exige de savoir QUEL élément
      précédent correspond à celui-ci. Sans identité déclarée, on ne le sait pas — et
      choisir au hasard sur des données de personnes est le mode d'échec qu'on s'est
      interdit. Le laisser tomber en silence perdrait l'intention de l'agent ; le
      stocker l'expédie chez la cliente. **Refuser en nommant le geste est la seule
      des trois issues qui ne ment pas.**
    """
    if not isinstance(nouveaux, list):
        return nouveaux

    refus: list[str] = []

    def _valeur(v: Any, ou: str) -> Any:
        if v == dsl.GARDE:
            refus.append(ou)
            return v
        return "" if v == dsl.VIDE_DELIBERE else v

    out = []
    for i, item in enumerate(nouveaux):
        if not isinstance(item, dict):
            out.append(item)
            continue
        propre = {}
        for cle, val in item.items():
            base = f"{chemin}[{i}].{cle}"
            if isinstance(val, dict) and dsv2.names_layers(val):
                propre[cle] = {c: _valeur(v, f"{base}.{c}" if c != dsv2.VALUE_LAYER
                                          else base)
                               for c, v in val.items()}
            else:
                propre[cle] = _valeur(val, base)
        out.append(propre)

    if refus:
        raise RowValidationError(
            [f"`{dsl.GARDE}` ne peut pas être tenu dans une liste sans identité "
             f"d'élément : {', '.join('`' + r + '`' for r in refus)}. Une liste se "
             "remplace en bloc, donc rien ne dit QUEL élément précédent correspond à "
             f"celui-ci. Deux issues : déclarer `of.key` au schéma de `{chemin}` (le "
             "nom d'un CRÉNEAU stable — `contact_rh`, jamais un nom de personne), et "
             f"la fusion se fera élément par élément ; ou renvoyer le contenu au lieu "
             f"de `{dsl.GARDE}`. `{dsl.VIDE_DELIBERE}`, lui, fonctionne ici — il ne "
             "demande aucun passé."])
    return out


def _merge_column(existing: Any, new: Any, champ: Any = None) -> Any:
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
    comment sans faire tomber le link — la valeur n'a pas changé, rien ne tombe.

    ⚠️ **Deux mots réservés depuis oto#140** : `@keep` sur un sous-champ repose ce qui
    était là — sans que l'appelant ait à le relire ni à le retaper —, `@empty` y pose un
    vide DÉLIBÉRÉ, qui ne se confond pas avec l'absence. Ils se résolvent ICI et nulle
    part ailleurs : qu'un seul passe en aval et il serait stocké comme une valeur, puis
    servi à une cliente comme sa propre donnée."""
    # Une LISTE dont le schéma déclare l'identité de ses éléments se fusionne
    # élément par élément ; sans déclaration, elle se remplace en bloc, comme avant.
    cle_item = _cle_d_item(champ)
    if isinstance(new, list):
        if cle_item:
            # ⚠️ **La colonne en place peut porter des COUCHES autour de sa liste** —
            # `{"valeur": [...], "origine": {...}}` — depuis qu'un import déclaré
            # (`donnees_d_origine`) fige la version d'origine de chaque case. Passer
            # ce dict tel quel à `_merge_items` le faisait lire comme « rien en
            # place » : la liste entière était remplacée, et l'origine partait avec.
            #
            # Mesuré le 08/09/2026 sur le geste le plus banal qui soit : importer un
            # contact « RH / Alice / ancien email », puis corriger le seul email.
            # Alice disparaissait, et la version d'origine aussi — c'est-à-dire
            # exactement ce que la fusion par créneau existe pour empêcher.
            #
            # On déballe donc pour fusionner, et on REPOSE le résultat dans les
            # couches qui étaient là. Deux gestes, parce que la liste est la valeur
            # de la colonne, pas la colonne.
            couches = _existing_layers(existing)
            fusion = _merge_items(couches.get(dsv2.VALUE_LAYER), new, cle_item,
                                  str((champ or {}).get("key") or "liste"))
            if len(couches) > 1 or dsv2.ORIGIN_LAYER in couches:
                couches[dsv2.VALUE_LAYER] = fusion
                return couches
            return fusion
        # Remplacement en bloc : personne ne descend plus dans les éléments après
        # cette ligne, donc les deux mots réservés se règlent ICI ou jamais.
        nom = str((champ or {}).get("key") or "liste")
        new = _sentinelles_dans_les_items(new, nom)

    # ── Les mots réservés valent AUSSI sur une valeur nue (oto#140) ─────────
    #
    # ⚠️ **C'est le trou que ce lot aurait ouvert sans cette garde**, et il a été
    # signalé par la campagne avant la mise en production, pas après : un agent
    # recopie ce qu'on lui montre — mesuré, cinquante emplois pour un exemple — mais
    # il peut le recopier AU MAUVAIS ENDROIT. `{"champ": "@keep"}` au lieu de
    # `{"champ": {"valeur": …, "comment": "@keep"}}`, et sans cette résolution la
    # chaîne `@keep` partait en base, puis chez la cliente comme sa propre donnée.
    #
    # On RÉSOUT plutôt que de refuser, parce que l'intention est claire dans les deux
    # cas et qu'un refus ferait rejouer l'appel sans que l'agent comprenne : poser le
    # mot sur toute la case dit la même chose que le poser sur son sous-champ.
    #
    # ⚠️ Le test est au mot ENTIER : `vu au registre @keep` reste du texte, et
    # `contact@keepcool.fr` aussi. Une sentinelle qui mordrait au milieu d'une chaîne
    # serait le défaut qu'on vient de passer la nuit à traquer — un motif plus large
    # que ce qu'il prétend viser.
    if not _writes_layers(new) and dsl.est_sentinelle(new):
        if new == dsl.GARDE:
            return existing                      # « n'y touche pas » : rien ne bouge
        new = ""                                 # `@empty` : le vide DÉLIBÉRÉ

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
    avant = _existing_layers(existing)

    # ── Les deux mots réservés, résolus AVANT toute décision (oto#140, palier 1) ──
    #
    # `@keep` = « je n'y touche pas, sans avoir à le renvoyer » ; `@empty` = « je le
    # vide, délibérément ». Résolus ici et nulle part ailleurs : l'aval ne doit jamais
    # voir passer une sentinelle, sous peine de la stocker comme une valeur.
    #
    # ⚠️ `@keep` sur une couche ABSENTE ne crée rien — on garde le néant, ce qui est
    # exactement ce que le mot promet. Poser `""` à la place inventerait un « vide
    # délibéré » que l'appelant n'a pas demandé, et les deux ne se lisent pas pareil.
    pose = {}
    for cle, val in new.items():
        if val == dsl.GARDE:
            if cle in avant:
                pose[cle] = avant[cle]
        elif val == dsl.VIDE_DELIBERE:
            pose[cle] = ""
        else:
            pose[cle] = val

    out = dict(avant)
    if dsv2.VALUE_LAYER in pose and not dsv2.same_value(out.get(dsv2.VALUE_LAYER),
                                                        pose[dsv2.VALUE_LAYER]):
        # ⚠️ La chute reste INCONDITIONNELLE, et c'est volontairement inchangé.
        #
        # J'avais d'abord écrit ici une garde « une couche nommée ne tombe pas ».
        # **Elle était du code mort**, et c'est l'épreuve rouge du banc qui l'a montré :
        # en la désarmant, les treize cas restaient verts. La raison est que `pose`
        # repose juste après ce que `@keep` a rattrapé — faire tomber puis reposer
        # donne le même résultat que ne pas faire tomber.
        #
        # Le comportement neuf vient donc ENTIÈREMENT de la résolution des sentinelles
        # au-dessus, et de rien d'autre. Le dire ici évite qu'on croie demain que cette
        # boucle porte la règle : elle ne porte que l'ancienne, intacte.
        for couche in dsv2.VALUE_BOUND_LAYERS:
            out.pop(couche, None)
    out.update(pose)
    out = {k: v for k, v in out.items() if v is not None}
    if not out:
        return None
    if set(out) == {dsv2.VALUE_LAYER}:
        return out[dsv2.VALUE_LAYER]
    return out
