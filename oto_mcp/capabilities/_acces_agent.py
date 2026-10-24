"""Qui peut VOIR, MODIFIER et PARTAGER un agent hébergé (`runner_triggers`).

Avant ce module, tout membre de l'org modifiait ou supprimait l'agent de n'importe
qui — et l'agent continuait de tourner SOUS SON PROPRIÉTAIRE, avec ses connexions.
Désormais un agent est à son propriétaire ; il le PARTAGE nommément :

- `owner`  — celui au nom de qui l'agent tourne (`runner_triggers.sub`) ;
- `admin`  — un admin de l'org de l'agent : tout, comme le propriétaire ;
- `editor` — un partage `write` (personne de l'org, ou l'org entière) : le modifie,
  l'allume, l'éteint, vide sa file. Il ne le supprime pas, ne change pas sa porte de
  webhook et ne le partage pas : ce sont des gestes de propriétaire ;
- `viewer` — un partage `read` : le lit, sans les corps de livraison (des données
  de tiers reçues par le webhook).

Les partages vivent dans `resource_grants`, sous le kind `runner_trigger`
(`ownership.py`). ⚠️ **On ne partage qu'à l'intérieur de l'org de l'agent** : un
agent tient les clés de l'org et les connexions de son propriétaire ; le confier à
quelqu'un du dehors lui ouvrirait les deux. D'où ses propres verbes (`share`,
`unshare`, `shares` sur `runner.triggers`) plutôt qu'`oto_resource`, qui sait
partager hors de l'org et à une adresse sans compte.

⚠️ **Le partage ne change pas l'identité d'exécution** : l'agent tourne toujours
sous son propriétaire, quel que soit celui qui l'a modifié. Partager en écriture,
c'est confier son identité — le front le dit au moment de partager.

⚠️ **Un agent posé sur l'ABONNEMENT personnel de son propriétaire** se modifie par
quiconque le tient en ÉCRITURE par un partage — à lui, à son équipe, ou à toute l'org
dont il est membre, quel qu'en soit l'auteur (`forfaits_pretes`, décidé le 23/10/2026 :
un agent partagé se modifie, quel que soit le modèle qui le fait tourner). Ce qui y
change s'exécute sur le forfait du propriétaire. Sans partage en écriture — un admin
qui gouverne l'agent sans le tenir, un lecteur — on peut seulement l'éteindre. Le prêt
vaut pour la famille où l'agent tourne DÉJÀ : poser un agent sur un abonnement reste
au propriétaire.
"""
from __future__ import annotations

from typing import Literal, Optional

from .. import db, ownership, roles, runner_models
from ._types import AuthzDenied, ResolvedCtx

KIND = "runner_trigger"

Acces = Literal["owner", "admin", "editor", "viewer"]
ECRIRE: tuple[str, ...] = ("owner", "admin", "editor")
GOUVERNER: tuple[str, ...] = ("owner", "admin")


# --- Le kind d'`ownership` ------------------------------------------------------

def _proprietaire(rid: str) -> Optional[tuple[str, str]]:
    t = db.trigger_sans_org(int(rid)) if str(rid).isdigit() else None
    return ("user", str(t["sub"])) if t else None


