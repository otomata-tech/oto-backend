"""Valider le SCHÉMA lui-même — « ce format est-il posable ? » (ADR 0046).

`validate_schema_def` est le seul point d'entrée : elle rend la liste des raisons pour
lesquelles un schéma est refusé À LA POSE, avant qu'une seule ligne ne soit écrite.
Elle délègue à deux aides qui portent le gros du texte : `_validate_fields_def`
(récursive — types, bornes, motifs, couches, composites) et `_validate_reserved_def`
(les crans `readonly` / `origine` / `agent_access`, et les endroits où ils n'ont pas
le droit de se poser).

**La différence avec `validation.py` est le MOMENT, et il change tout** : ici on juge
un format, une fois, à la pose ; là on juge une ligne, à chaque écriture. Un refus
d'ici est cher (il bloque un geste d'administration et il est lu par un humain) ; un
refus de là est chaud (il est lu par un agent, en boucle). C'est pourquoi les deux ne
partagent pas leurs textes.

**La différence avec `declaration.py` est la QUESTION** : `declaration.py` lit un
schéma qu'il suppose valide ; ici on décide s'il l'est. Ce module appelle donc l'autre,
jamais l'inverse.

Ce qu'il ne tient pas :
- **la liste fermée des attributs** qu'une colonne peut porter → `schema_keys.py` ;
- **le signalement (non bloquant) d'un attribut inconnu** → `vocabulaire.py` et
  `cles_inconnues.py` — le vocabulaire ne se ferme PAS, on signale ;
- **le refus d'une ligne** contre le format ainsi validé → `validation.py` ;
- **la validité du périmètre de réservation** → `claimable.erreurs`, appelée d'ici.
"""
from __future__ import annotations

from typing import Optional

from . import acces_agent as aga
from . import claimable
from . import schema_keys

from .couches import LAYER_KEYS, split_layer, SYSTEM_ORIGIN
from .motifs import PATTERN_MAX_SUBJECT, pattern_refusal
from .declaration import (
    FILE_KEYS,
    COMPOSITE_TYPES,
    DISPLAY_TITLE,
    _fields,
    max_length_of,
    readonly_fields,
    SCALAR_TYPES,
    status_field,
)
from .cycle_de_vie import lifecycle_of, terminal_states
from .hors_schema import UNKNOWN_FIELDS_MODES
from . import formule as _formule

# ── validation de la DÉFINITION du schéma ────────────────────────────────────

