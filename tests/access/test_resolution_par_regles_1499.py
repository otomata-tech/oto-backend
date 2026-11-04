"""La résolution d'un compte par les RÈGLES (#1499, étape 3, lot L3).

Sans base : le coffre et les règles sont doublés (`access.comptes_des_lignes`,
`access.comptes_d_autrui`), le reste de la résolution est le vrai chemin. Ce qu'on tient :
- l'ordre : le compte nommé, puis le sien, puis l'équipe ACTIVE, puis l'org ;
- un nom introuvable lève ; le compte d'autrui ne sert que nommé ;
- les décisions : D1 (entre orgs, coupé, en attente), D2 (équipe, non-membre coupé,
  tranchée), D3 (ambiguïté, sauf défaut, en attente) ;
- `services` d'une règle Google ; une règle `none` ferme ; le niveau exigé par l'op ;
- le cas de la mesure : un compte d'org repris (règle d'org `sensitive`) sert un envoi.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, db, group_store, org_store, session_org
from oto_mcp import tenant_vault
from oto_mcp.access import niveaux
from oto_mcp.mcp_errors import McpError

ORG, AUTRE_ORG, EQUIPE = 7, 8, 3
MOI, COLLEGUE = "moi", "collegue"


def _code(exc) -> str:
    return (getattr(getattr(exc, "error", None), "data", None) or {}).get("code")


class _Monde:
    """Le coffre (`(etype, eid, connector, account) → secret`), les règles par ligne, les
    défauts, et ce qui se lit des rôles."""

    def __init__(self):
        self.coffre: dict = {}
        self.regles: dict = {}
        self.defauts: set = set()
        self.admins: set = set()
        self.equipes_de: dict = {MOI: {EQUIPE}}
        self.defaut_par: dict = {}

    def poser(self, etype, eid, connector, account="", *, regles=(), defaut=False,
              defaut_par=None):
        self.coffre[(etype, str(eid), connector, account)] = f"K-{etype}-{eid}-{account}"
        self.regles[(etype, str(eid), account)] = tuple(regles)
        if defaut:
            self.defauts.add((etype, str(eid), account))
            self.defaut_par[(etype, str(eid), account)] = defaut_par

    def comptes(self, porteur, lignes):
        out = {}
        for etype, eid, account in lignes:
            if (etype, eid, porteur, account) not in self.coffre:
                continue
            org = (int(eid.partition(":")[0]) if etype == "member"
                   else int(eid) if etype == "org" else ORG)
            out[(etype, eid, account)] = niveaux.Compte(
                instance_id=hash((etype, eid, account)) % 10_000, owner_type=etype,
                owner_id=eid, connector=porteur, account=account, org_id=org,
                regles=self.regles.get((etype, eid, account), ()),
                default_par=self.defaut_par.get((etype, eid, account)),
                org_nom={ORG: "acme", AUTRE_ORG: "prêteuse"}.get(org))
        return out

    def hors_org(self, connector, org, sub, account=None):
        """Les comptes d'autres orgs : les siens (#172), ceux qu'une règle qui le nomme
        lui ouvre (D1)."""
        out = []
        for (etype, eid, con, acc) in self.coffre:
            if (connector is not None and con != connector) or (
                    account is not None and acc != account):
                continue
            c = self.comptes(con, [(etype, eid, acc)])[(etype, eid, acc)]
            if c.org_id == org:
                continue
            if c.proprietaire == sub or any(r.user == sub and r.level != "none"
                                            for r in c.regles):
                out.append(c)
        return out

    def autrui(self, connector, org, sub, account):
        return [c for (etype, eid, con, acc) in self.coffre
                if etype == "member" and con == connector and acc == account
                and eid.startswith(f"{org}:") and eid != f"{org}:{sub}"
                for c in self.comptes(connector, [(etype, eid, acc)]).values()]


@pytest.fixture
def monde(monkeypatch):
    m = _Monde()
    multi = {"google", "zoho"}
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: EQUIPE)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda p: None)
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    from oto_mcp import providers
    monkeypatch.setattr(access, "_is_multi_account",
                        lambda p, org=None: providers.credential_provider(p) in multi)
    monkeypatch.setattr(access, "comptes_des_lignes", m.comptes)
    monkeypatch.setattr(access, "comptes_d_autrui", m.autrui)
    monkeypatch.setattr(access, "comptes_hors_org", m.hors_org)

    def _lignes(etype, eid, connector):
        return [{"account": a, "meta": {"is_default": (e, i, a) in m.defauts}}
                for (e, i, c, a) in m.coffre if (e, i, c) == (etype, str(eid), connector)]
    monkeypatch.setattr(credentials_store, "list_accounts", _lignes)
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda e, i, c, account=None: any(
                            (k[0], k[1], k[2]) == (e, str(i), c) for k in m.coffre))
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda e, i, c, account="": m.coffre.get((e, str(i), c, account or "")))
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda s, o, c, account="": m.coffre.get(
                            ("member", f"{o}:{s}", c, account or "")))
    monkeypatch.setattr(db, "has_member_api_key",
                        lambda s, o, c: any(k[:3] == ("member", f"{o}:{s}", c) for k in m.coffre))
    monkeypatch.setattr(db, "member_instance_suspended", lambda *a: False)
    monkeypatch.setattr(group_store, "get_group_secret",
                        lambda g, c, account="": m.coffre.get(("group", str(g), c, account)))
    monkeypatch.setattr(org_store, "get_org_secret",
                        lambda o, c, account="": m.coffre.get(("org", str(o), c, account)))
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda s, g: g in m.equipes_de.get(s, ()))
    monkeypatch.setattr(group_store, "get_group", lambda g: {"id": g, "name": "ventes",
                                                             "org_id": ORG})
    monkeypatch.setattr(org_store, "get_org", lambda o: {"id": o, "name": "acme"})
    from oto_mcp import account_suspension, roles
    monkeypatch.setattr(roles, "is_org_admin", lambda s, o: s in m.admins)
    monkeypatch.setattr(roles, "can_admin_group", lambda s, g: s in m.admins)
    monkeypatch.setattr(roles, "is_org_member",
                        lambda s, o: o == ORG or (o == AUTRE_ORG and s == COLLEGUE))
    monkeypatch.setattr(org_store, "get_org_role",
                        lambda o, s: "org_member" if (o == ORG or (o == AUTRE_ORG and s in
                                                      (COLLEGUE, MOI))) else None)
    monkeypatch.setattr(account_suspension, "etat", lambda s: None)
    monkeypatch.setattr(db, "emails_by_subs", lambda subs: {s: f"{s}@x.test" for s in subs})
    monkeypatch.setattr(tenant_vault, "rung_tenant", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(credentials_store, "list_all_platform_instances", lambda: {})
    return m


@pytest.fixture
def appel():
    """Pose le relevé d'un appel d'outil dont l'op est de la classe donnée."""
    from oto_mcp.tools import effets
    toks = []

    def poser(classe: str):
        effets.EFFETS["essai_outil_1499"] = classe
        toks.append(access.noter_l_appel("essai_outil_1499", {}))
    yield poser
    effets.EFFETS.pop("essai_outil_1499", None)
    for t in reversed(toks):
        access.oublier_l_appel(t)


