"""Le descriptif OpenAPI est DÉRIVÉ — il décrit le serveur, pas une intention.

On ne teste pas une liste de chemins figée (elle mentirait au premier ajout), mais
les propriétés qui rendent le document utilisable par un intégrateur : les capacités
y sont, le verbe caché dans `op` est visible, l'admin n'y est pas, et le document
reste un OpenAPI valide de forme.
"""
from __future__ import annotations

import json

from oto_mcp import openapi
from oto_mcp.capabilities import registry


class _FakeRoute:
    def __init__(self, path, methods):
        self.path = path
        self.methods = set(methods)


def test_document_shape():
    doc = openapi.build()
    assert doc["openapi"].startswith("3.1")
    assert doc["info"]["title"]
    assert doc["paths"], "aucun chemin dérivé du registre de capacités"
    assert "bearerAuth" in doc["components"]["securitySchemes"]
    json.dumps(doc)                      # sérialisable tel quel (servi en JSON)


def test_capabilities_are_described():
    doc = openapi.build()
    expected = {
        b.path for cap in registry.CAPABILITIES if cap.is_exposed()
        for b in cap.rest_bindings() if not b.path.startswith("/api/admin/")
    }
    missing = {p for p in expected if openapi._openapi_path(p) not in doc["paths"]}
    assert not missing, f"capacités REST absentes du descriptif : {sorted(missing)}"


