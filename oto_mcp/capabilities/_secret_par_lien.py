"""Le porteur d'un webhook, remis à un AGENT : un lien, jamais le secret.

`oto_trigger op=create` (un webhook) et `op=rotate_secret` frappent un porteur neuf et
le rendaient EN CLAIR dans la réponse — donc dans le contexte du modèle, l'historique
du client et ses journaux. C'est le miroir, en SORTIE, de la règle « un secret brut ne
passe jamais en argument MCP » : un secret frappé par le serveur ne passe pas non plus
par la réponse d'un outil.

Sur la face MCP (`ctx.channel == "mcp"`, EXPLICITE — jamais « pas rest »), le porteur
frappé est JETÉ : seul son haché est posé, et personne ne le détient. La porte est donc
FERMÉE, pas ouverte — toute livraison est refusée tant qu'une personne n'a pas tiré un
porteur neuf depuis l'écran de l'agent, où le lien rendu mène (type de lien `webhook`,
déclaré par instance). Rien n'est stocké en clair, rien n'expire, rien n'est à purger :
le serveur sait refrapper un porteur à la demande, il n'a pas à en garder un.

La face REST (l'écran de l'agent) reçoit le porteur comme avant : c'est la personne
qui le lit, une fois.
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
    """Les clés à fusionner dans la réponse : le porteur à une personne (REST), un lien
    à un agent (MCP). `agent` = la ligne du déclencheur (son id, son préréglage). Le
    secret n'est jamais journalisé ici."""
    if ctx.channel != "mcp":
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
        out["hook_secret_missing"] = (
            f"{raison}. The agent's owner (or an org admin) gets the token from the "
            "agent's own screen in the product: Webhook ▸ Regenerate.")
    return out
