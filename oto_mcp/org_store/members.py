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
from ..db.lecture_d_appel import lecture_d_appel
from ..db.org_d_inscription import seule_d_inscription


ORG_ROLES = ("org_admin", "org_member")


# --- reads consommés par la résolution de clé (barreau 2) -------------------

@lecture_d_appel
def nom_d_org(org_id: int) -> Optional[str]:
    """The org's name alone, read once per call: how a listing names the org that owns
    an account, without rereading the org per connector (#1148)."""
    with _connect() as conn:
        row = conn.execute("SELECT name FROM orgs WHERE id = %s", (org_id,)).fetchone()
    return row["name"] if row else None


@lecture_d_appel
def get_active_org(sub: str) -> Optional[int]:
    """org_id de l'organisation active du `sub`, ou None s'il n'en a aucune.

    L'index partiel `org_members_one_active` garantit au plus une ligne active
    par sub ; LIMIT 1 reste défensif (ne jamais supposer exactement une TRUE).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE sub = %s AND is_active "
            # Le compte de service d'une org n'a qu'une org, la sienne (`_COMPTE_…`).
            "UNION ALL SELECT org_id FROM org_service_accounts WHERE sub = %s LIMIT 1",
            (sub, sub),
        ).fetchone()
        return int(row["org_id"]) if row else None


# --- le COMPTE DE SERVICE d'une org, porteur de ses clés d'API (#1188) ------
#
# Une ligne `users` par org, jamais une personne (`docs/cles-d-org.md`). Il n'a AUCUNE
# ligne `org_members` : son rôle `org_member` dans son org est DÉRIVÉ ici, par les trois
# lectures d'appartenance (`get_active_org`, `list_orgs_for_user`, `get_org_role`) — et
# par elles seules. Une requête qui joint `org_members` pour décider d'un accès ne le
# voit pas : il y est refusé (fail-closed) plutôt que de paraître dans les listes de
# membres, les sièges et la facturation. `org_member` et non `org_admin` : un admin voit
# toutes les équipes de l'org, le compte d'org ne voit que ce qu'on lui partage.

_PREFIXE_COMPTE = "org-"


def org_du_compte_de_service(sub: str) -> Optional[int]:
    """L'org dont `sub` est le compte de service, None pour toute personne."""
    with _connect() as conn:
        row = conn.execute("SELECT org_id FROM org_service_accounts WHERE sub = %s",
                           (sub,)).fetchone()
        return int(row["org_id"]) if row else None


def compte_de_service_de(org_id: int) -> Optional[str]:
    """Le sub du compte de service de l'org, None si elle n'a jamais eu de clé."""
    with _connect() as conn:
        row = conn.execute("SELECT sub FROM org_service_accounts WHERE org_id = %s",
                           (org_id,)).fetchone()
        return row["sub"] if row else None


