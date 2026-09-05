"""Propriété d'un compte Unipile : la garde commune au point d'écriture."""
from __future__ import annotations

from dataclasses import dataclass

from . import db

@dataclass(frozen=True)
class BindOutcome:
    """Ce qu'une tentative de liaison a RÉELLEMENT fait.

    Pas un booléen : le refus a une CAUSE, et un appelant qui ne peut pas la lire ne
    peut pas la journaliser — un refus muet est un refus que personne ne saura avoir
    eu."""

    bound: bool
    reason: "str | None" = None


def account_claimable(sub: str, account_id: str, *,
                      foreign: "set | None" = None) -> bool:
    """Ce `account_id` est-il réclamable par `sub` ?

    **LA garde de toute liaison d'un compte de messagerie hébergée.** Née sur un seul
    des deux chemins qui écrivaient alors (#559) : la réconciliation contrôlait, le
    webhook de notification reprenait `body["account_id"]` tel quel. Le nonce prouve
    « c'est bien la session de connexion de cette personne » ; il ne dit rien de
    « c'est bien le compte qui vient d'être créé ». Et la clé fournisseur étant
    **partagée entre les organisations**, un identifiant quelconque de l'abonnement —
    le siège d'une autre org — était joignable sous cette liaison. Le webhook est
    retiré depuis (#581, 2026-08-29) ; la garde reste, au point d'écriture.

    Règle : un identifiant déjà attribué à QUELQU'UN D'AUTRE, binding vivant ou mort,
    n'est pas réclamable. Une ligne morte d'un tiers vaut interdiction (Unipile
    réutilise le même identifiant à la reconnexion : elle prouve une propriété qui
    dure) ; les lignes du réclamant, elles, ne l'empêchent jamais de reprendre son
    propre compte.

    ⚠️ Ce qu'elle NE couvre pas : un siège présent sur l'abonnement partagé et lié à
    PERSONNE côté oto. Le fermer demande de confronter l'identifiant au fournisseur
    (date de création vs pending) — ce que la réconciliation fait déjà avec son
    plancher de date ; une lecture de compte par identifiant est absente du client
    oto-core.

    `foreign` : l'inventaire déjà chargé par un appelant qui teste N candidats (la
    réconciliation en voit tout l'abonnement). Absent, il est lu ici, au grain."""
    if not account_id:
        return False
    if foreign is not None:
        return account_id not in foreign
    return not db.is_foreign_unipile_account(sub, account_id)


def bind_account(sub: str, account_id: str, *, org_id: "int | None",
                 provider: str = "LINKEDIN", platform_seat: bool = False,
                 account_name: "str | None" = None,
                 foreign: "set | None" = None) -> BindOutcome:
    """Écrire la liaison `(sub, org, canal) → account_id`, **gardée**.

    Le seul chemin d'écriture, et c'est le point : #559 n'était pas une garde oubliée
    mais une garde posée UNE fois sur DEUX écritures parallèles. Les mettre sous la
    même fonction est ce qui empêche une troisième de naître sans elle. Le webhook,
    second écrivain, est retiré (#581, 2026-08-29) ; la garde reste ICI, au point
    d'écriture, pour que le prochain chemin — un webhook v2 signé ? — naisse gardé."""
    if not account_claimable(sub, account_id, foreign=foreign):
        return BindOutcome(False, "account_not_claimable")
    db.set_unipile_account(sub, account_id, account_name=account_name,
                           org_id=org_id, provider=provider,
                           platform_seat=platform_seat)
    return BindOutcome(True)
