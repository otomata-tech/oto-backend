"""What the messaging readers of the non-LinkedIn channels (`whatsapp`, `telegram`,
`instagram`) serve: the Unipile page, reshaped so that an agent reads it at face value.

The raw page misled agents in four ways (WhatsApp signals of 25/09 to 05/10/2026):

- **the list twice** — oto-core keeps Unipile's `data` and adds `items` (same list), and
  `next_cursor` beside `cursor`: 201 000 characters for 100 threads, half of them a copy.
  ONE list key (`items`) and ONE cursor key (`cursor`), as the LinkedIn lists already do
  (`_slim_search`, `_page_de_relations`).
- **a thread listed twice** in the same page (same `id`, two `last_message_timestamp`) —
  kept once, the first seen (Unipile serves the most recent first), and said.
- **identifiers formatted as phone numbers** — WhatsApp hands out a group id
  (`…@g.us`) or a privacy id (`…@lid`) for an unknown sender, and Unipile formats its
  digits as a number (`+12 0 36 31 80 581534143` for the group `120363180581534143@g.us`).
  An agent takes it for a real phone. A name that is only those digits is removed
  (`null`), the identifier stays where it was; a real number comes from a
  `…@s.whatsapp.net` id and is never touched.
- **a message with neither text nor attachment** (photo, voice note, poll not passed on by
  Unipile) read as an empty message — flagged `content_unavailable`, and counted.

And the bound of the list: WhatsApp refuses `limit` > 100 (400 « Invalid querystring ») and
gives NO cursor ('Cursor-based pagination is not supported by whatsapp', signal 1215): a
full page without a cursor says that the older threads are not reachable by the list —
never a silence that reads « that's all ».
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: Largest page of `op=list` the non-LinkedIn channels accept (WhatsApp: 400 beyond).
LISTE_MAX = 100

# Identifiers that are NOT a phone: a group, a privacy id. Their digits formatted as a
# number make a false phone.
_IDENTIFIANT_NON_TELEPHONE = re.compile(r"^(\d+)@(g\.us|lid)$")
# A « name » made only of a phone's characters.
_FORME_DE_NUMERO = re.compile(r"^\+?[\d\s().-]+$")
# E.164: at most 15 digits. Beyond, it is not a phone, whatever it looks like.
_E164_MAX = 15
_CLES_DE_NOM = ("display_name", "sender_display_name", "attendee_name", "name")


def _identifiants(obj: Any, out: set[str]) -> set[str]:
    """The digits of every group or privacy id found in `obj`, at any depth."""
    if isinstance(obj, dict):
        for v in obj.values():
            _identifiants(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _identifiants(v, out)
    elif isinstance(obj, str):
        m = _IDENTIFIANT_NON_TELEPHONE.match(obj.strip())
        if m:
            out.add(m.group(1))
    return out


def faux_numero(valeur: Any, identifiants: set[str]) -> bool:
    """True if `valeur` is an identifier's digits dressed as a phone number."""
    if not isinstance(valeur, str) or not _FORME_DE_NUMERO.match(valeur.strip()):
        return False
    chiffres = re.sub(r"\D", "", valeur)
    return bool(chiffres) and (chiffres in identifiants or len(chiffres) > _E164_MAX)


