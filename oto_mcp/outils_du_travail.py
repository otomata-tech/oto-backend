"""La liste d'outils d'un travail hébergé, tenue PAR LE SERVEUR sur chaque chemin d'appel (#1256).

Un travail du runner déclare ses outils (`payload.tools`, au canonique). Jusqu'ici, la seule
borne était celle du worker : il ne sert au modèle que ces outils (`tools/list` filtré) et
refuse un `tools/call` hors liste. Mais le jeton de délégation que le serveur lui remet
(`capabilities/runner_jobs._delegue`) ouvre TOUT le catalogue du porteur, et un outil de la
liste qui en appelle d'autres côté serveur les appelait sans que personne regarde la liste :
`oto_call`, `oto_recipe` (recette déclarative, `for_each`, échantillon, recette en code et son
pont). Vécu le 11/10/2026 : un `oto_recipe op=test` lancé par un travail a appelé `fr_siret`,
absent de sa liste — l'appel n'a échoué que faute de clé du fournisseur.

**Tout outil hors de la liste du travail est refusé, quel que soit le chemin** (code
`tool_not_in_job`). Les points de passage sont ceux de l'activation d'un connecteur
(`connectors/activation_gate.py`) :

- l'appel DIRECT (`middleware/call_context.CallContextMiddleware`) — le worker filtre déjà,
  mais une borne qui ne vit que chez le client n'en est pas une ;
- tout appel lancé DEPUIS le serveur (`tools/meta.executer_cible`, le corps d'`oto_call`, des
  pages d'une recette et des appels du pont d'une recette en code) ;
- et, pour une recette, son jugement AVANT tout effet (`recipes/moteur._outil`) : refusée
  avant d'ouvrir un tableau ou de déclarer une colonne, pas à la première page.

**La source** : le travail porté par le jeton de la requête (`verrou_org.travail_de_la_requete`,
posé au bord MCP et au bord REST), et sa charge EN BASE — jamais un argument de l'appel. La liste
est celle que le worker SERT (`outils_servis`, partagée avec la réservation) : un écart entre les
deux refuserait un outil que l'agent voit, ou laisserait passer un outil qu'il ne voit pas.

**Fail-closed** : une liste illisible ou qui n'en est pas une ne permet rien ; un travail
introuvable non plus. Seuls passent en plus les outils que l'HÔTE appelle lui-même avec le jeton
pour encadrer le travail (`OUTILS_DE_L_HOTE`) — jamais un outil de connecteur.

Hors jeton de délégation (une personne, une clé d'API, une clé d'org), rien ne change.
"""
from __future__ import annotations

from typing import Optional

from mcp.types import INVALID_PARAMS, ErrorData
from starlette.concurrency import run_in_threadpool

from . import db, deprecations, verrou_org
from .mcp_errors import McpError

#: Le code nommé d'un refus. Testé par les appelants, jamais la phrase.
CODE = "tool_not_in_job"

#: L'outil par lequel l'agent lit la procédure que son instruction lui désigne : ajouté à la
#: liste SERVIE d'un travail qui porte une procédure (`outils_servis`).
OUTIL_DE_LECTURE = "oto_procedure"

#: Ce que le WORKER appelle lui-même avec le jeton délégué, hors de la boucle du modèle :
#: ouvrir le run technique du travail et le conclure (oto-runner `worker.py`,
#: `conclusion.py`). Le modèle ne les reçoit que si la liste les nomme.
OUTILS_DE_L_HOTE = frozenset({"run_start", "run_finish"})


def outils_servis(charge: dict):
    """La liste d'outils que le worker SERT pour cette charge : `tools`, plus l'outil de
    lecture de la procédure quand le travail en porte une (`runner_jobs._charge_servie`).
    Une liste qui n'est pas une liste est rendue telle quelle : la réparer ici en
    inventerait une."""
    outils = charge.get("tools") if "tools" in charge else []
    if charge.get("procedure") and isinstance(outils, list) \
            and OUTIL_DE_LECTURE not in outils:
        return [*outils, OUTIL_DE_LECTURE]
    return outils


def _charge(job_id: Optional[int]) -> Optional[dict]:
    """Sync (base) : la charge stockée du travail, `None` s'il est introuvable."""
    charge = db.charge_du_travail(int(job_id)) if job_id is not None else None
    return charge if isinstance(charge, dict) else None


