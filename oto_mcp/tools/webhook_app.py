"""`oto_webhook_app` — a card in the conversation for ONE webhook agent: its name, whether
it already receives events, the kind of token its sender needs, and a button that opens
the page where the PERSON gets that token.

The visual face of `hook_secret_url` (`capabilities/_secret_par_lien.py`): on the MCP
face, `oto_trigger op=create` (webhook) and `op=rotate_secret` never return the token —
the AI shows this card instead of a bare link it might summarise or drop. The button only
OPENS a link (`OpenLink`, through the host): it calls no tool, carries no secret, and the
token is minted and shown on the page, once.

Same reads, same rights as `oto_trigger op=get` / `op=deliveries` (`_acces_agent`, viewer
and up), and the delivery log is read WITHOUT bodies (third-party data). Same conventions
as the other Prefab apps (`docs/mcp-apps.md`): guarded import, no `-> Card`, two channels,
fixed words on a refusal, no product name. Spine, outside the activation gate.
"""
from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from .. import access, db, links
from ..capabilities import _acces_agent
from ..capabilities._types import AuthzDenied, ResolvedCtx

# A delivery that started (or will start) a run — the sender got through the door.
_ACCEPTEES = {"queued", "delayed"}

# What the sender does with the token, by the agent's preset — the same two kinds
# `_secret_par_lien` can hand out. Other presets have no token of ours to fetch.
_GENRE = {
    "bearer": "Token in the Authorization header",
    "body_secret": "Token in the message's `secret` field",
}

# Fixed words only on a refusal: the id came from the model.
_REFUS = {
    "trigger_not_found": "No webhook agent with that id is visible to you.",
    "not_a_webhook": "That agent starts on a schedule, not on events: it has no webhook.",
}
_REFUS_DEFAUT = "This webhook can't be shown here."


def _alias(sub: str, texte: str) -> str:
    from .. import tool_alias
    prefix = tool_alias.prefix_for(sub)
    return tool_alias.rewrite_prose(texte, prefix) if prefix else texte


def lire(ctx: ResolvedCtx, trigger_id: int) -> dict:
    """What the card shows, read under the caller's own rights. Raises AuthzDenied."""
    t = db.get_trigger(int(trigger_id), ctx.org_id)
    if not t:
        raise AuthzDenied(404, "trigger_not_found", "automatisation inconnue")
    _acces_agent.exiger(ctx, t, _acces_agent.ECRIRE + ("viewer",))
    if (t.get("kind") or "schedule") != "webhook":
        raise AuthzDenied(400, "not_a_webhook", "cet agent n'est pas un webhook")
    mode = t.get("hook_auth") or "bearer"
    livraisons = db.livraisons(int(trigger_id), ctx.org_id, limit=20,
                               en_attente=False, avec_corps=False) or []
    acceptees = [d for d in livraisons if d.get("outcome") in _ACCEPTEES]
    return {
        "id": int(trigger_id),
        "label": t.get("label") or t.get("procedure") or f"Agent {trigger_id}",
        "mode": mode,
        "token": mode in _GENRE,
        "receiving": bool(acceptees),
        "last_event_at": str(acceptees[0].get("received_at")) if acceptees else None,
        "url": links.link_for("webhook", sub=ctx.sub, org=ctx.org_id, id=int(trigger_id)),
    }


def _texte(v: dict) -> str:
    etat = (f"receiving events (last at {v['last_event_at']})" if v["receiving"]
            else "no event received yet")
    lignes = [f"Webhook card shown for “{v['label']}” (agent {v['id']}): {etat}."]
    if v["token"]:
        lignes.append(f"Token kind: {_GENRE[v['mode']]}. You never see the token: the "
                      "person gets it on the page and pastes it into the sending app.")
    else:
        lignes.append(f"Mode `{v['mode']}`: no token to paste — the sender proves itself "
                      "another way, set on the agent's page.")
    lignes.append(f"Link: {v['url']}" if v["url"] else
                  "No link: this product declares no webhook page — the owner uses the "
                  "agent's own screen.")
    return "\n".join(lignes)


def register(mcp: FastMCP) -> None:
    try:
        from prefab_ui.actions import OpenLink  # type: ignore
        from prefab_ui.components import (  # type: ignore
            Badge, Button, Card, Column, Heading, Muted, Row, Separator, Text,
        )
    # noqa: SILENT — extra `apps` absent ⇒ no app, `hook_secret_url` is enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    def _carte_refus(sub: str, e: AuthzDenied) -> ToolResult:
        with Card() as card:
            with Column(gap=3):
                Heading("Cannot show this webhook")
                Text(_REFUS.get(e.code, _REFUS_DEFAUT))
        return ToolResult(content=[TextContent(
            type="text", text=_alias(sub, f"Refused ({e.code}): {e.message}"))],
            structured_content=card)

    @mcp.tool(app=True)
    def oto_webhook_app(trigger_id: int):  # no `-> Card`: see docs/mcp-apps.md (#69)
        """Show a WEBHOOK card for one webhook agent: its name, whether it already
        receives events, the kind of token its sender needs, and a button that opens the
        page where the PERSON gets the token — READ ONLY, never returns a token.

        Show it right after `oto_trigger op=create` (kind=webhook) or `op=rotate_secret`:
        their `hook_secret` is null on purpose, and this card is how the person reaches
        the token. Then wait for them to say it is set up; the card's status (or
        `oto_trigger op=deliveries`) tells you when the first event arrived. Without a
        rendering client, send `hook_secret_url` as a link instead.

        Args:
            trigger_id: the webhook agent's id (`trigger.id` from oto_trigger).
        """
        sub = access.current_user_sub_or_raise()
        ctx = ResolvedCtx(sub=sub, org_id=access.current_org(sub), channel="mcp")
        try:
            v = lire(ctx, trigger_id)
        except AuthzDenied as e:
            return _carte_refus(sub, e)

        if v["receiving"]:
            pastille, variante = "Receiving events", "success"
        elif v["token"]:
            pastille, variante = "Waiting for its token", "warning"
        else:
            pastille, variante = "Set up on its page", "secondary"

        with Card() as card:
            with Column(gap=0):
                with Column(gap=1, css_class="p-4"):
                    with Row(gap=2, align="center"):
                        Text(v["label"], bold=True, css_class="truncate")
                        Badge(pastille, variant=variante)
                    if v["token"]:
                        Muted("The token is never shown to the assistant. Get it on "
                              "the page, then paste it into the app that sends the "
                              "events.")
                    else:
                        Muted("This webhook's sender proves itself another way "
                              f"(`{v['mode']}`): it's set on its page.")
                Separator()
                with Row(gap=2, align="center", justify="between", css_class="px-4 py-3"):
                    if v["url"]:
                        Button("Get the token" if v["token"] and not v["receiving"]
                               else "Open webhook setup",
                               variant="default" if not v["receiving"] else "outline",
                               size="sm", on_click=OpenLink(v["url"]))
                    else:
                        Muted("No webhook page for your account: use the agent's own "
                              "screen.")
                    if v["token"]:
                        Muted(_GENRE[v["mode"]])
        return ToolResult(content=[TextContent(type="text", text=_alias(sub, _texte(v)))],
                          structured_content=card)
