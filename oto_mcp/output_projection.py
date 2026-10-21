"""Projection des SORTIES d'outils — le seam générique, la spécificité reste au module.

Aucun outil n'offrait de mode compact sur ce qu'il RENVOIE. Mesuré le 10/08/2026 sur une
conversation d'enrichissement réelle (agent Mistral, 7 appels) : `prompt_tokens=784`,
`total=8203` — **83 % du coût, ce sont les retours d'outils**, et un run a atteint
`finish_reason: error` à 27 % d'occupation de fenêtre. Le pilote avait dû écrire une
fonction de réduction maison par outil, invisible et refaite à sa façon par chaque
consommateur (oto-core#36).

**Ce qui est retiré par DÉFAUT vs sur demande — la ligne a bougé le 11/08/2026 :**

- **Duplication pure** (le même contenu servi deux fois) → retiré par défaut. Personne
  n'a besoin du texte brut ET du markdown de la même page ; il n'y a rien à perdre.
- **Détail moins utilisé** (knowledge graph, sitelinks, sources de vérification) → aussi
  retiré par défaut désormais, et rendu sur **`full=True`**. C'était l'inverse (opt-in
  `compact=True`), par prudence héritée d'oto-core#37 : `fr_get` projetait sur une
  ALLOWLIST et a laissé tomber `liste_idcc` en silence — un champ « IDCC vérifié » resté
  vide sur 500 lignes. Cette leçon vaut pour une allowlist, pas pour une **denylist de
  clés nommées** : celle-ci ne peut pas faire disparaître un champ imprévu, seulement
  ceux qu'on a écrits. Et l'opt-in ne servait personne — mesuré, aucun agent branché en
  direct ne passait `compact` : il ne peut pas savoir qu'il existe avant d'avoir lu le
  schéma, et le guide qui le pilote ne nomme aucun outil par choix. **Une économie
  qu'il faut connaître pour en bénéficier ne bénéficie à personne** : le défaut servait
  le cas rare et faisait payer le cas général.

Le nom porte l'intention (ADR 0047 §Amendement) : `full=True` dit ce qu'on obtient, là où
`compact=False` se lisait comme une double négation.

Le module ne connaît AUCUN outil : chaque connecteur déclare ce qu'il coupe, là où il
sait ce que ses champs valent.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def project(payload: Any, *, drop: Iterable[str] = (), items_path: Optional[str] = None,
            item_drop: Iterable[str] = (), fields: Optional[Iterable[str]] = None) -> Any:
    """Copie allégée d'un payload d'outil. Pur, non destructif, tolérant à la forme.

    `drop` = clés de premier niveau retirées. `items_path` = chemin pointé vers la LISTE
    de résultats (`organic`, `data.emails`) ; dans chacun de ses éléments, `item_drop`
    retire des clés et `fields`, s'il est fourni, ne garde QUE celles-là.

    Tolérant par construction : une clé absente, un chemin qui ne mène nulle part ou un
    payload d'une autre forme passent sans erreur. Une API tierce change de forme sans
    prévenir — une projection qui lève transformerait une réponse utile en panne, et
    c'est le connecteur qui porterait le blâme.

    `fields` ne filtre QUE les éléments de la liste, jamais l'enveloppe : `credits`,
    `total`, le curseur de pagination restent — sans eux l'agent croit avoir tout vu."""
    if not isinstance(payload, dict):
        return payload
    out = {k: v for k, v in payload.items() if k not in set(drop)}
    if not items_path:
        return out
    # Descente en RECOPIANT chaque niveau : `out` est une copie de surface, écrire dans
    # un sous-dict partagé muterait le payload d'appel (et le cache qui le détient).
    parts, node = items_path.split("."), out
    for p in parts[:-1]:
        child = node.get(p)
        if not isinstance(child, dict):
            return out
        copy = dict(child)
        node[p] = copy
        node = copy
    rows = node.get(parts[-1])
    if not isinstance(rows, list):
        return out
    keep, item_drop = (set(fields) if fields else None), set(item_drop)
    node[parts[-1]] = [
        row if not isinstance(row, dict) else
        ({k: v for k, v in row.items() if k in keep} if keep is not None
         else {k: v for k, v in row.items() if k not in item_drop})
        for row in rows
    ]
    return out


