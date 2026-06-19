"""Accès DB du palier organization (= périmètre / store serveur).

Domaine isolé du monolithe `db.py` : les tables org (orgs, org_members,
org_entitlements) restent déclarées dans `db._SCHEMA` (DDL centralisée, jouée
par `init_db`), mais leurs requêtes vivent ici. Les credentials d'org vivent
dans le coffre chiffré `connector_credentials` (entity_type='org'), pas dans
une table dédiée. Réutilise les primitives partagées de `db` (`_connect`,
`upsert_user`) plutôt que de les dupliquer.

Consommé par : `access.resolve_api_key`/`status_for` (reads org credential) et
`tools/orgs.py` (meta-tools de gestion). Cf. project_oto_mcp_org_tier.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from typing import Optional

from . import credentials_store
from . import connectors
from . import db
from .db import _connect, _hash_token, upsert_user


def _alpha_invite_quota() -> int:
    """Quota referral crédité à un invité alpha qui accepte (ADR 0013)."""
    return int(os.environ.get("OTO_ALPHA_INVITE_QUOTA", "3"))

ORG_ROLES = ("org_admin", "org_member")


# --- reads consommés par la résolution de clé (barreau 2) -------------------

def get_active_org(sub: str) -> Optional[int]:
    """org_id de l'organisation active du `sub`, ou None s'il n'en a aucune.

    L'index partiel `org_members_one_active` garantit au plus une ligne active
    par sub ; LIMIT 1 reste défensif (ne jamais supposer exactement une TRUE).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE sub = %s AND is_active LIMIT 1",
            (sub,),
        ).fetchone()
        return int(row["org_id"]) if row else None


def get_org_secret(org_id: int, provider: str) -> Optional[str]:
    """Clé du secret partagé `provider` possédé par l'org, ou None.

    `provider` validé dans le store (require_keyed). La restriction aux providers
    org-partageables (exclut slack) est portée par la couche access et le
    write-path.

    Lit le coffre chiffré `connector_credentials` (entité 'org', déchiffre).
    """
    return credentials_store.get_credential("org", str(org_id), provider)


def has_org_secret(org_id: int, provider: str) -> bool:
    """Présence d'un org_secret SANS le déchiffrer (status_for)."""
    return credentials_store.has_credential("org", str(org_id), provider)


# --- écritures + lectures de gestion (barreau 3, meta-tools platform_admin) --

def create_org(name: str, created_by: Optional[str] = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("nom d'org requis")
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO orgs (name, created_by) VALUES (%s, %s) RETURNING id",
            (name, created_by),
        ).fetchone()
        return int(row["id"])


def get_org(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, created_by, created_at, logo_url FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        return dict(row) if row else None


def list_all_orgs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_by, created_at, logo_url FROM orgs ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


# --- baseline de toolset de l'org (preset de visibilité, ADR 0015) ----------

def get_org_default_tools(org_id: int) -> Optional[list[str]]:
    """Preset par défaut de l'org (liste de noms de tools) = baseline de visibilité
    pour ses membres, ou None si l'org n'en impose pas. `[]` ≠ None : baseline
    « rien ». Miroir d'`org_groups.default_tools` (ADR 0012) hissé au niveau org."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT default_tools FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    if not row:
        return None
    dt = row["default_tools"]
    return list(dt) if dt is not None else None


def set_org_default_tools(org_id: int, tools: Optional[list[str]]) -> bool:
    """Pose (ou efface si None) la baseline de toolset de l'org. False si absente."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET default_tools = %s WHERE id = %s",
            (list(tools) if tools is not None else None, org_id),
        )
        return (cur.rowcount or 0) > 0


def set_org_logo(org_id: int, url: Optional[str]) -> None:
    """Pose (ou efface si url=None) l'URL publique du logo de l'org.

    URL publique (Object Storage), pas un secret → colonne en clair."""
    with _connect() as conn:
        conn.execute("UPDATE orgs SET logo_url = %s WHERE id = %s", (url, org_id))


# --- redaction de champs par connecteur (FieldFilter, ADR 0015) -------------

