"""Capacité admin : la session d'une instance de connecteur est-elle vivante ?
(oto-backend#863, ADR 0009)

Quand une personne signale que son connecteur à session ne marche plus, la première
question du support est toujours la même : **sa session est-elle encore valide, oui ou
non ?** De la réponse dépend tout le reste — soit elle n'a rien à faire, soit elle doit
refaire son login.

Jusqu'ici aucune surface n'y répondait pour un tiers : un outil résout toujours le
credential de CELUI QUI L'APPELLE, sans paramètre ni privilège pour sonder l'instance
de quelqu'un d'autre. La seule voie était de se connecter à la machine de production,
faire déchiffrer le credential par le service et piloter le navigateur à la main.
Vécu le 2026-09-03 : une utilisatrice a relancé **six fois** la connexion d'un
connecteur dont la session n'avait jamais cessé d'être valide ; établir ce fait a
demandé un accès serveur et un script jetable, écrit à chaud pendant qu'elle attendait.

Ce que ce chemin-là coûtait, et que celui-ci supprime : aucune trace produit (seuls les
journaux du service tiers en gardaient une empreinte), aucune borne (le script faisait
ce que son auteur décidait — la retenue était une discipline, pas une garantie), et il
était réservé aux porteurs de clés, donc fermé à un support qui n'a pas d'accès serveur
alors que c'est son travail.

## Les quatre bornes, et pourquoi chacune

**Sonde DÉCLARÉE par le connecteur, jamais une requête au choix de l'appelant.** On
nomme une instance, pas une URL. Un « agir en tant que » générique convertirait une
porte qui exige un accès à la machine de production en une porte qu'un jeton
d'administration ouvre à distance : le jour où ce jeton fuit, tous les accès
utilisateurs deviennent exploitables de n'importe où. La lourdeur du chemin serveur EST
un garde-fou — les cas qui dépassent la sonde y restent, avec leur friction assumée.

**Aucune donnée métier.** Un verdict et son motif. Rien d'autre n'est lu, donc rien
d'autre ne peut fuiter par ici.

**Lecture seule.** Aucune écriture n'est atteignable par ce chemin.

**Journalisée** : qui, quand, quelle instance, quel verdict — `tool_calls` porte
l'appelant et l'argument, ce module ajoute le VERDICT en clair dans son journal, parce
qu'un appel tracé dont on ignore la réponse ne dit pas ce qui a été appris.

## Le mandat de l'utilisateur — tranché le 04/09/2026

Se servir de l'accès de quelqu'un pour le diagnostiquer reste se servir de son accès,
même en lecture et même pour l'aider. Décision d'Alexis : **pas de consentement
préalable** (le support sonde quand il en a besoin — la question se pose justement quand
la personne est bloquée), et **pas de surface de restitution** : le diagnostic est un
acte de support ordinaire, comme lire un journal d'erreur.

⚠️ Ce qui rend ce régime tenable n'est donc pas le consentement, c'est **l'étroitesse de
ce qui est lisible par ici**. Élargir la sonde — un jour, pour un cas qui « dépasse » —
retirerait la seule chose qui la justifie. C'est la borne à défendre, pas la trace.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from .. import browser_session, credentials_store, db
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class InstanceHealthInput(BaseModel):
    instance_id: int = Field(
        description=("Identifiant de l'instance de connecteur à sonder (celle d'un "
                     "TIERS : c'est tout l'objet). On nomme une instance, jamais une "
                     "requête — la sonde exécutée est celle que le connecteur déclare."))


class InstanceHealth(BaseModel):
    """Le verdict, et de quoi le lire sans rien connaître d'autre.

    ⚠️ `connected` seul ne suffit pas et n'a jamais suffi : les causes d'un refus
    appellent des conduites OPPOSÉES — finir un login en cours, en refaire un rejeté,
    ou surtout ne PAS recommencer. C'est ce que `reason`/`retry` portent, et c'est
    exactement ce dont l'absence a fait relancer six fois une session valide."""
    instance_id: int
    connector: str
    owner_type: str
    owner_id: str
    account: str = ""
    #: `null` = la sonde n'a rendu AUCUN verdict (`ProbeUnavailable`). Ce n'est PAS
    #: « pas connecté » — c'est « je ne sais pas », et les confondre a déjà coûté une
    #: matinée : une sonde muette avait rendu un connecteur inconnectable pour tous.
    connected: Optional[bool] = None
    reason: str
    detail: str = ""
    #: `false` = inutile de refaire un login, le problème n'est pas chez la personne.
    retry: bool = True