def _regle(**k):
    return niveaux.Regle(user=k.get("user"), group=k.get("group"), org=k.get("org", False),
                         level=k["level"], services=k.get("services"))


def _servi(provider="zoho", **k):
    rc = access.resolve_credential(provider, sub=MOI, emit_on_failure=False, **k)
    return rc.entity_type, rc.entity_id, rc.account


# ── L'ordre ─────────────────────────────────────────────────────────────────────

def test_le_sien_avant_l_equipe_avant_l_org(monde):
    monde.poser("org", ORG, "zoho", "o")
    monde.poser("group", EQUIPE, "zoho", "e")
    monde.poser("member", f"{ORG}:{MOI}", "zoho", "m")
    assert _servi() == ("member", f"{ORG}:{MOI}", "m")
    del monde.coffre[("member", f"{ORG}:{MOI}", "zoho", "m")]
    assert _servi() == ("group", str(EQUIPE), "e")
    del monde.coffre[("group", str(EQUIPE), "zoho", "e")]
    assert _servi() == ("org", str(ORG), "o")


def test_le_compte_nomme_l_emporte(monde):
    monde.poser("member", f"{ORG}:{MOI}", "zoho", "m")
    monde.poser("org", ORG, "zoho", "o", regles=[_regle(org=True, level="sensitive")])
    assert _servi(account="o") == ("org", str(ORG), "o")


