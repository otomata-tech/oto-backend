"""Accès DB du palier organization (= périmètre / store serveur).

Domaine isolé du monolithe `db.py` : les tables org (orgs, org_members)
restent déclarées dans `db._SCHEMA` (DDL centralisée, jouée
par `init_db`), mais leurs requêtes vivent ici. Les credentials d'org vivent
dans le coffre chiffré `connector_credentials` (entity_type='org'), pas dans
une table dédiée. Réutilise les primitives partagées de `db` (`_connect`,
`upsert_user`) plutôt que de les dupliquer.

Consommé par : `access.resolve_api_key`/`status_for` (reads org credential) et
`tools/orgs.py` (meta-tools de gestion). Cf. project_oto_mcp_org_tier.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from typing import Optional

_log = logging.getLogger(__name__)

from . import credentials_store
from . import connectors
from . import db
from .db import _connect, _hash_token, upsert_user


def _alpha_invite_quota() -> int:
    """Budget d'invitations crédité à un invité alpha qui accepte (ADR 0013).
    Même plafond pour les invitations nominatives directes ET le lien referral
    (décision 2026-06-22). Défaut 5, modifiable par l'admin (set_invite_quota)."""
    return int(os.environ.get("OTO_ALPHA_INVITE_QUOTA", "5"))


# Codes courts lisibles (referral_code porteur + code d'invitation). Alphabet
# Crockford sans caractères ambigus (pas de I/L/O/U, 0/1 retirés) → dictable à
# l'oral, sans collision visuelle. 7 chars = ~34 bits ; single-use + TTL +
# rate-limit côté capacité couvrent le brute-force pour un gate alpha.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def _gen_code(n: int = 7) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def get_or_create_referral_code(sub: str) -> Optional[str]:
    """Code referral stable du user (lazy). Le génère à la 1re demande, en
    réessayant sur collision d'unicité. None si le user n'existe pas."""
    with _connect() as conn:
        row = conn.execute("SELECT referral_code FROM users WHERE sub = %s", (sub,)).fetchone()
        if not row:
            return None
        if row["referral_code"]:
            return row["referral_code"]
        for _ in range(8):
            code = _gen_code()
            cur = conn.execute(
                "UPDATE users SET referral_code = %s, updated_at = NOW() "
                "WHERE sub = %s AND referral_code IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM users WHERE referral_code = %s)",
                (code, sub, code),
            )
            if (cur.rowcount or 0) > 0:
                return code
            # Soit une course a posé un code (relire), soit collision (retry).
            again = conn.execute(
                "SELECT referral_code FROM users WHERE sub = %s", (sub,)).fetchone()
            if again and again["referral_code"]:
                return again["referral_code"]
        raise RuntimeError("impossible de générer un code referral unique")


def get_user_by_referral_code(code: str) -> Optional[dict]:
    """Le user porteur d'un referral_code (carrier du lien /invitation/<code>)."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub, email, name, referral_code FROM users WHERE referral_code = %s",
            (code,),
        ).fetchone()
        return dict(row) if row else None


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
            "SELECT id, name, description, created_by, created_at, logo_url FROM orgs WHERE id = %s",
            (org_id,),
        ).fetchone()
        return dict(row) if row else None


def update_org(org_id: int, name: Optional[str] = None,
               description: Optional[str] = None) -> bool:
    """Renomme / re-décrit une org. None = conserver le champ. False si absente.

    Miroir de `group_store.update_group` au grain org. Métadonnées en clair
    (nom/prose), hors coffre."""
    sets, params = [], []
    if name is not None:
        n = name.strip()
        if not n:
            raise ValueError("nom d'org vide")
        sets.append("name = %s")
        params.append(n)
    if description is not None:
        sets.append("description = %s")
        params.append(description.strip())
    if not sets:
        return get_org(org_id) is not None
    params.append(org_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE orgs SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )
        return (cur.rowcount or 0) > 0


def list_all_orgs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_by, created_at, logo_url FROM orgs "
            "WHERE archived_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def archive_org(org_id: int) -> bool:
    """Archive (soft-delete) une org : masquée de tous les listings, réversible
    (DB : `UPDATE orgs SET archived_at = NULL`). Aucune FK touchée (membres,
    credentials, usage, billing restent). Les membres qui l'avaient pour org
    active basculent sur leur plus ancienne org NON archivée restante (miroir
    `remove_org_member`) ; sans org restante → plus d'org active (= perso).
    False si l'org est inconnue ou déjà archivée."""
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE orgs SET archived_at = now() WHERE id = %s AND archived_at IS NULL",
                (org_id,),
            )
            if (cur.rowcount or 0) == 0:
                return False
            stranded = conn.execute(
                "SELECT sub FROM org_members WHERE org_id = %s AND is_active", (org_id,)
            ).fetchall()
            for r in stranded:
                sub = r["sub"]
                conn.execute(
                    "UPDATE org_members SET is_active = FALSE WHERE sub = %s AND org_id = %s",
                    (sub, org_id),
                )
                conn.execute(
                    """
                    UPDATE org_members SET is_active = TRUE
                     WHERE sub = %s AND org_id = (
                         SELECT m.org_id FROM org_members m JOIN orgs o ON o.id = m.org_id
                          WHERE m.sub = %s AND m.org_id <> %s AND o.archived_at IS NULL
                          ORDER BY m.joined_at ASC LIMIT 1
                     )
                    """,
                    (sub, sub, org_id),
                )
            return True


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