def _retirer_faux_numeros(obj: Any, identifiants: set[str]) -> int:
    """Set to None every name that is a false number, in place. Returns how many."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _CLES_DE_NOM and faux_numero(v, identifiants):
                obj[k] = None
                n += 1
            else:
                n += _retirer_faux_numeros(v, identifiants)
    elif isinstance(obj, list):
        for v in obj:
            n += _retirer_faux_numeros(v, identifiants)
    return n


def _une_liste(page: dict) -> dict:
    """One list key (`items`) and one cursor key (`cursor`), everything else kept."""
    out = {k: v for k, v in page.items() if k not in ("data", "next_cursor")}
    items = page.get("items")
    if not isinstance(items, list):
        items = page.get("data") if isinstance(page.get("data"), list) else []
    out["items"] = items
    cursor = page.get("cursor") or page.get("next_cursor")
    if cursor:
        out["cursor"] = cursor
    else:
        out.pop("cursor", None)
    return out


def _sans_doublons(items: list) -> tuple[list, int]:
    vus: set = set()
    garde = []
    for it in items:
        cle = it.get("id") if isinstance(it, dict) else None
        if cle is not None:
            if cle in vus:
                continue
            vus.add(cle)
        garde.append(it)
    return garde, len(items) - len(garde)


def _contenu_absent(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("is_event"):
        return False
    return not (message.get("text") or "").strip() and not message.get("attachments")


def page_de_fils(page: Any, limit: int) -> Any:
    """`op=list`: the threads, once each, with their names cleaned and the bound said."""
    if not isinstance(page, dict):
        return page
    out = _une_liste(page)
    items, doublons = _sans_doublons(out["items"])
    retires = 0
    for it in items:
        retires += _retirer_faux_numeros(it, _identifiants(it, set()))
    out["items"] = items
    if doublons:
        out["duplicates_removed"] = doublons
    if retires:
        out["false_numbers_removed"] = retires
        out["false_numbers_note"] = (
            "names that were only a group or privacy identifier formatted as a phone "
            "number are now null: they were never phone numbers. The identifier itself "
            "is still in the thread.")
    if "cursor" not in out and len(out["items"]) >= limit:
        out["truncated"] = True
        out["truncated_note"] = (
            f"a full page ({len(out['items'])} threads) and NO cursor: Unipile does not "
            "paginate this channel's thread list, so the older threads are NOT reachable "
            "by op='list'. This is not the whole inbox. Read an older thread by its "
            "`chat_id`.")
    return out


def page_de_messages(page: Any) -> Any:
    """`op=read`: the messages, one list key, names cleaned, missing content flagged."""
    if not isinstance(page, dict):
        return page
    out = _une_liste(page)
    identifiants = _identifiants(out["items"], set())
    retires = _retirer_faux_numeros(out["items"], identifiants)
    absents = 0
    for m in out["items"]:
        if _contenu_absent(m):
            m["content_unavailable"] = True
            absents += 1
    if retires:
        out["false_numbers_removed"] = retires
    if absents:
        out["content_unavailable"] = {
            "count": absents,
            "reason": "Unipile passed on neither text nor attachment for these messages "
                      "(typically a photo, a voice note, a poll or a sticker it does not "
                      "sync): their content is unknown, not empty.",
        }
    return out


def references_des_pieces(page: Any, canal: str) -> Any:
    """`op=read`, every channel: each attachment Unipile serves with a `url` gets a
    `source` — the REFERENCE `file_source` resolves server-side with the account's key
    (oto-backend#1149). Retrieving an attachment is reading its message: the bytes never
    go through the conversation, the reference goes to `oto_import` (to file it in a
    project) or to any tool that takes a `source`."""
    if not isinstance(page, dict):
        return page
    items = page.get("items") if isinstance(page.get("items"), list) else page.get("data")
    if not isinstance(items, list):
        return page
    designees = sans_url = 0
    for m in items:
        pieces = m.get("attachments") if isinstance(m, dict) else None
        for att in pieces if isinstance(pieces, list) else ():
            if not isinstance(att, dict):
                continue
            url = att.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                att["source"] = {"kind": "unipile", "channel": canal.lower(), "url": url}
                designees += 1
            else:
                sans_url += 1
    if designees:
        page["attachments_note"] = (
            "an attachment's `url` points to the Unipile API and requires its key (not "
            "readable with api_get): pass its `source` instead — to `oto_import("
            "source=…, target=\"project_file\", project_id=…)` to file it in a project, "
            "or to any tool that takes a `source`. The server fetches the bytes.")
    if sans_url:
        page["attachments_without_url"] = (
            f"{sans_url} attachment(s) came without a `url` from Unipile: they cannot be "
            "retrieved.")
    return page


def borne_de_liste(limit: Optional[int]) -> Optional[str]:
    """The refusal of a page Unipile would refuse, or None."""
    if limit is not None and not 1 <= limit <= LISTE_MAX:
        return (f"op='list': `limit` ranges from 1 to {LISTE_MAX} (beyond, Unipile "
                "refuses the page).")
    return None
