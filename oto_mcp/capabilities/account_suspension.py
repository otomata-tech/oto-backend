"""`admin.account` — mettre un compte en pause, et le réveiller.

**Le geste qui manquait, et pourquoi il manquait.** Un compte n'avait que deux
états : vivant, ou disparu. « Disparu » n'est d'ailleurs pas un geste de produit —
le seul `DELETE FROM users` du dépôt est l'étape 4 de `db.migrate_sub`. Et il ne
neutralise pas : la plupart des tables keyed-by-sub n'ont aucune FK vers `users`
(appartenances, projets, documents, journal), donc supprimer laisse des pointeurs
morts ; ce qui a une FK part en cascade. Les deux moitiés du résultat sont mauvaises,
et il n'y avait rien entre les deux.

**Un seul état, et c'est délibéré.** La demande disait « pauser ou archiver » — deux
mots pour deux intentions : suspendre quelqu'un qui reviendra, sortir quelqu'un qui
ne reviendra pas. Elles ne produisent pourtant **aucune différence de comportement** :
dans les deux cas le compte ne peut plus rien faire et rien n'est détruit. Ce qui les
sépare est ce que l'exploitant a en tête, et ça s'écrit — c'est le `reason`, exigé.
Deux états auraient demandé deux fois les mêmes gardes, avec un second qu'aucun test
ne pourrait distinguer du premier à l'exécution. Le « définitif », lui, existe déjà
plus loin : l'effacement de la personne (ADR 0062-D2), qui est un tout autre geste,
avec un tout autre délai et une pseudonymisation du journal.

**Ce que la pause change** : plus aucune requête ne passe, sur aucune face, dès la
suivante — jeton déjà émis compris (`account_suspension`). **Ce qu'elle ne change
pas** : tout le reste. Les appartenances restent, les projets et documents restent à
lui, ses lignes de journal restent les siennes, ses credentials restent dans le
coffre. Un document qu'il a écrit dans une org continue de dire qui l'a écrit. C'est
la raison d'être du geste : le départ d'un membre laisse à l'org un patrimoine
qu'elle arbitre objet par objet, et **l'inaction ne doit pas détruire** (ADR 0062-D4).
La pause est l'état dans lequel cet arbitrage peut prendre le temps qu'il prend.

**Qui peut le faire.** Un super admin de plateforme, sur n'importe quel compte ; un
admin de tenant, sur les comptes de SON tenant et eux seuls — sans privilège de
plateforme, comme l'ADR 0056-D3 le prévoit mot pour mot (« les administrer : créer,
paramétrer, suspendre »). Pas un org_admin : un compte n'appartient pas à une org, il
en a souvent plusieurs, et le mettre en pause le couperait d'espaces sur lesquels cet
administrateur n'a aucun titre. Le geste org-scopé existe déjà, c'est `admin.org_member
op=remove`, et il ne touche que l'appartenance visée.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import db
from ._authz import ADMIN_BY_OP, SUPER_ADMIN, TENANT_ADMIN_OF_TARGET
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_MOTIF_MAX = 500


class AccountSuspensionInput(BaseModel):
    op: Literal["suspend", "resume"]
    # ⚠️ Un **sub**, pas une adresse électronique — contrairement aux autres consoles
    # de compte, qui acceptent les deux. Le chantier qui a demandé ce geste porte huit
    # personnes présentes DEUX fois, une identité chez nous et une chez le partenaire,
    # sous la même adresse : résoudre par adresse choisirait à la place de l'opérateur
    # exactement là où il doit choisir lui-même. C'est aussi ce qui permet à la règle
    # d'autorisation de dériver le tenant de la cible sans lire la base.
    target: str
    # Exigé pour `suspend`. Un compte neutralisé sans motif écrit devient, six mois
    # plus tard, un compte que personne n'ose réveiller et que personne ne sait
    # expliquer. La contrainte est ici, dans l'`Input` : c'est la seule couche qui
    # s'exécute avant l'autorisation, et elle est publiée dans le schéma servi.
    reason: Optional[str] = None


class AccountSuspensionOut(BaseModel):
    """L'état APRÈS le geste — la même forme pour les deux ops.

    `changed` distingue « je viens de le mettre en pause » de « il l'était déjà », ce
    qu'un booléen `suspended` seul ne dit pas. Sans lui, un opérateur qui rejoue son
    geste ne peut pas savoir s'il a agi, et une console qui affiche « fait » ment une
    fois sur deux."""
    sub: str
    suspended: bool
    changed: bool
    suspended_at: Optional[str] = None
    suspended_by: Optional[str] = None
    suspended_reason: Optional[str] = None


def _vue(sub: str, etat: Optional[dict], *, changed: bool) -> dict:
    """Projette l'état de pause. ⚠️ Les horodatages arrivent déjà en chaînes ISO du
    driver (`_str_dict_row`) — leur appliquer `.isoformat()` produirait un 500 que les
    tests à doublure ne verraient jamais."""
    return {
        "sub": sub,
        "suspended": bool(etat),
        "changed": changed,
        "suspended_at": (etat or {}).get("suspended_at"),
        "suspended_by": (etat or {}).get("suspended_by"),
        "suspended_reason": (etat or {}).get("suspended_reason"),
    }


def _account(ctx: ResolvedCtx, inp: AccountSuspensionInput) -> dict:
    cible = inp.target.strip()
    if cible == ctx.sub:
        # On ne se neutralise pas soi-même. Ce n'est pas un anti-lockout (un super
        # admin de plateforme peut toujours réveiller quiconque) : c'est qu'un
        # opérateur qui se coupe l'accès en visant la mauvaise ligne perd aussi le
        # moyen de constater son erreur, et doit demander de l'aide pour la défaire.
        raise AuthzDenied(409, "self_suspend",
                          "On ne met pas son propre compte en pause : le geste "
                          "retire l'accès qui permettrait de le défaire.")
    if not db.get_user(cible):
        raise AuthzDenied(404, "unknown_user", f"Aucun compte `{cible}`.")

    if inp.op == "resume":
        change = db.resume_account(cible)
        logger.info("compte réveillé sub=%s par=%s (change=%s)", cible, ctx.sub, change)
        return _vue(cible, None, changed=change)

    motif = (inp.reason or "").strip()
    if not motif:
        raise AuthzDenied(400, "missing_reason",
                          "`reason` requis : une pause sans motif écrit devient une "
                          "pause que personne ne saura expliquer ni lever.")
    # ⚠️ REFUSER, pas raboter. Ce motif était tronqué à `_MOTIF_MAX` en silence, et le
    # premier gel posé avec cet outil (03/09/2026) s'est arrêté au milieu d'une phrase
    # — emportant sa dernière ligne, celle qui disait à quelle condition réveiller le
    # compte. C'est le pire endroit pour une coupe muette : on écrit la consigne de
    # sortie EN DERNIER, et le motif n'existe que pour être relu dans six mois par
    # quelqu'un qui n'était pas là. La colonne, elle, est en TEXT — la borne est un
    # choix de surface, pas une contrainte de stockage, donc elle doit se dire.
    if len(motif) > _MOTIF_MAX:
        raise AuthzDenied(
            400, "reason_too_long",
            f"`reason` fait {len(motif)} caractères pour {_MOTIF_MAX} au plus. Il est "
            "refusé entier plutôt que raboté : une coupe silencieuse emporterait sa "
            "fin, là où se trouve d'ordinaire la condition de réveil. Raccourcis-le "
            "toi-même, en gardant pourquoi la pause est posée et à quoi on la lève.")
    etat = db.suspend_account(cible, by=ctx.sub, reason=motif)
    # `suspend_account` ne réécrit pas une pause en cours : la comparaison sur
    # l'auteur dit si CE geste est celui qui a posé la pause.
    change = bool(etat) and etat.get("suspended_by") == ctx.sub and \
        etat.get("suspended_reason") == motif
    logger.warning("compte mis en pause sub=%s par=%s motif=%r", cible, ctx.sub, motif)
    return _vue(cible, etat, changed=change)


# Lire et écrire au même palier, à dessein : `TENANT_ADMIN_OF_TARGET` essaie d'abord
# la règle plateforme et ne laisse la main au rôle de tenant que sur un 403. Le
# plancher de visibilité qui en découle est `None` — l'outil entre donc dans la boîte
# de chaque compte. C'est assumé et c'est le prix à payer : masquer un outil ne
# protège rien (ADR 0031/0066-R4) mais fastmcp en refuse aussi l'APPEL, donc un
# admin de tenant à qui on le masquerait ne pourrait tout simplement pas s'en servir
# depuis un agent — et c'est par là qu'un partenaire travaille.
_PALIER = TENANT_ADMIN_OF_TARGET("target", platform=SUPER_ADMIN)

CAPABILITIES += [
    Capability(
        key="admin.account", handler=_account, Input=AccountSuspensionInput,
        Output=AccountSuspensionOut,
        authz=ADMIN_BY_OP({"suspend": _PALIER, "resume": _PALIER}),
        errors=(
            DeclaredError(409, "self_suspend", "la cible est l'appelant lui-même"),
            DeclaredError(404, "unknown_user", "aucun compte ne porte ce sub"),
            DeclaredError(400, "missing_reason", "op=suspend sans `reason`"),
        ),
        description=(
            "Pause an account without deleting anything. op=suspend (`target` = the "
            "account's sub — NOT an email; `reason` required) → the account can no "
            "longer do anything, on any face, from its very next request: a token "
            "issued before the pause stops working immediately. NOTHING is deleted or "
            "detached — org memberships, projects, documents, datastore rows, vault "
            "credentials and journal entries all stay, and still name this account as "
            "their author. The account keeps its seat in member lists, flagged as "
            "paused. What it SHARES (its personal connector accounts, at the levels "
            "their rules give others) stops with it and comes back unchanged on resume. op=resume (`target`) → lifts the pause; it is the only way back, "
            "no automatic mechanism can revive a paused account. Super platform admin "
            "on anyone; a tenant admin on accounts of their OWN tenant, with no "
            "platform role. Not an org admin: an account is not owned by an org, and "
            "pausing it would cut spaces that admin has no title over — to act "
            "on ONE org only, remove the member with oto_admin_org_member."),
        mcp="oto_admin_account",
        # Un SEUL binding, et en POST y compris pour lire l'état : l'adaptateur REST
        # fusionne les paramètres de requête dans l'`Input`, donc un `GET …?op=suspend`
        # muterait. L'état de pause se lit par la fiche du compte
        # (`GET /api/admin/users/{sub}`) et par la liste des membres d'une org, qui le
        # portent tous deux — il n'y a pas de troisième surface à inventer ici.
        rest=RestBinding("POST", "/api/admin/users/{sub}/suspension", {"sub": "target"}),
    ),
]
