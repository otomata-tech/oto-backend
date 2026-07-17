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
import re
import secrets
from typing import Optional

_log = logging.getLogger(__name__)

from . import credentials_store
from . import connectors
from . import db
from . import logodev
from .db import _connect, _hash_token, upsert_user


# Codes courts lisibles (code d'invitation d'org). Alphabet Crockford sans
# caractères ambigus (pas de I/L/O/U, 0/1 retirés) → dictable à l'oral, sans
# collision visuelle. 7 chars = ~34 bits ; single-use + TTL + rate-limit côté
# capacité couvrent le brute-force.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def _gen_code(n: int = 7) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


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

    La restriction aux providers org-partageables (`byo_org`) est portée par la
    couche access et le write-path (`org_secret_meta`).

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
            "SELECT id, name, description, created_by, created_at, logo_url, "
            "domain, industry, location FROM orgs WHERE id = %s",
            (org_id,),
        ).fetchone()
        return dict(row) if row else None


# Domaine de marque d'une org : hostname nu, minuscule (acme.com). Tolère une
# saisie en URL (schéma/chemin/`www.` retirés) ; lève sur une forme non-domaine
# (pas de fallback silencieux). Le domaine vide efface (colonne NULL).
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def normalize_domain(raw: str) -> Optional[str]:
    d = (raw or "").strip().lower()
    if not d:
        return None
    d = re.sub(r"^[a-z+]+://", "", d)          # https://acme.com/… → acme.com/…
    d = d.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if not _DOMAIN_RE.match(d):
        raise ValueError(f"domaine invalide : {raw!r}")
    return d


def effective_logo_url(org: dict) -> Optional[str]:
    """Logo affiché pour une org : l'upload (`logo_url`, Object Storage) prime,
    sinon dérivé du CDN logo.dev à partir du `domain` déclaré (même patron que
    le catalogue connecteurs). None → monogramme côté UI."""
    return org.get("logo_url") or logodev.logo_url(org.get("domain"))


def update_org(org_id: int, name: Optional[str] = None,
               description: Optional[str] = None,
               domain: Optional[str] = None,
               industry: Optional[str] = None,
               location: Optional[str] = None) -> bool:
    """Édite le profil d'une org. None = conserver le champ ; chaîne vide =
    effacer (domain → NULL). False si absente.

    Miroir de `group_store.update_group` au grain org. Métadonnées en clair
    (nom/prose/domaine), hors coffre."""
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
    if domain is not None:
        sets.append("domain = %s")
        params.append(normalize_domain(domain))
    if industry is not None:
        sets.append("industry = %s")
        params.append(industry.strip())
    if location is not None:
        sets.append("location = %s")
        params.append(location.strip())
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
            "SELECT id, name, created_by, created_at, logo_url, domain FROM orgs "
            "WHERE archived_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def archive_org(org_id: int) -> bool:
    """Archive (soft-delete) une org : masquée de tous les listings, réversible
    (DB : `UPDATE orgs SET archived_at = NULL`). Aucune FK touchée (membres,
    credentials, usage restent). Les membres qui l'avaient pour org
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
            # Une org archivée est invisible à `get_personal_org` (filtre
            # `archived_at IS NULL`) : elle doit AUSSI libérer le slot perso
            # (`uq_orgs_personal_of`, partiel `personal_of IS NOT NULL` mais PAS
            # sur l'archivage) — sinon elle occupe le slot d'un user sans être
            # trouvable → `ensure_personal_org` recrée en boucle une org qui
            # échoue sur l'UPDATE personal_of (UniqueViolation) à chaque boot.
            conn.execute(
                "UPDATE orgs SET personal_of = NULL WHERE id = %s", (org_id,)
            )
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


# --- baseline de connecteurs proposés par l'org (ADR 0019) ------------------

def get_org_default_connectors(org_id: int) -> Optional[list[str]]:
    """Baseline de connecteurs *proposés* par l'org (« org propose », ADR 0019) :
    liste de noms de connecteurs recommandés à ses membres, ou None si l'org n'en
    impose pas. Consultatif — le membre reste libre de (dé)sélectionner."""
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


def get_org_mfa(org_id: int) -> dict:
    """État MFA d'une org : `{require_mfa: bool, logto_org_id: str|None}`.
    `require_mfa` = l'org impose le 2ᵉ facteur à ses membres ; `logto_org_id` =
    l'organization Logto MIROIR (None si le MFA n'a jamais été activé). Défaut
    inerte si l'org est absente."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT require_mfa, logto_org_id FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        if not row:
            return {"require_mfa": False, "logto_org_id": None}
        return {"require_mfa": bool(row["require_mfa"]),
                "logto_org_id": row["logto_org_id"]}


