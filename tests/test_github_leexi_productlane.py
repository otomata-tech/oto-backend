"""Connecteurs GitHub, Leexi et Productlane — les trois câblés le 2026-09-02.

Les tripwires génériques couvrent déjà le registre (`test_providers_registry_snapshot`),
l'éditeur (`test_connector_publisher`), le logo (`test_connector_logos`), la
dérivation des modules (`test_capabilities_drift`), la prose servie
(`test_docstring_prose_served`) et la jointure aux clients oto-core
(`test_tools_client_methods_exist`). Ce fichier verrouille donc ce qui leur est
PROPRE, c'est-à-dire les endroits où ces trois modules ne se contentent pas de
passer le plat :

- les **deux dry-run par défaut** (diffusion de changelog, déclenchement de
  workflow) — les seuls gestes de ces connecteurs qui sortent de l'organisation,
  au même titre que l'envoi d'email de `lightfield` ;
- le filtre **« une PR est une issue »**, sans lequel compter les tickets d'un
  dépôt donne un nombre faux ;
- la **traduction du 404 GitHub**, qui doit parler de DROITS et pas d'un nom mal
  orthographié ;
- les **sondes de connexion**, dont le choix d'endpoint est un arbitrage (Leexi :
  `/calls` et non `/users`, parce qu'une clé neuve n'a que `read_calls`) ;
- les refus d'`op` et les arguments requis, qui doivent NOMMER ce qui manque.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of

from _dep_versions import trop_vieux

EXPECTED = {
    "leexi": {"leexi_calls", "leexi_notes", "leexi_meetings", "leexi_users",
              "leexi_teams"},
    "productlane": {"productlane_threads", "productlane_contacts",
                    "productlane_companies", "productlane_roadmap",
                    "productlane_changelogs", "productlane_docs",
                    "productlane_tags", "productlane_workspace"},
    "github": {"github_repos", "github_files", "github_issues", "github_pulls",
               "github_orgs", "github_actions", "github_search"},
}


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("k", False))
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {
            "key_id": "id", "key_secret": "s", "token": "t", "base_url": ""})


def _tools(module_name):
    from fastmcp import FastMCP
    from oto_mcp import tools as _t

    mod = __import__(f"oto_mcp.tools.{module_name}", fromlist=["register"])
    m = FastMCP("t")
    mod.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _call(module_name, tool_name, **kw):
    """Appelle un tool comme le ferait le protocole, et rend son CONTENU.

    `Tool.run` rend un `ToolResult` : lire `structured_content` plutôt que
    l'objet, sinon on teste l'enveloppe de fastmcp et pas notre sortie.
    """
    tool = _tools(module_name)[tool_name]
    res = asyncio.run(tool.run(kw))
    contenu = getattr(res, "structured_content", None)
    return contenu if contenu is not None else res


class _Fake:
    """Client oto-core simulé : enregistre les appels, rend une forme plausible.

    ⚠️ `search_is_truncated` est DÉLÉGUÉ au vrai client : c'est de la logique
    pure (lire `incomplete_results` et comparer `total_count` au plafond de
    1 000), pas un appel réseau. La simuler ferait passer le test sans jamais
    exercer la règle qu'il prétend verrouiller.
    """

    def __init__(self, retour=None):
        self.calls = []
        self._retour = retour if retour is not None else {"data": [], "page": {}}

    @staticmethod
    def search_is_truncated(payload):
        # ⚠️ Importé depuis le MIXIN, pas depuis `client.GitHubClient` : ce
        # dernier est justement la cible du patch, donc l'y chercher rendrait le
        # mock et non la règle. Le mixin, lui, n'est pas patché.
        from oto.tools.github._api.search import _SearchMixin
        return _SearchMixin.search_is_truncated(payload)

    def __getattr__(self, name):
        def _call(*a, **kw):
            self.calls.append((name, a, kw))
            return self._retour
        return _call


def _with(chemin, fake):
    return patch(chemin, return_value=fake)


# --- registre ------------------------------------------------------------------

@pytest.mark.parametrize("nom,categorie,editeur,domaine", [
    ("github", "Dev", "GitHub", "github.com"),
    ("leexi", "Knowledge", "Leexi", "leexi.ai"),
    ("productlane", "Métier", "Productlane", "productlane.com"),
])
def test_les_trois_sont_des_connecteurs_byo_hors_socle(nom, categorie, editeur,
                                                       domaine):
    c = providers.REGISTRY[nom]
    assert c.kind == "tools"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    # Aucun mode plateforme : ce sont les données (ou l'identité) du client.
    assert "platform" not in c.auth_modes
    assert c.default_active is False              # deny-by-default, hors socle
    assert c.category == categorie
    assert c.publisher_name == editeur
    assert providers._LOGO_DOMAIN_BY_CONNECTOR[nom] == domaine


def test_productlane_est_keyed_les_deux_autres_sont_multi_champs():
    """`resolve_api_key` ne rend qu'UNE valeur : un connecteur à deux champs doit
    passer par `resolve_credential_fields`, sinon la seconde valeur est perdue."""
    assert providers.REGISTRY["productlane"].keyed is True
    assert "productlane" in providers.KEY_PROVIDERS
    for nom in ("github", "leexi"):
        c = providers.REGISTRY[nom]
        assert c.keyed is False, f"{nom} est multi-champs : pas de keyed"
        assert c.secret_kind == "fields"
        assert len(c.secret_fields) == 2


def test_le_jeton_github_est_secret_mais_pas_son_url_denterprise():
    champs = {f.name: f for f in providers.REGISTRY["github"].secret_fields}
    assert champs["token"].secret is True
    # La base_url est un réglage d'instance, pas un secret : la rendre lisible
    # permet de VOIR quel GitHub Enterprise est visé.
    assert champs["base_url"].secret is False
    assert champs["base_url"].required is False


def test_lidentifiant_de_cle_leexi_est_lisible_son_secret_non():
    champs = {f.name: f for f in providers.REGISTRY["leexi"].secret_fields}
    assert champs["key_id"].secret is False
    assert champs["key_secret"].secret is True


@pytest.mark.parametrize("nom", ["github", "leexi", "productlane"])
def test_chacun_a_une_fiche_dembarquement(nom):
    kinds = {s.kind for s in providers.REGISTRY[nom].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ----------------------------------------------------------------

@pytest.mark.parametrize("module,attendus", sorted(EXPECTED.items()))
def test_la_surface_servie_est_celle_attendue(module, attendus):
    noms = set(_tools(module))
    assert noms == attendus


@pytest.mark.parametrize("module,attendus", sorted(EXPECTED.items()))
def test_chaque_outil_porte_une_description(module, attendus):
    """Régression du piège « docstring qui n'en est pas un » : un tool sans
    description est un tool que le modèle ne saura pas choisir."""
    for nom, tool in _tools(module).items():
        assert (tool.description or "").strip(), f"{nom} sans description"


@pytest.mark.parametrize("module,attendus", sorted(EXPECTED.items()))
def test_le_namespace_derive_bien_du_nom_de_chaque_outil(module, attendus):
    """Le gate de visibilité résout au plus long préfixe DÉCLARÉ : un nom d'outil
    dont le namespace ne matche pas ferait fail-open."""
    declares = set(providers.REGISTRY[module].namespaces)
    for nom in attendus:
        assert namespace_of(nom) in declares, nom


@pytest.mark.parametrize("module", sorted(EXPECTED))
def test_chacun_enregistre_une_sonde_de_connexion(module):
    assert connector_verify.supports(module)


# --- les deux dry-run par défaut -------------------------------------------------

def test_la_diffusion_de_changelog_est_en_dry_run_par_defaut():
    """C'est le seul appel de Productlane qui écrit à des TIERS (email aux
    abonnés, Slack), sans annulation ni rappel possible."""
    fake = _Fake()
    with _with("oto.tools.productlane.client.ProductlaneClient", fake):
        out = _call("productlane", "productlane_changelogs",
                    op="broadcast", changelog_id="c1", email=True)
    assert out["dry_run"] is True
    assert "email" in out["canaux"]
    # et surtout : RIEN n'est parti
    assert fake.calls == []


def test_la_diffusion_part_vraiment_quand_on_le_demande():
    fake = _Fake()
    with _with("oto.tools.productlane.client.ProductlaneClient", fake):
        _call("productlane", "productlane_changelogs", op="broadcast",
              changelog_id="c1", email=True, dry_run=False)
    assert [c[0] for c in fake.calls] == ["broadcast_changelog"]


def test_la_diffusion_sans_canal_est_refusee_avant_tout_appel():
    fake = _Fake()
    with _with("oto.tools.productlane.client.ProductlaneClient", fake):
        with pytest.raises(McpError, match="au moins un canal"):
            _call("productlane", "productlane_changelogs", op="broadcast",
                  changelog_id="c1", dry_run=False)
    assert fake.calls == []


def test_le_declenchement_de_workflow_est_en_dry_run_par_defaut():
    """`dispatch` lance une exécution réelle — donc potentiellement un déploiement."""
    fake = _Fake()
    with _with("oto.tools.github.client.GitHubClient", fake):
        out = _call("github", "github_actions", op="dispatch", owner="o",
                    repo="r", workflow="ci.yml", ref="main")
    assert out["dry_run"] is True
    assert out["workflow"] == "ci.yml" and out["ref"] == "main"
    assert fake.calls == []


def test_le_declenchement_part_vraiment_quand_on_le_demande():
    fake = _Fake()
    with _with("oto.tools.github.client.GitHubClient", fake):
        _call("github", "github_actions", op="dispatch", owner="o", repo="r",
              workflow="ci.yml", ref="main", dry_run=False)
    assert [c[0] for c in fake.calls] == ["dispatch_workflow"]


def test_aucun_autre_outil_nest_en_dry_run_par_defaut():
    """Le dry-run par défaut est une exception réservée à ce qui sort de
    l'organisation : l'étendre par mégarde rendrait des écritures ordinaires
    silencieusement inopérantes."""
    porteurs = set()
    for module in EXPECTED:
        for nom, tool in _tools(module).items():
            props = (getattr(tool, "parameters", None) or {}).get("properties", {})
            if props.get("dry_run", {}).get("default") is True:
                porteurs.add(nom)
    assert porteurs == {"productlane_changelogs", "github_actions"}


# --- « une pull request EST une issue » -------------------------------------------

def test_les_pull_requests_sont_ecartees_des_issues_par_defaut():
    """Sans ce tri, « combien de tickets ouverts ? » rend un nombre faux : l'API
    GitHub mélange issues et PR, et n'offre aucun filtre pour les séparer."""
    fake = _Fake(retour=[{"number": 1},
                         {"number": 2, "pull_request": {"url": "x"}}])
    # le client fait le tri : on vérifie que le tool le lui DEMANDE par défaut
    with _with("oto.tools.github.client.GitHubClient", fake):
        _call("github", "github_issues", op="search", owner="o", repo="r")
    nom, _a, kw = fake.calls[0]
    assert nom == "list_issues"
    assert kw["include_pull_requests"] is False


