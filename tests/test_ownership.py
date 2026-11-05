"""Seam d'ownership (ADR 0030) — plans CONTENU (can_access) vs GOUVERNANCE (can_govern).

On stubbe l'owner de la ressource, les grants, l'appartenance org/groupe et l'escalade
roles. Invariant clé : l'escalade plateforme GOUVERNE une ressource perso mais n'en LIT
pas le contenu (privacy by default).
"""
import pytest

from oto_mcp import ownership

RT = "datastore"
RID = "7"


def _wire(monkeypatch, *, owner, grant=None, org_ids=(), group_ids=(),
          super_admin=False, org_admin_of=(), org_member_of=(), group_admin_of=(),
          group_read_of=()):
    # owner de la ressource
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: owner)
    # L'ancien propriétaire garde un partage nominatif : sa maison (org perso) existe
    # déjà — sans cette doublure, `ownership.maison_du_partage` la lirait en base.
    monkeypatch.setattr(ownership.org_store, "home_org", lambda sub: 1)
    # scope de l'acteur
    monkeypatch.setattr(ownership, "accessor_scope",
                        lambda sub: ownership.AccessorScope(sub, list(org_ids), list(group_ids)))
    # grant unique (ou None) — get_resource_grant matche n'importe quel principal de l'acteur
    def _get_grant(rt, rid, ptype, pid):
        if grant and grant[0] == ptype and grant[1] == pid:
            # grant = (ptype, pid, permission[, role]) — role optionnel (ADR 0048).
            role = grant[3] if len(grant) > 3 else {"read": "viewer"}.get(grant[2], "editor")
            return {"permission": grant[2], "role": role}
        return None
    monkeypatch.setattr(ownership.db, "get_resource_grant", _get_grant)
    # escalade roles
    monkeypatch.setattr(ownership.roles, "is_platform_admin", lambda sub: super_admin)
    monkeypatch.setattr(ownership.roles, "is_org_admin",
                        lambda sub, oid: super_admin or oid in org_admin_of)
    monkeypatch.setattr(ownership.roles, "is_org_member",
                        lambda sub, oid: super_admin or oid in org_member_of or oid in org_admin_of)
    monkeypatch.setattr(ownership.roles, "can_admin_group",
                        lambda sub, gid: super_admin or gid in group_admin_of)
    monkeypatch.setattr(ownership.roles, "can_read_group",
                        lambda sub, gid: super_admin or gid in group_read_of or gid in group_admin_of)


def test_owner_user_has_full_content(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"))
    assert ownership.can_access("alice", RT, RID, "read")
    assert ownership.can_access("alice", RT, RID, "write")
    assert ownership.can_govern("alice", RT, RID)


def test_stranger_denied(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"))
    assert not ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_govern("bob", RT, RID)


def test_grantee_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write"))
    assert ownership.can_access("bob", RT, RID, "read")
    assert ownership.can_access("bob", RT, RID, "write")
    # un grant de contenu ne donne PAS la gouvernance
    assert not ownership.can_govern("bob", RT, RID)


def test_grantee_read_only_cannot_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "read"))
    assert ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_access("bob", RT, RID, "write")


def test_super_admin_governs_but_does_not_read_personal(monkeypatch):
    """Invariant privacy : le super_admin GOUVERNE un classeur perso (transfert)
    mais n'en LIT pas le contenu."""
    _wire(monkeypatch, owner=("user", "alice"), super_admin=True)
    assert ownership.can_govern("ops", RT, RID)         # peut transférer
    assert not ownership.can_access("ops", RT, RID, "read")  # mais pas lire


def test_org_owned_member_reads_admin_governs(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"), org_member_of={42})
    assert ownership.can_access("m", RT, RID, "read")
    assert ownership.can_access("m", RT, RID, "write")   # membre = read+write (classeur d'équipe)
    assert not ownership.can_govern("m", RT, RID)        # membre simple ne gouverne pas


def test_org_owned_admin_governs(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"), org_admin_of={42})
    assert ownership.can_govern("adm", RT, RID)


def test_org_owned_outsider_denied(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"))
    assert not ownership.can_access("x", RT, RID, "read")
    assert not ownership.can_govern("x", RT, RID)


def test_group_grant_read_honored(monkeypatch):
    # Partage à une ÉQUIPE : un membre du groupe (scope group_ids) lit via le grant,
    # n'écrit pas (read), ne gouverne pas.
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "read"), group_ids=(5,))
    assert ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_access("bob", RT, RID, "write")
    assert not ownership.can_govern("bob", RT, RID)


def test_group_grant_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "write"), group_ids=(5,))
    assert ownership.can_access("bob", RT, RID, "write")


