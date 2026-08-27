"""La toolbox du membre, en capacité : mêmes chemins, même inversion, MÊME fil.

Les six routes `/api/me/tools*` ont quitté `api_routes_tools.py` (module supprimé)
pour `capabilities/tools_me.py` (27/08). Ce fichier garde trois choses qu'une
migration naïve casse, et qui ne se voient qu'en jouant la vraie chaîne REST :

1. **L'ordre `registry` avant `{name}`.** `{name}` capture un segment : si le motif
   générique passait devant, `GET /api/me/tools/registry` serait servi comme la fiche
   d'un outil nommé « registry ». C'est le premier test, et il résout une URL contre
   le VRAI routeur plutôt que de relire l'ordre du code.
2. **POST désactive, DELETE réactive.** L'inversion est historique (le chemin nomme
   la ligne de denylist, pas le tool) et contre-intuitive : elle est figée ici pour
   qu'un « nettoyage » ne la retourne pas.
3. **Le corps LIBRE de `…/call`.** Deux formes acceptées depuis toujours — l'objet
   d'arguments nu, ou son enveloppe `{"arguments": {…}}` — plus quatre dégénérescences
   (corps absent, illisible, une liste, une enveloppe non-objet) qui doivent toutes
   donner « aucun argument » et non une erreur. Sans le cran `body_field`, la garde de
   champ inconnu refuserait le premier argument venu.
"""
from __future__ import annotations

import json

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities import tools_me as tm


class _FauxTool:
    def __init__(self, name, description, fn=None, parameters=None, output_schema=None):
        self.name, self.description, self.fn = name, description, fn
        self.parameters, self.output_schema = parameters, output_schema


def _echo(**kw):
    return {"echo": kw}


def _leve_type(**kw):
    raise TypeError("unexpected keyword argument 'zzz'")


def _leve(**kw):
    raise RuntimeError("amont indisponible")


class _FausseInstance:
    def __init__(self, outils):
        self._outils = outils

    async def list_tools(self, run_middleware=True):
        # Hors session MCP, la chaîne de middleware n'a pas de Context et lèverait.
        assert run_middleware is False
        return list(self._outils)


