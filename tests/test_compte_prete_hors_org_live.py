"""Un compte prêté HORS ORG (#1499, D1), sur une vraie base : une règle nomme une personne
qui n'est pas membre de l'org du compte. Le compte sert au niveau de la règle, NOMMÉ, ou
SANS NOM quand rien de local ne sert (étape 5) ; le journal dit la règle hors org ; les
travaux de la personne, sous un jeton de délégation verrouillé sur SON org, s'en servent
encore — le verrou interdit d'agir dans une autre org, pas d'être servi d'un compte
qu'une règle lui ouvre.
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import credentials_store, db
    from oto_mcp.db import regles_de_comptes
    from oto_mcp.org_store import members, orgs
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    u = uuid.uuid4().hex[:6]
    preteur, moi = f"preteur_{u}", f"moi_{u}"
    for sub in (preteur, moi):
        db.upsert_user(sub, email=f"{sub}@x.test", name=sub)
    y = orgs.create_org(f"Preteuse {u}", created_by=preteur)
    x = orgs.create_org(f"Maison {u}", created_by=moi)
    members.add_org_member(y, preteur, "org_member")
    members.add_org_member(x, moi, "org_member")
    credentials_store.set_credential("member", f"{y}:{preteur}", "zoho", "s3cret",
                                     set_by=preteur, meta={}, account="sa-boite")
    inst = regles_de_comptes.comptes_des_lignes(
        "zoho", [("member", f"{y}:{preteur}", "sa-boite")])[0]["instance_id"]
    regles_de_comptes.poser_regle(y, inst, subject_user=moi, level="write", acteur=preteur)
    return {"x": x, "y": y, "preteur": preteur, "moi": moi, "inst": inst}


def test_le_journal_dit_la_regle_hors_org(monde):
    from oto_mcp.db import regles_de_comptes
    j = regles_de_comptes.journal_du_compte(monde["inst"])
    assert [(e["action"], e["subject_id"], e["hors_org"]) for e in j] == [
        ("set", monde["moi"], True)]


def _resoudre(monde, **k):
    from oto_mcp import access
    return access.resolve_credential("zoho", sub=monde["moi"], emit_on_failure=False, **k)


def _dans_l_org(monkeypatch, monde):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: monde["x"])
    monkeypatch.setattr(access, "current_group", lambda sub: None)


def test_nomme_ou_sans_nom_faute_de_local(monde, monkeypatch):
    from oto_mcp import credentials_store
    _dans_l_org(monkeypatch, monde)
    prete = ("member", f"{monde['y']}:{monde['preteur']}")
    rc = _resoudre(monde, account="sa-boite")
    assert (rc.entity_type, rc.entity_id) == prete
    rc = _resoudre(monde)
    assert (rc.entity_type, rc.entity_id, rc.account) == (*prete, "sa-boite")
    # Sa propre clé dans l'org de l'appel passe devant.
    credentials_store.set_credential("member", f"{monde['x']}:{monde['moi']}", "zoho",
                                     "locale", set_by=monde["moi"], meta={},
                                     account="la-mienne")
    rc = _resoudre(monde)
    assert (rc.entity_type, rc.entity_id) == ("member", f"{monde['x']}:{monde['moi']}")


def test_172_sa_cle_d_une_autre_org_sert_sans_nom(monde, monkeypatch):
    from oto_mcp import access, credentials_store
    _dans_l_org(monkeypatch, monde)
    credentials_store.set_credential("member", f"{monde['y']}:{monde['moi']}", "payfit",
                                     "sa-cle", set_by=monde["moi"], meta={})
    from oto_mcp.org_store import members
    members.add_org_member(monde["y"], monde["moi"], "org_member")
    rc = access.resolve_credential("payfit", sub=monde["moi"], emit_on_failure=False)
    assert (rc.entity_type, rc.entity_id) == ("member", f"{monde['y']}:{monde['moi']}")


def test_sous_un_jeton_de_delegation_verrouille_sur_son_org(monde, monkeypatch):
    """Un travail de la personne, verrouillé sur l'org X, nomme le compte prêté par Y :
    il sert, dans X. Le pin `_instance=` garde l'org X (il ne la bascule pas vers Y)."""
    from oto_mcp import access, db, instance_refs, verrou_org
    from oto_mcp.capabilities import runner_jobs
    _dans_l_org(monkeypatch, monde)
    job = db.enqueue_job(monde["x"], "start", payload={"procedure": "p", "input": "x"},
                         sub=monde["moi"])
    jeton = runner_jobs._delegue({"id": job["id"], "sub": monde["moi"],
                                  "org_id": monde["x"]}, bail_s=60,
                                 claimant="test")["delegated_token"]
    tok = verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton)))
    try:
        rc = _resoudre(monde, account="sa-boite")
        assert rc.entity_id == f"{monde['y']}:{monde['preteur']}"
        assert _resoudre(monde).entity_id == f"{monde['y']}:{monde['preteur']}"
        ref = instance_refs.parse_ref(instance_refs.make_member_ref(
            monde["y"], monde["preteur"], "zoho", "sa-boite"))
        assert access.guard_instance_access(monde["moi"], ref) == monde["x"]
    finally:
        verrou_org.lever(tok)