def test_group_grant_non_member_denied(monkeypatch):
    # Le grant vise le groupe 5 ; un acteur hors de ce groupe ne matche aucun principal.
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "write"), group_ids=(6,))
    assert not ownership.can_access("eve", RT, RID, "read")


def test_group_owned_member_reads_admin_governs(monkeypatch):
    # Ressource POSSÉDÉE par un groupe (datastore d'équipe REST) : membre = contenu,
    # chef d'équipe = gouvernance.
    _wire(monkeypatch, owner=("group", "5"), group_read_of={5})
    assert ownership.can_access("m", RT, RID, "write")
    assert not ownership.can_govern("m", RT, RID)
    _wire(monkeypatch, owner=("group", "5"), group_admin_of={5})
    assert ownership.can_govern("chef", RT, RID)


# --- ADR 0048 : gouvernance GRANTABLE (rôle `gérant`) + tripwire ---------------

def test_manager_grant_governs_but_not_transfers(monkeypatch):
    """Le gérant (grant role=manager) GOUVERNE (re-partage/supprime) mais ne TRANSFÈRE
    pas la propriété (ADR 0048 §3) et n'a pas d'escalade structurelle."""
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write", "manager"))
    assert ownership.can_govern("bob", RT, RID)          # gouvernance grantée
    assert not ownership.can_transfer("bob", RT, RID)    # mais pas le transfert
    # un gérant a aussi le contenu (manager ⇒ permission write)
    assert ownership.can_access("bob", RT, RID, "write")


def test_editor_grant_never_governs(monkeypatch):
    """TRIPWIRE gouvernance : un éditeur (write, non-manager) ne gouverne JAMAIS."""
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write", "editor"))
    assert ownership.can_access("bob", RT, RID, "write")
    assert not ownership.can_govern("bob", RT, RID)
    assert not ownership.can_transfer("bob", RT, RID)


def test_govern_tripwire_stranger_never_governs(monkeypatch):
    """TRIPWIRE : ni un inconnu ni un lecteur/éditeur ne gouvernent une ressource
    qu'ils ne possèdent pas et sur laquelle ils n'ont pas de grant `gérant`."""
    for g in (None, ("user", "bob", "read", "viewer"), ("user", "bob", "write", "editor")):
        _wire(monkeypatch, owner=("user", "alice"), grant=g)
        assert not ownership.can_govern("bob", RT, RID)


def test_manager_grant_via_team(monkeypatch):
    """Gérant accordé à une ÉQUIPE : un membre du groupe gouverne via ce grant."""
    _wire(monkeypatch, owner=("org", "42"), grant=("group", "5", "write", "manager"),
          group_ids=(5,))
    assert ownership.can_govern("m", RT, RID)
    assert not ownership.can_transfer("m", RT, RID)   # gérant ≠ org_admin


# --- Garde-fou anti-lockout : would_retain_control (prédiction, pas de mutation) -----

def test_retain_control_self_and_admin_orgs(monkeypatch):
    """L'acteur RETIENT le contrôle en cédant à lui-même, ou à une org/équipe qu'il
    administre → pas de confirmation exigée."""
    _wire(monkeypatch, owner=("org", "42"), org_admin_of=(42,), group_admin_of=(5,))
    assert ownership.would_retain_control("alice", "user", "alice")   # à moi
    assert ownership.would_retain_control("alice", "org", "42")       # org que j'admin
    assert ownership.would_retain_control("alice", "group", "5")      # équipe que je pilote