async def _instance_health(ctx: ResolvedCtx, inp: InstanceHealthInput) -> dict:
    inst = db.connector_instances.instance_by_id(int(inp.instance_id))
    if inst is None:
        raise AuthzDenied(404, "unknown_instance",
                          f"Instance #{inp.instance_id} inconnue.")
    connector = str(inst["connector"])
    owner_type, owner_id = str(inst["owner_type"]), str(inst["owner_id"])
    account = str(inst.get("account") or "")

    # ⚠️ Une instance ARCHIVÉE se dit, elle ne se sonde pas. `instance_by_id` les rend
    # exprès (« retirée » n'est pas « n'a jamais existé ») — et la sonder ouvrirait une
    # session sur un contexte que son propriétaire a justement révoqué.
    if inst.get("revoked_at"):
        return {"instance_id": int(inp.instance_id), "connector": connector,
                "owner_type": owner_type, "owner_id": owner_id, "account": account,
                "connected": False, "reason": "instance_revoked",
                "detail": (f"Instance retirée le {inst['revoked_at']}"
                           + (f" ({inst['revoked_reason']})" if inst.get("revoked_reason") else "")
                           + " — il n'y a plus de session à sonder."),
                "retry": False}

    context_id = credentials_store.get_credential(owner_type, owner_id, connector,
                                                  account=account)
    if not context_id:
        # Le coffre ne rend rien : soit rien n'a jamais été posé, soit la ligne ne se
        # déchiffre plus (master key périmée). On ne devine pas laquelle — dire
        # « déconnecté » ferait refaire un login qui ne réparerait pas une corruption.
        return {"instance_id": int(inp.instance_id), "connector": connector,
                "owner_type": owner_type, "owner_id": owner_id, "account": account,
                "connected": None, "reason": "no_credential",
                "detail": ("Aucun credential lisible pour cette instance — jamais posé, "
                           "ou illisible (voir `oto_admin_vault_health`)."),
                "retry": False}

    base = {"instance_id": int(inp.instance_id), "connector": connector,
            "owner_type": owner_type, "owner_id": owner_id, "account": account}
    try:
        v = await browser_session.sonder(connector, context_id, account)
    except browser_session.ProbeUnavailable as e:
        # « Je ne sais pas » — surtout pas « pas connecté ». C'est la distinction qui a
        # coûté une matinée le 2026-09-03 quand elle n'existait pas.
        logger.warning("sonde #%s (%s) sans verdict : %s", inp.instance_id, connector, e)
        return {**base, "connected": None, "reason": "probe_unavailable",
                "detail": str(e), "retry": False}
    except browser_session.SessionError as e:
        raise AuthzDenied(400, "not_a_session_connector", str(e))

    # Le journal du module porte le VERDICT : `tool_calls` garde qui a appelé et avec
    # quel argument, jamais ce qui a été répondu. Un diagnostic tracé dont on ignore la
    # réponse ne dit pas ce qui a été appris — or c'est la réponse qui a décidé de la
    # conduite tenue ensuite envers la personne.
    logger.info("sonde instance #%s (%s, %s:%s) par %s → connected=%s reason=%s",
                inp.instance_id, connector, owner_type, owner_id, ctx.sub,
                v.connected, v.reason)
    return {**base, "connected": bool(v.connected), "reason": v.reason,
            "detail": v.detail, "retry": bool(v.retry)}


CAPABILITIES += [
    Capability(
        key="admin.instance_health", handler=_instance_health,
        Input=InstanceHealthInput, Output=InstanceHealth,
        authz=PLATFORM_ADMIN,
        description=(
            "[platform admin] Is a THIRD PARTY's connector session still alive? Runs "
            "the login probe the connector itself declares, against that instance's "
            "stored browser context, and returns a verdict — connected / rejected "
            "upstream / probe unavailable — with the reason and whether logging in "
            "again would help. Answers the support question 'must they reconnect, or "
            "is it something else?' without an SSH session and a throwaway script. "
            "⚠️ It runs ONLY the declared probe, never a request you choose: no "
            "business data is read, nothing is written, and the browser session it "
            "opens is ephemeral. `connected: null` means the probe gave NO verdict — "
            "that is 'unknown', not 'logged out'; telling someone to log in again on "
            "that basis is how a whole morning was lost."),
        mcp="oto_admin_instance_health",
        rest=RestBinding("POST", "/api/admin/instances/{instance_id:int}/health"),
    ),
]
