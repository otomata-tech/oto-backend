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

# Ce banc ne parle pas de l'existence de la procédure d'un agent (#1499, conftest).
pytestmark = pytest.mark.usefixtures("procedures_connues")

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
    monkeypatch.setattr(db, "porteurs_des_agents",
                        lambda ids, o: [{"id": 7, "org_id": ORG, "sub": agent["sub"]}]
                        if o == ORG and 7 in ids else [])

    def _maj(i, o, champs, hors_abonnement_d_autrui=None, par=None):
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

    def _visibilites(ids, o):
        org_role = partages.get(("org", str(o)))
        gens = sum(1 for (t, _) in partages if t != "org")
        return {int(i): {"org": org_role, "people": gens} for i in ids}
    monkeypatch.setattr(db, "visibilites_d_agents", _visibilites)

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
    monkeypatch.setattr(RT, "_servis_en_liste", lambda o, ts: ts)

    async def _sans(ctx, t, catalogues=None):
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


def test_chaque_agent_servi_dit_qui_le_voit(org):
    t, = _appel(PROPRIETAIRE, op="list")["triggers"]
    assert t["visibility"] == {"org": None, "people": 2}
    org["partages"][("org", str(ORG))] = "editor"
    assert _appel(PROPRIETAIRE, op="get", trigger_id=7)["trigger"]["visibility"] == {
        "org": "editor", "people": 2}


# ── naître : privé, sauf le réglage de l'org (`partage_par_defaut`) ───────────

