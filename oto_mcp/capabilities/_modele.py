"""Le modèle d'un agent, vu des capacités : le valider, et refuser de le PROMETTRE.

Partagé par `runner.triggers` et `runner.fleets` — les deux déclarent un agent, et
les deux doivent tenir la même règle avec les mêmes mots. Le catalogue lui-même
vit dans `runner_models` (pur, lu aussi par la base).
"""
from __future__ import annotations

from typing import Optional

from .. import db, runner_models
from . import _cle_exigee
from ._types import AuthzDenied


def exige_un_runner(org_id: int) -> dict:
    """Refuse de PROMETTRE une exécution que personne n'assure — et rend l'état lu,
    pour que la garde du modèle (`exige_servi`) juge sur la même lecture.

    ⚠️ Partagée par `runner.triggers` (poser un déclencheur) et `runner.fleets`
    (`launch`) — les deux gestes MENTENT de la même façon sans elle : ils
    rendent une promesse (`next_due`, `armed`) qu'aucun worker ne tient, et
    l'objet a l'air programmé sans jamais tourner. `runner_fleets#13` (oto-runner)
    a mesuré la conséquence sur `launch` : 41 travaux restés en file 13 jours,
    un armement qui a réussi et n'a jamais été repris.
    """
    etat = db.runner_arme(org_id)
    if etat["armed"]:
        return etat
    if etat["last_seen"] is None:
        detail = ("rien n'a jamais exécuté d'automatisation pour cette org")
    else:
        detail = (f"la dernière exécution possible pour cette org remonte au "
                  f"{etat['last_seen']} — au-delà de "
                  f"{db.ARME_FENETRE_S // 60} minutes, on ne la tient plus pour "
                  f"assurée")
    raise AuthzDenied(
        400, "no_runner_armed",
        f"rien n'exécute les automatisations de cette org pour l'instant "
        f"({detail}). Le geste réussirait pour rien, sans erreur — "
        "l'automatisation aurait l'air de marcher. Reprends-le quand l'exploitant "
        "de la plateforme aura rétabli l'exécution pour cette org. Ce qui existe "
        "reste gérable d'ici là : une automatisation horaire ou webhook se lit, se "
        "modifie et se supprime ; une automatisation de genre file se lit, se "
        "modifie et s'arrête (`stop`). Si le besoin est une tâche planifiée de "
        "l'utilisateur, ce n'est pas le bon outil : voir le guide "
        "`procedure-en-routine`.")


def famille_declaree(model: Optional[str],
                     provider: Optional[str] = None) -> Optional[str]:
    """La famille du modèle DÉCLARÉ, ou None si aucun ne l'est. Refuse l'inconnu.

    `""` vaut absence. Une absence n'est plus un choix (24/09/2026) : un agent
    hébergé DOIT déclarer son modèle, et c'est `exige_un_modele` qui le refuse
    aux moments de pose.

    `provider` n'existe que sur les flottes, où il précède ce catalogue. Il ne
    choisit plus rien — la famille se DÉDUIT du modèle —, donc il ne peut que
    confirmer : un fournisseur qui contredit le modèle, ou qui arrive seul, est
    refusé plutôt qu'avalé (un champ posé qui ne s'applique pas est un défaut).
    """
    if not model:
        if provider:
            raise AuthzDenied(
                400, "invalid_model",
                f"`provider={provider}` sans `model` ne choisit rien : la famille se "
                "déduit du modèle. Nomme un modèle, ou n'envoie ni l'un ni l'autre "
                "(le modèle par défaut s'applique alors).")
        return None
    f = runner_models.famille(model)
    if f is None:
        connus = ", ".join(m.id for m in runner_models.MODELES)
        raise AuthzDenied(
            400, "invalid_model",
            f"modèle inconnu : `{model}`. Modèles servis par la plateforme : {connus}.")
    if provider and provider != f:
        raise AuthzDenied(
            400, "invalid_model",
            f"`{model}` est un modèle `{f}`, pas `{provider}` — omets `provider`, il "
            "se déduit du modèle.")
    return f


#: Ce que lit une org qui a écarté les modèles qui tournent en Chine
#: (`db/regions_de_modeles.py`) : la raison d'un refus, à la pose comme à la réservation.
RAISON_REGION_CN = (
    "ce modèle tourne sur des serveurs en Chine, et un admin de l'organisation a écarté "
    "les modèles qui tournent en Chine (`oto_org_settings domain=model_regions`). "
    "Choisis un modèle qui tourne aux États-Unis ou en Europe, ou demande à un admin "
    "de les permettre.")


def region_bloquee(org_id: Optional[int], model: Optional[str]) -> bool:
    """Le modèle tourne-t-il dans une région que l'org a écartée ? Aujourd'hui une seule :
    la Chine (`runner_models.hebergement` → `cn`, `orgs.allow_cn_models`). Une org
    inconnue ou un modèle sans hébergement connu ne bloquent rien."""
    h = runner_models.hebergement(model)
    if not org_id or not h or h["region"] != "cn":
        return False
    return db.regions_de_modeles.chine_permise(int(org_id)) is False