def test_consolidated_verb_is_visible_in_the_schema():
    """ADR 0047 : le verbe vit dans le corps. Un intégrateur doit pouvoir LIRE les
    `op` possibles — c'est précisément ce que le sondage de chemins ne donne pas."""
    op = openapi.build()["paths"]["/api/me/projects"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    ops = schema["properties"]["op"]["enum"]
    assert {"list", "get", "create", "runs"} <= set(ops)


def test_path_params_are_lifted_out_of_the_body():
    doc = openapi.build()
    item = doc["paths"].get("/api/me/guides/{scope}/{slug}")
    assert item, "capacité à paramètres de chemin absente"
    # `.get` : un paramètre peut être une RÉFÉRENCE de composant (l'en-tête de run,
    # oto#227), qui ne porte ni `name` ni `in`.
    names = {p["name"] for p in item["get"]["parameters"] if p.get("in") == "path"}
    assert names == {"scope", "slug"}


def test_get_capabilities_document_query_params():
    """Un GET de capacité lit sa query string : les champs doivent s'y retrouver."""
    params = openapi.build()["paths"]["/api/me/search"]["get"]["parameters"]
    assert {p["name"] for p in params if p.get("in") == "query"}


def test_admin_surface_is_not_published():
    doc = openapi.build([_FakeRoute("/api/admin/platform-keys", ["GET"])])
    assert not [p for p in doc["paths"] if p.startswith("/api/admin/")]


def test_handwritten_routes_are_listed_without_schema():
    # ⚠️ Cet exemple a rouillé DEUX FOIS : `…/datastore/…/rows` d'abord (passé en
    # capacité, #302), puis `/api/me/tokens` (passé en capacité le 2026-08-27). Chaque
    # fois, l'exemple s'était mis à prouver le contraire de ce qu'il énonce.
    #
    # Le choix est donc désormais un chemin qui ne PEUT PAS devenir une capacité :
    # `/api/upload/{token}` n'a pas de JWT (le jeton scellé de l'URL fait foi) et reçoit
    # un corps BRUT ou un multipart — or l'adaptateur authentifie toujours et attend du
    # JSON. Il est classé NATURE, et le restera. Un exemple choisi pour ce qu'il est,
    # plutôt que pour ce qu'il n'a pas encore été.
    doc = openapi.build([
        _FakeRoute("/api/upload/{token}", ["GET", "POST", "OPTIONS"]),
    ])
    item = doc["paths"]["/api/upload/{token}"]
    assert set(item) == {"get", "post"}          # OPTIONS n'est pas une opération
    assert "requestBody" not in item["post"]     # forme non dérivable, dit comme tel
    assert item["get"]["tags"] == ["_legacy"]


def test_le_depot_en_volume_s_ouvre_en_ne_lisant_que_le_contrat():
    """oto#106 : un client REST pur voyait `/api/upload/{token}` sans pouvoir l'ouvrir —
    ni frapper le jeton, ni savoir quel corps envoyer, ni ce que l'accusé contient."""
    from oto_mcp.api import routes as api_routes
    doc = openapi.build(api_routes.make_routes(object()))
    # 1. Frapper le jeton : une opération REST, mêmes paramètres que l'outil agent.
    mint = doc["paths"]["/api/me/upload-url"]["post"]
    props = mint["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert {"target", "datastore", "format", "key", "donnees_d_origine"} <= set(props)
    sortie = mint["responses"]["200"]["content"]["application/json"]["schema"]
    assert {"url", "method", "headers", "max_bytes", "expires_at"} <= set(sortie["properties"])
    assert "unknown_namespace" in mint["responses"]["404"]["description"]
    # 2. Déposer : sans `Authorization`, le corps décrit, l'accusé et les refus déclarés.
    depot = doc["paths"]["/api/upload/{token}"]
    put = depot["put"]
    assert put["security"] == [] and put["tags"] == ["_nature"]
    assert {"application/x-ndjson", "text/csv"} <= set(put["requestBody"]["content"])
    assert "file" in depot["post"]["requestBody"]["content"]["multipart/form-data"][
        "schema"]["properties"]
    accuse = put["responses"]["200"]["content"]["application/json"]["schema"]
    assert {"kind", "inserted", "updated", "count"} <= set(accuse["properties"])
    assert "token_already_used" in put["responses"]["409"]["description"]
    assert "bad_ndjson" in put["responses"]["400"]["description"]
    assert "Route écrite à la main" not in depot["get"]["description"]
    assert "text/html" in depot["get"]["responses"]["200"]["content"]
    # Compatibilité avec les contrats épinglés d'avant (souches) : en-têtes de contexte
    # toujours déclarés, corps décrit mais pas obligatoire.
    ctx = [{"$ref": "#/components/parameters/XOtoOrg"},
           {"$ref": "#/components/parameters/XOtoGroup"}]
    for verbe in ("get", "put", "post"):
        assert all(r in depot[verbe]["parameters"] for r in ctx), verbe
    assert put["requestBody"]["required"] is False
    assert depot["post"]["requestBody"]["required"] is False


def test_capability_wins_over_handwritten_on_the_same_path():
    """Une route encore montée à la main ET déclarée en capacité : c'est la capacité
    (avec son schéma) qui doit décrire le chemin, pas la coquille vide."""
    doc = openapi.build([_FakeRoute("/api/me/projects", ["POST"])])
    assert "requestBody" in doc["paths"]["/api/me/projects"]["post"]


def test_starlette_converters_are_stripped():
    doc = openapi.build([_FakeRoute("/api/me/projects/{project_id:int}/files", ["GET"])])
    assert "/api/me/projects/{project_id}/files" in doc["paths"]


# ── Ce qu'un front tiers a dérivé du contrat (29/08) — le document le DIT ───────
def test_the_guide_body_limit_is_in_the_schema():
    """Le front dérive sa garde de saisie du contrat : la borne doit y être, et dire
    qu'elle est en octets — `maxLength` seul laisserait passer un texte accentué."""
    op = openapi.build()["paths"]["/api/me/guides/{scope}/{slug}"]["put"]
    body = op["requestBody"]["content"]["application/json"]["schema"]["properties"]["body_md"]
    assert body["maxLength"] == 65536
    assert "octets" in body["description"].lower()
    assert "body_too_large" in op["responses"]["400"]["description"]


def test_a_node_carries_its_handles_to_the_other_surfaces():
    """Le modèle `Output` de premier niveau est INLINE dans la 200 (pas un composant,
    cf. docs/alias-deprecies.md) ; ses sous-modèles sont hissés en composants."""
    doc = openapi.build()
    op = doc["paths"]["/api/me/nodes/{node_id}"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert {"doc_id", "project_id"} <= set(schema["properties"])
    assert "ordered" in doc["components"]["schemas"]["ContentBlock"]["properties"]


def test_the_error_envelope_is_a_component_every_refusal_references():
    doc = openapi.build()
    assert set(doc["components"]["schemas"]["Erreur"]["properties"]) == \
        {"error", "detail", "details"}
    op = doc["paths"]["/api/me/orgs/{id}/membership"]["delete"]
    assert op["responses"]["401"]["content"]["application/json"]["schema"] == \
        {"$ref": "#/components/schemas/Erreur"}
    # Les refus déclarés : l'énuméré `error` porte les codes, par statut.
    codes = {s: r["content"]["application/json"]["schema"]["allOf"][1]["properties"]["error"]["enum"]
             for s, r in op["responses"].items() if s in ("404", "409")}
    # Les refus de `X-Oto-Run` (oto#227) entrent dans la MÊME fusion, après les déclarés.
    assert codes == {"404": ["unknown_org", "not_a_member", "run_not_found"],
                     "409": ["last_org", "last_org_admin", "run_closed"]}


# ── L'en-tête de run (oto#227) : déclaré là où il peut survenir, et nulle part ailleurs ─

def _operations(doc):
    return [(p, m, o) for p, item in doc["paths"].items() for m, o in item.items()]


def test_l_en_tete_de_run_est_UN_composant_reference_par_chaque_operation_de_capacite():
    """Seul l'adaptateur des capacités lit `X-Oto-Run` : une route écrite à la main ou un
    alias 308 ne peut ni le lire ni rendre ses refus, et ne doit pas le déclarer."""
    doc = openapi.build([_FakeRoute("/api/upload/{token}", ["GET"])])
    param = doc["components"]["parameters"]["XOtoRun"]
    assert (param["name"], param["in"], param["required"]) == ("X-Oto-Run", "header", False)
    ref = {"$ref": "#/components/parameters/XOtoRun"}
    capacites = 0
    for chemin, verbe, op in _operations(doc):
        if op["tags"][0] in ("_legacy", "_deprecated", "_nature"):
            assert ref not in op.get("parameters", []), (chemin, verbe)
        else:
            capacites += 1
            assert op["parameters"].count(ref) == 1, (chemin, verbe)
    assert capacites, "aucune opération de capacité : le banc ne vérifie plus rien"


def test_les_refus_de_l_en_tete_sont_FUSIONNES_jamais_substitues():
    """Sur chaque opération de capacité, les trois refus de l'en-tête sont dits ; là où
    le statut portait déjà des refus déclarés, ceux-ci restent dans l'énuméré ; et la 403
    reste le texte générique, sans énuméré."""
    from oto_mcp.capabilities.run_thread import REFUS_DECLARES_DE_L_EN_TETE as REFUS
    doc = openapi.build()
    for chemin, verbe, op in _operations(doc):
        if op["tags"][0] in ("_legacy", "_deprecated", "_nature"):
            continue
        for e in REFUS:
            assert f"`{e.code}`" in op["responses"][str(e.status)]["description"], (
                chemin, verbe, e.code)

    fusions = [(cap, b, e) for cap in registry.CAPABILITIES if cap.is_exposed()
               for b in cap.rest_bindings() if not b.path.startswith("/api/admin/")
               for e in cap.errors if e.status in {r.status for r in REFUS}]
    assert fusions, "aucun refus déclaré sur 400/404/409 : la fusion n'est plus éprouvée"
    for cap, b, e in fusions:
        op = doc["paths"][openapi._openapi_path(b.path)][b.verb.lower()]
        schema = op["responses"][str(e.status)]["content"]["application/json"]["schema"]
        enum = set(schema["allOf"][1]["properties"]["error"]["enum"])
        assert {e.code} | {r.code for r in REFUS if r.status == e.status} <= enum, (
            b.path, e.code)

    ouvrir = doc["paths"]["/api/me/runs"]["post"]
    assert ouvrir["responses"]["403"]["content"]["application/json"]["schema"] == \
        {"$ref": "#/components/schemas/Erreur"}


class _VerifieurInerte:
    """`make_routes` n'a besoin que d'un objet à passer : rien n'est vérifié ici."""

    def verify_token(self, token):  # pragma: no cover — jamais appelé
        return None


def test_chaque_operationId_est_unique_dans_le_document_servi():
    """#436 : l'OpenAPI exige l'unicité des `operationId`. Un doublon ne se voit pas
    dans le document : il se voit chez le client GÉNÉRÉ, indexé sur l'id, qui ne
    compile pas ou garde un chemin et perd les autres en silence. Jugé sur le
    document que le serveur SERT — capacités, routes écrites à la main et alias
    dépréciés, bâtis depuis la table de routes vivante."""
    from collections import Counter

    from oto_mcp.api import routes as api_routes

    routes = api_routes.make_routes(_VerifieurInerte(), mcp_instance=None)
    doc = openapi.build(routes)
    ids = Counter(op["operationId"] for item in doc["paths"].values()
                  for op in item.values())
    doublons = {i: [f"{v.upper()} {p}" for p, item in doc["paths"].items()
                    for v, op in item.items() if op["operationId"] == i]
                for i, n in ids.items() if n > 1}
    assert not doublons, f"operationId portés par plusieurs opérations : {doublons}"


def test_une_capacite_a_plusieurs_chemins_garde_son_id_sur_le_premier():
    """Le PREMIER binding déclaré garde l'id de la capacité — celui qu'un client déjà
    généré appelle : le correctif ne renomme aucune méthode existante. Les suivants
    reçoivent l'id dérivé de leur chemin, la recette des routes écrites à la main."""
    doc = openapi.build()
    for chemin, attendu in (
            ("/api/me/guides/{scope}/{slug}", ("me_guides_get_get", "me_guides_set_put")),
            ("/api/orgs/{id}/guides/{scope}/{slug}",
             ("get_api_orgs_id_guides_scope_slug", "put_api_orgs_id_guides_scope_slug")),
            ("/api/groups/{id}/guides/{scope}/{slug}",
             ("get_api_groups_id_guides_scope_slug",
              "put_api_groups_id_guides_scope_slug"))):
        item = doc["paths"][chemin]
        assert (item["get"]["operationId"], item["put"]["operationId"]) == attendu, chemin


# ── Le mode d'emploi et le contexte d'org : DANS le contrat servi (oto-dashboard#155) ──

def test_les_en_tetes_de_contexte_sont_declares_sur_chaque_operation_qui_les_lit():
    """`X-Oto-Org` décide de la portée d'une écriture : un client généré doit le
    connaître. Le middleware le lit sur toute route `/api/*` — capacité ou route écrite à
    la main —, donc les deux familles le déclarent ; un alias 308 ne fait que renvoyer."""
    doc = openapi.build([_FakeRoute("/api/upload/{token}", ["GET"])])
    params = doc["components"]["parameters"]
    assert (params["XOtoOrg"]["name"], params["XOtoOrg"]["in"],
            params["XOtoOrg"]["required"]) == ("X-Oto-Org", "header", False)
    assert (params["XOtoGroup"]["name"], params["XOtoGroup"]["in"],
            params["XOtoGroup"]["required"]) == ("X-Oto-Group", "header", False)
    # Ce qu'il vaut ABSENT est dit dans le contrat, pas seulement dans une page.
    assert "maison" in params["XOtoOrg"]["description"]
    refs = [{"$ref": "#/components/parameters/XOtoOrg"},
            {"$ref": "#/components/parameters/XOtoGroup"}]
    vues = {"_legacy": 0, "capacite": 0}
    for chemin, verbe, op in _operations(doc):
        tag = op["tags"][0]
        for ref in refs:
            attendu = 0 if tag == "_deprecated" else 1
            assert op.get("parameters", []).count(ref) == attendu, (chemin, verbe, ref)
        if tag != "_deprecated":
            vues["_legacy" if tag == "_legacy" else "capacite"] += 1
    assert all(vues.values()), f"une famille n'est plus éprouvée : {vues}"


def test_le_mode_d_emploi_est_dans_la_description_servie():
    """La section « Démarrer » était collée par le build de docs.oto.cx : invisible à
    qui lit le contrat sans passer par cette page. Elle est servie, et elle nomme les
    deux en-têtes de contexte et le défaut quand ils manquent."""
    desc = openapi.build()["info"]["description"]
    assert "## Démarrer" in desc
    for mot in ("X-Oto-Org", "X-Oto-Group", "home_org", "token_scope_forbidden", '"op"'):
        assert mot in desc, mot
    # Servi par chaque instance : aucune adresse d'instance en dur.
    assert "oto.cx" not in desc and "oto.ninja" not in desc
