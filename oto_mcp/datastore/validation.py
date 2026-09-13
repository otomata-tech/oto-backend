"""VALIDER une ligne à l'écriture — le chemin chaud, et ses textes de refus.

`validate_row` est le point d'entrée : elle rend les raisons de refuser une ligne
fusionnée contre le format déclaré. Sous elle, `_row_errors` descend la déclaration et
`_type_error` juge chaque valeur — **mutuellement récursives** (un composite redescend
dans `_row_errors`) : c'est pourquoi elles ne se séparent pas.

Le reste est la PROSE du refus, et elle n'est pas décorative : lu par un agent en
boucle, un refus doit dire ce qui est attendu (`_forme_attendue`), pourquoi c'est exigé
maintenant (`_cause_required_when`, `_gated_by`) et sous quelle condition l'exigence
tomberait (`_clause_aiguillage`). Un refus muet fait rejouer le même appel.

Ce qu'il ne tient pas : la validité du FORMAT, jugée une fois à la pose
(`definition.py`) ; les couches exigées (`couches_exigees.py`) et les clés hors
référentiel (`hors_schema.py`), appelés d'ici ; les champs réservés
(`champs_reserves.py`), jugés sur le GESTE — payload + ligne en place — non sur une
ligne seule ; le coût d'un motif (`motifs.py`), qu'on ne fait ici qu'exécuter.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from .couches import (_is_empty, CLES_INTERNES, LAYER_KEYS, layer_value, split_layer,
                      unknown_layers, unwrap, vide_assume)
from .options_declarees import hors_des_options, montrable
from .motifs import _pattern_re
from .declaration import _fields, max_length_of, pattern_of, status_field, validation_active
from .etats_declares import etats_trahis
from .types_declares import types_trahis
from .cycle_de_vie import lifecycle_of, refus_de_transition
from .hors_schema import _unknown_subkey_refusal, _unknown_subkeys
from .couches_exigees import couches_manquantes
from .phrases_de_refus import (
    _forme_attendue, _gated_by, _cause_required_when, _clause_aiguillage,
)

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _conformite_scalaire(value: Any, ftype: Optional[str], path: str) -> list[str]:
    """La FORME d'une valeur scalaire, sans son appartenance à une liste — que
    `_hors_options` juge ensuite. `json` et l'absence de type n'ont pas de forme à
    tenir : ils rendent `[]`."""
    if ftype == "text":
        return [] if isinstance(value, str) else [f"{path}: attendu text, reçu {type(value).__name__}"]
    if ftype == "number":
        if isinstance(value, bool):
            return [f"{path}: attendu number, reçu bool"]
        if isinstance(value, (int, float)):
            return []
        if isinstance(value, str) and _NUM_RE.match(value.strip()):
            return []  # coercible — l'agent écrit souvent "42"
        return [f"{path}: attendu number, reçu {value!r}"]
    if ftype == "bool":
        return [] if isinstance(value, bool) else [f"{path}: attendu bool, reçu {value!r}"]
    if ftype in ("date", "datetime"):
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return []
            except ValueError:
                pass
        return [f"{path}: attendu {ftype} ISO, reçu {value!r}"]
    if ftype == "url":
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return []
        return [f"{path}: attendu une URL http(s), reçu {value!r}"]
    if ftype == "email":
        if isinstance(value, str) and "@" in value and " " not in value.strip():
            return []
        return [f"{path}: attendu un e-mail, reçu {value!r}"]
    return []


def _hors_options(value: Any, options: Optional[list], path: str,
                  hors: Optional[list]) -> list[str]:
    """L'appartenance à la liste DÉCLARÉE, pour tout type scalaire (#98).

    Jusqu'au 10/09/2026, seule la branche `enum` lisait `options` : sur un tableau
    strict, une liste posée sur un texte, un json ou une colonne sans type ne refusait
    rien, et le signalement du régime souple se taisait aussi dès que la validation
    était armée — personne ne voyait passer la valeur. La règle vient de
    `options_declarees`, la même que le signalement souple et que le relevé de
    l'existant à la pose.

    Le relevé structuré (`hors`) est rempli comme il l'était pour les enums : c'est lui
    qui permet à l'écriture d'ÉCARTER la valeur et d'écrire le reste (#667)."""
    if not hors_des_options(value, options):
        return []
    allowed = [str(o) for o in options]
    if hors is not None:
        hors.append({"champ": path, "valeur": value, "options": allowed})
    return [f"{path}: valeur {montrable(value)!r} hors options ({', '.join(allowed)})"]


