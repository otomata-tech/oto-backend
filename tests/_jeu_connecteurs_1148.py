"""Jeu de données réaliste pour les bancs de `GET /api/me/connectors` (oto-backend#1148).

Un compte membre de trois orgs et de plusieurs équipes, et des instances de connecteurs
à TOUS les paliers de la cascade : perso (dont multi-compte), équipe active, autre
équipe, org, autre org, prêtées par un autre membre (à l'équipe et à la personne),
plateforme accordée — plus une sélection (actifs, en pause, retirés), une base
recommandée par l'org et des procédures qui citent des espaces de noms. Le catalogue
est le registre entier (~100 connecteurs).

Les connecteurs sont choisis dans le registre par ce qu'ils ACCEPTENT
(`providers.require_credential`), jamais par un nom écrit en dur : un connecteur retiré
demain ne casse pas le jeu, il en sort.
"""
from __future__ import annotations

from dataclasses import dataclass

SUB = "membre-rapide-1148"
PRETEUR = "preteur-rapide-1148"


@dataclass
class Jeu:
    sub: str
    org: int            # org active
    autre_org: int
    troisieme_org: int
    equipe: int         # équipe active
    autre_equipe: int


def _eligibles(entity_type: str) -> list[str]:
    from oto_mcp import providers
    out = []
    for nom in sorted(providers.REGISTRY):
        try:
            providers.require_credential(entity_type, nom)
        except ValueError:
            continue
        out.append(nom)
    return out


def _cle(entity_type, entity_id, connecteur, *, account="", meta=None):
    from oto_mcp import credentials_store
    credentials_store.set_credential(entity_type, str(entity_id), connecteur,
                                     "valeur-factice-1148", set_by=SUB,
                                     meta=meta, account=account)


