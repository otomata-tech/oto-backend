"""Pas de patron ⟹ pas de lien. Et une redirection aboutit toujours.

La correction précédente faisait suivre l'ADRESSE au tenant. Le code du partenaire a
montré que ça ne suffit pas : **aucun de ses chemins ne ressemble aux nôtres**
(`/network/<org>/knowledge/<id>` contre `/docs/<id>`), et il n'a **aucun équivalent**
de nos tableaux. Coller nos chemins sous son domaine aurait fabriqué des liens morts —
pire qu'un lien à notre marque, parce qu'un lien mort ne se diagnostique pas.
"""
from __future__ import annotations

import pytest

from oto_mcp import config, links, tenancy


@pytest.fixture
def registre():
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[
            # Un partenaire calqué sur le vrai : ses chemins portent l'org, et il n'a
            # ni vue tableau ni page publique.
            {"slug": "acme", "issuer": "https://auth.acme.test/oidc",
             "dashboard_url": "https://app.acme.test",
             "link_paths": {"doc": "/network/{org}/knowledge/{id}",
                            "connectors": "/network/{org}/connectors",
                            "project": "/network/{org}/projects/{id}"}},
            # Déclaré mais sans rien : ne doit produire aucun lien, jamais les nôtres.
            {"slug": "beta", "issuer": "https://auth.beta.test/oidc"},
            # oto#63 : un partenaire qui A une page de tableau, et l'ouvre DANS une org.
            {"slug": "gamma", "issuer": "https://auth.gamma.test/oidc",
             "dashboard_url": "https://app.gamma.test",
             "link_paths": {"table": "/org/{org}/tables/{id}"}},
        ])))
    yield
    tenancy.install(avant)


# --- ce que le partenaire a --------------------------------------------------

def test_un_type_declare_utilise_SON_chemin(registre):
    assert links.link_for("doc", sub="acme:u", org=7, id=12) == \
        "https://app.acme.test/network/7/knowledge/12"


def test_le_chemin_de_lorg_est_rempli(registre):
    assert links.link_for("connectors", sub="acme:u", org=42) == \
        "https://app.acme.test/network/42/connectors"


# --- ce qu'il n'a PAS --------------------------------------------------------

def test_un_type_absent_ne_rend_AUCUN_lien(registre):
    """LE cœur du lot : son produit n'a pas de vue tableau. On n'écrit rien plutôt
    que d'envoyer chez nous ou vers une page inexistante."""
    assert links.link_for("table", sub="acme:u", id=203) is None
    assert links.link_for("public_doc", sub="acme:u", token="tok") is None


def test_un_tenant_sans_aucun_patron_ne_rend_rien(registre):
    """Déclarer un tenant ne doit pas lui faire hériter de NOS chemins : ils
    mèneraient à des pages qui n'existent pas chez lui."""
    for kind in ("doc", "table", "project", "connectors"):
        assert links.link_for(kind, sub="beta:u", org=1, id=2) is None, kind


def test_un_patron_sans_adresse_ne_mene_nulle_part(registre):
    entries = tenancy.build("https://auth.oto.ninja/oidc",
                            tenants=[{"slug": "gamma", "issuer": "https://g.test/oidc",
                                      "link_paths": {"doc": "/k/{id}"}}])
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(entries))
    try:
        assert links.link_for("doc", sub="gamma:u", id=1) is None
    finally:
        tenancy.install(avant)


def test_un_parametre_manquant_annule_le_lien(registre):
    """Une adresse à trous (`/network//knowledge/12`) mène à une page d'erreur en se
    faisant passer pour un lien valide. Mieux vaut pas de lien."""
    assert links.link_for("doc", sub="acme:u", id=12) is None       # `org` absent
    assert links.link_for("doc", sub="acme:u", org=None, id=12) is None


# --- nous, inchangés ---------------------------------------------------------

