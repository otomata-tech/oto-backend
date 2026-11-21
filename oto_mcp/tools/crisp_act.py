"""`crisp_act` — what acts on Crisp: the writes on conversations and contacts, and the
one send to a person.

The pair of the read-only `crisp` (`tools/crisp.py`, which registers this module):
`crisp` reads, `crisp_act` (`lecture.ACTE`) writes, the tool an MCP client asks to
confirm (`tests/test_convention_act.py`). Each op wraps one or more functions of the
`crisp` description (connectors repo, `connectors/crisp/connector.yaml`, 1.1.0).

**Every write touches the real conversations of real people** (the contacts of the
customer's own clients, patients included): the served text says so.

**The send is confirmed TWICE.** `op="send_message"` reaches the contact and cannot be
taken back. Beyond the confirmation the client asks for the whole tool, it requires
`confirm_send=True`, an argument no other op takes (pattern of `confirm_credits` /
`confirm_quota` in `tools/ubersuggest.py` and of the preview of `tools/klaviyo.py`):
without it, NOTHING is sent and the answer is a preview — the conversation's channel
(`meta.origin`), the contact's name and the time of the contact's last message, read
from Crisp — for the agent to show the person before calling again.

**Segments are added or removed, never overwritten.** Crisp takes a conversation's or a
contact's segments as a list and does not say whether the list replaces the current one:
`add_segments` / `remove_segments` read the current list, send it changed, and read it
again — an answer that does not hold the change is an error, never a silent success.

Client calls are written out (`_client().send_message(…)`) for the version-skew probe
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP

from .. import access
from .lecture import ACTE

_NAME = "crisp"

WRITES_TOUCH_REAL_PEOPLE = (
    "Every op acts on the real conversations and contacts of the workspace (patients "
    "included): act only on what the task names, and a message sent cannot be taken back.")

_ACT_OPS = ("add_note", "set_state", "update_conversation", "add_segments",
            "remove_segments", "update_person", "update_person_data", "send_message")
_CONTACT_FIELDS = frozenset({"nickname", "email", "phone", "address"})
#: For each op, the arguments it reads (`op` aside), and those it requires.
_ACT_TAKES: dict[str, frozenset] = {
    "add_note": frozenset({"session_id", "text"}),
    "set_state": frozenset({"session_id", "state"}),
    "update_conversation": frozenset({"session_id"}) | _CONTACT_FIELDS,
    "add_segments": frozenset({"session_id", "people_id", "segments"}),
    "remove_segments": frozenset({"session_id", "people_id", "segments"}),
    "update_person": frozenset({"people_id"}) | _CONTACT_FIELDS,
    "update_person_data": frozenset({"people_id", "data"}),
    "send_message": frozenset({"session_id", "text", "confirm_send"}),
}
_ACT_NEEDS: dict[str, tuple] = {
    "add_note": ("session_id", "text"),
    "set_state": ("session_id", "state"),
    "update_conversation": ("session_id",),
    "add_segments": ("segments",),
    "remove_segments": ("segments",),
    "update_person": ("people_id",),
    "update_person_data": ("people_id", "data"),
    "send_message": ("session_id", "text"),
}
_NOT_FOUND = {
    "conversation": "no conversation with this session_id in this Crisp workspace",
    "person": "no contact with this people_id or email in this Crisp workspace",
}


def _target(op: str, session_id: Optional[str], people_id: Optional[str]) -> str:
    """`add_segments` / `remove_segments` act on ONE conversation or ONE contact."""
    from .crisp import _bad

    if bool(session_id) == bool(people_id):
        raise _bad(f"op='{op}' acts on ONE conversation (`session_id`) or ONE contact "
                   f"(`people_id`), {'not both' if session_id else 'one is required'}. "
                   "Nothing was done.")
    return "conversation" if session_id else "person"


def _segments_of(answer: Any) -> list:
    """`data.segments` of a conversation meta or of a contact; absent = none."""
    data = answer.get("data") if isinstance(answer, dict) else None
    segments = data.get("segments") if isinstance(data, dict) else None
    return list(segments) if isinstance(segments, list) else []


def _last_contact_message(answer: Any) -> Optional[int]:
    """The timestamp (ms) of the contact's latest message (`from` user) in the latest
    batch of a conversation's messages, or None when the batch holds none."""
    data = answer.get("data") if isinstance(answer, dict) else None
    stamps = [m.get("timestamp") for m in data or () if isinstance(m, dict)
              and m.get("from") == "user" and isinstance(m.get("timestamp"), (int, float))]
    return max(stamps) if stamps else None