def validate_schema_def(schema: Optional[dict]) -> list[str]:
    """Erreurs de structure de la définition elle-même (posée par data_set_schema).
    Un schéma 0016 plat reste valide tel quel."""
    if schema is None:
        return []
    if not isinstance(schema, dict):
        return ["schema doit être un objet {fields:[...]} ou null"]
    errors: list[str] = []
    _validate_fields_def(_fields(schema), "fields", errors)
    errors.extend(_validate_formulas_def(_fields(schema)))
    # Une colonne titre par tableau (#317) : deux candidats, et le nom d'une ligne
    # dépendrait de l'ordre de déclaration — une inférence silencieuse, exactement ce
    # que le retrait des rôles supprime. Zéro conflit en production au moment de la
    # bascule : le refus ne casse personne.
    titres = [str(f.get("key")) for f in _fields(schema)
              if f.get("display") == DISPLAY_TITLE and f.get("key")]
    if len(titres) > 1:
        errors.append(
            f"display=\"title\" déclaré sur {len(titres)} colonnes ({', '.join(titres)}) "
            "— une seule nomme la ligne")
    # Un seul cycle de vie par tableau : le bloc DÉSIGNE la colonne d'état, donc deux
    # blocs feraient dépendre l'état de l'ordre de déclaration — en silence. Même
    # refus que deux `display: "title"`, et pour la même raison.
    files = [str(f.get("key")) for f in _fields(schema)
             if isinstance(f, dict) and isinstance(f.get("lifecycle"), dict)
             and f.get("key")
             and any(k in f["lifecycle"] for k in FILE_KEYS)]
    if len(files) > 1:
        errors.append(
            f"deux colonnes déclarent une FILE de travail ({', '.join(files)}) — "
            f"`claimable`, `max_claims` ou `abandon_state` ne peuvent vivre que sur "
            f"une seule, celle que `data_claim_next` réserve. Plusieurs cycles de vie "
            f"sont permis (une file d'agents et des états humains, par exemple), mais "
            f"une seule file.")
    # Une clé métier n'est JAMAIS un sous-tableau ni un sous-record (oto#22 §4). Elle
    # identifie la ligne : les écritures par lot dédupliquent dessus, et un index
    # d'unicité d'expression la compare. Une liste ne se réduit pas à une valeur —
    # l'unicité porterait sur le TEXTE d'un objet JSON, donc deux listes équivalentes
    # d'ordre différent ne collisionneraient pas. Refusé à la DÉCLARATION plutôt qu'à
    # la première écriture : le tableau serait déjà peuplé de doublons.
    cle = schema.get("key")
    if cle:
        porteur = next((f for f in _fields(schema) if f.get("key") == cle), None)
        if porteur and porteur.get("type") in COMPOSITE_TYPES:
            errors.append(
                f"key=\"{cle}\" désigne un champ de type \"{porteur.get('type')}\" — "
                "une clé métier identifie la ligne, elle doit être une valeur simple "
                "(une liste ne se réduit pas à une valeur, l'unicité serait fausse)")
    # `key_required` DURCIT la clé métier : sans elle, il n'y a plus aucun moyen de
    # désigner une ligne autrement que par son identifiant, et le tableau deviendrait
    # inécrivable pour tout agent qui ne relit pas d'abord. Refusé à la POSE, là où le
    # tableau se déclare — pas à la première écriture d'une campagne déjà lancée (même
    # parti que `max_claims` sans `abandon_state`).
    if schema.get("key_required") and not cle:
        errors.append(
            "key_required exige une clé métier : déclare `key` (la colonne qui "
            "identifie une ligne), sinon aucune écriture ne pourrait viser une ligne "
            "existante et le tableau serait inécrivable")
    # #606 (29/08/2026) : la clé figure dans CHAQUE écriture pour désigner la ligne.
    # `readonly` dessus — identique refusé — fermerait toutes les écritures du tableau,
    # et celui qui « complète » la pose dans six mois ne le saurait pas.
    if cle and cle in readonly_fields(schema):
        errors.append(
            f"`{cle}` est la clé métier : elle se protège par `key_required`, pas par "
            f"`readonly` — une autre valeur est une autre ligne, et la clé figure dans "
            f"chaque écriture pour désigner la sienne")
    # oto#83, troisième fois la même raison : la clé désigne la ligne, et elle figure
    # dans chaque écriture pour ça. La fermer à l'agent — ou pire, la lui cacher —
    # rendrait le tableau inécrivable ET illisible pour lui, sans qu'aucun refus ne
    # nomme la cause : il verrait des lignes sans identité et des écritures qui visent
    # à côté. Le tableau qu'un agent ne doit pas toucher se ferme par le partage, pas
    # colonne par colonne.
    if cle and aga.acces_declare(schema, cle) not in (None, aga.ECRITURE):
        errors.append(
            f"`{cle}` est la clé métier : elle ne se ferme pas par `{aga.CLE}` — un "
            f"agent la lit pour désigner la ligne qu'il écrit, et sans elle il "
            f"écrirait à côté. Ferme les colonnes de SUIVI, pas celle qui identifie ; "
            f"un tableau entier se ferme en ne le partageant pas")
    errors.extend(_erreurs_unknown_fields(schema))
    lc = lifecycle_of(schema)
    if lc is not None:
        states = lc.get("states")
        if not isinstance(states, list) or not states:
            errors.append("lifecycle.states doit être une liste non vide")
        else:
            known = {str(s) for s in states}
        # ⚠️ La FORME de `transitions` se juge AVANT de la parcourir. Elle ne le
        # faisait pas : une chaîne, une liste ou un nombre y produisait un
        # `AttributeError: 'str' object has no attribute 'items'` — une erreur
        # TECHNIQUE, qui n'est pas une `ValueError`, donc que la face REST ne traduit
        # pas : l'appelant recevait un 500 au corps vide sur un schéma qu'il venait
        # d'écrire, et pouvait croire la pose réussie. Même famille que `RowLocked`
        # (#317) : un refus juste qui ne sort pas comme un refus.
        transitions = lc.get("transitions")
        if transitions is not None and not isinstance(transitions, dict):
            errors.append(
                f"lifecycle.transitions doit être un objet "
                f"{{\"état\": [\"états atteignables\"]}} — reçu "
                f"{type(transitions).__name__}. Chaque clé est un état de départ, "
                f"chaque valeur la liste de ceux qu'il peut atteindre.")
            transitions = None
        if isinstance(states, list) and states:
            for frm, tos in (transitions or {}).items():
                if str(frm) not in known:
                    errors.append(f"lifecycle.transitions: état source inconnu {frm!r}")
                for to in tos if isinstance(tos, list) else [tos]:
                    if str(to) not in known:
                        errors.append(f"lifecycle.transitions: état cible inconnu {to!r}")
            for t in lc.get("terminal") or []:
                if str(t) not in known:
                    errors.append(f"lifecycle.terminal: état inconnu {t!r}")
        # Le plafond de reprises (#433) et son état d'abandon vont ENSEMBLE : un
        # plafond sans état où verser la ligne serait une garde qui ne peut pas
        # s'appliquer, et un état non terminal la remettrait dans la file qu'elle
        # vient de quitter. Les deux se refusent à la pose, là où le tableau se
        # déclare — pas au premier claim d'une campagne déjà lancée.
        plafond = lc.get("max_claims")
        if plafond is not None and (isinstance(plafond, bool)
                                    or not isinstance(plafond, int) or plafond < 1):
            errors.append(
                f"lifecycle.max_claims doit être un entier >= 1 (reçu {plafond!r}) — "
                "c'est le nombre de réservations SANS écriture qu'une ligne supporte "
                "avant de quitter la file")
        abandon = lc.get("abandon_state")
        if plafond is not None and abandon is None:
            errors.append(
                "lifecycle.max_claims exige lifecycle.abandon_state — l'état terminal "
                "où verser une ligne réservée N fois sans écriture")
        if abandon is not None and str(abandon) not in terminal_states(schema):
            errors.append(
                f"lifecycle.abandon_state: {abandon!r} n'est pas un état terminal déclaré "
                "(ajoute-le à lifecycle.terminal) — une ligne abandonnée reviendrait "
                "sinon dans la file qu'elle vient de quitter")
        # Le périmètre de réservation (#517) se valide par le moteur de filtre qui le
        # servira — refusé à la pose, comme le plafond : une déclaration illisible
        # au premier claim d'une campagne lancée est le pire moment pour l'apprendre.
        sf = status_field(schema) or {}
        errors.extend(claimable.erreurs(
            lc, declared={f.get("key") for f in _fields(schema)},
            strict=bool(schema.get("strict")), status_key=sf.get("key"),
            states={str(s) for s in (lc.get("states") or [])}
            if isinstance(lc.get("states"), list) else set()))
    # ⚠️ Il y avait ici un refus « lifecycle exige role="status" ». Retiré le
    # 08/09/2026 avec l'étiquette : le bloc DÉSIGNE désormais sa colonne, il n'y a plus
    # de placement à vérifier. Et ce refus n'avait pas protégé — cinq schémas de
    # production portaient un `lifecycle` sur une colonne non étiquetée, stocké, servi
    # et jamais lu, alors qu'il existait. Ce qui les arrête maintenant est plus haut :
    # deux blocs sont refusés, et un bloc seul EST l'état.
    return errors


