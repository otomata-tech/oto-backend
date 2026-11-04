"""Comptes Unipile/messagerie, options comp (admin), pending hosted-auth.

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from . import sous_instances
from ._conn import _connect
from .users import upsert_user


def set_unipile_account(sub: str, account_id: str, account_name: Optional[str] = None,
                        org_id: Optional[int] = None, provider: str = "LINKEDIN",
                        platform_seat: bool = False) -> None:
    """Associe (upsert) le compte Unipile `account_id` à `(sub, org_id, provider)`
    — scope MEMBRE (ADR 0033 B4) : le binding vaut DANS cette org. `platform_seat`
    = le compte consomme un siège de la clé plateforme (comptage/facturation par
    org) ; False en BYO."""
    if org_id is None:
        raise ValueError("org_id requis (scope membre, ADR 0033)")
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO unipile_accounts (sub, provider, account_id, account_name, org_id, platform_seat) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sub, org_id, provider) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, account_name = EXCLUDED.account_name, "
            "platform_seat = EXCLUDED.platform_seat, connected_at = NOW(), "
            "disconnected_at = NULL, "  # re-binder = réactiver une ligne soft-déconnectée
            # …et effacer la fin de droit qu'elle portait (`db/unipile_fin_de_droit`) :
            # un compte rebranché n'hérite ni du délai ni du préavis d'avant.
            "entitlement_lost_at = NULL, entitlement_notice_at = NULL",
            (sub, provider, account_id, account_name, org_id, platform_seat),
        )
        # Sa sous-instance, dans la même transaction : la cible de ses règles d'accès
        # (#1499, `db/sous_instances`).
        sous_instances.poser(conn, sub, org_id, provider)


def get_unipile_account_id(sub: str, org_id: Optional[int],
                           provider: str = "LINKEDIN") -> Optional[str]:
    """`account_id` Unipile du user pour ce canal DANS cette org (binding VIVANT),
    ou None. `org_id=None` (défensif) → None, jamais un repli org-agnostique."""
    if org_id is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM unipile_accounts "
            "WHERE sub = %s AND org_id = %s AND provider = %s AND disconnected_at IS NULL",
            (sub, org_id, provider),
        ).fetchone()
    return row["account_id"] if row else None


def seat_binding_elsewhere(sub: str, provider: str = "LINKEDIN",
                           exclude_org: Optional[int] = None) -> Optional[dict]:
    """Binding VIVANT du `sub` sur un SIÈGE plateforme dans une AUTRE org (le plus
    récent), ou None — la source de l'ADOPTION explicite : « ton LinkedIn est déjà
    connecté (via org X) — l'utiliser ici ». Restreint à `platform_seat=True` : un
    compte BYO vit sur la clé de SON org (un account_id n'existe que sur le tenant
    où il a été créé) → jamais proposable sous une autre clé."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id, account_name, org_id FROM unipile_accounts "
            "WHERE sub = %s AND provider = %s AND platform_seat AND disconnected_at IS NULL "
            "AND org_id IS DISTINCT FROM %s ORDER BY connected_at DESC LIMIT 1",
            (sub, provider, exclude_org),
        ).fetchone()
    return dict(row) if row else None


def dead_unipile_account_ids_for(sub: str, provider: str = "LINKEDIN") -> set:
    """`account_id` des bindings soft-déconnectés du `sub` sur ce canal — la preuve
    de propriété durable : à la reconnexion, Unipile RÉUTILISE le compte existant
    (même account_id) → le reconcile peut rebinder déterministiquement (le floor
    heuristique rate toute réutilisation, le compte étant antérieur au pending)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT account_id FROM unipile_accounts "
            "WHERE sub = %s AND provider = %s AND disconnected_at IS NOT NULL",
            (sub, provider),
        ).fetchall()
    return {r["account_id"] for r in rows}


def get_unipile_account(sub: str, org_id: Optional[int],
                        provider: str = "LINKEDIN") -> Optional[dict]:
    """Statut de connexion Unipile d'un canal dans CETTE org, ou None."""
    if org_id is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT provider, account_id, account_name, connected_at FROM unipile_accounts "
            "WHERE sub = %s AND org_id = %s AND provider = %s AND disconnected_at IS NULL",
            (sub, org_id, provider)
        ).fetchone()
    return dict(row) if row else None


