"""Le PARTAGE EN ATTENTE : partager un objet avec une adresse qui n'a pas encore de compte.

Décision d'Alexis du 29/09/2026, après un incident : une personne voulait partager UN
projet avec une collègue sans compte ; le partage rendait « utilisateur inconnu », et
la seule issue restante était d'inviter la collègue dans l'ORG — qui lui ouvrait tout ce
que l'org possède. Désormais, le partage reste en attente : un mail, et à l'inscription
l'accès à CET objet, jamais une adhésion.

Contre un PostgreSQL réel : ce qu'on vérifie, c'est ce que la base porte et ce qu'elle
ouvre — un simulacre rendrait ce qu'on lui a appris à rendre.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def monde(live, monkeypatch):
    """L'org A : son admin (propriétaire d'un projet de l'org et d'un second projet
    que personne ne partage). L'invitée n'a PAS de compte."""
    from oto_mcp import db, org_store
    from oto_mcp.auth import annuaire
    from oto_mcp.capabilities import resources as R
    monkeypatch.setenv("OTO_INVITE_BASE_URL", "https://front.example.test")
    monkeypatch.setattr(annuaire, "magic_url", lambda url, email, **_: url)
    envois: list[dict] = []
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda to, **kw: envois.append({"to": to, **kw}) or True)
    u = uuid.uuid4().hex[:8]
    owner = f"owner_{u}"
    db.upsert_user(owner, email=f"{owner}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=owner)
    org_store.add_org_member(org_a, owner, "org_admin")
    pid = db.create_project("org", str(org_a), f"p_{u}", created_by=owner)
    autre = db.create_project("org", str(org_a), f"secret_{u}", created_by=owner)
    return {"owner": owner, "org_a": org_a, "pid": pid, "autre": autre,
            "invitee": f"kenza_{u}@example.test", "envois": envois, "u": u}


def _geste(m: dict, op: str, *, sub: str | None = None, **kw):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    return R._resources(ResolvedCtx(sub=sub or m["owner"], org_id=m["org_a"]),
                        R.ResourceInput(op=op, resource_type="project",
                                        resource_id=str(m["pid"]), **kw))


def _inscrire(m: dict, email: str | None = None) -> str:
    from oto_mcp import db
    sub = f"new_{m['u']}_{uuid.uuid4().hex[:4]}"
    db.upsert_user(sub, email=email or m["invitee"])
    return sub


def _lignes(m: dict) -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM org_invitations WHERE resource_id = %s", (str(m["pid"]),))]


def test_partager_vers_une_adresse_inconnue_ne_rend_plus_404(monde):
    m = monde
    r = _geste(m, "share", email=m["invitee"], role="editor")
    assert r["pending"] is True and r["already_pending"] is False
    assert r["shared_with"] == m["invitee"] and r["role"] == "editor"
    assert "rien d'autre" in r["pending_note"]
    assert r["notified"] is True
    (envoi,) = m["envois"]
    assert envoi["to"] == m["invitee"]
    assert envoi["app_url"].startswith("https://front.example.test/invitation/inv_")
    (ligne,) = _lignes(m)
    assert ligne["org_id"] is None and ligne["resource_kind"] == "project"
    assert ligne["token_hash"] and "inv_" not in ligne["token_hash"], "seul le hash"


def test_a_l_inscription_l_acces_a_l_objet_et_pas_a_l_org(monde):
    from oto_mcp import org_store, ownership, roles
    m = monde
    _geste(m, "share", email=m["invitee"], role="viewer")
    sub = _inscrire(m)
    assert ownership.can_access(sub, "project", str(m["pid"]))
    assert not roles.is_org_member(sub, m["org_a"]), "jamais une adhésion"
    assert not ownership.can_access(sub, "project", str(m["autre"])), \
        "aucun autre objet de l'org"
    (ligne,) = _lignes(m)
    assert ligne["accepted_sub"] == sub
    grants = _geste(m, "get")["grants"]
    assert [g for g in grants if g.get("principal_id") == sub and not g.get("pending")]
    assert not [g for g in grants if g.get("pending")], "consommé : plus en attente"


def test_accepter_par_le_lien_donne_l_objet_seul(monde):
    """L'invitée crée son compte avec une AUTRE adresse, puis ouvre le lien du mail."""
    from oto_mcp import org_store, ownership, roles
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import invites
    m = monde
    _geste(m, "share", email=m["invitee"], role="viewer")
    token = m["envois"][0]["app_url"].rsplit("/", 1)[1]
    sub = _inscrire(m, email=f"autre_{m['u']}@example.test")
    assert not ownership.can_access(sub, "project", str(m["pid"]))
    r = invites._invite_accept(ResolvedCtx(sub=sub, org_id=None),
                               invites.InviteAcceptInput(token=token))
    assert r["resource_type"] == "project" and r["resource_id"] == str(m["pid"])
    assert r.get("org_id") is None
    assert ownership.can_access(sub, "project", str(m["pid"]))
    assert not roles.is_org_member(sub, m["org_a"])
    again = invites._invite_accept(ResolvedCtx(sub=sub, org_id=None),
                                   invites.InviteAcceptInput(token=token))
    assert again["resource_id"] == str(m["pid"]), "ré-accepter est idempotent"
    apercu = org_store.preview_invitation(token)
    assert apercu is None, "consommée : plus d'aperçu"


