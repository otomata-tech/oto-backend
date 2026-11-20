"""Ce que les capacités datastore partagent — aucun descripteur ici.

Deux choses seulement, et les deux existaient déjà dans les routes écrites à la main
qu'elles remplacent (`api/datastore.py`) : le 404 qui dit OÙ vit un tableau, et
la garde de gouvernance d'un datastore. Les recopier dans chaque module de capacité
aurait fait diverger le message d'erreur d'un chemin à l'autre — c'est exactement le
drift que la couche capacité combat.

⚠️ **Horodatages : sans fuseau.** Toute date rendue par le datastore traverse
`db/_conn.py::_normalize_value`, qui fait `replace(tzinfo=None)` puis
`isoformat(sep=" ")` → `"YYYY-MM-DD HH:MM:SS"`. L'offset est **retiré sans
conversion** (forme héritée de SQLite, que le dashboard consomme telle quelle) : ces
chaînes ne sont donc PAS de l'ISO 8601 UTC, et les parser comme telles décale l'heure.
C'est dit dans chaque champ de date des modèles de sortie — un générateur de types ne
doit pas promettre un instant absolu là où le serveur rend une heure murale.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, model_validator

from ... import deprecations, ownership
from ...datastore import hors_org
from ...datastore.core import DatastoreNotFound, make_store
from ...datastore.errors import est_un_nom_de_tableau, refus_tableau_par_nom
from .._types import AuthzDenied

# Phrase unique du champ de date des sorties datastore (cf. l'avertissement ci-dessus).
HORODATAGE = ("heure locale serveur, sans offset — `YYYY-MM-DD HH:MM:SS`, "
              "à ne pas parser comme de l'ISO UTC")


def ns_not_found(sub: Optional[str], datastore: str) -> AuthzDenied:
    """Le 404 qui dit OÙ vit le tableau quand il appartient à une autre org du user.

    L'API résout le store sur l'org ACTIVE ; viser le tableau d'une autre org demande
    l'en-tête `X-Oto-Org`, qui n'apparaissait ni dans la description des routes ni dans
    le moindre message. Un datastore bien réel répondait donc « datastore_not_found »,
    ce qui se lit comme « il n'existe pas » — temps perdu, et un faux diagnostic produit
    au passage (signal #316).

    On ne nomme que des orgs dont le porteur du token est MEMBRE : l'indice ne révèle
    rien qu'il ne puisse déjà lister. Fail-open : au moindre pépin, le 404 nu d'avant.

    Rend l'exception au lieu de la lever — l'appelant écrit `raise ns_not_found(…)`, ce
    qui garde le `raise` visible sur la ligne du chemin d'erreur.

    Un NOM de tableau n'est pas « introuvable » : il est refusé comme forme d'adresse
    (`refus_nom_de_tableau`), et le refus le dit.
    """
    if est_un_nom_de_tableau(datastore):
        return refus_nom_de_tableau(datastore)
    if ownership.vue_bornee() is not None:
        # Vue bornée (oto#270) : rien d'une autre org ne sort, pas même son indice —
        # la même règle que la face MCP (`DatastorePg._trouver`).
        return AuthzDenied(404, "datastore_not_found")
    try:
        # La recherche est celle de la face MCP (#631) ; seul le remède diffère.
        elsewhere = hors_org.ou_existe(sub, datastore)
        if elsewhere:
            where = ", ".join(f"{nom or 'org'} (org {oid})" for oid, nom in elsewhere)
            first = elsewhere[0][0]
            return AuthzDenied(
                404, "datastore_not_found",
                f"`{datastore}` exists, but in another of your organizations: "
                f"{where}. Replay the request with the header `X-Oto-Org: {first}`.")
    # noqa: SILENT — suggestion de tableau voisin : absente plutôt que fausse
    except Exception:  # noqa: BLE001 — un indice ne doit jamais casser la réponse
        pass
    return AuthzDenied(404, "datastore_not_found")


def refus_nom_de_tableau(datastore: Any) -> AuthzDenied:
    """Le 400 servi quand un tableau est désigné par son NOM (refusé depuis le
    08/11/2026) : le même code et la même phrase sur toutes les routes et tous les
    outils des capacités datastore, et ailleurs (lien de projet, automatisation, portée
    d'un jeton). Le code est écrit en littéral pour que le relevé des refus déclarés le
    voie ; c'est `errors.CODE_TABLEAU_PAR_NOM`."""
    return AuthzDenied(400, "datastore_by_name", refus_tableau_par_nom(datastore))


def govern_ns(sub: Optional[str], datastore: str) -> int:
    """Résout le datastore par nom + vérifie le droit de GOUVERNANCE de l'acteur
    (owner ∪ escalade `roles.py`, ADR 0030 — jamais un simple rôle d'org).

    ⚠️ Le 404 est ici **nu**, sans l'indice cross-org ci-dessus : c'est le comportement
    des routes de gouvernance d'avant la migration (renommer, partager), et un indice
    qui suggère « rejoue avec X-Oto-Org » n'aurait pas de sens sur un geste que
    l'acteur n'a de toute façon pas le droit de faire ailleurs.
    """
    if est_un_nom_de_tableau(datastore):
        raise refus_nom_de_tableau(datastore)
    try:
        ns_id = make_store(sub).resolve_ns_id(datastore)
    except DatastoreNotFound:
        raise AuthzDenied(404, "datastore_not_found")
    if not ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id)):
        raise AuthzDenied(403, "forbidden")
    return ns_id


