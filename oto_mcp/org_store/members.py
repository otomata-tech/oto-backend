"""L'APPARTENANCE à une org et l'org MAISON (`org_members`).

Ajouter / retirer un membre, son rôle, et le pointeur `is_active` = l'org
**maison** (défaut persistant, ADR 0023 — surtout PAS le contexte courant, qui se
lit par `access.current_org`).

⚠️ **N'importe PAS `group_store`** (qui dépend d'org_store → cycle) : l'invariant
« l'équipe est subordonnée à l'org » est tenu en **SQL direct** sur
`org_group_members` (`remove_org_member` en sort le membre, `set_active_org`
invalide le groupe actif). Cette règle a survécu à la découpe et vaut pour TOUT
module du package — le cliquet `test_org_store_surface_frozen` la vérifie.

Feuille du package : n'importe aucun de ses frères.
"""
from __future__ import annotations

from typing import Optional

from ..db import _connect, upsert_user


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


def _sync_mfa_mirror(org_id: int) -> None:
    """Reflète l'appartenance de l'org vers son org Logto miroir si elle impose le
    MFA (no-op sinon). Best-effort. Import PARESSEUX : `mfa_mirror` importe
    `org_store` → éviter le cycle en important au point d'appel."""
    from .. import mfa_mirror
    mfa_mirror.on_membership_changed(org_id)


# Le JOURNAL des entrées et sorties (`org_member_events`, otomata-tech/oto#145) :
# chaque geste qui ajoute, retire ou change le rôle d'un membre y écrit sa ligne DANS
# SA transaction — un geste sans ligne, ou une ligne sans geste, ne peut pas exister.
# Lu par les admins de l'org et l'opérateur plateforme (capacité `org.member.events`) ;
# la personne retirée n'est pas notifiée et ne lit rien de l'org quittée.


def _journaliser(conn, org_id: int, sub: str, action: str, old_role: Optional[str],
                 new_role: Optional[str], actor: Optional[str]) -> None:
    """`actor` = le sub de celui qui agit ; None = le système (création d'espace
    personnel, réconciliation d'invitation au signup)."""
    conn.execute(
        """
        INSERT INTO org_member_events (org_id, sub, action, old_role, new_role, actor_sub)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (org_id, sub, action, old_role, new_role, actor),
    )


def add_org_member(org_id: int, sub: str, org_role: str = "org_member", *,
                   actor: Optional[str] = None) -> None:
    """Ajoute (ou met à jour le rôle d') un membre, et choisit l'org maison.

    Journalise `added` (nouvelle adhésion) ou `role_changed` (rôle réellement changé ;
    un re-ajout au même rôle n'écrit rien), avec `actor` (sub de qui agit, None = le
    système). Tout appel de production NOMME son acteur, `actor=None` compris
    (garde `tests/test_journal_membres_145.py`) : le défaut ne sert qu'aux bancs.

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
                "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s",
                (org_id, sub),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE org_members SET org_role = %s WHERE org_id = %s AND sub = %s",
                    (org_role, org_id, sub),
                )
                if existing["org_role"] != org_role:
                    _journaliser(conn, org_id, sub, "role_changed",
                                 existing["org_role"], org_role, actor)
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
                if joining_personal:
                    # Une org perso est par définition mono-membre (`orgs.personal_of`,
                    # slot unique par sub) : dès qu'un 2e membre distinct la rejoint, ce
                    # n'en est plus une (même geste que `archive_org` qui libère ce
                    # slot). Sans ce clear, une org perso invitée en devient une VRAIE
                    # org multi-membre qui garde pourtant `personal_of` à vie — front
                    # et `is_personal_org` continuent de la traiter comme "personal"
                    # (masque Teams/Invite) alors qu'elle a de vrais coéquipiers. Vécu
                    # 2026-08-04 (un tenant tiers, org réelle restée bloquée "personal").
                    conn.execute(
                        "UPDATE orgs SET personal_of = NULL WHERE id = %s", (org_id,)
                    )
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
                _journaliser(conn, org_id, sub, "added", None, org_role, actor)
    _sync_mfa_mirror(org_id)   # pousse le nouveau membre dans l'org Logto miroir si MFA


def remove_org_member(org_id: int, sub: str, *, actor: Optional[str] = None) -> bool:
    """Retire un membre. Si on retire son org active et qu'il en reste, promeut
    la plus ancienne restante (mirroir delete_google_oauth).

    Journalise `removed` (ancien rôle, `actor` = qui agit, lui-même s'il part) dans la
    transaction du retrait. Aucune notification à la personne retirée."""
    with _connect() as conn:
        with conn.transaction():
            gone = conn.execute(
                "DELETE FROM org_members WHERE org_id = %s AND sub = %s RETURNING org_role",
                (org_id, sub),
            ).fetchone()
            removed = gone is not None
            if removed:
                _journaliser(conn, org_id, sub, "removed", gone["org_role"], None, actor)
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


def list_member_events(org_id: int, *, limit: int,
                       before_id: Optional[int] = None) -> list[dict]:
    """Le journal des entrées/sorties de l'org, le plus récent d'abord, par pages de
    `limit` lignes, antérieures à `before_id` (curseur = l'`id` de la dernière ligne
    lue). L'email et le nom du membre et de l'acteur viennent de `users` — `None` si le
    compte n'y est plus."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.sub, u.email, u.name, e.action, e.old_role, e.new_role,
                   e.actor_sub, a.email AS actor_email, e.at
              FROM org_member_events e
              LEFT JOIN users u ON u.sub = e.sub
              LEFT JOIN users a ON a.sub = e.actor_sub
             WHERE e.org_id = %s AND (%s::bigint IS NULL OR e.id < %s::bigint)
             ORDER BY e.id DESC
             LIMIT %s
            """,
            (org_id, before_id, before_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_org_member_by_email(org_id: int, email: str) -> Optional[dict]:
    """Le membre de l'org dont le COMPTE porte cet email (`users.email`, comparé en
    minuscules après strip), ou None. Sert au refus « déjà membre » d'une invitation
    (#622) : l'invitation vise une adresse, l'appartenance un `sub` — c'est ici que
    les deux se rejoignent."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT m.sub, m.org_role FROM org_members m JOIN users u ON u.sub = m.sub "
            "WHERE m.org_id = %s AND lower(u.email) = %s LIMIT 1",
            (org_id, email),
        ).fetchone()
        return dict(row) if row else None


def get_org_role(org_id: int, sub: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s",
            (org_id, sub),
        ).fetchone()
        return row["org_role"] if row else None