def get_org_field_filters(org_id: int) -> dict:
    """Politique de redaction de champs de l'org (par connecteur).

    Forme : `{ "<service>": { "salt"?, "rules": [...] } }`. `{}` si rien posé."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT field_filters FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        if not row:
            return {}
        return dict(row["field_filters"] or {})


def set_org_field_filters(org_id: int, service: str, block: Optional[dict]) -> bool:
    """Pose (ou efface si block=None) la politique de redaction d'un connecteur.

    Écriture par service (merge dans le JSONB existant) pour ne pas écraser les
    autres connecteurs. Prose de config, pas un secret → colonne en clair."""
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT field_filters FROM orgs WHERE id = %s FOR UPDATE", (org_id,)
            ).fetchone()
            if not row:
                return False
            current = dict(row["field_filters"] or {})
            if block is None:
                current.pop(service, None)
            else:
                current[service] = block
            conn.execute(
                "UPDATE orgs SET field_filters = %s::jsonb WHERE id = %s",
                (json.dumps(current), org_id),
            )
            return True


def add_org_member(org_id: int, sub: str, org_role: str = "org_member") -> None:
    """Ajoute (ou met à jour le rôle d') un membre. Auto-promeut l'org en active
    si c'est la 1ère adhésion du sub.

    Contrairement à set_google_oauth (table sans index unique partiel sur le
    flag, où deux TRUE sont tolérés), org_members a l'index partiel
    `org_members_one_active`. Le calcul make_active=(COUNT==0) est donc une
    lecture-modification-écriture qui, sous READ COMMITTED, casserait sur deux
    1ères adhésions concurrentes du MÊME sub (les deux liraient COUNT=0 →
    deux is_active=TRUE → IntegrityError). On sérialise par sub via un verrou
    advisory transactionnel ; `conn.transaction()` seul ne donne que
    l'atomicité, pas cette sérialisation.
    """
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r} (attendu: {ORG_ROLES})")
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (sub,))
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM org_members WHERE sub = %s", (sub,)
            ).fetchone()["n"]
            make_active = n == 0
            conn.execute(
                """
                INSERT INTO org_members (org_id, sub, org_role, is_active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (org_id, sub) DO UPDATE SET org_role = EXCLUDED.org_role
                """,
                (org_id, sub, org_role, make_active),
            )


def remove_org_member(org_id: int, sub: str) -> bool:
    """Retire un membre. Si on retire son org active et qu'il en reste, promeut
    la plus ancienne restante (mirroir delete_google_oauth)."""
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_members WHERE org_id = %s AND sub = %s", (org_id, sub)
            )
            removed = (cur.rowcount or 0) > 0
            if removed:
                # Retirer de l'org = sortir de tous ses groupes (ADR 0012 :
                # l'appartenance groupe est subordonnée à l'appartenance org).
                conn.execute(
                    """
                    DELETE FROM org_group_members
                     WHERE sub = %s AND group_id IN (
                         SELECT id FROM org_groups WHERE org_id = %s
                     )
                    """,
                    (sub, org_id),
                )
                has_active = conn.execute(
                    "SELECT 1 FROM org_members WHERE sub = %s AND is_active", (sub,)
                ).fetchone()
                if not has_active:
                    conn.execute(
                        """
                        UPDATE org_members SET is_active = TRUE
                         WHERE sub = %s AND org_id = (
                             SELECT org_id FROM org_members
                              WHERE sub = %s ORDER BY joined_at ASC LIMIT 1
                         )
                        """,
                        (sub, sub),
                    )
            return removed


def set_active_org(sub: str, org_id: int) -> bool:
    """Bascule l'org active du sub. False si le sub n'est pas membre de l'org."""
    with _connect() as conn:
        with conn.transaction():
            hit = conn.execute(
                "SELECT 1 FROM org_members WHERE org_id = %s AND sub = %s", (org_id, sub)
            ).fetchone()
            if not hit:
                return False
            # Deux passes (vider puis poser) : un seul UPDATE `is_active=(org_id=%s)`
            # viole transitoirement l'index partiel `org_members_one_active` (≤1 TRUE
            # par sub) car Postgres le vérifie ligne par ligne — la nouvelle TRUE peut
            # exister avant que l'ancienne passe FALSE. On efface tout, puis on pose.
            conn.execute(
                "UPDATE org_members SET is_active = FALSE WHERE sub = %s AND is_active",
                (sub,),
            )
            conn.execute(
                "UPDATE org_members SET is_active = TRUE WHERE sub = %s AND org_id = %s",
                (sub, org_id),
            )
            # Invariant ADR 0012 : le groupe actif appartient à l'org active.
            # Basculer d'org invalide donc le groupe actif (qui pointait l'ancienne
            # org) — on l'efface ; le membre re-choisira un groupe de la nouvelle
            # org via group_store.set_active_group. SQL direct (pas d'import
            # group_store → pas de cycle ; org_store reste le socle).
            conn.execute(
                "UPDATE org_group_members SET is_active = FALSE WHERE sub = %s AND is_active",
                (sub,),
            )
            return True


def clear_active_org(sub: str) -> None:
    """Désélectionne l'org active → profil perso/global (org_id=0, ADR 0015).
    Efface aussi le groupe actif (invariant ADR 0012 : pas d'org → pas de groupe)."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE org_members SET is_active = FALSE WHERE sub = %s AND is_active", (sub,))
            conn.execute(
                "UPDATE org_group_members SET is_active = FALSE WHERE sub = %s AND is_active", (sub,))


def list_orgs_for_user(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.org_id, o.name, o.logo_url, m.org_role, m.is_active, m.joined_at
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %s ORDER BY m.joined_at ASC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_org_for_user(sub: str, org: str) -> int:
    """Résout `org` (id numérique ou nom) parmi les orgs DU sub. Lève `ValueError`
    si inconnu/ambigu — jamais de choix implicite (mauvaise org = mauvais secret).
    Logique de store neutre (pas de McpError) : les adaptateurs traduisent."""
    org = (org or "").strip()
    mine = list_orgs_for_user(sub)
    if org.isdigit():
        oid = int(org)
        if any(o["org_id"] == oid for o in mine):
            return oid
        raise ValueError(f"Tu n'es membre d'aucune org #{oid}.")
    matches = [o for o in mine if o["name"].lower() == org.lower()]
    if len(matches) == 1:
        return matches[0]["org_id"]
    if not matches:
        raise ValueError(f"Aucune de tes orgs ne s'appelle `{org}`.")
    raise ValueError(f"Plusieurs de tes orgs s'appellent `{org}` — utilise l'id.")


def list_org_members(org_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, org_role, is_active, joined_at FROM org_members "
            "WHERE org_id = %s ORDER BY joined_at",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_org_role(org_id: int, sub: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s",
            (org_id, sub),
        ).fetchone()
        return row["org_role"] if row else None


def set_org_secret(org_id: int, provider: str, api_key: str, set_by: Optional[str] = None,
                   meta: Optional[dict] = None) -> None:
    """Pose/rote le secret partagé `provider` de l'org. `provider` validé comme
    org-partageable (byo_org : exclut slack/linkedin, inclut mm org-only) via le
    registre — plus restrictif que KEY_PROVIDERS puisque mm n'est pas keyed.
    `meta` : satellites non-secrets (ex. `base_url` du bridge d'un connecteur
    remote, ADR 0003).

    Un connecteur **remote** (ADR 0003/0011) est défini par la DONNÉE : un `meta`
    avec `base_url` (endpoint du bridge) ⇒ pas d'entrée registre attendue (zéro nom
    client en dur, cf. `connectors.org_secret_meta`). Sinon, garde d'éligibilité
    org-partageable via le registre."""
    if not (meta and meta.get("base_url")):
        connectors.require_credential("org", provider)
    if not api_key:
        raise ValueError("api_key requise")
    # Coffre chiffré, source unique (entité 'org').
    credentials_store.set_credential(
        "org", str(org_id), provider, api_key, set_by=set_by, meta=meta)


def delete_org_secret(org_id: int, provider: str) -> bool:
    return credentials_store.clear_credential("org", str(org_id), provider)


def list_org_secrets(org_id: int) -> list[dict]:
    """Providers posés sur l'org — SANS l'api_key (jamais exposée via API).
    Lit le coffre (entité 'org'). `base_url` exposé pour les connecteurs
    remote (satellite non-secret dans `meta`)."""
    out: list[dict] = []
    for c in credentials_store.list_credentials("org", str(org_id)):
        entry = {"provider": c["connector"], "set_by": c["set_by"], "set_at": c["set_at"]}
        base_url = (c.get("meta") or {}).get("base_url")
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)
    return out


# --- entitlements : plafond de visibilité plateforme -> org (barreau 4) ------

def list_org_entitled_namespaces(org_id: int) -> list[str]:
    """Namespaces gouvernés débloqués pour les membres de l'org."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace FROM org_entitlements WHERE org_id = %s ORDER BY namespace",
            (org_id,),
        ).fetchall()
        return [r["namespace"] for r in rows]