def set_org_require_mfa(org_id: int, require: bool) -> bool:
    """Pose le drapeau `require_mfa` de l'org (toggle org_admin). False si org
    absente. **Ne provisionne PAS** l'org Logto miroir — c'est la couche
    `mfa_mirror` qui, après ce flag, crée/supprime l'organization Logto et
    enregistre son id via `set_org_logto_org_id`. Ici, uniquement le drapeau PG."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE orgs SET require_mfa = %s WHERE id = %s RETURNING id",
            (bool(require), org_id),
        ).fetchone()
        return row is not None


def set_org_logto_org_id(org_id: int, logto_org_id: Optional[str]) -> bool:
    """Mémorise (ou efface avec None) l'id de l'organization Logto miroir de l'org.
    False si org absente."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE orgs SET logto_org_id = %s WHERE id = %s RETURNING id",
            (logto_org_id, org_id),
        ).fetchone()
        return row is not None


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


def _sync_mfa_mirror(org_id: int) -> None:
    """Reflète l'appartenance de l'org vers son org Logto miroir si elle impose le
    MFA (no-op sinon). Best-effort. Import PARESSEUX : `mfa_mirror` importe
    `org_store` → éviter le cycle en important au point d'appel."""
    from . import mfa_mirror
    mfa_mirror.on_membership_changed(org_id)


def add_org_member(org_id: int, sub: str, org_role: str = "org_member") -> None:
    """Ajoute (ou met à jour le rôle d') un membre, et choisit l'org maison.

    Auto-activation de l'org rejointe (`org_members.is_active`, = l'org maison lue
    par `get_active_org`) : une org **réelle** l'emporte TOUJOURS sur l'org perso
    silencieuse (ADR 0030/0033 : tout user a une org perso « Mon espace » créée
    d'office → sans ça, un invité atterrit sur sa perso au lieu de sa boîte). Règle,
    sur une **nouvelle** adhésion :
    - aucune org active → la nouvelle devient maison ;
    - sinon, l'org rejointe est non-perso ET l'active courante est la perso →
      promotion (la perso cède la place) ;
    - sinon → non-active (on ne débarque jamais un user d'une maison réelle établie).
    Un **re-ajout** (ligne déjà présente) ne change que le rôle, jamais l'active.

    Contrairement à set_google_oauth (table sans index unique partiel sur le
    flag, où deux TRUE sont tolérés), org_members a l'index partiel
    `org_members_one_active` (≤1 active par sub) : le calcul de make_active est une
    lecture-modification-écriture qui, sous READ COMMITTED, casserait sur deux
    adhésions concurrentes du MÊME sub (deux is_active=TRUE → IntegrityError). On
    sérialise par sub via un verrou advisory transactionnel ; `conn.transaction()`
    seul ne donne que l'atomicité, pas cette sérialisation. (L'org perso est marquée
    `personal_of` APRÈS son propre add_org_member — au 1er membre elle est donc vue
    non-perso, ce qui est sans effet : active=None → elle devient maison de toute
    façon.)
    """
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r} (attendu: {ORG_ROLES})")
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (sub,))
            existing = conn.execute(
                "SELECT 1 FROM org_members WHERE org_id = %s AND sub = %s",
                (org_id, sub),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE org_members SET org_role = %s WHERE org_id = %s AND sub = %s",
                    (org_role, org_id, sub),
                )
            else:
                active = conn.execute(
                    """
                    SELECT m.org_id, (o.personal_of IS NOT NULL) AS personal
                      FROM org_members m JOIN orgs o ON o.id = m.org_id
                     WHERE m.sub = %s AND m.is_active
                    """,
                    (sub,),
                ).fetchone()
                jp = conn.execute(
                    "SELECT (personal_of IS NOT NULL) AS p FROM orgs WHERE id = %s",
                    (org_id,),
                ).fetchone()
                joining_personal = bool(jp and jp["p"])
                if active is None:
                    make_active = True
                elif not joining_personal and active["personal"]:
                    conn.execute(
                        "UPDATE org_members SET is_active = FALSE WHERE sub = %s AND org_id = %s",
                        (sub, active["org_id"]),
                    )
                    make_active = True
                else:
                    make_active = False
                conn.execute(
                    """
                    INSERT INTO org_members (org_id, sub, org_role, is_active)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (org_id, sub, org_role, make_active),
                )
    _sync_mfa_mirror(org_id)   # pousse le nouveau membre dans l'org Logto miroir si MFA


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
    if removed:
        _sync_mfa_mirror(org_id)   # retire le membre parti de l'org Logto miroir si MFA (conn libérée)
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
            SELECT m.org_id, o.name, o.logo_url, o.domain, m.org_role, m.is_active, m.joined_at
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %s AND o.archived_at IS NULL ORDER BY m.joined_at ASC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Projet KB ancré par id (lot 3, chantier 0.3) ---------------------------
# Fin de l'identification « par son NOM » (renommable → 2 KB, transfert → KB cassée) :
# `orgs.kb_project_id` = l'ancre. kb.py résout par id + auto-répare (clear/claim).

def get_kb_project_id(org_id: int) -> Optional[int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT kb_project_id FROM orgs WHERE id = %s", (org_id,)).fetchone()
        return int(row["kb_project_id"]) if row and row["kb_project_id"] is not None else None


def claim_kb_project(org_id: int, project_id: int) -> bool:
    """Pose l'ancre SI ELLE EST LIBRE (verrou optimiste de création — deux appels
    concurrents créent chacun leur projet, un seul claim gagne, le perdant archive
    son doublon). True = ce projet est désormais LA KB de l'org."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET kb_project_id = %s WHERE id = %s AND kb_project_id IS NULL",
            (project_id, org_id))
        return (cur.rowcount or 0) > 0


