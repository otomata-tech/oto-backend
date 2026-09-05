"""Deux surfaces de gouvernance côte à côte : l'héritée intacte, la stricte en bêta.

**L'incident, daté.** Le 2026-09-01, #756 a rendu `resource_type` OBLIGATOIRE sur
`oto_resource` / `POST /api/resources`. Le motif était juste — ce champ vaut
`datastore_namespace` par défaut, donc un appelant qui vise un projet et l'omet
interroge silencieusement une autre famille, et sur `transfer`/`share` **agit sur une
autre ressource**. Mais le champ était déclaré sans défaut sur le modèle d'entrée,
donc obligatoire sur **toutes** les op, pas seulement `op=get` : le journal des appels
a montré que de vrais appelants en dépendaient, dont un `op=list` qui serait passé de
« fonctionne » à « refusé » sans préavis. Le lot a été reverté (#774) avant le tag.

**La décision (Alexis, 2026-09-01) : pas de rupture — on DUPLIQUE.** L'héritée
continue de servir son défaut, défaut de conception compris, **documenté comme connu
dans sa description servie** ; la stricte exige le champ et ne se propose qu'aux
comptes à qui un admin a posé l'option `beta`. Les deux vivent en parallèle et les
appelants migrent quand ils veulent — sans date-couperet.

**Ce que ce fichier garde, et pourquoi le premier compte le plus.** Les tests de #756
affirmaient l'INTENTION du lot (« `resource_type` n'a plus de défaut implicite ») :
ils sont passés au vert sur la rupture, et l'ont donc gravée au lieu de l'attraper.
Le cliquet ci-dessous fait l'inverse — il fige ce qu'un appelant REÇOIT, octet pour
octet, et rougirait le jour où quelqu'un referait le geste.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from oto_mcp import openapi, session_visibility as SV
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities import resources_contract as C
from oto_mcp.capabilities import resources_v2 as V2
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.tool_visibility import BETA_OPTION, BETA_TOOLS

EMPREINTE = pathlib.Path(__file__).resolve().parent / "resources_input_legacy.json"


def _cap(cle: str):
    for c in CAPABILITIES:
        if c.key == cle:
            return c
    raise AssertionError(f"capacité absente du registre : {cle}")


# ── 1. Le cliquet : l'héritée ne bouge pas d'un octet ────────────────────────


def _schema_servi(nom: str) -> dict:
    """Le schéma que `tools/list` rend pour `nom`, monté comme le serveur monte.

    ⚠️ On mesure la surface SERVIE, pas `Input.model_json_schema()` : entre les deux
    il y a `apply_flat_signature` et l'injection du paramètre `_org`. Épingler le
    modèle pydantic laisserait passer un changement d'adaptateur, qui est justement
    ce qu'un appelant verrait.
    """
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.capabilities import _mcp_adapter
    from oto_mcp.capabilities import registry as cap_registry
    from oto_mcp.tools import register_all

    mcp = FastMCP("cliquet-resources")
    register_all(mcp)
    _mcp_adapter.register(mcp, cap_registry.CAPABILITIES)
    for t in asyncio.run(mcp.list_tools()):
        if t.name == nom:
            return getattr(t, "parameters", None) or {}
    raise AssertionError(f"outil non monté : {nom}")


def test_le_schema_servi_de_la_surface_heritee_est_inchange():
    """CLIQUET — relevé sur `origin/main` au 2026-09-01, après le revert de #756.

    ⚠️ **Regravé le 2026-09-04, une seule valeur : `permission` passe de `"write"` à
    `"read"`** (ADR 0068, décision d'Alexis « pas de v2 » — on corrige l'outil que les
    gens utilisent). Ce cliquet protège d'une rupture qui fait ÉCHOUER un appel : #756
    rendait un champ obligatoire, et un appelant qui l'omettait passait de
    « fonctionne » à « refusé ». Un défaut plus RESTRICTIF ne fait échouer personne —
    l'appel réussit, avec moins de droit. Et le seul appelant hors backend, le
    dashboard, passe toujours `role` explicitement (`shareResource(…, role)`), qui
    prime sur `permission` : vérifié dans `oto-dashboard/frontend/src/api/console.ts`
    avant de graver, pas supposé.
    Le fichier est regravé **au format d'origine** (une ligne, `sort_keys`) pour que
    le diff montre LA valeur qui bouge et non une réindentation — un cliquet illisible
    ne se relit plus, et ne garde donc plus rien.

    Ce fichier `.json` EST la déclaration : le régénérer est un geste qui se voit en
    revue et qui se nomme, exactement comme `tests/api/api_routes_table.txt`. Toute
    évolution du contrat d'entrée de la gouvernance passe par `oto_resource_v2`, dont
    le schéma n'est PAS figé ici — c'est le sens même de la duplication.
    """
    attendu = json.loads(EMPREINTE.read_text(encoding="utf-8"))
    servi = _schema_servi("oto_resource")
    assert servi == attendu, (
        "le schéma d'entrée de `oto_resource` a bougé. C'est la surface que des "
        "appelants réels consomment (mesuré au journal des appels le 2026-09-01) : "
        "une contrainte AJOUTÉE ici casse en production, chez quelqu'un d'autre, "
        "sans trace. Ce qui durcit le contrat va sur `oto_resource_v2`."
    )


def test_le_defaut_du_discriminant_est_toujours_servi():
    """Le champ n'est pas obligatoire, et son défaut est bien celui d'avant #756."""
    champ = R.ResourceInput.model_fields["resource_type"]
    assert not champ.is_required()
    assert champ.default == "datastore_namespace"
    assert R.ResourceInput(op="list").resource_type == "datastore_namespace"


