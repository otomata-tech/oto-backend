"""Ce qui vit HORS de la base et désigne l'org : objets stockés, sièges de messagerie,
comptes de connexion.

Chaque geste est fait AVANT la purge de la base, parce qu'il lit la base pour savoir
quoi faire (les clés d'objet, les bindings Unipile, les comptes qui partent). Chacun
lève s'il échoue : la purge ne part pas, la demande passe en `failed` et le travail
suivant la rejoue — un geste déjà fait ne coûte rien à refaire (404 = déjà parti).
"""
from __future__ import annotations

import logging

_log = logging.getLogger("oto_mcp.effacement")


class EtapeExterneEchouee(RuntimeError):
    pass


def supprimer_objets(cles: set[str]) -> int:
    """Retire chaque objet du stockage. Lève au premier refus (rien n'est tu)."""
    from .. import media_store
    n = 0
    for cle in sorted(cles):
        try:
            media_store.supprimer_objet(cle)
        except media_store.MediaError as e:
            raise EtapeExterneEchouee(f"objet {cle!r} : suppression refusée ({e})") from e
        n += 1
    return n


def liberer_sieges_unipile(org_id: int) -> dict:
    """Les comptes de messagerie de l'org sur la clé PLATEFORME : un compte que plus
    aucune autre org ne sert est rendu (supprimé chez Unipile — sinon on le paie, et la
    session LinkedIn y survit) ; un compte encore servi ailleurs est seulement délié de
    cette org. Un compte sur la clé PROPRE de l'org (BYO) est le sien : on ne le
    supprime pas chez elle, la purge retire son binding."""
    from .. import db
    from ..capabilities import unipile_seats
    lignes = db.unipile_account_owners(include_disconnected=True)
    de_l_org = {r["account_id"] for r in lignes if r.get("org_id") == org_id}
    if not de_l_org:
        return {"rendus": 0, "delies": 0, "hors_plateforme": 0}
    client = unipile_seats._platform_client()
    sur_la_plateforme = set()
    if client is not None:
        try:
            sur_la_plateforme = {a.get("id") for a in client.list_accounts()}
        except Exception as e:  # noqa: BLE001 — panne amont : la purge attend
            raise EtapeExterneEchouee(f"unipile : liste des comptes illisible ({e})") from e
    rendus = delies = hors = 0
    for account_id in sorted(de_l_org):
        rows = [r for r in lignes if r["account_id"] == account_id]
        ailleurs = [r for r in rows if r.get("org_id") != org_id
                    and r.get("disconnected_at") is None]
        if account_id not in sur_la_plateforme:
            hors += 1
            continue
        if ailleurs:
            for r in rows:
                if r.get("org_id") == org_id and r.get("disconnected_at") is None:
                    db.clear_unipile_account(r["sub"], org_id, r.get("provider") or "LINKEDIN")
            delies += 1
            continue
        try:
            unipile_seats.liberer(client, account_id, rows)
        except Exception as e:  # noqa: BLE001 — refus amont : la purge attend
            raise EtapeExterneEchouee(f"unipile : compte {account_id} non rendu ({e})") from e
        rendus += 1
    return {"rendus": rendus, "delies": delies, "hors_plateforme": hors}


def supprimer_comptes_de_connexion(partants: tuple[str, ...]) -> dict:
    """Supprime, dans l'annuaire, le compte de chaque partant — ses sessions et ses
    autorisations partent avec lui chez Logto. Rejouable : un compte déjà parti rend 404.
    Un compte d'un annuaire de TENANT tiers n'est pas le nôtre à supprimer : il est
    compté (`a_faire_par_le_tenant`), le reçu le dit.

    ⚠️ Un JWT déjà émis reste valide jusqu'à son échéance (une heure) : la purge de la
    base attend donc `travail.ATTENTE_APRES_COMPTES` après ce geste, sans quoi le premier
    appel d'un partant encore connecté recréerait sa ligne `users`."""
    from ..auth import annuaire
    from ..tenancy import current, primary_slug
    supprimes, tiers = 0, []
    for sub in partants:
        if current().tenant_of(sub) != primary_slug():
            tiers.append(sub)
            continue
        try:
            annuaire.supprimer_compte(sub)
        except annuaire.SuppressionInachevee as e:
            raise EtapeExterneEchouee(f"compte de connexion : {e}") from e
        supprimes += 1
    return {"supprimes": supprimes, "a_faire_par_le_tenant": len(tiers)}


def supprimer_organisation_logto(logto_org_id: str | None) -> bool:
    """L'organisation Logto miroir de l'org (double authentification exigée), si elle
    en a une."""
    if not logto_org_id:
        return False
    from ..auth import annuaire
    try:
        return annuaire.supprimer_organisation(logto_org_id)
    except annuaire.SuppressionInachevee as e:
        raise EtapeExterneEchouee(str(e)) from e