def clear_kb_project(org_id: int, expected_project_id: int) -> None:
    """Lève une ancre PENDOUILLANTE (projet archivé/transféré hors org) — compare-and-
    clear pour ne jamais écraser une réparation concurrente déjà re-posée."""
    with _connect() as conn:
        conn.execute(
            "UPDATE orgs SET kb_project_id = NULL WHERE id = %s AND kb_project_id = %s",
            (org_id, expected_project_id))


def get_personal_org(sub: str) -> Optional[int]:
    """Org PERSO (privée, mono-membre) de `sub`, marquée `personal_of=sub`, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM orgs WHERE personal_of = %s AND archived_at IS NULL", (sub,)
        ).fetchone()
        return int(row["id"]) if row else None


def is_personal_org(org_id: int) -> bool:
    """True si l'org est un **espace personnel** (`personal_of` renseigné) — non
    supprimable (elle serait recréée au boot par `ensure_personal_org`)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT personal_of FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        return bool(row and row["personal_of"] is not None)


def _personal_label(email: Optional[str], name: Optional[str]) -> str:
    return (name or (email.split("@")[0] if email else None) or "Mon espace").strip() or "Mon espace"


def _reclaim_or_create_personal(sub: str, email: Optional[str], name: Optional[str]) -> int:
    """Récupère ou crée l'org perso de `sub`. **Réclamation SÛRE** : on ne marque une
    org existante comme perso QUE si c'est la SEULE org du user (mono-membre, créée par
    lui) — un user multi-org garde ses orgs partagées intactes, on lui crée une perso
    fraîche."""
    with _connect() as conn:
        # Auto-soin (couvre les DEUX branches, reclaim ET create) : une org perso
        # ARCHIVÉE détient encore le slot unique `uq_orgs_personal_of` tout en étant
        # invisible à `get_personal_org` (filtre `archived_at IS NULL`) → la relâcher
        # AVANT tout marquage, sinon UniqueViolation en boucle à chaque boot (vécu
        # 2026-07-01 : perso archivée → orgs orphelines recréées, une par boot ; la
        # collision frappait aussi bien la branche reclaim que la branche create).
        conn.execute(
            "UPDATE orgs SET personal_of = NULL "
            "WHERE personal_of = %s AND archived_at IS NOT NULL",
            (sub,),
        )
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
    # Onboarding = un projet (ADR 0032 §7) : on sème le projet « Découverte » dans l'org
    # perso fraîchement créée (une seule fois, ici — pas sur la branche reclaim). Best-effort.
    from . import discovery
    discovery.seed_for_org(sub, oid)
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
                      ttl_days: int = 7, source: Optional[str] = None,
                      group_id: Optional[int] = None,
                      group_role: Optional[str] = None) -> tuple[int, str, str]:
    """Crée une invitation nominative. **Scope dérivé** des cibles (feature cascade
    plateforme/org/équipe, comme les connecteurs) :
    - `org_id=None, group_id=None` → invitation **plateforme** (onboarding pur : à
      l'acceptation l'invité a juste son compte + org perso) ;
    - `org_id` seul → invitation **org** (rejoint l'org) ;
    - `org_id` + `group_id` → invitation **équipe** (rejoint l'org PUIS l'équipe avec
      `group_role`).
    `email` est OPTIONNEL : sans email, l'émetteur partage le code lui-même (pas d'envoi
    mail). Renvoie (id, token plaintext, code court) — token pour le lien mail legacy
    (seul son hash est persisté), code pour le lien /invitation/<code> partageable."""
    email = (email or "").strip().lower() or None
    if email is not None and "@" not in email:
        raise ValueError("email invalide")
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r}")
    if group_id is not None and org_id is None:
        raise ValueError("une invitation d'équipe exige l'org parente (org_id)")
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
                    (org_id, email, org_role, token_hash, code, invited_by, source,
                     group_id, group_role, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + (%s || ' days')::interval)
                RETURNING id
                """,
                (org_id, email, org_role, _hash_token(token), code, invited_by, source,
                 group_id, group_role, str(int(ttl_days))),
            ).fetchone()
            return int(row["id"]), token, code
        raise RuntimeError("impossible de générer un code d'invitation unique")


# Listing enrichi : chaque ligne porte de quoi afficher le scope (nom d'org/équipe)
# + un `scope` dérivé ('platform'|'org'|'team'), commun aux 3 niveaux de la cascade.
_INV_LIST_SELECT = """
    SELECT i.id, i.email, i.code, i.org_role, i.group_role, i.org_id, i.group_id,
           i.invited_by, i.source, i.created_at, i.expires_at,
           o.name AS org_name, g.name AS group_name
      FROM org_invitations i
      LEFT JOIN orgs       o ON o.id = i.org_id
      LEFT JOIN org_groups g ON g.id = i.group_id
     WHERE {pred} AND i.accepted_at IS NULL AND i.expires_at > NOW()
     ORDER BY i.created_at DESC