@pytest.mark.parametrize("valeur", ["abc", "", "7a", "../7"])
def test_l_heritee_accepte_encore_un_identifiant_non_numerique(valeur):
    """#756 posait un motif `^\\d+$` sur l'entrée. C'est un bon correctif — un
    identifiant non numérique y levait un 500 pour un tableau et un projet — mais
    il DURCIT l'entrée, donc il va sur la surface stricte. Ici, rien ne change :
    le refus continue de se produire plus loin, comme avant."""
    assert R.ResourceInput(op="get", resource_id=valeur).resource_id == valeur


# ── 2. La surface stricte : ce que #756 apportait de bon, sous flag ──────────


def test_la_stricte_exige_le_discriminant():
    assert V2.ResourceInputV2.model_fields["resource_type"].is_required()
    with pytest.raises(ValidationError):
        V2.ResourceInputV2(op="list")


@pytest.mark.parametrize("famille", ["datastore_namespace", "project", "doctrine"])
@pytest.mark.parametrize("valeur", ["abc", "", "7a", "1 OR 1", "../7"])
def test_la_stricte_refuse_un_identifiant_non_numerique(famille, valeur):
    with pytest.raises(ValidationError):
        V2.ResourceInputV2(op="get", resource_type=famille, resource_id=valeur)


def test_la_stricte_herite_de_TOUS_les_champs_de_l_heritee():
    """Dériver plutôt que recopier : un champ ajouté à `ResourceInput` doit arriver
    sur la stricte sans qu'on y pense, sinon les deux surfaces divergent au premier
    lot et la migration devient une réécriture.

    ⚠️ INCLUSION, pas égalité — corrigé le 05/09/2026. L'égalité disait deux
    choses à la fois : « la stricte n'oublie aucun champ » (l'intention, juste) et
    « la stricte n'en ajoute aucun » (jamais voulu, et contradictoire avec le
    cliquet ci-dessus, qui dit que TOUTE évolution du contrat d'entrée passe par
    la stricte). Les deux textes ne pouvaient pas être vrais ensemble : v2 était
    la surface où faire évoluer le contrat, et le seul banc qui la regardait lui
    interdisait d'évoluer.

    Le sens qui reste est le seul qu'on voulait garder : rien de l'héritée ne
    manque à la stricte."""
    manquants = set(R.ResourceInput.model_fields) - set(V2.ResourceInputV2.model_fields)
    assert not manquants, (
        f"la stricte a perdu {sorted(manquants)} : elle DÉRIVE de l'héritée, "
        "un champ ne peut pas s'y perdre sans qu'on l'ait retiré exprès")


def test_la_stricte_n_ajoute_RIEN_de_son_cote():
    """Le pendant de l'inclusion, et la trace d'un aller-retour : `sub` a d'abord
    été posé sur la stricte, au motif que le schéma de l'héritée est gelé. C'était
    contraire à la décision d'Alexis du 04/09 (ADR 0068, « pas de v2 ») — **on
    corrige l'outil que les gens utilisent**, et le cliquet se regrave.

    Un champ propre à la stricte reste possible ; il devra se déclarer ici,
    puisqu'il voudra dire que l'héritée ne pouvait pas le recevoir."""
    ajouts = set(V2.ResourceInputV2.model_fields) - set(R.ResourceInput.model_fields)
    assert ajouts == set(), f"ajout non déclaré sur la stricte : {sorted(ajouts)}"


def test_les_deux_surfaces_partagent_handler_autorisation_et_sortie():
    """La duplication porte sur le CONTRAT d'entrée, et sur rien d'autre. Deux
    handlers seraient deux comportements à faire diverger."""
    legacy, stricte = _cap("resources.govern"), _cap("resources.govern.v2")
    assert legacy.handler is stricte.handler
    assert legacy.Output is stricte.Output is C.ResourceOut
    assert type(legacy.authz) is type(stricte.authz)


def test_seule_l_heritee_declare_le_refus_du_type_inconnu():
    """Les deux surfaces refusent une famille inconnue, à des couches différentes —
    donc elles ne déclarent pas les mêmes refus. L'héritée accepte `str` et refuse
    dans le handler (`unsupported_resource_type`, un 400 nommé) ; la stricte porte un
    `Literal`, donc la validation refuse avant, et déclarer ce code chez elle
    promettrait un refus que le serveur ne rend jamais."""
    codes = lambda c: {e.code for e in c.errors}  # noqa: E731
    assert "unsupported_resource_type" in codes(_cap("resources.govern"))
    assert "unsupported_resource_type" not in codes(_cap("resources.govern.v2"))
    assert codes(_cap("resources.govern.v2")) == {e.code for e in C.REFUS}