def test_on_peut_demander_explicitement_les_pull_requests():
    fake = _Fake(retour=[])
    with _with("oto.tools.github.client.GitHubClient", fake):
        _call("github", "github_issues", op="search", owner="o", repo="r",
              include_pull_requests=True)
    assert fake.calls[0][2]["include_pull_requests"] is True


# --- traduction des refus ----------------------------------------------------------

def test_le_404_github_parle_de_DROITS_pas_dun_nom_mal_ecrit():
    """GitHub répond 404 (et non 403) sur une ressource privée hors portée du
    jeton, exprès. Rendre « introuvable » tel quel enverrait chercher une faute
    de frappe là où il manque un scope."""
    from oto_mcp.tools.github import _upstream_message
    from oto.tools.common.errors import UpstreamHTTPError

    msg = _upstream_message(UpstreamHTTPError(404, {"message": "Not Found"},
                                              service="github"))
    assert "jeton" in msg.lower()
    assert "privée" in msg or "PRIVÉE" in msg


def test_le_403_leexi_nomme_les_scopes_qui_engagent_la_facturation():
    from oto_mcp.tools.leexi import _upstream_message
    from oto.tools.common.errors import UpstreamHTTPError

    msg = _upstream_message(UpstreamHTTPError(403, {}, service="leexi"))
    assert "write_users" in msg and "licences" in msg