def test_le_tenant_primaire_garde_nos_chemins(registre):
    assert links.link_for("table", sub="bn01jfy76a5n", id=203) == \
        f"{config.dashboard_url()}/data/203"
    assert links.link_for("public_doc", sub="bn01jfy76a5n", token="tok") == \
        f"{config.dashboard_url()}/p/d/tok"


def test_sans_compte_on_garde_nos_chemins(registre):
    assert links.link_for("table", sub=None, id=1) == f"{config.dashboard_url()}/data/1"


def test_un_type_inconnu_ne_rend_rien(registre):
    assert links.link_for("licorne", sub=None) is None


# --- la redirection, elle, aboutit toujours ----------------------------------

def test_une_redirection_replie_sur_la_notre(registre):
    """On ne peut pas « ne pas rediriger » : au retour d'un consentement, le
    navigateur doit atterrir. Voir notre marque une fois vaut mieux qu'une page
    blanche au milieu d'une connexion."""
    url = links.redirect_for("connector_return", sub="acme:u", connector="zoho")
    assert url == f"{config.dashboard_url()}/connectors?connector=zoho"
    assert url  # jamais None, jamais vide


def test_une_redirection_utilise_le_patron_du_tenant_sil_existe(registre):
    url = links.redirect_for("connectors", sub="acme:u", org=7)
    assert url == "https://app.acme.test/network/7/connectors"


def test_une_redirection_sans_type_connu_mene_au_moins_quelque_part(registre):
    assert links.redirect_for("licorne", sub="acme:u") == config.dashboard_url()


# --- les surfaces --------------------------------------------------------------

def test_le_lien_dun_tableau_disparait_chez_un_partenaire_sans_tableaux(registre):
    from oto_mcp.datastore.core import _ns_url
    assert _ns_url(203, "acme:u") is None
    assert _ns_url(203, "bn01jfy76a5n") == f"{config.dashboard_url()}/data/203"


def test_le_lien_public_dune_page_disparait_de_meme(registre):
    from oto_mcp.capabilities.docs.view import public_doc_url
    assert public_doc_url("tok", "acme:u") is None
    assert public_doc_url("tok", "bn01jfy76a5n") == f"{config.dashboard_url()}/p/d/tok"


def test_des_patrons_illisibles_valent_aucun_lien():
    """Une valeur qu'on n'a pas su lire ne doit jamais devenir un lien construit."""
    for valeur in ("pas du json", 42, [], None, {"doc": ""}, {"doc": 12}):
        entries = tenancy.build("https://auth.oto.ninja/oidc",
                                tenants=[{"slug": "delta", "issuer": "https://d.test/oidc",
                                          "dashboard_url": "https://app.d.test",
                                          "link_paths": valeur}])
        avant = tenancy.current()
        tenancy.install(tenancy.IssuerRegistry(entries))
        try:
            assert links.link_for("doc", sub="delta:u", id=1) is None, valeur
        finally:
            tenancy.install(avant)


# --- retour de consentement des connecteurs « fédérés » (folk, atlassian) ------
#
# Ces callbacks sont appelés PAR LE FOURNISSEUR, sans session : le `sub` ne leur
# arrive que par le state signé. Ils l'avaient déjà — ils ne s'en servaient pas, et
# renvoyaient tout le monde à la RACINE du dashboard oto.

def test_le_retour_federe_suit_le_tenant(registre):
    """`connector_return` porte le connecteur : un seul patron sert folk, atlassian
    et les suivants, sans que le tenant ait à en déclarer un par connecteur."""
    assert links.link_for("connector_return", sub="acme:u", org=7,
                          connector="folk") is None  # acme n'a pas cette vue


def test_sans_patron_le_callback_retombe_sur_lhistorique(registre):
    """C'est ce `None` qui fait retomber le callback sur `/?folk=connected`, à
    l'octet près. Un tenant mal configuré ne doit jamais produire un lien MORT —
    l'ancienne destination vaut mieux qu'une page qui n'existe pas."""
    for connecteur in ("folk", "atlassian"):
        assert links.link_for("connector_return", sub="acme:u",
                              connector=connecteur) is None


