"""Livraison d'un projet complet vers l'org d'un client (#52).

Trois pièces, testées au grain unitaire (style monkeypatch maison) :
- `oto_resource` : partage à une ORG (`org_id`) + `cascade=true` sur share/transfer
  d'un projet — tableaux suivent le geste, procédures grantées read (share) ou
  copiées + lien re-pointé (transfer), connecteurs rapportés `recipient_credential`.
- kind `guide` du seam ownership (owner DÉRIVÉ d'org_id).
- `oto_get_doctrine(doctrine_id=…)` : lecture par id honorant les grants (le chemin
  de consommation cross-org du client).
"""
import asyncio

import pytest

from oto_mcp import ownership
from oto_mcp.capabilities.orgs import instructions as oi
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="oto", org_id=1)

LINKS = [
    {"target_type": "tableau", "target_ref": "11", "label": "leads"},
    {"target_type": "tableau", "target_ref": "12", "label": "hors périmètre"},
    {"target_type": "procedure", "target_ref": "77", "label": "process mutuelle"},
    {"target_type": "connecteur", "target_ref": "unipile", "label": None},
]


def _wire(monkeypatch, *, governed=("11", "77")):
    """Câble un projet #7 lié aux entités LINKS ; l'acteur gouverne `governed`."""
    calls = {"grants": [], "transfers": [], "revokes": [], "copies": [], "repoints": []}
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: {"name": "acme"})
    monkeypatch.setattr(R.roles, "is_org_member", lambda sub, oid: True)
    monkeypatch.setattr(R.db, "list_project_links", lambda pid: list(LINKS))
    monkeypatch.setattr(R.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(R.ownership, "can_govern",
                        lambda sub, rt, rid: rid in governed)
    # transfert re-gardé (ADR 0048) : le projet #7 est transférable par l'acteur.
    monkeypatch.setattr(R.ownership, "can_transfer", lambda sub, rt, rid: True)
    # Garde-fou anti-lockout hors scope de ces tests cascade (testé dans test_resources_project) :
    # l'acteur retient toujours le contrôle → pas de confirmation exigée.
    monkeypatch.setattr(R.ownership, "would_retain_control", lambda sub, ot, oid: True)
    # ADR 0048 : grant est désormais keyé par RÔLE (viewer/editor/manager) ; on
    # enregistre la permission dérivée pour garder les assertions read/write lisibles.
    def _grant(rt, rid, pt, pid, perm=None, granted_by=None, role=None):
        eff = perm or {"viewer": "read", "editor": "write", "manager": "write"}.get(role, "write")
        calls["grants"].append((rt, rid, pt, pid, eff))
    monkeypatch.setattr(R.ownership, "grant", _grant)
    monkeypatch.setattr(R.ownership, "transfer",
                        lambda rt, rid, ot, oid: calls["transfers"].append((rt, rid, ot, oid)))
    monkeypatch.setattr(R.ownership, "revoke",
                        lambda rt, rid, pt, pid: calls["revokes"].append((rt, rid, pt, pid)) or True)
    monkeypatch.setattr(R.org_store, "copy_instruction_to_owner",
                        # ⚠️ `oid` reste du TEXTE : depuis l'ADR 0068 la cible peut être
                        # une PERSONNE, dont l'identifiant est un `sub`. L'`int()` qui
                        # vivait ici levait un ValueError que la cascade attrapait et
                        # rendait comme « raison » de l'entrée — un refus fabriqué par
                        # le banc, pas par le code qu'il teste.
                        lambda iid, otype, oid, set_by=None:
                        calls["copies"].append((iid, oid))
                        or {"id": 501, "slug": "process-mutuelle", "owner_type": otype,
                            "owner_id": str(oid),
                            # ⚠️ NULL pour une PERSONNE : `org_id` porte l'org PARENTE
                            # et la cascade de suppression, et personne n'en a une
                            # (ADR 0068). Le stub le reflète, sinon il décrit une
                            # ligne que le store refuserait d'écrire.
                            "org_id": None if otype == "user" else int(oid)})
    monkeypatch.setattr(R.db, "update_project_link_ref",
                        lambda pid, t, old, new: calls["repoints"].append((pid, t, old, new)) or 1)
    return calls


# ── oto_resource : partage à une org ─────────────────────────────────────────

def test_share_to_org_principal(monkeypatch):
    """⚠️ Le droit attendu est `read` depuis l'ADR 0068 : partager sans préciser ne
    donne plus l'écriture. Ce banc enregistrait `write` — le défaut « rétro-compat »
    que le code annonçait lui-même comme un héritage, jamais comme une intention."""
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert ("project", "7", "org", "35", "read") in calls["grants"]
    # Et l'écriture reste accessible, en la demandant.
    R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                      resource_id="7", org_id=35, permission="write"))
    assert ("project", "7", "org", "35", "write") in calls["grants"]
    assert out["shared_with"] == "acme" and out["principal_type"] == "org"


