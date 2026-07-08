"""Capacités de redaction de champs par org (ADR 0009 + ADR 0015).

L'org_admin configure, par connecteur, comment les champs sensibles des réponses
sont redactés avant d'atteindre l'agent (masque, pseudonyme cohérent,
généralisation, hash, suppression). Décision « contrôle total org » : la politique
d'org est autoritaire ; sans politique, repli sur le défaut serveur
(`field_filter_defaults`). Lecture = membre ; écriture = org_admin.

Une déclaration → deux surfaces (MCP `oto_*` + REST `/api/orgs/{id}/field-filters`)
via les adaptateurs. Pattern de référence : `orgs_secrets.py`.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from .. import connector_field_schema, connector_schema_store, db, field_filter_defaults, org_store
from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}

# Actions reconnues par le moteur FieldFilter (oto-core). Rejette tout le reste à
# l'écriture (le moteur fail-safe en masque total, mais autant le dire à l'org).
_ACTIONS = {"mask", "drop", "remove", "pseudonym", "generalize", "hash", "anonymize"}

# Schéma des modes pour piloter le formulaire dashboard (action + sous-options).
_ACTION_SCHEMA = [
    {"action": "mask", "label": "Masquer", "params": [
        {"key": "preserve", "type": "select", "label": "Format préservé",
         "options": ["", "email", "phone", "iban"]},
        {"key": "keep_first", "type": "int", "label": "Garder N premiers"},
        {"key": "keep_last", "type": "int", "label": "Garder N derniers"},
    ]},
    {"action": "pseudonym", "label": "Pseudonyme cohérent", "params": [
        {"key": "kind", "type": "select", "label": "Type",
         "options": ["name", "first_name", "last_name", "email", "company",
                     "phone_number", "address"]},
    ]},
    {"action": "generalize", "label": "Généraliser", "params": [
        {"key": "to", "type": "select", "label": "Précision",
         "options": ["year", "month", "department", "range"]},
        {"key": "step", "type": "int", "label": "Pas (mode range)"},
    ]},
    {"action": "hash", "label": "Hacher (SHA-256)", "params": []},
    {"action": "anonymize", "label": "Anonymiser (person_…)", "params": []},
    {"action": "drop", "label": "Supprimer le champ", "params": []},
]


class GetFieldFiltersInput(BaseModel):
    org_id: int
    # Le bloc `schemas` (catalogue de champs de TOUS les connecteurs) pèse ~160 KB et
    # dépasse le plafond de tokens MCP (oto-backend#109) — omis par défaut, opt-in.
    include_schemas: bool = False


class SetFieldFilterInput(BaseModel):
    org_id: int
    service: str
    rules: Optional[list[dict]] = None     # None efface la politique du service
    salt: Optional[str] = None


class PreviewFieldFilterInput(BaseModel):
    org_id: int
    service: str
    payload: Any                            # un échantillon de réponse réel (dict ou list)
    rules: Optional[list[dict]] = None      # règles à tester ; None = politique effective du service
    salt: Optional[str] = None


def _validate_rules(rules: list[dict]) -> None:
    for rule in rules:
        if not rule.get("fields"):
            raise AuthzDenied(400, "rule_without_fields", "Chaque règle doit lister des `fields`.")
        action = rule.get("action", "mask")
        if action not in _ACTIONS:
            raise AuthzDenied(400, "unknown_action",
                              f"Action inconnue : {action!r} (attendu {sorted(_ACTIONS)}).")


def _get_field_filters(ctx: ResolvedCtx, inp: GetFieldFiltersInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    filters = org_store.get_org_field_filters(inp.org_id)
    out = {
        "org_id": inp.org_id,
        "filters": filters,
        "defaults": field_filter_defaults.SERVER_DEFAULTS,   # vide : rien par défaut
        "templates": field_filter_defaults.TEMPLATES,         # jeux applicables en 1 clic
        "schema": _ACTION_SCHEMA,
    }
    if not inp.include_schemas:
        return out
    # Schéma par connecteur = OBSERVÉ (squelette des vraies réponses, source de vérité)
    # fusionné avec le curé (libellés/sensibilité). Union de tous les services connus
    # (observés + déjà configurés + curés) pour ne rien cacher. Volumineux (~160 KB) →
    # opt-in (#109).
    observed = db.get_all_connector_schemas()   # {service: {name: {type, paths}}}
    services = set(connector_field_schema.CONNECTOR_FIELD_SCHEMA) | set(filters) | set(observed)

    def _merged_schema(svc: str) -> list[dict]:
        curated = connector_field_schema.schema_for(svc)
        seen = {f["name"].lower() for f in curated}
        extra = [f for f in connector_schema_store.as_fields(observed.get(svc, {}))
                 if f["name"].lower() not in seen]
        return curated + extra

    out["schemas"] = {svc: _merged_schema(svc) for svc in sorted(services)}
    return out


def _set_field_filter(ctx: ResolvedCtx, inp: SetFieldFilterInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    service = (inp.service or "").strip()
    if not service:
        raise AuthzDenied(400, "empty_service", "service vide.")

    block: Optional[dict]
    if inp.rules is None:
        block = None     # efface la politique de ce connecteur
    else:
        _validate_rules(inp.rules)
        block = {"rules": inp.rules}
        if inp.salt:
            block["salt"] = inp.salt

    org_store.set_org_field_filters(inp.org_id, service, block)
    return {"ok": True, "org_id": inp.org_id, "service": service,
            "cleared": block is None, "rules": 0 if block is None else len(inp.rules or [])}


def _preview_field_filter(ctx: ResolvedCtx, inp: PreviewFieldFilterInput) -> dict:
    """Dry-run : passe un échantillon de réponse réel à travers le filtre et renvoie
    la version redactée — pour voir EXACTEMENT ce qui est masqué (clés imbriquées
    incluses), sans deviner. `rules` fournis = on teste ce brouillon ; sinon on applique
    la politique effective du service (org → défaut serveur)."""
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if not isinstance(inp.payload, (dict, list)):
        raise AuthzDenied(400, "bad_payload", "`payload` doit être un objet ou une liste JSON.")
    service = (inp.service or "").strip()

    from oto.tools.common import FieldFilter

    if inp.rules is not None:
        _validate_rules(inp.rules)
        block: Optional[dict] = {"rules": inp.rules, "salt": inp.salt} if inp.salt else {"rules": inp.rules}
    else:
        configured = org_store.get_org_field_filters(inp.org_id)
        block = configured.get(service) or field_filter_defaults.SERVER_DEFAULTS.get(service)

    ff = FieldFilter(rules=(block or {}).get("rules", []), salt=(block or {}).get("salt"))
    return {"org_id": inp.org_id, "service": service, "redacted": ff.apply(inp.payload)}


CAPABILITIES += [
    Capability(
        key="org.field_filters.get", handler=_get_field_filters, Input=GetFieldFiltersInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("Read the org's field-redaction policy per connector, plus the "
                     "server defaults and the available redaction modes/params. Pass "
                     "include_schemas=true to also get the full per-connector field "
                     "catalog (large — omitted by default)."),
        rest=RestBinding("GET", "/api/orgs/{id}/field-filters", _ID),
    ),
    Capability(
        key="org.field_filters.set", handler=_set_field_filter, Input=SetFieldFilterInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Set the org's field-redaction rules for one connector (service). "
                     "Each rule = {fields:[...], action, ...params}. Actions: mask "
                     "(+preserve email/phone/iban or keep_first/keep_last), pseudonym "
                     "(+kind), generalize (+to year/month/department/range), hash, "
                     "anonymize, drop. Pass rules=null to clear the connector's policy "
                     "(falls back to the server default). The org policy is authoritative."),
        rest=RestBinding("PUT", "/api/orgs/{id}/field-filters/{service}", _ID),
    ),
    Capability(
        key="org.field_filters.preview", handler=_preview_field_filter, Input=PreviewFieldFilterInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=("Dry-run a connector's field-redaction on a real sample response: "
                     "returns the redacted payload so you can SEE exactly which fields "
                     "(incl. nested keys) get masked. Pass `rules` to test a draft, or omit "
                     "to apply the service's effective policy (org → server default)."),
        rest=RestBinding("POST", "/api/orgs/{id}/field-filters/{service}/preview", _ID),
    ),
]