def _erreurs_unknown_fields(schema: dict) -> list[str]:
    """`unknown_fields` : la valeur, et les deux façons dont le cran serait INERTE.

    Un cran inerte n'est pas neutre — il est pire que son absence, parce qu'on
    cesse de surveiller ce qu'on croit gardé. C'est le défaut même que #614
    rapporte sur `strict` (« une option qui promet plus qu'elle ne fait »), et il
    serait grotesque de le refaire en le corrigeant. D'où deux refus à la POSE,
    devant celui qui peut encore choisir :

    - **sans `strict`** : `off_schema_keys` ne relève RIEN hors mode strict (un
      champ libre y est un droit explicite du contrat) — le cran ne pourrait
      jamais parler ;
    - **sans aucun champ déclaré** : sans référentiel, TOUT serait hors schéma.
      Le cran parlerait alors sur chaque écriture, et le tableau deviendrait
      inécrivable d'un coup. Les deux extrêmes du même trou."""
    mode = schema.get("unknown_fields")
    if mode is None:
        return []
    if mode not in UNKNOWN_FIELDS_MODES:
        return [f"unknown_fields: valeurs possibles \"report\" (défaut — la colonne "
                f"non déclarée est créée et SIGNALÉE dans `hors_schema`) et "
                f"\"reject\" (elle est refusée, rien n'est écrit) ; reçu {mode!r}"]
    if mode != "reject":
        return []
    errs = []
    if not schema.get("strict"):
        errs.append(
            "unknown_fields: \"reject\" exige `strict: true` — hors mode strict, une "
            "colonne libre est un droit du contrat et rien ne la relève : le cran ne "
            "refuserait jamais rien, tout en annonçant le contraire")
    if not _fields(schema):
        errs.append(
            "unknown_fields: \"reject\" exige au moins une colonne déclarée — sans "
            "référentiel, TOUTE colonne est hors schéma et le tableau devient "
            "inécrivable dès la pose. Déclare le format d'abord, ferme-le ensuite")
    return errs