def test_un_nom_introuvable_leve(monde):
    monde.poser("org", ORG, "zoho", "o")
    with pytest.raises(access.CompteIntrouvable):
        _servi(account="nulle-part")


def test_l_equipe_non_active_ne_sert_pas_sans_etre_nommee(monde, monkeypatch):
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monde.poser("group", EQUIPE, "zoho", "e")
    with pytest.raises(McpError):
        _servi()


# ── Le compte d'autrui ─────────────────────────────────────────────────────────

def test_le_compte_d_autrui_ne_sert_que_nomme(monde):
    monde.poser("member", f"{ORG}:{COLLEGUE}", "zoho", "sa-boite",
                regles=[_regle(user=MOI, level="sensitive")])
    with pytest.raises(McpError):
        _servi()                                   # jamais par défaut
    assert _servi(account="sa-boite") == ("member", f"{ORG}:{COLLEGUE}", "sa-boite")


def test_le_compte_d_autrui_sans_regle_est_introuvable(monde):
    monde.poser("member", f"{ORG}:{COLLEGUE}", "zoho", "sa-boite")
    with pytest.raises(access.CompteIntrouvable):
        _servi(account="sa-boite")


def test_le_compte_d_autrui_sous_le_niveau_est_refuse_en_le_nommant(monde, appel):
    monde.poser("member", f"{ORG}:{COLLEGUE}", "zoho", "sa-boite",
                regles=[_regle(user=MOI, level="read")])
    appel("envoyer")
    with pytest.raises(access.NiveauInsuffisant) as e:
        _servi(account="sa-boite")
    d = e.value.error.data
    assert (d["code"], d["account"], d["level"], d["required"]) == (
        "insufficient_level", "sa-boite", "read", "sensitive")
    assert d["managed_by"] == f"{COLLEGUE}@x.test" and d["status"] == 403


# ── D1, D2, D3 ──────────────────────────────────────────────────────────────────

def test_d1_seule_une_regle_nominative_traverse_les_orgs(monde):
    appelant = niveaux.Appelant(MOI, ORG, EQUIPE)
    prete = niveaux.Compte(1, "member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "x", AUTRE_ORG,
                           regles=(_regle(user=MOI, level="write"),))
    assert niveaux.niveau(appelant, prete) == "write"
    # Ni une règle d'org, ni d'équipe, ni un rôle ne traversent les orgs.
    d_org = niveaux.Compte(2, "org", str(AUTRE_ORG), "zoho", "x", AUTRE_ORG,
                           regles=(_regle(org=True, level="sensitive"),
                                   _regle(group=EQUIPE, level="sensitive")))
    monde.admins.add(MOI)
    assert niveaux.niveau(appelant, d_org) == "none"
    # Sa propre clé posée dans une autre org (#172) : il la gère.
    sienne = niveaux.Compte(3, "member", f"{AUTRE_ORG}:{MOI}", "zoho", "x", AUTRE_ORG)
    assert niveaux.niveau(appelant, sienne) == "manage"


