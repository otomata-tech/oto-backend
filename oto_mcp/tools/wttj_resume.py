"""DURABLE resume links of the `wttj` connector — the synchronous part.

WTTJ hands a candidate's resume out as `resume_url`: a CloudFront URL signed for 30
minutes (`Expires`). A human who clicks it later gets "Access denied". The tool
therefore adds `resume_link`, an oto address that does not expire with it:
`{public_base_url}/api/wttj/resume/{token}`. On each click, the route
(`api/wttj.py`) asks WTTJ for the candidate again and redirects (302) to the fresh
`resume_url`. **Nothing is stored by oto, and no resume content goes through it.**

- **Token**: the signed upload token (`upload_tokens.sign/verify`, HMAC) under a
  DISTINCT `typ` (`wttj_resume`) — it opens no other route, no other token opens
  this one. It seals `sub`, `org`, the team (`group`) under which the key was
  resolved, and the `candidate_reference`. Lives `TTL_S` (30 days) and is NOT
  single use: the link opens again. Signed, NOT encrypted — whoever holds the link
  reads those four values; none of them is a secret (an opaque reference and
  internal ids), and none opens anything without the replay below.
- **Replay** (`prepare`): the sealed org and team are REPLAYED at each click, not
  trusted — membership of the account in the org (and in the team, when one is
  sealed), org suspension, connector activation, then the WTTJ key resolved under
  that account/org/team by the same cascade as the tool. A member removed from the
  team, or from the org, holds a dead link.
- **Fetch** (`fresh_resume_url`): one `get_candidate`, no database.

Every refusal is a `Refused` with a short sentence for a human: no internal id, no
name — the page is read by whoever was handed the link.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn, Optional

from .. import config, upload_tokens

TYP = "wttj_resume"
KIND = "wttj_resume"
CONNECTOR = "wttj"
#: 30 days: long enough for a link pasted in a ticket or an email to still open when
#: someone gets to it; bounded, since the account's rights are replayed anyway.
TTL_S = 30 * 24 * 3600

#: The stable name of an opening in the call journal.
JOURNAL_TOOL = "wttj_resume_link"


class Refused(Exception):
    """A named refusal of the resume route: HTTP status, machine code, human sentence."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ── The token ─────────────────────────────────────────────────────────────────────

def mint(sub: str, org_id: Optional[int], group_id: Optional[int],
         candidate_reference: str) -> str:
    """The durable link of one candidate's resume, sealed for this account, org and
    team."""
    target = {"kind": KIND, "group": group_id,
              "candidate_reference": candidate_reference}
    token, _ = upload_tokens.sign(sub, org_id, target, ttl=TTL_S, typ=TYP)
    return f"{config.public_base_url()}/api/wttj/resume/{token}"


def verify(token: str) -> Optional[dict]:
    """The payload of a valid resume token (signature, `typ`, expiry, target), `None`
    otherwise."""
    payload = upload_tokens.verify(token, typ=TYP)
    if payload is None:
        return None
    target = payload.get("target") or {}
    ref = target.get("candidate_reference")
    if target.get("kind") != KIND or not isinstance(ref, str) or not ref:
        return None
    return payload


# ── Replay (database, vault) then fetch (WTTJ only) ──────────────────────────────

@dataclass
class Prepared:
    key_mode: Optional[str] = None
    # The WTTJ token: never rendered by `repr` (an exception trace would copy it
    # into a log otherwise).
    key: str = field(default="", repr=False)


def _check_membership(sub: str, org_id: Optional[int], group_id: Optional[int]) -> None:
    from .. import group_store, roles

    if org_id is None:
        raise Refused(403, "no_organization",
                      "This link was created outside any organization: ask for a new "
                      "one.")
    if not roles.is_org_member(sub, org_id):
        raise Refused(403, "not_an_org_member",
                      "The account that created this link no longer belongs to its "
                      "organization.")
    if group_id is not None:
        group = group_store.get_group(group_id) or {}
        if group.get("org_id") != org_id or not group_store.is_group_member(sub, group_id):
            raise Refused(403, "not_a_team_member",
                          "The account that created this link no longer belongs to its "
                          "team.")


def _resolve_key(sub: str, org_id: int, group_id: Optional[int]):
    """The WTTJ key under the SEALED org and team — pinned, otherwise the cascade
    would fall back on the account's home org and team, which are not necessarily
    those of the call that minted the link. No sealed team = the org level, forced
    (`X-Oto-Group: 0` semantics): the home team must not slip in."""
    from .. import access, session_org
    from ..mcp_errors import McpError

    pin_org = session_org.set_call_org(org_id)
    pin_group = (session_org.set_call_group(group_id) if group_id is not None
                 else session_org.set_view_group(0))
    try:
        return access.resolve_credential(CONNECTOR, sub=sub)
    except McpError as e:
        raise Refused(424, "credential_unavailable",
                      "No Welcome to the Jungle key is reachable any more for the "
                      "account that created this link.") from e
    finally:
        if group_id is not None:
            session_org.reset_call_group(pin_group)
        else:
            session_org.reset_view_group(pin_group)
        session_org.reset_call_org(pin_org)