def _rows_at(payload: Any, items_path: str) -> list:
    """La liste au bout de `items_path`, ou `[]` si le chemin ne mène à aucune liste."""
    node = payload
    for p in items_path.split("."):
        if not isinstance(node, dict):
            return []
        node = node.get(p)
    return node if isinstance(node, list) else []


def missing_fields(payload: Any, *, items_paths: Iterable[str],
                   fields: Iterable[str]) -> list[str]:
    """Les clés de `fields` qu'AUCUNE ligne des listes `items_paths` ne porte.

    `project(fields=…)` garde ce qui existe et tait le reste : une clé demandée que
    l'endpoint ne rend jamais disparaît sans un mot, et l'appelant écrit une colonne
    vide en aval (oto#174, `apollo_search_organizations` : `industry`,
    `estimated_num_employees`, `country` demandés, jamais rendus). Cette fonction
    rend ce silence nommable ; au connecteur de dire où trouver la donnée.

    Pur. Ordre de la demande conservé. Une page SANS ligne ne prouve l'absence de
    rien : elle rend `[]` — nommer « absentes » toutes les clés d'une page vide
    ferait passer un résultat vide pour un défaut de projection."""
    rows = [r for path in items_paths for r in _rows_at(payload, path)
            if isinstance(r, dict)]
    if not rows:
        return []
    return [f for f in dict.fromkeys(fields) if not any(f in r for r in rows)]


RAW = "*"  # `fields=["*"]` — le chemin vers le brut, même jeton que data_rows/le feed


_HINT_TRI = (f'Triage view. Bodies and columns left out: `fields=["{RAW}"]` '
             "returns the whole row, `fields=[…]` picks.")


def summarize(rows: list[dict], *, body_fields: Iterable[str],
              fields: Optional[Iterable[str]] = None,
              always: Iterable[str] = (),
              hint: Optional[str] = None) -> tuple[list[dict], Optional[dict]]:
    """Vue de LISTE : un corps devient sa TAILLE, jamais un extrait.

    Une liste est une étape de NAVIGATION — elle sert à décider quoi ouvrir ensuite,
    donc à adresser, trier et écarter sans se tromper. Le contenu se demande dans un
    second temps. Rendre le corps de chaque élément fait dépasser le plafond d'un tool
    result (mesuré : 201 K caractères pour 37 pages), et le client doit alors déverser
    en fichier puis reparser — un agent sans shell, lui, cale simplement.

    ⚠️ **Projeter ≠ tronquer.** On retire des COLONNES (réversible, et le retour dit
    lesquelles) ; on ne coupe pas un texte à N caractères. Un extrait arbitraire tombe
    pile avant ce qui départage deux éléments et l'agent croit avoir lu — mesuré le
    11/08 sur un feed coupé à 600 c., deux cas limites sur cinq tranchés à l'aveugle.
    D'où `<champ>_length` : l'agent sait ce qu'il n'a PAS.

    `body_fields` = les colonnes-corps. `fields` : omis → vue de tri ; `["*"]` → le
    brut ; `[…]` → exactement ces clés, plus `always` (de quoi ADRESSER l'élément
    ensuite — sans quoi la liste rendrait des lignes inutilisables).

    `hint` remplace la phrase servie dans la notice. Le seam ne sert pas que des LISTES :
    la même projection s'applique à une page unique — lecture projetée, et surtout ACCUSÉ
    d'écriture (`oto_doc` op=update/patch, signaux #506/#530). Dire « vue de tri » à un
    agent qui vient d'écrire serait faux, et une notice fausse est pire qu'absente : elle
    est lue avec confiance. C'est un paramètre plutôt qu'une seconde fonction — deux
    façons de projeter divergeraient (leçon `fr_get`, cf. l'en-tête du module).

    Rend `(rows, notice)` — `notice` NOMME ce qui a été écarté et le chemin vers le
    brut ; `None` quand rien ne l'a été."""
    bodies = list(body_fields)
    wanted = None if fields is None else list(fields)
    if wanted is not None and RAW in wanted:
        return rows, None
    keep = None if wanted is None else (set(wanted) | set(always))
    dropped: set[str] = set()
    out: list[dict] = []
    for row in rows:
        item = {k: v for k, v in row.items() if keep is None or k in keep}
        for b in bodies:
            if keep is not None and b in keep:
                continue
            if b in row:
                item.pop(b, None)
                dropped.add(b)
            # La taille est rendue même quand la colonne est absente de la ligne :
            # « 0 » et « pas de corps » se lisent pareil pour qui trie.
            item[f"{b}_length"] = len(row.get(b) or "")
        out.append(item)
    if keep is not None:
        dropped |= {k for row in rows for k in row if k not in keep}
    if not dropped:
        return out, None
    return out, {"omitted": sorted(dropped), "hint": hint or _HINT_TRI}


