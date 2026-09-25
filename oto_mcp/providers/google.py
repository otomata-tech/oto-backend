"""Déclaration de registre du connecteur `google` — le COMPTE Google, porteur du
credential des six connecteurs de service (split du 2026-09-26).

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.

Jusqu'au split, `google` portait six namespaces (gmail, tasks, calendar, sheets,
drive, chat) : UNE carte, UNE activation, UN consentement qui demandait les six
scopes d'un coup — dont trois RESTRICTED chez Google (Gmail, Drive, Chat). Un tenant
qui n'offre que Gmail et Drive devait pourtant faire vérifier Chat et Tasks, et un
utilisateur qui ne voulait que son agenda livrait sa boîte mail. Même mouvement que
le split unipile du 2026-08-28 : chaque service est désormais un connecteur à part
entière — sa carte, son activation, sa sélection, sa visibilité, SON consentement
(ses scopes seulement, en autorisation incrémentale sur le même compte) — et
emprunte le compte d'ici (`credential_of="google"`, cf. `service` en bas).

Ce que le compte garde en propre : le coffre (une ligne par adresse, le refresh
token, le client OAuth qui l'a émis), le rappel `/api/google/oauth/callback`, la
liste des comptes et le compte par défaut. Son propre consentement demande les six
scopes sous NOTRE app (l'état d'avant, pour un tableau de bord à carte unique) et
seulement l'identité sous l'app d'un tenant — un partenaire ne demande jamais un
scope que son projet Google ne déclare pas ; ses services les ajoutent un à un
(`auth/google.scopes_for`).
"""
from __future__ import annotations

from ._model import _c

# Un seul namespace depuis le split : `google_*` (le compte). Les six autres sont
# devenus des connecteurs — un namespace n'appartient qu'à UN connecteur.
CONNECTOR = _c(
    "google", ["google"],
    auth_modes={"byo_user"},
    personal_session=True, secret_kind="oauth",
    # OAuth ⟹ la dérivation dirait mono ; or N consentements = N comptes, et le
    # coffre porte une ligne par adresse. Déclaré ici, pas dans une liste transverse.
    cardinality="multi", account_axis_static=True,
    label="Compte Google",
    help="le compte Google que Gmail, Drive, Sheets, Calendar, Tasks et Chat "
         "empruntent — chaque service se connecte depuis sa propre carte",
    modules=("google",),
)

CATEGORY = "Comms"
PUBLISHER = "Google"
LOGO_DOMAIN = "google.com"

DESCRIPTION = (
    "Ton compte Google, par OAuth : le porteur que les six services empruntent. "
    "Chaque adresse Google connectée devient un compte distinct dans le coffre — "
    "plusieurs consentements, plusieurs comptes — et chaque service (Gmail, Drive, "
    "Sheets, Calendar, Tasks, Chat) s'autorise depuis sa carte, avec ses seuls scopes."
)


def service(name: str, *, label: str, help: str, href: str,
            modules: tuple[str, ...] | None = None):
    """Un connecteur de SERVICE Google — une carte par service, sur le compte partagé.

    Ce qu'il partage avec les cinq autres — le mode d'auth, la délégation de credential,
    la cardinalité multi-compte, l'éditeur — est décrit ICI, chez le porteur du compte,
    parce que c'est une propriété du COMPTE et pas du service. Le recopier six fois,
    c'est se donner cinq occasions de le faire diverger (même raison que
    `unipile.channel`).

    Le service ne DÉTIENT rien : `credential_of="google"` renvoie coffre et comptes sur
    le porteur (`providers.credential_provider`). Ce qu'il possède en propre, c'est ce
    qui se gouverne par service — activation, sélection, visibilité de ses tools — et
    SON consentement : `auth/google.SERVICE_SCOPES[name]`, et rien d'autre.

    `href` est celui du SERVICE : ce que la personne autorise, c'est son Gmail ou son
    Drive. `publisher` reste Google — l'éditeur nomme qui reçoit l'appel."""
    return _c(
        name, [name],
        auth_modes={"byo_user"},
        personal_session=True, secret_kind="oauth",
        credential_of="google",
        cardinality="multi", account_axis_static=True,
        publisher="Google",
        label=label, help=help, href=href,
        modules=modules or (name,),
    )
