"""Output schema declared per connector — for the transformations UI (ADR 0015).

`FieldFilter` (oto-core) matches by **leaf key name**, recursively and case-insensitively,
in a connector's responses. Today the org_admin types these names blind; this registry
declares, per connector, the **notable fields** it may emit
so the dashboard shows them (the connector card's "transformations" tab) instead of
guessing them.

Curated, not derived: there is no source of truth for a connector's output schema
(clients return free-form dicts). So we explicitly declare the leaves worth
redacting. An incomplete/absent schema is acceptable: the UI keeps a free field input since
`FieldFilter` matches any name.

Shape per field:
    {"name": <leaf key>, "label": <UI label>, "type": <hint>, "sensitive": <bool>}

Redaction is applied at the tool boundary (`middleware.field_redaction.FieldRedactionMiddleware`)
for ALL connectors; this registry therefore no longer has to follow any client wiring. To
be extended when a connector emits notable fields to offer to the dashboard.
"""
from __future__ import annotations

# Candidate fields (recruiting use case) — shared by unipile + the ATSs. The names
# cover case/format variants; `FieldFilter` matches the leaf key.
_CANDIDATE_FIELDS: list[dict] = [
    {"name": "first_name", "label": "first name", "type": "string", "sensitive": True},
    {"name": "last_name", "label": "last name", "type": "string", "sensitive": True},
    {"name": "name", "label": "full name", "type": "string", "sensitive": True},
    {"name": "email", "label": "email", "type": "string", "sensitive": True},
    {"name": "phone", "label": "phone", "type": "string", "sensitive": True},
    {"name": "photo_url", "label": "photo", "type": "string", "sensitive": True},
    {"name": "public_profile_url", "label": "public profile URL", "type": "string", "sensitive": True},
    {"name": "headline", "label": "headline", "type": "string", "sensitive": False},
    {"name": "location", "label": "location", "type": "string", "sensitive": False},
]