def test_le_404_leexi_rappelle_la_portee_dacces():
    from oto_mcp.tools.leexi import _upstream_message
    from oto.tools.common.errors import UpstreamHTTPError

    msg = _upstream_message(UpstreamHTTPError(404, {}, service="leexi"))
    assert "portée" in msg


def test_le_401_productlane_nomme_le_piege_de_la_cle_v1():
    from oto_mcp.tools.productlane import _upstream_message
    from oto.tools.common.errors import UpstreamHTTPError

    msg = _upstream_message(UpstreamHTTPError(401, {}, service="productlane"))
    assert "v1" in msg and "v2" in msg


def test_le_request_id_de_productlane_est_relaye():
    """C'est la seule coordonnée qui permet au client de retrouver l'appel dans
    ses propres journaux."""
    from oto_mcp.tools.productlane import _upstream_message
    from oto.tools.common.errors import UpstreamHTTPError

    e = UpstreamHTTPError(422, {"error": {"code": "x", "request_id": "req_42"}},
                          service="productlane")
    assert "req_42" in _upstream_message(e)


# --- sondes de connexion -------------------------------------------------------------

def test_la_sonde_leexi_interroge_les_appels_pas_les_utilisateurs():
    """Une clé neuve ne porte que `read_calls` : sonder `/users` ferait afficher
    rouge sur une clé saine mais volontairement restreinte."""
    fake = _Fake()
    with _with("oto.tools.leexi.client.LeexiClient", fake):
        from oto_mcp.tools.leexi import _verify
        _verify({"key_id": "id", "key_secret": "s"})
    assert [c[0] for c in fake.calls] == ["probe"]