def test_chez_nous_le_retour_federe_reste_le_notre(registre):
    """Tenant primaire (pas de `sub`, ou le nôtre) : `link_for` sert NOS chemins,
    et le callback garde sa destination historique."""
    # L'adresse par défaut vient de l'env (cutover ADR 0040) : on la DÉRIVE plutôt
    # que de la figer, sinon le test suit le domaine du jour et pas la règle.
    assert links.link_for("connector_return", connector="folk") == \
        f"{config.dashboard_url()}/connectors?connector=folk"


# --- oto#63 : l'org que le patron réclame, et la raison d'un lien absent ----------

def test_un_tableau_s_ouvre_dans_l_org_que_le_patron_reclame(registre):
    from oto_mcp.datastore.core import _ns_url
    assert _ns_url(203, "gamma:u", org=58) == "https://app.gamma.test/org/58/tables/203"
    assert _ns_url(203, "gamma:u") is None, "sans org, le patron ne se remplit pas : aucun lien"


def test_le_patron_dit_ce_qu_il_reclame(registre):
    assert links.patron_reclame("table", "org", sub="gamma:u") is True
    assert links.patron_reclame("table", "org", sub="acme:u") is False    # pas de page
    assert links.patron_reclame("table", "org", sub="bn01jfy76a5n") is False  # `/data/{id}`


@pytest.mark.parametrize("sub, params, attendu", [
    ("acme:u", {"id": 203}, "declares no table page"),
    ("beta:u", {"id": 203}, "declares no table page"),
    ("gamma:u", {"id": 203}, "requires `org`"),
])
def test_un_lien_absent_dit_pourquoi(registre, sub, params, attendu):
    assert links.link_for("table", sub=sub, **params) is None
    raison = links.raison_sans_lien("table", sub=sub, **params)
    assert raison and attendu in raison, raison


def test_un_lien_present_n_a_pas_de_raison(registre):
    assert links.raison_sans_lien("table", sub="gamma:u", id=203, org=58) is None
    assert links.raison_sans_lien("table", sub="bn01jfy76a5n", id=203) is None


def test_la_raison_nomme_le_remede_quand_c_est_l_org_qui_manque(registre):
    assert "`_org=`" in links.raison_sans_lien("table", sub="gamma:u", id=203)


def test_l_org_des_liens_se_resout_UNE_fois_et_seulement_si_le_patron_la_reclame(
        registre, monkeypatch):
    """Une liste de cent tableaux ne doit pas payer cent résolutions d'org — et chez
    nous, qui ouvrons un tableau par son seul id, elle n'en paie aucune."""
    from oto_mcp import access
    from oto_mcp.datastore.registre import RegistreMixin
    appels: list = []
    monkeypatch.setattr(access, "current_org", lambda sub: appels.append(sub) or 58)

    class _Store(RegistreMixin):
        def __init__(self, sub):
            self.sub = sub

    partenaire = _Store("gamma:u")
    assert [partenaire._org_des_liens() for _ in range(3)] == [58, 58, 58]
    assert appels == ["gamma:u"], "résolue une seule fois par geste"
    appels.clear()
    assert _Store("bn01jfy76a5n")._org_des_liens() is None
    assert _Store("acme:u")._org_des_liens() is None
    assert appels == [], "aucune résolution quand le patron ne la réclame pas"



# --- les chemins de L'INSTANCE (OTO_DASHBOARD_LINK_PATHS) ----------------------
#
# Une instance dont le tableau de bord range tout sous l'org servait `/data/{id}`
# pour chaque tableau : une adresse que son tableau de bord ne route pas. Les
# chemins se DÉCLARENT par instance (convention de `docs/tenants.md`), pas par tenant.

_CHEMINS_ORG = ('{"table": "/org/{org}/tables/{id}", '
                '"doc": "/org/{org}/projects/{project_id}/memory/{id}", '
                '"project": "/org/{org}/projects/{id}"}')


