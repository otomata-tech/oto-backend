"""Registry declaration of the `nextmotion` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# nextmotion: aesthetic-medicine clinic management, ADMINISTRATIVE side, read and
# write (clinics, practitioners, calendar, catalogue, sales, leads,
# calls and messages, statistics, stock, settings), plus the patient's IDENTITY.
# keyed api_key (Bearer), BYO only: a key acts on behalf of the
# user of the application that generated it, on the clinics where they are
# employed — a platform key would make no sense.
#
# ⚠️ Vendor of software that HOSTS HEALTH DATA. Medical content
# (history, photos, prescriptions, signed consents, treatments, consultations,
# visits, questionnaire answers) is NOT served; everything that goes out passes
# through an allowlist, so does everything that comes in, and every write is a preview
# (`dry_run`) unless told otherwise. The patient's identity is served
# only by `nextmotion_patient`; elsewhere the patient is just an id — see
# `tools/nextmotion.py`.
#
# Seven modules, one key: the tools of `nextmotion.py` and its siblings, mounted
# together by `modules`.
CONNECTOR = _c(
    "nextmotion", ["nextmotion"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Nextmotion",
    help="aesthetic clinic: calendar, catalogue, quotes, invoices, payments, leads, "
         "statistics, stock, settings, patient identity — read and write "
         "(without the medical record)",
    href="https://www.nextmotion.net",
    modules=("nextmotion", "nextmotion_catalogue", "nextmotion_agenda",
             "nextmotion_ventes", "nextmotion_crm", "nextmotion_patient",
             "nextmotion_analyse"),
)

CATEGORY = "Custom & industry apps"
PUBLISHER = "Nextmotion"
LOGO_DOMAIN = "nextmotion.net"

DESCRIPTION = (
    "The administrative side of an aesthetic-medicine clinic managed with "
    "Nextmotion, read and write: clinics, practitioners, calendar (rooms, "
    "devices, slots, absences, appointments, online requests, free slots), "
    "catalogue and packages, quotes, invoices, credit notes and payments, leads, calls and "
    "messages, revenue statistics, stock, templates and settings; "
    "patient identity (record, search, creation, modification); patient base "
    "and device utilization as aggregates. Every write is a preview first. "
    "The medical record is not served: no history, no photos, no prescriptions, "
    "no treatments, no consultations."
)