def test_share_to_unknown_org_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: None)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                          resource_id="7", org_id=99))
    assert e.value.code == "unknown_org"


def test_share_to_user_still_works(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email", lambda e: [{"sub": "u2", "email": e}])
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "sharer@x.co"})
    monkeypatch.setattr(R.db, "get_project_by_id", lambda pid: {"name": "Campagne mutuelle"})
    sent = {}
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda to, **kw: sent.update({"to": to, **kw}) or True)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", email="jane@x.co"))
    assert ("project", "7", "user", "u2", "read") in calls["grants"]
    assert out["principal_type"] == "user"
    # Le bénéficiaire user est notifié par email (best-effort, une seule fois).
    assert out["notified"] is True
    assert sent["to"] == "jane@x.co" and sent["type_label"] == "projet"
    # Le mail dit le droit RÉELLEMENT accordé — `read` depuis l'ADR 0068. Un mail qui
    # annoncerait l'écriture sur un grant en lecture ferait chercher un bug ailleurs.
    assert sent["name"] == "Campagne mutuelle" and sent["permission"] == "read"


def test_share_to_user_passes_recipient_locale_and_english_label(monkeypatch):
    """oto-backend#700 : la préférence `users.locale` du bénéficiaire suit
    jusqu'au gabarit, ET le `type_label` passé est déjà dans SA langue — le
    gabarit ne traduit pas un mot qu'on lui donne."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email",
                        lambda e: [{"sub": "u2", "email": e, "locale": "en"}])
    # ⚠️ La doublure DISTINGUE les deux comptes : la notification lit désormais le
    # destinataire par son `sub` (celui du grant qu'on vient de poser) au lieu de
    # le re-chercher par adresse — une adresse peut désigner deux comptes, et on
    # prendrait la langue du mauvais. Une doublure qui rend le même compte pour
    # tout sub masquerait précisément ce que ce banc mesure.
    monkeypatch.setattr(R.db, "get_user",
                        lambda sub: ({"sub": "u2", "email": "dest@x.co", "locale": "en"}
                                     if sub == "u2" else {"email": "sharer@x.co"}))
    monkeypatch.setattr(R.db, "get_project_by_id", lambda pid: {"name": "Campagne mutuelle"})
    sent = {}
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda to, **kw: sent.update({"to": to, **kw}) or True)
    R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                      resource_id="7", email="jane@x.co"))
    assert sent["locale"] == "en"
    assert sent["type_label"] == "project"   # pas "projet" : la langue du destinataire


def test_transfer_to_user_emails_new_owner(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email", lambda e: [{"sub": "u2", "email": e}])
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "sharer@x.co"})
    monkeypatch.setattr(R.db, "get_project_by_id", lambda pid: {"name": "Campagne mutuelle"})
    sent = {}
    monkeypatch.setattr(R.email, "send_resource_transferred_email",
                        lambda to, **kw: sent.update({"to": to, **kw}) or True)
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_email="jane@x.co"))
    assert out["new_owner"] == "jane@x.co" and out["notified"] is True
    assert sent["to"] == "jane@x.co" and sent["name"] == "Campagne mutuelle"


def test_share_to_org_does_not_email(monkeypatch):
    """Partage à une ORG : pas de notif user (qui reçoit reste à trancher, #77)."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda *a, **k: pytest.fail("ne doit pas notifier une org"))
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert out["principal_type"] == "org" and "notified" not in out


# ── cascade au PARTAGE (modèle licence : oto garde l'ownership) ──────────────

def test_share_cascade_carries_linked_entities(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35,
                                            permission="write", cascade=True))
    # tableau gouverné → même geste, même permission ; guide → READ toujours.
    assert ("datastore_namespace", "11", "org", "35", "write") in calls["grants"]
    assert ("doctrine", "77", "org", "35", "read") in calls["grants"]
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("tableau", "11")]["status"] == "shared"
    assert by_ref[("tableau", "12")] == {"target_type": "tableau", "target_ref": "12",
                                         "label": "hors périmètre", "status": "skipped",
                                         "reason": "not_governed"}
    assert by_ref[("procedure", "77")]["permission"] == "read"
    assert by_ref[("connecteur", "unipile")]["status"] == "action_required"