def peupler() -> Jeu:
    from oto_mcp import credentials_store, db, group_store, org_store, providers
    from oto_mcp.connectors import selection as connector_selection

    for sub in (SUB, PRETEUR):
        db.upsert_user(sub, email=f"{sub}@example.invalid")
    org = org_store.create_org("org-rapide-1148", created_by=SUB)
    autre = org_store.create_org("org-rapide-1148-b", created_by=SUB)
    troisieme = org_store.create_org("org-rapide-1148-c", created_by=SUB)
    org_store.add_org_member(autre, SUB, "org_member")
    org_store.add_org_member(troisieme, SUB, "org_member")
    org_store.add_org_member(org, SUB, "org_admin")
    org_store.add_org_member(org, PRETEUR, "org_member")
    org_store.set_active_org(SUB, org)

    equipe = group_store.create_group(org, "ventes", created_by=SUB)
    autre_equipe = group_store.create_group(org, "ops", created_by=SUB)
    hors = group_store.create_group(org, "finance", created_by=SUB)   # pas membre
    equipe_b = group_store.create_group(autre, "support", created_by=SUB)
    for g in (equipe, autre_equipe, equipe_b):
        group_store.add_group_member(g, SUB)
    group_store.add_group_member(equipe, PRETEUR)
    group_store.set_active_group(SUB, equipe)

    perso = _eligibles("user")
    partage = _eligibles("org")
    plateforme = _eligibles("platform")

    # Perso (org active) : douze clés, dont une multi-compte.
    for nom in perso[:12]:
        _cle("member", credentials_store.member_id(org, SUB), nom)
    _cle("member", credentials_store.member_id(org, SUB), perso[0], account="second")
    # Équipe active, autre équipe, équipe dont je ne suis pas.
    for nom in partage[:5]:
        _cle("group", equipe, nom)
    for nom in partage[5:8]:
        _cle("group", autre_equipe, nom)
    for nom in partage[8:10]:
        _cle("group", hors, nom)
    # Org active, autre org (+ son équipe et ma clé perso là-bas : « à portée »).
    for nom in partage[10:18]:
        _cle("org", org, nom)
    for nom in partage[18:22]:
        _cle("org", autre, nom)
    for nom in partage[22:24]:
        _cle("group", equipe_b, nom)
    for nom in perso[12:15]:
        _cle("member", credentials_store.member_id(autre, SUB), nom)
    # Comptes d'un autre membre partagés par règle (#1499) : à mon équipe, et à moi.
    from oto_mcp.db import connector_instances, regles_de_comptes
    for i, nom in enumerate(perso[15:19]):
        _cle("member", credentials_store.member_id(org, PRETEUR), nom)
        iid = connector_instances.instance_id_for_vault_row(
            "member", credentials_store.member_id(org, PRETEUR), nom, "")
        sujet = {"subject_group": equipe} if i % 2 else {"subject_user": SUB}
        regles_de_comptes.poser_regle(org, iid, **sujet, level="read", acteur=PRETEUR)
    # Plateforme : accordée à l'org, à moi, et une non accordée.
    for i, nom in enumerate(plateforme[:5]):
        credentials_store.set_platform_key(f"cle-{nom}", nom, "valeur-factice-1148")
        if i % 3 == 0:
            credentials_store.platform_grant(nom, f"org:{org}")
        elif i % 3 == 1:
            credentials_store.platform_grant(nom, f"user:{SUB}")

    # Comptes OAuth : Google perso (deux comptes) et partagé par l'org, Microsoft perso
    # sur une application configurée — c'est la lecture de leur lien, service par
    # service, qui pèse sur la liste.
    from oto_mcp.auth import google as auth_google
    from oto_mcp.auth import microsoft as auth_microsoft
    from oto_mcp.db import connector_settings
    portees = auth_google.SERVICE_SCOPES
    tout = " ".join(s for svc in portees.values() for s in svc)
    gmail_drive = " ".join(portees["gmail"] + portees["drive"])
    moi = credentials_store.member_id(org, SUB)
    _cle("member", moi, "google", account="moi@example.invalid",
         meta={"scopes": tout, "is_default": True, "granted_at": "2026-09-01T00:00:00"})
    _cle("member", moi, "google", account="moi2@example.invalid",
         meta={"scopes": gmail_drive, "granted_at": "2026-09-02T00:00:00"})
    _cle("org", org, "google", account="equipe@example.invalid",
         meta={"scopes": gmail_drive, "is_default": True, "connected_by": SUB,
               "granted_at": "2026-09-03T00:00:00"})
    for cle in ("client_id", "client_secret"):
        connector_settings.set_connector_setting("platform", "", auth_microsoft.CONNECTOR,
                                                 cle, f"factice-{cle}")
    ms = auth_microsoft.service_scopes(auth_microsoft.SERVICES[0])
    _cle("member", moi, auth_microsoft.CONNECTOR, account="moi@example.invalid",
         meta={"scopes": " ".join(ms), "is_default": True})
    # Et un compte Microsoft de l'org et de l'équipe active : leurs paliers se lisent aussi.
    _cle("org", org, auth_microsoft.CONNECTOR, account="boite-org@example.invalid",
         meta={"scopes": " ".join(ms), "connected_by": SUB})
    _cle("group", equipe, auth_microsoft.CONNECTOR, account="boite-equipe@example.invalid",
         meta={"scopes": " ".join(ms), "connected_by": SUB})
    # Session navigateur (connecteur à cookie) : perso.
    sessions = [c.name for c in providers.REGISTRY.values()
                if c.secret_kind == "cookie" and c.name in perso]
    if sessions:
        _cle("member", moi, sessions[0])

    # Sélection : actifs, en pause, retirés ; base recommandée de l'org.
    catalogue = [c["name"] for c in providers.public_catalog()]
    for nom in catalogue[:25]:
        connector_selection.set_state(SUB, nom, connector_selection.ACTIVE, org)
    for nom in catalogue[25:30]:
        connector_selection.set_state(SUB, nom, connector_selection.PAUSED, org)
    for nom in catalogue[30:34]:
        connector_selection.set_state(SUB, nom, connector_selection.ACTIVE, org)
        connector_selection.unselect(SUB, nom, org)
    org_store.set_org_default_connectors(org, catalogue[::7])

    # Procédures de l'org qui citent des espaces de noms d'outils.
    espaces = [ns for c in providers.REGISTRY.values() for ns in c.namespaces]
    for i in range(15):
        corps = " ".join(f"<tool:{espaces[(i * 3 + k) % len(espaces)]}_lire>"
                         for k in range(3))
        org_store.set_instruction("org", org, f"procedure-{i}", f"# P{i}\n{corps}\n",
                                  set_by=SUB)
    return Jeu(sub=SUB, org=org, autre_org=autre, troisieme_org=troisieme,
               equipe=equipe, autre_equipe=autre_equipe)
