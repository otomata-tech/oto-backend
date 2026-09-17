"""`/shell` v0 — le chrome en un appel (contrat front `shell-contract.md`).

Ce que ces tests figent n'est pas « le handler rend un dict » : ce sont les quatre
promesses qu'on a signées, et le prédicat sans lequel le rail avale le datastore.
"""
import json

import pytest

from oto_mcp.capabilities import shell as S
from oto_mcp.capabilities._types import NotModified, ResolvedCtx
from oto_mcp.db import nodes as db_nodes
from oto_mcp.db import shell as db_shell

CTX = ResolvedCtx(sub="u1", org_id=2)


# ── Le prédicat de genre : la garde qui vaut pour le modèle ET pour l'index ─────
def test_chaque_requete_du_rail_exclut_les_LIGNES():
    """Le test lit le SQL, pas un résultat.

    Un banc peuplé de pages sans lignes passerait sans rien prouver — c'est
    exactement la forme de test qui a laissé passer des bugs ici (un banc qui
    reconstitue une partie du système et promet le tout). On vérifie donc que le
    prédicat est DANS le texte de chaque requête qui lit `nodes`.
    """
    import ast
    import inspect

    source = inspect.getsource(db_shell)
    arbre = ast.parse(source)
    lecteurs = []
    for fn in [n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)]:
        corps = ast.get_source_segment(source, fn) or ""
        if "FROM nodes" not in corps:
            continue
        lecteurs.append(fn.name)
        # Par FONCTION, pas par ligne : une requête tient sur plusieurs lignes, et un
        # test qui découpe au saut de ligne échouerait sur du SQL bien formaté tout en
        # laissant passer une requête écrite d'un seul tenant.
        assert "_HORS_LIGNES" in corps, f"{fn.name} lit `nodes` sans le prédicat de genre"
    assert lecteurs, "aucune lecture de `nodes` trouvée — le test ne garde plus rien"
    assert db_shell._HORS_LIGNES == "n.kind <> 'ligne'"


def test_le_pont_vers_les_grants_est_le_MEME_calcul_que_la_conversion():
    """L'identifiant dérivé doit matcher celui que le SQL de conversion produit.

    Les deux implémentations existent (une SQL, une Python) parce que recalculer un
    md5 en base pour chaque grant coûterait plus que de le faire ici. Le prix, c'est
    ce test : on compare les DEUX, jamais une constante gelée — une constante ne
    dirait rien le jour où la formule bouge d'un seul côté.
    """
    sql = db_nodes._public_id_sql("prj", "42")
    # Le SQL est `'nod_' || substr(md5('prj:' || (42)::text), 1, 24)` : on reproduit
    # sa sémantique sans base, et on la confronte à notre dérivation.
    import hashlib
    attendu = "nod_" + hashlib.md5(b"prj:42").hexdigest()[:24]
    assert db_shell._public_id_derive("prj", "42") == attendu
    assert "md5('prj:'" in sql and "1, 24" in sql


def test_les_TROIS_natures_de_partage_designent_un_noeud():
    """Depuis le lot ⑧, `guide` a sa famille de conversion.

    ⚠️ Ce test affirmait le contraire jusqu'au 21/08 — « une procédure partagée est
    comptée faute de nœud » — et c'était vrai : le compteur existait pour qu'une section
    « Partagé » incomplète ne se lise pas comme « rien de partagé ». La conversion des
    procédures ferme ce trou.
    """
    par_id, sans_noeud = db_shell.resolve_grant_nodes([
        {"resource_type": "project", "resource_id": "7"},
        {"resource_type": "doctrine", "resource_id": "41"},
        {"resource_type": "datastore_namespace", "resource_id": "12"},
    ])
    assert len(par_id) == 3 and sans_noeud == 0


def test_le_compteur_RESTE_pour_la_prochaine_nature_sans_noeud():
    # On ne retire pas un compteur parce qu'il vaut zéro : c'est lui qui signalera la
    # nature suivante, et un compteur retiré ne se remet pas.
    par_id, sans_noeud = db_shell.resolve_grant_nodes([
        {"resource_type": "une_nature_future", "resource_id": "1"},
    ])
    assert not par_id and sans_noeud == 1


# ── Les trois garanties du contrat ──────────────────────────────────────────────
def _ligne(pid, *, owner, oid, parent=None, nid=None, title="T", kind="page"):
    return {"public_id": pid, "parent_id": parent, "id": nid or int(pid[-1]),
            "kind": kind, "owner_type": owner, "owner_id": oid,
            "position": 0, "title": title}