def _permis(charge: Optional[dict]) -> frozenset:
    """Les outils que cette charge permet, au canonique. Vide si le travail est introuvable
    ou si sa liste n'en est pas une."""
    outils = outils_servis(charge) if charge is not None else None
    if not isinstance(outils, list):
        return frozenset()
    return frozenset(deprecations.tool_canonique(n) for n in outils if isinstance(n, str))


def refus(name: str) -> Optional[str]:
    """Sync (base) : le message du refus si `name` est hors de la liste du travail de la
    requête — None si l'appel passe (outil de la liste, outil de l'hôte, ou requête qui
    n'est pas un travail hébergé)."""
    verrou = verrou_org.travail_de_la_requete()
    if verrou is None:
        return None
    nom = deprecations.tool_canonique(name)
    if nom in OUTILS_DE_L_HOTE or nom in _permis(_charge(verrou.job_id)):
        return None
    return (f"`{name}` is not in this hosted agent's tool list (job {verrou.job_id}). "
            "A job only calls the tools it declares, on every path — directly, through "
            "`oto_call`, or from a recipe. Add the tool to the agent's `tools` to allow it.")


def permis_du_travail(job_id: Optional[int]) -> frozenset:
    """Sync (base) : tout ce que le travail `job_id` peut appeler, au canonique — sa liste
    servie et les outils de l'hôte (`exiger`). Vide hors de l'hôte si le travail est
    introuvable : fail-closed, comme le refus à l'appel."""
    return _permis(_charge(job_id)) | OUTILS_DE_L_HOTE


def en_trop(charge: dict) -> Optional[list[str]]:
    """Sync (base) : les outils que la charge `charge` d'un AUTRE travail servirait et que
    le travail de la requête ne peut pas appeler, au canonique et triés — `None` hors
    travail hébergé, `[]` si la liste servie de `charge` tient dans la sienne.

    C'est la borne d'un lancement depuis un travail (`run_now`, armement d'une campagne,
    #1499) : l'enfant tourne avec SA liste, donc un parent qui lance un enfant plus
    outillé que lui sortirait de la sienne en une marche. Même calcul des deux côtés
    (`outils_servis`, `deprecations.tool_canonique`) ; les outils de l'hôte sont hors du
    calcul — le parent les a toujours. Parent introuvable : il ne permet que l'hôte."""
    verrou = verrou_org.travail_de_la_requete()
    if verrou is None:
        return None
    return sorted(_permis(charge) - permis_du_travail(verrou.job_id))


def manquants(noms) -> Optional[dict]:
    """Sync (base) : TOUS les outils de `noms` hors de la liste du travail de la requête,
    en une lecture — `None` hors travail hébergé ou si rien ne manque. Rend
    `{job_id, trigger_id, tools, missing}` (`tools` : la liste de l'agent telle que ce
    travail l'a reçue) : de quoi refuser une fois en nommant chaque outil et l'agent à
    compléter, plutôt qu'un refus par outil, une exécution chacun."""
    verrou = verrou_org.travail_de_la_requete()
    if verrou is None:
        return None
    charge = _charge(verrou.job_id)
    permis = _permis(charge)
    manque = []
    for n in noms:
        if not isinstance(n, str):
            continue
        c = deprecations.tool_canonique(n)
        if c not in OUTILS_DE_L_HOTE and c not in permis and c not in manque:
            manque.append(c)
    if not manque:
        return None
    declencheur = (charge or {}).get("trigger_id")
    outils = (charge or {}).get("tools")
    return {"job_id": verrou.job_id, "missing": manque,
            "tools": [n for n in outils if isinstance(n, str)] if isinstance(outils, list) else [],
            "trigger_id": int(declencheur) if str(declencheur or "").isdigit() else None}


def correctif(trigger_id: Optional[int], outils: list, manque: list) -> dict:
    """Le geste qui lève le refus, sous une forme qu'un écran joue en un clic : la liste
    de l'agent (`outils`) augmentée de ces outils, posée par `oto_trigger op=update` —
    `tools` REMPLACE la liste, d'où la liste entière. Un geste de personne : sous un jeton
    de délégation, l'élargissement est refusé (`tools_human_only`)."""
    return {"tool": "oto_trigger", "op": "update", "trigger_id": trigger_id,
            "tools": list(dict.fromkeys([*outils, *manque]))}


async def exiger(name: str) -> None:
    """Lève le refus nommé `tool_not_in_job` si `name` est hors de la liste du travail.
    Hors boucle : la liste se lit en base."""
    message = await run_in_threadpool(refus, name)
    if message is not None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message=f"Refusal `{CODE}`: {message}",
            data={"code": CODE, "retryable": False, "tool": name}))
