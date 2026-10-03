"""Le partage d'UNE procédure par lien (« See who's reading »), contre un PostgreSQL réel.

Ce qu'on vérifie, c'est ce que la base porte et ce que chaque face SERT — en premier
lieu ce qu'elle ne sert PAS : la vitrine anonyme ne rend jamais le corps ni un texte
d'étape (oto#84), et la forme du graphe n'accepte aucun texte libre.
"""
from __future__ import annotations

import json
import uuid

import pytest

_CORPS = ("# Relance\n\nÉtape secrète : écrire au client avec <tool:serper_search>.\n"
          "Deuxième phrase confidentielle.")
_FORME = {"v": 1, "trigger": {"kind": "schedule"},
          "steps": [{"kind": "ai", "connectors": ["hubspot"]},
                    {"kind": "human", "connectors": ["slack"]},
                    {"kind": "check", "connectors": []}]}


@pytest.fixture
def monde(live, monkeypatch):
    """L'org A et son admin, propriétaire d'une procédure ; un membre simple de A ;
    deux lecteurs extérieurs (leur org perso naît à l'inscription)."""
    from oto_mcp import db, org_store
    monkeypatch.setenv("OTO_APP_URL", "https://front.example.test")
    u = uuid.uuid4().hex[:8]
    owner, membre = f"owner_{u}", f"membre_{u}"
    db.upsert_user(owner, email=f"{owner}@northwind.example", name="Julie Martin")
    db.upsert_user(membre, email=f"{membre}@northwind.example", name="Léa")
    org_a = org_store.create_org(f"Northwind {u}", created_by=owner)
    org_store.add_org_member(org_a, owner, "org_admin")
    org_store.add_org_member(org_a, membre, "org_member")
    org_store.set_instruction("org", org_a, "lost-deal-revival", _CORPS,
                              title="Lost deal revival", description="Revive lost deals.",
                              set_by=owner)
    lecteurs = []
    for i, email in enumerate((f"marie_{u}@acme.example", f"tom_{u}@gmail.com")):
        sub = f"lecteur{i}_{u}"
        db.upsert_user(sub, email=email, name=f"Lecteur {i}")
        lecteurs.append(sub)
    return {"owner": owner, "membre": membre, "org_a": org_a, "lecteurs": lecteurs,
            "slug": "lost-deal-revival", "u": u}


def _ctx(sub, org_id=None, channel="rest"):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=org_id, channel=channel)


def _ecrire(m, op, **kw):
    from oto_mcp.capabilities import partages_procedure as P
    return P._share_write(_ctx(m["owner"], m["org_a"]),
                          P.ShareWriteInput(slug=m["slug"], op=op, **kw))


def _publier(m, **kw):
    return _ecrire(m, "publish", **kw)["share"]


def _lire(m, sub, token):
    from oto_mcp.capabilities import partages_procedure as P
    return P._read(_ctx(sub), P.TokenInput(token=token))


def _copier(m, sub, token):
    from oto_mcp.capabilities import partages_procedure as P
    return P._copy(_ctx(sub), P.TokenInput(token=token))


def _lecteurs(m):
    from oto_mcp.capabilities import partages_procedure as P
    return P._share_readers(_ctx(m["owner"], m["org_a"]),
                            P.ShareSlugInput(slug=m["slug"]))


def _client():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from oto_mcp.api import public
    return TestClient(Starlette(routes=[
        Route("/api/public/process-shares/{token}", public.process_share_preview),
        Route("/o/r/{token}", public.readers_digest_unsubscribe)]))


# ── Propriétaire ───────────────────────────────────────────────────────────────

def test_publier_est_idempotent_et_le_lien_vit_sur_le_front(monde):
    m = monde
    a = _publier(m)
    b = _publier(m)
    assert a["token"] == b["token"] and len(a["token"]) >= 10
    assert a["url"] == f"https://front.example.test/p/{a['token']}"
    assert a["show_readers"] is True and a["shape_fresh"] is False