def list_unipile_accounts(sub: str) -> list[dict]:
    """Les comptes Unipile CONNECTÉS du user (bindings vivants), tous canaux confondus
    (`[{provider, account_id, account_name, org_id, platform_seat, connected_at}]`) —
    pour le dashboard. `org_id` = l'org à laquelle le compte est rattaché (ventilation
    par org, fiche admin). Les lignes soft-déconnectées sont exclues (elles ne servent
    qu'au rebind, cf. `dead_unipile_account_ids_for`)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider, account_id, account_name, org_id, platform_seat, connected_at "
            "FROM unipile_accounts WHERE sub = %s AND disconnected_at IS NULL "
            "ORDER BY provider", (sub,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_unipile_account(sub: str, org_id: Optional[int], provider: str = "LINKEDIN") -> None:
    """SOFT-déconnecte le canal dans CETTE org (la ligne survit, `disconnected_at`
    posé) — par-org : le binding est un acte par org (modèle explicite), déconnecter
    ICI ne touche pas les autres orgs. La ligne morte reste la preuve de propriété
    du compte (rebind déterministe à la reconnexion, cf. `dead_unipile_account_ids_for`)."""
    if org_id is None:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE unipile_accounts SET disconnected_at = NOW() "
            "WHERE sub = %s AND org_id = %s AND provider = %s",
            (sub, org_id, provider))


def org_unipile_account_ids(org_id: Optional[int], provider: str = "LINKEDIN") -> set:
    """`account_id` VIVANTS rattachés à CETTE org sur ce canal, tous membres confondus.

    Sert la garde du chemin SANS `sub` (endpoint MCP publié d'un projet, ADR 0032) : le
    projet déclare une identité de connecteur, et il faut prouver qu'elle appartient bien
    à l'org propriétaire avant d'opérer sous elle. Sans ce recoupement, un lien de projet
    nommant un `acc_…` quelconque ferait agir l'endpoint sous le compte LinkedIn d'un
    autre tenant — la clé Unipile partagée adresse tout l'abonnement."""
    if org_id is None:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT account_id FROM unipile_accounts "
            "WHERE org_id = %s AND provider = %s AND disconnected_at IS NULL",
            (org_id, provider),
        ).fetchall()
    return {r["account_id"] for r in rows}


def count_unipile_accounts_for_org(org_id: int) -> int:
    """Nombre de SIÈGES de la clé plateforme consommés par cet org (base du plafond
    anti-dérapage sur les comptes hébergés). Les comptes BYO (platform_seat=false)
    et les bindings soft-déconnectés ne comptent pas."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts "
            "WHERE org_id = %s AND platform_seat AND disconnected_at IS NULL",
            (org_id,)
        ).fetchone()["n"]


def unipile_connection_key(org_id: int, sub: str, provider: str) -> str:
    """Clé OPAQUE d'une connexion (un canal d'un membre dans une org) — ce qu'un
    relevé déduplique pour compter une connexion une fois par période.

    Tirée de la clé primaire `(sub, org_id, provider)` et non d'`account_id` : une
    reconnexion peut changer de compte Unipile, elle reste la même connexion. Et elle
    ne nomme pas le membre, parce que la lentille qui la sert est lisible par tout
    membre de l'org."""
    return hashlib.sha256(f"{int(org_id)}:{sub}:{provider}".encode()).hexdigest()[:24]


