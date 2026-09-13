"""Les clés d'origine d'un nœud, et la surface où il s'écrit, dérivées en UN seul endroit.

Un nœud issu de la conversion garde sa source dans `props.legacy` / `props.legacy_id`
(`db/nodes.py`) : une page est un `doc`, un projet un `prj`. Deux surfaces servent
cette clé — la fiche (`node_view`) et le rail (`shell`) — et la règle ne doit donc
vivre dans aucune des deux.

La SURFACE D'ÉDITION (`edit_surface`, oto#198) suit le même régime, avec un lecteur de
plus : la garde d'écriture de `node_edit`. La fiche annonce où un nœud s'écrit, la garde
refuse ce qui ne s'écrit pas ici, et c'est la MÊME fonction qui répond aux deux — une
annonce et un refus écrits séparément finiraient par se contredire, et le client
croirait la fiche.

⚠️ **Pourquoi un module à part plutôt qu'un import de l'une vers l'autre** : les deux
DÉCLARENT des capacités, et l'ordre d'enregistrement des routes REST est un contrat
figé (Starlette sert le premier match). Faire importer l'une par l'autre réordonne la
table sans rapport avec le sujet. Ce module n'enregistre rien : il peut être importé
de partout sans déplacer une route.

Il ne porte que de la dérivation pure — aucun accès base, aucun modèle servi.
"""
from __future__ import annotations

from typing import Literal, Mapping, Optional


def doc_id_de(legacy: Optional[str], legacy_id) -> Optional[int]:
    """La poignée `doc_id` d'un nœud, à partir de sa seule clé legacy.

    ⚠️ **Écrite ICI et nulle part ailleurs.** La règle du dépôt vaut pour une
    dérivation comme pour un prédicat SQL : deux endroits qui l'écrivent finissent par
    diverger, et celui qui se trompe ne le montre pas — il rend l'entier d'une autre
    page, qui s'ouvre sans erreur.

    `None` dès que la source n'est pas une page : un projet, un tableau natif ou une
    procédure n'ont pas de document derrière eux, et deviner en fabriquerait un faux.
    La colonne SQL rend du texte, d'où la conversion.
    """
    return int(legacy_id) if legacy == "doc" and legacy_id is not None else None


# ── La surface d'édition (oto#198) ─────────────────────────────────────────────

EditSurface = Literal["node", "doc", "project", "procedure", "datastore", "guide"]

# La famille de conversion → la surface qui fait autorité sur son contenu. FERMÉ : ce
# sont les cinq constantes que posent les requêtes de conversion (`db/nodes._FAMILY_*`),
# et aucune autre écriture ne pose `legacy`. Une ligne s'écrit là où s'écrit son tableau.
_SURFACE_PAR_FAMILLE: dict[str, str] = {
    "doc": "doc", "prj": "project", "prc": "procedure", "tbl": "datastore",
    "row": "datastore",
}

# La poignée que chaque surface EXIGE sur la fiche servie — celle qu'un client repasse à
# la surface annoncée. `guide` n'en a pas : la fiche ne sert ni slug ni livraison, et
# l'écran n'offre donc pas l'édition d'un guide depuis la fiche.
POIGNEE_PAR_SURFACE: dict[str, Optional[str]] = {
    "node": "id", "doc": "doc_id", "project": "project_id",
    "procedure": "procedure", "datastore": "datastore", "guide": None,
}


class NoeudIncoherent(Exception):
    """Le stockage d'un nœud contredit le modèle : il ne peut pas dire où il s'écrit.

    LEVÉE, jamais servie en l'état : une fiche qui annoncerait une surface sans la
    poignée pour l'atteindre ferait deviner le client — exactement ce que `edit_surface`
    retire. Le lecteur la traduit en refus nommé (`noeud_incoherent`), pas en repli.
    """