def test_lose_control_stranger_or_unadmined_scope(monkeypatch):
    """L'acteur PERD le contrôle en cédant à un autre user, ou à une org/équipe qu'il
    n'administre pas → confirmation exigée (would_retain_control = False)."""
    _wire(monkeypatch, owner=("org", "42"), org_admin_of=(42,), org_member_of=(99,))
    assert not ownership.would_retain_control("alice", "user", "bob")   # cession à un tiers
    assert not ownership.would_retain_control("alice", "org", "99")     # simple membre, pas admin
    assert not ownership.would_retain_control("alice", "group", "7")    # équipe non pilotée


def test_platform_admin_always_retains_control(monkeypatch):
    """Le platform_admin rattrape n'importe quelle ressource → il retient toujours le
    contrôle, quelle que soit la cible : jamais bloqué par la confirmation."""
    _wire(monkeypatch, owner=("user", "alice"), super_admin=True)
    assert ownership.would_retain_control("ops", "user", "someone-else")
    assert ownership.would_retain_control("ops", "platform", "platform")


def test_transfer_reparents_and_keeps_previous_owner_write(monkeypatch):
    calls = {}
    _wire(monkeypatch, owner=("user", "alice"))
    monkeypatch.setattr(ownership, "_kind", lambda rt: ownership.ResourceKind(
        owner_getter=lambda rid: ("user", "alice"),
        reparent=lambda rid, nt, ni: calls.setdefault("reparent", (rid, nt, ni)),
    ))
    monkeypatch.setattr(ownership.db, "revoke_resource_grant",
                        lambda *a: calls.setdefault("revoke", a))
    monkeypatch.setattr(ownership.db, "grant_resource",
                        lambda *a: calls.setdefault("grant", a))
    ownership.transfer(RT, RID, "user", "bob")
    assert calls["reparent"] == (RID, "user", "bob")
    # le nouveau propriétaire perd son éventuel grant
    assert calls["revoke"] == (RT, RID, "user", "bob")
    # l'ancien propriétaire user garde un accès write
    assert calls["grant"] == (RT, RID, "user", "alice", "write")


# --- Transfert d'un PROJET : le « rangé chez » suit le détenteur ---------------
# Régression 2026-07-28 : `reparent_project` ne touchait pas `context_org_id`, or un
# projet perso n'est listé que dans SON org de contexte → transférer un projet d'org à
# une personne le rendait invisible PARTOUT, nouveau propriétaire compris.

def _wire_project(monkeypatch, row, *, member_of=()):
    monkeypatch.setattr(ownership.db, "get_project_by_id", lambda pid: row)
    monkeypatch.setattr(ownership.roles, "is_org_member",
                        lambda sub, oid: oid in member_of)
    monkeypatch.setattr(ownership.org_store, "ensure_home_org", lambda sub: 999)
    seen = {}
    monkeypatch.setattr(ownership.db, "reparent_project",
                        lambda pid, nt, ni, context_org_id=None:
                        seen.update(pid=pid, nt=nt, ni=ni, ctx=context_org_id))
    return seen


def test_project_transfer_to_user_garde_l_org_de_travail(monkeypatch):
    """Org-owned → personne MEMBRE de cette org : le projet reste rangé chez elle."""
    seen = _wire_project(monkeypatch, {"owner_type": "org", "owner_id": "35"}, member_of=(35,))
    ownership._project_reparent("116", "user", "jane")
    assert seen == {"pid": 116, "nt": "user", "ni": "jane", "ctx": 35}


def test_project_transfer_to_outsider_retombe_sur_son_espace_perso(monkeypatch):
    """Destinataire NON membre de l'org d'origine : jamais NULL — son espace perso."""
    seen = _wire_project(monkeypatch, {"owner_type": "org", "owner_id": "35"}, member_of=())
    ownership._project_reparent("116", "user", "etranger")
    assert seen["ctx"] == 999


def test_project_transfer_depuis_une_equipe_prend_l_org_parente(monkeypatch):
    seen = _wire_project(monkeypatch, {"owner_type": "group", "owner_id": "4"}, member_of=(35,))
    monkeypatch.setattr(ownership.group_store, "get_group", lambda gid: {"id": gid, "org_id": 35})
    ownership._project_reparent("22", "user", "bob")
    assert seen["ctx"] == 35