def list_org_unipile_connections(org_id: int, since: Optional[str] = None) -> list[dict]:
    """Les connexions de messagerie hébergées de CETTE org (lentille
    `org.usage.connections`) : `[{connection_key, provider, platform_seat,
    connected_at, disconnected_at}]`, dates ISO UTC.

    Vivantes, plus les soft-déconnectées APRÈS `since` : coupée le 12, une connexion a
    existé ce mois-là. TOUTES les clés : `platform_seat` dit si la connexion occupe un
    siège de la clé plateforme ou vit sur une clé propre (BYO). Ce qu'on en compte est
    l'affaire du consommateur, pas de cette lecture.

    ⚠️ `connected_at` repart à NOW() à chaque reconnexion (`set_unipile_account`) et la
    table ne garde aucun historique : ce qui a déjà été compté, c'est au consommateur
    de le retenir."""
    clauses, params = ["org_id = %s"], [int(org_id)]
    if since:
        clauses.append("(disconnected_at IS NULL OR disconnected_at >= %s::timestamptz)")
        params.append(since)
    else:
        clauses.append("disconnected_at IS NULL")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, provider, platform_seat, "
            "to_char(connected_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS connected_at, "
            "to_char(disconnected_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS disconnected_at "
            f"FROM unipile_accounts WHERE {' AND '.join(clauses)} ORDER BY connected_at, provider",
            tuple(params),
        ).fetchall()
    return [{"connection_key": unipile_connection_key(org_id, r["sub"], r["provider"]),
             "provider": r["provider"], "platform_seat": bool(r["platform_seat"]),
             "connected_at": r["connected_at"], "disconnected_at": r["disconnected_at"]}
            for r in rows]


def list_unipile_accounts_by_org() -> list[dict]:
    """`[{org_id, provider, account_id, sub}]` des comptes consommant un SIÈGE de la
    clé plateforme (ventilation facturation par org — BYO et déconnectés exclus)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id, provider, account_id, sub FROM unipile_accounts "
            "WHERE platform_seat AND disconnected_at IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def unipile_account_owners(include_disconnected: bool = False) -> list[dict]:
    """Les comptes unipile mappés → propriétaire (sub/email) + org. Pour la vue admin
    « sièges de la clé plateforme » : réconcilier les comptes présents sur l'instance
    partagée avec leurs propriétaires oto.

    `include_disconnected=True` rend AUSSI les bindings soft-déconnectés (`disconnected_at`
    posé). Sans eux, un siège que son propriétaire a simplement déconnecté paraît
    **orphelin** alors qu'il est réclamé — le libérer casse le rebind déterministe et
    on ne sait plus à qui écrire. Un vrai orphelin n'a AUCUNE ligne, morte comprise."""
    where = "" if include_disconnected else " WHERE ua.disconnected_at IS NULL"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ua.account_id, ua.provider, ua.account_name, ua.sub, u.email, "
            "ua.org_id, o.name AS org_name, ua.connected_at, ua.disconnected_at, "
            "ua.platform_seat, ua.entitlement_lost_at "
            "FROM unipile_accounts ua "
            "LEFT JOIN users u ON u.sub = ua.sub "
            "LEFT JOIN orgs o ON o.id = ua.org_id" + where
        ).fetchall()
    return [dict(r) for r in rows]


def get_org_unipile_limit(org_id: int) -> Optional[int]:
    """Plafond de comptes Unipile de l'org (NULL = pas de plafond propre → défaut env)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT unipile_account_limit FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    return row["unipile_account_limit"] if row else None


def set_org_unipile_limit(org_id: int, limit: Optional[int]) -> None:
    """Pose (ou efface, limit=None) le plafond de comptes Unipile d'un org."""
    with _connect() as conn:
        conn.execute(
            "UPDATE orgs SET unipile_account_limit = %s WHERE id = %s", (limit, org_id)
        )


# Sentinelle de `set_option_comp(expires_at=…)` : « ne touche pas à l'échéance ».
# Elle existe parce que le défaut naturel (None) est déjà PORTEUR DE SENS — None veut
# dire « perpétuel » — et que deux appelants re-posent un don sans rien savoir des
# échéances (console connecteur, fiche user). Sans sentinelle, leur geste anodin
# effacerait en silence une date posée ailleurs : le don resterait, l'échéance non.
KEEP_EXPIRY = object()