def test_d1_le_compte_prete_hors_org_sert_nomme_au_niveau_de_la_regle(monde, appel):
    monde.poser("member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "sa-boite",
                regles=[_regle(user=MOI, level="write")])
    appel("ecrire")
    toks = session_org.set_call_trace({})
    try:
        assert _servi(account="sa-boite") == ("member", f"{AUTRE_ORG}:{COLLEGUE}",
                                              "sa-boite")
        assert session_org.current_call_trace()["compte_d_une_autre_org"] == {
            "org": "prêteuse", "level": "write", "own": False}
    finally:
        session_org.reset_call_trace(toks)
    appel("envoyer")          # la garde vaut au niveau de la règle
    with pytest.raises(access.NiveauInsuffisant) as e:
        _servi(account="sa-boite")
    d = e.value.error.data
    assert (d["level"], d["required"]) == ("write", "sensitive")
    assert d["managed_by"] == "whoever manages it in the org prêteuse"


def test_d1_sans_nom_jamais_mais_liste_parmi_les_candidats(monde, appel):
    monde.poser("member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "sa-boite",
                regles=[_regle(user=MOI, level="sensitive")])
    appel("lire")
    with pytest.raises(access.CredentialUnavailable) as e:
        _servi()
    assert "`sa-boite` lent by prêteuse, level sensitive" in e.value.error.message
    assert e.value.error.data["to_name"][0]["lent_by_org"] == "prêteuse"
    # Avec deux comptes d'org ambigus, le refus le liste aussi.
    monde.poser("org", ORG, "zoho", "fr")
    monde.poser("org", ORG, "zoho", "us")
    with pytest.raises(access.CompteAmbigu) as e:
        _servi()
    assert [c["label"] for c in e.value.error.data["to_name"]] == ["sa-boite"]
    assert "lent by prêteuse" in e.value.error.message


def test_d1_un_nom_porte_par_deux_comptes_hors_org_est_ambigu(monde):
    monde.poser("member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "boite",
                regles=[_regle(user=MOI, level="read")])
    monde.poser("member", f"{AUTRE_ORG}:{MOI}", "zoho", "boite")       # la sienne, #172
    with pytest.raises(access.CompteAmbigu):
        _servi(account="boite")


def test_d1_le_nom_se_cherche_d_abord_dans_l_org_de_l_appel(monde):
    monde.poser("org", ORG, "zoho", "boite")
    monde.poser("member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "boite",
                regles=[_regle(user=MOI, level="read")])
    assert _servi(account="boite") == ("org", str(ORG), "boite")


def test_172_sa_cle_d_une_autre_org_sert_nommee(monde):
    monde.poser("member", f"{AUTRE_ORG}:{MOI}", "zoho", "ma-cle")
    assert _servi(account="ma-cle") == ("member", f"{AUTRE_ORG}:{MOI}", "ma-cle")


def test_172_sans_nom_refusee_avec_le_geste(monde):
    monde.poser("member", f"{AUTRE_ORG}:{MOI}", "zoho", "ma-cle")
    with pytest.raises(access.CredentialUnavailable) as e:
        _servi()
    m = e.value.error.message
    assert "your own account `ma-cle` in the org prêteuse" in m
    assert "name it, or set the key in this org" in m
    assert e.value.error.data["to_name"][0]["your_account_in_org"] == "prêteuse"


def test_d2_un_compte_d_equipe_ne_sert_pas_a_un_non_membre(monde):
    compte = niveaux.Compte(1, "group", "99", "zoho", "", ORG)
    assert niveaux.niveau(niveaux.Appelant(MOI, ORG, EQUIPE), compte) == "none"
    membre = niveaux.Compte(2, "group", str(EQUIPE), "zoho", "", ORG)
    assert niveaux.niveau(niveaux.Appelant(MOI, ORG, EQUIPE), membre) == "sensitive"


def test_d3_deux_comptes_a_la_meme_etape_sont_ambigus(monde):
    monde.poser("org", ORG, "zoho", "fr")
    monde.poser("org", ORG, "zoho", "us")
    with pytest.raises(access.CompteAmbigu) as e:
        _servi()
    d = e.value.error.data
    assert d["code"] == "ambiguous_account"
    assert {c["label"] for c in d["accounts"]} == {"fr", "us"}
    assert "_account=" in e.value.error.message