def _type_error(value: Any, ftype: Optional[str], path: str,
                fields: Optional[list] = None, of: Optional[dict] = None,
                options: Optional[list] = None, *,
                closed: bool = False,
                hors: Optional[list] = None) -> list[str]:
    """Erreurs de conformité d'UNE valeur à un type déclaré (récursif).

    `closed` = le référentiel de CE composite est fermé (#544) : un attribut que sa
    déclaration ne nomme pas est refusé, au lieu d'être traversé en silence. Il se
    propage vers le bas — une liste d'objets dans un objet reste fermée.

    `hors` (liste mutable, optionnelle) = le relevé STRUCTURÉ des valeurs hors
    options (#667), rempli en chemin : `{champ, valeur, options}`. Il existe pour
    que l'appelant puisse ÉCARTER la valeur sans reparser le message — un refus
    français relu comme un contrat est un contrat déguisé. Optionnel par
    construction : ce validateur reste pur si personne ne le lui passe."""
    if ftype == "enum":
        # `options` absentes ⇒ enum libre (le client rend un select vide, pas d'erreur).
        if not isinstance(value, str):
            return [f"{path}: attendu une valeur d'énumération, reçu {value!r}"]
        return _hors_options(value, options, path, hors)
    if ftype == "object":
        if not isinstance(value, dict):
            return [f"{path}: attendu object, reçu {type(value).__name__}"]
        return _row_errors(fields or [], value, path, closed=closed, hors=hors)
    if ftype == "list":
        if not isinstance(value, list):
            return [f"{path}: attendu list, reçu {type(value).__name__}"]
        errors: list[str] = []
        of = of or {}
        sub_fields = of.get("fields")
        # Un attribut inconnu se nomme UNE fois pour toute la colonne, sur le premier
        # élément qui le porte : les items d'une liste partagent leur déclaration,
        # donc 300 contacts fautifs diraient 300 fois la même chose. Même borne que
        # l'agrégation du relevé `hors_schema` (`clé[].sous_clé`), et même raison :
        # un refus qu'on ne peut pas lire ne vaut pas mieux qu'un silence.
        vus: set = set()
        for i, item in enumerate(value):
            ipath = f"{path}[{i}]"
            if isinstance(sub_fields, list):
                if not isinstance(item, dict):
                    errors.append(f"{ipath}: attendu object, reçu {type(item).__name__}")
                else:
                    errors.extend(_row_errors(
                        [x for x in sub_fields if isinstance(x, dict)], item, ipath,
                        closed=closed, vus=vus, hors=hors))
            elif of.get("type") or of.get("options"):
                errors.extend(_type_error(item, of.get("type"), ipath,
                                          of.get("fields"), of.get("of"),
                                          of.get("options"), closed=closed,
                                          hors=hors))
        return errors
    # Tout autre type scalaire — et l'absence de type — passe par la MÊME liste (#98) :
    # d'abord la forme, puis l'appartenance, jamais les deux sur une même valeur. Deux
    # refus pour un seul relevé `hors` feraient refuser la fiche entière, là où
    # l'écriture sait écarter la valeur et écrire le reste (#667).
    return _conformite_scalaire(value, ftype, path) or _hors_options(value, options, path, hors)