def grant_org_entitlement(org_id: int, namespace: str, granted_by: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_entitlements (org_id, namespace, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, namespace) DO UPDATE SET
                granted_at = NOW(), granted_by = EXCLUDED.granted_by
            """,
            (org_id, namespace, granted_by),
        )


def revoke_org_entitlement(org_id: int, namespace: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_entitlements WHERE org_id = %s AND namespace = %s",
            (org_id, namespace),
        )
        return (cur.rowcount or 0) > 0


def list_org_entitlements(org_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace, granted_by, granted_at FROM org_entitlements "
            "WHERE org_id = %s ORDER BY namespace",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- création self-serve + invitations (onboarding SaaS) --------------------

def count_orgs_created_by(sub: str) -> int:
    """Nombre d'orgs créées par ce sub (anti-abus du self-serve `org.create`)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM orgs WHERE created_by = %s", (sub,)
        ).fetchone()["n"]


def create_invitation(org_id: Optional[int], email: str, org_role: str, invited_by: str,
                      ttl_days: int = 7, source: Optional[str] = None) -> tuple[int, str]:
    """Crée une invitation (ADR 0013, table unifiée). `org_id` renseigné = rejoindre
    cette org ; `org_id=None` = referral alpha (l'invité crée sa propre org).
    `org_role` n'a de sens que pour la saveur org. Renvoie (id, token plaintext —
    exposé une seule fois, seul son hash est persisté)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("email invalide")
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r}")
    token = "inv_" + secrets.token_urlsafe(32)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO org_invitations (org_id, email, org_role, token_hash, invited_by, source, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW() + (%s || ' days')::interval)
            RETURNING id
            """,
            (org_id, email, org_role, _hash_token(token), invited_by, source, str(int(ttl_days))),
        ).fetchone()
        return int(row["id"]), token