def test_d3_un_defaut_tranche(monde):
    monde.poser("org", ORG, "zoho", "fr")
    monde.poser("org", ORG, "zoho", "us", defaut=True)          # d'avant : sans auteur
    assert _servi() == ("org", str(ORG), "us")


def test_d3_un_defaut_pose_par_un_gestionnaire_tranche(monde):
    monde.admins.add("l_admin")
    monde.poser("org", ORG, "zoho", "fr")
    monde.poser("org", ORG, "zoho", "us", defaut=True, defaut_par="l_admin")
    assert _servi() == ("org", str(ORG), "us")


def test_d3_un_defaut_pose_par_qui_ne_gere_pas_ne_tranche_pas(monde):
    monde.poser("org", ORG, "zoho", "fr")
    monde.poser("org", ORG, "zoho", "us", defaut=True, defaut_par="un_membre")
    with pytest.raises(access.CompteAmbigu):
        _servi()


def test_d3_un_defaut_sous_le_niveau_ne_tranche_pas(monde, appel):
    """Le défaut ne départage que les comptes au niveau exigé : sous ce niveau, il ne
    sert pas, et l'autre compte (seul au niveau) sert."""
    monde.poser("org", ORG, "zoho", "fr", regles=[_regle(org=True, level="sensitive")])
    monde.poser("org", ORG, "zoho", "us", defaut=True)
    appel("envoyer")
    assert _servi() == ("org", str(ORG), "fr")


def test_d3_le_defaut_ne_passe_pas_devant_le_compte_personnel(monde):
    monde.poser("member", f"{ORG}:{MOI}", "zoho", "le-mien")
    monde.poser("org", ORG, "zoho", "us", defaut=True)
    assert _servi() == ("member", f"{ORG}:{MOI}", "le-mien")


def test_oto_context_dit_les_comptes_a_nommer_et_les_defauts(monde, monkeypatch):
    monde.poser("member", f"{AUTRE_ORG}:{COLLEGUE}", "zoho", "sa-boite",
                regles=[_regle(user=MOI, level="read")])
    monde.poser("member", f"{AUTRE_ORG}:{MOI}", "zoho", "ma-cle")
    monde.poser("org", AUTRE_ORG, "zoho", "pas-pour-moi")      # rien ne me l'ouvre
    monkeypatch.setattr(access, "defauts_des_etapes", lambda o, g: [
        {"entity_type": "org", "entity_id": str(ORG), "connector": "zoho", "account": "us",
         "default_par": "l_admin", "default_le": "2026-11-04T10:00:00+00:00"}])
    vu = access.comptes_du_contexte(MOI, ORG, EQUIPE)
    assert sorted((c["label"], c.get("lent_by_org"), c.get("your_account_in_org"),
                   c["level"]) for c in vu["to_name"]) == [
        ("ma-cle", None, "prêteuse", "manage"), ("sa-boite", "prêteuse", None, "read")]
    assert vu["defaults"] == [{"step": "org", "connector": "zoho", "account": "us",
                               "set_by": "l_admin@x.test",
                               "set_at": "2026-11-04T10:00:00+00:00"}]


# ── Les règles ─────────────────────────────────────────────────────────────────

def test_services_d_une_regle_google(monde, appel):
    monde.poser("member", f"{ORG}:{COLLEGUE}", "google", "c@x.test",
                regles=[_regle(user=MOI, level="sensitive", services=("gmail",))])
    appel("lire")
    rc = access.resolve_credential("gmail", sub=MOI, account="c@x.test",
                                   emit_on_failure=False)
    assert rc.entity_id == f"{ORG}:{COLLEGUE}"
    with pytest.raises(McpError):       # Drive n'est pas dans la règle : jamais servi
        access.resolve_credential("drive", sub=MOI, account="c@x.test", emit_on_failure=False)