def test_project_transfer_vers_une_org_efface_le_contexte(monkeypatch):
    """Non-perso : le contexte se DÉRIVE de l'owner → la colonne repasse à NULL."""
    seen = _wire_project(monkeypatch, {"owner_type": "user", "owner_id": "clem",
                                       "context_org_id": 35})
    ownership._project_reparent("167", "org", "35")
    assert seen == {"pid": 167, "nt": "org", "ni": "35", "ctx": None}


# --- Transfert vers une ÉQUIPE : même garde pour tous les kinds (seam unique) ----
# Une équipe range DANS une org ; elle ne fait pas changer d'org. La garde vit dans
# `ownership.transfer`, donc couvre aussi la cascade d'un projet livré.

def _kind_stub(monkeypatch, owner, context_org=None):
    calls = []
    # L'ancien propriétaire garde un partage nominatif : sa maison (org perso) existe
    # déjà — sans cette doublure, `ownership.maison_du_partage` la lirait en base.
    monkeypatch.setattr(ownership.org_store, "home_org", lambda sub: 1)
    monkeypatch.setattr(ownership, "_kind", lambda rt: ownership.ResourceKind(
        owner_getter=lambda rid: owner,
        reparent=lambda rid, nt, ni: calls.append((rid, nt, ni)),
        context_org=(lambda rid: context_org) if context_org else None))
    monkeypatch.setattr(ownership.group_store, "org_ids_of_groups",
                        lambda gids: {g: {4: 35, 9: 77}[g] for g in gids if g in (4, 9)})
    monkeypatch.setattr(ownership.db, "revoke_resource_grant", lambda *a: False)
    monkeypatch.setattr(ownership.db, "grant_resource", lambda *a, **k: None)
    return calls


@pytest.mark.parametrize("owner,ctx", [(("org", "35"), None), (("group", "4"), None),
                                       (("user", "u1"), 35)])
def test_transfer_group_autre_org_refuse(monkeypatch, owner, ctx):
    calls = _kind_stub(monkeypatch, owner, ctx)
    with pytest.raises(ownership.GroupOutsideResourceOrg):
        ownership.transfer("project", "7", "group", "9")
    assert calls == []


@pytest.mark.parametrize("owner,ctx", [(("org", "35"), None), (("group", "4"), None),
                                       (("user", "u1"), 35),
                                       # org inconnue (tableau perso) ou nulle (plateforme) :
                                       # rien à comparer, l'équipe de l'acteur fait foi.
                                       (("user", "u1"), None), (("platform", "platform"), None)])
def test_transfer_group_meme_org_ou_org_inconnue_passe(monkeypatch, owner, ctx):
    calls = _kind_stub(monkeypatch, owner, ctx)
    target = "9" if owner[0] in ("platform",) or (owner[0] == "user" and ctx is None) else "4"
    ownership.transfer("datastore_namespace", "7", "group", target)
    assert calls == [("7", "group", target)]


def test_transfer_group_inconnu_leve(monkeypatch):
    _kind_stub(monkeypatch, ("org", "35"))
    with pytest.raises(ValueError, match="inconnue"):
        ownership.transfer("doctrine", "7", "group", "404")


def test_le_kind_project_lit_son_org_de_rangement(monkeypatch):
    monkeypatch.setattr(ownership.db, "get_project_by_id",
                        lambda pid: {"owner_type": "user", "owner_id": "u1", "context_org_id": 35})
    assert ownership.RESOURCE_KINDS["project"].context_org("7") == 35


# --- ADR 0049 : cran PLATFORM (bibliothèque) + visibilité contextuelle group ----

def test_platform_owned_readable_by_all_but_not_writable(monkeypatch):
    """Un projet bibliothèque (platform-owned) se lit par tout utilisateur authentifié
    (fait pour être copié) ; l'écriture et la gouvernance restent plateforme."""
    _wire(monkeypatch, owner=("platform", "platform"))
    assert ownership.can_access("anyone", RT, RID, "read")
    assert not ownership.can_access("anyone", RT, RID, "write")
    assert not ownership.can_govern("anyone", RT, RID)
    assert not ownership.can_transfer("anyone", RT, RID)