_HINT_SELECTION = (f'Fields kept by `fields`. `fields=["{RAW}"]` returns the whole '
                   "response.")
_HINT_AMBIGU = ("Several result lists: nothing was filtered. Call the tool directly, "
                "or filter on your side.")


def _est_liste_de_lignes(valeur: Any) -> bool:
    """Une liste VIDE compte : `{"results": [], "total": 0}` doit garder sa forme,
    pas être lu comme un objet unique dont on trierait les clés."""
    return isinstance(valeur, list) and (
        not valeur or any(isinstance(v, dict) for v in valeur))


def _chemins_de_lignes(payload: dict) -> list[str]:
    """Les chemins pointés des listes de lignes, sur deux niveaux au plus — la
    profondeur où les API rangent leurs résultats (`organic`, `data.emails`)."""
    chemins = []
    for cle, valeur in payload.items():
        if _est_liste_de_lignes(valeur):
            chemins.append(cle)
        elif isinstance(valeur, dict):
            chemins += [f"{cle}.{sous}" for sous, v in valeur.items()
                        if _est_liste_de_lignes(v)]
    return chemins


def select_fields(payload: Any, fields: Iterable[str]) -> tuple[Any, Optional[dict]]:
    """Le payload réduit aux `fields` demandés par l'APPELANT, quel que soit l'outil
    (`oto_call fields=`). Pur, non destructif.

    Où porte le tri — sans rien connaître de l'outil :
    - une liste de lignes, au sommet ou à deux niveaux au plus : chaque ligne ne garde
      que `fields`, l'enveloppe (`total`, curseur, crédits) reste, comme `project` ;
    - aucune liste : un objet unique, dont on garde les seules clés demandées ;
    - PLUSIEURS listes : rien n'est trié. Choisir l'une au hasard ferait disparaître
      l'autre sans un mot — la notice nomme les chemins.

    Rend `(payload, notice)`. La notice NOMME ce qui a été écarté (`omitted`) et ce qui
    était demandé mais absent partout (`missing`, leçon oto#174) ; `None` quand rien
    n'a bougé ou que `["*"]` demande le brut."""
    voulus = list(dict.fromkeys(fields))
    if RAW in voulus:
        return payload, None
    garde = set(voulus)
    if isinstance(payload, list):
        lignes = [r for r in payload if isinstance(r, dict)]
        out = [{k: v for k, v in r.items() if k in garde} if isinstance(r, dict) else r
               for r in payload]
        ecartes = {k for r in lignes for k in r if k not in garde}
        manquants = [f for f in voulus if lignes and not any(f in r for r in lignes)]
        return out, _notice(ecartes, manquants)
    if not isinstance(payload, dict):
        return payload, None
    chemins = _chemins_de_lignes(payload)
    if len(chemins) > 1:
        return payload, {"ambiguous": chemins, "hint": _HINT_AMBIGU}
    if chemins:
        chemin = chemins[0]
        lignes = [r for r in _rows_at(payload, chemin) if isinstance(r, dict)]
        ecartes = {k for r in lignes for k in r if k not in garde}
        manquants = missing_fields(payload, items_paths=[chemin], fields=voulus)
        notice = _notice(ecartes, manquants)
        if notice is not None:
            notice = {"items_path": chemin, **notice}
        return project(payload, items_path=chemin, fields=voulus), notice
    out = {k: v for k, v in payload.items() if k in garde}
    manquants = [f for f in voulus if f not in payload]
    return out, _notice(set(payload) - garde, manquants)


def _notice(ecartes: set[str], manquants: list[str]) -> Optional[dict]:
    if not ecartes and not manquants:
        return None
    notice: dict = {}
    if ecartes:
        notice["omitted"] = sorted(ecartes)
    if manquants:
        notice["missing"] = manquants
    notice["hint"] = _HINT_SELECTION
    return notice
