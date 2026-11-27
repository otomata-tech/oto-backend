"""Billing the Apollo phone reveals ordered on the platform key that nobody read back.

A reveal outlives its call: `apollo_reveal_phone` answers with a `request_id`, and
Apollo delivers the numbers minutes later — with what it charged, `credits_consumed`
(8 Apollo credits for a person whose number is found, 0 when none is). Only the read
that collects it (`apollo_reveal_phone_result`) traces that cost for the biller. An
order nobody collects costs the platform and bills nobody. So every order placed on
the platform key is recorded at ordering (`db/apollo_reveals.py`, `apollo_receiver`),
and this daily job does the rest — the Higgsfield settlement's shape
(`higgsfield_releve.py`):

Each platform order whose cost is not yet traced, ordered more than `_ATTENTE` ago, is
settled at what Apollo declared — read from what Apollo POSTed to us, else polled on
the platform key (`webhook_result/{id}`, 0 credit). It gets a journal line shaped like
the read that would have billed it — tool `apollo_reveal_phone_result`, `quantity` in
Apollo credits, `apollo_reveal_request_id` — under the org and person who ORDERED it,
of nature `kind='settlement'` (`journal_jour.KIND_RELEVE`): the billing lens reads it
like any other, the usage lenses do not count it as the person's activity. The biller
keeps one line per order: a late read by the orderer changes nothing.

**An unreadable cost is never zero.** A delivered order whose `credits_consumed` cannot
be read, or one still undelivered after `_ABANDON_APRES` (or with no `request_id` to
poll), is settled at the WORST case of a reveal (`PIRE_CAS_TELEPHONE`): the platform
may have paid it, and a cost we cannot read is not one we can give away.

Run by `oto-mcp maintenance apollo-phones`, before its purge — prod only (the database
is shared).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import credentials_store
from .calllog import truncated_args
from .db import apollo_reveals as db_apollo
from .db import usage
from .db.journal_jour import KIND_RELEVE

logger = logging.getLogger(__name__)

#: What a phone reveal costs Apollo at worst: 8 credits for a person whose number is
#: found (measured on a real account on 2026-11-22). The match it makes is billed on
#: the order's own call (`apollo_reveal_phone`), not here.
PIRE_CAS_TELEPHONE = 8

#: An order is left to its reader this long before the settlement looks at it.
_ATTENTE_MIN = 60

#: Undelivered after this long, an order is settled at the worst case: Apollo keeps a
#: result thirty days, and delivers within minutes.
_ABANDON_APRES = timedelta(days=2)

#: The argument a settlement line carries for an order without a `request_id`: the
#: biller still needs one job per order.
_SANS_ID = "jeton:"


def _instant(valeur: Any) -> datetime:
    lue = (valeur if isinstance(valeur, datetime)
           else datetime.fromisoformat(str(valeur).replace("Z", "+00:00")))
    return lue if lue.tzinfo else lue.replace(tzinfo=timezone.utc)


def cout_declare(enveloppe: Any) -> Optional[int]:
    """What Apollo declared it charged, in Apollo credits — `credits_consumed` of the
    delivered envelope — or None when it cannot be read."""
    cout = enveloppe.get("credits_consumed") if isinstance(enveloppe, dict) else None
    if isinstance(cout, int) and not isinstance(cout, bool) and cout >= 0:
        return cout
    return None


class PlusieursClesPlateforme(RuntimeError):
    """More than one Apollo platform key: which one ordered a reveal is not recorded."""


def _cle_plateforme() -> Optional[str]:
    lignes = credentials_store.list_platform_credentials("apollo")
    if not lignes:
        return None
    if len(lignes) > 1:
        noms = ", ".join(str(l["label"]) for l in lignes)
        raise PlusieursClesPlateforme(
            f"{len(lignes)} Apollo platform keys ({noms}): the settlement serves one")
    ligne = credentials_store.get_credential_with_meta(
        credentials_store.PLATFORM, lignes[0]["label"], "apollo", "")
    if not ligne:
        raise RuntimeError("the Apollo platform row no longer resolves")
    return credentials_store.unpack_secret("apollo", ligne["secret"])["key"]


def _tracer(ligne: dict, credits: int) -> None:
    """The billed line of an order, under its orderer — the tool and the argument name
    the read writes: the billing lens reads it as the same order."""
    job = ligne["request_id"] or (_SANS_ID + str(ligne["token_hash"])[:16])
    usage.insert_tool_call({
        "kind": KIND_RELEVE,
        "tool": "apollo_reveal_phone_result", "sub": ligne["sub"],
        "org_id": ligne["org_id"], "ok": True, "duration_ms": 0,
        "quantity": int(credits), "key_mode": "platform",
        "args": truncated_args({"apollo_reveal_request_id": str(job),
                                "releve_serveur": True}),
    })


def _relever_un(client: Any, ligne: dict, *, dry_run: bool, maintenant: datetime) -> str:
    age = maintenant - _instant(ligne["created_at"])
    if ligne.get("payload") is not None:
        credits = cout_declare(ligne["payload"])
        if credits is None:
            logger.warning("apollo reveal %s: delivered without a readable cost, settled "
                           "at the worst case", ligne["request_id"])
            credits = PIRE_CAS_TELEPHONE
        issue = "factures"
    elif ligne.get("request_id") and client is not None:
        sonde = client.poll_webhook_result(ligne["request_id"])
        if sonde.get("done"):
            credits = cout_declare(sonde.get("result") or {})
            if credits is None:
                logger.warning("apollo reveal %s: done without a readable cost, settled "
                               "at the worst case", ligne["request_id"])
                credits = PIRE_CAS_TELEPHONE
            issue = "factures"
        elif age > _ABANDON_APRES:
            credits, issue = PIRE_CAS_TELEPHONE, "pire_cas"
        else:
            return "en_cours"
    elif age > _ABANDON_APRES:
        credits, issue = PIRE_CAS_TELEPHONE, "pire_cas"
    else:
        return "en_cours"
    if not dry_run:
        if credits > 0:
            _tracer(ligne, credits)
        db_apollo.marquer_releve_jeton(ligne["token_hash"])
    return issue if credits > 0 else "gratuits"


def relever(cle: Optional[str], *, dry_run: bool = False) -> dict:
    """Settle the platform orders whose cost is not yet traced. One failed order is left
    for tomorrow."""
    client = None
    if cle is not None:
        from oto.tools.apollo import ApolloClient
        client = ApolloClient(api_key=cle)
    maintenant = datetime.now(timezone.utc)
    compte = {"factures": 0, "pire_cas": 0, "gratuits": 0, "en_cours": 0, "echecs": 0}
    for ligne in db_apollo.a_relever(_ATTENTE_MIN):
        try:
            compte[_relever_un(client, ligne, dry_run=dry_run, maintenant=maintenant)] += 1
        except Exception:
            logger.warning("apollo reveal %s: settlement failed, retried tomorrow",
                           ligne.get("request_id"), exc_info=True)
            compte["echecs"] += 1
    return compte


def maintenance(*, dry_run: bool = False) -> dict:
    """Settle, BEFORE the purge: an order is never purged unbilled (it is settled at the
    worst case after `_ABANDON_APRES`, long before its thirty days)."""
    return {"releve": relever(_cle_plateforme(), dry_run=dry_run)}
