"""Le PARTAGE d'un agent hébergé (`capabilities/_acces_agent.py`).

Avant : tout membre de l'org modifiait ou supprimait l'agent de n'importe qui — qui
tournait ensuite SOUS SON PROPRIÉTAIRE. Désormais un agent est à son propriétaire,
qui le partage nommément, à l'intérieur de son org :

- invisible (ni propriétaire, ni admin, ni partage) → 404, et absent de la liste ;
- `viewer` → lit, ne modifie pas, ne lit pas les corps de livraison ;
- `editor` → modifie, ne supprime pas, ne partage pas ;
- propriétaire / admin d'org → tout, partager compris, et seulement à un membre.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import ownership, roles
from oto_mcp.capabilities import _acces_agent as A
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 41
PROPRIETAIRE, EDITEUR, LECTEUR, AUTRE, ADMIN, DEHORS = (
    "proprio", "editeur", "lecteur", "autre", "admin", "dehors")


@pytest.fixture
def org(monkeypatch):
    """Une org, un agent, et des partages en mémoire."""
    agent = {"id": 7, "org_id": ORG, "sub": PROPRIETAIRE, "procedure": "veille",
             "tools": ["a"], "input": "fais la veille", "enabled": False,
             "kind": "schedule", "cron": "0 8 * * *", "tz": "Europe/Paris",
             "model": "claude-sonnet-5"}
    partages: dict[tuple[str, str], str] = {
        ("user", EDITEUR): "editor", ("user", LECTEUR): "viewer"}
    etat = {"agent": agent, "partages": partages, "ecrit": None, "supprime": False}

    db = RT.db
    monkeypatch.setattr(db, "list_triggers", lambda o: [dict(agent)] if o == ORG else [])
    monkeypatch.setattr(db, "get_trigger",
                        lambda i, o: dict(agent) if (i, o) == (7, ORG) else None)

    def _maj(i, o, champs, hors_abonnement_d_autrui=None):
        etat["ecrit"] = champs
        return {**agent, **champs}
    monkeypatch.setattr(db, "update_trigger", _maj)
    monkeypatch.setattr(db, "delete_trigger",
                        lambda i, o: etat.update(supprime=True) or True)
    monkeypatch.setattr(db, "livraisons", lambda *a, **k: [])
    monkeypatch.setattr(db, "runner_arme", lambda o: None)
    monkeypatch.setattr(RT._modele, "etat_servi", lambda etat, o: None)
    monkeypatch.setattr(db, "trigger_sans_org",
                        lambda i: {"id": 7, "org_id": ORG, "sub": PROPRIETAIRE}
                        if i == 7 else None)

    def _partages(ids, principaux):
        best = None
        for p in principaux:
            role = partages.get((p[0], str(p[1])))
            if role:
                perm = "write" if role == "editor" else "read"
                best = "write" if "write" in (best, perm) else perm
        return {7: best} if best and 7 in ids else {}
    monkeypatch.setattr(db, "partages_d_agents", _partages)

    class _Scope:
        def __init__(self, sub):
            self.sub = sub

        def principal_pairs(self):
            pairs = [("user", self.sub)]
            return pairs + ([("org", str(ORG))] if self.sub != DEHORS else [])
    monkeypatch.setattr(ownership, "accessor_scope", _Scope)
    monkeypatch.setattr(roles, "is_org_admin", lambda s, o: s == ADMIN and o == ORG)
    monkeypatch.setattr(roles, "is_org_member", lambda s, o: s != DEHORS and o == ORG)
    monkeypatch.setattr(db, "get_users_by_email",
                        lambda e: [{"sub": e.split("@")[0], "email": e}])

    def _grant(kind, rid, ptype, pid, role=None, granted_by=None, **k):
        partages[(ptype, pid)] = role
    monkeypatch.setattr(ownership, "grant", _grant)
    monkeypatch.setattr(ownership, "revoke",
                        lambda kind, rid, ptype, pid: partages.pop((ptype, pid), None)
                        is not None)
    monkeypatch.setattr(ownership, "list_grants", lambda kind, rid: [
        {"principal_type": t, "principal_id": i, "role": r,
         "email": f"{i}@x.test" if t == "user" else None}
        for (t, i), r in partages.items()])
    # Ce banc ne parle ni des pertes, ni de l'adresse, ni des avertissements d'outils.
    monkeypatch.setattr(RT, "_avec_pertes", lambda o, t: t)
    monkeypatch.setattr(RT, "_avec_hook", lambda o, t: t)

    async def _sans(ctx, t):
        return t
    monkeypatch.setattr(RT, "_avec_tool_warnings", _sans)
    return etat


def _appel(sub, **kw):
    return asyncio.run(RT._triggers(ResolvedCtx(sub=sub, org_id=ORG),
                                    RT.TriggerInput(**kw)))


def _refus(sub, **kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _appel(sub, **kw)
    return e.value


# ── voir ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sub,acces", [(PROPRIETAIRE, "owner"), (ADMIN, "admin"),
                                       (EDITEUR, "editor"), (LECTEUR, "viewer")])
def test_la_liste_sert_l_agent_avec_ce_que_l_appelant_peut_en_faire(org, sub, acces):
    t, = _appel(sub, op="list")["triggers"]
    assert t["my_access"] == acces
    assert t["can_edit"] is (acces != "viewer")
    assert t["can_share"] is (acces in ("owner", "admin"))


def test_un_membre_sans_partage_ne_le_voit_pas(org):
    assert _appel(AUTRE, op="list")["triggers"] == []
    e = _refus(AUTRE, op="get", trigger_id=7)
    assert (e.status, e.code) == (404, "trigger_not_found"), (
        "ne pas voir un agent, c'est ne pas savoir qu'il existe : même 404")


def test_partage_a_toute_l_org_le_rend_visible_et_modifiable(org):
    org["partages"][("org", str(ORG))] = "editor"
    t, = _appel(AUTRE, op="list")["triggers"]
    assert t["my_access"] == "editor"
    _appel(AUTRE, op="update", trigger_id=7, label="renommé")
    assert org["ecrit"] == {"label": "renommé"}


# ── modifier ─────────────────────────────────────────────────────────────────

def test_un_editeur_modifie(org):
    t = _appel(EDITEUR, op="update", trigger_id=7, input="nouvelle consigne")["trigger"]
    assert org["ecrit"] == {"input": "nouvelle consigne"}
    assert t["my_access"] == "editor"
    assert t["sub"] == PROPRIETAIRE, "modifier ne change PAS sous qui l'agent tourne"


def test_un_lecteur_ne_modifie_pas(org):
    e = _refus(LECTEUR, op="update", trigger_id=7, label="x")
    assert (e.status, e.code) == (403, "trigger_edit_forbidden")
    assert org["ecrit"] is None


def test_un_membre_sans_partage_ne_modifie_pas_et_n_apprend_rien(org):
    e = _refus(AUTRE, op="update", trigger_id=7, label="x")
    assert (e.status, e.code) == (404, "trigger_not_found")
    assert org["ecrit"] is None


def test_un_lecteur_ne_lit_pas_les_corps_de_livraison(org):
    assert _appel(LECTEUR, op="deliveries", trigger_id=7)["deliveries"] == []
    e = _refus(LECTEUR, op="deliveries", trigger_id=7, with_input=True)
    assert e.code == "trigger_edit_forbidden"
    _appel(EDITEUR, op="deliveries", trigger_id=7, with_input=True)


def test_un_lecteur_ne_vide_pas_la_file(org):
    assert _refus(LECTEUR, op="clear_queue", trigger_id=7).code == "trigger_edit_forbidden"


# ── supprimer ────────────────────────────────────────────────────────────────

def test_un_editeur_ne_supprime_pas(org):
    assert _refus(EDITEUR, op="delete", trigger_id=7).code == "trigger_edit_forbidden"
    assert not org["supprime"]


@pytest.mark.parametrize("sub", [PROPRIETAIRE, ADMIN])
def test_le_proprietaire_et_l_admin_suppriment(org, sub):
    assert _appel(sub, op="delete", trigger_id=7)["ok"]
    assert org["supprime"]


# ── partager ─────────────────────────────────────────────────────────────────

def test_le_proprietaire_partage_a_un_membre_par_son_adresse(org):
    rep = _appel(PROPRIETAIRE, op="share", trigger_id=7,
                 share_with_email="autre@x.test", role="viewer")
    assert org["partages"][("user", AUTRE)] == "viewer"
    assert {"principal_type": "user", "sub": AUTRE, "email": "autre@x.test",
            "role": "viewer", "granted_at": None} in rep["shares"]


def test_le_role_par_defaut_est_editeur(org):
    _appel(ADMIN, op="share", trigger_id=7, share_with_sub=AUTRE)
    assert org["partages"][("user", AUTRE)] == "editor"


def test_toute_l_org_se_sert_sous_everyone(org):
    rep = _appel(PROPRIETAIRE, op="share", trigger_id=7, everyone=True)
    assert org["partages"][("org", str(ORG))] == "editor"
    assert any(s["principal_type"] == "everyone" for s in rep["shares"])


def test_on_ne_partage_pas_hors_de_l_org(org):
    e = _refus(PROPRIETAIRE, op="share", trigger_id=7, share_with_sub=DEHORS)
    assert e.code == "share_not_org_member"
    e = _refus(PROPRIETAIRE, op="share", trigger_id=7,
               share_with_email="dehors@x.test")
    assert e.code == "share_not_org_member"
    assert ("user", DEHORS) not in org["partages"]


def test_un_editeur_ne_partage_pas(org):
    e = _refus(EDITEUR, op="share", trigger_id=7, share_with_sub=AUTRE)
    assert e.code == "trigger_edit_forbidden"


def test_un_lecteur_voit_avec_qui_l_agent_est_partage(org):
    assert len(_appel(LECTEUR, op="shares", trigger_id=7)["shares"]) == 2


def test_retirer_un_partage_retire_l_acces(org):
    _appel(PROPRIETAIRE, op="unshare", trigger_id=7, share_with_sub=EDITEUR)
    assert _appel(EDITEUR, op="list")["triggers"] == []


def test_on_retire_le_partage_d_un_membre_parti(org):
    org["partages"][("user", DEHORS)] = "editor"
    _appel(PROPRIETAIRE, op="unshare", trigger_id=7, share_with_sub=DEHORS)
    assert ("user", DEHORS) not in org["partages"]


def test_un_beneficiaire_et_un_seul(org):
    assert _refus(PROPRIETAIRE, op="share", trigger_id=7).code == "share_target_required"
    assert _refus(PROPRIETAIRE, op="share", trigger_id=7, everyone=True,
                  share_with_sub=AUTRE).code == "share_target_ambiguous"
    assert _refus(PROPRIETAIRE, op="share", trigger_id=7,
                  share_with_sub=PROPRIETAIRE).code == "share_with_owner"


# ── le kind d'ownership ──────────────────────────────────────────────────────

def test_le_kind_runner_trigger_est_enregistre(org):
    assert ownership.owner_of(A.KIND, "7") == ("user", PROPRIETAIRE)
    assert ownership.owner_of(A.KIND, "8") is None


# ── les travaux d'un agent qu'on ne peut pas modifier ────────────────────────

def _travail():
    return {"id": 1, "payload": {"trigger_id": 7, "procedure": "veille",
                                 "label": "la veille", "model": "claude-sonnet-5",
                                 "input": "consigne", "tools": ["a"],
                                 "webhook_body": {"email": "x@y.test"}}}


@pytest.mark.parametrize("sub", [LECTEUR, AUTRE])
def test_la_file_ne_montre_pas_ce_qu_un_agent_execute_a_qui_ne_peut_le_modifier(org, sub):
    j, = A.masquer_charges(ResolvedCtx(sub=sub, org_id=ORG), [_travail()])
    assert j["payload"] == {"trigger_id": 7, "procedure": "veille",
                            "label": "la veille", "model": "claude-sonnet-5",
                            "payload_redacted": True}
    assert j["id"] == 1, "la ligne reste : le compte de la file ne ment pas"


@pytest.mark.parametrize("sub", [PROPRIETAIRE, EDITEUR, ADMIN])
def test_la_file_montre_tout_a_qui_peut_modifier_l_agent(org, sub):
    assert A.masquer_charges(ResolvedCtx(sub=sub, org_id=ORG), [_travail()]) == [_travail()]


def test_un_travail_lance_a_la_main_n_est_pas_touche(org):
    j = {"id": 2, "payload": {"procedure": "veille", "input": "à la main"}}
    assert A.masquer_charges(ResolvedCtx(sub=AUTRE, org_id=ORG), [j]) == [j]


def test_les_travaux_d_un_agent_SUPPRIME_restent_a_qui_les_portait_et_aux_admins(org):
    j = {"id": 3, "sub": PROPRIETAIRE,
         "payload": {"trigger_id": 99, "procedure": "veille", "input": "consigne"}}
    for sub in (PROPRIETAIRE, ADMIN):
        assert A.masquer_charges(ResolvedCtx(sub=sub, org_id=ORG), [j]) == [j]
    masque, = A.masquer_charges(ResolvedCtx(sub=EDITEUR, org_id=ORG), [j])
    assert "input" not in masque["payload"] and masque["payload"]["payload_redacted"]


# ── ne pas voir un agent, c'est ne pas savoir qu'il existe ───────────────────

def test_une_retouche_refusee_ne_decrit_pas_l_agent_qu_on_ne_voit_pas(org):
    """Un `cron` sur un webhook répond `invalid_schedule` : ce refus-là dirait le
    GENRE de l'agent. Il ne doit sortir qu'à qui peut le modifier."""
    org["agent"]["kind"] = "webhook"
    assert _refus(AUTRE, op="update", trigger_id=7, cron="0 9 * * *").code == "trigger_not_found"
    assert _refus(LECTEUR, op="update", trigger_id=7, cron="0 9 * * *").code == "trigger_edit_forbidden"
    assert _refus(EDITEUR, op="update", trigger_id=7, cron="0 9 * * *").code == "invalid_schedule"


def test_la_porte_d_un_agent_invisible_repond_404(org):
    org["agent"]["kind"] = "webhook"
    assert _refus(AUTRE, op="rotate_secret", trigger_id=7).code == "trigger_not_found"
    assert _refus(EDITEUR, op="rotate_secret", trigger_id=7).code == "trigger_owner_or_admin_required"


def test_on_retire_par_son_adresse_le_partage_d_un_membre_parti(org):
    org["partages"][("user", DEHORS)] = "editor"
    _appel(PROPRIETAIRE, op="unshare", trigger_id=7, share_with_email="dehors@x.test")
    assert ("user", DEHORS) not in org["partages"]