"""


def _scope_of(r: dict) -> str:
    if r.get("group_id") is not None:
        return "team"
    if r.get("org_id") is not None:
        return "org"
    return "platform"


def _list_invitations(pred: str, *args) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(_INV_LIST_SELECT.format(pred=pred), args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scope"] = _scope_of(d)
        out.append(d)
    return out


def list_invitations(org_id: int) -> list[dict]:
    """Invitations d'ORG en attente (hors invitations d'équipe, qui vivent sur l'écran
    équipe). Non acceptées, non expirées."""
    return _list_invitations("i.org_id = %s AND i.group_id IS NULL", org_id)


def list_group_invitations(group_id: int) -> list[dict]:
    """Invitations d'ÉQUIPE en attente pour ce groupe."""
    return _list_invitations("i.group_id = %s", group_id)


def list_platform_invitations() -> list[dict]:
    """Invitations émises PAR LA PLATEFORME (source='platform_admin'), tous scopes —
    onboarding pur (org_id NULL) ou rattachement direct à une org choisie par l'admin."""
    return _list_invitations("i.source = 'platform_admin'")


def revoke_invitation(org_id: int, inv_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE org_id = %s AND group_id IS NULL "
            "AND id = %s AND accepted_at IS NULL",
            (org_id, inv_id),
        )
        return (cur.rowcount or 0) > 0


def revoke_group_invitation(group_id: int, inv_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE group_id = %s AND id = %s "
            "AND accepted_at IS NULL",
            (group_id, inv_id),
        )
        return (cur.rowcount or 0) > 0


def revoke_platform_invitation(inv_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_invitations WHERE id = %s AND source = 'platform_admin' "
            "AND accepted_at IS NULL",
            (inv_id,),
        )
        return (cur.rowcount or 0) > 0


def _preview_from_row(r: dict) -> dict:
    return {"email": r.get("email"), "inviter": r.get("inviter"),
            "org_name": r.get("org_name"), "group_name": r.get("group_name"),
            "scope": _scope_of(r)}