def test_platform_owned_admin_writes_governs_transfers(monkeypatch):
    _wire(monkeypatch, owner=("platform", "platform"), super_admin=True)
    assert ownership.can_access("ops", RT, RID, "write")
    assert ownership.can_govern("ops", RT, RID)
    assert ownership.can_transfer("ops", RT, RID)


def _wire_groups(monkeypatch, parent_org=42):
    monkeypatch.setattr(ownership.group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": parent_org})
    monkeypatch.setattr(ownership.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])


def test_visible_in_org_group_owned_member(monkeypatch):
    """Un projet de pôle appartient au contexte de son org PARENTE : visible pour un
    lecteur du groupe dans cette org, invisible depuis une autre org."""
    _wire(monkeypatch, owner=("group", "5"), group_read_of={5})
    _wire_groups(monkeypatch, parent_org=42)
    assert ownership.visible_in_org("m", 42, RT, RID)
    assert not ownership.visible_in_org("m", 43, RT, RID)


def test_visible_in_org_group_owned_non_member_denied(monkeypatch):
    _wire(monkeypatch, owner=("group", "5"))
    _wire_groups(monkeypatch, parent_org=42)
    assert not ownership.visible_in_org("x", 42, RT, RID)


def test_visible_in_org_group_owned_org_admin_reads(monkeypatch):
    # Gouvernance inaliénable : l'org_admin du parent lit le projet du pôle
    # (can_read_group escalade) — pas de cran « caché aux admins » (ADR 0049 §C).
    _wire(monkeypatch, owner=("group", "5"), group_admin_of={5})
    _wire_groups(monkeypatch, parent_org=42)
    assert ownership.visible_in_org("adm", 42, RT, RID)


def test_visible_in_org_platform_everywhere(monkeypatch):
    _wire(monkeypatch, owner=("platform", "platform"))
    _wire_groups(monkeypatch)
    assert ownership.visible_in_org("anyone", 42, RT, RID)
    assert ownership.visible_in_org("anyone", 7, RT, RID)


# --- L'appartenance d'un tableau : lue sans son schéma (#1108) -------------------
# `owner_of` est appelé à chaque contrôle d'accès ; il relisait la ligne entière, `schema`
# jsonb compris, et la décodait — 8 à 14 fils à la fois dans les gels relevés.

def _lecture_d_appartenance(monkeypatch, row):
    lus = []
    monkeypatch.setattr(ownership.db, "get_datastore_owner",
                        lambda ns_id: lus.append(ns_id) or row)
    monkeypatch.setattr(ownership.db, "get_datastore_by_id", lambda ns_id: pytest.fail(
        "l'appartenance relit la ligne entière, schéma compris"))
    return lus


@pytest.mark.parametrize("row,attendu", [
    ({"owner_type": "org", "owner_id": "35"}, ("org", "35")),
    ({"owner_type": "user", "owner_id": "u1"}, ("user", "u1")),
    ({"owner_type": "group", "owner_id": "4"}, ("group", "4")),
    (None, None),                                       # tableau introuvable
    ({"owner_type": "user", "owner_id": None}, None),   # propriétaire nul
])
def test_datastore_owner_ne_lit_que_l_appartenance(monkeypatch, row, attendu):
    lus = _lecture_d_appartenance(monkeypatch, row)
    assert ownership.owner_of(ownership.TYPE_RESSOURCE_DATASTORE, "7") == attendu
    assert lus == [7]


def test_la_lecture_d_appartenance_ne_selectionne_pas_le_schema(monkeypatch):
    from contextlib import contextmanager

    from oto_mcp.db import datastore_ns
    requetes = []

    class _Conn:
        def execute(self, sql, params):
            requetes.append((sql, params))
            return self

        def fetchone(self):
            return {"owner_type": "org", "owner_id": "35"}

    @contextmanager
    def _connect():
        yield _Conn()

    monkeypatch.setattr(datastore_ns, "_connect", _connect)
    assert datastore_ns.get_datastore_owner(7) == {"owner_type": "org", "owner_id": "35"}
    [(sql, params)] = requetes
    assert params == (7,)
    selection = sql.split("FROM")[0]
    assert "owner_type" in selection and "owner_id" in selection
    assert "schema" not in selection and "*" not in selection
