"""Registers all MCP tools on a FastMCP instance.

Each connector lives in its own module; importing it lazy keeps startup fast
and isolates failures (a missing API key for one connector doesn't kill the
whole server).
"""
from __future__ import annotations

from fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    import logging

    log = logging.getLogger("oto_mcp.tools")

    # Meta-tools — the user controls tool visibility from the conversation.
    # No external dependency, registered first.
    from . import meta
    meta.register(mcp)

    # (The "my situation with oto" profile — `oto_profile` — has been a CAPABILITY
    # since 2026-07-28, mounted by `_mcp_adapter`: no hand-written tool here anymore.
    # ADR 0042 §Surface convergence, Decision 4.)

    # Whoami — current MCP identity (account × active org × active group) served to
    # the agent so it knows who it acts for and in what context. Spine, outside the
    # activation gate, always visible (PROTECTED_TOOLS). No external dependency.
    from . import whoami
    whoami.register(mcp)

    # (Guides — `oto_guide` — have been a CAPABILITY since 2026-07-28, mounted by
    # `_mcp_adapter`. Their per-(sub, org) index always enriches the description at
    # `tools/list` — `DynamicInstructionsMiddleware`, by tool NAME.)

    # Email — sends a free-form message (written by the agent) via the Otomata
    # mailer. Building block for agent-driven onboarding (guide + datastore). Spine,
    # outside the activation gate; super_admin-gated in the handler + hidden by default.
    from . import email
    email.register(mcp)

    # The organization tier (orgs/members/secrets/switch + guide/instructions)
    # is 100% migrated to capabilities (ADR 0009) — mounted by `_mcp_adapter`/`_rest_adapter`
    # from `capabilities.registry`, no `tools/orgs.py` anymore.

    # Datastore (ADR 0016) — platform spine `data_*` on a native PG substrate, plus
    # a Google connector. Loaded explicitly (like meta/orgs), hence outside the
    # activation gate. No external dependency.
    from . import datastore
    datastore.register(mcp)
    # `data_app` in a partner front's own table look — only where the instance
    # declares that front's bundle (OTO_DATA_APP_BUNDLE); else the Prefab card above.
    from . import datastore_table_app
    datastore_table_app.register(mcp)

    # Docs app — MCP App variant rendered from `oto_doc` (read/browse a project's
    # pages + org KB). Spine, outside the activation gate; only registers if the
    # prefab_ui extra is present (guarded import in the module).
    from . import docs_app
    docs_app.register(mcp)

    # Review queue — the only MCP App that writes (a status, on a still-pending
    # row). Spine `data_*`, prefab_ui import guarded as above.
    from . import datastore_review_app
    datastore_review_app.register(mcp)

    # Procedure graph — a procedure's drawing rendered by a partner front's own
    # bundle. Registers only where the instance declares that bundle
    # (OTO_PROCEDURE_APP_BUNDLE); spine, outside the activation gate.
    from . import procedure_app
    procedure_app.register(mcp)

    # Connect card — a connector's name + a Connect button opening its page in the
    # caller's product (`oto_connector op=setup_link`, visual face). Spine, prefab_ui
    # import guarded as above.
    from . import connector_app
    connector_app.register(mcp)

    # Runs (ADR 0017) — run_start/finish verbs (spine). The run_id set in
    # session state is stamped on every tool_call by the calllog sink. No
    # external dependency.
    from . import guide_run
    guide_run.register(mcp)

    # Connectors — loading DERIVED FROM THE REGISTRY (ADR 0010/0011, #24). End of
    # the hardcoded list: for each `kind="tools"` provider, we import its
    # `tools/<m>.py` modules (`Connector.modules`, default = the provider name) and
    # call `register(mcp)`. The `providers/` registry is the ONLY source.
    #
    # - `kind="remote"` is EXCLUDED: handled by remote.register (generic).
    # - try/except per module (uniform resilience): a connector missing an
    #   optional dependency (oto-cli behind, duckdb/o-browser absent, parquet
    #   not found…) disables itself by logging a warning WITHOUT taking the
    #   server down — exactly the class of 502 we are eliminating.
    # - Actual exposure is still governed by per-session VISIBILITY
    #   (UserDisabledToolsMiddleware + connector_activation), not at load time.
    from .. import providers  # oto_mcp.providers (parent package, not tools/)

    loaded: set[str] = set()
    for c in providers.REGISTRY.values():
        if c.kind != "tools":
            continue
        for mod_name in (c.modules or (c.name,)):
            if mod_name in loaded:
                continue
            loaded.add(mod_name)
            try:
                mod = __import__(f"oto_mcp.tools.{mod_name}", fromlist=[mod_name])
                mod.register(mcp)
            except Exception as e:
                log.warning("%s tools disabled: %s", mod_name, e)