def test_l_apercu_public_dit_l_objet(monde):
    from oto_mcp import org_store
    m = monde
    _geste(m, "share", email=m["invitee"], role="viewer")
    token = m["envois"][0]["app_url"].rsplit("/", 1)[1]
    p = org_store.preview_invitation(token)
    assert p["scope"] == "resource" and p["resource_type"] == "project"
    assert p["resource_name"] == f"p_{m['u']}" and p["org_name"] is None


def test_un_doublon_ne_cree_pas_deux_invitations(monde):
    m = monde
    _geste(m, "share", email=m["invitee"], role="viewer")
    r = _geste(m, "share", email=m["invitee"].upper(), role="viewer")
    assert r["pending"] is True and r["already_pending"] is True
    assert r["notified"] is False, "pas de second lien, donc pas de second mail"
    assert len(_lignes(m)) == 1 and len(m["envois"]) == 1


def test_la_vue_montre_le_partage_en_attente_et_unshare_le_retire(monde):
    from oto_mcp import ownership
    m = monde
    _geste(m, "share", email=m["invitee"], role="editor")
    (g,) = [g for g in _geste(m, "get")["grants"] if g.get("pending")]
    assert g["email"] == m["invitee"] and g["role"] == "editor"
    assert g["principal_id"] is None and g["invitation_expires_at"]
    r = _geste(m, "unshare", email=m["invitee"])
    assert r["removed"] is True
    assert not [g for g in _geste(m, "get")["grants"] if g.get("pending")]
    sub = _inscrire(m)
    assert not ownership.can_access(sub, "project", str(m["pid"])), \
        "retiré avant l'inscription : rien n'est accordé"


def test_un_partage_caduc_ne_donne_rien(monde):
    """L'émetteur a perdu la main sur l'objet entre le partage et l'inscription."""
    from oto_mcp import org_store, ownership, roles
    m = monde
    _geste(m, "share", email=m["invitee"], role="viewer")
    org_store.remove_org_member(m["org_a"], m["owner"])
    assert not ownership.can_govern(m["owner"], "project", str(m["pid"]))
    sub = _inscrire(m)
    assert not ownership.can_access(sub, "project", str(m["pid"]))


def test_la_cascade_attend_l_inscription(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    with pytest.raises(AuthzDenied) as e:
        _geste(m, "share", email=m["invitee"], role="viewer", cascade=True)
    assert e.value.code == "pending_share_plain_only"
    assert not _lignes(m)


def test_le_jeton_du_lien_est_masque_au_journal():
    """Le lien d'un partage en attente est un lien d'invitation : son aperçu public
    passe par `/api/invitations/{token}`, déjà déclarée secrète."""
    from oto_mcp import journal_secrets
    from oto_mcp.api import routes
    routes.make_routes(object(), mcp_instance=None)  # déclare la table servie
    chemin = "/api/invitations/inv_" + "a" * 43
    route, masques = journal_secrets.route_and_secrets(chemin)
    assert "inv_" + "a" * 43 not in route