_PREVIEW_SELECT = """
    SELECT i.email, i.org_id, i.group_id,
           COALESCE(u.name, u.email) AS inviter,
           o.name AS org_name,
           g.name AS group_name
      FROM org_invitations i
      LEFT JOIN users      u ON u.sub = i.invited_by
      LEFT JOIN orgs       o ON o.id  = i.org_id
      LEFT JOIN org_groups g ON g.id  = i.group_id
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
    """Aperçu PUBLIC par code court (lien /invitation/<code>)."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with _connect() as conn:
        row = conn.execute(
            _PREVIEW_SELECT.format(pred="i.code = %s"), (code,)
        ).fetchone()
        return _preview_from_row(dict(row)) if row else None


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
            SELECT id, org_id, email, org_role, group_id, group_role,
                   invited_by, source, expires_at
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


def _idempotent_accept(pred: str, val, sub: str) -> Optional[dict]:
    """Retour idempotent quand l'invitation ciblée a DÉJÀ été acceptée par le MÊME
    sub (cas vécu : `reconcile_signup_with_invitation` la consomme au 1er getMe, puis
    l'accept explicite la retrouve déjà utilisée → faux 410 alors que l'user est bien
    membre). Renvoie le même dict de succès qu'une acceptation fraîche, ou None si
    l'invitation est vraiment invalide / expirée / acceptée par un AUTRE sub."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT org_id, org_role, group_id, group_role, accepted_sub "
            f"FROM org_invitations WHERE {pred}",
            (val,),
        ).fetchone()
    if row and row["accepted_sub"] == sub:
        return {"org_id": row.get("org_id"), "org_role": row.get("org_role"),
                "group_id": row.get("group_id"), "group_role": row.get("group_role")}
    return None


def accept_invitation(token: str, sub: str) -> Optional[dict]:
    """Accepte une invitation d'org par token mail. Idempotent si déjà acceptée
    par le même sub ; None si token invalide/expiré/à autrui."""
    if not token:
        return None
    inv = get_invitation_by_token(token)
    if inv:
        return _accept_invitation_row(inv, sub)
    return _idempotent_accept("token_hash = %s", _hash_token(token), sub)


def accept_invitation_by_code(code: str, sub: str) -> Optional[dict]:
    """Accepte une invitation d'org par code court. Idempotent si déjà acceptée
    par le même sub ; None si code invalide/expiré/à autrui."""
    code = (code or "").strip().upper()
    if not code:
        return None
    inv = get_invitation_by_code(code)
    if inv:
        return _accept_invitation_row(inv, sub)
    return _idempotent_accept("code = %s", code, sub)


def _accept_invitation_row(inv: dict, sub: str) -> dict:
    """Cœur de l'acceptation d'une invitation à partir d'une ligne déjà résolue (par
    token, code OU email lors d'une réconciliation de signup). Selon le scope :
    - **org** (org_id présent) → ajoute le membre d'org + bascule l'org active ;
    - **équipe** (group_id présent) → ajoute AUSSI l'équipe (avec `group_role`) et la
      rend active (l'org parente est jointe d'abord — invariant équipe ⊂ org) ;
    - **plateforme** (ni l'un ni l'autre) → l'invité a déjà son compte + org perso au
      signup ; l'acceptation ne fait que marquer l'invitation consommée (attribution).
    """
    org_id = inv.get("org_id")
    if org_id is not None:
        add_org_member(org_id, sub, inv["org_role"])
        set_active_org(sub, org_id)
    group_id = inv.get("group_id")
    if group_id is not None:
        # Import paresseux : org_store n'importe PAS group_store au niveau module
        # (group_store dépend d'org_store → cycle). À l'appel, les deux sont chargés.
        from . import group_store
        group_store.add_group_member(group_id, sub, inv.get("group_role") or "group_member")
        group_store.set_active_group(sub, group_id)
    _mark_invitation_accepted(inv["id"], sub)
    return {"org_id": org_id, "org_role": inv.get("org_role"),
            "group_id": group_id, "group_role": inv.get("group_role")}


def reconcile_signup_with_invitation(sub: str, email: str) -> Optional[dict]:
    """Honore une invitation d'org par l'EMAIL au signup : si un nouvel inscrit a une
    invitation d'org en attente pour son email vérifié, on l'accepte automatiquement
    — il rejoint directement l'org au lieu de rester avec une invitation orpheline
    (cas vécu : invité qui s'inscrit sans passer par le lien /invite). Sûr car l'email
    est vérifié par Logto (signup email+code). None si aucune invitation."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, org_id, org_role, group_id, group_role, invited_by
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
# Modèle unifié servi par oto_procedure(op='get') / oto_*_instruction(s). Le slug réservé
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


def _base_readme(org_id: int, version: Optional[int]) -> Optional[dict]:
    """Le readme d'org (slug `claude_md`) présenté comme une instruction, mais LU dans
    `guides` (delivery='init') — depuis l'ADR 0042 le readme n'est plus une procédure :
    pas de version/slots/historique. None si absent OU version demandée (pas d'historique).
    Shape complète (champs neutres) pour rester compatible avec les vues doctrine."""
    if version is not None:
        return None
    from . import guide_store
    st = guide_store.get_init_guide("org", org_id)
    if st["updated_at"] is None:
        return None
    return {"id": None, "org_id": org_id, "slug": BASE_SLUG, "title": "",
            "description": "", "body_md": st["body_md"], "slots": [], "version": 1,
            "set_by": None, "created_at": st["updated_at"], "updated_at": st["updated_at"]}


def get_instruction(org_id: int, slug: str, version: Optional[int] = None) -> Optional[dict]:
    """Une instruction (courante, ou une `version` archivée précise). None si absente.
    Le slug réservé `claude_md` = le readme d'org, lu dans `guides` (ADR 0042)."""
    slug = normalize_slug(slug)
    if slug == BASE_SLUG:
        return _base_readme(org_id, version)
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT id, org_id, slug, title, description, body_md, slots, version, set_by, "
                "created_at, updated_at FROM org_instructions "
                "WHERE owner_type = 'org' AND org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT org_id, slug, title, description, body_md, slots, version, set_by, "
                "created_at FROM org_instruction_revisions "
                "WHERE owner_type = 'org' AND org_id = %s AND slug = %s AND version = %s",
                (org_id, slug, version),
            ).fetchone()
        return dict(row) if row else None