def set_option_comp(entity_type: str, entity_id: str, option: str,
                    *, granted_by: Optional[str] = None,
                    expires_at: object = KEEP_EXPIRY) -> None:
    """Offre (comp gratuit) une option de connecteur à une entité user|org. Idempotent.

    `expires_at` : omis = l'échéance existante est CONSERVÉE (cf. `KEEP_EXPIRY`) ;
    `None` explicite = don perpétuel (efface une date) ; un datetime = don qui se
    ferme à cette date — `has_option_comp` cesse alors de l'accorder, sans qu'aucune
    ligne ne soit supprimée (donc réversible en effaçant la date)."""
    keep = expires_at is KEEP_EXPIRY
    with _connect() as conn:
        conn.execute(
            "INSERT INTO option_comps (entity_type, entity_id, option, granted_by, "
            "expires_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (entity_type, entity_id, option) "
            "DO UPDATE SET granted_by = EXCLUDED.granted_by, granted_at = NOW(), "
            + ("expires_at = option_comps.expires_at" if keep
               else "expires_at = EXCLUDED.expires_at"),
            (entity_type, str(entity_id), option, granted_by,
             None if keep else expires_at),
        )


def clear_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    """Retire un comp d'option. True si une ligne a été supprimée."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM option_comps WHERE entity_type=%s AND entity_id=%s AND option=%s",
            (entity_type, str(entity_id), option),
        ).rowcount
    return n > 0


def has_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    """Un don VIVANT accorde-t-il cette option ? Une ligne échue (`expires_at` passé)
    ne compte pas — c'est ICI que l'échéance mord, une fois, pour tout le monde :
    `access.has_option` est le seul lecteur de cette fonction (sa version groupée,
    `orgs_with_option_comp`, porte la même règle), et toutes les surfaces
    d'entitlement passent par lui. Le filtre est en SQL (`NOW()`), pas en Python :
    l'horloge de la base est la même pour les trois processus qui la lisent."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM option_comps WHERE entity_type=%s AND entity_id=%s "
            "AND option=%s AND (expires_at IS NULL OR expires_at > NOW())",
            (entity_type, str(entity_id), option),
        ).fetchone() is not None


def orgs_with_option_comp(org_ids: list[int], option: str) -> set[int]:
    """`has_option_comp("org", o, option)` pour plusieurs orgs, en UNE requête :
    même règle d'échéance, lue au même endroit. Lecteur unique : `access.orgs_with_option`."""
    ids = sorted({str(int(o)) for o in org_ids})
    if not ids:
        return set()
    with _connect() as conn:
        return {int(r["entity_id"]) for r in conn.execute(
            "SELECT entity_id FROM option_comps WHERE entity_type='org' "
            "AND entity_id = ANY(%s) AND option=%s "
            "AND (expires_at IS NULL OR expires_at > NOW())",
            (ids, option),
        )}


def list_option_comps(entity_type: str, entity_id: str) -> list[str]:
    """Options offertes (comp) à cette entité — pour l'affichage admin.

    ⚠️ **Ne filtre PAS l'échéance**, à la différence de `has_option_comp` : une
    console admin doit voir le don échu, sinon il devient invisible et donc
    irrécupérable (on ne rouvre pas ce qu'on ne voit plus). Deux questions
    différentes, deux règles assumées — `list_option_comp_rows` rend la date pour
    qui veut les distinguer."""
    with _connect() as conn:
        return [r["option"] for r in conn.execute(
            "SELECT option FROM option_comps WHERE entity_type=%s AND entity_id=%s",
            (entity_type, str(entity_id)),
        )]


