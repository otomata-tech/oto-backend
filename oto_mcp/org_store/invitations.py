"""Les INVITATIONS — plateforme, org, équipe : émission, listing, acceptation, REFUS.

Une seule table `org_invitations` porte les trois scopes de la cascade, dérivés
des cibles posées (`org_id`/`group_id` ⇒ `_scope_of`). Un lien mail porte un
token long (256 bits, seul son hash est persisté) — c'est l'UNIQUE façon d'entrer.
Le code court partageable a existé (oto-backend#560 : 7 caractères, ~34 bits,
brute-forçable) et a été RETIRÉ le 15/09/2026 sur arbitrage d'Alexis ; la colonne
`code` et son index restent en base (boot additif seulement, jamais de DROP —
`docs/live-migrations.md`), simplement plus écrits ni lus.

Quatre façons d'en sortir, et elles ne se ressemblent pas : **acceptée**
(`accepted_at`, l'invité rejoint), **refusée** (`declined_at`, #654 — l'invité dit
non, aucune appartenance créée), **expirée** (`expires_at`), **révoquée** (la ligne
est SUPPRIMÉE, geste de l'émetteur). Le prédicat `_PENDING` est la définition
unique de « encore en attente » ; toute requête qui filtre l'état passe par lui.

Étage 1 du package : consomme `members` (adhésion + maison). Les paliers
voisins (`roles`, `group_store`) restent en import
PARESSEUX au point d'appel — c'est ce qui évite le cycle.
"""
from __future__ import annotations

import secrets
from typing import Optional

from . import members
from ..db import _connect, _hash_token


def create_invitation(org_id: Optional[int], email: Optional[str], org_role: str, invited_by: str,
                      ttl_days: int = 7, source: Optional[str] = None,
                      group_id: Optional[int] = None,
                      group_role: Optional[str] = None) -> tuple[int, str]:
    """Crée une invitation nominative. **Scope dérivé** des cibles (feature cascade
    plateforme/org/équipe, comme les connecteurs) :
    - `org_id=None, group_id=None` → invitation **plateforme** (onboarding pur : à
      l'acceptation l'invité a juste son compte + org perso) ;
    - `org_id` seul → invitation **org** (rejoint l'org) ;
    - `org_id` + `group_id` → invitation **équipe** (rejoint l'org PUIS l'équipe avec
      `group_role`).
    `email` est OPTIONNEL : sans email, l'émetteur partage le lien lui-même (pas d'envoi
    mail). Renvoie (id, token plaintext) — seul son hash est persisté."""
    email = (email or "").strip().lower() or None
    if email is not None and "@" not in email:
        raise ValueError("email invalide")
    if org_role not in members.ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r}")
    if group_id is not None and org_id is None:
        raise ValueError("une invitation d'équipe exige l'org parente (org_id)")
    token = "inv_" + secrets.token_urlsafe(32)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO org_invitations
                (org_id, email, org_role, token_hash, invited_by, source,
                 group_id, group_role, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    NOW() + (%s || ' days')::interval)
            RETURNING id
            """,
            (org_id, email, org_role, _hash_token(token), invited_by, source,
             group_id, group_role, str(int(ttl_days))),
        ).fetchone()
        return int(row["id"]), token


# « EN ATTENTE » — la définition, écrite UNE fois (#654). ⚠️ Elle était recopiée dans
# six requêtes : y ajouter le refus en en oubliant une, c'était une invitation refusée
# encore acceptable (`_get_invitation`), ou ré-acceptée toute seule au signup
# (`reconcile_signup_with_invitation`).
_PENDING = "accepted_at IS NULL AND declined_at IS NULL AND expires_at > NOW()"
_PENDING_I = ("i.accepted_at IS NULL AND i.declined_at IS NULL "
              "AND i.expires_at > NOW()")

# Listing enrichi : chaque ligne porte de quoi afficher le scope (nom d'org/équipe)
# + un `scope` dérivé ('platform'|'org'|'team'), commun aux 3 niveaux de la cascade.
_INV_LIST_SELECT = f"""
    SELECT i.id, i.email, i.org_role, i.group_role, i.org_id, i.group_id,
           i.invited_by, i.source, i.created_at, i.expires_at,
           o.name AS org_name, g.name AS group_name
      FROM org_invitations i
      LEFT JOIN orgs       o ON o.id = i.org_id
      LEFT JOIN org_groups g ON g.id = i.group_id
     WHERE {{pred}} AND {_PENDING_I}
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


