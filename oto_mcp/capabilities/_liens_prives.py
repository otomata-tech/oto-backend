"""Rendre privé, SUR DEMANDE, ce qu'on lie à un projet privé (`op=link make_private=true`).

Le défaut constaté : un projet possédé par une personne ou une équipe, partagé à aucune
org, liait des procédures et des tableaux possédés par l'ORG ; tout membre de l'org sans
partage du projet les lisait par le catalogue de l'org, et rien ne le disait.

`op=link` sans `make_private` ne touche à rien : son comportement servi ne change pas
(un contrat servi ne se durcit pas en place). Avec `make_private=true`, une procédure ou
un tableau possédé par l'org, lié à un projet privé, est TRANSFÉRÉ au propriétaire du
projet et partagé à ses bénéficiaires vivants — tableau : même rôle ; procédure :
`viewer` (aucun chemin servi n'édite la copie personnelle d'un autre). Transfert,
rangement, partages et journal tiennent dans UNE transaction
(`db.transaction_d_ecriture`) : un échec au milieu n'en laisse rien.

Relier un lien déjà posé (`link_status: unchanged`) avec `make_private=true` applique la
même règle : c'est le geste de rattrapage d'un projet d'avant.

Ce qui n'est pas rendu privé le DIT (`privacy.made_private: false` + `reason`) ; le
lien, lui, est posé :

- `project_not_private` : le projet est possédé par l'org, ou partagé à une org ;
- `not_org_owned` : la cible n'est pas à l'org (déjà personnelle ou d'équipe) ;
- `linked_by_other_projects` : un autre projet vivant lie la même cible ; la rendre
  privée la lui retirerait ;
- `needs_org_admin` : sortir une ressource de l'org est un transfert, réservé à qui
  administre l'org (`ownership.can_transfer`) ;
- `agents_depend_on_it` : un agent hébergé ACTIF de l'org la lit et ne la lirait plus ;
  `agents` les liste (`id`, `label`). La même règle pour les deux cibles : déplacée chez
  une équipe, la cible est perdue pour TOUS ses agents ; chez une personne, pour ceux
  d'un autre porteur. Une procédure est lue par l'agent qui l'exécute (son slug), par
  slug chez SON porteur puis à l'org, jamais à l'équipe (`_procedure_du_travail`) ; un
  tableau est lu par l'agent qui travaille dans le projet (`_projet_de_l_agent.servis` :
  le projet de l'agent, sinon le premier de sa procédure), par les slots de ce projet ;
- `slug_taken_at_owner` / `name_taken_at_owner` : le propriétaire du projet a déjà une
  procédure de ce slug (le déplacement la suffixerait, et ses agents liraient l'autre)
  ou un tableau de ce nom.

⚠️ Limite connue : une procédure qu'une AUTRE procédure cite entre accents graves (la
chaîne d'un agent), ou un tableau qu'une procédure nomme sans passer par un slot du
projet, n'est pas cherché. Un agent d'un autre porteur qui l'atteint ainsi ne le lirait
plus.

Le code des raisons est le contrat ; la phrase ne l'est pas."""
from __future__ import annotations

from typing import Optional

from .. import db, org_store, ownership
from . import _projet_de_l_agent

_RTYPE = {"tableau": ownership.TYPE_RESSOURCE_DATASTORE,
          "procedure": ownership.TYPE_RESSOURCE_PROCEDURE}
CIBLES = tuple(_RTYPE)


def _grants_vivants(project_id: int) -> list[dict]:
    return [g for g in db.list_resource_grants("project", str(project_id))
            if not g.get("expired")]


def projet_prive(row: dict) -> bool:
    """Privé = possédé par une personne ou une équipe, et partagé à aucune org."""
    if row.get("owner_type") not in ("user", "group"):
        return False
    return not any(g["principal_type"] == "org" for g in _grants_vivants(int(row["id"])))


def _pas_prive(reason: str, agents: Optional[list[dict]] = None) -> dict:
    out: dict = {"made_private": False, "reason": reason}
    if agents is not None:
        out["agents"] = agents
    return out


def _actifs(org_id: int) -> list[dict]:
    return [t for t in db.list_triggers(int(org_id)) if t.get("enabled")]


def _lecteurs_procedure(org_id: Optional[int], slug: str, project_id: int) -> list[dict]:
    """Les agents actifs de l'org qui EXÉCUTENT la procédure `slug`."""
    if org_id is None:
        return []
    return [t for t in _actifs(int(org_id)) if (t.get("procedure") or "") == slug]