def get_org_default_connectors(org_id: int) -> Optional[list[str]]:
    """Baseline de connecteurs *proposés* par l'org (« org propose », ADR 0019) :
    liste de noms de connecteurs recommandés à ses membres, ou None si l'org n'en
    impose pas. Consultatif — le membre reste libre de (dé)sélectionner. Miroir de
    `default_tools` au grain connecteur."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT default_connectors FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    if not row:
        return None
    dc = row["default_connectors"]
    return list(dc) if dc is not None else None


def set_org_default_connectors(org_id: int, connectors: Optional[list[str]]) -> bool:
    """Pose (ou efface si None) la baseline de connecteurs proposés de l'org. False si absente."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET default_connectors = %s WHERE id = %s",
            (list(connectors) if connectors is not None else None, org_id),
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


# --- adresses expéditrices d'email de l'org, PAR CONNECTEUR ------------------
# Modèle calqué sur field_filters : JSONB sur orgs, keyé par connecteur. Un
# expéditeur appartient à un connecteur (scaleway/resend) → le transport en dérive
# (providers.EMAIL_CONNECTOR_TRANSPORT). Forme :
#   { "scaleway": {"senders":[{email,name?,reply_to?}], "quiet_hours?":{...}},
#     "resend":   {"senders":[...], "quiet_hours?":{...}} }

# Ordre de résolution d'un expéditeur par défaut (from_email omis).
_EMAIL_CONNECTORS_ORDER = ("scaleway", "resend")