# ── `namespace` a été renommé `datastore` (09/09/2026) ───────────────────────
#
# Le paramètre n'existe plus sous son ancien nom. Sans la garde ci-dessous, pydantic
# **ignore silencieusement** la clé inconnue (`extra="ignore"`, le défaut) et l'appel
# échoue un cran plus loin sur :
#
#     datastore: Field required
#
# ⚠️ **Le verdict est juste et le message envoie chercher au mauvais endroit.** Un
# agent qui lit « champ requis manquant » cherche ce qu'il a OUBLIÉ ; il ne peut pas
# deviner qu'il a fourni la bonne valeur sous un nom qui n'existe plus. C'est
# exactement le défaut qui, sur un autre refus, a fait brûler 60 appels à dix agents
# persuadés que leur APPEL était fautif.
#
# On ne change pas le verdict — l'appel échouait, il échoue toujours. On change ce
# qu'il DIT, et c'est tout l'objet : un refus doit nommer sa cause et le geste qui
# aboutit.
#
# ⚠️ **Et on n'accepte pas `namespace` comme alias.** Les alias de ce renommage sont
# des redirections ANNONCÉES (308 + en-tête `Sunset` au 08/11/2026), jamais des
# acceptations muettes : un paramètre repris en silence laisserait les textes non
# migrés le rester, sans que personne l'apprenne — et masquer un problème au lieu de
# lever est précisément ce qu'on ne fait pas ici.

# La date et le texte du refus vivent dans `deprecations.PARAMETRES_RENOMMES` : une
# source unique, que l'enveloppe d'erreur des outils écrits à la main sert aussi (oto#135).


class EntreeDatastore(BaseModel):
    """Base des entrées datastore : elle ne fait qu'une chose, nommer un renommage.

    Aucun champ ajouté — la signature MCP est dérivée de `model_fields`, et un champ
    de plus deviendrait un paramètre offert. Un validateur, lui, est invisible au
    schéma servi.
    """

    @model_validator(mode="before")
    @classmethod
    def _namespace_a_ete_renomme(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "namespace" not in data:
            return data
        # Ne parle que là où le renommage a EU LIEU : une entrée sans champ
        # `datastore` (s'il en existe une) n'a rien à dire sur ce mot.
        if "datastore" not in cls.model_fields:
            return data
        raise ValueError(deprecations.refus_parametre_renomme(
            "namespace", data.get("namespace")))