def test_share_without_cascade_touches_nothing_linked(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert "cascade" not in out
    assert all(g[0] == "project" for g in calls["grants"])


# ── cascade au TRANSFERT (remise des clés) ────────────────────────────────────

def test_transfer_cascade_to_org(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_org=35,
                                            cascade=True))
    assert ("project", "7", "org", "35") in calls["transfers"]
    assert ("datastore_namespace", "11", "org", "35") in calls["transfers"]
    # procédure : COPIÉE chez la cible (l'originale reste), lien re-pointé sur la copie.
    assert calls["copies"] == [(77, "35")]
    assert calls["repoints"] == [(7, "procedure", "77", "501")]
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("procedure", "77")]["status"] == "copied"
    assert by_ref[("procedure", "77")]["new_ref"] == "501"


def test_transfer_cascade_vers_une_PERSONNE_copie_la_procedure(monkeypatch):
    """⚠️ Ce banc s'appelait `…_skips_guide` et vérifiait que la cascade SAUTAIT la
    procédure quand le destinataire est une personne — parce que le palier personnel
    n'existait pas (`doctrine_needs_org_owner`). L'ADR 0068 l'a ouvert : la procédure
    suit désormais le projet chez son nouveau propriétaire, comme elle le fait déjà
    vers une org ou une équipe.

    C'est le sens du transfert : livrer un projet sans sa procédure livrait un mode
    d'emploi manquant, et le rapport de cascade le disait sans que personne puisse y
    remédier."""
    calls = _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email", lambda e: [{"sub": "u2", "email": e}])
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_email="jane@x.co",
                                            cascade=True))
    assert calls["copies"] == [(77, "u2")], "la procédure est copiée chez la personne"
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("procedure", "77")]["status"] == "copied"


def test_cascade_entity_failure_does_not_break_delivery(monkeypatch):
    calls = _wire(monkeypatch)

    def _boom(rt, rid, pt, pid, perm=None, granted_by=None, role=None):
        if rt == "datastore_namespace":
            raise RuntimeError("pg down")
        eff = perm or {"viewer": "read", "editor": "write", "manager": "write"}.get(role, "write")
        calls["grants"].append((rt, rid, pt, pid, eff))
    monkeypatch.setattr(R.ownership, "grant", _boom)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35, cascade=True))
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("tableau", "11")]["status"] == "failed"
    assert by_ref[("procedure", "77")]["status"] == "shared"   # la suite continue


# ── cascade à la RÉVOCATION ───────────────────────────────────────────────────

def test_unshare_cascade_revokes_linked(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="unshare", resource_type="project",
                                            resource_id="7", org_id=35, cascade=True))
    assert ("project", "7", "org", "35") in calls["revokes"]
    assert ("datastore_namespace", "11", "org", "35") in calls["revokes"]
    assert ("doctrine", "77", "org", "35") in calls["revokes"]
    assert {(e["target_type"], e["target_ref"]) for e in out["cascade"]} == \
        {("tableau", "11"), ("procedure", "77")}


