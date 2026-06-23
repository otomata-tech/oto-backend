"""Capacités de gestion des adresses expéditrices d'email d'une org (ADR 0009).

L'org_admin déclare les adresses depuis lesquelles `email_send` peut envoyer, et le
**transport** de chacune : `mailer` (service Otomata, domaine vérifié côté Scaleway
TEM + allowlist du service) ou `resend` (BYOK, la clé Resend de l'org vit dans le
coffre — `oto_set_org_secret(provider="resend")`). Lecture = membre ; écriture =
org_admin.

Une déclaration → deux surfaces (MCP `oto_*` + REST `/api/orgs/{id}/email-settings`).
Pattern de référence : `orgs_field_filters.py`.
"""
from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .. import org_store
from ..scheduler import DEFAULT_QUIET_HOURS
from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}

_TRANSPORTS = ("mailer", "resend")


class GetEmailSettingsInput(BaseModel):
    org_id: int


class SetEmailSettingsInput(BaseModel):
    org_id: int
    senders: Optional[list[dict]] = None     # [{email, name?, reply_to?, transport}]
    quiet_hours: Optional[dict] = None       # {tz, start, end} — fenêtre d'envoi interdite


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
        transport = (s.get("transport") or "mailer").strip().lower()
        if transport not in _TRANSPORTS:
            raise AuthzDenied(400, "unknown_transport",
                              f"Transport inconnu : {transport!r} (attendu {list(_TRANSPORTS)}).")
        clean: dict = {"email": email, "transport": transport}
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
    settings = org_store.get_org_email_settings(inp.org_id)
    has_resend = org_store.has_org_secret(inp.org_id, "resend")
    return {
        "org_id": inp.org_id,
        "senders": settings.get("senders") or [],
        "quiet_hours": settings.get("quiet_hours") or DEFAULT_QUIET_HOURS,
        "quiet_hours_default": settings.get("quiet_hours") is None,
        "transports": list(_TRANSPORTS),
        "resend_key_set": has_resend,   # rappel : le transport=resend exige la clé d'org
    }


def _set_email_settings(ctx: ResolvedCtx, inp: SetEmailSettingsInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if inp.senders is None and inp.quiet_hours is None:
        raise AuthzDenied(400, "nothing_to_set", "Fournis `senders` et/ou `quiet_hours`.")
    senders = _validate_senders(inp.senders) if inp.senders is not None else None
    quiet = _validate_quiet_hours(inp.quiet_hours) if inp.quiet_hours is not None else None
    org_store.set_org_email_settings(inp.org_id, senders=senders, quiet_hours=quiet)
    out: dict = {"ok": True, "org_id": inp.org_id}
    if senders is not None:
        out["senders"] = senders
        out["count"] = len(senders)
    if quiet is not None:
        out["quiet_hours"] = quiet
    return out


CAPABILITIES += [
    Capability(
        key="org.email_settings.get", handler=_get_email_settings, Input=GetEmailSettingsInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("Read the org's email sender addresses used by `email_send` "
                     "(each with its transport: mailer or resend), plus whether the "
                     "org's Resend key is set."),
        mcp="oto_get_org_email_settings",
        rest=RestBinding("GET", "/api/orgs/{id}/email-settings", _ID),
    ),
    Capability(
        key="org.email_settings.set", handler=_set_email_settings, Input=SetEmailSettingsInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Set the org's email config for `email_send`. `senders` "
                     "= [{email, name?, reply_to?, transport}], transport ∈ "
                     "{mailer (Otomata service, domain verified on Scaleway TEM + in the "
                     "service allowlist), resend (BYOK — set the org's Resend key via "
                     "oto_set_org_secret provider=resend; domain verified on Resend)} — "
                     "replaces the full list; the first sender is the default when "
                     "`email_send` omits `from_email`. `quiet_hours` = {tz, start, end} "
                     "(hours 0..23, wrap-around midnight ok, ex. Europe/Paris 20→8): emails "
                     "composed inside this window are auto-deferred to the next `end`. "
                     "Pass either field or both (merge)."),
        mcp="oto_set_org_email_settings",
        rest=RestBinding("PUT", "/api/orgs/{id}/email-settings", _ID),
    ),
]