# Ce qu'une COLONNE seule peut déclarer — donc ce qu'une cible de couche ne peut pas.
# Chacune désigne la colonne en tant que telle : nommer la ligne (`display`), porter
# son statut (`role`), se subdiviser (`fields`/`of`), dire à qui elle est servie
# (`agent_access`). Posées sur une couche, elles ne seraient lues nulle part — la
# forme acceptée-inerte que #347 a fermée.
# DÉRIVÉ de la déclaration unique des attributs (`schema_keys`), jamais recopié : le
# validateur est le PREMIER client de cette liste, l'avertissement sur les clés
# inconnues le second. Une liste parallèle mentirait au premier attribut ajouté.
_COLUMN_ONLY_KEYS = schema_keys.COLONNE_SEULEMENT


def _validate_reserved_def(f: dict, fpath: str, errors: list[str], *,
                           top: bool) -> None:
    """#586/#606 : un cran qui ne peut pas s'appliquer se refuse à la POSE, devant
    celui qui peut corriger — jamais accepté-inerte (#347). `None` passe : c'est
    la forme par laquelle un patch LÈVE le cran sans réécrire le schéma."""
    ro, so, ftype = f.get("readonly"), f.get("origine"), f.get("type")
    if ro is not None and not isinstance(ro, bool):
        errors.append(
            f"{fpath}: readonly doit être true ou false (reçu {ro!r}) — `true` = "
            f"colonne du fichier source, dont la valeur ne change pas par une "
            f"écriture (ses couches `comment`/`link` restent ouvertes)")
    # ⚠️ **`origine` n'est PLUS validée ici, et son absence est le correctif.**
    #
    # Ces deux refus décrivaient la CAPTURE : « la couche est alors posée par la
    # plateforme, à partir de la valeur en place ». Le cran `origine: "system"` a été
    # supprimé le 08/09/2026 au profit de `donnees_d_origine`, déclaré à l'import — et
    # plus rien ne pose cette couche. Les refus survivaient donc à leur raison : ils
    # exigeaient la bonne forme d'un attribut devenu inerte, et leur MOTIF promettait
    # un mécanisme mort. C'est exactement ce qu'un des deux commentaires ci-dessous
    # nommait, un mois plus tôt, sans se l'appliquer : *un motif qui survit à sa raison
    # est un mensonge en attente.*
    #
    # La clé n'est pas refusée pour autant — 500 colonnes de production la portent, et
    # un refus à la pose gèlerait dix tableaux vivants. Elle est déclarée **non
    # appliquée** dans `schema_keys`, ce qui la fait nommer par l'avertissement des
    # clés que la plateforme ne lit pas. Dire « je ne m'en sers pas » est le seul geste
    # honnête pour un attribut qu'on ne peut ni appliquer ni retirer.

    # oto#83 — quatrième cran de la famille : à QUI la colonne est servie.
    # ⚠️ La valeur inconnue est REFUSÉE, et c'est le point du cran. Le vocabulaire des
    # CLÉS reste ouvert (on signale, on n'empêche pas) ; celui d'une VALEUR que le
    # validateur exécute ne peut pas l'être : `agent_access: "non"` retomberait en
    # silence sur le défaut « write », et le propriétaire croirait sa colonne fermée
    # alors qu'elle est grande ouverte. C'est mot pour mot la plaie de `read_only`
    # écrit pour `readonly`, à ceci près qu'ici on peut la fermer.
    aa = f.get(aga.CLE)
    if aa is not None and aa not in aga.VALEURS:
        errors.append(
            f"{fpath}: {aga.CLE}: valeur inconnue {aa!r} — les seules sont "
            + ", ".join(repr(v) for v in aga.VALEURS)
            + f" ({aga.ECRITURE!r} = le défaut ; {aga.LECTURE!r} = un agent la voit "
            f"mais n'en écrit pas la valeur ; {aga.AUCUN!r} = un agent ne la voit pas "
            f"du tout). Une valeur que la plateforme ne sait pas lire laisserait la "
            f"colonne ouverte sous un réglage qui promet le contraire")
    if not top and aa is not None:
        errors.append(
            f"{fpath}: {aga.CLE} ne se pose qu'au premier niveau — sous un sous-record "
            f"ni le masquage ni le refus ne le lisent, et une déclaration que rien ne "
            f"lit n'est pas inerte, elle ment")
    if not top and (ro is True or so == SYSTEM_ORIGIN):
        errors.append(
            f"{fpath}: readonly / origine: \"{SYSTEM_ORIGIN}\" ne se posent qu'au "
            f"premier niveau — sous un sous-record la garde ne les lit pas, et une "
            f"déclaration que rien ne lit n'est pas inerte, elle ment")


