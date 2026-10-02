"""BoondManager — le CRM des ESN : recherche, lecture, création. Rien d'autre.

Wrappe `oto.tools.boondmanager.BoondManagerClient` (API REST Boond, en-tête
`X-Jwt-Client-BoondManager` signé par appel), credential à trois champs résolu
par appel via `access.resolve_credential_fields("boondmanager")` (ADR 0011).

**Surface** (4 tools, volontairement étroite) :
- `boondmanager_search` — contacts, sociétés, opportunités, actions ;
- `boondmanager_get` — l'onglet « information » d'un enregistrement ;
- `boondmanager_dictionary` — les ids des états, types, origines, types
  d'action du compte (sans eux, ni filtre ni action créable) ;
- `boondmanager_create` — **dry-run par défaut** : le corps est validé et rendu
  sans appel ; `dry_run=false` crée pour de vrai.

Pas de mise à jour, de suppression ni de fusion : elles écrasent ou détruisent,
et n'entreront qu'une fois éprouvées sur une instance réelle. Boond compte les
appels d'API par MOIS : aucun tool ne pagine ni n'enchaîne d'appels de lui-même.

Les appels au client sont écrits en clair (`_client().search(…)`) : c'est ce qui
les rend vérifiables par la sonde version-skew (`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

_NAME = "boondmanager"

Entity = Literal["contacts", "companies", "opportunities", "actions"]

# Profondeur rendue par `boondmanager_dictionary` sans `path` : les CLÉS, pas le
# contenu — le dictionnaire complet d'un compte est volumineux.
_DICT_PREVIEW_DEPTH = 2


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _credential_refused(e) -> bool:
    """Boond refuse un jeton invalide en 422 (« unable to load agency key »), pas
    en 401 : l'erreur porte alors `source.parameter` = `xJwtClient`."""
    if e.status_code in (401, 403):
        return True
    if e.status_code != 422 or not isinstance(e.body, dict):
        return False
    return any(isinstance(err, dict)
               and str((err.get("source") or {}).get("parameter", "")).lower()
               .startswith("xjwt")
               for err in e.body.get("errors") or [])


def _upstream_message(e) -> str:
    status = e.status_code
    if _credential_refused(e):
        return (f"BoondManager refused access (HTTP {status}): the client token, "
                "client key or user token is wrong, REST API access is not "
                "allowed on the Boond account, or this user lacks the right on "
                "this record.")
    if status == 404:
        return "BoondManager: not found (404) — check the id and the entity."
    if status == 422:
        return ("BoondManager rejected the data (HTTP 422): "
                f"{str(e.body)[:600]}")
    if status == 429:
        return ("BoondManager: API limit reached (429). Boond counts API calls "
                "per month and per minute; retry later, and narrow searches "
                "rather than paging through everything.")
    if status >= 500:
        return f"BoondManager is temporarily unavailable (HTTP {status}); retry later."
    return f"BoondManager refused the request (HTTP {status}): {str(e.body)[:400]}"


_HINT = "full=true returns the raw JSON:API payload."