# ── kind `guide` (ownership : le propriétaire est LU, plus dérivé d'org_id) ──

def test_guide_owner_lit_les_colonnes_de_propriete(monkeypatch):
    """#681 : `owner_type`/`owner_id` font foi. Dériver d'`org_id` rendait ('org', 42)
    pour une procédure d'ÉQUIPE — 42 n'en est que l'org parente."""
    monkeypatch.setattr(ownership.org_store, "get_instruction_by_id",
                        lambda i: {"id": 77, "org_id": 42, "owner_type": "org",
                                   "owner_id": "42", "slug": "process"})
    assert ownership.owner_of("doctrine", "77") == ("org", "42")
    monkeypatch.setattr(ownership.org_store, "get_instruction_by_id",
                        lambda i: {"id": 77, "org_id": 42, "owner_type": "group",
                                   "owner_id": "9", "slug": "process"})
    assert ownership.owner_of("doctrine", "77") == ("group", "9")


def test_guide_owner_none_for_slug_ref():
    assert ownership.owner_of("doctrine", "vieux-slug") is None


def test_guide_reparent_refuse_un_palier_INCONNU():
    """⚠️ Ce banc gardait la fermeture du palier PERSONNEL, et sa raison était juste :
    `org_instructions.org_id` était NOT NULL. La phase 2 de #681 l'a levée (ADR 0068,
    « procédure doit pouvoir être privée ») — `user` est désormais un palier ouvert.

    Ce qu'il reste à garder, et c'est ce que ce banc vérifie maintenant : ouvrir `user`
    n'ouvre pas TOUT. Un palier hors liste est refusé en nommant les trois qui
    existent, plutôt que de retomber silencieusement sur l'org."""
    with pytest.raises(ValueError) as e:
        ownership._guide_reparent("77", "team", "42")
    msg = str(e.value)
    assert "org" in msg and "group" in msg and "user" in msg


def test_guide_listed_in_resource_ops():
    assert "doctrine" in R._OPS


# ── oto_get_doctrine(doctrine_id) : lecture par id + grants ──────────────────

def _wire_guide_read(monkeypatch, *, can_access):
    monkeypatch.setattr(oi.org_store, "get_instruction_by_id",
                        lambda i: {"id": 77, "org_id": 42, "owner_type": "org",
                                   "owner_id": "42", "slug": "process",
                                   "title": "T", "description": "d", "version": 3,
                                   "body_md": "corps"} if i == 77 else None)
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rt, rid, want="read": can_access)

    async def _manifest(*a, **k):
        return []
    monkeypatch.setattr(oi.tool_registry, "manifest_for", _manifest)


def test_get_guide_by_id_with_grant(monkeypatch):
    _wire_guide_read(monkeypatch, can_access=True)
    out = asyncio.run(oi._get_guide(ResolvedCtx(sub="client", org_id=35),
                                       oi.GuideGetInput(doctrine_id=77)))
    # l'org_id rendu = l'org PROPRIÉTAIRE du guide, pas l'org active du lecteur
    assert out["org_id"] == 42 and out["doctrine_id"] == 77
    assert out["slug"] == "process" and out["body_md"] == "corps"


def test_get_guide_by_id_denied_without_grant(monkeypatch):
    _wire_guide_read(monkeypatch, can_access=False)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(oi._get_guide(ResolvedCtx(sub="intrus", org_id=9),
                                     oi.GuideGetInput(doctrine_id=77)))
    assert e.value.status == 403


def test_get_guide_by_id_unknown_404(monkeypatch):
    _wire_guide_read(monkeypatch, can_access=True)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(oi._get_guide(ResolvedCtx(sub="client", org_id=35),
                                     oi.GuideGetInput(guide_id=999)))
    # Code d'aujourd'hui, code d'hier conservé dans `details` : un code d'erreur ne
    # se double pas (il n'y a qu'un champ `error`), donc l'ancien se lit là (#519).
    assert e.value.code == "unknown_guide"
    assert (e.value.details or {}).get("legacy_code") == "unknown_doctrine"