def register_act(mcp: FastMCP) -> None:
    from oto.tools.crisp import CrispClient
    from oto.tools.common.errors import UpstreamHTTPError

    from .crisp import _bad, _upstream_message

    def _client() -> CrispClient:
        creds = access.resolve_credential_fields(_NAME)
        return CrispClient(token_identifier=creds.get("token_identifier"),
                           token_key=creds.get("token_key"),
                           website_id=creds.get("website_id"))

    def _run(what: str, fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(f"{e} Nothing was done.")
        except UpstreamHTTPError as e:
            message = _upstream_message(e, "")
            if e.status_code == 404:
                message = f"Crisp: not found (404) — {_NOT_FOUND[what]}."
            elif e.status_code == 406:
                message = ("Crisp refused the message under a messaging rule (406: spam or "
                           "blocked): nothing was sent.")
            elif e.status_code == 409 and what == "person":
                message = "Crisp: another contact of this workspace already has this email (409)."
            elif e.status_code == 409:
                message = ("Crisp refused the message: too many messages in this conversation "
                           "(409); nothing was sent.")
            raise _bad(message)

    def _segments(op: str, what: str, session_id, people_id, segments: list) -> dict:
        """Read the current list, send it changed, read it again: the answer says what
        Crisp holds, and an unchanged list is an error, never a silent success."""
        if not isinstance(segments, list) or not segments:
            raise _bad(f"op='{op}': `segments` is a non-empty list of segment names. "
                       "Nothing was done.")
        wanted = list(dict.fromkeys(segments))
        if what == "conversation":
            read = lambda: _client().get_conversation_meta(session_id)  # noqa: E731
        else:
            read = lambda: _client().get_person(people_id)  # noqa: E731
        before = _segments_of(_run(what, read))
        if op == "add_segments":
            after = before + [s for s in wanted if s not in before]
        else:
            after = [s for s in before if s not in wanted]
        if after == before:
            return {"changed": False, "segments": before,
                    "note": "nothing to change: the list already says so."}
        if what == "conversation":
            _run(what, lambda: _client().set_conversation_segments(session_id, after))
        else:
            _run(what, lambda: _client().set_person_segments(people_id, after))
        now = _segments_of(_run(what, read))
        missing = ([s for s in wanted if s not in now] if op == "add_segments"
                   else [s for s in wanted if s in now])
        if missing or any(s not in now for s in after):
            raise _bad(f"Crisp answered the change, but its {what} now holds the segments "
                       f"{now} (expected {after}). Check in Crisp before trying again.")
        return {"changed": True, "segments": now}

    @mcp.tool(name="crisp_act", annotations=ACTE)
    def crisp_act(
        op: Literal[_ACT_OPS],
        session_id: Optional[str] = None,
        people_id: Optional[str] = None,
        text: Optional[str] = None,
        state: Optional[Literal["pending", "unresolved", "resolved"]] = None,
        nickname: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        address: Optional[str] = None,
        segments: Optional[list[str]] = None,
        data: Optional[dict[str, str | int | float | bool]] = None,
        confirm_send: bool = False,
    ) -> Any:
        """Act on Crisp customer messaging: an internal note, a conversation's state, the
        contact details of a conversation or of a contact, segments, a contact's custom
        data, and a text message sent to the contact. Every op acts on the real
        conversations and contacts of the workspace (patients included): act only on
        what the task names, and a message sent cannot be taken back. Nothing deletes,
        nothing touches the knowledge base or moves a conversation to another inbox.
        Each call acts once (never retried).

        `op`:
        - `add_note` — an internal note (`text`) in the conversation `session_id`: the
          team sees it, the contact NEVER receives it. It stays in the history.
        - `set_state` — the conversation's `state`: pending, unresolved or resolved.
        - `update_conversation` — the contact details the conversation carries:
          `nickname`, `email`, `phone`, `address` (only those given).
        - `update_person` — the contact (`people_id` or its email): `email`,
          `nickname`, `phone`, `address` (only those given; Crisp keeps the others).
        - `update_person_data` — the contact's custom data: `data` keys MERGED with
          those already set (a key given replaces its value, the others stay).
        - `add_segments` / `remove_segments` — add or remove `segments` on ONE
          conversation (`session_id`) or ONE contact (`people_id`); the other segments
          are kept. Crisp is read before and after: the answer is the list it holds.
        - `send_message` — ⚠️ sends `text` to the CONTACT of the conversation, on its
          channel: it reaches a real person and cannot be taken back. Without
          `confirm_send=True` NOTHING is sent: the answer is a preview (channel,
          contact, time of the contact's last message) to show the person; call again
          with `confirm_send=True` only once the person has approved this exact text.
          Crisp's API documents nothing about a channel's messaging window (such as
          WhatsApp's 24 hours after the contact's last message): if the contact wrote
          more than 24 hours ago on WhatsApp, the message may not be delivered.

        Args:
            op: What to do (see above).
            session_id: The conversation (session_...).
            people_id: The contact's people_id, or its email.
            text: add_note / send_message — the text.
            state: set_state — pending | unresolved | resolved.
            nickname: First and last name.
            email: Email address.
            phone: Phone number, international format (+33...).
            address: Postal address.
            segments: add_segments / remove_segments — segment names.
            data: update_person_data — custom data keys and values (text, number, boolean).
            confirm_send: send_message only — True once the person approved this text;
                without it nothing is sent.
        """
        if op not in _ACT_TAKES:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_ACT_OPS)}. "
                       "Nothing was done.")
        given = dict(session_id=session_id, people_id=people_id, text=text, state=state,
                     nickname=nickname, email=email, phone=phone, address=address,
                     segments=segments, data=data, confirm_send=confirm_send or None)
        stray = sorted(k for k, v in given.items() if v is not None and k not in _ACT_TAKES[op])
        if stray:
            raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                       "Nothing was done.")
        for name in _ACT_NEEDS[op]:
            if given.get(name) in (None, "", []):
                raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")
        contact = {k: given[k] for k in ("nickname", "email", "phone", "address")
                   if given[k] is not None}
        if op in ("update_conversation", "update_person") and not contact:
            raise _bad(f"op='{op}' changes at least one of `nickname`, `email`, `phone`, "
                       "`address`. Nothing was done.")

        if op == "add_note":
            return _run("conversation", lambda: _client().add_conversation_note(session_id, text))
        if op == "set_state":
            return _run("conversation", lambda: _client().change_conversation_state(
                session_id, state))
        if op == "update_conversation":
            return _run("conversation", lambda: _client().update_conversation_meta(
                session_id, **contact))
        if op == "update_person":
            return _run("person", lambda: _client().update_person(people_id, **contact))
        if op == "update_person_data":
            return _run("person", lambda: _client().update_person_data(people_id, data))
        if op in ("add_segments", "remove_segments"):
            return _segments(op, _target(op, session_id, people_id), session_id, people_id,
                             segments)
        # send_message
        if confirm_send is not True:
            meta = _run("conversation", lambda: _client().get_conversation_meta(session_id))
            messages = _run("conversation", lambda: _client().list_conversation_messages(
                session_id))
            m = meta.get("data") if isinstance(meta, dict) else None
            m = m if isinstance(m, dict) else {}
            return {
                "sent": False,
                "preview": {"session_id": session_id, "channel": m.get("origin"),
                            "contact": m.get("nickname"), "text": text,
                            "last_contact_message_at": _last_contact_message(messages)},
                "next": ("Nothing was sent. Show the person this text, the contact and the "
                         "channel; call again with confirm_send=true only once they approve "
                         "it. A message sent cannot be taken back."),
            }
        return _run("conversation", lambda: _client().send_message(session_id, text))