def edit_surface_de(props: Optional[Mapping]) -> str:
    """La surface CANONIQUE où ce nœud s'écrit. Pas une permission : écrire reste jugé
    par la garde de propriété, et un non-propriétaire lit `node` puis reçoit 404.

    - `legacy` présent : la surface d'origine de sa famille ; famille inconnue → incohérent ;
    - `delivery` présent, QUELLE QUE SOIT sa valeur : une couche de contexte (`guide`).
      C'est la règle de stockage déjà écrite (`db/nodes.py` : « un nœud natif ne porte
      JAMAIS `delivery` »). L'ancienne table `guides` n'avait aucune contrainte CHECK sur
      cette valeur : l'exiger connue ferait tomber des fiches sans rien protéger ;
    - les deux ensemble : incohérent, aucune écriture ne pose les deux ;
    - ni l'un ni l'autre : un nœud né ici, `node`.

    ⚠️ C'est la PRÉSENCE des clés qui compte, pas leur vérité : un `legacy` vide n'est
    pas un nœud natif, c'est une donnée à expliquer — et la garde SQL des écritures
    (`props->>'legacy' IS NULL`) le refuserait de toute façon.
    """
    p = props or {}
    guide = "delivery" in p
    if "legacy" in p:
        if guide:
            raise NoeudIncoherent("il porte à la fois `legacy` et `delivery`")
        famille = p["legacy"]
        surface = _SURFACE_PAR_FAMILLE.get(famille) if isinstance(famille, str) else None
        if surface is None:
            raise NoeudIncoherent(f"sa famille d'origine `{famille}` est inconnue")
        return surface
    return "guide" if guide else "node"


def exiger_poignee(surface: str, corps: Mapping) -> None:
    """La poignée que `surface` exige est-elle dans le corps servi ? Sinon, incohérent.

    Par construction, chaque poignée vient d'une colonne source non nulle et plus rien ne
    réécrit les nœuds convertis depuis l'arrêt de la recopie (01/09/2026). **Un cas reste
    POSSIBLE et n'a pas été mesuré** : un tableau au nom vide — `user_datastores.namespace`
    est NOT NULL, pas non vide, et aucune validation lue ne refuse la chaîne vide. Il tombe
    ici avec les autres, plutôt que de servir `datastore: null` sur un tableau.
    """
    cle = POIGNEE_PAR_SURFACE[surface]
    if cle is not None and corps.get(cle) in (None, ""):
        raise NoeudIncoherent(f"sa surface `{surface}` exige `{cle}`, que rien ne porte")


def refus_guide(owner_type: str, owner_id: str, props: Mapping) -> tuple[str, dict]:
    """Le message et les `details` du refus d'écrire une couche de contexte ici.

    Nommer la DESTINATION est tout l'objet : un refus qui dit seulement « non » fait
    tourner un agent en rond. Ce texte ne se sert qu'APRÈS la garde de propriété —
    l'appelant possède ce guide, lui dire son scope et son slug ne révèle rien.

    Trois adresses, dictées par `oto_guide` (`capabilities/guides._init_ref`) : un guide à
    la demande par son slug ; le readme injecté d'une org, d'une équipe ou d'une personne
    par son seul scope, le slug y étant canonique ; celui de la plateforme par son slug,
    qui y désigne le bloc.
    """
    scope, slug, delivery = str(owner_type), props.get("slug"), props.get("delivery")
    cible = f", owner_id={owner_id}" if scope in ("org", "group") else ""
    details = {"edit_surface": "guide", "scope": scope, "slug": slug, "delivery": delivery}
    if scope in ("org", "group"):
        details["owner_id"] = str(owner_id)
    if delivery == "init":
        adresse = f", slug={slug}" if scope == "platform" else f"{cible}, sans slug"
        message = ("Ce nœud est le readme injecté au démarrage de session : il s'édite par "
                   f"`oto_guide` (op=write, scope={scope}, delivery=init{adresse} ; un "
                   "body_md vide le retire), pas par `oto_node_edit`.")
    else:
        message = ("Ce nœud est un guide : il s'édite par `oto_guide` (op=write pour le "
                   f"corps, op=delete pour le retirer ; scope={scope}, slug={slug}{cible}), "
                   "pas par `oto_node_edit`.")
    return message, details