def _reparenter(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    """Un agent appartient à une PERSONNE (c'est elle qu'il incarne) : il ne passe
    ni à une équipe ni à une org."""
    if new_owner_type != "user":
        raise ValueError("un agent appartient à une personne de son org, jamais à "
                         "une équipe ou à une org : il tourne sous son identité.")
    t = db.trigger_sans_org(int(rid))
    if t is None or db.reprendre_trigger(int(rid), int(t["org_id"]), new_owner_id) is None:
        raise ValueError(f"agent #{rid} introuvable")


def _org_de(rid: str) -> Optional[int]:
    t = db.trigger_sans_org(int(rid)) if str(rid).isdigit() else None
    return int(t["org_id"]) if t else None


ownership.register_kind(KIND, ownership.ResourceKind(
    owner_getter=_proprietaire, reparent=_reparenter, context_org=_org_de))


# --- Le niveau d'accès ----------------------------------------------------------

def niveaux(sub: str, org_id: int, agents: list[dict]) -> dict[int, Optional[str]]:
    """Le niveau de `sub` sur chacun des `agents` (lignes de `runner_triggers` de
    `org_id`) — None = il ne le voit pas. Une requête de partages pour toute la
    liste."""
    autrui = [t for t in agents if t.get("sub") != sub]
    # Rien à lire quand l'appelant possède tout ce qu'on lui montre : le cas
    # courant, et celui d'une création — qui ne doit pas payer une lecture de rôle.
    admin = bool(autrui) and roles.is_org_admin(sub, org_id)
    a_lire = [int(t["id"]) for t in autrui if not admin]
    partages = (db.partages_d_agents(a_lire,
                                     ownership.accessor_scope(sub).principal_pairs())
                if a_lire else {})
    out: dict[int, Optional[str]] = {}
    for t in agents:
        i = int(t["id"])
        if t.get("sub") == sub:
            out[i] = "owner"
        elif admin:
            out[i] = "admin"
        else:
            out[i] = {"write": "editor", "read": "viewer"}.get(partages.get(i, ""))
    return out


def niveau(sub: str, org_id: int, agent: dict) -> Optional[str]:
    return niveaux(sub, org_id, [agent])[int(agent["id"])]


def sur_le_forfait_d_autrui(sub: str, agent: dict) -> bool:
    """L'agent tourne-t-il sur l'abonnement PERSONNEL d'un autre que `sub` ? Même
    lecture que la garde d'écriture (`db.update_trigger`) : le modèle STOCKÉ."""
    proprietaire = agent.get("sub")
    return (agent.get("model") in runner_models.MODELES_PERSONNELS
            and bool(proprietaire) and proprietaire != sub)


def forfaits_pretes(sub: str, agents: list[dict]) -> set[int]:
    """Parmi `agents`, ceux qui tournent sur le forfait d'un autre ET que `sub` tient
    en écriture par un partage (à lui, à son équipe ou à son org). Une requête pour
    toute la liste, aucune quand rien ne tourne sur le forfait d'autrui."""
    a_lire = [int(t["id"]) for t in agents if sur_le_forfait_d_autrui(sub, t)]
    return db.forfaits_pretes(a_lire, sub) if a_lire else set()


def prete(sub: str, agent: dict) -> bool:
    return int(agent["id"]) in forfaits_pretes(sub, [agent])


def forfait_ferme(sub: str, agent: dict, pretes: Optional[set[int]] = None) -> bool:
    """`sub` ne peut rien changer à cet agent, sauf l'éteindre : il tourne sur le
    forfait d'un autre, et aucun partage en écriture ne le lui prête. `pretes` = `forfaits_pretes`
    d'une liste déjà lue ; absent, il se lit pour ce seul agent."""
    if not sur_le_forfait_d_autrui(sub, agent):
        return False
    if pretes is None:
        pretes = forfaits_pretes(sub, [agent])
    return int(agent["id"]) not in pretes


def avec_acces(t: dict, acces: Optional[str], *, ferme: bool = False) -> dict:
    """Le déclencheur servi, augmenté de ce que l'APPELANT peut en faire — le front
    grise ses boutons sur ces champs plutôt que de re-déduire la règle.

    `ferme` (`forfait_ferme`) : `can_edit` dit alors non, même à un éditeur ou à un
    admin — la retouche serait refusée (`subscription_personal_only`). L'éteindre
    reste ouvert à tout niveau qui écrit, et `my_access` le dit."""
    return {**t, "my_access": acces, "can_edit": acces in ECRIRE and not ferme,
            "can_share": acces in GOUVERNER}


def avec_visibilites(org_id: int, agents: list[dict]) -> list[dict]:
    """Chaque agent servi avec `visibility` — qui le voit au-delà de son propriétaire
    et des admins. UNE lecture de partages pour toute la liste : l'écran des agents
    l'affiche sur chaque ligne (« Privé » / « Toute l'org »), comme les tableaux."""
    vis = db.visibilites_d_agents([int(t["id"]) for t in agents], org_id)
    return [{**t, "visibility": vis[int(t["id"])]} for t in agents]


def ouvrir_a_l_org(ctx: ResolvedCtx, agent: dict) -> None:
    """Le partage posé à la NAISSANCE d'un agent : `editor` à toute son org. Un agent
    travaille pour l'org (clés et procédures d'org) — il naît visible et modifiable
    par elle ; le rendre privé est le geste explicite (`private=true`, ou `unshare
    everyone`). Ne prête PAS un abonnement personnel (cf. tête de module)."""
    ownership.grant(KIND, str(agent["id"]), "org", str(ctx.org_id),
                    role="editor", granted_by=ctx.sub)


def _inconnu() -> AuthzDenied:
    # Même 404 qu'un agent qui n'existe pas : ne pas voir un agent, c'est ne pas
    # savoir qu'il existe.
    return AuthzDenied(404, "trigger_not_found", "automatisation inconnue")


def exiger(ctx: ResolvedCtx, agent: Optional[dict], requis: tuple[str, ...]) -> str:
    """Le niveau de l'appelant sur `agent`, ou le refus nommé. Invisible → 404 ;
    visible mais insuffisant → 403 `trigger_edit_forbidden`."""
    if not agent:
        raise _inconnu()
    n = niveau(ctx.sub, ctx.org_id, agent)
    if n is None:
        raise _inconnu()
    if n not in requis:
        geste = ("le modifier" if requis == ECRIRE
                 else "le supprimer, le partager ou changer sa porte")
        raise AuthzDenied(
            403, "trigger_edit_forbidden",
            f"tu as accès à cet agent en {n} : {geste} est réservé à "
            + ("son propriétaire, aux personnes avec qui il l'a partagé en écriture, "
               "et aux admins de l'org." if requis == ECRIRE
               else "son propriétaire et aux admins de l'org."))
    return n


# --- Les partages ---------------------------------------------------------------

ROLES = ("viewer", "editor")


def lister(org_id: int, agent_id: int) -> list[dict]:
    """Les partages de l'agent, pour l'écran « Partager ». `everyone` = l'org entière."""
    out = []
    for g in ownership.list_grants(KIND, str(agent_id)):
        if g.get("expired"):
            continue
        ptype, pid = g.get("principal_type"), str(g.get("principal_id") or "")
        if ptype == "org" and pid != str(org_id):
            continue   # un partage hors de l'org ne donne rien ici : on ne le sert pas
        out.append({"principal_type": "everyone" if ptype == "org" else ptype,
                    "sub": pid if ptype == "user" else None,
                    "email": g.get("email"), "role": g.get("role"),
                    "granted_at": g.get("granted_at")})
    return out


def principal(ctx: ResolvedCtx, *, everyone: bool, sub: Optional[str],
              email: Optional[str], strict: bool = True) -> tuple[str, str]:
    """Le bénéficiaire d'un partage : l'org de l'agent entière, ou UN membre de
    cette org — par son `sub` ou son adresse. `strict=False` (retrait) ne vérifie pas
    l'appartenance : on doit pouvoir retirer le partage d'un membre parti."""
    if everyone:
        if sub or email:
            raise AuthzDenied(400, "share_target_ambiguous",
                              "`everyone` OU `share_with_sub`/`share_with_email`, pas les deux.")
        return "org", str(ctx.org_id)
    if bool(sub) == bool(email):
        raise AuthzDenied(400, "share_target_required",
                          "nomme UN bénéficiaire : `share_with_sub` ou "
                          "`share_with_email` (ou `everyone=true`).")
    if email:
        porteurs = [u for u in db.get_users_by_email(email.strip())
                    if not strict or roles.is_org_member(u["sub"], ctx.org_id)]
        # Deux refus littéraux, pas un ternaire : le cliquet des refus déclarés ne
        # lit que `AuthzDenied(<status>, "<code>")` écrit en toutes lettres.
        if not porteurs:
            raise AuthzDenied(404, "share_not_org_member",
                              f"`{email}` n'est pas membre de cette org : un agent ne "
                              "se partage qu'à l'intérieur de son org. Invite d'abord "
                              "la personne dans l'org.")
        if len(porteurs) > 1:
            raise AuthzDenied(400, "ambiguous_email",
                              f"`{email}` désigne plusieurs comptes de l'org : passe "
                              "`share_with_sub`.")
        return "user", porteurs[0]["sub"]
    if strict and not roles.is_org_member(sub, ctx.org_id):
        raise AuthzDenied(404, "share_not_org_member",
                          "un agent ne se partage qu'à un membre de son org.")
    return "user", str(sub)


# --- Les travaux d'un agent qu'on ne peut pas modifier ---------------------------

#: Ce qu'un travail laisse voir de sa charge à qui ne peut pas MODIFIER son agent :
#: de quoi le reconnaître dans une liste (quel agent, quelle procédure, quel modèle),
#: rien de ce qu'il exécute — ni la consigne, ni les outils, ni le corps reçu par un
#: webhook (une donnée de tiers).
_CHARGE_LISIBLE = ("trigger_id", "procedure", "label", "model", "model_family")


def masquer_charges(ctx: ResolvedCtx, jobs: list[dict]) -> list[dict]:
    """`runner.jobs` (`list`, `get`) sert la file de TOUTE l'org : un agent privé y
    laissait lire sa consigne et ses corps de livraison. La charge d'un travail
    enfilé par un agent que l'appelant ne peut pas modifier est réduite à
    `_CHARGE_LISIBLE` — la ligne reste (le compte de la file ne ment pas), son
    contenu non. Un travail sans agent (lancé à la main) n'est pas touché : il
    porte l'identité de qui l'a lancé, et sa lecture a ses propres règles."""
    ids = {int(p["trigger_id"]) for j in jobs
           if isinstance(p := j.get("payload"), dict)
           and str(p.get("trigger_id") or "").isdigit()}
    if not ids:
        return jobs
    agents = [t for i in sorted(ids) if (t := db.get_trigger(i, ctx.org_id))]
    n = niveaux(ctx.sub, ctx.org_id, agents)
    # Un agent SUPPRIMÉ n'a plus de partages à lire : ses travaux restent lisibles à
    # qui les portait (son propriétaire d'alors) et aux admins de l'org.
    orphelins = ids - set(n)
    admin = bool(orphelins) and roles.is_org_admin(ctx.sub, ctx.org_id)
    out = []
    for j in jobs:
        p = j.get("payload")
        tid = p.get("trigger_id") if isinstance(p, dict) else None
        if str(tid or "").isdigit():
            lisible = (n[int(tid)] in ECRIRE if int(tid) in n
                       else admin or j.get("sub") == ctx.sub)
            if not lisible:
                # `payload_redacted` : la charge servie n'est PAS celle du travail —
                # un écran ne doit pas proposer de la rejouer.
                j = {**j, "payload": {**{k: p[k] for k in _CHARGE_LISIBLE if k in p},
                                      "payload_redacted": True}}
        out.append(j)
    return out