def get_org_email_settings(org_id: int) -> dict:
    """Réglages d'envoi d'email de l'org, keyés PAR CONNECTEUR. `{}` si rien posé.

    Forme : `{ "<connector>": {"senders": [...], "quiet_hours"?: {...}} }`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT email_settings FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        if not row:
            return {}
        return dict(row["email_settings"] or {})


def set_org_email_settings(org_id: int, connector: str, *,
                           senders: Optional[list[dict]] = None,
                           quiet_hours: Optional[dict] = None,
                           clear_quiet_hours: bool = False) -> bool:
    """Met à jour le bloc d'un CONNECTEUR (merge dans le JSONB ; ne touche pas les
    autres connecteurs). False si org absente.

    `senders`/`quiet_hours=None` = ne touche pas ce champ ; `clear_quiet_hours=True`
    = efface la fenêtre du connecteur (retour au défaut plateforme à l'envoi),
    exclusif avec `quiet_hours`. Prose de config (pas un secret) → colonne en clair ;
    la clé Resend, elle, vit dans le coffre (`set_org_secret(org_id, "resend", ...)`)."""
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT email_settings FROM orgs WHERE id = %s FOR UPDATE", (org_id,)
            ).fetchone()
            if not row:
                return False
            current = dict(row["email_settings"] or {})
            block = dict(current.get(connector) or {})
            if senders is not None:
                block["senders"] = senders
            if clear_quiet_hours:
                block.pop("quiet_hours", None)
            elif quiet_hours is not None:
                block["quiet_hours"] = quiet_hours
            current[connector] = block
            conn.execute(
                "UPDATE orgs SET email_settings = %s::jsonb WHERE id = %s",
                (json.dumps(current), org_id),
            )
            return True


def list_scheduled_emails(org_id: int, status: str = "pending") -> list[dict]:
    """Emails programmés de l'org (délégation au journal db)."""
    return db.list_scheduled_emails(org_id, status=status)


def cancel_scheduled_email(org_id: int, email_id: int) -> bool:
    """Annule un email encore en attente de l'org (délégation au journal db)."""
    return db.cancel_scheduled_email(org_id, email_id)


def _email_connectors_in_order(settings: dict) -> list[str]:
    """Connecteurs email présents, dans un ordre déterministe (scaleway, resend,
    puis tout autre keyé inattendu trié)."""
    present = list(settings.keys())
    ordered = [c for c in _EMAIL_CONNECTORS_ORDER if c in present]
    return ordered + sorted(c for c in present if c not in _EMAIL_CONNECTORS_ORDER)


def resolve_sender(org_id: int, from_email: Optional[str] = None
                   ) -> Optional[tuple[dict, str]]:
    """`(sender, connector)` à utiliser, ou None si l'org n'a aucun expéditeur.

    `from_email` fourni = doit matcher un expéditeur déclaré (dans n'importe quel
    connecteur) ; absent = le 1er expéditeur du 1er connecteur (ordre déterministe).
    Le connecteur retourné détermine le transport (EMAIL_CONNECTOR_TRANSPORT)."""
    settings = get_org_email_settings(org_id)
    want = from_email.strip().lower() if from_email else None
    for connector in _email_connectors_in_order(settings):
        senders = (settings.get(connector) or {}).get("senders") or []
        for s in senders:
            if want is None:
                return s, connector
            if (s.get("email") or "").strip().lower() == want:
                return s, connector
    return None


def org_email_quiet_hours(org_id: int, connector: str) -> Optional[dict]:
    """Fenêtre calme d'un connecteur email de l'org (None = pas posée)."""
    return (get_org_email_settings(org_id).get(connector) or {}).get("quiet_hours")


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


def list_orgs_for_user(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.org_id, o.name, o.logo_url, m.org_role, m.is_active, m.joined_at
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %s AND o.archived_at IS NULL ORDER BY m.joined_at ASC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_personal_org(sub: str) -> Optional[int]:
    """Org PERSO (privée, mono-membre) de `sub`, marquée `personal_of=sub`, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM orgs WHERE personal_of = %s AND archived_at IS NULL", (sub,)
        ).fetchone()
        return int(row["id"]) if row else None


def _personal_label(email: Optional[str], name: Optional[str]) -> str:
    return (name or (email.split("@")[0] if email else None) or "Mon espace").strip() or "Mon espace"


def _reclaim_or_create_personal(sub: str, email: Optional[str], name: Optional[str]) -> int:
    """Récupère ou crée l'org perso de `sub`. **Réclamation SÛRE** : on ne marque une
    org existante comme perso QUE si c'est la SEULE org du user (mono-membre, créée par
    lui) — un user multi-org garde ses orgs partagées intactes, on lui crée une perso
    fraîche."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT o.id FROM orgs o
             WHERE o.created_by = %s AND o.personal_of IS NULL AND o.archived_at IS NULL
               AND (SELECT count(*) FROM org_members m WHERE m.org_id = o.id) = 1
               AND EXISTS (SELECT 1 FROM org_members m WHERE m.org_id = o.id AND m.sub = %s)
               AND (SELECT count(*) FROM org_members m2 JOIN orgs o2 ON o2.id = m2.org_id
                     WHERE m2.sub = %s AND o2.archived_at IS NULL) = 1
             LIMIT 1
            """,
            (sub, sub, sub),
        ).fetchone()
        if row:
            oid = int(row["id"])
            conn.execute("UPDATE orgs SET personal_of = %s WHERE id = %s", (sub, oid))
            _log.info("ensure_personal_org: org #%s réclamée comme perso de %s", oid, sub)
            return oid
    oid = create_org(_personal_label(email, name), created_by=sub)
    add_org_member(oid, sub, org_role="org_admin")
    with _connect() as conn:
        conn.execute("UPDATE orgs SET personal_of = %s WHERE id = %s", (sub, oid))
    _log.info("ensure_personal_org: org perso #%s créée pour %s", oid, sub)
    return oid


def ensure_personal_org(sub: str, email: Optional[str] = None, name: Optional[str] = None) -> int:
    """Garantit l'**org perso** de `sub` (suppression du perso `org_id=0`) ET qu'il a une
    org active (la perso si aucune autre). Idempotent."""
    pid = get_personal_org(sub)
    if pid is None:
        pid = _reclaim_or_create_personal(sub, email, name)
    if get_active_org(sub) is None:   # nouveau user / ex-perso → la perso devient maison
        set_active_org(sub, pid)
    return pid


def backfill_personal_orgs() -> dict:
    """One-shot idempotent (boot) : chaque user a une org perso marquée ; ses ressources
    `owner_type='user'` (datastores/projets) y MIGRENT (owner_type='user' disparaît des
    données) ; si aucune org active → la perso devient maison. No-op aux boots suivants."""
    from . import db as _db
    counts = {"users": 0, "datastores": 0, "projects": 0}
    with _connect() as conn:
        users = conn.execute("SELECT sub, email, name FROM users").fetchall()
    for u in users:
        sub = u["sub"]
        try:
            pid = ensure_personal_org(sub, u.get("email"), u.get("name"))
        except Exception:
            _log.warning("backfill_personal_orgs: ensure échoué %s", sub, exc_info=True)
            continue
        for d in _db.list_datastore_namespaces_for_owners([("user", sub)]):
            try:
                _db.reparent_datastore_namespace(int(d["id"]), "org", str(pid))
                counts["datastores"] += 1
            except Exception:
                _log.warning("reparent datastore %s échoué", d.get("id"), exc_info=True)
        for p in _db.list_projects_for_owners([("user", sub)], include_archived=True):
            try:
                _db.reparent_project(int(p["id"]), "org", str(pid))
                counts["projects"] += 1
            except Exception:
                _log.warning("reparent project %s échoué", p.get("id"), exc_info=True)
        counts["users"] += 1
    if counts["datastores"] or counts["projects"]:
        _log.info("backfill_personal_orgs: %s", counts)
    return counts


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


# --- création self-serve + invitations (onboarding SaaS) --------------------

def count_orgs_created_by(sub: str) -> int:
    """Nombre d'orgs créées par ce sub (anti-abus du self-serve `org.create`)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM orgs WHERE created_by = %s", (sub,)
        ).fetchone()["n"]


