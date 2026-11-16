"""`oto_connector(op="setup_link", name=…)` — the page where a PERSON connects one
catalog connector themselves, returned to the agent that needs it.

Why: an agent that hits a missing key had two bad ways out — ask for the key in the
chat (it then sits in the model's context, the client's history and its logs), or
tell the person "go to your connectors page" and let them find the card. The front
already serves one URL per connector (`<connectors page>/<name>`, it opens that
connector's panel: key form, OAuth button or hosted login), and `api(op="setup_link")`
already handed it out for the Custom API alone. This op hands it out for EVERY catalog
connector, with what the person will be asked for, so the agent can say it in one
message.

What it does NOT do, on purpose:
- it takes no secret and puts none in the link — the key is typed in the form, which
  saves it under the person's own rights (`/api/settings/api-keys/{provider}`);
- it carries no free text chosen by the model to the page: a sentence picked by an
  agent, shown on a trusted page next to a key field, would be a phishing vector;
- it says nothing about whether a key already exists: `op=list name=…` does
  (`credential`, `next_step`), and is how the agent checks the person is done.
"""
from __future__ import annotations

from typing import Optional

from ... import links, providers
from .._types import AuthzDenied, ResolvedCtx
from . import selection as sel

# What the person does on the page, by `auth.method` of the card.
_GESTE = {
    "secret": ("They paste the key IN THE FORM on that page: oto keeps it encrypted and "
               "checks it when the connector offers a check."),
    "oauth": ("They click Connect on that page and approve access at the provider — "
              "no key to copy."),
    "hosted": ("They log in to their account in the hosted window the page opens "
               "(2FA and captcha included)."),
    "cookie": ("They follow the page's steps to connect their session; nothing is "
               "pasted in the chat."),
    "none": ("This connector needs no credential: the page only lets them add it to "
             "their toolbox."),
}

_REGLE = ("Send this link to the person who must connect it. Never ask for a key, "
          "password or token in the conversation and never pass one to a tool. If a key "
          "was already pasted in a chat, ask them to revoke it at the provider and create "
          "a new one. When they say it is done, check with "
          "`oto_connector(op=\"list\", name=\"{name}\")` (`credential`).")

# Doc sections that say how to GET the credential, in the order the person needs them.
_SECTIONS_UTILES = ("prerequisite", "setup")
_MAX_GUIDE = 1500


def _champs(card: dict) -> list[dict]:
    """The fields the form will ask for — names and labels only, never a value."""
    return [{"name": f["name"], "label": f.get("label") or f["name"],
             "secret": bool(f.get("secret")), "required": bool(f.get("required"))}
            for f in (card.get("auth") or {}).get("fields") or []]


def _guide(card: dict) -> Optional[str]:
    """Where to find the key, from the connector's own doc sections (bounded)."""
    parts = []
    for s in card.get("doc_sections") or []:
        kind = s.get("kind") if isinstance(s, dict) else getattr(s, "kind", None)
        if kind not in _SECTIONS_UTILES:
            continue
        title = s.get("title") if isinstance(s, dict) else getattr(s, "title", "")
        body = s.get("body_md") if isinstance(s, dict) else getattr(s, "body_md", "")
        parts.append(f"{title}: {body}".strip())
    texte = "\n\n".join(p for p in parts if p)
    if not texte:
        return None
    return texte if len(texte) <= _MAX_GUIDE else texte[:_MAX_GUIDE].rstrip() + "…"


def setup_link(ctx: ResolvedCtx, name: str) -> dict:
    catalog = sel._visible_catalog(ctx)
    trouves = sel._resoudre_nom(catalog, name)
    if not trouves:
        # Same refusal as `op=list name=…`: close names + the Custom API way out.
        raise sel._refus_nom_inconnu(catalog, name, ctx)
    if len(trouves) > 1:
        noms = [c["name"] for c in trouves]
        raise AuthzDenied(
            400, "ambiguous_connector",
            f"`{name}` designates {len(noms)} connectors: "
            + ", ".join(f"`{n}`" for n in noms)
            + ". Retry with the exact name — a link is for ONE connector.",
            details={"query": name, "candidates": noms})
    card = trouves[0]
    connector = card["name"]
    if connector == "api":
        raise AuthzDenied(
            400, "use_http_setup_link",
            "The Custom API connector has its own setup link, pre-filled with the API's "
            "name, base URL and auth mode: `api(op=\"setup_link\", label=…, base_url=…)`.")
    method = (card.get("auth") or {}).get("method") or "secret"
    # The product's one-connector page when it declares one (`connect` link kind);
    # else its connectors page, opened on this card, with `via=agent` — a FIXED
    # marker (never model text) for which the page shows its own "paste the key
    # here, never in the chat" banner.
    url = (links.link_for("connect", sub=ctx.sub, org=ctx.org_id, connector=connector)
           or links.connector_setup_link(connector, sub=ctx.sub, org=ctx.org_id,
                                         via="agent"))
    geste = _GESTE.get(method, _GESTE["secret"])
    out = {
        "connector": connector,
        "label": card.get("label") or connector,
        # Public catalog data, for a card that shows the connector the way the
        # product's own card does.
        "description": card.get("description") or "",
        "logo_url": card.get("logo_url"),
        "method": method,
        "url": url,
        "fields": _champs(card),
        # What the PERSON does on the page — the line a card shows them.
        "action": geste,
        "instructions": geste + " " + _REGLE.format(name=connector),
    }
    guide = _guide(card)
    if guide:
        out["where_to_find"] = guide
    if (card.get("auth") or {}).get("credential_of"):
        out["credential_of"] = card["auth"]["credential_of"]
    if url is None:
        out["url_missing"] = links.raison_sans_lien("connectors", sub=ctx.sub,
                                                    org=ctx.org_id)
    return out
