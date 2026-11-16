"""Procedure — the rendered GRAPH (`oto_procedure_app`): a procedure's drawing, drawn
in the conversation as the partner front's process page draws it. READ-ONLY.

**Per instance, by configuration.** The drawing is optional in the core (18/09/2026: a
partner's rendering need is not a core rule), and its grammar lives in ONE place, the
partner's front (`procedure_diagram.py` refuses a Python twin). So this module draws
nothing itself: its ui:// page is a shell that loads the front's own bundle, and the
tool only ships the drawing. `OTO_PROCEDURE_APP_BUNDLE` (the bundle's URL, e.g.
`https://app.example.com/mcp-app/process-graph.js`, its stylesheet next to it as
`.css`) is both the address and the switch: unset, nothing registers — an instance
without such a front serves no app pointing at nobody. No tenant axis (frozen,
`docs/tenants.md`): one instance, one front.

Not a Prefab card (`docs_app`, `data_review_app`): Prefab renders its own component
tree, and the drawing needs the front's parser and canvas. A plain MCP Apps resource
(SEP-1865), whose CSP admits the bundle's origin and the connector logos.

**Two channels** (cf. `docs_app`, signal #1083): `structuredContent` feeds the view
(the drawing + the connectors it may name, for their logos) and is kept by
`un_seul_canal` because the tool declares `_meta.ui.resourceUri`; the MODEL reads one
line of text pointing at `oto_procedure op=get` for the steps.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

URI = "ui://oto/procedure-graph.html"

# Logos come from the registry (logo.dev for almost all); a host that blocks one
# shows the front's fallback mark instead — never a broken image.
_LOGO_DOMAINS = ("https://img.logo.dev",)

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="{css}">
</head>
<body>
<script src="{js}"></script>
</body>
</html>
"""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _connectors_named(trace: str, org_id: int | None) -> list[dict]:
    """The registry connectors the drawing may name — a SUPERSET of what the front's
    matching will keep (a namespace as a token, or the label as a word), so the view
    re-runs the real matching on a roster of a few entries instead of 600 KB. Through
    the org's effective overlay, like every surface that serves the catalogue (#732)."""
    from .. import providers
    from ..connectors import cardinality
    texte = trace.lower()
    mots = set(re.findall(r"[a-z0-9]+", texte))
    retenus = []
    for c in cardinality.overlay_for_org(providers.public_catalog(), org_id):
        namespaces = c.get("namespaces") or []
        label = (c.get("label") or "").lower()
        if any(ns in mots or f"{ns}_" in texte for ns in namespaces) or (
                label and label in texte) or c["name"] in mots:
            retenus.append({
                "name": c["name"], "label": c.get("label") or c["name"],
                "category": c.get("category") or "", "logo_url": c.get("logo_url"),
                "namespaces": namespaces, "state": "active",
            })
    return retenus


def _peut_ecrire(sub: str, scope: str) -> bool:
    """Would `oto_procedure op=set` at this scope be allowed? The same rule as its
    guard (`procedure_console._ECRIRE`): mine always, the team as a member (it was read
    as the ACTIVE team), the org as its admin. Asked here so a member who cannot write
    is not sent through three calls to a refusal."""
    if scope != "org":
        return True
    from .. import access, roles
    org_id = access.current_org(sub)
    return org_id is not None and roles.is_org_admin(sub, org_id)


def _page_du_process(sub: str, slug: str) -> str | None:
    """The process page in the front that hosts the caller's org (it opens on the graph
    when there is one) — the same address as the readers digest (`digest_lecteurs`).
    `None` when no front is known: no link rather than a guessed one."""
    from .. import access, config, org_store
    org_id = access.current_org(sub)
    if not org_id:
        return None
    base = org_store.org_front(org_id)[0] or config.dashboard_url_for(sub)
    return f"{base.rstrip('/')}/org/{org_id}/processes/{slug}" if base else None


def _aux_noms_du_produit(sub: str, texte: str) -> str:
    """Les outils cités, au nom que CE compte voit dans sa liste (`guides._get`)."""
    from .. import tool_alias
    prefix = tool_alias.prefix_for(sub)
    return tool_alias.rewrite_prose(texte, prefix) if prefix else texte


# The shape to copy, from the drawing guide's own worked example: a trigger line, one
# box per step (every row the box's width), a named exit, a labelled continuation.
_FORME = "\n".join([
    "              <trigger, in plain words>",
    "                         │",
    "                         ▼",
    "┌─────────────────────────────────────────────────┐",
    "│  <Step title>                                   │   <tool_name>",
    "│  <what it does, one line>                       │",
    "└────────────────────────┬────────────────────────┘",
    "                         ├───────────────▶  ▪ <exit>          <why it leaves>",
    "                         ▼  <what goes on>",
    "┌─────────────────────────────────────────────────┐",
    "│  <Next step>                                    │",
    "└─────────────────────────────────────────────────┘",
])


