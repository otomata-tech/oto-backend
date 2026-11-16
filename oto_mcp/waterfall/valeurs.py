"""Reading a provider's answer: the shape of a value (an email, a phone, a domain), a
path into a response, a key searched by name — and the errors a caller's input raises."""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError

# A response only counts as a hit if it carries a value of the right SHAPE: a provider
# that echoes the input back, or a sentence in an `email` field, is not a find.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_DIGITS = re.compile(r"\d")


#: A LinkedIn profile, whatever its host (`fr.`, `www.`, none) and its trail (`/`, `?…`).
_PROFILE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.I)


def _person_key(p: dict) -> str:
    """Who a person is, for deduplication — ONE key for the whole cascade (a source's
    own list, the next pages bought, the people Jev already judged): their LinkedIn
    profile slug (two searches name the same profile `fr.linkedin.com/in/x` and
    `www.linkedin.com/in/x/`), else their URL, else their name."""
    url = str(p.get("linkedin_url") or "")
    m = _PROFILE.search(url)
    return (f"in/{m.group(1)}" if m else url.rstrip("/") or p.get("full_name") or "").lower()


def _bad(message: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _refused(code: str, message: str) -> McpError:
    """A NAMED refusal (`data.code`): what a caller or a table pass tells apart."""
    return McpError(ErrorData(code=INVALID_PARAMS, message=message,
                              data={"code": code, "retryable": False}))


def _first(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def _get(obj, path: str):
    """Dotted path with list indices (`a.b[0].c`); None as soon as it leads nowhere."""
    cur = obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _scalar(v) -> bool:
    return isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""


def _pick(obj, *paths):
    """The first path that leads to a non-empty SCALAR."""
    for p in paths:
        v = _get(obj, p)
        if _scalar(v):
            return v
    return None


def _email(value) -> Optional[str]:
    if isinstance(value, dict):
        value = _first(value.get("email"), value.get("address"), value.get("value"))
    if not isinstance(value, str):
        return None
    value = value.strip().removeprefix("mailto:").strip()
    return value.lower() if _EMAIL_RE.match(value) else None


def _phone(value) -> Optional[str]:
    if isinstance(value, dict):
        value = _first(value.get("number"), value.get("phone"), value.get("value"),
                       value.get("sanitized_number"), value.get("raw_number"),
                       value.get("international"), value.get("phone_number"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value))
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if len(_PHONE_DIGITS.findall(value)) >= 6 else None


def _emails_in(values) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    for v in values:
        e = _email(v)
        if e and e not in out:
            out.append(e)
    return out


def _phones_in(values) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    for v in values:
        p = _phone(v)
        if p and p not in out:
            out.append(p)
    return out



def _domain(value: Optional[str]) -> Optional[str]:
    """`https://www.Acme.com/x` and `jane@acme.com` → `acme.com`."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = re.sub(r"^[a-z]+://", "", v).split("/", 1)[0].split("?", 1)[0]
    v = v.removeprefix("www.")
    return v if "." in v else None


def _deep(obj, wanted: str, read: Callable[[object], list[str]],
          skip_keys: tuple[str, ...] = ("status", "qualification", "type", "required",
                                        "feedback", "verified", "score")) -> list[str]:
    """Every value under a key whose name contains `wanted` (`workEmail`,
    `professionalEmails`, `phones`…), read by `read` — for providers whose response
    nests contact points under varying names. A key that only QUALIFIES a contact
    point (`emailStatus`, `phoneType`) is not one."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if wanted in lk and not any(s in lk for s in skip_keys):
                    for x in read(v):
                        if x not in out:
                            out.append(x)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(obj)
    return out


def _find_key(obj, names: tuple[str, ...], depth: int = 3):
    """The first non-empty SCALAR under one of `names` (case-insensitive), searched
    breadth-first down to `depth` — for providers whose response shape no test here
    pins (Cognism, Forager, Lusha's company block, lemlist's LinkedIn data)."""
    wanted = {n.lower() for n in names}
    level = [obj]
    for _ in range(depth + 1):
        nxt = []
        for node in level:
            if isinstance(node, dict):
                items = node.items()
            elif isinstance(node, list):
                items = enumerate(node)
            else:
                continue
            for k, v in items:
                if isinstance(k, str) and k.lower() in wanted and _scalar(v):
                    return v
                if isinstance(v, (dict, list)):
                    nxt.append(v)
        level = nxt
    return None


def _not_input(values: list[str], inputs: dict, key: str) -> list[str]:
    """A provider that echoes the contact point it was GIVEN has not found it."""
    given = (inputs.get(key) or "").lower()
    return [v for v in values if v.lower() != given]


def _email_find(emails: list[str], status: Optional[str] = None) -> Optional[dict]:
    if not emails:
        return None
    out = {"email": emails[0], "all": emails}
    if status:
        out["status"] = status
    return out


def _phone_find(phones: list[str]) -> Optional[dict]:
    return {"phone": phones[0], "all": phones} if phones else None


def _slug(linkedin_url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.I)
    return (m.group(1) if m else (linkedin_url or "").strip("/")).lower()


def _clean(d: dict, required: tuple[str, ...]) -> Optional[dict]:
    out = {k: v for k, v in d.items() if v not in (None, "", [], {})}
    return out if any(out.get(r) for r in required) else None


def _norm_name(v: str) -> str:
    v = unicodedata.normalize("NFKD", v or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", v))


def _same_company(inputs: dict, name, domain) -> bool:
    """A SEARCH always returns something: a result only counts when it IS the company
    asked for — same domain, or the asked name contained in its name. A waterfall that
    stopped on a namesake would hide the right answer further down."""
    if inputs.get("domain") and domain:
        return _domain(str(domain)) == inputs["domain"]
    asked = _norm_name(inputs.get("company_name") or "")
    got = _norm_name(str(name or ""))
    return bool(asked and got and (asked == got or f" {asked} " in f" {got} "))


_COMPANY_PAGE = re.compile(r"linkedin\.com/(?:company|school|showcase)/[^/?#\s]+", re.I)


def _company_page(url) -> Optional[str]:
    m = _COMPANY_PAGE.search(str(url or ""))
    return f"https://www.{m.group(0).lower()}/" if m else None