def test_le_statut_affiche_dit_le_compte_prete_sans_requete_par_connecteur(monde,
                                                                            monkeypatch):
    """La sonde préchargée des listes lit les comptes d'ailleurs UNE fois : le mode
    affiché de zoho est `elsewhere`, avec l'org prêteuse ; un connecteur sans compte
    d'ailleurs reste ce qu'il était."""
    from oto_mcp import access
    from oto_mcp.access import comptes
    _dans_l_org(monkeypatch, monde)
    lus = []
    reel = comptes.candidats_etape5
    monkeypatch.setattr(access, "candidats_etape5",
                        lambda *a, **k: lus.append(a) or reel(*a, **k))
    sonde = access.preloaded_presence_probe(monde["moi"], org=monde["x"], groups=[])
    assert len(lus) == 1
    assert access.credential_mode_for(monde["moi"], "zoho", org=monde["x"], group=None,
                                      probe=sonde) == "elsewhere"
    vu = access.compte_d_ailleurs(monde["moi"], "zoho", monde["x"], sonde)
    assert list(vu) == ["lent_by_org"] and vu["lent_by_org"].startswith("Preteuse")
    assert sonde.ailleurs("hubspot") is None
    assert len(lus) == 1


def test_la_liste_dit_elsewhere_quand_seule_l_etape_5_sert(monde, monkeypatch):
    """L'écran ne ment pas (#1499) : payfit, servi seulement par SA clé posée dans l'org Y
    (étape 5), porte `credential.level == "elsewhere"` sur sa ligne de la liste — pas une
    ligne sans credential alors que l'appel serait servi. La ligne passe le modèle."""
    import _mcp_app

    from oto_mcp import credentials_store, db
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as S
    from oto_mcp.connectors.credential_presence import CredentialPresence
    from oto_mcp.org_store import members
    assert "payfit" in db.KEY_PROVIDERS        # le statut ne parcourt que ceux-là
    _mcp_app.static_mcp()
    _dans_l_org(monkeypatch, monde)
    members.add_org_member(monde["y"], monde["moi"], "org_member")
    credentials_store.set_credential("member", f"{monde['y']}:{monde['moi']}", "payfit",
                                     "sa-cle", set_by=monde["moi"], meta={})
    out = S._me(ResolvedCtx(sub=monde["moi"], org_id=monde["x"]),
                S.MyConnectorsInput(name="payfit"))
    ligne = next(c for c in out["connectors"] if c["name"] == "payfit")
    assert ligne["credential"]["level"] == "elsewhere"
    assert CredentialPresence(**ligne["credential"]).level == "elsewhere"