def list_instructions(org_id: int, include_base: bool = False) -> list[dict]:
    """Métadonnées des instructions (SANS body) = l'index des skills. Exclut la
    doctrine de base sauf `include_base` (surface admin)."""
    # `owner_type='org'` : post-fusion (chantier procédures, cadrage 10/07) la table
    # porte aussi les lignes GROUP (org_id = org parente) — une liste d'org ne doit
    # jamais les ratisser.
    where = ("owner_type = 'org' AND org_id = %s" if include_base
             else "owner_type = 'org' AND org_id = %s AND slug <> %s")
    params: tuple = (org_id,) if include_base else (org_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, slug, title, description, version, updated_at "
            f"FROM org_instructions WHERE {where} ORDER BY slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_instruction_bodies(org_id: int) -> list[dict]:
    """Slug + body_md des instructions d'une org (hors doctrine de base) — pour
    dériver les références d'outils `<tool:slug>` (compteur « doctrine-only », ADR 0024)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, body_md FROM org_instructions "
            "WHERE owner_type = 'org' AND org_id = %s AND slug <> %s",
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
            "SELECT id, slug, title, description, body_md, version, updated_at "
            "FROM org_instructions WHERE owner_type = 'org' AND org_id = %s " + base_filter +
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
                    description: Optional[str] = None, set_by: Optional[str] = None,
                    slots: Optional[list] = None) -> int:
    """Crée/met à jour une instruction ; renvoie la NOUVELLE version et archive un
    snapshot. `title`/`description`/`slots` None = conserver l'existant ('' / [] à
    la création). `slots` = entités requises déclarées (ADR 0035, validées en amont
    par `slots.validate_slots`). Sérialisé par (org, slug) via verrou advisory."""
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    # Le readme (claude_md) vit dans `guides` (ADR 0042) — prose plate, sans version/slots.
    if slug == BASE_SLUG:
        from . import guide_store
        guide_store.set_init_guide("org", org_id, body_md)
        return 1
    with _connect() as conn:
        with conn.transaction():
            # Verrou + arbitre sur la clé OWNER (chantier procédures B1) : la PK legacy
            # (org_id, slug) tombe en B2 — l'unicité vivante est (owner_type, owner_id, slug).
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"oi:org:{org_id}:{slug}",))
            cur = conn.execute(
                "SELECT version, title, description, slots FROM org_instructions "
                "WHERE owner_type = 'org' AND org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
            new_version = (cur["version"] + 1) if cur else 1
            new_title = title if title is not None else (cur["title"] if cur else "")
            new_desc = description if description is not None else (cur["description"] if cur else "")
            new_slots = json.dumps(slots if slots is not None
                                   else ((cur["slots"] if cur else None) or []))
            conn.execute(
                """
                INSERT INTO org_instructions
                    (org_id, owner_type, owner_id, slug, title, description, body_md, slots,
                     version, set_by, updated_at)
                VALUES (%s, 'org', %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (owner_type, owner_id, slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, slots = EXCLUDED.slots,
                    version = EXCLUDED.version,
                    set_by = EXCLUDED.set_by, updated_at = NOW()
                """,
                (org_id, str(org_id), slug, new_title, new_desc, body_md, new_slots,
                 new_version, set_by),
            )
            conn.execute(
                """
                INSERT INTO org_instruction_revisions
                    (org_id, owner_type, owner_id, slug, version, title, description,
                     body_md, slots, set_by)
                VALUES (%s, 'org', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (org_id, str(org_id), slug, new_version, new_title, new_desc, body_md,
                 new_slots, set_by),
            )
            return new_version


def list_instruction_versions(org_id: int, slug: str) -> list[dict]:
    """Historique d'une instruction (métadonnées par version, plus récent d'abord).
    Le readme (claude_md) n'a plus d'historique (ADR 0042) → [] ."""
    slug = normalize_slug(slug)
    if slug == BASE_SLUG:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, title, set_by, created_at FROM org_instruction_revisions "
            "WHERE owner_type = 'org' AND org_id = %s AND slug = %s ORDER BY version DESC",
            (org_id, slug),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_instruction(org_id: int, slug: str) -> bool:
    """Supprime une instruction ET son historique. False si elle n'existait pas."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_instructions "
                "WHERE owner_type = 'org' AND org_id = %s AND slug = %s", (org_id, slug)
            )
            removed = (cur.rowcount or 0) > 0
            conn.execute(
                "DELETE FROM org_instruction_revisions "
                "WHERE owner_type = 'org' AND org_id = %s AND slug = %s",
                (org_id, slug),
            )
    return removed


# --- doctrine = ressource possédée (ADR 0030, épic « couverture des autres types »,
# livraison de projet #52) : l'identité PUBLIQUE d'une doctrine est son `id` surrogate
# (ADR 0032 « stop using slug ») ; son propriétaire est porté par `owner_type/owner_id`
# (chantier procédures, cadrage 10/07 — 'org' aujourd'hui, 'group' à la fusion B2 ; il
# dérivait d'`org_id` avant). Ces fonctions alimentent le kind `doctrine`
# d'`ownership.py` + la cascade de livraison d'un projet (`oto_resource`).

def get_instruction_by_id(instruction_id: int) -> Optional[dict]:
    """Une instruction par son id surrogate (identité publique). None si absente."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, org_id, owner_type, owner_id, slug, title, description, body_md, "
            "slots, version, set_by, created_at, updated_at "
            "FROM org_instructions WHERE id = %s",
            (instruction_id,),
        ).fetchone()
        return dict(row) if row else None


def _free_instruction_slug(conn, org_id: int, slug: str) -> str:
    """Slug libre dans `org_id` : le slug tel quel, sinon suffixé (-2, -3…). On ne
    remplace JAMAIS une doctrine existante de l'org cible (livraison non destructive)."""
    candidate = slug
    for i in range(2, 100):
        row = conn.execute(
            "SELECT 1 FROM org_instructions "
            "WHERE owner_type = 'org' AND org_id = %s AND slug = %s",
            (org_id, candidate),
        ).fetchone()
        if row is None:
            return candidate
        candidate = f"{slug}-{i}"
    raise ValueError(f"aucun slug libre dérivé de `{slug}` dans l'org {org_id}")


def copy_instruction_to_org(instruction_id: int, dest_org_id: int,
                            set_by: Optional[str] = None) -> dict:
    """Copie une doctrine dans une AUTRE org (livraison par transfert de projet, #52) :
    nouvelle doctrine v1 chez la cible (slug suffixé si pris — jamais d'écrasement),
    l'originale reste intacte chez la source. Renvoie {id, slug, org_id} de la copie."""
    src = get_instruction_by_id(instruction_id)
    if src is None:
        raise ValueError(f"doctrine #{instruction_id} introuvable")
    with _connect() as conn:
        dest_slug = _free_instruction_slug(conn, dest_org_id, src["slug"])
    set_instruction(dest_org_id, dest_slug, src["body_md"],
                    title=src.get("title"), description=src.get("description"),
                    set_by=set_by, slots=src.get("slots") or [])
    created = get_instruction(dest_org_id, dest_slug)
    return {"id": created["id"], "slug": dest_slug, "org_id": dest_org_id}


def reparent_instruction(instruction_id: int, new_org_id: int) -> str:
    """Déplace une doctrine vers une autre org (transfert d'ownership ADR 0030, id
    surrogate stable). Slug suffixé si pris chez la cible ; l'historique suit quand
    il ne collisionne pas (sinon il reste chez la source — append-only, pas de perte).
    Renvoie le slug final chez la cible."""
    src = get_instruction_by_id(instruction_id)
    if src is None:
        raise ValueError(f"doctrine #{instruction_id} introuvable")
    if int(src["org_id"]) == int(new_org_id):
        return src["slug"]
    with _connect() as conn:
        dest_slug = _free_instruction_slug(conn, new_org_id, src["slug"])
        conn.execute(
            "UPDATE org_instructions SET org_id = %s, owner_type = 'org', owner_id = %s, "
            "slug = %s, updated_at = NOW() WHERE id = %s",
            (new_org_id, str(new_org_id), dest_slug, instruction_id),
        )
    # L'historique suit dans un second temps (hors transaction principale) : une
    # collision de revisions chez la cible ne doit pas annuler le transfert — il
    # reste alors chez la source (append-only, rien n'est perdu).
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE org_instruction_revisions SET org_id = %s, owner_type = 'org', "
                "owner_id = %s, slug = %s "
                "WHERE owner_type = %s AND owner_id = %s AND slug = %s",
                (new_org_id, str(new_org_id), dest_slug,
                 src.get("owner_type") or "org", src.get("owner_id") or str(src["org_id"]),
                 src["slug"]),
            )
    except Exception:
        _log.warning("reparent_instruction: historique laissé chez la source "
                     "(collision revisions, doctrine #%s)", instruction_id)
    return dest_slug


