"""Le porteur d'un webhook, remis à un AGENT : selon l'instance, un lien ou le porteur.

`oto_trigger op=create` (un webhook) et `op=rotate_secret` frappent un porteur neuf. Le
rendre EN CLAIR dans la réponse le met dans le contexte du modèle, l'historique du
client et ses journaux. Une instance qui sert un écran où une PERSONNE tire le porteur
le dit en déclarant le type de lien `webhook` (`OTO_DASHBOARD_LINK_PATHS`) ; ce choix
d'instance, explicite, décide de ce que reçoit un agent :

- **lien `webhook` déclaré** : sur la face MCP (`ctx.channel == "mcp"`, EXPLICITE —
  jamais « pas rest »), le porteur frappé est JETÉ : seul son haché est posé, et
  personne ne le détient. La porte est FERMÉE — toute livraison est refusée tant
  qu'une personne n'a pas tiré un porteur neuf depuis l'écran, où le lien rendu mène.
  Rien n'est stocké en clair, rien n'expire, rien n'est à purger. Un chemin déclaré
  qu'on ne sait pas rendre ici est une faute dite (`hook_secret_missing`), jamais un
  repli sur le porteur.
- **lien `webhook` non déclaré** : l'instance n'a pas cet écran, et l'agent reçoit le
  porteur, une fois, dans `hook_secret` — c'est le seul chemin par lequel une personne
  l'obtient. Ce n'est pas un repli qui masque une erreur : c'est la configuration de
  l'instance, lue à chaque appel.

La face REST (l'écran de l'agent) reçoit le porteur dans les deux cas : c'est la
personne qui le lit, une fois.
"""
from __future__ import annotations

from .. import links
from ._types import ResolvedCtx

# Le TYPE de porteur frappé, selon le préréglage de l'agent — ce que la personne devra
# en faire. La page qu'ouvre le lien le relit sur le SERVEUR (le préréglage de l'agent),
# jamais dans le lien : un lien se recopie, l'état de l'agent fait foi. Un préréglage
# qui ne porte pas NOTRE porteur (signature, sans preuve, Slack) n'arrive jamais ici :
# `rotate_secret` le refuse, `create` naît en `bearer`, `hook-auth` n'a pas de face MCP.
GENRES = {
    "bearer": "bearer token (the sender sends it in an `Authorization: Bearer` header)",
    "body_secret": "token the sender puts in each message, as a top-level `secret` field",
}

NOTE = ("The token was NOT returned to you: an AI never holds a webhook's token. Send "
        "`hook_secret_url` to the person who sets up the sending app — they generate "
        "the token on that page, which guides them for this agent's mode "
        "(`hook_secret_kind`), and paste it there, never in the chat. If your client "
        "renders cards, show `oto_webhook_app(trigger_id=…)` instead of the bare link. "
        "Until they do, the webhook refuses every delivery.")


def remettre(ctx: ResolvedCtx, agent: dict, secret: str) -> dict:
    """Les clés à fusionner dans la réponse : le porteur à une personne (REST) et à un
    agent d'une instance sans écran du porteur ; un lien à un agent (MCP) d'une instance
    qui le déclare. `agent` = la ligne du déclencheur (son id, son préréglage). Le
    secret n'est jamais journalisé ici."""
    if ctx.channel != "mcp" or not links.lien_declare("webhook", sub=ctx.sub):
        return {"hook_secret": secret}
    trigger_id = agent["id"]
    genre = agent.get("hook_auth") or "bearer"
    url = links.link_for("webhook", sub=ctx.sub, org=ctx.org_id, id=trigger_id)
    out: dict = {"hook_secret": None, "hook_secret_url": url,
                 "hook_secret_kind": genre,
                 "hook_secret_note": NOTE + f" Kind: {GENRES.get(genre, genre)}."}
    if url is None:
        raison = links.raison_sans_lien("webhook", sub=ctx.sub, org=ctx.org_id,
                                        id=trigger_id)
        # Le chemin est DÉCLARÉ mais ne se rend pas ici (un paramètre manque, une base
        # d'adresse absente) : une faute de configuration, dite telle quelle. Le texte ne
        # nomme aucun écran ni menu — l'agent ne renvoie pas la personne vers un bouton
        # qu'il n'a pas pu adresser. La porte reste fermée.
        out["hook_secret_missing"] = (
            f"{raison}. The token cannot be retrieved for now, so the webhook refuses "
            "every delivery. Tell the agent's owner; do not retry `create` or "
            "`rotate_secret`, which would only mint another token nobody holds.")
    return out