# ── Étape 5 : un compte de la MÊME org qu'une règle lui ouvre (#1499) ────────────

def _pret_dans_l_org(monde, sub, account, connector="zoho", **sujet):
    """La clé de `sub` (membre de l'org X) posée et prêtée par une règle de X."""
    from oto_mcp import credentials_store, db
    from oto_mcp.db import regles_de_comptes
    from oto_mcp.org_store import members
    db.upsert_user(sub, email=f"{sub}@x.test", name=sub)
    members.add_org_member(monde["x"], sub, "org_member")
    credentials_store.set_credential("member", f"{monde['x']}:{sub}", connector, "k-" + sub,
                                     set_by=sub, meta={}, account=account)
    inst = regles_de_comptes.comptes_des_lignes(
        connector, [("member", f"{monde['x']}:{sub}", account)])[0]["instance_id"]
    regles_de_comptes.poser_regle(monde["x"], inst, level="sensitive", acteur=sub,
                                  **(sujet or {"subject_user": monde["moi"]}))
    return inst


def test_le_pret_de_l_org_sert_sans_nom_avant_celui_d_une_autre_org(monde, monkeypatch):
    """Rien de local : le compte qu'une collègue lui prête dans SON org sert avant celui
    que Y lui prête ; la réponse dit qui le prête (`lent_by`)."""
    from oto_mcp import session_org
    _dans_l_org(monkeypatch, monde)
    collegue = f"collegue_{monde['moi']}"
    _pret_dans_l_org(monde, collegue, "sa-boite")
    trace: dict = {}
    tok = session_org.set_call_trace(trace)
    try:
        rc = _resoudre(monde)
    finally:
        session_org.reset_call_trace(tok)
    assert (rc.entity_type, rc.entity_id, rc.account) == (
        "member", f"{monde['x']}:{collegue}", "sa-boite")
    assert trace["compte_prete"] == {"account": "sa-boite", "lent_by": f"{collegue}@x.test"}


def test_de_deux_prets_de_l_org_la_regle_la_plus_recente_sert(monde, monkeypatch):
    """La date du PRÊT (la règle, `prete_le`) ordonne, pas l'ordre des comptes — une
    règle sur son équipe active compte comme une règle qui la nomme."""
    from oto_mcp import access, group_store
    from oto_mcp.db._conn import _connect
    _dans_l_org(monkeypatch, monde)
    equipe = group_store.create_group(monde["x"], f"g_{monde['moi']}")
    group_store.add_group_member(equipe, monde["moi"])
    monkeypatch.setattr(access, "current_group", lambda sub: equipe)
    a, b = f"a_{monde['moi']}", f"b_{monde['moi']}"
    _pret_dans_l_org(monde, b, "recent", subject_group=equipe)
    vieux = _pret_dans_l_org(monde, a, "ancien")          # compte plus récent, prêt ancien
    with _connect() as conn:
        conn.execute("UPDATE connector_account_rules SET created_at = now() - "
                     "interval '1 day' WHERE instance_id = %s", (vieux,))
    assert _resoudre(monde).account == "recent"


def test_le_statut_dit_elsewhere_et_qui_prete_dans_l_org(monde, monkeypatch):
    from oto_mcp import access
    _dans_l_org(monkeypatch, monde)
    collegue = f"c_{monde['moi']}"
    _pret_dans_l_org(monde, collegue, "sa-boite")
    sonde = access.preloaded_presence_probe(monde["moi"], org=monde["x"], groups=[])
    assert access.credential_mode_for(monde["moi"], "zoho", org=monde["x"], group=None,
                                      probe=sonde) == "elsewhere"
    assert access.compte_d_ailleurs(monde["moi"], "zoho", monde["x"], sonde) == {
        "lent_by": f"{collegue}@x.test"}