def _row_errors(fields: list, data: dict, path: str,
                written: Optional[set] = None, *,
                strict: bool = False, closed: bool = False,
                vus: Optional[set] = None,
                details: Optional[dict] = None,
                hors: Optional[list] = None,
                gelees: Optional[list] = None) -> list[str]:
    """Erreurs d'un (sous-)record. `written` = clés effectivement RÉÉCRITES par ce
    geste (None = toutes) : la borne de longueur, le motif, la fermeture d'un
    composite **et le TYPE** s'y restreignent — eux seuls, cf. `validate_row`. La
    récursion dans un sous-record repart à None — remplacer une clé de premier niveau
    réécrit tout ce qu'elle contient.

    `gelees` = liste OUT (patron `hors`) où part le type qui échoue sur une colonne
    que le geste **n'écrit pas**. Elle ne refuse plus : elle se DIT.

    `strict` = le tableau déclare `strict: true`. Il n'interdit rien ICI (une clé
    inconnue au premier niveau crée une colonne libre, droit du contrat 0016 : elle
    est SIGNALÉE par `hors_schema`, jamais refusée — arbitrage #294) ; il FERME les
    composites déclarés d'un cran plus bas (#544). `closed` porte cette fermeture.

    Pourquoi l'asymétrie, alors que « strict s'applique récursivement » : au premier
    niveau, un nom inconnu crée une vraie colonne, que l'interface affiche et qu'on
    peut déclarer après coup — c'est ce qui permet d'explorer un tableau avant de le
    typer. Dans un composite déclaré, il n'existe pas de « sous-colonne libre » :
    `of.fields` EST le seul référentiel, et l'attribut serait stocké là où rien ne le
    lit. Le geste qu'on protège en haut n'existe pas en bas.

    `vus` = les attributs déjà nommés pour la colonne-liste courante (borne du
    refus, cf. `_type_error`).

    `details` (dict mutable, optionnel) = le refus STRUCTURÉ que l'appelant récupère,
    aujourd'hui `expected_column` (#545). Renseigné au PREMIER cas rencontré et jamais
    écrasé : un refus en porte une, pas une liste — le message, lui, les dit toutes.
    Non propagé aux sous-records : « la colonne attendue » d'un sous-champ imbriqué
    serait ambiguë côté client, et un pointeur ambigu ne vaut pas mieux qu'aucun."""
    errors: list[str] = []
    if closed:
        for cle in _unknown_subkeys(fields, data):
            if vus is not None:
                if cle in vus:
                    continue
                vus.add(cle)
            errors.append(_unknown_subkey_refusal(
                f"{path}.{cle}" if path else cle, fields))
    # Les colonnes-AIGUILLAGE de ce niveau, et ce qu'elles rendent requis.
    portes = _gated_by(fields)
    for f in fields:
        key = f.get("key")
        if not key:
            continue
        fpath = f"{path}.{key}" if path else key
        # Le marqueur du vide assumé (oto#204) est une clé INTERNE, pas un sous-champ.
        inconnues = [k for k in unknown_layers(data.get(key)) if k not in CLES_INTERNES]
        if inconnues:
            errors.append(
                f"{fpath}: sous-champ(s) inconnu(s) {', '.join(repr(k) for k in inconnues)}"
                f" — disponibles : {', '.join(LAYER_KEYS)}. Une couche stockée sans "
                "être lue donnerait l'illusion d'une provenance renseignée.")
        # Déballer avant de juger : c'est la VALEUR qui doit respecter le type, la
        # borne et les options — pas son enveloppe. Sans ça un schéma strict refuse
        # toute écriture en couches, donc la primitive est inutilisable là où elle
        # sert le plus.
        #
        # Une clé POINTÉE (#377) désigne une couche : `qualification.comment` est la
        # justification portée par la colonne `qualification`, pas une colonne du
        # même nom. Le contrôle lisait `data["qualification.comment"]` — un nom que
        # `_refuse_dotted_names` interdit précisément d'ÉCRIRE : la contrainte ne
        # pouvait donc jamais être satisfaite, et refusait jusqu'aux écritures qui
        # portaient bien le commentaire. Accepté à la pose, bloquant à l'écriture :
        # un cran plus grave qu'inerte, parce que la déclaration avait l'air d'avoir
        # pris.
        base, layer = split_layer(key)
        value = (layer_value(data.get(base), layer) if layer
                 else unwrap(data.get(key)))
        # Ce que le geste POSE — hissé ici parce que la fermeture d'un composite s'y
        # restreint, exactement comme la borne et le motif : la validation porte sur
        # le MERGÉ, donc juger un composite que le geste ne réécrit pas rendrait
        # inécritable toute ligne portant déjà un attribut hors format, y compris
        # pour un patch sans rapport (les 23 lignes gelées d'oto-backend#284).
        pose = written is None or key in written
        required = bool(f.get("required"))
        rw = f.get("required_when")
        if not required and isinstance(rw, dict) and rw:
            # Une condition en LISTE = requis quand la valeur ∈ liste (#347).
            # Avant, str(liste) ne matchait jamais : la déclaration qui semblait
            # ÉLARGIR la garde la rendait inerte, sans un mot.
            # ⚠️ La valeur de condition se DÉBALLE (unwrap) comme toute valeur
            # jugée — une qualification écrite en couches ({"valeur": …}) est un
            # dict brut qui ne matche rien : la garde était désarmée par le
            # geste NORMAL des agents (justifier en couches), et par tout merge
            # sur une ligne portant déjà une couche (prouvé en re-validation :
            # 5 fiches écartées sans motif, aucun refus).
            required = all(
                str(unwrap(data.get(k))) in {str(x) for x in v}
                if isinstance(v, (list, tuple))
                else str(unwrap(data.get(k))) == str(v)
                for k, v in rw.items())
        if _is_empty(value):
            # oto#204 : le vide ASSUMÉ (marqueur posé par la résolution de `@empty`)
            # satisfait l'obligation ; une chaîne vide ordinaire, non.
            if required and not (not layer and vide_assume(data.get(key))):
                cause = (_cause_required_when(rw)
                         if not f.get("required") and rw else "")
                # #545 : la colonne était déjà nommée ; ce qui manquait, c'est sa
                # FORME — et la prévention du geste suivant. Sans elle, l'agent
                # corrige en écrivant le motif DANS l'aiguillage, se fait refuser une
                # seconde fois, et paie deux allers-retours pour une seule ligne :
                # exactement la séquence mesurée (35 refus sur 105, 27 rattrapés au
                # coup d'après).
                errors.append(f"{fpath}: champ requis manquant{cause} — "
                              f"elle attend {_forme_attendue(f)}"
                              + _clause_aiguillage(fields, rw))
                if details is not None:
                    details.setdefault("expected_column", str(key))
            continue
        # `options` sans type compte aussi (#98) : la liste est déclarée, la valeur doit
        # y être — le type absent dit seulement qu'il n'y a pas de FORME à tenir.
        if f.get("type") or f.get("options"):
            errs_type = _type_error(value, f.get("type"), fpath,
                                    f.get("fields"), f.get("of"), f.get("options"),
                                    closed=closed or (strict and pose),
                                    hors=hors)
            # #545 : la colonne qui vient de refuser est-elle un AIGUILLAGE dont une
            # autre colonne dépend ? Alors la chaîne libre qu'on y a écrite a une
            # destination déclarée, et le refus doit la donner — c'est le cas
            # majoritaire des 35 : « le motif va dans `retraitement_motif`, pas dans
            # `retraitement` ». La condition est stricte (énuméré + options déclarées
            # + valeur texte hors liste) : hors de là, le pointeur serait une
            # devinette.
            gardees = portes.get(str(key)) or []
            if (errs_type and gardees and f.get("type") == "enum"
                    and isinstance(value, str)
                    and value not in [str(o) for o in (f.get("options") or [])]):
                cible = gardees[0]
                errs_type[-1] += (
                    f" — cette valeur va dans `{cible.get('key')}` "
                    f"({_forme_attendue(cible)}), pas dans `{key}`")
                if details is not None:
                    details.setdefault("expected_column", str(cible.get("key")))
                # #667 : cette valeur-là a une DESTINATION déclarée — elle est mal
                # rangée, pas indésirable. L'écarter écrirait une fiche qui prétend
                # ne pas avoir été retraitée, et l'agent verrait un succès : la
                # corruption silencieuse, en pire que la perte. Le relevé porte donc
                # la destination, et l'écartement s'y refuse.
                if hors and hors[-1].get("champ") == fpath:
                    hors[-1]["destination"] = str(cible.get("key"))
            if errs_type and not pose:
                # La colonne n'est pas écrite par ce geste. La refuser rendrait la
                # ligne INÉCRITABLE pour toujours, sur n'importe quel champ — c'est
                # exactement ce que la restriction de la borne et du motif a corrigé
                # (23 lignes gelées chez un client), et le type y avait été oublié.
                #
                # ⚠️ On ne se tait pas pour autant : la valeur en base ne passe plus
                # le format déclaré, et l'agent qui écrit à côté est le mieux placé
                # pour le savoir. Refuser gèle, taire cache — on DIT.
                if gelees is not None:
                    gelees.append({"champ": fpath, "refus": errs_type[0]})
            else:
                errors.extend(errs_type)
        mi = f.get("max_items")
        if (isinstance(mi, int) and not isinstance(mi, bool) and mi > 0
                and isinstance(value, list) and len(value) > mi):
            # Même forme que la borne de longueur : le CONSTATÉ autant que la borne,
            # sinon le refus fait deviner de combien on dépasse.
            #
            # Et même RESTRICTION qu'elle, alignée le 07/09/2026 : c'est une propriété
            # de la valeur qu'on POSE. Sur une colonne que le geste n'écrit pas, une
            # liste déjà trop longue rendrait la ligne inécritable pour toujours, y
            # compris sur un champ sans rapport — le défaut que le type venait de
            # quitter, laissé sur son voisin immédiat.
            trop = f"{fpath}: {len(value)} éléments, maximum {mi}"
            if pose:
                errors.append(trop)
            elif gelees is not None:
                gelees.append({"champ": fpath, "refus": trop})
        ml = max_length_of(f)
        trop_long = False
        if ml and pose:
            n = len(value) if isinstance(value, str) else len(str(value))
            if n > ml:
                # La longueur CONSTATÉE autant que la borne : un refus qui ne dit
                # pas de combien on dépasse fait deviner (signal #383).
                errors.append(f"{fpath}: {n} caractères, maximum {ml}")
                trop_long = True
        # #387 : la FORME, là où la taille ne sépare rien. Restreint aux clés que le
        # geste ÉCRIT, comme la borne et pour la même raison : la validation porte
        # sur le résultat MERGÉ, donc sans cette restriction une ligne déjà non
        # conforme deviendrait inécritable pour n'importe quel patch, y compris sur
        # un champ sans rapport (23 lignes gelées chez un client, oto-backend#284).
        # Sauté quand la valeur dépasse déjà la borne : c'est ELLE qui garantit que
        # le motif s'exécute sur un sujet de taille connue — et le refus est déjà
        # posé, l'ajouter en double ne dirait rien de plus.
        motif = pattern_of(f)
        if motif and pose and not trop_long:
            texte = value if isinstance(value, str) else str(value)
            if not _pattern_re(motif).search(texte):
                # La valeur CONSTATÉE autant que le motif attendu : sans le motif, le
                # refus ne laisse rien à corriger ; sans la valeur, il fait relire la
                # ligne pour savoir ce qui coince.
                errors.append(
                    f"{fpath}: {texte!r} ne suit pas le motif `{motif}`")
    return errors


