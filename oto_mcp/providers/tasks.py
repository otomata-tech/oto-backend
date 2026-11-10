"""Registry declaration of the `tasks` connector — Google Tasks, on the Google account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The form common
to the six Google services lives with the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Tasks: the person authorizes THIS service on their Google account, from this
# card, with its own scopes only — the account (the vault row, the refresh token) is
# the `google` connector's, shared with the five other services.
CONNECTOR = service(
    "tasks",
    label="Google Tasks",
    help="your tasks — lists, creation, update, completion; scope `tasks`, granted on your Google account",
    href="https://tasks.google.com",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Tasks, on your Google account: list your task lists, create, update and complete a task. A consent that asks only for the Tasks scope."
)