def list_invitations(org_id: int) -> list[dict]:
    """Invitations en attente (non acceptées, non expirées)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, email, org_role, invited_by, created_at, expires_at
              FROM org_invitations
             WHERE org_id = %s AND accepted_at IS NULL AND expires_at > NOW()
             ORDER BY created_at DESC
            """,
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_invitation(org_id: int, inv_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE org_id = %s AND id = %s AND accepted_at IS NULL",
            (org_id, inv_id),
        )
        return (cur.rowcount or 0) > 0


def list_alpha_invitations() -> list[dict]:
    """Invitations alpha (referral, `org_id IS NULL`) en attente — non acceptées,
    non expirées. Vue admin des invitations émises mais pas encore consommées."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, email, invited_by, source, created_at, expires_at
              FROM org_invitations
             WHERE org_id IS NULL AND accepted_at IS NULL AND expires_at > NOW()
             ORDER BY created_at DESC
            """,
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_alpha_invitation(inv_id: int) -> bool:
    """Révoque une invitation alpha (referral) en attente par id."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE id = %s AND org_id IS NULL AND accepted_at IS NULL",
            (inv_id,),
        )
        return (cur.rowcount or 0) > 0


def revoke_alpha_invitations_for_email(email: str) -> int:
    """Révoque toutes les invitations alpha en attente pour un email (supersede au
    moment d'un renvoi, pour ne pas empiler des liens valides). Renvoie le nombre."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE org_id IS NULL AND accepted_at IS NULL AND lower(email) = %s",
            (email,),
        )
        return cur.rowcount or 0