def _validate_fields_def(fields: list, path: str, errors: list[str]) -> None:
    for f in fields:
        key = f.get("key")
        fpath = f"{path}.{key or '?'}"
        if not isinstance(key, str) or not key:
            errors.append(f"{fpath}: key manquante")
            continue
        # Cible de COUCHE (#377) : `qualification.comment` contraint la couche du
        # même nom SUR la colonne `qualification` — ce n'est pas une colonne de
        # plus. Toute autre forme pointée se refuse ICI plutôt que d'être stockée :
        # elle ne désignerait rien, et une contrainte qui ne désigne rien n'est pas
        # inerte, elle est INSATISFIABLE — c'est le défaut de #377, où la pose
        # passait et toute écriture déclenchante était ensuite refusée, y compris
        # celle qui portait bien la justification.
        base, layer = split_layer(key)
        if layer:
            if not any(str(x.get("key") or "") == base
                       for x in fields if isinstance(x, dict)):
                errors.append(
                    f"{fpath}: la couche `{layer}` porte sur la colonne `{base}`, "
                    f"qui n'est pas déclarée ici — déclare-la, ou corrige le nom. "
                    f"Une contrainte sur une colonne absente ne pourrait jamais "
                    f"être satisfaite.")
            interdites = [k for k in _COLUMN_ONLY_KEYS if k in f]
            if interdites:
                errors.append(
                    f"{fpath}: {', '.join(repr(k) for k in interdites)} ne se "
                    f"déclare que sur une COLONNE, pas sur une couche — une couche "
                    f"ne nomme pas la ligne, ne porte pas son statut et ne se "
                    f"subdivise pas. Sur `{key}` ces clés ne seraient lues nulle "
                    f"part : déplace-les sur `{base}`.")
        elif "." in key:
            errors.append(
                f"{fpath}: `{key}` n'est pas un nom de colonne — un point ne "
                f"désigne qu'une couche, et les couches sont "
                f"{', '.join(LAYER_KEYS)}. La valeur, elle, se désigne par le nom "
                f"NU (`{key.rpartition('.')[0]}`). Une colonne littérale portant un "
                f"point serait invisible au filtre et au tri du même nom.")
        ftype = f.get("type")
        if ftype is not None and ftype not in SCALAR_TYPES + COMPOSITE_TYPES:
            errors.append(f"{fpath}: type inconnu {ftype!r}")
        if ftype == "object":
            sub = f.get("fields")
            if not isinstance(sub, list) or not sub:
                errors.append(f"{fpath}: type=object exige fields:[...]")
            else:
                _validate_fields_def([x for x in sub if isinstance(x, dict)],
                                     fpath, errors)
        if ftype == "list":
            of = f.get("of")
            if of is None:
                errors.append(f"{fpath}: type=list exige of:<field-def>")
            elif isinstance(of, dict):
                if isinstance(of.get("fields"), list):
                    _validate_fields_def(
                        [x for x in of["fields"] if isinstance(x, dict)], fpath, errors)
                elif of.get("type") is not None and \
                        of["type"] not in SCALAR_TYPES + COMPOSITE_TYPES:
                    errors.append(f"{fpath}.of: type inconnu {of.get('type')!r}")
            else:
                errors.append(f"{fpath}: of doit être un objet field-def")
        rw = f.get("required_when")
        if rw is not None and (not isinstance(rw, dict) or not rw):
            errors.append(f"{fpath}: required_when doit être un objet {{champ: valeur}}")
        elif isinstance(rw, dict):
            # La règle de la famille #329/#331 : une forme non interprétée se
            # REFUSE à la pose en nommant l'attendu — jamais stockée-inerte
            # (vécu #347 : une condition en liste était acceptée et désarmait
            # la contrainte pour TOUTES les valeurs, scalaires comprises).
            for ck, cv in rw.items():
                ok_scalaire = isinstance(cv, (str, int, float, bool))
                ok_liste = (isinstance(cv, (list, tuple)) and len(cv) > 0
                            and all(isinstance(x, (str, int, float, bool)) for x in cv))
                if not (ok_scalaire or ok_liste):
                    errors.append(
                        f"{fpath}: required_when — la condition de `{ck}` doit être "
                        f"une valeur ou une liste non vide de valeurs (requis quand "
                        f"la valeur du champ est / est parmi) ; reçu {cv!r}")
        # oto#75 : MÊME règle de famille (#329/#331/#347) — une forme non
        # interprétée se REFUSE devant celui qui la pose. Une couche mal
        # orthographiée (`commentaire`) serait stockée sans rien exiger, et
        # l'attribut a déjà vécu trois schémas de production dans cet état.
        # ⚠️ **La liste VIDE est ACCEPTÉE, et ce n'est pas une tolérance.** À
        # l'écriture, `required_layers_of` lit déjà `[]` comme « aucune couche
        # exigée » — la refuser ICI serait une asymétrie entre les deux moitiés du
        # même attribut. Elle a un coût mesuré : quatre tableaux VIVANTS portent
        # `required_layers: []`, dont deux chez une organisation cliente ; les
        # refuser rendrait leur schéma inpatchable — y compris pour un patch portant
        # sur une TOUT AUTRE colonne, puisque `patch_schema` repasse le schéma
        # FUSIONNÉ par `set_schema` (`schema_ops.py`). Un geste qui marchait aurait
        # cessé de marcher sur un schéma que personne n'a modifié.
        rl = f.get("required_layers")
        if rl is not None and not (
                isinstance(rl, (list, tuple))
                and all(isinstance(c, str) and c in LAYER_KEYS for c in rl)):
            errors.append(
                f"{fpath}: required_layers doit être une liste de couches parmi "
                f"{', '.join(repr(c) for c in LAYER_KEYS)} — la liste vide "
                f"n'exige rien et reste acceptée ; reçu {rl!r} — une couche que la "
                f"plateforme ne connaît pas n'exigerait rien, et son auteur croirait "
                f"la provenance exigée")
        ml = f.get("max_length")
        if ml is not None:
            if isinstance(ml, bool) or not isinstance(ml, int) or ml <= 0:
                errors.append(f"{fpath}: max_length doit être un entier > 0, reçu {ml!r}")
            elif ftype in COMPOSITE_TYPES:
                errors.append(
                    f"{fpath}: max_length ne borne qu'un champ scalaire "
                    f"(type={ftype} — borne le sous-champ concerné)")
        # #387 : le motif se refuse ICI, devant celui qui le pose — jamais à
        # l'écriture d'une ligne trois semaines plus tard. Un motif fautif accepté
        # puis inerte est le pire des deux mondes : son auteur croit avoir posé un
        # contrat. Trois refus, chacun nommant sa raison.
        motif = f.get("pattern")
        if motif is not None:
            bornes = max_length_of(f)
            if not isinstance(motif, str) or not motif:
                errors.append(
                    f"{fpath}: pattern doit être une expression régulière (une "
                    f"chaîne non vide), reçu {motif!r}")
            elif ftype in COMPOSITE_TYPES:
                errors.append(
                    f"{fpath}: pattern ne contraint qu'un champ scalaire "
                    f"(type={ftype} — pose-le sur le sous-champ concerné)")
            elif not bornes:
                # La borne n'est pas un confort : c'est elle qui rend le coût du
                # motif majorable. Sans sujet borné, aucune garantie — et le motif
                # tourne dans la boucle UNIQUE du serveur, à chaque écriture.
                errors.append(
                    f"{fpath}: pattern exige max_length sur le même champ — le coût "
                    f"d'un motif se majore contre la longueur de ce qu'il lit, et "
                    f"oto n'exécute pas ce dont elle ne sait pas majorer le prix "
                    f"(borne le champ, puis repose le motif)")
            elif bornes > PATTERN_MAX_SUBJECT:
                errors.append(
                    f"{fpath}: pattern sur un champ borné à {bornes} caractères — "
                    f"maximum {PATTERN_MAX_SUBJECT} : au-delà, contraindre la FORME "
                    f"d'une valeur n'a plus de sens et son coût n'est plus majorable")
            else:
                raison = pattern_refusal(motif, bornes)
                if raison:
                    errors.append(f"{fpath}: pattern {motif!r} refusé — {raison}")
        # #586/#606 : les champs que l'appelant n'écrit pas. Sur une cible de couche,
        # `_COLUMN_ONLY_KEYS` a déjà parlé.
        if not layer:
            _validate_reserved_def(f, fpath, errors, top=(path == "fields"))


