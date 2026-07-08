"""Capacités de gestion d'email d'une org, PAR CONNECTEUR (ADR 0009).

L'org_admin déclare, **par connecteur email** (`scaleway` = hébergé Otomata ; `resend`
= BYOK), les adresses expéditrices que `email_send` peut utiliser + une fenêtre calme.
Le **transport dérive du connecteur** (plus de champ transport sur l'expéditeur). La
clé Resend, elle, se pose dans le coffre (`oto_set_org_secret(provider="resend")`).
Lecture = membre ; écriture = org_admin.

Modèle calqué sur `orgs_field_filters.py` : get global (toutes les configs keyées par
connecteur) + set par connecteur. Une déclaration → MCP `oto_*` + REST
`/api/orgs/{id}/email-settings[/{connector}]`.
"""
from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .. import org_store, providers
from ..scheduler import DEFAULT_QUIET_HOURS
from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}
_ID_CONNECTOR = {"id": "org_id", "connector": "connector"}


class GetEmailSettingsInput(BaseModel):
    org_id: int


class SetEmailSettingsInput(BaseModel):
    org_id: int
    connector: str                           # "scaleway" | "resend"
    senders: Optional[list[dict]] = None     # [{email, name?, reply_to?}] — SANS transport
    quiet_hours: Optional[dict] = None       # {tz, start, end} — fenêtre d'envoi interdite
    clear_quiet_hours: bool = False          # True = efface la fenêtre du connecteur


def _validate_senders(senders: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for s in senders:
        email = (s.get("email") or "").strip()
        if not email or "@" not in email:
            raise AuthzDenied(400, "bad_sender_email",
                              f"Adresse expéditrice invalide : {email!r}.")
        key = email.lower()
        if key in seen:
            raise AuthzDenied(400, "duplicate_sender", f"Adresse en double : {email}.")
        seen.add(key)
        clean: dict = {"email": email}
        if s.get("name"):
            clean["name"] = str(s["name"]).strip()
        if s.get("reply_to"):
            clean["reply_to"] = str(s["reply_to"]).strip()
        out.append(clean)
    return out


def _validate_quiet_hours(qh: dict) -> dict:
    tz = (qh.get("tz") or DEFAULT_QUIET_HOURS["tz"]).strip()
    try:
        ZoneInfo(tz)
    except Exception:
        raise AuthzDenied(400, "bad_tz", f"Fuseau horaire inconnu : {tz!r} (ex. Europe/Paris).")
    try:
        start, end = int(qh["start"]), int(qh["end"])
    except (KeyError, TypeError, ValueError):
        raise AuthzDenied(400, "bad_quiet_hours", "`start` et `end` (heures 0..23) requis.")
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise AuthzDenied(400, "bad_quiet_hours", "`start`/`end` doivent être dans 0..23.")
    if start == end:
        raise AuthzDenied(400, "bad_quiet_hours", "`start` et `end` doivent différer.")
    return {"tz": tz, "start": start, "end": end}


def _get_email_settings(ctx: ResolvedCtx, inp: GetEmailSettingsInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    return {
        "org_id": inp.org_id,
        "settings": org_store.get_org_email_settings(inp.org_id),   # keyé par connecteur
        "connectors": list(providers.EMAIL_CONNECTOR_TRANSPORT),
        "transports": dict(providers.EMAIL_CONNECTOR_TRANSPORT),
        "quiet_hours_default": DEFAULT_QUIET_HOURS,
        "resend_key_set": org_store.has_org_secret(inp.org_id, "resend"),
    }


def _set_email_settings(ctx: ResolvedCtx, inp: SetEmailSettingsInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    connector = (inp.connector or "").strip()
    if connector not in providers.EMAIL_CONNECTOR_TRANSPORT:
        raise AuthzDenied(400, "unknown_email_connector",
                          f"Connecteur email inconnu : {connector!r} "
                          f"(attendu {list(providers.EMAIL_CONNECTOR_TRANSPORT)}).")
    if inp.clear_quiet_hours and inp.quiet_hours is not None:
        raise AuthzDenied(400, "bad_quiet_hours",
                          "`quiet_hours` et `clear_quiet_hours` sont exclusifs.")
    if inp.senders is None and inp.quiet_hours is None and not inp.clear_quiet_hours:
        raise AuthzDenied(400, "nothing_to_set",
                          "Fournis `senders`, `quiet_hours` ou `clear_quiet_hours`.")
    senders = _validate_senders(inp.senders) if inp.senders is not None else None
    quiet = _validate_quiet_hours(inp.quiet_hours) if inp.quiet_hours is not None else None
    org_store.set_org_email_settings(inp.org_id, connector, senders=senders,
                                     quiet_hours=quiet, clear_quiet_hours=inp.clear_quiet_hours)
    out: dict = {"ok": True, "org_id": inp.org_id, "connector": connector}
    if senders is not None:
        out["senders"] = senders
        out["count"] = len(senders)
    if quiet is not None:
        out["quiet_hours"] = quiet
    if inp.clear_quiet_hours:
        out["quiet_hours"] = None
    return out


CAPABILITIES += [
    Capability(
        key="org.email_settings.get", handler=_get_email_settings, Input=GetEmailSettingsInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("Read the org's email config keyed by connector (scaleway = Otomata-"
                     "hosted, resend = BYOK): per-connector senders + quiet hours, the known "
                     "email connectors, connector→transport map, and whether the org's Resend "
                     "key is set."),
        rest=RestBinding("GET", "/api/orgs/{id}/email-settings", _ID),
    ),
    Capability(
        key="org.email_settings.set", handler=_set_email_settings, Input=SetEmailSettingsInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Set ONE email connector's config for `email_send`. `connector` ∈ "
                     "{scaleway (Otomata-hosted via Scaleway TEM — domain verified + in the "
                     "service allowlist), resend (BYOK — set the org's Resend key via "
                     "oto_set_org_secret provider=resend; domain verified on Resend)}; the "
                     "transport is DERIVED from the connector. `senders` = [{email, name?, "
                     "reply_to?}] (no transport) — replaces this connector's list; the first "
                     "sender across connectors is the default when `email_send` omits "
                     "`from_email`. `quiet_hours` = {tz, start, end} (hours 0..23, wrap-around "
                     "midnight ok): emails composed inside the window are auto-deferred to the "
                     "next `end`. `clear_quiet_hours=true` removes this connector's window. "
                     "Pass any field (merge)."),
        rest=RestBinding("PUT", "/api/orgs/{id}/email-settings/{connector}", _ID_CONNECTOR),
    ),
]