def list_option_comp_rows(entity_type: str, entity_id: str) -> list[dict]:
    """Les dons de cette entité AVEC leur date de pose et leur échéance. Pendant
    détaillé de `list_option_comps` (qui ne rend que des noms, contrat du dashboard) :
    ce que lit l'écran d'abonnement pour nommer l'avantage et annoncer sa fin."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT option, granted_by, granted_at, expires_at FROM option_comps "
            "WHERE entity_type=%s AND entity_id=%s ORDER BY option",
            (entity_type, str(entity_id)),
        )]


def list_option_comps_for_option(option: str) -> list[dict]:
    """Bénéficiaires (entity_type, entity_id) d'une option donnée (comp) — l'inverse de
    `list_option_comps`, pour la vue admin connecteur-centrique (ADR 0044 §H : « qui a
    droit à ce connecteur au niveau plateforme »)."""
    with _connect() as conn:
        return [{"entity_type": r["entity_type"], "entity_id": r["entity_id"],
                 "granted_at": r["granted_at"]} for r in conn.execute(
            "SELECT entity_type, entity_id, granted_at FROM option_comps WHERE option=%s",
            (option,),
        )]


def create_unipile_pending(nonce: str, sub: str, org_id: Optional[int] = None,
                           provider: str = "LINKEDIN", platform_seat: bool = False) -> None:
    """Mappe un `nonce` (posé comme `name` sur le lien hosted-auth) au
    `(sub, org de contexte, provider)` + `platform_seat` : la moitié oto de la
    connexion, que la réconciliation consomme. Prune les nonces expirés (> 1h)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_pending WHERE created_at < NOW() - INTERVAL '1 hour'")
        conn.execute(
            "INSERT INTO unipile_pending (nonce, sub, org_id, provider, platform_seat) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (nonce) DO NOTHING",
            (nonce, sub, org_id, provider, platform_seat),
        )