def create_invitation(org_id: Optional[int], email: Optional[str], org_role: str, invited_by: str,
                      ttl_days: int = 7, source: Optional[str] = None) -> tuple[int, str, str]:
    """Crée une invitation nominative (ADR 0013, table unifiée). `org_id` renseigné
    = rejoindre cette org ; `org_id=None` = referral alpha (l'invité crée sa propre
    org). `org_role` n'a de sens que pour la saveur org. `email` est OPTIONNEL : sans
    email, l'émetteur partage le code lui-même (pas d'envoi mail). Renvoie
    (id, token plaintext, code court) — token pour le lien mail legacy (seul son hash
    est persisté), code pour le lien /invitation/<carrier>/<code> partageable."""
    email = (email or "").strip().lower() or None
    if email is not None and "@" not in email:
        raise ValueError("email invalide")
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r}")
    token = "inv_" + secrets.token_urlsafe(32)
    with _connect() as conn:
        for _ in range(8):
            code = _gen_code()
            if conn.execute(
                "SELECT 1 FROM org_invitations WHERE code = %s", (code,)).fetchone():
                continue
            row = conn.execute(
                """
                INSERT INTO org_invitations
                    (org_id, email, org_role, token_hash, code, invited_by, source, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + (%s || ' days')::interval)
                RETURNING id
                """,
                (org_id, email, org_role, _hash_token(token), code, invited_by, source,
                 str(int(ttl_days))),
            ).fetchone()
            return int(row["id"]), token, code
        raise RuntimeError("impossible de générer un code d'invitation unique")