def test_une_regle_none_ferme_le_compte_d_org(monde, appel):
    monde.poser("org", ORG, "zoho", "o", regles=[_regle(org=True, level="sensitive"),
                                                   _regle(user=MOI, level="none")])
    appel("lire")
    with pytest.raises(access.NiveauInsuffisant) as e:
        _servi()
    assert e.value.error.data["level"] == "none"


def test_la_regle_d_equipe_l_emporte_sur_celle_de_l_org(monde, appel):
    monde.poser("org", ORG, "zoho", "o", regles=[_regle(org=True, level="sensitive"),
                                                   _regle(group=EQUIPE, level="read")])
    appel("ecrire")
    with pytest.raises(access.NiveauInsuffisant):
        _servi()


@pytest.mark.parametrize("classe,ok", [("lire", True), ("ecrire", False),
                                       ("envoyer", False), ("detruire", False)])
def test_compte_d_org_sans_regle_lecture_seule(monde, appel, classe, ok):
    """Le défaut d'oto-core : un membre lit le compte d'org, l'admin le gère."""
    monde.poser("org", ORG, "zoho", "o")
    appel(classe)
    if ok:
        assert _servi() == ("org", str(ORG), "o")
    else:
        with pytest.raises(access.NiveauInsuffisant) as e:
            _servi()
        assert e.value.error.data["managed_by"] == "an admin of the org acme"
        monde.admins.add(MOI)
        assert _servi() == ("org", str(ORG), "o")


@pytest.mark.parametrize("classe,ok", [("lire", True), ("ecrire", True),
                                       ("envoyer", True), ("detruire", True)])
def test_compte_d_equipe_sensible_pour_ses_membres(monde, appel, classe, ok):
    monde.poser("group", EQUIPE, "zoho", "e")
    appel(classe)
    assert _servi() == ("group", str(EQUIPE), "e")


def test_sous_le_niveau_jamais_la_cle_plateforme_a_sa_place(monde, appel):
    """Un compte d'org seulement lisible pour un envoi : refusé en le nommant, jamais la
    clé plateforme qui servirait derrière lui."""
    monde.poser("org", ORG, "zoho", "o")
    appel("envoyer")
    with pytest.raises(access.NiveauInsuffisant) as e:
        _servi()
    assert e.value.error.data["account"] == "o"


def test_cas_de_la_mesure_compte_d_org_repris_sert_un_envoi_gmail(monde, appel):
    """La mesure (L2) a vu 1 personne × compte au niveau calculé inférieur à l'exercé. Sa
    forme probable : un compte d'org Google (règle d'org `sensitive` reprise) appelé par
    un service (`gmail`), que la mesure n'a pas rapporté à l'instance du porteur et a
    jugé au défaut `read`. La résolution juge l'instance de la ligne de coffre du porteur :
    la règle reprise s'applique, l'envoi passe."""
    monde.poser("org", ORG, "google", "boite@x.test",
                regles=[_regle(org=True, level="sensitive")])
    appel("envoyer")
    rc = access.resolve_credential("gmail", sub=MOI, emit_on_failure=False)
    assert (rc.entity_type, rc.account) == ("org", "boite@x.test")


def test_l_identifiant_du_compte_servi_va_au_journal(monde):
    monde.poser("org", ORG, "zoho", "o")
    toks = session_org.set_call_trace({})
    try:
        _servi()
        trace = session_org.current_call_trace()
    finally:
        session_org.reset_call_trace(toks)
    attendu = monde.comptes("zoho", [("org", str(ORG), "o")])[("org", str(ORG), "o")]
    assert trace["instance_id"] == attendu.instance_id and trace["key_mode"] == "org"


def test_une_op_sans_classe_declaree_leve(monde):
    monde.poser("org", ORG, "zoho", "o")
    tok = access.noter_l_appel("outil_jamais_declare_1499", {})
    try:
        with pytest.raises(McpError) as e:
            _servi()
    finally:
        access.oublier_l_appel(tok)
    assert _code(e.value) == "effect_undeclared"