def list_instructions_for_orgs(org_ids: list[int]) -> list[dict]:
    """Doctrines (hors base) des orgs données — plan GOUVERNANCE (métadonnées + org_id,
    sans body). Alimente `oto_resource(op=list, resource_type='doctrine')`."""
    if not org_ids:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, slug, title, description, version, updated_at "
            "FROM org_instructions "
            "WHERE owner_type = 'org' AND org_id = ANY(%s) AND slug <> %s ORDER BY org_id, slug",
            (org_ids, BASE_SLUG),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_instructions() -> list[dict]:
    """Toutes les doctrines nommées (vue opérateur plateforme — gouvernance)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, slug, title, description, version, updated_at "
            "FROM org_instructions WHERE owner_type = 'org' AND slug <> %s ORDER BY org_id, slug",
            (BASE_SLUG,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- bibliothèque publique de doctrines (marketplace, table doctrine_library) ---
#
# Un catalogue cherchable de doctrines PUBLIÉES, chaque entrée portant un AUTEUR
# ('otomata' = la plateforme, ou 'org' = un créateur privé). Preview + fork dans
# son org (copie versionnée via set_instruction). En clair (prose publiable).
# Deny-by-default sur la surface anonyme : visibility='public' uniquement, jamais
# 'unlisted' (servi seulement par slug exact à un appelant authentifié).

_LIBRARY_COLS = (
    "id, slug, title, description, body_md, slots, author_kind, author_org_id, "
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
                     published_by: Optional[str] = None,
                     slots: Optional[list] = None) -> dict:
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
                    (slug, title, description, body_md, slots, author_kind, author_org_id,
                     author_display, category, tags, visibility, source_org_id,
                     source_slug, forked_from, version, published_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, slots = EXCLUDED.slots,
                    author_kind = EXCLUDED.author_kind,
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
                (slug, title, description, body_md, json.dumps(slots or []),
                 author_kind, author_org_id,
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
        slots=entry.get("slots") or [],
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