def resolve_unipile_pending(nonce: str) -> Optional[dict]:
    """Consomme un nonce → `{sub, org_id, provider, platform_seat}` (et le supprime),
    ou None si inconnu/expiré."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM unipile_pending WHERE nonce = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour' "
            "RETURNING sub, org_id, provider, platform_seat",
            (nonce,),
        ).fetchone()
    return dict(row) if row else None


def list_unipile_pending_for_sub(sub: str) -> list[dict]:
    """Pendings VIVANTS (<1h) d'un `sub` → `[{nonce, org_id, provider,
    platform_seat, created_at}]`, du plus ancien au plus récent. Base de la
    réconciliation poll-and-bind — LE chemin de liaison depuis le retrait du webhook
    (#581, 2026-08-29)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT nonce, org_id, provider, platform_seat, created_at "
            "FROM unipile_pending WHERE sub = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour' ORDER BY created_at",
            (sub,),
        ).fetchall()
    return [dict(r) for r in rows]


def unipile_pending_floors_elsewhere(sub: str, provider: str,
                                     platform_seat: bool) -> list:
    """Horodatages des pendings VIVANTS (<1h) des AUTRES `sub`, même canal, même
    population de clé (`platform_seat`) — ceux qui pourraient réclamer le même
    compte fraîchement créé.

    La réconciliation sans preuve (oto#247) : sur une clé PARTAGÉE, un compte
    candidat n'est attribuable à `sub` que si personne d'autre n'attend dans la
    même fenêtre. Siège plateforme et clé BYO ne listent pas les mêmes comptes :
    on ne compare qu'une population à elle-même."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT created_at FROM unipile_pending WHERE sub <> %s AND provider = %s "
            "AND platform_seat = %s AND created_at >= NOW() - INTERVAL '1 hour'",
            (sub, provider, platform_seat),
        ).fetchall()
    return [r["created_at"] for r in rows]


def bound_unipile_account_ids() -> set:
    """Tous les `account_id` déjà attribués — bindings vivants ET soft-déconnectés
    (une ligne morte d'un TIERS prouve que le compte est à lui : jamais adoptable
    par un autre au reconcile). L'appelant ré-autorise les morts du sub réclamant
    via `dead_unipile_account_ids_for`."""
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT account_id FROM unipile_accounts").fetchall()
    return {r["account_id"] for r in rows}


def foreign_unipile_account_ids(sub: str) -> set:
    """Les `account_id` attribués à QUELQU'UN D'AUTRE que `sub` — bindings VIVANTS
    et MORTS.

    `bound_unipile_account_ids` vu depuis un réclamant. Une ligne MORTE d'un tiers
    interdit autant qu'une vivante : à la reconnexion Unipile RÉUTILISE le même
    identifiant, elle prouve donc une propriété qui dure. Symétriquement, les lignes
    du réclamant lui-même n'y sont pas — reprendre son propre compte n'a jamais été
    le problème.

    Forme d'ENSEMBLE, pour l'appelant qui teste N candidats en une passe (la
    réconciliation en voit tout l'abonnement partagé) ; le grain unitaire est
    `is_foreign_unipile_account`."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT account_id FROM unipile_accounts WHERE sub <> %s",
            (sub,),
        ).fetchall()
    return {r["account_id"] for r in rows}


def is_foreign_unipile_account(sub: str, account_id: str) -> bool:
    """Ce compte est-il attribué à quelqu'un d'autre que `sub` (ligne vivante ou
    morte) ? Forme SCALAIRE : le webhook n'a qu'un identifiant à confronter, et
    charger tout l'inventaire pour chaque notification serait un scan par appel
    d'une route ANONYME."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM unipile_accounts WHERE account_id = %s AND sub <> %s LIMIT 1",
            (account_id, sub),
        ).fetchone() is not None


def backfill_unipile_member_scope() -> dict:
    """One-shot idempotent (boot, ADR 0033 B4) : `unipile_accounts` passe au grain
    (sub, org, provider). Historique : `org_id` = « org porteuse du SIÈGE plateforme »
    (NULL en BYO) et la résolution l'ignorait → un compte LinkedIn connecté dans
    l'org A servait depuis l'org B. Désormais `org_id` = **org de contexte du
    binding** (toujours posée) et `platform_seat` garde la sémantique facturation.

    Étapes (gardées par l'état du PK — 'org_id' déjà dedans ⇒ no-op) :
    1. marquer `platform_seat=true` sur les lignes historiques à org_id posé
       (= mode plateforme, seules comptées par l'ancien plafond) ;
    2. org_id NULL (BYO/legacy) → org maison du sub ;
    3. verrou : NOT NULL + FK CASCADE + PK (sub, org_id, provider).
    S'exécute APRÈS backfill_personal_orgs (org maison garantie)."""
    from .. import org_store  # lazy (org_store importe credentials_store → db)
    with _connect() as conn:
        pk = conn.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'unipile_accounts'::regclass AND i.indisprimary"
        ).fetchall()
        if any(r["attname"] == "org_id" for r in pk):
            return {"done": True}
        conn.execute(
            "UPDATE unipile_accounts SET platform_seat = TRUE WHERE org_id IS NOT NULL")
        rows = conn.execute(
            "SELECT sub, provider FROM unipile_accounts WHERE org_id IS NULL").fetchall()
    moved = 0
    for r in rows:
        home = org_store.get_active_org(r["sub"])
        if home is None:
            logger.warning("backfill_unipile: pas d'org maison pour %s — skip", r["sub"])
            continue
        with _connect() as conn:
            conn.execute(
                "UPDATE unipile_accounts SET org_id = %s "
                "WHERE sub = %s AND provider = %s AND org_id IS NULL",
                (home, r["sub"], r["provider"]))
        moved += 1
    with _connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id IS NULL").fetchone()["n"]
        if remaining:
            logger.warning("backfill_unipile: %s lignes sans org — PK non migré (retry au prochain boot)",
                           remaining)
            return {"moved": moved, "remaining": remaining}
        conn.execute("ALTER TABLE unipile_accounts ALTER COLUMN org_id SET NOT NULL")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_org_id_fkey")
        conn.execute("ALTER TABLE unipile_accounts ADD CONSTRAINT unipile_accounts_org_id_fkey "
                     "FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_pkey")
        conn.execute("ALTER TABLE unipile_accounts ADD PRIMARY KEY (sub, org_id, provider)")
    logger.info("backfill_unipile_member_scope: moved=%s, PK migré", moved)
    return {"moved": moved, "remaining": 0}