def test_retirer_puis_republier_change_de_jeton_et_l_ancien_meurt(monde):
    from oto_mcp.capabilities import partages_procedure as P
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    ancien = _publier(m)["token"]
    assert _ecrire(m, "unpublish") == {"share": None}
    assert P._share_get(_ctx(m["owner"], m["org_a"]),
                        P.ShareSlugInput(slug=m["slug"])) == {"share": None}
    for geste in (_lire, _copier):
        with pytest.raises(AuthzDenied) as e:
            geste(m, m["lecteurs"][0], ancien)
        assert e.value.code == "not_found"
    assert _client().get(f"/api/public/process-shares/{ancien}").status_code == 404
    assert _publier(m)["token"] != ancien


def test_regler_sans_partage_est_un_404_nomme(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _ecrire(monde, "set", show_readers=False)
    assert e.value.code == "no_share"


def test_seul_l_admin_de_l_org_regle_le_partage(monde):
    from oto_mcp.capabilities._authz import capacite_autorise
    m = monde
    for cle in ("org.instruction.share.get", "org.instruction.share.write",
                "org.instruction.share.readers"):
        assert capacite_autorise(cle, m["owner"], org=m["org_a"])
        assert not capacite_autorise(cle, m["membre"], org=m["org_a"])
        assert not capacite_autorise(cle, m["lecteurs"][0], org=m["org_a"])


def test_un_agent_ne_publie_pas_mais_peut_retirer(monde):
    from oto_mcp.capabilities import procedure_console as C
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    ctx = _ctx(m["owner"], m["org_a"], channel="mcp")
    with pytest.raises(AuthzDenied) as e:
        C._dispatch_procedure(ctx, C.ProcedureInput(op="share_publish", slug=m["slug"]))
    assert e.value.code == "publication_reservee_a_l_humain"
    _publier(m)
    assert C._dispatch_procedure(ctx, C.ProcedureInput(
        op="share_get", slug=m["slug"]))["share"]["token"]
    assert C._dispatch_procedure(ctx, C.ProcedureInput(
        op="share_unpublish", slug=m["slug"])) == {"share": None}


# ── La forme du graphe : un schéma FERMÉ ───────────────────────────────────────

@pytest.mark.parametrize("forme", [
    "pas un objet",
    {"v": 1, "trigger": None, "steps": [{"kind": "ai"}], "titre": "texte"},
    {"v": 2, "trigger": None, "steps": [{"kind": "ai"}]},
    {"v": True, "trigger": None, "steps": [{"kind": "ai"}]},
    {"v": 1, "trigger": {"kind": "every monday"}, "steps": [{"kind": "ai"}]},
    {"v": 1, "trigger": {"kind": "chat", "label": "x"}, "steps": [{"kind": "ai"}]},
    {"v": 1, "trigger": None, "steps": []},
    {"v": 1, "trigger": None, "steps": [{"kind": "ai"}] * 31},
    {"v": 1, "trigger": None, "steps": [{"kind": "AI finds deals"}]},
    {"v": 1, "trigger": None, "steps": [{"kind": "ai", "title": "Find deals"}]},
    {"v": 1, "trigger": None, "steps": [{"kind": "ai", "connectors": ["find the deals"]}]},
    {"v": 1, "trigger": None, "steps": [{"kind": "ai", "connectors": ["a", "b", "c", "d", "e"]}]},
    {"v": 1, "trigger": None, "steps": [{"kind": "ai", "connectors": [3]}]},
])
def test_la_forme_refuse_tout_texte_libre(forme):
    from oto_mcp.capabilities import partages_procedure as P
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        P.valider_forme(forme)
    assert e.value.code == "invalid_preview_shape" and e.value.status == 422


def test_une_forme_sans_sa_version_est_refusee(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _publier(monde, preview_shape=_FORME)
    assert e.value.code == "invalid_preview_shape"


# ── La vitrine anonyme ─────────────────────────────────────────────────────────

def test_la_vitrine_ne_sert_jamais_le_corps(monde):
    from oto_mcp import org_store
    m = monde
    version = org_store.get_instruction("org", m["org_a"], m["slug"])["version"]
    token = _publier(m, preview_shape=_FORME, shape_version=version)["token"]
    r = _client().get(f"/api/public/process-shares/{token}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    brut = r.text
    assert "secrète" not in brut and "confidentielle" not in brut and "body_md" not in brut
    v = r.json()
    assert v["title"] == "Lost deal revival" and v["description"] == "Revive lost deals."
    assert v["author"] == {"name": "Julie Martin", "org_name": f"Northwind {m['u']}"}
    assert v["preview_shape"] == _FORME and v["step_count"] == 3
    assert v["connectors"][:2] == ["hubspot", "slack"]
    assert v["reader_count"] is None


def test_une_forme_perimee_n_est_plus_servie(monde):
    from oto_mcp import org_store
    m = monde
    version = org_store.get_instruction("org", m["org_a"], m["slug"])["version"]
    token = _publier(m, preview_shape=_FORME, shape_version=version)["token"]
    org_store.set_instruction("org", m["org_a"], m["slug"], _CORPS + "\nv2",
                              title="Lost deal revival", set_by=m["owner"])
    v = _client().get(f"/api/public/process-shares/{token}").json()
    assert v["preview_shape"] is None and v["step_count"] is None
    from oto_mcp.capabilities import partages_procedure as P
    assert P._share_get(_ctx(m["owner"], m["org_a"]),
                        P.ShareSlugInput(slug=m["slug"]))["share"]["shape_fresh"] is False


def test_la_vitrine_compte_ses_vues(monde):
    m = monde
    token = _publier(m)["token"]
    c = _client()
    for _ in range(3):
        c.get(f"/api/public/process-shares/{token}")
    assert _lecteurs(m)["totals"]["preview_views_30d"] == 3


def test_la_vitrine_est_bornee_par_ip(monde, monkeypatch):
    from oto_mcp import subdomain_project
    m = monde
    token = _publier(m)["token"]
    monkeypatch.setenv("OTO_ANON_RATE_BURST", "2")
    monkeypatch.setenv("OTO_ANON_RATE_PER_MIN", "0.001")
    subdomain_project._BUCKETS.clear()
    c = _client()
    codes = [c.get(f"/api/public/process-shares/{token}").status_code for _ in range(3)]
    subdomain_project._BUCKETS.clear()
    assert codes == [200, 200, 429]


def test_le_nombre_de_lecteurs_n_apparait_qu_a_partir_de_cinq(monde):
    from oto_mcp import db
    m = monde
    token = _publier(m)["token"]
    for i in range(4):
        sub = f"foule{i}_{m['u']}"
        db.upsert_user(sub, email=f"{sub}@corp{i}.example")
        _lire(m, sub, token)
    assert _client().get(f"/api/public/process-shares/{token}").json()["reader_count"] is None
    _lire(m, m["lecteurs"][0], token)
    assert _client().get(f"/api/public/process-shares/{token}").json()["reader_count"] == 5


# ── Le lecteur connecté ────────────────────────────────────────────────────────

def test_lire_connecte_rend_le_corps_et_note_le_lecteur(monde):
    from oto_mcp import db
    m = monde
    token = _publier(m)["token"]
    lecteur = m["lecteurs"][0]
    r = _lire(m, lecteur, token)
    assert r["body_md"] == _CORPS and r["is_member"] is False and r["copied"] is None
    _lire(m, lecteur, token)
    (row,) = _lecteurs(m)["readers"]
    assert row["email"].startswith("marie_") and row["domain"] == "acme.example"
    assert row["reads"] == 2
    profil = db.get_account_profile(lecteur)["profile"]
    assert profil["acquired_via"]["kind"] == "process_share"
    assert profil["acquired_via"]["token"] == token


def test_un_membre_de_l_org_n_est_jamais_note(monde):
    m = monde
    token = _publier(m)["token"]
    assert _lire(m, m["membre"], token)["is_member"] is True
    assert _lecteurs(m)["readers"] == []


def test_sans_voir_qui_lit_le_lecteur_reste_invisible_meme_apres(monde):
    m = monde
    token = _publier(m, show_readers=False)["token"]
    _lire(m, m["lecteurs"][0], token)
    _ecrire(m, "set", show_readers=True)
    _lire(m, m["lecteurs"][0], token)
    assert _lecteurs(m)["readers"] == [], \
        "une lecture faite sous « ne pas voir » ne devient pas visible ensuite"


def test_les_totaux_ne_comptent_pas_la_messagerie_grand_public(monde):
    m = monde
    token = _publier(m)["token"]
    for sub in m["lecteurs"]:
        _lire(m, sub, token)
    _copier(m, m["lecteurs"][0], token)
    t = _lecteurs(m)["totals"]
    assert t == {"readers": 2, "companies": 1, "copies": 1, "preview_views_30d": 0}


# ── La copie ───────────────────────────────────────────────────────────────────

def test_copier_va_dans_l_org_perso_et_est_idempotent(monde):
    from oto_mcp import org_store
    m = monde
    token = _publier(m)["token"]
    lecteur = m["lecteurs"][0]
    a = _copier(m, lecteur, token)
    perso = org_store.get_personal_org(lecteur)
    assert a["org_id"] == perso and a["created"] is True and a["is_new_account"] is True
    copie = org_store.get_instruction("org", perso, a["slug"])
    assert copie["body_md"] == _CORPS and copie["version"] == 1
    b = _copier(m, lecteur, token)
    assert b == {**a, "created": False}
    assert _lire(m, lecteur, token)["copied"] == {"org_id": perso, "slug": a["slug"]}


def test_copier_va_dans_l_org_active_que_l_on_administre(monde):
    from oto_mcp import db, org_store
    m = monde
    token = _publier(m)["token"]
    sub = f"admin_b_{m['u']}"
    db.upsert_user(sub, email=f"{sub}@globex.example")
    org_b = org_store.create_org(f"Globex {m['u']}", created_by=sub)
    org_store.add_org_member(org_b, sub, "org_admin")
    org_store.set_active_org(sub, org_b)
    r = _copier(m, sub, token)
    assert r["org_id"] == org_b and r["is_new_account"] is False


def test_un_compte_deja_accueilli_n_est_pas_nouveau(monde):
    from oto_mcp import db
    m = monde
    token = _publier(m)["token"]
    lecteur = m["lecteurs"][1]
    db.update_account_profile(lecteur, {"onboarded_at": "2026-10-01T00:00:00Z"})
    assert _copier(m, lecteur, token)["is_new_account"] is False


# ── Le résumé quotidien ────────────────────────────────────────────────────────

@pytest.fixture
def mails(monkeypatch):
    from oto_mcp import email
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://api.example.test")
    envois: list[dict] = []
    monkeypatch.setattr(email, "send_process_readers_digest_email",
                        lambda to, **kw: envois.append({"to": to, **kw}) or True)
    return envois


def test_le_resume_reste_a_blanc_tant_que_le_drapeau_est_ferme(monde, mails, monkeypatch):
    from oto_mcp import digest_lecteurs
    m = monde
    monkeypatch.delenv("OTO_DIGEST_LECTEURS", raising=False)
    _lire(m, m["lecteurs"][0], _publier(m)["token"])
    out = digest_lecteurs.balayer()
    assert out["a_blanc"] is True and out["lecteurs"] >= 1 and mails == []


def test_le_resume_part_une_fois_par_proprietaire(monde, mails, monkeypatch):
    from oto_mcp import digest_lecteurs
    from oto_mcp.db import partages_procedure as db_partages
    m = monde
    monkeypatch.setenv("OTO_DIGEST_LECTEURS", "1")
    token = _publier(m)["token"]
    for sub in m["lecteurs"]:
        _lire(m, sub, token)
    db_partages.marquer_resumes([(r["share_id"], r["reader_sub"])
                                 for r in db_partages.a_resumer()
                                 if r["created_by"] != m["owner"]])
    digest_lecteurs.balayer()
    (envoi,) = [e for e in mails if e["to"] == f"{m['owner']}@northwind.example"]
    (proc,) = envoi["processes"]
    assert proc["title"] == "Lost deal revival"
    assert proc["url"] == (f"https://front.example.test/org/{m['org_a']}/processes/"
                           f"{m['slug']}/readers")
    assert {r["company"] for r in proc["readers"]} == {"acme.example", None}
    assert envoi["unsubscribe_url"].startswith("https://api.example.test/o/r/")
    mails.clear()
    digest_lecteurs.balayer()
    assert [e for e in mails if e["to"] == f"{m['owner']}@northwind.example"] == []


def test_le_lien_de_desinscription_coupe_le_resume(monde, mails, monkeypatch):
    from oto_mcp import digest_lecteurs, outreach_optout
    m = monde
    monkeypatch.setenv("OTO_DIGEST_LECTEURS", "1")
    _lire(m, m["lecteurs"][0], _publier(m)["token"])
    lien = outreach_optout.lien_lecteurs(m["owner"])
    jeton = lien.rsplit("/", 1)[1]
    assert outreach_optout.verify_digest(jeton) is None, "jamais le jeton d'un autre canal"
    assert _client().get(f"/o/r/{jeton}").status_code == 200
    digest_lecteurs.balayer()
    assert [e for e in mails if e["to"] == f"{m['owner']}@northwind.example"] == []


def test_le_gabarit_rend_les_deux_langues(monkeypatch):
    from oto_mcp import email, email_templates
    envois = []
    monkeypatch.setattr(email, "_send",
                        lambda to, subject, html: envois.append((subject, html)) or True)
    procs = [{"title": "Lost deal revival", "url": "https://front.example.test/x",
              "readers": [{"name": "Marie", "company": "acme.example", "copied": True}]}]
    assert email_templates.send_process_readers_digest_email(
        "o@example.test", processes=procs, locale="en")
    assert email_templates.send_process_readers_digest_email(
        "o@example.test", processes=procs, locale=None)
    (en_sujet, en_html), (fr_sujet, fr_html) = envois
    assert "1 new reader" in en_sujet and "copied it" in en_html
    assert "1 nouveau lecteur" in fr_sujet and "l'a copiée" in fr_html
    assert not email_templates.send_process_readers_digest_email(
        "o@example.test", processes=[{"title": "x", "readers": []}])
    json.dumps(procs)


# ── Par la vraie route REST (adaptateur des capacités) ─────────────────────────

def test_le_fil_rest_du_proprietaire_et_du_lecteur(monde):
    from _datastore_rest import call
    m = monde
    org = str(m["org_a"]).encode()
    code, corps = call("org.instruction.share.get", sub=m["owner"],
                       path_params={"slug": m["slug"]}, query=b"org=" + org)
    assert (code, corps) == (200, {"share": None})
    code, corps = call("org.instruction.share.write", sub=m["owner"],
                       path_params={"slug": m["slug"]},
                       body={"op": "publish", "org": m["org_a"]})
    assert code == 200 and corps["share"]["show_readers"] is True
    token = corps["share"]["token"]
    code, corps = call("org.instruction.share.write", sub=m["owner"],
                       path_params={"slug": m["slug"]},
                       body={"op": "publish", "org": m["org_a"], "title": "x"})
    assert code == 400 and corps["error"] == "unknown_fields"
    code, corps = call("org.instruction.share.write", sub=m["membre"],
                       path_params={"slug": m["slug"]},
                       body={"op": "unpublish", "org": m["org_a"]})
    assert code == 403
    lecteur = m["lecteurs"][0]
    code, corps = call("me.process_share.read", sub=lecteur, path_params={"token": token})
    assert code == 200 and corps["body_md"] == _CORPS
    code, corps = call("me.process_share.copy", sub=lecteur, path_params={"token": token},
                       body={})
    assert code == 200 and corps["created"] is True
    code, corps = call("org.instruction.share.readers", sub=m["owner"],
                       path_params={"slug": m["slug"]}, query=b"org=" + org)
    assert code == 200 and corps["totals"]["readers"] == 1
    assert corps["readers"][0]["copied_at"]
    code, corps = call("me.process_share.read", sub=lecteur,
                       path_params={"token": "inconnu"})
    assert (code, corps["error"]) == (404, "not_found")