def assurer_compte_de_service(org_id: int, by: str) -> str:
    """Le sub du compte de service de l'org, créé au premier appel. Idempotent.

    ⚠️ Sa ligne `users` naît ICI, jamais par `upsert_user` : une naissance par
    `upsert_user` lui créerait une org perso. Son sub porte le préfixe du TENANT de
    l'org (`<slug>:org-<id>`) : c'est ce préfixe qui fait couper ses jetons par la
    désactivation du tenant (`db.tenants`), sans code de plus."""
    from .. import tenancy  # paresseux : feuilles du package
    from ..db.tenants import org_tenant_slug
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT o.id, o.name, s.sub FROM orgs o "
                "LEFT JOIN org_service_accounts s ON s.org_id = o.id "
                "WHERE o.id = %s AND o.archived_at IS NULL FOR NO KEY UPDATE OF o",
                (org_id,)).fetchone()
            if row is None:
                raise ValueError(f"org #{org_id} inconnue ou archivée")
            if row["sub"]:
                return row["sub"]
            tenant = org_tenant_slug(org_id, conn=conn)
            # Le tenant primaire ne préfixe pas ses comptes.
            prefixe = "" if tenant == tenancy.primary_slug() else f"{tenant}:"
            sub = f"{prefixe}{_PREFIXE_COMPTE}{org_id}"
            conn.execute(
                "INSERT INTO users (sub, name) VALUES (%s, %s) ON CONFLICT (sub) DO NOTHING",
                (sub, f"Clés d'API — {row['name']}"))
            conn.execute(
                "INSERT INTO org_service_accounts (org_id, sub, created_by) "
                "VALUES (%s, %s, %s)", (org_id, sub, by))
            return sub


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
    par `get_active_org`) : une org d'équipe l'emporte sur l'org que l'inscription a
    posée d'office et où le compte est encore SEUL (`db.org_d_inscription.
    seule_d_inscription`, l'équivalent de l'ex-org perso) — sans ça, un invité atterrit
    sur l'org de son inscription au lieu de sa boîte. `auto_created` seul ne suffit
    pas : la marque est permanente, et l'org d'une entreprise née d'une inscription la
    garde une fois peuplée. Règle, sur une **nouvelle** adhésion :
    - aucune org active → la nouvelle devient maison ;
    - sinon, l'org rejointe n'est pas une org d'inscription seule (une org où quelqu'un
      d'autre est déjà, `auto_created` ou non : on y invite, c'est une org d'équipe) ET
      l'active courante est une org d'inscription seule → promotion ;
    - sinon, l'active courante est SUSPENDUE et l'org rejointe ne l'est pas →
      promotion aussi (on ne laisse personne sur un mur) ;
    - sinon → non-active (on ne débarque jamais un user d'une maison réelle établie,
      ni d'une org d'entreprise peuplée, `auto_created` ou non).
    Un **re-ajout** (ligne déjà présente) ne change que le rôle, jamais l'active.

    Contrairement à set_google_oauth (table sans index unique partiel sur le
    flag, où deux TRUE sont tolérés), org_members a l'index partiel
    `org_members_one_active` (≤1 active par sub) : le calcul de make_active est une
    lecture-modification-écriture qui, sous READ COMMITTED, casserait sur deux
    adhésions concurrentes du MÊME sub (deux is_active=TRUE → IntegrityError). On
    sérialise par sub via un verrou advisory transactionnel ; `conn.transaction()`
    seul ne donne que l'atomicité, pas cette sérialisation.
    """
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            add_org_member_in(conn, org_id, sub, org_role, actor=actor)
    _sync_mfa_mirror(org_id)   # pousse le nouveau membre dans l'org Logto miroir si MFA


def add_org_member_in(conn, org_id: int, sub: str, org_role: str, *,
                      actor: Optional[str]) -> None:
    """Le corps d'`add_org_member`, sur la transaction OUVERTE de l'appelant : pour
    lier l'adhésion à une écriture qui doit réussir ou échouer avec elle (la
    consommation d'une invitation, `invitations._accept_invitation_row`). La ligne
    `users` et le miroir MFA restent à l'appelant, hors de la transaction."""
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r} (attendu: {ORG_ROLES})")
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
            f"""
            SELECT m.org_id, {seule_d_inscription('o', '%(sub)s')} AS seule,
                   (o.suspended_at IS NOT NULL) AS suspended
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %(sub)s AND m.is_active
            """,
            {"sub": sub},
        ).fetchone()
        # La ligne du compte n'est pas encore posée : « seule » = personne d'autre n'y
        # est, soit l'org que l'inscription pose à son premier membre.
        jp = conn.execute(
            f"SELECT {seule_d_inscription('o', '%(sub)s')} AS p, "
            "(o.suspended_at IS NOT NULL) AS s FROM orgs o WHERE o.id = %(org)s",
            {"sub": sub, "org": org_id},
        ).fetchone()
        joining_personal = bool(jp and jp["p"])
        # Une maison SUSPENDUE cède aussi la place à une org active qu'on rejoint :
        # rester dessus, c'est atterrir sur le mur de l'essai fini à chaque
        # connexion alors que l'org qui paie est là. Les clés membre rangées sous
        # l'ancienne maison n'y servaient de toute façon plus (la suspension
        # refuse ses appels) ; la lever ne déplace rien en retour.
        cede = active is not None and (
            active["seule"]
            or (active.get("suspended") and not (jp and jp.get("s"))))
        if active is None:
            make_active = True
        elif not joining_personal and cede:
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
                # Ses favoris PERSONNELS dans cette org partent avec lui : ils ne
                # servaient qu'à lui, ici (`capabilities/orgs/favoris.py`). Les favoris
                # PARTAGÉS qu'il a posés restent à l'org.
                conn.execute(
                    "DELETE FROM org_favorites WHERE org_id = %s AND owner_sub = %s",
                    (org_id, sub),
                )
                has_active = conn.execute(
                    "SELECT 1 FROM org_members WHERE sub = %s AND is_active", (sub,)
                ).fetchone()
                if not has_active:
                    # Jamais une org ARCHIVÉE : promue org active, elle devenait le
                    # contexte d'un compte alors qu'elle est sortie de tous les listings
                    # (même règle que `orgs.archive_org`, plus ancienne org VIVANTE).
                    conn.execute(
                        """
                        UPDATE org_members SET is_active = TRUE
                         WHERE sub = %s AND org_id = (
                             SELECT m.org_id FROM org_members m
                               JOIN orgs o ON o.id = m.org_id
                              WHERE m.sub = %s AND o.archived_at IS NULL
                              ORDER BY m.joined_at ASC LIMIT 1
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
    """Orgs non archivées du compte. `personal` est toujours False : plus
    d'org perso (05/11/2026), le champ reste servi pour ses lecteurs."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.org_id, o.name, o.logo_url, o.domain, m.org_role, m.is_active, m.joined_at,
                   FALSE AS personal
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %s AND o.archived_at IS NULL
            UNION ALL
            SELECT s.org_id, o.name, o.logo_url, o.domain, 'org_member', TRUE, s.created_at,
                   FALSE
              FROM org_service_accounts s JOIN orgs o ON o.id = s.org_id
             WHERE s.sub = %s AND o.archived_at IS NULL
             ORDER BY joined_at ASC
            """,
            (sub, sub),
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


def list_org_members_with_users(org_id: int) -> list[dict]:
    """`list_org_members`, plus ce que la fiche d'org lit de la ligne `users` de chaque
    membre (`email`, `name`, `avatar_url`, `suspended_at`, `user_role`), en UNE
    requête : la fiche faisait un `db.get_user` par membre. `None` pour un membre sans
    ligne `users`, comme le `get_user(...) or {}` qu'elle remplace."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT m.sub, m.org_role, m.is_active, m.joined_at, u.email, u.name, "
            "u.avatar_url, u.suspended_at, u.role AS user_role "
            "FROM org_members m LEFT JOIN users u ON u.sub = m.sub "
            "WHERE m.org_id = %s ORDER BY m.joined_at",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_org_members(org_ids: list[int]) -> dict[int, int]:
    """`len(list_org_members(o))` pour chaque org, en UNE requête : les mêmes lignes
    (`org_members` seul, ni compte de service ni filtre), comptées. Une org sans
    membre vaut 0. Sert la liste des orgs d'un compte, qui comptait org par org."""
    ids = sorted({int(o) for o in org_ids})
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id, count(*) AS n FROM org_members "
            "WHERE org_id = ANY(%s) GROUP BY org_id",
            (ids,),
        ).fetchall()
    comptes = {r["org_id"]: r["n"] for r in rows}
    return {o: comptes.get(o, 0) for o in ids}


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


def membres_par_email(org_id: int, email: str) -> list[str]:
    """Les `sub` de TOUS les membres réels de l'org (`org_members` : ni l'escalade
    plateforme de `roles.effective_org_role`, ni le compte de service) dont le compte
    porte cet email, comparé en minuscules après strip — tous, pour qu'une adresse
    portée par deux comptes se refuse au lieu de se deviner. Pour l'agent Slack de
    l'org (`slack_events.membre`) : l'email Slack désigne qui le run incarne."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT m.sub FROM org_members m JOIN users u ON u.sub = m.sub "
            "WHERE m.org_id = %s AND lower(u.email) = %s ORDER BY m.sub",
            (org_id, email),
        ).fetchall()
        return [r["sub"] for r in rows]


@lecture_d_appel
def get_org_role(org_id: int, sub: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s "
            "UNION ALL SELECT 'org_member' FROM org_service_accounts "
            "WHERE org_id = %s AND sub = %s LIMIT 1",
            (org_id, sub, org_id, sub),
        ).fetchone()
        return row["org_role"] if row else None