def test_la_sonde_productlane_utilise_me_qui_nexige_aucun_scope():
    fake = _Fake(retour={"scopes": []})
    with _with("oto.tools.productlane.client.ProductlaneClient", fake):
        from oto_mcp.tools.productlane import _verify
        _verify({"key": "k"})
    assert [c[0] for c in fake.calls] == ["me"]


def test_la_sonde_github_refuse_une_reponse_qui_nidentifie_personne():
    """Une URL d'API qui ne pointe pas vers un GitHub peut répondre 200 : la
    sonde exige de savoir QUI est le jeton, pas seulement qu'un serveur a parlé."""
    from oto_mcp.tools.github import _verify
    with _with("oto.tools.github.client.GitHubClient", _Fake(retour={})):
        with pytest.raises(ValueError, match="identifier le compte"):
            _verify({"token": "t", "base_url": ""})


def test_la_sonde_github_passe_quand_le_compte_est_nomme():
    from oto_mcp.tools.github import _verify
    with _with("oto.tools.github.client.GitHubClient",
               _Fake(retour={"login": "octocat"})):
        _verify({"token": "t", "base_url": ""})


# --- refus d'op et arguments requis ---------------------------------------------------

@pytest.mark.skipif(bool(trop_vieux("fastmcp")), reason=str(trop_vieux("fastmcp")))
@pytest.mark.parametrize("module,outil", [
    ("leexi", "leexi_calls"),
    ("productlane", "productlane_threads"),
    ("github", "github_repos"),
])
def test_une_op_inconnue_est_refusee_AVANT_tout_appel(module, outil):
    """L'`op` est typée `Literal` : le schéma la refuse au bord du protocole, en
    listant les valeurs valides, sans que le handler ni le client soient touchés.

    Le `raise _bad_op(...)` en fin de chaque handler reste — il couvre l'appel
    DIRECT (tests, futur dispatch interne) — mais ce n'est pas lui qui parle ici,
    et ce test dit lequel des deux garde la porte.
    """
    from fastmcp.exceptions import ValidationError

    chemins = {"leexi": "oto.tools.leexi.client.LeexiClient",
               "productlane": "oto.tools.productlane.client.ProductlaneClient",
               "github": "oto.tools.github.client.GitHubClient"}
    fake = _Fake()
    with _with(chemins[module], fake):
        with pytest.raises(ValidationError, match="op"):
            _call(module, outil, op="nawak", owner="o", repo="r")
    assert fake.calls == []


def test_lire_des_notes_sans_appel_est_refuse():
    with _with("oto.tools.leexi.client.LeexiClient", _Fake()):
        with pytest.raises(McpError, match="call_uuid"):
            _call("leexi", "leexi_notes", op="list")


def test_ecrire_un_fichier_github_sans_message_est_refuse():
    with _with("oto.tools.github.client.GitHubClient", _Fake()):
        with pytest.raises(McpError, match="message"):
            _call("github", "github_files", op="write", owner="o", repo="r",
                  path="a.txt", content="x")


def test_une_operation_de_depot_sans_owner_est_refusee():
    with _with("oto.tools.github.client.GitHubClient", _Fake()):
        with pytest.raises(McpError, match="owner"):
            _call("github", "github_repos", op="get", repo="r")


def test_la_fusion_dentreprise_exige_la_source_absorbee():
    with _with("oto.tools.productlane.client.ProductlaneClient", _Fake()):
        with pytest.raises(McpError, match="source_id"):
            _call("productlane", "productlane_companies", op="merge",
                  company_id="c1")


# --- la troncature de recherche est DITE, pas devinée ----------------------------------

def test_la_recherche_github_annonce_sa_troncature():
    """`total_count` peut annoncer 12 000 quand seuls 1 000 sont récupérables :
    sans ce drapeau, l'agent lit le compteur comme une promesse."""
    fake = _Fake(retour={"total_count": 5000, "items": [{"id": 1}]})
    with _with("oto.tools.github.client.GitHubClient", fake):
        out = _call("github", "github_search", op="repos", q="oto")
    assert out["troncature"]["tronque"] is True

    fake = _Fake(retour={"total_count": 3, "items": [{"id": 1}]})
    with _with("oto.tools.github.client.GitHubClient", fake):
        out = _call("github", "github_search", op="repos", q="oto")
    assert out["troncature"]["tronque"] is False


def test_la_recherche_sans_terme_est_refusee():
    with _with("oto.tools.github.client.GitHubClient", _Fake()):
        with pytest.raises(McpError, match="`q` requis"):
            _call("github", "github_search", op="code")