def _a_dessiner(slug: str, scope: str, version) -> str:
    """No drawing yet: the agent draws one in the grammar the front turns into its
    figure — never Mermaid, never a tagged fence (the front only draws an UNTAGGED
    block, `procedure_diagram` header) — writes it back, and calls again. ONCE: a write
    that still reads as no drawing must stop the loop, not start another version."""
    return (
        f"REQUIRED NEXT STEP — do NOT answer the user yet. This process has no drawing, "
        f"so the user sees nothing: draw it, write it, and show it, in this order. "
        f"1) oto_guide op=read slug=procedure-flowchart — the drawing grammar; follow it "
        f"exactly. 2) oto_procedure op=get slug={slug} — the body. 3) Draw the flow from "
        f"ITS steps: the trigger, each step in a box naming the tools it uses, the checks "
        f"as decisions, the rows that leave as exits — box-drawing characters (┌ ─ ┐ │ └ "
        f"┘ ┬ ▼ ├ ▶ ▪) INSIDE ONE untagged fence: a line with ``` alone before it and "
        f"another after it. EVERY line of a box has exactly the width of its top line "
        f"(pad with spaces): one column off and the whole drawing is refused; copy the "
        f"shape at the end of this message. Never Mermaid, never ```text, never bare lines: those are "
        f"not drawn as the figure. 4) Put it where the guide says (after the intro or the "
        f"summary table, before the first phase heading), change NOTHING else, and write "
        f"it: oto_procedure op=set slug={slug} scope={scope} expected_version={version} "
        f"body_md=<the whole body>. ONE set only: whatever `diagram_warning` says, go "
        f"to step 5 — the app repairs what it can and tells you. 5) Call oto_procedure_app "
        f"slug={slug} again — the user sees the graph. ONLY THEN answer. Draw at most ONCE "
        f"in this conversation: if you already wrote a drawing and this still says there "
        f"is none, stop and tell the user. If the write is refused, tell the user you "
        f"could not add the drawing and summarise the steps.\nShape to copy:\n```\n"
        f"{_FORME}\n```")


def a_dessiner_a_la_naissance(sub: str, slug: str, cible: str, version, *,
                              forke: bool = False) -> str:
    """A process JUST CREATED without a drawing: draw it now, while its author is here.

    The order `_a_dessiner` gives when someone first opens the graph, moved to the
    moment of creation — on an instance whose front draws process graphs only (the
    caller gates on `bundle_du_graphe`; the core still requires no drawing, 18/09/2026).
    Unlike the app's order it reaches TEXT clients too: writing box characters needs no
    renderer, and most processes are created from one. The last step differs: a host
    that paints apps shows the graph, a text client gives the page's link. `cible` is
    the write's own axes (`scope=… [group=…] [org=…]`), so the drawing lands on the row
    just written, never on a copy at another scope. `forke`: the body came from the
    library, the agent has not read it — and usually adapts it next, so the drawing
    rides on THAT write rather than adding a version of its own."""
    from ..procedure_diagram import client_affiche_les_apps
    if client_affiche_les_apps():
        montrer = (f"5) Call oto_procedure_app slug={slug} — the user sees the graph. "
                   "ONLY THEN answer.")
    else:
        url = _page_du_process(sub, slug)
        montrer = ("5) Then answer, and tell the user the graph is on the process page"
                   + (f": {url}" if url else "") + ".")
    if forke:
        quand = ("this process was just forked WITHOUT a drawing, and its page opens on its "
                 "graph: add the drawing in your NEXT write of it — the adaptation, if you "
                 "adapt it now; otherwise right away, before you answer. ")
        source = (f"the steps of its body (oto_procedure op=get slug={slug} {cible}), as "
                  "adapted")
    else:
        quand = ("this process was just created WITHOUT a drawing, and its page opens on "
                 "its graph: add the drawing now, before you answer. ")
        source = "the steps you just wrote"
    texte = (
        f"REQUIRED NEXT STEP — {quand}"
        f"1) oto_guide op=read slug=procedure-flowchart — follow its grammar and worked "
        f"example exactly (only the drawing: add nothing else it mentions). 2) Draw the "
        f"flow from {source}: the trigger, each step in "
        f"a box naming the tools it uses, the checks as decisions, the rows that leave as "
        f"exits — box-drawing characters (┌ ─ ┐ │ └ ┘ ┬ ▼ ├ ▶ ▪) INSIDE ONE untagged "
        f"fence: a line with ``` alone before it and another after it. EVERY line of a box "
        f"has exactly the width of its top line (pad with spaces): one column off and the "
        f"whole drawing is refused; copy the shape at the end of this message. Never "
        f"Mermaid, never ```text, never bare lines. 3) Put it after the intro (or the "
        f"summary table), before the first step heading, change NOTHING else. "
        f"4) oto_procedure op=set slug={slug} {cible} expected_version={version} "
        f"body_md=<the whole body>. If `diagram_warning` says the drawing will not render, "
        f"fix exactly what it names and set ONCE more — never a third write; if a write is "
        f"refused, tell the user the drawing could not be added. {montrer}"
        f"\nShape to copy:\n```\n{_FORME}\n```")
    return _aux_noms_du_produit(sub, texte)