def find_pending_invitation(org_id: int, email: str) -> Optional[dict]:
    """L'invitation d'ORG encore valide (non acceptée, non REFUSÉE, non expirée — une
    révoquée est SUPPRIMÉE) adressée à cet email, hors invitations d'équipe :
    `{id, created_at, expires_at}`, sans le token. None s'il n'y en a pas. La plus
    récente si la file en porte plusieurs (possible pour les lignes d'avant le refus
    #622).

    Conséquence directe du refus (#654) : une invitation déclinée ne bloque plus la
    suivante. L'émetteur peut donc réinviter sans avoir à révoquer d'abord — c'est la
    seule reprise possible après un refus, et elle ne demande aucun geste de plus."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, created_at, expires_at FROM org_invitations "
            "WHERE org_id = %s AND group_id IS NULL AND lower(email) = %s "
            f"AND {_PENDING} "
            "ORDER BY created_at DESC LIMIT 1",
            (org_id, email),
        ).fetchone()
        return dict(row) if row else None


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


_PREVIEW_SELECT = f"""
    SELECT i.email, i.org_id, i.group_id,
           u.name AS inviter,
           o.name AS org_name,
           g.name AS group_name
      FROM org_invitations i
      LEFT JOIN users      u ON u.sub = i.invited_by
      LEFT JOIN orgs       o ON o.id  = i.org_id
      LEFT JOIN org_groups g ON g.id  = i.group_id
     WHERE {{pred}} AND {_PENDING_I}
"""
# ⚠️ oto#86 : `inviter` était `COALESCE(u.name, u.email)` — nommer l'invitant EST
# intentionnel (accompagner l'accueil avant création de compte), mais le REPLI
# vers son adresse ne l'était pas. Un compte frais sans nom déclaré servait donc
# son email à un anonyme, sur la route publique ci-dessous. `inviter` est
# `string | null` côté contrat (`oto-dashboard/frontend/src/types/api.ts`) et le
# client dégrade déjà vers un message générique quand il est absent — retirer le
# repli n'a donc pas besoin d'un remplacement, juste de servir `None`.


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


def get_invitation_by_token(token: str) -> Optional[dict]:
    """Invitation EN ATTENTE (cf. `_PENDING`) pour ce token, sinon None."""
    if not token:
        return None
    return _get_invitation("token_hash = %s", _hash_token(token))


def _get_invitation(pred: str, val) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT id, org_id, email, org_role, group_id, group_role,
                   invited_by, source, expires_at
              FROM org_invitations
             WHERE {pred} AND {_PENDING}
            """,
            (val,),
        ).fetchone()
        return dict(row) if row else None


# --- Refus par l'invité (#654) ----------------------------------------------

_PEEK_SELECT = """
    SELECT i.id, i.org_id, i.email, i.org_role, i.group_id, i.group_role,
           i.invited_by, i.source, i.expires_at,
           i.accepted_at, i.accepted_sub, i.declined_at, i.declined_sub,
           (i.expires_at > NOW()) AS live,
           o.name AS org_name, g.name AS group_name
      FROM org_invitations i
      LEFT JOIN orgs       o ON o.id = i.org_id
      LEFT JOIN org_groups g ON g.id = i.group_id
     WHERE {pred}
"""