def list_invitations(org_id: int) -> list[dict]:
    """Invitations en attente (non acceptées, non expirées)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, email, code, org_role, invited_by, created_at, expires_at
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
            SELECT id, email, code, invited_by, source, created_at, expires_at
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


class InviteQuotaExhausted(Exception):
    """Le parrain n'a plus d'invitations disponibles au moment de l'acceptation
    (débit à l'accept, décision 2026-06-22)."""


def _preview_from_row(r: dict) -> dict:
    return {"email": r.get("email"), "referral": r["org_id"] is None,
            "inviter": r.get("inviter"), "org_name": r.get("org_name")}


_PREVIEW_SELECT = """
    SELECT i.email, i.org_id,
           COALESCE(u.name, u.email) AS inviter,
           o.name AS org_name
      FROM org_invitations i
      LEFT JOIN users u ON u.sub = i.invited_by
      LEFT JOIN orgs  o ON o.id  = i.org_id
     WHERE {pred} AND i.accepted_at IS NULL AND i.expires_at > NOW()
"""


def preview_invitation(token: str) -> Optional[dict]:
    """Aperçu PUBLIC d'une invitation nominative valide (page d'accueil d'invitation,
    avant authentification), par token mail. None si invalide/expirée/déjà acceptée."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            _PREVIEW_SELECT.format(pred="i.token_hash = %s"), (_hash_token(token),)
        ).fetchone()
        return _preview_from_row(dict(row)) if row else None


def preview_invitation_by_code(code: str) -> Optional[dict]:
    """Aperçu PUBLIC par code court (lien /invitation/<carrier>/<code>)."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with _connect() as conn:
        row = conn.execute(
            _PREVIEW_SELECT.format(pred="i.code = %s"), (code,)
        ).fetchone()
        return _preview_from_row(dict(row)) if row else None


def preview_referral(carrier: str) -> Optional[dict]:
    """Aperçu PUBLIC d'un lien referral réutilisable (/invitation/<carrier>) : nom
    du porteur, saveur referral. None si le code porteur est inconnu. `exhausted` =
    le porteur n'a plus de budget (l'accept échouera)."""
    u = get_user_by_referral_code(carrier)
    if not u:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT invite_quota FROM users WHERE sub = %s",
                           (u["sub"],)).fetchone()
    return {"email": None, "referral": True, "org_name": None,
            "inviter": u.get("name") or u.get("email"),
            "exhausted": int((row or {}).get("invite_quota") or 0) <= 0}


def get_invitation_by_token(token: str) -> Optional[dict]:
    """Invitation valide (non acceptée, non expirée) pour ce token, sinon None."""
    if not token:
        return None
    return _get_invitation("token_hash = %s", _hash_token(token))


def get_invitation_by_code(code: str) -> Optional[dict]:
    """Invitation valide (non acceptée, non expirée) pour ce code court, sinon None."""
    code = (code or "").strip().upper()
    if not code:
        return None
    return _get_invitation("code = %s", code)


def _get_invitation(pred: str, val) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT id, org_id, email, org_role, invited_by, source, expires_at
              FROM org_invitations
             WHERE {pred} AND accepted_at IS NULL AND expires_at > NOW()
            """,
            (val,),
        ).fetchone()
        return dict(row) if row else None


def _mark_invitation_accepted(inv_id: int, sub: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE org_invitations SET accepted_at = NOW(), accepted_sub = %s "
            "WHERE id = %s AND accepted_at IS NULL",
            (sub, inv_id),
        )


def _is_active(sub: str) -> bool:
    u = db.get_user(sub)
    return bool(u and u.get("access_status") == "active")


def _grant_referral_access(invitee_sub: str, inviter_sub: Optional[str], source: Optional[str]) -> bool:
    """Accorde l'accès plateforme à un invité via une saveur referral (lien ou code
    nominatif). Débite le budget du PARRAIN à l'acceptation (décision 2026-06-22),
    sauf émission admin (hors quota). Idempotent : si l'invité est déjà actif, no-op
    (pas de re-débit sur double-clic). Lève InviteQuotaExhausted si le parrain est à
    sec. Renvoie True si un accès vient d'être accordé, False si déjà actif."""
    if _is_active(invitee_sub):
        return False
    if inviter_sub and source != "admin":
        if not db.consume_invite_quota(inviter_sub):
            raise InviteQuotaExhausted()
    db.grant_platform_access(invitee_sub, invited_by=inviter_sub, quota=_alpha_invite_quota())
    return True


def accept_invitation(token: str, sub: str) -> Optional[dict]:
    """Accepte une invitation nominative par token mail. None si token invalide."""
    inv = get_invitation_by_token(token)
    return _accept_invitation_row(inv, sub) if inv else None


def accept_invitation_by_code(code: str, sub: str) -> Optional[dict]:
    """Accepte une invitation nominative par code court. None si code invalide."""
    inv = get_invitation_by_code(code)
    return _accept_invitation_row(inv, sub) if inv else None


