"""Résoudre une adresse en COMPTE — le seul endroit qui sache qu'elle peut mentir.

⚠️ Une adresse ne désigne pas un compte. Mesuré le 05/09/2026 : dix adresses en
portent deux, vingt comptes concernés, dont une paire **sans aucun tenant** — ce
n'est donc pas une conséquence de la qualification par émetteur (ADR 0052), c'est
une propriété de la résolution par adresse.

`db.get_user_by_email` rend `fetchone()`, dans un ordre que rien ne fixe. Six
surfaces le lisaient pour DÉCIDER : partager un tableau, accorder un accès
connecteur, suspendre un compte, changer un rôle, ajouter un membre, filtrer une
mesure. Chacune choisissait un porteur en silence.

Ce module existe pour que la garde ne soit pas recopiée une septième fois : la
prochaine surface qui résout une adresse trouvera la question déjà posée.

⚠️ Ce qu'il ne fait PAS : décider quoi répondre quand personne ne porte l'adresse.
Chaque surface a son code (404 ici, 400 là), et les uniformiser serait un
changement de contrat déguisé en refactorisation. Elles reçoivent une liste vide
et lèvent ce qu'elles levaient.
"""
from __future__ import annotations

from .. import db
from ._types import AuthzDenied


def porteurs_de(email: str) -> list[dict]:
    """Tous les comptes portant cette adresse — et un REFUS s'il y en a plusieurs.

    Rend une liste (vide si personne) plutôt qu'un compte : l'appelant garde le
    choix de son refus pour l'absence, mais plus celui d'ignorer l'ambiguïté.
    """
    porteurs = db.get_users_by_email((email or "").strip())
    if len(porteurs) > 1:
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise AuthzDenied(
            400, "ambiguous_email",
            f"L'adresse `{email}` désigne {len(porteurs)} comptes : {subs}. "
            "Une adresse n'identifie pas un compte — deux émetteurs peuvent la "
            "porter, et deux comptes ordinaires aussi. Reprends avec le `sub` de "
            "celui que tu vises.")
    return porteurs