@pytest.fixture
def seams(monkeypatch):
    etat = {"lignes": [], "grants": [], "partages": []}
    monkeypatch.setattr(S.org_store, "get_org",
                        lambda oid: {"name": "Acme", "logo_url": "https://l"})
    monkeypatch.setattr(S.group_store, "list_groups_for_user",
                        lambda sub, oid: [{"group_id": 9, "name": "Finance"}])
    monkeypatch.setattr(S.db_shell, "nodes_for_owners", lambda o: etat["lignes"])
    monkeypatch.setattr(S.project_nodes, "lignes_pour_proprietaires", lambda o: [])
    monkeypatch.setattr(S.db_shell, "direct_grants", lambda sub: etat["grants"])
    monkeypatch.setattr(S.db_shell, "nodes_by_public_id", lambda ids: etat["partages"])
    monkeypatch.setattr(S.db_shell, "names_of", lambda subs: {"u1": "Alexis", "u2": "Théo"})
    monkeypatch.setattr(S, "_connecteurs", lambda sub, oid: [])
    monkeypatch.setattr(S, "_compteurs", lambda ctx: {"home": 2})
    return etat


def test_l_ordre_des_sections_vient_de_NOUS(seams):
    out = S._compose(CTX)
    assert [s["kind"] for s in out["sections"]] == ["everyone", "team", "private"]


def test_shared_est_ABSENTE_quand_vide_et_SANS_contexte_sinon(seams):
    assert all(s["kind"] != "shared" for s in S._compose(CTX)["sections"])

    seams["grants"] = [{"resource_type": "project", "resource_id": "7",
                        "granted_by": "u2"}]
    pid = db_shell._public_id_derive("prj", "7")
    seams["partages"] = [_ligne(pid, owner="user", oid="u9", nid=77)]
    sections = S._compose(CTX)["sections"]
    shared = next(s for s in sections if s["kind"] == "shared")
    assert "context" not in shared          # le SEUL cas sans contexte
    assert shared["nodes"][0]["sharedBy"] == "Théo"
    assert all("context" in s for s in sections if s["kind"] != "shared")


def test_PAS_DE_DOUBLON_un_noeud_couvert_par_l_equipe_ne_revient_pas_en_partage(seams):
    """La garantie 1, et le cas exact qu'elle vise.

    Un nœud possédé par mon équipe ET partagé nominativement se range sous l'équipe.
    Sans ça le rail montre deux fois la même page, et on en déduit qu'il y en a deux.
    """
    pid = db_shell._public_id_derive("prj", "7")
    seams["lignes"] = [_ligne(pid, owner="group", oid="9", nid=77)]
    seams["grants"] = [{"resource_type": "project", "resource_id": "7",
                        "granted_by": "u2"}]
    # Le partage est bien émis, mais le nœud est DÉJÀ rangé → pas de section shared.
    sections = S._compose(CTX)["sections"]
    assert all(s["kind"] != "shared" for s in sections)
    equipe = next(s for s in sections if s["kind"] == "team")
    assert [n["id"] for n in equipe["nodes"]] == [pid]


def test_origin_porte_l_equipe_et_seulement_sur_team(seams):
    sections = S._compose(CTX)["sections"]
    equipe = next(s for s in sections if s["kind"] == "team")
    assert equipe["origin"] == "9"
    assert all("origin" not in s for s in sections if s["kind"] != "team")


def test_le_prive_garde_ce_que_la_personne_a_PARTAGE(seams):
    # Nuance assumée : c'est SON rail. Une page disparaissant de son propre rail au
    # moment où elle la partage ferait lire le partage comme une perte.
    seams["lignes"] = [_ligne("nod_a", owner="user", oid="u1", nid=1)]
    seams["grants"] = [{"resource_type": "project", "resource_id": "1",
                        "granted_by": "u1"}]
    prive = next(s for s in S._compose(CTX)["sections"] if s["kind"] == "private")
    assert [n["id"] for n in prive["nodes"]] == ["nod_a"]


# ── L'arbre : borné en profondeur ET en nombre, ce qui dépasse est COMPTÉ ───────
def test_la_profondeur_se_coupe_et_le_reste_est_compte():
    lignes = [_ligne("nod_r", owner="org", oid="2", nid=1)]
    parent = 1
    for i in range(2, 7):                      # une branche de 5 niveaux
        lignes.append(_ligne(f"nod_{i}", owner="org", oid="2", parent=parent, nid=i))
        parent = i
    arbre = S._arbre(lignes)
    assert len(arbre) == 1                     # une seule racine
    n = arbre[0]
    # `_PROFONDEUR = 2` rend la racine PLUS deux niveaux — le même compte que l'épine
    # d'un projet, dont ce code est le patron. La branche est coupée au 3ᵉ.
    assert n.children[0].children[0].children is None
    assert n.children[0].children[0].more == 3   # les 3 niveaux coupés, comptés


def test_un_noeud_dont_le_parent_vit_AILLEURS_remonte_en_racine():
    # Sinon une page rattachée à un parent d'une autre section disparaît du rail.
    arbre = S._arbre([_ligne("nod_x", owner="user", oid="u1", parent=999, nid=5)])
    assert [n.id for n in arbre] == ["nod_x"]


def test_le_budget_borne_une_section_large():
    lignes = [_ligne(f"nod_{i}", owner="org", oid="2", nid=i) for i in range(1, 400)]
    arbre = S._arbre(lignes)
    assert len(arbre) <= S._BUDGET_PAR_SECTION
    # Ce qui a été coupé est COMPTÉ, jamais tu.
    assert arbre[-1].more == 399 - len(arbre)