@pytest.fixture()
def socle(monkeypatch):
    """Un serveur d'outils en mémoire + un coffre de préférences qui enregistre."""
    outils = [
        _FauxTool("fr_get", " Fiche entreprise agrégée.\nSuite. ", _echo,
                  {"type": "object", "properties": {"siren": {"type": "string"}}}),
        _FauxTool("fr_stock_search", "Recherche stock.", _leve_type, {"type": "object"}),
        _FauxTool("browser_eval", "Évalue du JS.", _echo, {"type": "object"}),
        _FauxTool("oto_whoami", "Identité de session.", _echo, {"type": "object"}),
        _FauxTool("sirene_search", "Recherche SIRENE.", _leve, {"type": "object"},
                  {"type": "object"}),
    ]
    ecrits: list = []
    monkeypatch.setattr(tm.tool_registry, "_INSTANCE", _FausseInstance(outils))
    monkeypatch.setattr(tm.tool_registry, "_REGISTRY", None)
    monkeypatch.setattr(tm.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(tm.db, "list_user_disabled_tools",
                        lambda sub, org: ["fr_stock_search", "outil_disparu"])
    for nom in ("add_user_disabled_tool", "remove_user_disabled_tool",
                "add_user_enabled_tool", "remove_user_enabled_tool"):
        monkeypatch.setattr(tm.db, nom,
                            (lambda n: lambda *a: ecrits.append((n,) + a))(nom))

    class _Conn:
        name, label, kind = "sirene", "Entreprises FR", "native"

    monkeypatch.setattr(tm.connectors, "connector_for_namespace",
                        lambda ns: _Conn() if ns == "fr" else None)
    from oto_mcp import providers
    monkeypatch.setattr(providers, "connector_for_namespace",
                        lambda ns: _Conn() if ns == "fr" else None)
    return ecrits


# --- 1. L'ordre est un contrat de ROUTAGE -----------------------------------

def test_registry_precede_le_motif_generique():
    """Le seul test qui compte pour l'ordre : on résout l'URL contre le VRAI routeur.
    Relire l'ordre du code prouverait que le code n'a pas bougé, pas qu'il route bien."""
    from starlette.routing import Match, Router
    from oto_mcp import api_routes

    router = Router(routes=api_routes.make_routes(object(), mcp_instance=None))
    scope = {"type": "http", "method": "GET", "path": "/api/me/tools/registry",
             "root_path": "", "headers": [], "query_string": b""}
    gagnante = next(r for r in router.routes if r.matches(scope)[0] == Match.FULL)
    assert gagnante.path == "/api/me/tools/registry", (
        f"`{gagnante.path}` a gagné : le motif générique est passé devant, et "
        "`registry` est servi comme un nom d'outil.")


# --- 2. La liste et le registre ---------------------------------------------

def test_la_liste_reinjecte_les_desactives(monkeypatch, socle):
    """Le middleware retire les désactivés de `list_tools` ; sans réinjection, la
    grille du dashboard ne pourrait plus les RÉACTIVER — ils auraient disparu."""
    stub_authz(monkeypatch)
    code, out = call("me.tools.list")
    assert code == 200, out
    par_nom = {t["name"]: t for t in out["tools"]}
    assert "outil_disparu" in par_nom, "un désactivé absent du serveur doit rester listé"
    assert par_nom["fr_stock_search"]["enabled"] is False
    assert par_nom["fr_get"]["enabled"] is True
    assert [t["name"] for t in out["tools"]] == sorted(par_nom), "tri par nom"


def test_les_proteges_sont_marques(monkeypatch, socle):
    stub_authz(monkeypatch)
    _, out = call("me.tools.list")
    par_nom = {t["name"]: t["protected"] for t in out["tools"]}
    assert par_nom["oto_whoami"] is True and par_nom["fr_get"] is False


def test_le_registre_ecrete_la_description_a_une_ligne(monkeypatch, socle):
    """`description` est un RÉSUMÉ (la 1ʳᵉ ligne, écrêtée), pas la fiche : c'est ce qui
    distingue le registre de `…/detail`, qui rend la docstring entière."""
    stub_authz(monkeypatch)
    code, out = call("me.tools.registry")
    assert code == 200, out
    assert out["count"] == len(out["tools"])
    e = next(t for t in out["tools"] if t["name"] == "fr_get")
    assert "\n" not in e["description"]
    assert e["source"] == "native"


# --- 3. L'inversion POST/DELETE ---------------------------------------------

def test_post_desactive_et_delete_reactive(monkeypatch, socle):
    """L'inversion est HISTORIQUE : le chemin nomme la ligne de denylist, pas le tool.
    Poser la ligne (POST) masque, la retirer (DELETE) démasque."""
    stub_authz(monkeypatch)
    code, out = call("me.tools.disable", path_params={"name": "fr_get"})
    assert (code, out) == (200, {"ok": True, "name": "fr_get", "enabled": False})
    code, out = call("me.tools.enable", path_params={"name": "fr_get"})
    assert (code, out) == (200, {"ok": True, "name": "fr_get", "enabled": True})


def test_desactiver_leve_aussi_l_override_positif(monkeypatch, socle):
    """Sinon un tool masqué-par-défaut déjà forcé visible resterait visible : la ligne
    négative serait posée, l'override positif la lèverait, et rien ne se passerait."""
    stub_authz(monkeypatch)
    call("me.tools.disable", path_params={"name": "browser_eval"})
    assert socle == [("add_user_disabled_tool", "u-1", "browser_eval", 35),
                     ("remove_user_enabled_tool", "u-1", "browser_eval", 35)]


def test_reactiver_un_masque_par_defaut_pose_l_override(monkeypatch, socle):
    """Retirer la ligne négative ne suffit pas sur un masqué-par-défaut plateforme :
    il faut l'override positif, sinon le tool reste invisible et le 200 ment."""
    stub_authz(monkeypatch)
    call("me.tools.enable", path_params={"name": "browser_eval"})
    assert socle == [("remove_user_disabled_tool", "u-1", "browser_eval", 35),
                     ("add_user_enabled_tool", "u-1", "browser_eval", 35)]


def test_un_outil_protege_refuse_le_masquage(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.tools.disable", path_params={"name": "oto_whoami"})
    assert code == 400 and out["error"] == "protected_tool:oto_whoami"
    assert socle == [], "rien ne doit être écrit quand la bascule est refusée"


# --- 4. La fiche ------------------------------------------------------------

def test_la_fiche_rend_la_description_entiere_et_les_schemas(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.tools.detail", path_params={"name": "fr_get"})
    assert code == 200, out
    assert out["description"] == "Fiche entreprise agrégée.\nSuite."   # strip, pas écrêtage
    assert out["input_schema"]["properties"] == {"siren": {"type": "string"}}
    assert out["output_schema"] is None       # un outil n'est pas tenu d'en déclarer un
    assert out["connector"] == {"name": "sirene", "label": "Entreprises FR"}
    assert set(out) == set(tm.ToolDetailView.model_fields)


def test_la_fiche_d_un_outil_inconnu_est_un_404_nomme(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.tools.detail", path_params={"name": "inconnu"})
    assert code == 404 and out["error"] == "unknown_tool:inconnu"


# --- 5. Le corps LIBRE de `…/call` ------------------------------------------

@pytest.mark.parametrize("corps,attendu", [
    ({"siren": "123"}, {"siren": "123"}),                 # arguments NUS
    ({"arguments": {"siren": "123"}}, {"siren": "123"}),  # ENVELOPPÉS
    ({}, {}),
    (None, {}),                                           # aucun corps
    ([1, 2], {}),                                         # corps non-objet
    ({"arguments": "x"}, {}),                             # enveloppe non-objet
])
def test_les_deux_formes_du_corps_et_leurs_degenerescences(monkeypatch, socle,
                                                           corps, attendu):
    """Sans `body_field`, la garde de champ inconnu refuserait `siren` — un 400 sur
    chaque test d'outil. Et les quatre dégénérescences doivent donner « aucun
    argument », jamais une erreur : c'est le comportement servi depuis toujours."""
    stub_authz(monkeypatch)
    code, out = call("me.tools.call", path_params={"name": "fr_get"}, body=corps)
    assert code == 200, out
    assert out["result"] == {"echo": attendu}
    assert out["ok"] is True and isinstance(out["elapsed_ms"], int)


def test_l_erreur_de_l_outil_revient_en_donnee_pas_en_4xx(monkeypatch, socle):
    """Voir ce que renvoie l'outil — y compris son erreur — EST le but du bouton
    « tester ». Un 500 masquerait le message, qui est le seul résultat utile."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(tm, "is_testable", lambda n: n == "sirene_search")
    code, out = call("me.tools.call", path_params={"name": "sirene_search"}, body={})
    assert code == 200
    assert out == {"ok": False, "name": "sirene_search", "error": "amont indisponible"}


def test_un_mauvais_argument_est_un_400_actionnable(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.tools.call", path_params={"name": "fr_stock_search"},
                     body={"zzz": 1})
    assert code == 400
    assert out["error"] == "bad_arguments:unexpected keyword argument 'zzz'"


def test_un_outil_non_testable_est_refuse_avant_tout(monkeypatch, socle):
    """⚠️ Le gate de testabilité passe AVANT la résolution du nom : un outil INCONNU
    rend donc `403 not_testable`, pas `404 unknown_tool`. C'est l'ordre servi depuis
    toujours — le figer évite qu'un « nettoyage » de l'ordre change deux codes."""
    stub_authz(monkeypatch)
    assert call("me.tools.call", path_params={"name": "sirene_search"},
                body={})[1]["error"] == "not_testable:sirene_search"
    code, out = call("me.tools.call", path_params={"name": "inconnu"}, body={})
    assert code == 403 and out["error"] == "not_testable:inconnu"


# --- 6. Ce qui CHANGE pour un appelant --------------------------------------

def test_un_champ_inconnu_hors_corps_est_desormais_refuse(monkeypatch, socle):
    """**Changement visible.** Le corps de `…/call` est libre (il EST les arguments),
    mais la query string, elle, reste couverte par la garde : un paramètre non déclaré
    était jeté en silence, il est maintenant refusé en nommant le champ. Aucun
    consommateur connu n'en envoie sur ces six chemins."""
    stub_authz(monkeypatch)
    code, out = call("me.tools.list", query=b"filtre=x")
    assert code == 400
    assert out["error"] == "unknown_fields" and "filtre" in out["detail"]