def _lecteurs_tableau(org_id: Optional[int], slug: str, project_id: int) -> list[dict]:
    """Les agents actifs de l'org qui TRAVAILLENT dans le projet — ses slots désignent
    le tableau. Les autres projets qui le lient sont déjà écartés
    (`linked_by_other_projects`)."""
    if org_id is None:
        return []
    return [t for t in _projet_de_l_agent.servis(int(org_id), _actifs(int(org_id)))
            if t.get("project_id") is not None and int(t["project_id"]) == project_id]


def _coupes(agents: list[dict], owner_type: str, owner_id: str) -> list[dict]:
    """Ceux des `agents` qui ne liraient plus la cible chez `(owner_type, owner_id)` :
    tous chez une équipe (palier qu'un agent ne résout jamais), ceux d'un autre porteur
    chez une personne."""
    if owner_type == "group":
        return agents
    return [t for t in agents if str(t.get("sub")) != owner_id]


def _obstacle(target_type: str, ref: str, org_id: Optional[int], project_id: int,
              owner_type: str, owner_id: str) -> Optional[dict]:
    if target_type == "procedure":
        instr = org_store.get_instruction_by_id(int(ref))
        if instr is None:
            raise ValueError(f"procédure #{ref} introuvable")
        nom, lecteurs = instr["slug"], _lecteurs_procedure
    else:
        tab = db.get_datastore_by_id(int(ref))
        if tab is None:
            raise ValueError(f"tableau #{ref} introuvable")
        nom, lecteurs = tab["datastore"], _lecteurs_tableau
    coupes = _coupes(lecteurs(org_id, nom, project_id), owner_type, owner_id)
    if coupes:
        return _pas_prive("agents_depend_on_it",
                          [{"id": t["id"], "label": t.get("label")} for t in coupes])
    if target_type == "procedure":
        if not org_store.instruction_slug_libre(owner_type, owner_id, nom):
            return _pas_prive("slug_taken_at_owner")
    elif db.datastore_name_taken(owner_type, owner_id, nom):
        return _pas_prive("name_taken_at_owner")
    return None


def rendre_prive(sub: str, row: dict, target_type: str, target_ref: str) -> dict:
    """Rend privée la cible `target_type`/`target_ref` du projet `row`, juste liée, à la
    demande explicite de l'appelant (`make_private=true`). Rend le compte rendu servi
    sous `privacy` : ce qui a été fait, ou pourquoi rien ne l'a été."""
    rtype = _RTYPE[target_type]
    ref = str(target_ref)
    if not projet_prive(row):
        return _pas_prive("project_not_private")
    owner = ownership.owner_of(rtype, ref)
    if owner is None or owner[0] != "org":
        return _pas_prive("not_org_owned")
    pid = int(row["id"])
    if any(p != pid for p in db.projects_linking(target_type, ref)):
        return _pas_prive("linked_by_other_projects")
    if not ownership.can_transfer(sub, rtype, ref):
        return _pas_prive("needs_org_admin")
    dest = (str(row["owner_type"]), str(row["owner_id"]))
    with db.transaction_d_ecriture():
        # Les sondes relues DANS la transaction qui écrit : ce qu'elles disent est ce
        # que le transfert trouve.
        obstacle = _obstacle(target_type, ref, int(owner[1]), pid, *dest)
        if obstacle:
            return obstacle
        ownership.transfer(rtype, ref, *dest)
        if target_type == "tableau" and dest[0] == "user" and row.get("context_org_id"):
            # Un tableau perso se range dans l'org où il vit (oto#160) : celle du projet,
            # sinon il ne sortirait que dans la maison de son nouveau propriétaire.
            db.ranger_tableau(int(ref), int(row["context_org_id"]))
        partages = []
        for g in _grants_vivants(pid):
            if (g["principal_type"], str(g["principal_id"])) == dest:
                continue
            # Une procédure personnelle ne s'écrit que par son propriétaire : aucun
            # chemin servi n'édite la copie d'un autre (`op=set` écrit chez soi ou à
            # l'org). Un partage `editor` y serait lettre morte : on partage en lecture.
            ecrit = (g.get("role") in ("editor", "manager") or g.get("permission") == "write")
            role = "editor" if ecrit and target_type == "tableau" else "viewer"
            ownership.grant(rtype, ref, g["principal_type"], str(g["principal_id"]),
                            role=role, granted_by=sub)
            partages.append({"principal_type": g["principal_type"],
                             "principal_id": str(g["principal_id"]), "role": role})
        db.log_project_activity(pid, sub, "project.link_private",
                                f"{target_type}:{ref} → {dest[0]}:{dest[1]}")
    return {"made_private": True, "moved_from": {"owner_type": owner[0],
                                                 "owner_id": owner[1]},
            "owner": {"owner_type": dest[0], "owner_id": dest[1]},
            "shared_with": partages}