def find_pending_alpha_invite_by_email(email: str) -> Optional[dict]:
    """Invitation alpha en attente la plus récente pour cet email, sinon None.
    Sert la fiche admin user (proposer un renvoi)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, expires_at
              FROM org_invitations
             WHERE org_id IS NULL AND accepted_at IS NULL AND expires_at > NOW()
               AND lower(email) = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (email,),
        ).fetchone()
        return dict(row) if row else None


def preview_invitation(token: str) -> Optional[dict]:
    """Aperçu PUBLIC d'une invitation valide (pour la page d'accueil d'invitation,
    avant authentification) : email visé, saveur referral/org, nom de l'inviteur et
    nom de l'org. Le token EST le secret — rien de sensible au-delà. None si
    invalide/expirée/déjà acceptée."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT i.email, i.org_id,
                   COALESCE(u.name, u.email) AS inviter,
                   o.name AS org_name
              FROM org_invitations i
              LEFT JOIN users u ON u.sub = i.invited_by
              LEFT JOIN orgs  o ON o.id  = i.org_id
             WHERE i.token_hash = %s AND i.accepted_at IS NULL AND i.expires_at > NOW()
            """,
            (_hash_token(token),),
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return {"email": r["email"], "referral": r["org_id"] is None,
                "inviter": r.get("inviter"), "org_name": r.get("org_name")}


def get_invitation_by_token(token: str) -> Optional[dict]:
    """Invitation valide (non acceptée, non expirée) pour ce token, sinon None."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, org_id, email, org_role, invited_by, source, expires_at
              FROM org_invitations
             WHERE token_hash = %s AND accepted_at IS NULL AND expires_at > NOW()
            """,
            (_hash_token(token),),
        ).fetchone()
        return dict(row) if row else None


def _mark_invitation_accepted(inv_id: int, sub: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE org_invitations SET accepted_at = NOW(), accepted_sub = %s "
            "WHERE id = %s AND accepted_at IS NULL",
            (sub, inv_id),
        )


def accept_invitation(token: str, sub: str) -> Optional[dict]:
    """Accepte une invitation (ADR 0013, table unifiée). Les DEUX saveurs accordent
    l'accès plateforme. Renvoie {org_id, org_role, referral} ou None si token
    invalide. Le contrôle d'email (matche le compte) est fait par la capacité
    appelante.

    - referral (`org_id=None`) : accorde l'accès + crédite le quota referral + pose
      le parrain ; PAS de rattachement d'org (l'invité crée la sienne via org.create).
    - org (`org_id` renseigné) : ajoute le membre, bascule l'org active, et accorde
      aussi l'accès plateforme (un membre invité est de fait dans Oto)."""
    inv = get_invitation_by_token(token)
    if not inv:
        return None
    return _accept_invitation_row(inv, sub)


def _accept_invitation_row(inv: dict, sub: str) -> dict:
    """Cœur de l'acceptation à partir d'une ligne d'invitation déjà résolue (par
    token OU par email lors d'une réconciliation de signup). Cf. accept_invitation."""
    if inv["org_id"] is None:
        db.grant_platform_access(sub, invited_by=inv.get("invited_by"),
                                 quota=_alpha_invite_quota())
        _mark_invitation_accepted(inv["id"], sub)
        return {"org_id": None, "org_role": None, "referral": True}
    add_org_member(inv["org_id"], sub, inv["org_role"])
    set_active_org(sub, inv["org_id"])
    db.grant_platform_access(sub)
    _mark_invitation_accepted(inv["id"], sub)
    return {"org_id": inv["org_id"], "org_role": inv["org_role"], "referral": False}


def reconcile_signup_with_invitation(sub: str, email: str) -> Optional[dict]:
    """Honore une invitation par l'EMAIL au signup (ADR 0013) : si un nouvel inscrit
    a une invitation en attente pour son email vérifié, on l'accepte automatiquement
    — il saute la waitlist au lieu d'y rester coincé avec une invitation orpheline
    (cas vécu : invité qui s'inscrit sans passer par le lien /invite). Sûr car
    l'email est vérifié par Logto (signup email+code). None si aucune invitation."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, org_id, org_role, invited_by
              FROM org_invitations
             WHERE accepted_at IS NULL AND expires_at > NOW() AND lower(email) = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (email,),
        ).fetchone()
    if not row:
        return None
    return _accept_invitation_row(dict(row), sub)


# --- instructions d'org : doctrine de base + skills versionnés ----------------
#
# Modèle unifié servi par oto_get_doctrine() / oto_*_instruction(s). Le slug réservé
# BASE_SLUG ("claude_md") = la doctrine de base (servie d'office) ; les autres =
# des skills chargés à la demande. En clair (prose, hors coffre), lu à l'appel
# (pas de cache). Écriture = incrément de version + snapshot d'historique.

BASE_SLUG = "claude_md"
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_slug(slug: str) -> str:
    """Slug canonique : minuscules, [a-z0-9_-], séparateurs compactés. '' si vide."""
    return _SLUG_RE.sub("-", (slug or "").strip().lower()).strip("-_")


def _snippet(body: str, query: str, width: int = 200) -> str:
    """Extrait de `body` autour de la 1ʳᵉ occurrence de `query` (pour la recherche)."""
    i = body.lower().find(query.lower())
    if i < 0:
        return body[:width].strip()
    start = max(0, i - width // 3)
    end = min(len(body), i + len(query) + (2 * width) // 3)
    return ("…" if start else "") + body[start:end].strip() + ("…" if end < len(body) else "")


def get_instruction(org_id: int, slug: str, version: Optional[int] = None) -> Optional[dict]:
    """Une instruction (courante, ou une `version` archivée précise). None si absente."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT org_id, slug, title, description, body_md, version, set_by, "
                "created_at, updated_at FROM org_instructions "
                "WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT org_id, slug, title, description, body_md, version, set_by, "
                "created_at FROM org_instruction_revisions "
                "WHERE org_id = %s AND slug = %s AND version = %s",
                (org_id, slug, version),
            ).fetchone()
        return dict(row) if row else None