def _a_encadrer(slug: str, scope: str, version) -> str:
    """The drawing was written bare, outside any fence: wrap it, never redraw it."""
    return (f"The drawing is in the body but NOT inside a ``` fence, so the page does not "
            f"draw it. Fix ONLY that: oto_procedure op=get slug={slug}, put a line with ``` "
            f"alone right before the drawing's first line and another right after its last "
            f"line, change nothing else, then oto_procedure op=set slug={slug} scope={scope} "
            f"expected_version={version} body_md=<the whole body>, and call "
            f"oto_procedure_app slug={slug} again. If it still says so, stop and tell the user.")


def _a_detaguer(slug: str, scope: str, version, lang: str) -> str:
    """The drawing is there, under a tag the page does not draw: one precise fix,
    never a fresh drawing (that would add a version every call)."""
    return (f"The drawing is in a ```{lang} fence, which the page does not draw. Fix "
            f"ONLY that: oto_procedure op=get slug={slug}, remove the `{lang}` tag from "
            f"that fence (keep ``` alone), change nothing else, then oto_procedure op=set "
            f"slug={slug} scope={scope} expected_version={version} body_md=<the whole body>, "
            f"and call oto_procedure_app slug={slug} again. If it still says so, stop and "
            f"tell the user.")


def register(mcp: FastMCP) -> None:
    from ..procedure_diagram import bundle_du_graphe
    bundle = bundle_du_graphe()
    if bundle is None:
        return
    try:
        from fastmcp.apps import AppConfig, ResourceCSP, UI_MIME_TYPE
    # noqa: SILENT — extra `apps` absent ⇒ no app, oto_procedure is enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    page = _SHELL.format(js=html.escape(bundle, quote=True),
                         css=html.escape(bundle[:-3] + ".css", quote=True))

    @mcp.resource(URI, name="procedure_graph", mime_type=UI_MIME_TYPE,
                  app=AppConfig(csp=ResourceCSP(resource_domains=[_origin(bundle), *_LOGO_DOMAINS]),
                                prefers_border=True))
    def procedure_graph_page() -> str:
        return page

    @mcp.tool(app=AppConfig(resource_uri=URI))
    def oto_procedure_app(slug: str) -> ToolResult:
        """SHOW a process to the user: draws the procedure's process graph INLINE in the
        conversation (MCP App) — trigger, steps, checks and the connectors each step uses.

        ALWAYS call it, FIRST, whenever the user asks about a process or procedure:
        to see it, understand it, explain it, review it, or after you create or change
        one. Then answer in text — the graph replaces no explanation. It gives YOU only
        the title: the steps are in `oto_procedure op=get`. If the process has no
        drawing yet, the answer tells you how to draw one: do it, then call this again.

        Args:
            slug: the procedure's slug (cf. `oto_procedure op=list`).
        """
        from .. import access, tool_alias
        from ..procedure_diagram import (DOUBLE, client_affiche_les_apps, dessin_hors_fence,
                                         dessin_tague, diagram_check, le_trace,
                                         reparer_les_bords)
        from .guide_run import procedure_servie
        sub = access.current_user_sub_or_raise()
        lu = procedure_servie(sub, slug)
        if lu is None:
            message = _aux_noms_du_produit(
                sub, f"No procedure `{slug}` of yours, in your active org or in your team — list them with oto_procedure op=list.")
            return ToolResult(content=[TextContent(type="text", text=message)],
                              structured_content={"slug": slug, "message": message})
        row, scope = lu
        title = row.get("title") or slug
        version = row.get("version")
        if not client_affiche_les_apps():
            # A text client (Claude Code, a runner job) paints nothing and hands
            # structuredContent to the MODEL: no drawing, no roster, no drawing orders —
            # the steps are what it needs, and they are one call away. The sentence rides
            # in `message` too: that channel is the only one such a client reads, and a
            # model handed `{slug, title, version}` alone told the user the graph was shown.
            url = _page_du_process(sub, slug)
            texte = (f"{title} (`{slug}`, v{version}) — this client does NOT display the "
                     f"graph: the user sees nothing. Say so, explain the process in text "
                     f"from its steps (oto_procedure op=get slug={slug})"
                     + (f" and give the link to its page, where the graph is: {url}" if url
                        else "") + ". Do not draw it yourself.")
            message = _aux_noms_du_produit(sub, texte)
            rendu = {"slug": slug, "title": title, "version": version, "message": message}
            if url:
                rendu["url"] = url
            return ToolResult(content=[TextContent(type="text", text=message)],
                              structured_content=rendu)
        trace = le_trace(row.get("body_md") or "")
        casse = (diagram_check(row.get("body_md") or "")["diagram_warning"]
                 if trace else None)
        # Two drawings: the page draws the FIRST — a warning for the author, not a
        # drawing the user cannot see.
        if casse == DOUBLE:
            casse = None
        # Box edges off by a column are the usual way a model's drawing fails: the user
        # sees the repaired one now, and the model is handed it to save — copying a
        # block is reliable, re-counting columns is not.
        repare = reparer_les_bords(trace) if casse else None
        montre = repare or trace
        rendu = {"slug": slug, "title": title, "version": version, "drawing": montre,
                 "connectors": _connectors_named(montre, access.current_org(sub)) if montre else [],
                 # Where `/brand/…` logos resolve: the view cannot always tell its own
                 # script's origin once the host's sandbox has re-served it.
                 "origin": _origin(bundle)}
        entete = f"{title} (`{slug}`, v{version})"
        ecrivain = not tool_alias.sert_un_worker() and _peut_ecrire(sub, scope)
        if trace and not casse:
            texte = (f"{entete} — the graph is shown to the user (the card says so if "
                     f"the page cannot read it). Do not call this again for the same "
                     f"question. Its steps: oto_procedure op=get slug={slug}.")
        elif repare and ecrivain:
            texte = (f"{entete} — its drawing had box edges off by a column; the user sees "
                     f"it REPAIRED. Save the repair ONCE, without redrawing: oto_procedure "
                     f"op=get slug={slug}, replace the lines between the drawing's ``` "
                     f"fences by EXACTLY the block below (nothing else changes), then "
                     f"oto_procedure op=set slug={slug} scope={scope} "
                     f"expected_version={version} body_md=<the whole body>. Then answer — "
                     f"do not call this again for it.\n```\n{repare}\n```")
        elif repare:
            texte = (f"{entete} — the graph is drawn for the user (its stored drawing has "
                     f"box edges off by a column; an editor of this procedure can fix it). "
                     f"Its steps: oto_procedure op=get slug={slug}.")
        elif trace and not ecrivain:
            texte = (f"{entete} — its drawing shows as raw characters ({casse}). "
                     f"Its steps: oto_procedure op=get slug={slug}.")
        elif trace:
            texte = (f"{entete} — the user sees the drawing as RAW CHARACTERS, not the "
                     f"graph: {casse} Fix ONLY those lines, ONCE: oto_procedure op=get "
                     f"slug={slug}, correct them, oto_procedure op=set slug={slug} "
                     f"scope={scope} expected_version={version} body_md=<the whole body>, "
                     f"then call oto_procedure_app slug={slug} again. If it still says so, "
                     f"stop and tell the user.")
        elif tool_alias.sert_un_worker():
            # A runner job renders nothing to anyone: never send it to rewrite a
            # procedure unattended.
            texte = f"{entete} — no drawing; rendering hosts only. Its steps: oto_procedure op=get slug={slug}."
        elif not _peut_ecrire(sub, scope):
            texte = (f"{entete} — no drawing yet, and adding one needs an admin of this "
                     f"org. Tell the user, and explain the process from its steps: "
                     f"oto_procedure op=get slug={slug}.")
        elif (lang := dessin_tague(row.get("body_md") or "")) is not None:
            texte = f"{entete}. " + _a_detaguer(slug, scope, version, lang)
        elif dessin_hors_fence(row.get("body_md") or ""):
            texte = f"{entete}. " + _a_encadrer(slug, scope, version)
        else:
            texte = f"{entete}. " + _a_dessiner(slug, scope, version)
        return ToolResult(content=[TextContent(type="text", text=_aux_noms_du_produit(sub, texte))],
                          structured_content=rendu)
