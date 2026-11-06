"""`oto_connector_app` — a card in the conversation: the connector's name, what the
person will do, and a Connect button that opens its page in their product.

The visual face of `oto_connector(op="setup_link")` (same handler, same refusals): the
agent that needs a key shows the card instead of asking for the key. The button only
OPENS a link (`OpenLink`, through the host) — it calls no tool, writes nothing, and the
key is typed on the page, never in the card nor in the chat.

Same conventions as the other Prefab apps (`docs/mcp-apps.md`): guarded `prefab_ui`
import, no `-> Card` return annotation, two channels (the card to the host, the same
facts as text to the model), every colour left to the host. Spine, outside the
activation gate.
"""
from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from .. import access, links
from ..capabilities._types import AuthzDenied, ResolvedCtx

_BOUTON = {"oauth": "Connect", "hosted": "Log in", "none": "Add to my tools"}

# What a REFUSAL card says, by code — fixed words only. The refusal's own message
# repeats the `name` the model passed (and a Custom API link carrying it): shown in a
# card the host paints as ours, a prompt-injected agent could put "your session
# expired, re-enter your key at <elsewhere>" under our heading. The message goes to
# the MODEL (text channel); the person reads only this.
_REFUS = {
    "unknown_connector": "No connector with that name is available to your workspace.",
    "ambiguous_connector": "That name matches several connectors.",
    "use_http_setup_link": "This tool connects through the Custom API form instead.",
}
_REFUS_DEFAUT = "This connector can't be shown here."

# Logos the card may load: the iframe's CSP admits these origins only (the same as
# `oto_procedure_app`). Any other logo URL draws the connector's initial instead of a
# broken image.
_LOGO_ORIGINS = ("https://img.logo.dev",)
_CSP_LOGOS = list(_LOGO_ORIGINS)


def _logo(url) -> str | None:
    return url if isinstance(url, str) and url.startswith(
        tuple(o + "/" for o in _LOGO_ORIGINS)) else None


def _pastille(presence: dict | None) -> tuple[str, str]:
    """(label, Badge variant) — the chip the product's own card shows: who answers
    for THIS person, read from the same snapshot as `oto_connector op=list`."""
    if not presence:
        return "Not set up", "warning"
    if presence.get("status") == "pending_step":
        return "One step left", "warning"
    if presence.get("level") in ("platform", "tenant"):
        return "Included", "secondary"
    return "Connected", "success"


def _presence(sub: str, org, connector: str) -> dict | None:
    """This person's credential for the connector, or None — never a guess: an
    unreadable snapshot is logged by `credential_presence` and reads as no chip
    change (Not set up), the card's button stays the way in."""
    from ..connectors import credential_presence
    presences, _etat = credential_presence.lire(sub, org=org)
    return presences.get(connector)


def _alias(sub: str, texte: str) -> str:
    """Tool names as THIS account's product spells them (white-label prefix), like
    `oto_procedure_app` — never our names to a partner's end user."""
    from .. import tool_alias
    prefix = tool_alias.prefix_for(sub)
    return tool_alias.rewrite_prose(texte, prefix) if prefix else texte


def _texte(out: dict) -> str:
    """What the model reads: the same facts as the card, and the link."""
    lignes = [f"{out['label']} (`{out['connector']}`) — connection card shown to the user."]
    if out.get("url"):
        lignes.append(f"Link: {out['url']}")
    else:
        lignes.append(f"No link: {out.get('url_missing') or 'no connectors page'}.")
    if out.get("fields"):
        lignes.append("The form asks for: " + ", ".join(f["label"] for f in out["fields"]))
    lignes.append(out["instructions"])
    return "\n".join(lignes)


