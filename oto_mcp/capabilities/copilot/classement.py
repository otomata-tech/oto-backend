"""What the in-app agent may call, and what each call needs: run, ask, or always ask.

Built on DECLARATIONS first (`tools/lecture.py`): a tool that declares `readOnlyHint`
reads; one that declares `destructiveHint` (`ENVOI`/`ACTE`) acts at a third party or in
the person's name. The spine tools (`oto_*`, `data_*`) carry no annotation: their
read-only ops are listed below, checked against each tool's own `op` enum. ANYTHING
NOT DECLARED A READ ASKS — the default is the card, never a presumed reader.

Three outcomes:
- `read`: runs.
- `write`: a change to the person's own workspace objects (a table, a doc, a project),
  with no outside URL in its arguments: asks, unless the person ticked "don't ask
  again" for that kind (tool + op) in this conversation.
- `destructive`: everything else — a send, a delete, a launch, a write that steers
  other agents later, an access change, an outbound call the agent aims itself, a
  write carrying an outside URL, any undeclared tool. ALWAYS asks, never rememberable.
"""
from __future__ import annotations

import json
import re
from typing import Optional

#: What the model always sees; everything else is reached through `oto_call`.
CORE_TOOLS = (
    "oto_search", "oto_call", "oto_tool_schema", "oto_context", "oto_whoami",
    "oto_doc", "oto_procedure", "oto_trigger", "oto_project", "oto_guide", "oto_fleet",
    "data_list_datastores", "data_get_schema", "data_rows", "data_aggregate",
    "data_write", "data_update_where", "data_patch_schema", "data_create_datastore",
    "feedback",
)

#: Never from the in-app agent, even for a person entitled to them: platform and tenant
#: administration, raw access, session switches and wipes, the run bookkeeping (the
#: server's job). Matched on the TARGET of an `oto_call` too.
_INTERDITS = re.compile(
    r"^(oto_admin_|oto_clear_|oto_use_|oto_shell$|oto_account_access$|oto_enable_tool$|"
    r"oto_disable_tool$|run_start$|run_finish$|oto_run_thread$|oto_call$)")

#: Tools that reach an outside URL or service the agent picks. Text planted in the
#: person's data could steer one to carry that data OUT in a URL: always a card,
#: showing exactly where — declared readers included.
_SORTANTS = re.compile(
    r"^(web_read|web_image|serper_scrape|serper_lens|apify_|http_|browser_|oto_import$|"
    r"oto_upload_url$|.*_webhook$)")

_NOM_DESTRUCTEUR = re.compile(
    r"(delete|drop|remove|send|push|post|publish|_act$|unsubscribe|archive|invite|share|"
    r"transfer|grant|token|access|member|role|mfa|secret|webhook)")
_OP_DESTRUCTRICE = re.compile(
    r"^(delete|archive|send|publish|publish_mcp|set_public|share|transfer|invite|run_now|"
    r"enable|resume|launch|start|push|give|take_over|rotate|connect_source|clear)")

#: Every write of these is a launch: something runs outside the person's screen.
_LANCEURS = frozenset({"oto_trigger", "oto_fleet", "oto_recipe", "oto_function"})
#: Writes that change what OTHER agents do later (the org's guide, a process every agent
#: of it follows, a profile, the org's settings): a planted instruction would act long
#: after this conversation.
_PILOTES = frozenset({"oto_guide", "oto_guide_v2", "oto_procedure", "oto_profile",
                      "oto_org_settings", "oto_org", "oto_group"})

#: Spine tools with no annotation: the ops that only read, from each tool's `op` enum.
OPS_EN_LECTURE: dict[str, frozenset] = {
    "oto_doc": frozenset({"get", "list", "search", "outline", "revisions", "backlinks",
                          "shared_with_me"}),
    "oto_procedure": frozenset({"get", "list", "library_list", "library_get", "share_get",
                                "share_readers"}),
    "oto_project": frozenset({"get", "list", "list_templates", "runs", "inventory",
                              "activity", "lint"}),
    "oto_trigger": frozenset({"get", "list", "deliveries", "shares", "links", "graph"}),
    "oto_fleet": frozenset({"get", "list", "state"}),
    "oto_guide": frozenset({"read", "list"}),
    "oto_connector": frozenset({"list"}),
    "oto_org_settings": frozenset({"get", "preview"}),
    "oto_org_monitoring": frozenset({"summary", "calls", "call", "connectors", "adoption",
                                     "runs", "run", "gaps", "tool_quality", "signals",
                                     "agents_en_echec"}),
    "oto_list_my_tools": frozenset({"list", "search"}),
    "oto_recipe": frozenset({"list", "get", "versions"}),
    "oto_function": frozenset({"list", "get", "versions"}),
    "oto_profile": frozenset({"get"}),
    "oto_apps": frozenset({"list", "get", "revisions", "validate"}),
}
#: Spine tools with no `op` that only read.
OUTILS_EN_LECTURE = frozenset({
    "oto_search", "oto_context", "oto_whoami", "oto_tool_schema", "oto_list_orgs",
    "data_rows", "data_get_schema", "data_list_datastores", "data_aggregate",
    "data_row_history", "data_row_comments",
})
#: Writes that may be remembered ("don't ask again"): the person's own workspace
#: objects. A write to a third party always asks.
_ECRITURES_SIMPLES = frozenset({"data_write", "data_update_where", "data_patch_schema",
                                "data_create_datastore", "oto_doc", "oto_project"})