def validate_row(schema: Optional[dict], merged: dict, *,
                 prev_status: Any = None,
                 written: Optional[set] = None,
                 details: Optional[dict] = None,
                 hors: Optional[list] = None,
                 gelees: Optional[list] = None) -> list[str]:
    """Erreurs d'une row TELLE QU'ELLE SERA ÉCRITE (le résultat mergé, pas le
    patch) : required / required_when / types / structure imbriquée — si la
    validation est active — plus le cycle de vie (états + transitions) dès qu'un
    `lifecycle` est déclaré, même hors mode strict. Liste vide = OK.

    Sur un tableau `strict`, un composite DÉCLARÉ est en plus un référentiel FERMÉ
    (#544) : un attribut absent de `of.fields` / `fields` est refusé. La fermeture
    ne descend que dans les composites que le geste RÉÉCRIT — même restriction que
    `max_length`, et même raison.

    `written` = les clés que ce geste réécrit (None = la row entière, cas d'un
    insert ou d'un remplacement). **Quatre** contrôles s'y restreignent, et eux
    seuls : la borne `max_length`, le motif `pattern`, la fermeture d'un composite
    et le **TYPE** — ce sont des propriétés de la valeur qu'on POSE, pas de l'état
    final. Sans ça, une valeur trop longue (ou hors format, ou hors type) déjà en
    base ferait échouer tout patch ultérieur de la ligne, même portant sur un champ
    sans rapport (signal #383, et les 23 lignes gelées d'oto-backend#284). Le reste
    continue de se juger sur le mergé : un requis manquant est un défaut de la row,
    quel que soit le geste qui l'y laisse.

    ⚠️ **Le type est arrivé le 07/09/2026, et son absence était un OUBLI, pas un
    choix.** La restriction des trois premiers cite en toutes lettres le défaut
    qu'elle ferme — « une valeur déjà en base ferait échouer tout patch ultérieur,
    même sur un champ sans rapport » — et le type produisait exactement ça, signalé
    par un tenant sur des lignes de socle devenues inécritables. Il ne se TAIT pas
    pour autant : ce qu'il ne refuse plus part dans `gelees`.

    `gelees` = liste OUT : les colonnes dont la valeur EN BASE ne passe plus le type
    déclaré, alors que ce geste ne les écrit pas. Ni refus ni silence — l'agent qui
    écrit à côté est le mieux placé pour l'apprendre.

    `details` (dict mutable, optionnel) = le refus STRUCTURÉ, rempli en chemin —
    aujourd'hui `expected_column`, la colonne où la valeur aurait dû atterrir (#545).
    Optionnel par construction : un validateur PUR ne doit pas exiger un accumulateur
    de ses appelants pour rendre ses erreurs."""
    errors: list[str] = []
    if validation_active(schema):
        # required_when se juge sur la row finale (le statut mergé, pas l'ancien)
        errors.extend(_row_errors(_fields(schema), merged, "", written,
                                  strict=bool(schema.get("strict")),
                                  details=details, hors=hors, gelees=gelees))
    # oto#75 barreau 1 : HORS du garde `validation_active`, comme le cycle de vie
    # ci-dessous — la déclaration `required_layers` s'arme elle-même.
    errors.extend(couches_manquantes(schema, merged, written=written))
    # 08/09/2026 — même raison, même place : un `type` déclaré s'arme lui-même. Le
    # contrôle existait sous `validation_active` et n'y voyait rien passer (0 violation
    # sur 88 tableaux) pendant que 248 tableaux sans validation en portaient 118.
    errors.extend(types_trahis(schema, merged))
    # 09/09/2026 — le cran suivant de la même famille : une colonne SECONDAIRE qui
    # déclare ses états les fait respecter, elle aussi. Seule la file était vérifiée ;
    # 131 colonnes du parc déclaraient une liste que personne n'appliquait. Mesuré
    # avant de brancher : zéro valeur hors liste sur 8 646 cellules — la garde ne
    # refuse rien d'existant, elle ferme la porte avant qu'on la pousse.
    errors.extend(etats_trahis(schema, merged, written=written, gelees=gelees))
    lc = lifecycle_of(schema)
    if lc:
        sf = status_field(schema)
        key = sf.get("key") if sf else None
        # ⚠️ La valeur se DÉBALLE, comme partout ailleurs dans cette validation
        # (#586, 29/08). Trois lignes plus haut dans le chemin d'écriture, la
        # plateforme pose elle-même `<champ>.origine` sur une colonne déclarée
        # `origine: "system"` — la colonne d'état devient
        # `{'valeur': 'enrichi', 'origine': 'a_enrichir'}` — et ce contrôle la lisait
        # BRUTE : il refusait « état inconnu » sur la ligne que la plateforme venait
        # elle-même de compléter. Zéro fiche écrite sur cent, campagne arrêtée.
        # *Deux gestes voisins qui lisent la même colonne doivent la lire pareil* —
        # les contrôles de champ déballaient déjà, chacun après un défaut du même
        # genre (#329 les couches, #347 `required_when`).
        new = unwrap(merged.get(key)) if key else None
        if new is not None:
            states = {str(s) for s in lc.get("states") or []}
            # L'état est-il POSÉ par ce geste ? Même partage que le type et ses
            # quatre voisins (07/09/2026) : « cet état est-il permis » juge une
            # VALEUR, donc ce que l'appel écrit. Un état stocké devenu invalide —
            # parce qu'on l'a retiré de la liste déclarée depuis — gelait sinon la
            # ligne entière, y compris pour une écriture sans rapport, et pour
            # toujours. C'était le SIXIÈME contrôle de la famille, et le seul que
            # personne n'avait rapporté : il ne sortait pas d'un signalement mais
            # d'une vérification faite en cherchant autre chose.
            #
            # La TRANSITION, elle, n'a pas besoin d'être gardée ici : elle ne se
            # juge que si l'état change, donc que si le geste l'écrit.
            pose_etat = written is None or key in written
            if states and str(new) not in states:
                inconnu = f"{key}: état inconnu {new!r} (états: {sorted(states)})"
                if pose_etat:
                    errors.append(inconnu)
                elif gelees is not None:
                    gelees.append({"champ": str(key), "refus": inconnu})
            # L'état PRÉCÉDENT se déballe aussi : dès la deuxième écriture la ligne
            # porte des couches, donc le cas normal est un objet, pas un mot.
            elif prev_status is not None and str(unwrap(prev_status)) != str(new):
                transitions = lc.get("transitions")
                if isinstance(transitions, dict):
                    allowed = {str(t)
                               for t in transitions.get(str(unwrap(prev_status))) or []}
                    if str(new) not in allowed:
                        errors.append(refus_de_transition(
                            str(key), str(unwrap(prev_status)), str(new),
                            sorted(allowed)))
    return errors