def _slim_record(rec: Any) -> Any:
    """Un enregistrement JSON:API resserré : `id`, `type`, `attributes`, et ses
    relations réduites à leurs ids (`{"company": "12", "influencers": ["3"]}`)."""
    if not isinstance(rec, dict):
        return rec
    out = {k: rec[k] for k in ("id", "type", "attributes") if k in rec}
    rels = {}
    for name, rel in (rec.get("relationships") or {}).items():
        data = rel.get("data") if isinstance(rel, dict) else None
        if isinstance(data, list):
            rels[name] = [d.get("id") for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            rels[name] = data.get("id")
    if rels:
        out["relationships"] = rels
    return out


def _slim(payload: Any, full: bool) -> Any:
    """Vue resserrée d'une recherche (sauf `full`) : les enregistrements resserrés,
    `meta` intact, `included` retiré — et la réponse NOMME ce qu'elle a retiré."""
    if full or not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    out = {k: v for k, v in payload.items() if k not in ("data", "included")}
    out["data"] = ([_slim_record(r) for r in data] if isinstance(data, list)
                   else _slim_record(data))
    omitted = ["relationships.links"]
    if "included" in payload:
        omitted.insert(0, "included")
    out["projection"] = {"omitted": omitted, "hint": _HINT}
    return out


def _keys_preview(value: Any, depth: int) -> Any:
    """La forme d'un dictionnaire : ses clés sur `depth` niveaux, sans valeurs."""
    if not isinstance(value, dict):
        return f"<{type(value).__name__}>"
    if depth <= 1:
        return sorted(value)
    return {k: _keys_preview(v, depth - 1) for k, v in value.items()}


def _at_path(payload: Any, path: str) -> Any:
    """Descend `a.b.c` dans le dictionnaire (sous `data` s'il y en a un) ; une clé
    absente lève en NOMMANT celles qui existent à cet endroit."""
    node = payload.get("data", payload) if isinstance(payload, dict) else payload
    walked = []
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            here = sorted(node) if isinstance(node, dict) else []
            where = ".".join(walked) or "(root)"
            raise _bad(f"`path`: no key {key!r} under {where}. Keys there: "
                       + (", ".join(here[:60]) or "none") + ".")
        node = node[key]
        walked.append(key)
    return node


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : `GET /application/current-user`, un appel
    authentifié qui dit aussi pour QUI les appels agiront."""
    from oto.tools.boondmanager import BoondManagerClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        BoondManagerClient(client_token=fields.get("client_token"),
                           client_key=fields.get("client_key"),
                           user_token=fields.get("user_token")).current_user()
    except UpstreamHTTPError as e:
        if _credential_refused(e):
            raise connector_verify.NonAutorise(
                f"BoondManager HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"BoondManager HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.boondmanager import BoondManagerClient
    from oto.tools.boondmanager.client import build_create_body
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> BoondManagerClient:
        creds = access.resolve_credential_fields(_NAME)
        return BoondManagerClient(client_token=creds.get("client_token"),
                                  client_key=creds.get("client_key"),
                                  user_token=creds.get("user_token"))

    def _run(fn):
        try:
            return fn()
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))

    @mcp.tool()
    def boondmanager_search(
        entity: Entity,
        keywords: Optional[str] = None,
        keywords_type: Optional[str] = None,
        period: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        filters: Optional[dict] = None,
        sort: Optional[str] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        page: Optional[int] = None,
        max_results: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Search BoondManager contacts, companies, opportunities or actions
        (calls, meetings, notes logged on a record).

        `keywords` is free text, or ids with Boond's prefixes: `CCON12` (contact
        12), `CSOC3` (company 3), `AO7` (opportunity 7)… — e.g. the contacts of
        company 3: `entity="contacts", keywords="CSOC3"`; the actions on contact
        12: `entity="actions", keywords="CCON12"`. A prefix the entity does not
        understand is refused (Boond would silently return nothing).

        `keywords_type` narrows where free text is matched — contacts: default,
        lastName, firstName, fullName (`Last#First`), strictFullName,
        companyFullName (`CSOCid#Last#First`), emails, phones, socialNetworks;
        companies: default, name, phones, emails, socialNetworks.

        `period` + `start_date`/`end_date` (YYYY-MM-DD) bound the search —
        contacts/companies: created, updated, noAction, withActions,
        withoutActions; opportunities: created, updatedPositioning, started,
        updated, closingDate, noAction, withActions, withoutActions; actions:
        started, created, updated.

        `filters` = the entity's list filters, values are ids from
        `boondmanager_dictionary` — contacts: states, companyStates, typesOf,
        activityAreas, tools, expertiseAreas, origins, flags, influencers,
        returnMoreData; companies: states, expertiseAreas, origins, flags,
        influencers, returnMoreData; opportunities: opportunityStates,
        opportunityTypes, positioningStates, expertiseAreas, activityAreas,
        tools, places, durations, origins, flags, returnMoreData; actions:
        actionTypes, origins, flags. `returnMoreData` adds — contacts:
        lastAction, previousAction, nextAction; companies: previousAction,
        nextAction; opportunities: hrManager, previousAction, nextAction,
        alerts. Example: `{"states": [1, 2], "returnMoreData": ["lastAction"]}`.

        ⚠️ Boond counts API calls per MONTH (500 per manager on its Core plan):
        prefer one precise search with a large `max_results` over many pages.
        Results: `data` (records with `id`, `type`, `attributes`, and
        `relationships` reduced to ids, e.g. `{"company": "12"}`),
        `meta.totals.rows` = total matches. The side-loaded `included` records
        are dropped (named under `projection`); `full=true` returns the raw
        payload.

        Args:
            entity: contacts | companies | opportunities | actions.
            keywords: free text and/or prefixed ids, space-separated.
            keywords_type: where free text is matched (see above).
            period: which date `start_date`/`end_date` bound (see above).
            start_date: YYYY-MM-DD (or 'YYYY-MM-DD HH:MM:SS').
            end_date: YYYY-MM-DD (or 'YYYY-MM-DD HH:MM:SS').
            filters: entity list filters, `{name: value | [values]}`.
            sort: column to sort on — contacts: company.name, town, lastName,
                firstName, function, state, company.expertiseArea,
                mainManager.lastName, updateDate; companies: name, information,
                town, state, expertiseArea, mainManager.lastName, updateDate;
                opportunities: creationDate, title, company.name, place,
                numberOfActivePositionings, startDate, endDate, duration, state,
                alertCount, closingDate, updateDate, answerDate,
                totalWeightedTurnOverExcludingTax, mainManager.lastName;
                actions: startDate, typeOf, mainManager.lastName,
                dependsOn.lastName, dependsOn.name, dependsOn.title,
                dependsOn.id…
            order: asc (default) | desc.
            page: page number, from 1.
            max_results: rows per page, 1-500 (1-100 for actions; default 30).
            full: raw JSON:API payload instead of the trimmed view.
        """
        return _slim(_run(lambda: _client().search(
            entity, keywords=keywords, keywords_type=keywords_type, period=period,
            start_date=start_date, end_date=end_date, filters=filters, sort=sort,
            order=order, page=page, max_results=max_results)), full)

    @mcp.tool()
    def boondmanager_get(entity: Entity, record_id: int) -> Any:
        """One BoondManager record: the information tab of a contact, company or
        opportunity, or an action. Its related records are listed under
        `relationships` (ids) and `included`. For a record's actions, use
        `boondmanager_search(entity="actions", keywords="CCON<id>")` (contact),
        `CSOC<id>` (company) or `AO<id>` (opportunity).

        Args:
            entity: contacts | companies | opportunities | actions.
            record_id: the record's numeric id (without prefix).
        """
        return _run(lambda: _client().get(entity, record_id))

    @mcp.tool()
    def boondmanager_dictionary(path: Optional[str] = None,
                                language: Optional[Literal["fr", "en", "es"]] = None
                                ) -> Any:
        """The BoondManager account's dictionary: the ids behind states, types,
        origins, activity areas, action types… — what `filters`, `state`,
        `typeOf` and `origin` take. Each account has its own.

        Without `path`, returns only the dictionary's KEYS (it is large); then
        call again with a dotted `path` to read one branch, e.g.
        `setting.state.contact` (also company, opportunity),
        `setting.typeOf.contact`, `setting.action.contact` (action types on
        contacts; also opportunity), `setting.origin`, `setting.activityArea`,
        `setting.tool`, `setting.expertiseArea`. An unknown key is refused with the keys that exist
        there. Each call is one API call.

        Args:
            path: dotted path of the branch to return.
            language: fr | en | es — language of the labels.
        """
        payload = _run(lambda: _client().dictionary(language=language))
        if not path:
            root = payload.get("data", payload) if isinstance(payload, dict) else payload
            return {"keys": _keys_preview(root, _DICT_PREVIEW_DEPTH),
                    "hint": "call again with `path`, e.g. setting.state.contact"}
        return {"path": path, "value": _at_path(payload, path)}

    @mcp.tool()
    def boondmanager_create(
        entity: Entity,
        attributes: dict,
        relationships: Optional[dict] = None,
        dry_run: bool = True,
    ) -> Any:
        """Create ONE BoondManager contact, company, opportunity or action. **Dry
        run by default**: the body is validated and returned, nothing is sent —
        call again with `dry_run=false` to create. Never updates or deletes.

        Search first (`boondmanager_search`) so as not to create a duplicate:
        Boond does not deduplicate.

        Required — contact: `firstName`, `lastName` + relationship `company`
        (Boond has no contact without a company: create or find it first);
        company: `name`; opportunity: `title`; action: `typeOf` (an action type
        id from `boondmanager_dictionary`) + relationship `dependsOn` (the record
        it is logged on).

        Attributes are checked against Boond's creation schema before anything
        is sent: an unknown name, a wrong type or a text over its length is
        refused with the accepted list. Main ones — contact: civility, email1-3,
        phone1-2, function, department, address, postcode, town, country, state,
        typesOf (ids), origin `{"typeOf": id, "detail": text}`, socialNetworks
        `[{"network": linkedin|x|facebook|viadeo, "url": …}]`; company:
        website, phone1, address, postcode, town, country, state, staff,
        expertiseArea, vatNumber, registrationNumber, apeCode; opportunity:
        reference, state, typeOf (≥1), place, startDate (YYYY-MM-DD or
        "immediate"), endDate, duration, estimatesExcludingTax, isVisible;
        action: title, text, description, location, startDate/endDate as
        `2026-10-02T09:30:00+0200` (offset without colon).

        Relationships are `{name: {"type": …, "id": …}}` — contact: company,
        mainManager, agency, pole, influencers (list); company: parentCompany,
        mainManager, agency, pole, influencers (list); opportunity: company +
        contact (both or neither), mainManager, agency, pole; action:
        dependsOn (type contact, opportunity, project, resource, candidate,
        order or invoice — not company), company, mainManager. E.g.
        `{"company": {"type": "company", "id": 12}}`,
        `{"dependsOn": {"type": "contact", "id": 34}}`. `mainManager`
        (`{"type": "resource", …}`) defaults to the token's user.

        Args:
            entity: contacts | companies | opportunities | actions.
            attributes: the record's fields (Boond attribute names).
            relationships: linked records, `{name: {"type", "id"}}`.
            dry_run: True (default) validates and shows the body without sending.
        """
        if dry_run:
            body = _run(lambda: build_create_body(entity, attributes, relationships))
            return {"dry_run": True, "would_post": f"/{entity}", "body": body,
                    "to_create": "call again with dry_run=false"}
        return _run(lambda: _client().create(entity, attributes, relationships))