CONNECTOR_FIELD_SCHEMA: dict[str, list[dict]] = {
    # Silae (FR payroll). ⚠️ No server floor: unlike `payfit`, nothing
    # is masked here until the org sets its policy (see
    # `field_filter_defaults.SERVER_DEFAULTS`). These fields are what the UI OFFERS.
    # Names are the REAL keys of the Silae API (OpenAPI « Partenaires » 2026-09-28):
    # the filter matches a key exactly (case-insensitive), so an invented name
    # (`numeroSecu`, `nom`) masks nothing.
    "silae": [
        {"name": "numeroSecuriteSociale", "label": "social security no. (NIR)", "type": "string", "sensitive": True},
        {"name": "nomUsuel", "label": "usual last name", "type": "string", "sensitive": True},
        {"name": "nomNaissance", "label": "birth name", "type": "string", "sensitive": True},
        {"name": "nomMarital", "label": "married name", "type": "string", "sensitive": True},
        {"name": "nomAffiche", "label": "displayed name", "type": "string", "sensitive": True},
        {"name": "nomSalarie", "label": "employee name", "type": "string", "sensitive": True},
        {"name": "prenom", "label": "first name", "type": "string", "sensitive": True},
        {"name": "dateNaissance", "label": "date of birth", "type": "date", "sensitive": True},
        {"name": "communeNaissance", "label": "place of birth", "type": "string", "sensitive": True},
        {"name": "nomVoie", "label": "street", "type": "string", "sensitive": True},
        {"name": "complementAdresse", "label": "address complement", "type": "string", "sensitive": True},
        {"name": "email", "label": "email", "type": "string", "sensitive": True},
        {"name": "eMailPro", "label": "work email", "type": "string", "sensitive": True},
        {"name": "telephonePortable", "label": "mobile phone", "type": "string", "sensitive": True},
        {"name": "telephoneDomicile", "label": "home phone", "type": "string", "sensitive": True},
        {"name": "iban", "label": "IBAN", "type": "string", "sensitive": True},
        {"name": "iban2", "label": "IBAN (2nd)", "type": "string", "sensitive": True},
        {"name": "iban3", "label": "IBAN (3rd)", "type": "string", "sensitive": True},
        {"name": "bic", "label": "BIC", "type": "string", "sensitive": True},
        {"name": "rib", "label": "RIB", "type": "string", "sensitive": True},
        {"name": "salaireDeBase", "label": "base salary", "type": "number", "sensitive": True},
    ],
    # Folk (Otomata CRM). Contacts: identity + contact details.
    "folk": [
        {"name": "firstName", "label": "first name", "type": "string", "sensitive": True},
        {"name": "lastName", "label": "last name", "type": "string", "sensitive": True},
        {"name": "name", "label": "name (company/person)", "type": "string", "sensitive": True},
        {"name": "emails", "label": "emails", "type": "list", "sensitive": True},
        {"name": "phones", "label": "phones", "type": "list", "sensitive": True},
        {"name": "jobTitle", "label": "job title", "type": "string", "sensitive": False},
    ],
    # Forager (prospecting — job posts/firmographics/contacts). Contact fields
    # resolved by the person_* lookups (detail, reverse by email/phone, personal/work
    # emails, phones) — names aligned with the real Forager schema (`full_name`,
    # `phone_number`…), not those of `_CANDIDATE_FIELDS` (unipile/ATS).
    "forager": [
        {"name": "full_name", "label": "full name", "type": "string", "sensitive": True},
        {"name": "first_name", "label": "first name", "type": "string", "sensitive": True},
        {"name": "last_name", "label": "last name", "type": "string", "sensitive": True},
        {"name": "email", "label": "email", "type": "string", "sensitive": True},
        {"name": "phone_number", "label": "phone", "type": "string", "sensitive": True},
        {"name": "photo", "label": "photo", "type": "string", "sensitive": True},
        {"name": "headline", "label": "headline", "type": "string", "sensitive": False},
    ],
    # Pennylane (FR accounting). Third parties & addresses.
    "pennylane": [
        {"name": "name", "label": "third-party name", "type": "string", "sensitive": True},
        {"name": "emails", "label": "emails", "type": "list", "sensitive": True},
        {"name": "address", "label": "address", "type": "string", "sensitive": True},
        {"name": "billing_address", "label": "billing address", "type": "string", "sensitive": True},
        {"name": "city", "label": "city", "type": "string", "sensitive": False},
        {"name": "postal_code", "label": "postal code", "type": "string", "sensitive": False},
    ],
    # Recruiting — profiles/candidates (anonymization by default, see field_filter_defaults).
    # ⚠️ Keyed `linkedin`, NOT `unipile` — it is the NAMESPACE that the
    # redaction middleware resolves (`namespace_of(tool)`), and since the split of
    # 2026-08-28 it is also a connector. Under `unipile`, an org policy only
    # governed `unipile_connect_start`, which returns no profile: the
    # catalog offered the admin to mask fields on tools that don't
    # serve any, and LinkedIn profiles came out in clear.
    # PayFit (payroll and HR). The connector removes NOTHING in hard: these names are
    # exactly those that the server floor masks (`field_filter_defaults`), and
    # this is where the org_admin finds them to lift them or add more.
    # ⚠️ `absence_type` is not the upstream's name (which says `type`): PayFit serves it
    # under that name precisely so that a rule can target it without touching
    # `emails[].type` & co — see `tools/payfit_socle.py`.
    "payfit": [
        {"name": "socialSecurityNumber", "label": "NIR", "type": "string", "sensitive": True},
        {"name": "numeroSecuriteSociale", "label": "NIR (FR contract)", "type": "string", "sensitive": True},
        {"name": "temporaryTechnicalNumber", "label": "NTT", "type": "string", "sensitive": True},
        {"name": "numeroTechniqueTemporaire", "label": "NTT (FR contract)", "type": "string", "sensitive": True},
        {"name": "iban", "label": "IBAN", "type": "string", "sensitive": True},
        {"name": "bic", "label": "BIC", "type": "string", "sensitive": True},
        {"name": "absence_type", "label": "absence reason", "type": "string", "sensitive": True},
        {"name": "absence_category", "label": "absence category", "type": "string", "sensitive": False},
        {"name": "motifRuptureDeContratDsn", "label": "termination reason (DSN)", "type": "string", "sensitive": True},
        {"name": "birthDate", "label": "date of birth", "type": "date", "sensitive": True},
        {"name": "nationality", "label": "nationality", "type": "string", "sensitive": True},
        {"name": "gender", "label": "gender", "type": "string", "sensitive": True},
        {"name": "addresses", "label": "addresses", "type": "list", "sensitive": True},
        {"name": "phoneNumbers", "label": "phones", "type": "list", "sensitive": True},
        {"name": "emails", "label": "emails", "type": "list", "sensitive": True},
        {"name": "employeeFullName", "label": "employee (accounting entry)", "type": "string", "sensitive": True},
    ],
    # Luma (events). The guests and the calendar's contacts: who registered, how
    # to reach them, what they answered. ⚠️ Names are the REAL leaf keys of the
    # Luma API (a guest is `user_email`/`user_name`…, a contact `email`/`name`…):
    # the filter matches a key exactly. `name` is deliberately NOT offered: it is
    # also the key of an event, a ticket type, a tag and a tier, and a rule on it
    # would mask them all. No server floor (`field_filter_defaults`): none of these
    # is sensitive by nature — the org decides.
    "luma": [
        {"name": "user_email", "label": "guest email", "type": "string", "sensitive": True},
        {"name": "user_name", "label": "guest name", "type": "string", "sensitive": True},
        {"name": "user_first_name", "label": "guest first name", "type": "string", "sensitive": True},
        {"name": "user_last_name", "label": "guest last name", "type": "string", "sensitive": True},
        {"name": "phone_number", "label": "guest phone", "type": "string", "sensitive": True},
        {"name": "registration_answers", "label": "registration answers", "type": "list", "sensitive": True},
        {"name": "email", "label": "contact / host email", "type": "string", "sensitive": True},
        {"name": "first_name", "label": "contact first name", "type": "string", "sensitive": True},
        {"name": "last_name", "label": "contact last name", "type": "string", "sensitive": True},
        {"name": "avatar_url", "label": "photo", "type": "string", "sensitive": True},
    ],
    "linkedin_unipile": _CANDIDATE_FIELDS,
    "ashby": _CANDIDATE_FIELDS,
    "greenhouse": _CANDIDATE_FIELDS,
    "lever": _CANDIDATE_FIELDS,
    "recruitee": _CANDIDATE_FIELDS,
    "teamtailor": _CANDIDATE_FIELDS,
}


def schema_for(service: str) -> list[dict]:
    """Declared output fields of a connector (empty list if not declared)."""
    return CONNECTOR_FIELD_SCHEMA.get(service, [])