_URL = re.compile(r"https?://", re.I)

LECTURE, ECRITURE, DESTRUCTIF = "read", "write", "destructive"


def cible(name: str, args: Optional[dict]) -> tuple[str, dict]:
    """The tool that actually runs and its arguments: the TARGET of an `oto_call`."""
    args = args if isinstance(args, dict) else {}
    if name == "oto_call":
        inner = args.get("arguments")
        return str(args.get("name") or ""), inner if isinstance(inner, dict) else {}
    return name, args


def interdit(target: str) -> bool:
    return bool(_INTERDITS.match(target or ""))


def classer(target: str, args: dict, annotations=None) -> str:
    """`read`, `write` or `destructive` for a call to `target` (already unwrapped)."""
    op = str(args.get("op") or "")
    if _SORTANTS.match(target):
        return DESTRUCTIF
    if getattr(annotations, "destructiveHint", None) is True:
        return DESTRUCTIF
    if getattr(annotations, "readOnlyHint", None) is True:
        return LECTURE
    if not op and target in OUTILS_EN_LECTURE:
        return LECTURE
    # A question the agent could not answer, flagged to the team behind the product: no
    # card, or every miss would cost the person a click. A link in it still asks.
    if target == "feedback" and args.get("signal") == "gap" \
            and not _URL.search(json.dumps(args, ensure_ascii=False, default=str)):
        return LECTURE
    if op and op in OPS_EN_LECTURE.get(target, ()):
        return LECTURE
    if (_NOM_DESTRUCTEUR.search(target) or _OP_DESTRUCTRICE.match(op)
            or target in _LANCEURS or target in _PILOTES):
        return DESTRUCTIF
    if target.endswith("_settings") and op == "set":
        return DESTRUCTIF
    if target not in _ECRITURES_SIMPLES:
        return DESTRUCTIF
    # A link to the outside written into a doc or a row could be fetched later (an image
    # in a doc loads on open): ask, every time.
    if _URL.search(json.dumps(args, ensure_ascii=False, default=str)):
        return DESTRUCTIF
    return ECRITURE


def rouge(target: str, args: dict) -> bool:
    """Drawn in red on the card: an actual deletion, not every "always asks"."""
    op = str(args.get("op") or "")
    return bool(re.search(r"(delete|drop|remove)", target)
                or re.match(r"^(delete|archive|unpublish|clear)", op))


def cle_de_sorte(name: str, args: Optional[dict]) -> str:
    """"Don't ask again" is remembered per KIND of change: the tool and its op."""
    target, a = cible(name, args)
    return f"{target}:{a.get('op') or ''}"


#: The axes of a call are the SERVER's, never the model's.
AXES_DU_SERVEUR = ("_org", "_project", "_run_id", "_group")


def epingler(args: Optional[dict], org: Optional[int], projet: Optional[int],
             run_id: Optional[str]) -> dict:
    """The arguments with the server's axes forced: the viewed org, the route's project,
    the conversation's run — on the call AND on an `oto_call`'s target arguments (the
    backend reads the axes from the target, so an `_org` planted there would read another
    of the person's orgs). `_group` is dropped: the in-app agent works in the org being
    viewed."""
    def pin(a) -> dict:
        out = {k: v for k, v in (a or {}).items() if k not in AXES_DU_SERVEUR}
        if org is not None:
            out["_org"] = org
        if projet is not None:
            out["_project"] = projet
        if run_id:
            out["_run_id"] = run_id
        return out
    out = pin(args if isinstance(args, dict) else {})
    if isinstance(out.get("arguments"), dict):
        out["arguments"] = pin(out["arguments"])
    return out