def exige_region_permise(org_id: Optional[int], model: Optional[str]) -> None:
    """Refuse de poser un agent sur un modèle d'une région que l'org a écartée. Lève
    `model_region_blocked` (403)."""
    if region_bloquee(org_id, model):
        raise AuthzDenied(403, "model_region_blocked", RAISON_REGION_CN)


def exige_un_modele(famille: Optional[str]) -> None:
    """Refuse de poser un agent hébergé sans modèle déclaré. Lève `model_required`.

    ⚠️ Décidé le 24/09/2026, avec l'ouverture des agents hébergés à toutes les
    orgs : chaque org paie son modèle. Or la garde d'argent (`_cle_exigee`) ne
    juge que la famille DÉCLARÉE — un agent sans modèle n'exigeait aucune clé et
    tournait sur le modèle du worker, c'est-à-dire sur NOTRE clé, sans que rien
    ne le dise. Appelée à chaque moment de pose (création, rallumage, `launch`) ;
    la réservation tient la même règle (`runner_jobs._avec_cle`).
    """
    if famille:
        return
    connus = ", ".join(m.id for m in runner_models.MODELES)
    raise AuthzDenied(
        400, "model_required",
        "un agent hébergé déclare son modèle : il tourne sur la clé de modèle de son "
        "organisation, et sans modèle déclaré il n'y aurait aucune clé à exiger. "
        f"Nomme `model` — modèles servis par la plateforme : {connus}.")


def exige_servi(etat: dict, famille: Optional[str]) -> None:
    """Refuse de promettre un modèle qu'aucun worker vivant ne sert.

    ⚠️ Même asymétrie que `no_runner_armed` : un travail d'une famille que personne
    ne sert reste `pending` — le claim le filtre — puis PÉRIME à l'occurrence
    suivante. L'agent a l'air programmé et ne tourne jamais.

    ⚠️ **Ne regarde que si une famille est DEMANDÉE.** Un agent sans modèle ne se
    pose plus (`exige_un_modele`, 24/09/2026) : ce cas n'arrive ici que pour un
    geste qui ne promet aucun modèle.

    ⚠️ **Les familles ne se lisent que chez les workers de PLATEFORME** : un worker
    au jeton d'org (l'ancien chemin) ne dépose pas sa famille, et une org servie
    par lui seul se verra refuser tout modèle explicite. Aucune org de production
    n'est servie ainsi ; le jour où une le sera, c'est ici que ça mord — et la
    réponse est de lui faire nommer son dépôt, pas de retirer la garde.
    """
    if not famille:
        return
    servies = etat.get("families") or []
    if famille in servies:
        return
    vivantes = (f"familles servies : {', '.join(servies)}"
                if servies else "aucune famille n'est déclarée servie")
    # Une org routée vers la ferme (`db.OPTION_FERME`) : sa famille ferme n'est
    # servie que par un worker de ferme vivant. Le dire, sinon « non servie » se
    # lit comme une panne du parc alors que la boucle tourne.
    ferme = (" Cette organisation est routée vers la ferme Claude Code : ses agents "
             f"`{db.FAMILLE_FERME}` n'y sont exécutés que par un worker de la ferme, et "
             "aucun n'est vivant pour elle." if etat.get("farm_routed")
             and famille == db.FAMILLE_FERME else "")
    raise AuthzDenied(
        400, "model_not_served",
        f"les modèles `{famille}` ne sont pas servis en ce moment ({vivantes})."
        f"{ferme} L'exécution resterait en attente sans une erreur, puis périmerait. "
        "Choisis un modèle servi (`runner.models` sur `op=list`).")


def etat_servi(etat: dict, org_id: Optional[int] = None) -> dict:
    """L'état du runner tel que servi : présence, familles, et le catalogue marqué
    de ce qui est servi — ce dont un écran a besoin pour proposer un modèle sans
    proposer celui qui serait refusé.

    ⚠️ Avec `org_id`, le défaut proposé écarte les familles servies dont l'org n'a
    pas déposé la clé EXIGÉE : les proposer, c'est proposer un refus
    `model_key_required` à la pose. Lu seulement quand des familles sont servies — la
    lecture du réglage est froide, et un état sans famille n'a rien à écarter. Une org
    routée vers la ferme (`farm_routed`) voit écartée sa famille ferme tant qu'elle
    n'a pas déposé SA clé — une clé partagée n'y suffit pas (`_cle_exigee._manque`)."""
    familles = list(etat.get("families") or [])
    sans_cle = (_cle_exigee.manquantes(org_id, familles,
                                       routee=bool(etat.get("farm_routed")))
                if org_id is not None and familles else [])
    modeles = runner_models.catalogue(familles, sans_cle)
    # Lu seulement quand un modèle qui tourne en Chine est SERVI : sinon rien à écarter,
    # et l'état du runner se lit à chaque écran d'agent — pas une lecture de plus.
    if org_id is not None and any(m["served"] and (m.get("hosting") or {}).get("region")
                                  == "cn" for m in modeles):
        if db.regions_de_modeles.chine_permise(int(org_id)) is False:
            # Écartés par l'org : jamais proposés, jamais par défaut — un défaut écarté
            # proposerait un refus `model_region_blocked`.
            for m in modeles:
                if (m.get("hosting") or {}).get("region") == "cn":
                    m["allowed"], m["default"] = False, False
    return {**etat, "families": familles, "models": modeles}
