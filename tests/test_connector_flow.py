"""Le geste « connecter » est DÉCLARÉ par le connecteur, dérivé partout ailleurs.

Ce que ces tests empêchent de revenir : le dashboard montait le widget de consentement
derrière `['zoho','zohodesk','zohoanalytics'].includes(name)`. Salesforce, qui a pourtant
la même forme côté backend, n'y était pas — donc aucun bouton, et un client ne pouvait
pas terminer sa connexion. La correction n'est pas « ajouter un nom » : c'est que plus
personne n'ait à en connaître.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp import connector_flow, providers, status_hints
from oto_mcp.tools import register_all


@pytest.fixture(scope="module", autouse=True)
def _declarations():
    register_all(FastMCP("connector-flow-probe"))
    # ⚠️ Un flux se déclare à l'IMPORT de son module — donc ce banc doit charger
    # EXACTEMENT ce que charge le boot, ni plus ni moins.
    #
    # `register_all` couvre les connecteurs à outils. Les fédérations OAuth, elles,
    # sont importées au boot par `api_routes` en montant leurs routes : on importe
    # donc CE module, pas les leurs. La nuance n'est pas cosmétique — importer un
    # module de complaisance a masqué un vrai défaut le 12/08 : le flux hébergé était
    # déclaré dans un module que RIEN n'importe au boot (import paresseux dans les
    # handlers). Le test le voyait, la production non, et le catalogue de prod ne
    # l'a jamais servi. Un banc qui charge plus que le boot ne garde rien : il
    # certifie une couverture qui n'existe pas.
    from oto_mcp import api_routes  # noqa: F401


# --- le contrat du descripteur -------------------------------------------------

def test_les_connecteurs_a_flux_sont_ceux_quon_attend():
    """⚠️ Cette liste est un INVENTAIRE, pas une préférence : elle doit grossir dès
    qu'un geste « connecter » existe quelque part. Trois flux vivaient hors du point
    de passage — des routes REST écrites à la main qui rendaient la bonne forme par
    coïncidence, sans que rien ne l'impose et sans que ce garde-fou les voie (#300).
    """
    assert set(connector_flow.entries()) == {
        "zoho", "zohodesk", "zohoanalytics", "salesforce",
        "atlassian", "folkmcp", "google", "unipile"}


def test_le_descripteur_ne_porte_ni_url_ni_nom_de_capacite():
    """Le catalogue `/api/connectors` est servi SANS authentification : y publier des
    chemins internes ferait de la surface d'attaque un effet de bord de la doc. Le
    chemin est fixe et connu du client, il n'a pas à voyager."""
    for name in connector_flow.entries():
        blob = repr(connector_flow.describe(name))
        for interdit in ("http", "/api/", "me.", "oto_"):
            assert interdit not in blob, f"{name} : « {interdit} » dans le descripteur"


def test_un_choix_ferme_a_toujours_des_options():
    """Un select vide est indémarrable. La déclaration le refuse déjà ; ce test fige
    la garantie pour les descripteurs réellement servis."""
    for name in connector_flow.entries():
        for p in connector_flow.describe(name)["params"]:
            if p["required"]:
                assert p["options"] or p["default"], f"{name}.{p['name']}"


def test_les_regions_zoho_ne_vivent_quici():
    """Les 6 data centers étaient recopiés à plusieurs endroits, dont un libellé de
    registre qui en annonçait un que le code REJETTE. Le descripteur est désormais leur
    domicile, et il est dérivé de la table de résolution elle-même."""
    from oto_mcp.tools.zoho import _DC_DOMAINS
    options = {o["value"] for o in connector_flow.describe("zoho")["params"][0]["options"]}
    assert options == set(_DC_DOMAINS), "le descripteur a divergé de la table de domaines"


def test_le_catalogue_expose_la_forme_et_rien_pour_les_autres():
    cat = {c["name"]: c for c in providers.public_catalog()}
    assert cat["salesforce"]["connect"]["label"]
    assert cat["serper"]["connect"] is None       # 56 connecteurs sur 70
    # Le flux hébergé était l'exception (« pas encore déclaré ici ») : il ne l'est
    # plus — son geste est déclaré comme les autres, avec son choix de canal.
    assert cat["unipile"]["connect"]["params"][0]["name"] == "channel"


# --- ce que le front n'a plus le droit de faire --------------------------------

def test_tout_connecteur_a_deux_temps_declare_son_flux():
    """TOTALITÉ, côté flux. Un connecteur qui annonce « mon credential se complète
    ailleurs » (`status_hints.register_state`) DOIT dire où — sinon la fiche affiche
    « il reste une étape » sans le moyen de la faire. C'est exactement l'état dans
    lequel Salesforce a passé la semaine."""
    manquants = sorted(
        n for n in providers.REGISTRY
        if status_hints.has_state(n) and not connector_flow.supports(n))
    assert not manquants, (
        f"{manquants} déclarent un credential qui se complète hors formulaire mais "
        "aucun flux : le front ne pourra pas proposer le geste.")