def accept_referral(carrier: str, sub: str) -> Optional[dict]:
    """Accepte un lien referral réutilisable (/invitation/<carrier>). Le porteur ne
    peut pas s'auto-parrainer. Débite le porteur, journalise l'entrée (arbre viral),
    accorde l'accès. None si le code porteur est inconnu. Lève InviteQuotaExhausted
    si le porteur est à sec."""
    u = get_user_by_referral_code(carrier)
    if not u:
        return None
    if u["sub"] == sub:
        return {"org_id": None, "org_role": None, "referral": True, "self": True}
    granted = _grant_referral_access(sub, u["sub"], "referral_link")
    if granted:
        # Journalise l'entrée pour l'arbre viral (pas de pré-création ; token bidon
        # pour honorer la contrainte NOT NULL UNIQUE de token_hash).
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO org_invitations
                    (org_id, email, token_hash, invited_by, source, expires_at,
                     accepted_at, accepted_sub)
                VALUES (NULL, NULL, %s, %s, 'referral_link', NOW(), NOW(), %s)
                """,
                ("ref_" + secrets.token_urlsafe(24), u["sub"], sub),
            )
    return {"org_id": None, "org_role": None, "referral": True}


def _accept_invitation_row(inv: dict, sub: str) -> dict:
    """Cœur de l'acceptation à partir d'une ligne d'invitation déjà résolue (par
    token, code OU email lors d'une réconciliation de signup).

    - referral (`org_id=None`) : débite le parrain + accorde l'accès + crédite le
      budget de l'invité ; pas de rattachement d'org (l'invité crée la sienne).
    - org (`org_id` renseigné) : ajoute le membre, bascule l'org active, accorde
      l'accès plateforme (hors budget referral — ajout d'équipe)."""
    if inv["org_id"] is None:
        _grant_referral_access(sub, inv.get("invited_by"), inv.get("source"))
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
            SELECT id, org_id, org_role, invited_by, source
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


def list_instruction_bodies(org_id: int) -> list[dict]:
    """Slug + body_md des instructions d'une org (hors doctrine de base) — pour
    dériver les références d'outils `<tool:slug>` (compteur « doctrine-only », ADR 0024)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, body_md FROM org_instructions WHERE org_id = %s AND slug <> %s",
            (org_id, BASE_SLUG),
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


# --- bibliothèque publique de doctrines (marketplace, table doctrine_library) ---
#
# Un catalogue cherchable de doctrines PUBLIÉES, chaque entrée portant un AUTEUR
# ('otomata' = la plateforme, ou 'org' = un créateur privé). Preview + fork dans
# son org (copie versionnée via set_instruction). En clair (prose publiable).
# Deny-by-default sur la surface anonyme : visibility='public' uniquement, jamais
# 'unlisted' (servi seulement par slug exact à un appelant authentifié).

_LIBRARY_COLS = (
    "id, slug, title, description, body_md, author_kind, author_org_id, "
    "author_display, category, tags, visibility, source_org_id, source_slug, "
    "forked_from, version, published_by, created_at, updated_at"
)
_LIBRARY_META_COLS = (
    "id, slug, title, description, author_kind, author_org_id, author_display, "
    "category, tags, visibility, version, created_at, updated_at"
)


def publish_doctrine(*, slug: str, title: str = "", description: str = "",
                     body_md: str, author_kind: str, author_org_id: Optional[int] = None,
                     author_display: str = "", category: str = "",
                     tags: Optional[list] = None, visibility: str = "public",
                     source_org_id: Optional[int] = None, source_slug: Optional[str] = None,
                     forked_from: Optional[int] = None,
                     published_by: Optional[str] = None) -> dict:
    """Publie (ou re-publie) une doctrine dans la bibliothèque. Upsert par `slug` :
    re-publier le même slug incrémente `version` et remplace le corps. Sérialisé
    par slug via verrou advisory. Renvoie la row publiée (avec body)."""
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    if author_kind not in ("otomata", "org"):
        raise ValueError("author_kind invalide (otomata|org)")
    if visibility not in ("public", "unlisted"):
        raise ValueError("visibility invalide (public|unlisted)")
    tags = list(tags or [])
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"dl:{slug}",))
            cur = conn.execute(
                "SELECT version FROM doctrine_library WHERE slug = %s", (slug,)
            ).fetchone()
            new_version = (cur["version"] + 1) if cur else 1
            row = conn.execute(
                f"""
                INSERT INTO doctrine_library
                    (slug, title, description, body_md, author_kind, author_org_id,
                     author_display, category, tags, visibility, source_org_id,
                     source_slug, forked_from, version, published_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, author_kind = EXCLUDED.author_kind,
                    author_org_id = EXCLUDED.author_org_id,
                    author_display = EXCLUDED.author_display,
                    category = EXCLUDED.category, tags = EXCLUDED.tags,
                    visibility = EXCLUDED.visibility,
                    source_org_id = EXCLUDED.source_org_id,
                    source_slug = EXCLUDED.source_slug,
                    forked_from = EXCLUDED.forked_from, version = EXCLUDED.version,
                    published_by = EXCLUDED.published_by, updated_at = NOW()
                RETURNING {_LIBRARY_COLS}
                """,
                (slug, title, description, body_md, author_kind, author_org_id,
                 author_display, category, tags, visibility, source_org_id,
                 source_slug, forked_from, new_version, published_by),
            ).fetchone()
            return dict(row)


