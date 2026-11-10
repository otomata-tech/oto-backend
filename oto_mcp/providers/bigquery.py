"""Registry declaration of the `bigquery` connector — Google BigQuery, on the Google account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape shared
by the Google services lives with the account holder (`providers/google.service`) —
here, only what distinguishes THIS one (seventh service, 2026-10-02).
"""
from __future__ import annotations

from .google import service

# Google BigQuery: the person authorizes THIS service on their Google account, from this
# card (scope `bigquery`); queries see exactly what THEIR IAM rights
# see, and are billed to the project they designate. Read-only enforced by the
# tools (SELECT dry run required, billed-bytes cap on every query).
CONNECTOR = service(
    "bigquery",
    label="Google BigQuery",
    help="your BigQuery warehouse — explore projects, datasets and tables, run "
         "read-only SQL queries; scope `bigquery`, granted on your Google account",
    href="https://console.cloud.google.com/bigquery",
)

CATEGORY = "Product & data"
LOGO_DOMAIN = "cloud.google.com"
DESCRIPTION = (
    "Google BigQuery, on your Google account: browse projects, datasets and schemas, "
    "free table preview, read-only SQL queries (SELECT) with cost estimate "
    "and billed-bytes cap. Your BigQuery IAM rights apply."
)