# Chemin de connexion NOMMÉ d'après un connecteur, toléré parce qu'il est ANTÉRIEUR à
# la règle et qu'il est déjà supersédé. Cette liste ne doit que se vider.
_NOMMES_TOLERES = {
    # ⚠️ Découvert par CE garde-fou le 2026-08-27, en migrant la messagerie hébergée en
    # capacité : le chemin existait depuis toujours, mais il était écrit à la main —
    # or ce test ne parcourt QUE le registre de capacités. Même angle mort que le glob
    # de `test_rest_modules_are_capabilities` avant #286 : un garde-fou ne voit que là
    # où il regarde, et rendre un objet visible est ce qui fait apparaître sa dette.
    #
    # Il est déjà SUPERSÉDÉ par `me.connector_connect`
    # (`POST /api/me/connectors/{name}/connect`, le chemin fixe qui ne nomme personne) —
    # les deux appellent le MÊME `unipile_connect.hosted_auth_url`, donc ils ne peuvent
    # pas diverger. Ce qui manque est la bascule du FRONT ; le jour où elle a lieu, on
    # retire la capacité et cette ligne avec.
    "me.unipile.connect",
}


def test_aucune_face_rest_de_connexion_nommee_dapres_un_connecteur():
    """Les chemins `/api/<nom>/oauth/start` ont été RETIRÉS une fois le dashboard de prod
    passé au chemin fixe (v1.19.0). Il ne reste qu'une face REST pour démarrer un flux,
    et son chemin ne nomme personne — hors `_NOMMES_TOLERES`, qui est daté et se vide."""
    from oto_mcp.capabilities import registry
    for cap in registry.CAPABILITIES:
        if cap.rest is None or "connect" not in cap.key or cap.key in _NOMMES_TOLERES:
            continue
        for n in providers.REGISTRY:
            assert f"/{n}/" not in cap.rest.path, (
                f"{cap.key} expose une face REST nommée : {cap.rest.path}")


def test_la_tolerance_de_chemin_nomme_ne_ment_pas():
    """Une clé disparue doit quitter la liste : une tolérance qui ne correspond plus au
    réel masque la prochaine vraie."""
    from oto_mcp.capabilities import registry
    connues = {c.key for c in registry.CAPABILITIES}
    fantomes = sorted(_NOMMES_TOLERES - connues)
    assert not fantomes, (
        f"Ces capacités n'existent plus : {fantomes}. Retire-les de `_NOMMES_TOLERES` — "
        "si `me.unipile.connect` est partie, c'est que le front a basculé sur le chemin "
        "fixe, et la tolérance n'a plus lieu d'être.")


def test_le_demarrage_generique_partage_le_handler_des_capacites_nommees():
    """Il n'existe qu'UNE façon de démarrer un consentement : la face MCP par connecteur
    et le flux générique appellent le MÊME `start_for`, donc les deux surfaces ne peuvent
    pas diverger."""
    from oto_mcp.capabilities import salesforce_connect, zoho_connect
    import inspect
    assert "start_for" in inspect.getsource(zoho_connect._start)
    assert "start_for" in inspect.getsource(salesforce_connect._start)
    # et le flux générique passe par les mêmes
    from oto_mcp.tools import salesforce as sf_tools, zoho as zoho_tools
    assert "start_for" in inspect.getsource(sf_tools._start_flow)
    assert "start_for" in inspect.getsource(zoho_tools._start_flow)


def test_la_capacite_generique_est_montee_sur_un_chemin_fixe():
    from oto_mcp.capabilities import registry
    cap = next(c for c in registry.CAPABILITIES if c.key == "me.connector_connect")
    assert cap.rest is not None and cap.rest.path == "/api/me/connectors/{name}/connect"
    # le nom du connecteur voyage en PARAMÈTRE, il n'est pas dans le chemin
    for n in providers.REGISTRY:
        assert n not in cap.rest.path


# --- la SORTIE du seam est commune, comme son entrée ---------------------------