def prepare(sub: str, org_id: Optional[int], group_id: Optional[int]) -> Prepared:
    """Replays the SEALED org and team, then resolves the key. Synchronous (database,
    vault): called off the event loop. Raises `Refused`."""
    from .. import org_suspension
    from ..connectors import activation

    _check_membership(sub, org_id, group_id)
    if org_suspension.refus(org_id):
        raise Refused(403, "org_suspended", "The organization of this link is suspended.")
    if activation.cran_qui_coupe(CONNECTOR, org_id, group_id) is not None:
        raise Refused(403, "connector_disabled",
                      "The Welcome to the Jungle connector is disabled for the "
                      "organization of this link.")
    rc = _resolve_key(sub, org_id, group_id)
    return Prepared(key_mode=rc.mode, key=rc.key)


def _raise_upstream(e) -> NoReturn:
    """WTTJ's refusal, named for a human. Raises (never returns): the refusals stay
    `raise Refused(<status>, "<code>")` on the line, where the declared-errors check
    reads them."""
    status = e.status_code
    if status == 404:
        raise Refused(404, "candidate_not_found",
                      "Welcome to the Jungle no longer finds this candidate.") from e
    if status in (401, 403):
        raise Refused(424, "wttj_access_refused",
                      "Welcome to the Jungle refused the connector's key for this "
                      "candidate.") from e
    if status == 429:
        raise Refused(503, "wttj_rate_limited",
                      "Welcome to the Jungle asks to slow down: open the link again "
                      "in a moment.") from e
    raise Refused(502, "wttj_unavailable",
                  "Welcome to the Jungle did not answer properly: try again later.") from e


def fresh_resume_url(prep: Prepared, candidate_reference: str) -> str:
    """The candidate's `resume_url`, signed NOW by WTTJ. Talks to WTTJ only."""
    import requests
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.wttj_ats import WttjAtsClient

    try:
        candidate = WttjAtsClient(api_key=prep.key).get_candidate(candidate_reference)
    except UpstreamHTTPError as e:
        _raise_upstream(e)
    except requests.RequestException as e:
        raise Refused(502, "wttj_unreachable",
                      "Welcome to the Jungle could not be reached: try again "
                      "later.") from e
    url = candidate.get("resume_url") if isinstance(candidate, dict) else None
    if not url:
        raise Refused(404, "no_resume", "This candidate has no resume on file.")
    if not isinstance(url, str) or not url.startswith("https://"):
        # Never redirect a browser to something WTTJ did not hand out as a download.
        raise Refused(502, "wttj_unavailable",
                      "Welcome to the Jungle returned an unusable resume address.")
    return url


#: A resume larger than this is not read: a CV is a few pages, not a book.
RESUME_MAX_BYTES = 10 * 1024 * 1024


def resume_text(url: str) -> dict:
    """The TEXT of the resume at `url` (a `resume_url` WTTJ just signed), read in
    memory and dropped: nothing is stored, the file itself never leaves this call.

    Returns `{format, text, pages, truncated}`, or `{text_unavailable}` naming why
    (a scanned resume has no text: there is no OCR). Raises `ValueError` on a
    download that cannot be made — the caller turns it into a refusal."""
    from urllib.parse import unquote, urlsplit

    import requests

    from .. import egress, file_extract
    from ..file_content import INLINE_TEXT_CAP

    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Welcome to the Jungle returned an unusable resume address.")
    try:
        egress.check_url(url, connector="wttj", field="resume_url",
                         exceptions_declarees=False)
    except egress.EgressRefused as e:
        raise ValueError(str(e)) from e
    try:
        with requests.get(url, timeout=(10, 30), stream=True) as r:
            if r.status_code != 200:
                raise ValueError(f"the resume could not be downloaded (HTTP "
                                 f"{r.status_code}) — try again: its address expires "
                                 "in 30 minutes.")
            data = bytearray()
            for chunk in r.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > RESUME_MAX_BYTES:
                    raise ValueError("the resume is over 10 MB: not read.")
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    except requests.RequestException as e:
        raise ValueError(f"the resume could not be downloaded "
                         f"({type(e).__name__}).") from e
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "resume"
    ex = file_extract.extract(bytes(data), filename, mime)
    if not ex.ok:
        return {"text_unavailable": f"{ex.status}: {ex.detail}"}
    return {"format": filename.rsplit(".", 1)[-1].lower() if "." in filename else mime,
            "text": ex.text[:INLINE_TEXT_CAP], "pages": ex.pages,
            "truncated": len(ex.text) > INLINE_TEXT_CAP}


def journal_line(payload: dict, *, ok: bool, error: Optional[str], duration_ms: int,
                 key_mode: Optional[str] = None) -> dict:
    """The `tool_calls` row of one opening. `kind='mcp'`, like the Pennylane relay: the
    opening is the end of an agent's gesture (`wttj_candidate`), and `connector` is
    reserved for credential resolution FAILURES. Never the token, nor the WTTJ URL."""
    target = payload.get("target") or {}
    return {"kind": "mcp", "tool": JOURNAL_TOOL, "sub": payload.get("sub"),
            "org_id": payload.get("org"),
            "args": {"candidate_reference": target.get("candidate_reference"),
                     "group_id": target.get("group")},
            "ok": ok, "error": error, "duration_ms": duration_ms, "key_mode": key_mode}