# ── `rev` et le 304 ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_meme_rev_rend_la_sentinelle_pas_un_corps(seams):
    corps = S._compose(CTX)
    res = await S._shell(CTX, S.ShellInput(rev=corps["rev"]))
    assert isinstance(res, NotModified) and res.rev == corps["rev"]


@pytest.mark.asyncio
async def test_un_rev_perime_rend_le_corps_entier(seams):
    res = await S._shell(CTX, S.ShellInput(rev="périmé"))
    assert isinstance(res, dict) and res["sections"]


def test_le_rev_suit_un_changement_que_nodes_updated_at_ne_verrait_PAS(seams, monkeypatch):
    # Le rail dépend du nom de l'org, d'une appartenance d'équipe, d'un verdict de
    # connecteur — rien de tout ça ne touche `nodes.updated_at`. Une empreinte de
    # contenu ne peut pas rater ce qu'un horodatage ignore.
    avant = S._compose(CTX)["rev"]
    monkeypatch.setattr(S.org_store, "get_org", lambda oid: {"name": "Acme RENOMMÉE"})
    assert S._compose(CTX)["rev"] != avant


def test_le_rev_ne_se_calcule_PAS_sur_lui_meme(seams):
    corps = S._compose(CTX)
    sans = {k: v for k, v in corps.items() if k != "rev"}
    assert corps["rev"] == S._rev(sans)


# ── La forme déclarée est celle qui est servie ─────────────────────────────────
def test_le_corps_valide_l_Output_declare(seams):
    # `Output` DÉCRIT sans valider à l'exécution ; ce test est l'endroit où la
    # divergence se voit, plutôt que chez le client.
    S.ShellOut(**S._compose(CTX))


def test_les_compteurs_absents_valent_mieux_que_faux():
    # `home` comptait l'inbox, retirée (oto#191) : rien n'est compté, donc aucune clé —
    # jamais `{"home": 0}`, qui affirmerait « rien ne t'attend ».
    assert S._compteurs(CTX) == {}


# ── Le moule : chaque adaptateur traduit la sentinelle dans SON transport ───────
@pytest.mark.asyncio
async def test_REST_rend_une_304_NUE():
    """304 sans corps — c'est la spec, et c'est tout l'intérêt.

    Une 200 portant « rien n'a changé » ferait ranger CE message dans le cache du
    client à la place des données. La différence ne se voit pas au niveau du handler ;
    elle se voit au deuxième appel d'un vrai client, et c'est pour ça qu'elle se teste
    à l'ADAPTATEUR.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from oto_mcp.capabilities import _rest_adapter as R
    from oto_mcp.capabilities._types import Capability, RestBinding

    cap = Capability(key="t.nm", handler=lambda ctx, inp: NotModified("abc"),
                     Input=S.ShellInput, authz=lambda raw, inp: ResolvedCtx(sub="u1"),
                     rest=RestBinding("GET", "/t"))
    binding = cap.rest_bindings()[0]

    async def _auth(request, verifier):
        return "u1", None

    def _jr(request, payload, status=200):
        return JSONResponse(payload, status_code=status,
                            headers={"Access-Control-Allow-Origin": "https://x"})

    h = R._make_handler(cap, binding, None, _auth, _jr, lambda *a, **k: None)
    scope = {"type": "http", "method": "GET", "path": "/t", "headers": [],
             "query_string": b"", "path_params": {}}
    resp = await h(Request(scope))
    assert resp.status_code == 304
    assert resp.body == b""
    # Une 304 reste une réponse pour le navigateur : sans CORS, le dashboard voit une
    # erreur là où le serveur a répondu « ton cache est bon ».
    assert resp.headers.get("access-control-allow-origin") == "https://x"


@pytest.mark.asyncio
async def test_MCP_rend_une_DONNEE_faute_de_code_d_etat(monkeypatch):
    from oto_mcp.capabilities import _mcp_adapter as M
    from oto_mcp.capabilities._types import Capability

    cap = Capability(key="t.nm2", handler=lambda ctx, inp: NotModified("abc"),
                     Input=S.ShellInput, authz=lambda raw, inp: ResolvedCtx(sub="u1"),
                     mcp="t_nm2")
    monkeypatch.setattr(M, "current_user_sub_from_token", lambda: "u1")
    tool = M._make_tool(cap)
    assert await tool(rev="abc") == {"not_modified": True, "rev": "abc"}


def test_la_surface_est_DECLAREE_provisoire():
    # Sans la marque, une absence de mention se lit comme « forme gravée » — et le
    # front s'y brancherait en croyant le contrat figé.
    from oto_mcp import openapi
    from oto_mcp.capabilities import registry

    cap = next(c for c in registry.CAPABILITIES if c.key == "me.shell")
    binding = cap.rest_bindings()[0]
    assert binding.provisoire is True
    op, _ = openapi._operation(cap, binding)
    assert op["x-oto-provisoire"] is True