async def _both_flows(monkeypatch) -> dict[str, dict]:
    """Démarre RÉELLEMENT les deux flux déclarés (seuls les appels sortants sont
    neutralisés) et rend ce que chacun sert à l'appelant."""
    from types import SimpleNamespace

    from oto_mcp import access, salesforce_oauth, zoho_oauth
    monkeypatch.setattr(zoho_oauth, "app_fields", lambda *a, **k: {})
    monkeypatch.setattr(zoho_oauth, "build_auth_url",
                        lambda *a, **k: "https://accounts.zoho.eu/oauth/v2/auth?x=1")
    monkeypatch.setattr(salesforce_oauth, "build_auth_url",
                        lambda *a, **k: "https://login.salesforce.com/services/oauth2/authorize?x=1")
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    ctx = SimpleNamespace(sub="u-1", org_id=1)
    return {n: (await connector_flow.start(n, ctx, {"data_center": "eu"})).as_dict()
            for n in ("zoho", "salesforce")}


@pytest.mark.asyncio
async def test_les_deux_flux_rendent_la_meme_forme(monkeypatch):
    """L'écart que ce contrat ferme : `me.connector_connect` existe pour qu'un front
    branche un connecteur SANS savoir lequel, et sa sortie ne le permettait pas —
    Zoho échotait `{auth_url, connector}`, Salesforce `{auth_url, scope}`. Le
    premier niveau est désormais commun ; le propre du connecteur est sous `details`."""
    shapes = await _both_flows(monkeypatch)
    assert set(shapes["zoho"]) == set(shapes["salesforce"]) == {"auth_url", "details"}
    assert shapes["zoho"]["details"] == {"connector": "zoho"}
    assert shapes["salesforce"]["details"] == {"scope": "member"}


@pytest.mark.asyncio
async def test_le_contrat_publie_decrit_les_deux_flux_sans_champ_libre(monkeypatch):
    """Le modèle de réponse était ouvert (`extra="allow"`) avec deux champs
    optionnels : il DOCUMENTAIT l'incohérence au lieu de la fermer. Il doit
    maintenant décrire exactement ce que les deux flux servent."""
    from oto_mcp.capabilities.connectors_connect import ConnectorConnectStarted

    assert ConnectorConnectStarted.model_config.get("extra") != "allow"
    attendus = set(ConnectorConnectStarted.model_fields)
    for name, payload in (await _both_flows(monkeypatch)).items():
        assert set(payload) == attendus, f"{name} sert autre chose que le contrat publié"
        ConnectorConnectStarted.model_validate(payload)


def test_un_flux_qui_invente_sa_forme_est_refuse():
    """La garantie n'était qu'un commentaire de type — un troisième flux aurait donc
    inventé sa clé sans que rien ne proteste. Elle est vérifiée au point de passage."""
    connector_flow.declare("_probe_forme", start=lambda ctx, values: {"auth_url": "https://x"})
    try:
        with pytest.raises(TypeError, match="details"):
            asyncio.run(connector_flow.start("_probe_forme", None, {}))
    finally:
        connector_flow._FLOWS.pop("_probe_forme", None)


# --- l'URL de retour : dérivée, et du bon côté ---------------------------------

def test_lurl_de_retour_est_derivee_de_lenvironnement(monkeypatch):
    """Elle vivait en PROSE dans la doc du connecteur, domaine de prod écrit à la main.
    Un utilisateur de preprod y lisait donc une URL que SON backend n'utilise pas, et le
    consentement échouait sur un `redirect_uri_mismatch` dont le message accusait sa
    Connected App. Désormais elle suit `OTO_MCP_PUBLIC_URL`."""
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.example.test")
    assert connector_flow.callback_url("salesforce") == (
        "https://mcp.example.test/api/salesforce/oauth/callback")


def test_lurl_de_retour_nest_PAS_dans_le_catalogue_anonyme():
    """`/api/connectors` est servie sans authentification. Le descripteur public porte
    la FORME du geste, pas les adresses — c'est la projection authentifiée qui ajoute
    l'URL, parce que c'est là qu'elle sert à quelqu'un."""
    for name in connector_flow.entries():
        assert "callback_url" not in (connector_flow.describe(name) or {})
    cat = {c["name"]: c for c in providers.public_catalog()}
    assert "callback_url" not in (cat["salesforce"]["connect"] or {})


def test_aucune_url_de_retour_ecrite_en_dur_dans_la_prose_client():
    """TRIPWIRE — la doc et les messages d'erreur lus par un CLIENT ne doivent plus
    contenir de domaine en dur : ils sont servis aux deux environnements."""
    import pathlib as _p
    for f in (_p.Path("oto_mcp/connector_docs.py"),
              _p.Path("oto_mcp/tools/salesforce.py"),
              _p.Path("oto_mcp/tools/zoho.py")):
        src = f.read_text(encoding="utf-8")
        for ligne in src.splitlines():
            if "oauth/callback" in ligne and "mcp.oto." in ligne and not ligne.lstrip().startswith("#"):
                raise AssertionError(f"{f} code une URL de retour en dur : {ligne.strip()[:110]}")