# ── colonnes calculées (oto-backend#1008) ───────────────────────────────────

def _validate_formulas_def(fields: list) -> list[str]:
    """Une colonne `type: "formula"` DOIT porter un texte `formula` qui PARSE
    (fonction du sous-ensemble fermé, grammaire correcte), ne référence QUE des
    colonnes déclarées au premier niveau, et ne chaîne PAS sur une autre colonne
    formule (v1). Refusé À LA POSE, jamais stocké-invalide."""
    errors: list[str] = []
    top_level = [f for f in fields if isinstance(f, dict)]
    colonnes = {f["key"] for f in top_level
                if isinstance(f.get("key"), str) and f["key"]}
    formules = {f["key"] for f in top_level
                if f.get("type") == "formula" and isinstance(f.get("key"), str)
                and f["key"]}
    for f in top_level:
        if f.get("type") != "formula":
            continue
        key = f.get("key")
        fpath = f"fields.{key or '?'}"
        texte = f.get("formula")
        if not isinstance(texte, str) or not texte.strip():
            errors.append(
                f"{fpath}: type=\"formula\" exige `formula` (texte OpenFormula "
                f"non vide)")
            continue
        try:
            _formule.valider(texte, colonnes, formules - {key}, champs_def=top_level)
        except _formule.FormulaError as e:
            errors.append(f"{fpath}: formule refusée — {e}")
    return errors