def list_library(*, query: Optional[str] = None, category: Optional[str] = None,
                 author_kind: Optional[str] = None, author_org_id: Optional[int] = None,
                 include_unlisted: bool = False, limit: int = 100) -> list[dict]:
    """Liste/recherche la bibliothèque (métadonnées + `snippet`, SANS body complet).
    Par défaut visibility='public' uniquement (surface anonyme/vitrine) ;
    `include_unlisted` élargit (surface authentifiée). `query` = substring sur
    title/description/body."""
    where = [] if include_unlisted else ["visibility = 'public'"]
    params: list = []
    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(title ILIKE %s OR description ILIKE %s OR body_md ILIKE %s)")
        params += [like, like, like]
    if category:
        where.append("category = %s")
        params.append(category)
    if author_kind:
        where.append("author_kind = %s")
        params.append(author_kind)
    if author_org_id is not None:
        where.append("author_org_id = %s")
        params.append(author_org_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sel = _LIBRARY_META_COLS + (", body_md" if q else "")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {sel} FROM doctrine_library{clause} "
            f"ORDER BY updated_at DESC LIMIT %s",
            tuple(params) + (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if q:
            d["snippet"] = _snippet(d.pop("body_md", "") or "", q)
        out.append(d)
    return out


def get_library_entry(*, entry_id: Optional[int] = None, slug: Optional[str] = None,
                      include_unlisted: bool = False) -> Optional[dict]:
    """Une entrée complète (avec body_md) par id OU slug. Respecte la visibilité :
    sans `include_unlisted`, ne renvoie que les entrées publiques."""
    if entry_id is None and not slug:
        raise ValueError("entry_id ou slug requis")
    vis = "" if include_unlisted else " AND visibility = 'public'"
    with _connect() as conn:
        if entry_id is not None:
            row = conn.execute(
                f"SELECT {_LIBRARY_COLS} FROM doctrine_library WHERE id = %s{vis}",
                (entry_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {_LIBRARY_COLS} FROM doctrine_library WHERE slug = %s{vis}",
                (normalize_slug(slug),),
            ).fetchone()
    return dict(row) if row else None


def fork_into_org(*, entry_id: int, org_id: int, new_slug: Optional[str] = None,
                  set_by: Optional[str] = None) -> dict:
    """Copie une entrée de la bibliothèque dans org_instructions de `org_id` sous
    un nouveau slug (défaut = slug source, suffixé -2/-3… si collision). Réutilise
    set_instruction → la doctrine forkée devient un skill d'org versionné (v1)."""
    entry = get_library_entry(entry_id=entry_id, include_unlisted=True)
    if not entry:
        raise ValueError("entrée de bibliothèque inconnue")
    base_slug = normalize_slug(new_slug or entry["slug"])
    slug = base_slug
    n = 2
    while get_instruction(org_id, slug) is not None:
        slug = f"{base_slug}-{n}"
        n += 1
    version = set_instruction(
        org_id, slug, entry["body_md"], title=entry.get("title") or "",
        description=entry.get("description") or "", set_by=set_by,
    )
    return {
        "org_id": org_id, "slug": slug, "version": version,
        "forked_from": entry["id"], "source_title": entry.get("title") or "",
    }


def unpublish_doctrine(entry_id: int) -> bool:
    """Retire une entrée publiée. False si elle n'existait pas."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM doctrine_library WHERE id = %s", (entry_id,))
        return (cur.rowcount or 0) > 0