def register(mcp: FastMCP) -> None:
    try:
        from prefab_ui.actions import OpenLink  # type: ignore
        from prefab_ui.components import (  # type: ignore
            Badge, Button, Card, Column, Div, Heading, Image, Muted, Row, Separator,
            Text,
        )
        from fastmcp.apps.config import PrefabAppConfig, ResourceCSP
    # noqa: SILENT — extra `apps` absent ⇒ no app, `oto_connector op=setup_link` is enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    from ..capabilities.connectors import setup_link as _setup

    def _carte_refus(sub: str, e: AuthzDenied) -> ToolResult:
        with Card() as card:
            with Column(gap=3):
                Heading("Cannot show this connector")
                Text(_REFUS.get(e.code, _REFUS_DEFAUT))
        return ToolResult(content=[TextContent(
            type="text", text=_alias(sub, f"Refused ({e.code}): {e.message}"))],
            structured_content=card)

    @mcp.tool(app=PrefabAppConfig(csp=ResourceCSP(resource_domains=_CSP_LOGOS)))
    def oto_connector_app(name: str):  # no `-> Card`: see docs/mcp-apps.md (gotcha #69)
        """Show a CONNECT card for one connector: its name, what the person will do
        (paste a key, approve access, log in) and a Connect button that opens the
        connector's page in their product — READ ONLY, takes no secret.

        Use it instead of asking for an API key, password or token in the chat: the
        person clicks, connects on the page, and tells you when it is done. Then check
        with `oto_connector(op="list", name=…)` (`credential`). The text result gives
        YOU the link and the fields. Without a rendering client, use
        `oto_connector(op="setup_link", name=…)` and send the `url`.

        Args:
            name: the connector — exact name, label or namespace (e.g. "hunter",
                "HubSpot"). The Custom API has its own link: `http(op="setup_link")`.
        """
        sub = access.current_user_sub_or_raise()
        ctx = ResolvedCtx(sub=sub, org_id=access.current_org(sub))
        try:
            out = _setup.setup_link(ctx, name)
        except AuthzDenied as e:
            return _carte_refus(sub, e)

        label, variant = _pastille(_presence(sub, ctx.org_id, out["connector"]))
        a_connecter = label != "Connected"
        page = links.connector_setup_link(out["connector"], sub=sub, org=ctx.org_id)
        logo = _logo(out.get("logo_url"))

        # The layout of Ask Tulina's connector card (front, connector-chat-card):
        # logo · name + status chip · two lines of description, then a footer with
        # the action on the left and the full connector page on the right.
        with Card() as card:
            with Column(gap=0):
                with Row(gap=3, align="start", css_class="p-4"):
                    if logo:
                        Image(src=logo, alt="", width="36px", height="36px",
                              css_class="shrink-0 rounded-lg border bg-white p-1")
                    else:
                        with Div(css_class="flex size-9 shrink-0 items-center justify-center "
                                           "rounded-lg bg-muted"):
                            Text(out["label"][:1].upper(), bold=True)
                    with Column(gap=1, css_class="min-w-0 flex-1"):
                        with Row(gap=2, align="center"):
                            Text(out["label"], bold=True, css_class="truncate")
                            Badge(label, variant=variant)
                        if out.get("description"):
                            Muted(out["description"], css_class="line-clamp-2")
                        if a_connecter and out.get("fields"):
                            Muted("You will be asked for: "
                                  + ", ".join(f["label"] for f in out["fields"]))
                Separator()
                with Row(gap=2, align="center", justify="between", css_class="px-4 py-3"):
                    if out.get("url"):
                        Button(_BOUTON.get(out["method"], "Connect") if a_connecter
                               else "Add another account",
                               variant="default" if a_connecter else "outline",
                               size="sm", on_click=OpenLink(out["url"]))
                    else:
                        Muted("No connection page for your account: "
                              + (out.get("url_missing") or "ask your admin."))
                    if page:
                        # No product name: a partner's product serves this card too.
                        Button("Open full page →", variant="ghost", size="sm",
                               on_click=OpenLink(page))
                if a_connecter and out.get("url"):
                    Muted("You enter your key on that page — never in the chat.",
                          css_class="px-4 pb-3")
        return ToolResult(content=[TextContent(type="text", text=_alias(sub, _texte(out)))],
                          structured_content=card)