def peek_invitation(*, token: Optional[str] = None) -> Optional[dict]:
    """La ligne visée par un secret d'invitation, **SANS filtre d'état**.

    `get_invitation_by_token` ne rend que les invitations en attente : un None y
    confond « ce token n'existe pas », « expirée », « déjà acceptée » et « tu l'as
    déjà refusée ». Le refus doit les distinguer (succès idempotent au dernier cas,
    410 aux autres) et lire l'adresse invitée avant d'autoriser quoi que ce soit.
    Cette lecture sert donc à DÉCIDER ; ce n'est jamais elle qui autorise.

    Rend en plus `live` (non expirée), `scope` et `org_name`. None si le secret ne
    désigne aucune ligne — une révoquée en fait partie : elle est SUPPRIMÉE."""
    if not token:
        return None
    pred, val = "i.token_hash = %s", _hash_token(token)
    with _connect() as conn:
        row = conn.execute(_PEEK_SELECT.format(pred=pred), (val,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scope"] = _scope_of(d)
    return d


def mark_invitation_declined(inv_id: int, sub: str) -> bool:
    """Marque l'invitation REFUSÉE par `sub`. True si cet appel l'a écrit.

    False quand la ligne a déjà été acceptée ou déjà refusée — la garde est dans le
    WHERE, pas dans une lecture préalable : deux clics simultanés ne doivent pas
    pouvoir écraser une acceptation par un refus. **N'ajoute aucune appartenance et
    n'en retire aucune** : refuser, c'est fermer l'invitation, pas quitter une org
    (pour ça, `remove_org_member` / `me.leave_org`)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE org_invitations SET declined_at = NOW(), declined_sub = %s "
            "WHERE id = %s AND accepted_at IS NULL AND declined_at IS NULL",
            (sub, inv_id),
        )
        return (cur.rowcount or 0) > 0


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
    l'invitation est vraiment invalide / expirée / REFUSÉE / acceptée par un AUTRE
    sub — une refusée a `accepted_sub` NULL, donc elle tombe ici sans rien rendre."""
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
        return _accept_invitation_row(inv, sub, actor=sub)
    return _idempotent_accept("token_hash = %s", _hash_token(token), sub)


def _accept_invitation_row(inv: dict, sub: str, *, actor: Optional[str]) -> dict:
    """Cœur de l'acceptation d'une invitation à partir d'une ligne déjà résolue (par
    token OU email lors d'une réconciliation de signup). Selon le scope :
    - **org** (org_id présent) → ajoute le membre d'org ; la MAISON n'est posée que
      par `add_org_member`, sous SA condition (voir plus bas) ;
    - **équipe** (group_id présent) → ajoute AUSSI l'équipe (avec `group_role`) et la
      rend active SI la maison est bien l'org du groupe (l'org parente est jointe
      d'abord — invariant équipe ⊂ org) ;
    - **plateforme** (ni l'un ni l'autre) → l'invité a déjà son compte + org perso au
      signup ; l'acceptation ne fait que marquer l'invitation consommée (attribution).

    **Accepter est un AJOUT, jamais une rétrogradation (#297).** `add_org_member` et
    `add_group_member` sont des upserts : écrire le rôle de l'invitation tel quel
    écrasait VERS LE BAS le rôle déjà détenu — un org_admin invité en `org_member`
    perdait ses droits en cliquant « accepter », et au palier équipe le défaut
    `group_member` rétrogradait un chef même quand l'invitation ne parlait pas
    d'équipe. On garde donc le **maximum des deux rôles** ; l'administrateur qui veut
    rétrograder a la route dédiée (`org.member.set_role`, gardée #273/#280). Les rangs
    viennent de `roles` (source unique de la hiérarchie), jamais recopiés ici.

    **Accepter ne déplace pas la MAISON (oto#161).** Ce corps appelait
    `members.set_active_org(sub, org_id)` juste après l'ajout — un `is_active` posé
    SANS condition, qui débarquait l'invité de son org par défaut, y compris sans
    aucun clic par `reconcile_signup_with_invitation`. Mesuré en production le
    10/09/2026 : 7 personnes déplacées d'une org réelle vers une autre, et 8 dont les
    clés membre (rangées sous `{maison}:{sub}`, jamais migrées quand la maison change)
    sont devenues injoignables par défaut — leurs appels tombent en « non configuré ».
    La règle de la maison est écrite UNE fois, dans `add_org_member` (ADR 0030/0033 :
    aucune maison → la nouvelle ; maison = l'espace perso silencieux → promotion ;
    maison réelle établie → on n'y touche pas). On la laisse décider ici aussi plutôt
    que de la recopier : deux formulations divergeraient au premier changement.
    Corollaire au palier équipe — `set_active_group` écrit lui AUSSI
    `org_members.is_active` (invariant ADR 0012, groupe actif ⊂ org active) : l'appeler
    nu ré-ouvrait le même trou par la bande, d'où la condition sur la maison.
    """
    # Import paresseux : `roles` importe org_store (et group_store) au niveau module
    # → cycle si on l'importait en tête. À l'appel, tout est chargé.
    from .. import roles
    org_id = inv.get("org_id")
    org_role = inv.get("org_role")
    if org_id is not None:
        org_role = roles.max_org_role(members.get_org_role(org_id, sub), org_role)
        members.add_org_member(org_id, sub, org_role, actor=actor)
    group_id = inv.get("group_id")
    group_role = inv.get("group_role")
    if group_id is not None:
        # Import paresseux : org_store n'importe PAS group_store au niveau module
        # (group_store dépend d'org_store → cycle). À l'appel, les deux sont chargés.
        from .. import group_store
        group_role = roles.max_group_role(group_store.get_group_role(group_id, sub),
                                          group_role or "group_member")
        group_store.add_group_member(group_id, sub, group_role)
        if org_id is not None and members.get_active_org(sub) == org_id:
            group_store.set_active_group(sub, group_id)
    _mark_invitation_accepted(inv["id"], sub)
    # Les rôles rendus sont ceux ÉCRITS, pas ceux de l'invitation : sinon l'écho
    # annonce « tu es org_member » à quelqu'un qui vient de rester org_admin.
    return {"org_id": org_id, "org_role": org_role,
            "group_id": group_id, "group_role": group_role}


def reconcile_signup_with_invitation(sub: str, email: str) -> Optional[dict]:
    """Honore une invitation d'org par l'EMAIL au signup : si un nouvel inscrit a une
    invitation d'org en attente pour son email vérifié, on l'accepte automatiquement
    — il rejoint directement l'org au lieu de rester avec une invitation orpheline
    (cas vécu : invité qui s'inscrit sans passer par le lien /invite). Sûr car l'email
    est vérifié par Logto (signup email+code). None si aucune invitation.

    ⚠️ Une invitation REFUSÉE (#654) en est exclue par `_PENDING`, et c'est le point
    le plus facile à manquer : sans ça, refuser puis créer son compte avec la même
    adresse aurait fait rejoindre l'org automatiquement — le refus annulé par le
    signup, sans que personne ne l'ait demandé."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT id, org_id, org_role, group_id, group_role, invited_by
              FROM org_invitations
             WHERE {_PENDING} AND lower(email) = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (email,),
        ).fetchone()
    if not row:
        return None
    # Aucun clic : c'est le système qui honore l'invitation au signup.
    return _accept_invitation_row(dict(row), sub, actor=None)