@pytest.fixture
def creation(org, monkeypatch):
    """Une création : la procédure se lit, rien ne tourne déjà dessus ; le réglage de
    l'org est `org["reglage"]` (la valeur STOCKÉE), et chaque partage posé garde son
    `granted_by`."""
    db = RT.db
    org["partages"].clear()
    org["reglage"] = None
    org["auteurs"] = {}
    org["cree"] = False
    monkeypatch.setattr(db, "partage_par_defaut_stocke",
                        lambda o: org["reglage"] if o == ORG else None)
    monkeypatch.setattr(db, "preset_agent_partage", lambda sub, org: False)
    monkeypatch.setattr(db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(db, "create_trigger", lambda o, sub, **k: org.update(cree=True) or {
        "id": 7, "org_id": o, "sub": sub, **k})

    def _grant(kind, rid, ptype, pid, role=None, granted_by=None, **k):
        org["partages"][(ptype, pid)] = role
        org["auteurs"][(ptype, pid)] = granted_by
    monkeypatch.setattr(ownership, "grant", _grant)
    return org


def _creer(monkeypatch, **kw):
    # Ce banc ne parle ni du modèle, ni du runner, ni des clés : seulement du partage.
    for nom in ("famille_declaree", "exige_un_modele", "exige_un_runner", "exige_servi"):
        monkeypatch.setattr(RT._modele, nom, lambda *a, **k: None)
    monkeypatch.setattr(RT._cle_exigee, "exiger_a_la_pose", lambda *a, **k: None)
    monkeypatch.setattr(RT._abonnement, "exiger_a_la_pose", lambda *a, **k: None)
    return _appel(PROPRIETAIRE, op="create", procedure="veille", cron="0 8 * * *",
                  tools=["a"], model="claude-sonnet-5", **kw)["trigger"]


def test_sans_reglage_un_agent_nait_prive(creation, monkeypatch):
    t = _creer(monkeypatch)
    assert creation["partages"] == {}, "le code ne partage jamais d'office un objet neuf"
    assert t["visibility"] == {"org": None, "people": 0}
    creation["reglage"] = {}
    _creer(monkeypatch, private=False)
    assert creation["partages"] == {}, "`private=false` n'ouvre pas au-delà du réglage"


def test_en_lecture_l_org_voit_l_agent_sans_pouvoir_le_modifier(creation, monkeypatch):
    creation["reglage"] = {"agent": "lecture"}
    t = _creer(monkeypatch)
    assert creation["partages"] == {("org", str(ORG)): "viewer"}
    assert creation["auteurs"] == {("org", str(ORG)): RT.db.MARQUE_PARTAGE_PAR_DEFAUT}, (
        "le partage né du réglage est MARQUÉ : il se retire sans toucher aux autres")
    assert t["visibility"] == {"org": "viewer", "people": 0}
    vu, = _appel(AUTRE, op="list")["triggers"]
    assert (vu["my_access"], vu["can_edit"]) == ("viewer", False)
    e = _refus(AUTRE, op="update", trigger_id=7, label="renommé")
    assert (e.status, e.code) == (403, "trigger_edit_forbidden")


def _preset(monkeypatch, valeur):
    monkeypatch.setattr(RT.db, "preset_agent_partage",
                        lambda sub, org: valeur == "shared" and org == ORG)


def test_le_preset_shared_partage_et_prete_a_la_naissance(creation, monkeypatch):
    _preset(monkeypatch, "shared")
    prets = []
    monkeypatch.setattr(RT.db, "poser_pret_du_forfait",
                        lambda i, o, sub, m: prets.append((sub, m)) or None)
    monkeypatch.setattr(RT.runner_models, "MODELES_PERSONNELS", {"claude-sonnet-5"})
    _creer(monkeypatch)
    assert creation["partages"] == {("org", str(ORG)): "editor"}
    assert creation["auteurs"] == {("org", str(ORG)): PROPRIETAIRE}, (
        "un geste du propriétaire, pas le partage marqué d'un réglage d'admin")
    assert prets == [(PROPRIETAIRE, "claude-sonnet-5")]


def test_le_preset_ne_prete_rien_hors_abonnement(creation, monkeypatch):
    _preset(monkeypatch, "shared")
    monkeypatch.setattr(RT.db, "poser_pret_du_forfait",
                        lambda *a: pytest.fail("rien à prêter"))
    monkeypatch.setattr(RT.runner_models, "MODELES_PERSONNELS", set())
    _creer(monkeypatch)
    assert creation["partages"] == {("org", str(ORG)): "editor"}


def test_private_l_emporte_sur_le_preset(creation, monkeypatch):
    _preset(monkeypatch, "shared")
    _creer(monkeypatch, private=True)
    assert creation["partages"] == {}


def test_le_refus_de_l_admin_l_emporte_sur_le_preset(creation, monkeypatch):
    """Le préréglage ignoré, l'agent suit le réglage de l'org — ici, privé."""
    _preset(monkeypatch, "shared")
    creation["reglage"] = {"preset_agent": "refuse"}
    _creer(monkeypatch)
    assert creation["partages"] == {}


def test_private_l_emporte_sur_le_reglage(creation, monkeypatch):
    creation["reglage"] = {"agent": "lecture"}
    t = _creer(monkeypatch, private=True)
    assert creation["partages"] == {}
    assert t["visibility"] == {"org": None, "people": 0}


@pytest.mark.parametrize("stocke", [{"agent": "ecriture"}, {"agent": "tous"},
                                    {"tableau": "lecture"}, ["agent"], "lecture"])
def test_un_reglage_illisible_leve_avant_toute_creation(creation, monkeypatch, stocke):
    """Ni `aucun` ni autre chose : un réglage qu'on ne sait pas lire ne se devine pas —
    et aucun agent n'est créé derrière le refus. `ecriture` stocké pour un agent est
    illisible : rien ne peut l'avoir posé."""
    creation["reglage"] = stocke
    with pytest.raises(AuthzDenied) as e:
        _creer(monkeypatch)
    assert (e.value.status, e.value.code) == (409, "default_sharing_unreadable")
    assert creation["cree"] is False and creation["partages"] == {}


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
    assert {"principal_type": "user", "sub": AUTRE, "group_id": None,
            "email": "autre@x.test", "role": "viewer", "granted_at": None} in rep["shares"]


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


# ── le forfait du propriétaire (04/10/2026) ──────────────────────────────────
#
# Un agent posé sur l'abonnement Claude de son propriétaire ne se modifiait que par
# lui : un éditeur ne pouvait que l'éteindre, et seule la reprise (`take_over`)
# ouvrait l'agent. Désormais le propriétaire PRÊTE son forfait pour cet agent en
# nommant un éditeur ; à une équipe ou à l'org entière, seulement par `op=lend`
# (29/10/2026). Ne le prêtent pas : la révision 0035, un partage posé par un admin.

NOMME_PAR_ADMIN = "nomme-par-admin"


@pytest.fixture
def forfait(org, monkeypatch):
    """L'agent tourne sur l'abonnement de PROPRIETAIRE, sans prêt collectif. EDITEUR
    est nommé par lui ; NOMME_PAR_ADMIN est éditeur nommé, mais par un admin ;
    AUTRE ne l'est que par le partage à toute l'org de la révision 0035 ; EQUIPIER est
    membre de l'équipe 12."""
    from oto_mcp import runner_models
    agent = org["agent"]
    agent["model"] = "sub:sonnet"
    agent["forfait_prete_model"] = None
    partages = org["partages"]
    partages[("user", NOMME_PAR_ADMIN)] = "editor"
    partages[("org", str(ORG))] = "editor"
    poses_par = {("user", EDITEUR): PROPRIETAIRE, ("user", LECTEUR): PROPRIETAIRE,
                 ("user", NOMME_PAR_ADMIN): ADMIN,
                 ("org", str(ORG)): "migration:0035_agents_partages_a_l_org"}
    org["poses_par"] = poses_par

    def _grant(kind, rid, ptype, pid, role=None, granted_by=None, **k):
        partages[(ptype, pid)] = role
        poses_par[(ptype, pid)] = granted_by
    monkeypatch.setattr(ownership, "grant", _grant)

    equipes = {EQUIPIER: "12"}

    def _par_lui(c):
        return partages.get(c) == "editor" and poses_par.get(c) == agent["sub"]

    def _pretes(ids, sub, nomme=False):
        # `_forfait_prete` en base : le prêt NOMMÉ (partage `write` à la personne posé
        # par le propriétaire), OR le prêt COLLECTIF (prêt vivant sous le modèle
        # courant, et partage posé par lui à son équipe ou à toute l'org).
        atteint = _par_lui(("user", sub))
        if not nomme and agent.get("forfait_prete_model") == agent["model"]:
            cibles = [("org", str(ORG))] + ([("group", equipes[sub])] if sub in equipes else [])
            atteint = atteint or any(_par_lui(c) for c in cibles)
        return {7} if atteint and 7 in ids else set()
    monkeypatch.setattr(RT.db, "forfaits_pretes", _pretes)

    def _maj(i, o, champs, hors_abonnement_d_autrui=None, par=None):
        h = hors_abonnement_d_autrui
        if (h and agent["model"] in runner_models.MODELES_PERSONNELS
                and agent["sub"] != h and 7 not in _pretes([7], h)):
            return None   # la garde d'écriture, comme en base
        org["ecrit"] = champs
        org["par"] = par
        return {**agent, **champs}
    monkeypatch.setattr(RT.db, "update_trigger", _maj)

    def _preter(i, o, proprietaire, modele):
        # `poser_pret_du_forfait` en base : n'écrit que si le propriétaire l'est
        # encore et, pour un prêt, si le modèle n'a pas bougé.
        if agent["sub"] != proprietaire or (modele is not None and agent["model"] != modele):
            return None
        agent["forfait_prete_model"] = modele
        return dict(agent)
    monkeypatch.setattr(RT.db, "poser_pret_du_forfait", _preter)

    class _Scope:
        def __init__(self, sub):
            self.sub = sub

        def principal_pairs(self):
            # Comme le vrai `accessor_scope` : l'org n'est une paire que d'un membre.
            pairs = [("user", self.sub)] + ([("org", str(ORG))] if self.sub != DEHORS else [])
            return pairs + ([("group", equipes[self.sub])] if self.sub in equipes else [])
    monkeypatch.setattr(ownership, "accessor_scope", _Scope)
    from oto_mcp import group_store
    monkeypatch.setattr(group_store, "get_group",
                        lambda g: {"id": g, "org_id": ORG} if g == 12 else None)

    # Rallumer : seule la garde de pose d'un abonnement parle ici — et l'on note
    # AU NOM DE QUI elle se juge.
    org["poses"] = []
    monkeypatch.setattr(RT._abonnement, "exiger_a_la_pose",
                        lambda sub, proprio, famille, **k:
                        org["poses"].append((sub, proprio, famille)))
    monkeypatch.setattr(RT._modele, "exige_un_runner", lambda o: {})
    monkeypatch.setattr(RT._modele, "exige_servi", lambda etat, famille: None)
    monkeypatch.setattr(RT._cle_exigee, "exiger_a_la_pose", lambda o, famille, etat=None: None)
    return org


@pytest.mark.parametrize("sub,peut", [(PROPRIETAIRE, True), (EDITEUR, True),
                                      (NOMME_PAR_ADMIN, False), (AUTRE, False),
                                      (ADMIN, False), (LECTEUR, False)])
def test_sur_un_forfait_can_edit_dit_qui_peut_vraiment_le_modifier(forfait, sub, peut):
    t, = _appel(sub, op="list")["triggers"]
    assert t["can_edit"] is peut
    assert _appel(sub, op="get", trigger_id=7)["trigger"]["can_edit"] is peut


def test_l_editeur_nomme_par_le_proprietaire_modifie_l_agent_sur_son_forfait(forfait):
    t = _appel(EDITEUR, op="update", trigger_id=7, input="nouvelle consigne")["trigger"]
    assert forfait["ecrit"] == {"input": "nouvelle consigne"}
    assert t["sub"] == PROPRIETAIRE and t["can_edit"] is True


@pytest.mark.parametrize("sub", [AUTRE, NOMME_PAR_ADMIN, ADMIN])
def test_qui_n_a_pas_ete_nomme_par_le_proprietaire_ne_modifie_pas_son_forfait(forfait, sub):
    e = _refus(sub, op="update", trigger_id=7, input="autre chose")
    assert (e.status, e.code) == (400, "subscription_personal_only")
    assert forfait["ecrit"] is None


@pytest.mark.parametrize("sub", [AUTRE, ADMIN])
def test_qui_ecrit_l_eteint_quand_meme(forfait, sub):
    forfait["agent"]["enabled"] = True
    _appel(sub, op="update", trigger_id=7, enabled=False)
    assert forfait["ecrit"] == {"enabled": False}


def test_l_editeur_nomme_rallume_POUR_le_proprietaire(forfait):
    """C'est la connexion du propriétaire qui servira : c'est elle qui se juge."""
    _appel(EDITEUR, op="update", trigger_id=7, enabled=True)
    assert forfait["poses"] == [(PROPRIETAIRE, PROPRIETAIRE, "claude_subscription")]
    assert forfait["ecrit"]["enabled"] is True


def test_un_admin_qui_rallume_se_juge_sur_lui_et_n_ecrit_rien(forfait):
    """Il gouverne l'agent, il ne dispose pas du forfait d'autrui."""
    assert _refus(ADMIN, op="update", trigger_id=7, enabled=True).code == \
        "subscription_personal_only"
    assert forfait["poses"] == [(ADMIN, PROPRIETAIRE, "claude_subscription")]
    assert forfait["ecrit"] is None


def test_l_editeur_nomme_change_de_modele_dans_le_forfait(forfait):
    _appel(EDITEUR, op="update", trigger_id=7, model="sub:opus")
    assert forfait["ecrit"] == {"model": "sub:opus"}


def test_l_editeur_nomme_peut_sortir_l_agent_du_forfait(forfait):
    _appel(EDITEUR, op="update", trigger_id=7, model="claude-sonnet-5")
    assert forfait["ecrit"] == {"model": "claude-sonnet-5"}


def test_sortir_du_forfait_d_autrui_sans_pret_dit_pourquoi(forfait):
    """Refusé par la garde d'écriture : le refus nomme le forfait, pas un agent
    inconnu (la clé d'org visée, elle, ne garde rien)."""
    e = _refus(AUTRE, op="update", trigger_id=7, model="claude-sonnet-5")
    assert (e.status, e.code) == (400, "subscription_personal_only")
    assert forfait["ecrit"] is None


def test_POSER_l_agent_sur_le_forfait_reste_au_proprietaire(forfait):
    """Le prêt vaut pour l'agent qui tourne DÉJÀ sur le forfait."""
    forfait["agent"]["model"] = "claude-sonnet-5"
    e = _refus(EDITEUR, op="update", trigger_id=7, model="sub:sonnet")
    assert e.code == "subscription_personal_only"
    assert forfait["ecrit"] is None
    _appel(PROPRIETAIRE, op="update", trigger_id=7, model="sub:sonnet")
    assert forfait["ecrit"] == {"model": "sub:sonnet"}


def test_retirer_le_partage_retire_le_pret(forfait):
    _appel(PROPRIETAIRE, op="unshare", trigger_id=7, share_with_sub=EDITEUR)
    # Il voit encore l'agent (le partage à toute l'org), sans pouvoir le modifier.
    assert _refus(EDITEUR, op="update", trigger_id=7, input="x").code == \
        "subscription_personal_only"


def test_le_proprietaire_qui_nomme_un_editeur_lui_prete_son_forfait(forfait):
    _appel(PROPRIETAIRE, op="share", trigger_id=7, share_with_sub=AUTRE)
    _appel(AUTRE, op="update", trigger_id=7, input="x")
    assert forfait["ecrit"] == {"input": "x"}


def test_un_admin_qui_repose_le_partage_ne_prete_pas_le_forfait_d_autrui(forfait):
    """Le partage se réécrit avec son auteur (`granted_by`) : reposé par un admin,
    il garde l'accès à l'agent mais plus le forfait."""
    _appel(ADMIN, op="share", trigger_id=7, share_with_sub=EDITEUR, role="editor")
    assert _refus(EDITEUR, op="update", trigger_id=7, input="x").code == \
        "subscription_personal_only"


def test_apres_une_reprise_les_partages_de_l_ancien_ne_pretent_pas_le_nouveau(forfait):
    forfait["agent"]["sub"] = ADMIN   # repris : EDITEUR avait été nommé par l'ancien
    assert _refus(EDITEUR, op="update", trigger_id=7, input="x").code == \
        "subscription_personal_only"


# ── le prêt, un geste du titulaire (29/10/2026) ──────────────────────────────

EQUIPIER = "equipier"


def _equipe_et_org_par_le_proprietaire():
    _appel(PROPRIETAIRE, op="share", trigger_id=7, share_with_group=12)
    _appel(PROPRIETAIRE, op="share", trigger_id=7, everyone=True)


def test_sans_pret_l_org_entiere_et_l_equipe_restent_fermees(forfait):
    """Partagés en écriture par le propriétaire, AVANT tout geste : ni l'équipe ni l'org
    n'empruntent. L'éditeur nommé emprunte comme avant."""
    _equipe_et_org_par_le_proprietaire()
    for sub in (EQUIPIER, AUTRE):
        assert _refus(sub, op="update", trigger_id=7, input="x").code == \
            "subscription_personal_only", sub
        t, = _appel(sub, op="list")["triggers"]
        assert t["can_edit"] is False
    _appel(EDITEUR, op="update", trigger_id=7, input="par le nommé")
    assert forfait["ecrit"] == {"input": "par le nommé"}


def test_le_titulaire_prete_a_l_equipe_et_a_l_org(forfait):
    _equipe_et_org_par_le_proprietaire()
    t = _appel(PROPRIETAIRE, op="lend", trigger_id=7)["trigger"]
    assert forfait["agent"]["forfait_prete_model"] == "sub:sonnet"
    assert t["sub"] == PROPRIETAIRE, "l'identité d'exécution ne bouge pas"
    for sub in (EQUIPIER, AUTRE, EDITEUR):
        _appel(sub, op="update", trigger_id=7, input=f"par {sub}")
        assert forfait["ecrit"] == {"input": f"par {sub}"}
        assert forfait["par"] == sub, "l'auteur de la retouche est enregistré"


def test_qui_emprunte_par_le_pret_collectif_ne_change_pas_de_modele(forfait):
    """Le prêt collectif vaut pour le modèle du geste ; l'éditeur nommé, lui, garde
    ses droits d'avant."""
    _equipe_et_org_par_le_proprietaire()
    _appel(PROPRIETAIRE, op="lend", trigger_id=7)
    e = _refus(EQUIPIER, op="update", trigger_id=7, model="sub:opus")
    assert (e.status, e.code) == (400, "subscription_personal_only")
    assert forfait["ecrit"] is None
    _appel(EDITEUR, op="update", trigger_id=7, model="sub:opus")
    assert forfait["ecrit"] == {"model": "sub:opus"}


def test_le_partage_a_une_equipe_d_une_autre_org_est_refuse(forfait):
    e = _refus(PROPRIETAIRE, op="share", trigger_id=7, share_with_group=99)
    assert (e.status, e.code) == (404, "share_group_not_in_org")
    e = _refus(PROPRIETAIRE, op="share", trigger_id=7, share_with_group=12, everyone=True)
    assert e.code == "share_target_ambiguous"


@pytest.mark.parametrize("op", ["lend", "unlend"])
@pytest.mark.parametrize("sub", [ADMIN, EDITEUR, AUTRE])
def test_un_non_titulaire_ne_prete_ni_ne_reprend(forfait, sub, op):
    avant = forfait["agent"]["forfait_prete_model"]
    e = _refus(sub, op=op, trigger_id=7)
    assert (e.status, e.code) == (403, "trigger_owner_required")
    assert forfait["agent"]["forfait_prete_model"] == avant


def test_qui_ne_voit_pas_l_agent_ne_prete_pas_et_n_apprend_rien(forfait):
    e = _refus(DEHORS, op="lend", trigger_id=7)
    assert (e.status, e.code) == (404, "trigger_not_found")


def test_un_agent_hors_abonnement_n_a_rien_a_preter(forfait):
    forfait["agent"]["model"] = "claude-sonnet-5"
    assert _refus(PROPRIETAIRE, op="lend", trigger_id=7).code == "lend_not_on_subscription"


def test_un_changement_de_modele_rend_le_pret_caduc(forfait):
    _equipe_et_org_par_le_proprietaire()
    _appel(PROPRIETAIRE, op="lend", trigger_id=7)
    forfait["agent"]["model"] = "sub:opus"   # le propriétaire l'a changé
    assert _refus(AUTRE, op="update", trigger_id=7, input="x").code == \
        "subscription_personal_only"
    _appel(PROPRIETAIRE, op="lend", trigger_id=7)   # un nouveau geste, sous opus
    _appel(AUTRE, op="update", trigger_id=7, input="x")
    assert forfait["ecrit"] == {"input": "x"}


def test_retirer_le_pret_referme_l_equipe_pas_le_nomme(forfait):
    _equipe_et_org_par_le_proprietaire()
    _appel(PROPRIETAIRE, op="lend", trigger_id=7)
    _appel(PROPRIETAIRE, op="unlend", trigger_id=7)
    assert forfait["agent"]["forfait_prete_model"] is None
    assert _refus(EQUIPIER, op="update", trigger_id=7, input="x").code == \
        "subscription_personal_only"
    _appel(EDITEUR, op="update", trigger_id=7, input="y")
    assert forfait["ecrit"] == {"input": "y"}


def test_une_page_de_travaux_lit_ses_agents_en_UNE_lecture(org, monkeypatch):
    """Un `get_trigger` par agent de la page empruntait le pool autant de fois :
    la page lit ses agents d'un coup, quel que soit leur nombre."""
    lus = []
    monkeypatch.setattr(A.db, "porteurs_des_agents",
                        lambda ids, o: lus.append(list(ids)) or
                        [{"id": i, "org_id": o, "sub": PROPRIETAIRE} for i in ids])
    monkeypatch.setattr(A.db, "get_trigger",
                        lambda i, o: pytest.fail("une lecture par agent"))
    jobs = [{"id": n, "payload": {"trigger_id": 100 + n % 9, "input": "x"}} for n in range(40)]
    assert A.masquer_charges(ResolvedCtx(sub=PROPRIETAIRE, org_id=ORG), jobs) == jobs
    assert lus == [sorted({100 + n % 9 for n in range(40)})]