def list_instructions(org_id: int, include_base: bool = False) -> list[dict]:
    """Métadonnées des instructions (SANS body) = l'index des skills. Exclut la
    doctrine de base sauf `include_base` (surface admin)."""
    where = "org_id = %s" if include_base else "org_id = %s AND slug <> %s"
    params: tuple = (org_id,) if include_base else (org_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT slug, title, description, version, updated_at "
            f"FROM org_instructions WHERE {where} ORDER BY slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def search_instructions(org_id: int, query: str, include_base: bool = False) -> list[dict]:
    """Recherche substring (title/description/body) dans les instructions de l'org.
    Renvoie les métadonnées + un `snippet` ; le body complet passe par get_instruction."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    base_filter = "" if include_base else "AND slug <> %s "
    head: tuple = (org_id,) if include_base else (org_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, title, description, body_md, version, updated_at "
            "FROM org_instructions WHERE org_id = %s " + base_filter +
            "AND (title ILIKE %s OR description ILIKE %s OR body_md ILIKE %s) "
            "ORDER BY (title ILIKE %s) DESC, (description ILIKE %s) DESC, updated_at DESC",
            head + (like, like, like, like, like),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = _snippet(d.pop("body_md", "") or "", q)
        out.append(d)
    return out


def set_instruction(org_id: int, slug: str, body_md: str, title: Optional[str] = None,
                    description: Optional[str] = None, set_by: Optional[str] = None) -> int:
    """Crée/met à jour une instruction ; renvoie la NOUVELLE version et archive un
    snapshot. `title`/`description` None = conserver l'existant ('' à la création).
    Sérialisé par (org, slug) via verrou advisory (mirroir add_org_member)."""
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"oi:{org_id}:{slug}",))
            cur = conn.execute(
                "SELECT version, title, description FROM org_instructions "
                "WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
            new_version = (cur["version"] + 1) if cur else 1
            new_title = title if title is not None else (cur["title"] if cur else "")
            new_desc = description if description is not None else (cur["description"] if cur else "")
            conn.execute(
                """
                INSERT INTO org_instructions
                    (org_id, slug, title, description, body_md, version, set_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (org_id, slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, version = EXCLUDED.version,
                    set_by = EXCLUDED.set_by, updated_at = NOW()
                """,
                (org_id, slug, new_title, new_desc, body_md, new_version, set_by),
            )
            conn.execute(
                """
                INSERT INTO org_instruction_revisions
                    (org_id, slug, version, title, description, body_md, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (org_id, slug, new_version, new_title, new_desc, body_md, set_by),
            )
            return new_version


def list_instruction_versions(org_id: int, slug: str) -> list[dict]:
    """Historique d'une instruction (métadonnées par version, plus récent d'abord)."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, title, set_by, created_at FROM org_instruction_revisions "
            "WHERE org_id = %s AND slug = %s ORDER BY version DESC",
            (org_id, slug),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_instruction(org_id: int, slug: str) -> bool:
    """Supprime une instruction ET son historique. False si elle n'existait pas."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_instructions WHERE org_id = %s AND slug = %s", (org_id, slug)
            )
            removed = (cur.rowcount or 0) > 0
            conn.execute(
                "DELETE FROM org_instruction_revisions WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            )
    return removed
