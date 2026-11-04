"""Roles + API key resolution + per-tool quotas.

The `users.role` role decides access to the admin UI, on **3 tiers** (from
weakest to strongest):

- **member**: default role (non-admin), no effect on access to
  tools. Access is decided via `user_grants` (see below).
- **admin** (intermediate OPERATIONAL tier): platform supervision —
  user list, user page, call monitoring, connector activation,
  operational read/admin of orgs. **NO** bulk escalation to third-party orgs.
- **super_admin** (the all-powerful): everything operational + `org_admin`
  escalation on ALL orgs and `group_admin` on ALL groups, platform role
  management, platform keys, token issuance, writes on third-party orgs
  (entitlements, another org's guide), org creation.
  Bootstrap: env `OTO_MCP_ADMIN_SUB` forces this sub to **super_admin**
  whatever is in the DB.

API key resolution per call (`resolve_api_key`):

1. If a user key was set by the user themselves on `/account` → take it,
   no quota.
2. Otherwise, look for an explicit grant in `user_grants` (an admin set an
   authorization) → take the most recently granted `platform_keys.api_key`.
3. Otherwise (including for an admin without a grant) → actionable McpError.

Daily quota: each grant carries an optional `daily_quota` (per-user, set by
the admin at grant time). If null, fall back to the
`OTO_MCP_QUOTA_<PROVIDER>_DAILY` env or `_QUOTA_DEFAULTS`.

Platform keys live in the DB (`platform_keys` vault) — set/rotated via the admin
surface (REST `/api/admin/platform-keys`, `oto_admin_*` meta-tools), no more
SOPS/env import at boot (oto-mcp#12). Importing ≠ auto-granting: a key is only
accessible with an explicit admin grant.

## The package (split of 2026-08-27) — what lives where

`access.py` was 2,000 lines and concentrated four subjects that do not read
together. As the file was the unit of session occupancy on a shared tree, it was
also the bottleneck of all connector work. The split is a **PURE MOVE**: no
caller changes (see `tests/test_access_surface_frozen.py`).

- `scope`   — who acts: platform role, org/team/project of the call, what the
              project PINS, `_UNSET`, and the org the caller is a MEMBER of in the
              call (`org_d_appartenance`: none in a project shared from another org —
              #1499, nothing of that org serves her). Depends on nothing.
- `quotas`  — what is metered (daily quota, usage) and what is paid (paid
              option = declared entitlement of the org or the person, via `entitlements`).
- `cascade` — the SINGLE walker `personal > team > org > tenant > platform`,
              its probes, the platform tier.
- `niveaux` — the LEVEL an account's rules give a caller (none < read < write <
              sensitive < manage), what a call requires, D1–D3 (#1499). Pure.
- `comptes` — the accounts a resolution may choose at a step, read with their rules
              (`ChoixDuParcours`), and the echo of the account that served.
- `rbac`    — who is allowed: hidden tools, instance
              guard, in-scope instances, redaction filter.
- `indices` — the TEXT of the "nothing resolves" refusals: key removed, in-scope
              instances, project that already pins (#499). Read-only, fail-soft.
- `resolved_credential` — the TYPE returned by every resolution (extracted 08/29, #584).
- `tenant_budget` — the per-org budget of the tenant→org edge (L-keys PR 2), applied
              when a tenant winner is resolved.
- `resolve_anon` — resolution for the anonymous MCP endpoint (ADR 0032), extracted
              from `resolve` on 08/29 (#584). The tenant tier only comes in via an edge.
- `resolve` — the actual resolution of a credential (hot path); its refusals
              carry the `indices`.
- `views`   — the thin views: key, fields, mode, option raised,
              resolvability of an org.
- `status`  — the per-connector snapshot of `/api/me`.
- `entitlements` — the DECLARED entitlements of an org or a person (ADR 0070 §7),
              re-read on every use; depends only on `db`, never on `billing`.
- `reglement` — the settlement of a call that succeeded (L8): debits what the tool
              noted (`units`) on the key the resolver noted, at the edge of the call.

The graph is a strict DAG — no cycle, every arrow goes downward:

```
                    scope                    (depends on nothing)
                   ↗  ↑  ↖
            quotas   cascade   niveaux       (quotas → entitlements ; cascade → scope)
                ↑     ↑  ↖       ↑
                |    rbac      comptes       (rbac → scope, cascade ; comptes → niveaux)
                |   ↗   ↑  ↖     ↑
                |  |    |   indices          (indices → rbac)
                |  |    |  ↗     |
              resolve ──────────┘            (resolve → scope, quotas, rbac, indices,
                ↑       |                     cascade, comptes)
              views   status                 (status → scope, quotas, rbac, cascade)
```

## The surface stays FLAT, and the patch point stays `access.<name>`

Two mechanisms, both here and nowhere else:

1. **Flat re-export** — `access.<name>` returns what it returned before the split,
   privates included (`_UNSET`, `_resolve_credential_impl`, `_platform_grant_meta`…
   are consumed from outside). Same idiom as the `db` package.
2. **Write propagation** — a submodule calls its neighbour through the
   MODULE (`scope.current_org(...)`), never through an imported name: that is what
   tells the reader where the function comes from. But then a write on the facade
   (`monkeypatch.setattr(access, "current_org", …)`, the idiom in ~200 places
   of the suite) would no longer reach the inside of the package: the neighbour
   would still read the original. The facade therefore propagates every write to
   the submodules that DEFINE that name. Without it, the move would change the
   behaviour of tests it was not supposed to touch — and it would change it
   SILENTLY, leaving them green on a path that is no longer the one they think
   they exercise.

Propagation goes DOWN, it does not come back up: a write on a submodule
(`access.scope.current_org`) does not reach whoever reads the facade. Hence the
target of #896, kept at zero by `tests/test_facades_lecteurs_cible.py` — outside,
we read and patch the facade, never a submodule it re-exports; inside, we read
the neighbour through the module (`docs/roles-and-resolution.md`).
"""
from __future__ import annotations

import logging
import sys
import types

from . import (scope, quotas, cascade, platform_grant, rbac, indices, resolved_credential,
               tenant_budget, resolve_anon, resolve, views, status, entitlements, niveaux,
               comptes, reglement)

_MODULES = (scope, quotas, cascade, platform_grant, rbac, indices, resolved_credential,
            tenant_budget, resolve_anon, resolve, views, status, entitlements, niveaux,
            comptes, reglement)

# Flat re-export (public + single-underscore privates; dunders stay with the
# package) + `name -> modules that define it` map, which serves the propagation
# below. Names are disjoint across modules, except for commonly imported
# modules (`db`, `connectors`…): there, all holders are recorded, and a write
# on the facade reaches all of them — as when they were a single module.
_OWNERS: dict = {}
_g = globals()
for _mod in _MODULES:
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        _g[_name] = getattr(_mod, _name)
        _OWNERS[_name] = _OWNERS.get(_name, ()) + (_mod,)
del _g, _mod, _name

# `access.logger` stays the logger of the NAME `oto_mcp.access` (the loop above
# would have left the last submodule's one).
logger = logging.getLogger(__name__)


class _Facade(types.ModuleType):
    """The `access` module itself, with write propagation (see §2 of the
    docstring). `__delattr__` is NOT overridden: removing a name from the facade
    must not behead the submodule that serves it."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for mod in _OWNERS.get(name, ()):
            setattr(mod, name, value)


sys.modules[__name__].__class__ = _Facade