# ── 3. Le défaut connu est écrit LÀ OÙ L'APPELANT LE LIT ─────────────────────


def test_l_heritee_annonce_son_defaut_connu_dans_sa_description_servie():
    """Un défaut qu'on garde et qu'on ne dit pas est un piège ; un défaut qu'on garde
    et qu'on écrit est un contrat. La description servie est le seul texte que le
    modèle relit à chaque appel — un fichier de `docs/` ne l'atteint jamais."""
    d = _cap("resources.govern").description
    assert "datastore_namespace" in d
    assert "transfer" in d and "share" in d
    assert "oto_resource_v2" in d, "le défaut se documente AVEC sa sortie de secours"


def test_le_defaut_connu_est_publie_dans_le_document_REST():
    """Le consommateur pur du REST ne voit jamais la description MCP ; il lit
    l'OpenAPI. Les deux faces servent la même prose (`Capability.description`), et
    c'est ce test qui empêche de documenter le piège d'un seul côté."""
    op = openapi.build()["paths"]["/api/resources"]["post"]
    assert "datastore_namespace" in op["description"]
    assert "/api/resources/v2" in op["description"]


# ── 4. Le gate bêta : la stricte se propose, l'héritée ne se retire jamais ───


def test_la_stricte_est_beta_et_l_heritee_ne_l_est_JAMAIS():
    """La seconde assertion est le garde-fou qui compte. `BETA_TOOLS` masque
    fail-CLOSED : y faire entrer `oto_resource` retirerait d'un coup, à tous les
    comptes sans l'option, une surface vivante — la rupture de #756 en pire, parce
    qu'elle serait silencieuse."""
    assert "oto_resource_v2" in BETA_TOOLS
    assert "oto_resource" not in BETA_TOOLS


class _Ctx:
    class _FastMCP:
        def __init__(self, noms):
            self._noms = noms

        async def list_tools(self, run_middleware=False):
            return [type("T", (), {"name": n})() for n in self._noms]

    def __init__(self, noms):
        self.fastmcp = self._FastMCP(noms)


@pytest.fixture
def socle(monkeypatch):
    """Un compte ordinaire — les blocs voisins neutralisés, comme
    `tests/test_outils_beta.py`, pour que le seul écart observable soit le gate."""
    monkeypatch.setattr(SV.access, "current_org", lambda sub: 1)
    monkeypatch.setattr(SV.access, "current_group", lambda sub: None)
    monkeypatch.setattr(SV.access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(SV.access, "org_admin_hidden_tools", lambda org: set())
    monkeypatch.setattr(SV.access, "group_admin_hidden_tools", lambda g: set())
    monkeypatch.setattr(SV.access, "rbac_denied_connectors", lambda s, o: set())
    monkeypatch.setattr(SV.access, "group_rbac_denied_connectors", lambda s, g: set())
    monkeypatch.setattr(SV.db, "list_user_disabled_tools", lambda s, o: [])
    monkeypatch.setattr(SV.db, "list_user_enabled_tools", lambda s, o: [])
    monkeypatch.setattr(SV.connector_activation, "exposed_connectors", lambda o: set())
    monkeypatch.setattr(SV.connector_selection, "is_seeded", lambda s, o: True)
    monkeypatch.setattr(SV.connector_selection, "list_selection", lambda s, o: {})
    return _Ctx({"oto_resource", "oto_resource_v2", "oto_whoami"})


@pytest.mark.asyncio
async def test_sans_l_option_la_stricte_est_masquee_et_l_heritee_reste(socle, monkeypatch):
    monkeypatch.setattr(SV.access, "has_option", lambda sub, opt, org=None: False)
    caches = await SV.compute_hidden_tools(socle, "sub-1")
    assert "oto_resource_v2" in caches
    assert "oto_resource" not in caches


@pytest.mark.asyncio
async def test_sur_une_ERREUR_la_stricte_reste_masquee_sans_emporter_l_heritee(
        socle, monkeypatch):
    """Le bloc bêta est fail-CLOSED. On vérifie ici qu'il le reste ET qu'il ne
    déborde pas : un hoquet de base ne doit pas coûter à un appelant ordinaire la
    surface dont il se sert."""
    def _boum(sub, opt, org=None):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(SV.access, "has_option", _boum)
    caches = await SV.compute_hidden_tools(socle, "sub-1")
    assert "oto_resource_v2" in caches
    assert "oto_resource" not in caches


@pytest.mark.asyncio
async def test_avec_l_option_la_stricte_revient(socle, monkeypatch):
    vu = {}

    def _oui(sub, opt, org=None):
        vu.update(option=opt, org=org)
        return True

    monkeypatch.setattr(SV.access, "has_option", _oui)
    caches = await SV.compute_hidden_tools(socle, "sub-1")
    assert "oto_resource_v2" not in caches
    assert vu["option"] == BETA_OPTION, "l'option est celle des comptes bêta, pas une neuve"
    assert vu["org"] == 1
