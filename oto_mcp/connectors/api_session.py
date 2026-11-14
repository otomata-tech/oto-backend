"""The `session` and `visitor` auth modes of the `api` connector, on the host's side: the
egress guard of each login hop, and the cookie jar kept in the vault.

The `visitor` mode reads a public site that has no account: no login nor password, the
"sign-in" is a GET of the entry page the card's `session` field names, whose answer sets
the visitor's cookies (oto-core: `VisitorSession`). It needs the same three things as the
session mode, given by the same functions — one guard, one sealed jar, one fingerprint.

oto-core (`oto.tools.http.session`) signs in with the card's login and password, follows
the declared steps and reads with the session cookie. It asks the host for three things,
given here:

- `guard(url)`: called before EACH hop of the login (the POST, the URL the login answer
  names, every redirect). It is `egress.check_url`, the guard of every card destination.
  The named exceptions of the deployment (`OTO_EGRESS_ALLOW`) apply to the base URL's
  host only — the one the admin typed on the card; a hop the service points at gets
  the strict policy, like a URL an agent chose;
- `jar`: the cookies of the last sign-in, read back from the vault so a call does not
  sign in each time;
- `on_jar(text)`: called after a sign-in and whenever the service changes a cookie; the
  text goes to the vault ENCRYPTED.

**Where the jar lives.** In the card's own vault row, as the sealed satellite
`session_jar` (`credentials_store.seal_satellite`): AES-GCM under the vault's key, its
AAD bound to the row AND to the satellite's name, stored in `meta.session_jar_enc` and
removed from every status surface like an OAuth bearer. Per account by construction:
each named account of the card is its own row. The secret blob is not rewritten — a
cookie change neither bumps the row's `version` nor races an admin re-setting the card.

**Bound to the card that produced it.** The sealed text carries a fingerprint of the
fields that decide WHO signs in (base URL, login, password, steps). A jar sealed for
other fields — the card was re-set while a call was running — is not used: the call
signs in again, and its new jar replaces the old one.

**Several processes.** Each process holds its own client and may sign in on its own;
the last writer wins. That costs at most one extra sign-in, never a wrong account: a
jar only serves the fields it was sealed for. Accepted, not locked.

**A jar that no longer decrypts** (the row was moved to another level or account, or
copied to another instance: its AAD or key changed) is a lost cache, not a vault fault
— the secret blob is read by its own path, which raises on its own. It is logged and
the call signs in again.

The jar is never returned to the agent, never journaled, never put in `meta` in clear.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable, Optional
from urllib.parse import urlsplit

from .. import credentials_store, crypto, egress

log = logging.getLogger("oto_mcp.connectors.api_session")

CONNECTOR = "api"
MODE = "session"
#: The modes that sign in and keep a cookie jar: an account's session, a visitor's.
JAR_MODES = (MODE, "visitor")
SATELLITE = "session_jar"
#: The card fields that decide who signs in: a jar sealed for other values is not used.
_IDENTITY = ("base_url", "username", "password", "session")

#: A vault row of the card: (entity_type, entity_id, account).
Row = tuple[str, str, str]


def _mode(fields: dict) -> str:
    return (fields.get("auth_mode") or "").strip().lower()


def is_session(fields: dict) -> bool:
    """The account's session mode — the one that only reads."""
    return _mode(fields) == MODE


def keeps_jar(fields: dict) -> bool:
    """A mode that signs in and keeps its cookies in the card's row (`JAR_MODES`)."""
    return _mode(fields) in JAR_MODES


def _fingerprint(fields: dict) -> str:
    ident = {k: str(fields.get(k) or "") for k in _IDENTITY}
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()


def guard(base_url: str) -> Callable[[str], None]:
    """The egress guard of every login hop. Raises `egress.EgressRefused`."""
    base_host = (urlsplit(base_url).hostname or "").lower()

    def check(url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        egress.check_url(url, connector=CONNECTOR, field="session",
                         exceptions_declarees=host == base_host)
    return check


def read_jar(row: Row, fields: dict) -> Optional[str]:
    """The jar sealed for these card fields, or None (never sealed, sealed for other
    fields, or unreadable — the call then signs in)."""
    try:
        text = credentials_store.unseal_satellite(*row[:2], CONNECTOR, row[2], SATELLITE)
    # `crypto.unseal` raises its OWN type, never `InvalidTag` (it translates it): catching
    # the latter let a jar sealed for an older row (an account renamed by the first named
    # key, `ensure_named_coexistence`) go up as "Internal server error."
    except (crypto.EnveloppeIndechiffrable, ValueError):
        log.warning("api session: the stored jar of %s/%s (account=%r) does not decrypt "
                    "any more — signing in again", row[0], row[1], row[2])
        return None
    if text is None:
        return None
    try:
        sealed = json.loads(text)
    except ValueError:
        log.warning("api session: the stored jar of %s/%s (account=%r) is not the "
                    "sealed shape — signing in again", row[0], row[1], row[2])
        return None
    if not isinstance(sealed, dict) or sealed.get("for") != _fingerprint(fields):
        return None
    jar = sealed.get("jar")
    return jar if isinstance(jar, str) else None


def jar_writer(row: Row, fields: dict) -> Callable[[str], None]:
    """`on_jar`: seals the jar oto-core hands over into the card's row."""
    fingerprint = _fingerprint(fields)

    def write(jar: str) -> None:
        sealed = json.dumps({"for": fingerprint, "jar": jar})
        if not credentials_store.seal_satellite(*row[:2], CONNECTOR, row[2], SATELLITE,
                                                sealed):
            # The card was removed during the call: nothing to keep the jar for.
            log.info("api session: no vault row any more for %s/%s (account=%r), "
                     "jar not kept", row[0], row[1], row[2])
    return write