def test_l_instance_declare_ses_chemins(registre, monkeypatch):
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, _CHEMINS_ORG)
    base = config.dashboard_url()
    assert links.link_for("table", sub="bn01jfy76a5n", id=203, org=42) == \
        f"{base}/org/42/tables/203"
    assert links.link_for("doc", sub="bn01jfy76a5n", id=5, project_id=7, org=42) == \
        f"{base}/org/42/projects/7/memory/5"
    assert links.link_for("project", sub=None, id=7, org=42) == \
        f"{base}/org/42/projects/7"
    # Un type que l'instance ne redéclare pas garde le chemin par défaut.
    assert links.link_for("public_doc", sub=None, token="tok") == f"{base}/p/d/tok"


def test_un_chemin_d_instance_qui_reclame_l_org_sans_l_avoir_ne_rend_rien(
        registre, monkeypatch):
    """Jamais `/org//tables/203` : sans org, pas de lien, et la raison le dit."""
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, _CHEMINS_ORG)
    assert links.link_for("table", sub="bn01jfy76a5n", id=203) is None
    assert links.patron_reclame("table", "org", sub="bn01jfy76a5n") is True
    assert "`org`" in links.raison_sans_lien("table", sub="bn01jfy76a5n", id=203)


def test_les_chemins_d_instance_ne_touchent_pas_un_tenant_tiers(registre, monkeypatch):
    """Un partenaire garde SES chemins, et n'hérite pas de ceux de l'instance."""
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, _CHEMINS_ORG)
    assert links.link_for("table", sub="acme:u", id=203, org=1) is None
    assert links.link_for("doc", sub="acme:u", org=7, id=12) == \
        "https://app.acme.test/network/7/knowledge/12"


@pytest.mark.parametrize("valeur", ["pas du json", "[]", '{"table": 12}', '"x"'])
def test_une_declaration_d_instance_illisible_leve(registre, monkeypatch, valeur):
    """Jamais nos chemins par défaut en silence : la déclaration illisible lève, au
    démarrage (`verifier_reglages`) comme à l'appel."""
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, valeur)
    with pytest.raises(links.InstancePathsInvalid, match=links.ENV_INSTANCE_PATHS):
        links.verifier_reglages()
    with pytest.raises(links.InstancePathsInvalid):
        links.link_for("table", sub=None, id=1)


def test_un_type_de_lien_inconnu_est_refuse(registre, monkeypatch):
    """Un type qu'aucun appelant ne demande ne peut être qu'une faute de frappe."""
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, '{"tables": "/org/{org}/tables/{id}"}')
    with pytest.raises(links.InstancePathsInvalid, match="tables"):
        links.verifier_reglages()


def test_la_garde_des_chemins_est_appelee_au_demarrage():
    """`server.main` lit la déclaration avant de servir, à côté des autres réglages."""
    import inspect

    from oto_mcp import server

    assert "links.verifier_reglages()" in inspect.getsource(server.main)


def test_sans_declaration_d_instance_rien_ne_change(registre, monkeypatch):
    monkeypatch.delenv(links.ENV_INSTANCE_PATHS, raising=False)
    links.verifier_reglages()
    assert links.instance_paths() is links.DEFAULT_PATHS


def test_le_lien_d_une_app_n_existe_que_declare(registre, monkeypatch):
    """`app` n'a pas de chemin par défaut (notre tableau de bord n'a pas d'apps) : pas
    de lien tant que l'instance ne le déclare pas, puis `/org/{org}/apps/{id}`."""
    monkeypatch.delenv(links.ENV_INSTANCE_PATHS, raising=False)
    assert links.link_for("app", sub=None, org=42, id=6) is None
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS, '{"app": "/org/{org}/apps/{id}"}')
    links.verifier_reglages()
    assert links.link_for("app", sub=None, org=42, id=6) == f"{config.dashboard_url()}/org/42/apps/6"
    assert links.link_for("app", sub=None, id=6) is None, "sans org, pas de lien troué"
