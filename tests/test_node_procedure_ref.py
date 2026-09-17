"""Un nœud « agent » mène à sa fiche de procédure (#417) — sur les deux surfaces.

Le `nod_*` d'un agent est DÉRIVÉ (`md5('prc:' || id)`) : rien ne permettait d'en
revenir à l'id de guide que la fiche accepte. Le nœud porte donc sa référence,
`{id, slug, scope}`, lue dans ses propriétés — une seule définition
(`capabilities/node_procedure_ref`) pour le rail ET la fiche, et `None` partout où
elle n'est pas LISIBLE : jamais un id deviné.
"""
import hashlib

import pytest

from oto_mcp.capabilities import node_procedure_ref as P
from oto_mcp.capabilities import node_view as N
from oto_mcp.capabilities import shell as S
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.db import nodes as db_nodes
from oto_mcp.db import shell as db_shell

CTX = ResolvedCtx(sub="u1", org_id=2)


# ── Le rail ────────────────────────────────────────────────────────────────────
def _ligne(pid, *, owner="org", oid="2", role=None, legacy=None, legacy_id=None,
           slug=None, kind="page", nid=None):
    return {"public_id": pid, "parent_id": None, "id": nid or 1, "kind": kind,
            "owner_type": owner, "owner_id": oid, "position": 0, "title": "T",
            "role": role, "legacy": legacy, "legacy_id": legacy_id, "slug": slug}


def test_le_rail_lit_la_reference_dans_la_MEME_requete_sans_jointure():
    """Trois colonnes de plus dans le SELECT — pas une requête par nœud, pas de JOIN :
    le rail est le chemin le plus chaud du produit."""
    for cle in ("'legacy'", "'legacy_id'", "'slug'"):
        assert cle in db_shell._COLS, f"{cle} n'est pas lu par le rail"
    import inspect
    import re
    # Le mot-clé SQL, en majuscules — `','.join(` du Python n'en est pas un.
    assert not re.search(r"\bJOIN\b", inspect.getsource(db_shell.nodes_for_owners))


def test_un_agent_du_rail_porte_sa_procedure_id_slug_scope():
    # `legacy_id` arrive en TEXTE (`props->>`), et doit sortir en entier.
    noeuds = S._arbre([_ligne("nod_a", owner="group", oid="9", role="procedure",
                              legacy="prc", legacy_id="41", slug="qualification")])
    assert noeuds[0].type == "agent"
    assert noeuds[0].procedure == P.ProcedureRef(id=41, slug="qualification", scope="group")
    dump = noeuds[0].model_dump(exclude_none=True)
    assert dump["procedure"] == {"id": 41, "slug": "qualification", "scope": "group"}


def test_une_page_du_rail_n_a_PAS_de_procedure_et_le_dump_l_omet():
    noeuds = S._arbre([_ligne("nod_p")])
    assert noeuds[0].procedure is None
    assert "procedure" not in noeuds[0].model_dump(exclude_none=True)


def test_un_agent_SANS_reference_lisible_ne_recoit_RIEN():
    """Un nœud natif à rôle `procedure` (pas de `legacy`), une famille inconnue, ou un
    id illisible : on ne sert rien plutôt qu'un id deviné — c'est ce 404-là qu'on
    retire, pas un autre qu'on fabrique."""
    natif = _ligne("nod_n", role="procedure")
    autre_famille = _ligne("nod_f", role="procedure", legacy="prj", legacy_id="41")
    illisible = _ligne("nod_i", role="procedure", legacy="prc", legacy_id="abc")
    for l in (natif, autre_famille, illisible):
        n = S._arbre([l])[0]
        assert n.type == "agent"
        assert n.procedure is None, l["public_id"]


def test_le_scope_est_le_PROPRIETAIRE_du_noeud():
    org = S._arbre([_ligne("nod_o", owner="org", oid="2", role="procedure",
                           legacy="prc", legacy_id="7")])[0]
    equipe = S._arbre([_ligne("nod_g", owner="group", oid="9", role="procedure",
                              legacy="prc", legacy_id="8")])[0]
    assert org.procedure.scope == "org" and equipe.procedure.scope == "group"


def test_le_rev_du_rail_couvre_la_reference(monkeypatch):
    """Deux rails identiques sauf l'id de procédure ont deux `rev` : un client qui
    porte l'ancien `rev` ne recevra pas un 304 pour un lien qui a changé."""
    monkeypatch.setattr(S.org_store, "get_org", lambda oid: {"name": "Acme"})
    monkeypatch.setattr(S.group_store, "list_groups_for_user", lambda sub, oid: [])
    monkeypatch.setattr(S.db_shell, "direct_grants", lambda sub: [])
    monkeypatch.setattr(S.db_shell, "nodes_by_public_id", lambda ids: [])
    monkeypatch.setattr(S.project_nodes, "lignes_pour_proprietaires", lambda o: [])
    monkeypatch.setattr(S.db_shell, "names_of", lambda subs: {})
    monkeypatch.setattr(S, "_connecteurs", lambda sub, oid: [])
    monkeypatch.setattr(S, "_compteurs", lambda ctx: {})
    monkeypatch.setattr(S, "_executions", lambda sub, oid: [])
    revs = []
    for ident in ("41", "42"):
        monkeypatch.setattr(S.db_shell, "nodes_for_owners", lambda o, i=ident: [
            _ligne("nod_a", role="procedure", legacy="prc", legacy_id=i)])
        revs.append(S._compose(CTX)["rev"])
    assert revs[0] != revs[1]


# ── La fiche ───────────────────────────────────────────────────────────────────
AGENT = {"id": 3, "public_id": "nod_ag", "parent_id": None, "kind": "page",
         "owner_type": "org", "owner_id": "2", "position": 0,
         "props": {"title": "Qualifier", "role": "procedure", "legacy": "prc",
                   "legacy_id": 41, "slug": "qualification", "created_by": "u1"},
         "created_at": "2026-08-01", "updated_at": "2026-08-14 10:00:00"}
PAGE = {**AGENT, "public_id": "nod_page",
        "props": {"title": "Brief", "created_by": "u1"}}


@pytest.fixture
def seams(monkeypatch):
    etat = {"fiche": AGENT}
    monkeypatch.setattr(N.db_node, "node_by_public_id", lambda pid: etat["fiche"])
    monkeypatch.setattr(N.db_node, "blocks_of", lambda nid: [])
    monkeypatch.setattr(N.db_node, "ancestors_of", lambda nid, max_depth=12: [])
    monkeypatch.setattr(N.db_node, "siblings_of", lambda p, owner, cap=50: {})
    monkeypatch.setattr(N.db_shell, "direct_grants", lambda sub: [])
    monkeypatch.setattr(N.db_shell, "names_of", lambda subs: {"u1": "Alexis"})
    monkeypatch.setattr(N.ownership, "active_org_principals",
                        lambda sub, org: [("org", "2"), ("user", "u1"), ("group", "9")])
    return etat


def test_la_fiche_d_un_agent_porte_sa_procedure(seams):
    corps = N._compose(CTX, "nod_ag")
    assert corps["type"] == "agent"
    assert corps["procedure"] == {"id": 41, "slug": "qualification", "scope": "org"}
    N.NodeOut(**corps)          # la forme servie valide l'Output déclaré


def test_la_fiche_d_une_page_sert_NULL_pas_une_absence(seams):
    """`null` dit « n'exécute aucune procédure » — une affirmation qu'on peut tenir,
    à la différence d'`access` et `dependencies`, ABSENTS parce qu'inconnus."""
    seams["fiche"] = PAGE
    corps = N._compose(CTX, "nod_page")
    assert "procedure" in corps and corps["procedure"] is None
    N.NodeOut(**corps)


def test_la_fiche_d_un_agent_SANS_reference_sert_NULL(seams):
    seams["fiche"] = {**AGENT, "props": {"title": "Natif", "role": "procedure"}}
    corps = N._compose(CTX, "nod_ag")
    assert corps["type"] == "agent" and corps["procedure"] is None


def test_le_rev_de_la_fiche_couvre_la_reference(seams):
    a = N._compose(CTX, "nod_ag")["rev"]
    seams["fiche"] = {**AGENT, "props": {**AGENT["props"], "legacy_id": 42}}
    assert N._compose(CTX, "nod_ag")["rev"] != a


# ── Une définition, deux surfaces ──────────────────────────────────────────────
def test_les_deux_surfaces_disent_la_MEME_reference(seams):
    fiche = N._compose(CTX, "nod_ag")["procedure"]
    rail = S._arbre([_ligne("nod_ag", owner="org", oid="2", role="procedure",
                            legacy="prc", legacy_id="41", slug="qualification")])[0]
    assert rail.procedure.model_dump() == fiche


def test_la_famille_est_CELLE_de_la_conversion_et_du_pont_des_grants():
    # Trois écritures de `prc`, comparées ici plutôt qu'importées l'une de l'autre —
    # même patron que `_public_id_derive` ↔ `_public_id_sql`.
    assert P.FAMILLE_GUIDE == db_nodes._FAMILY_GUIDE == db_shell._FAMILLE_PAR_GRANT["doctrine"]
    # Et l'id référencé est bien celui dont le `nod_*` dérive.
    attendu = "nod_" + hashlib.md5(b"prc:41").hexdigest()[:24]
    assert db_shell._public_id_derive(P.FAMILLE_GUIDE, "41") == attendu


def test_seule_la_nature_agent_porte_une_reference():
    source = {"legacy": "prc", "legacy_id": 41, "slug": "x"}
    assert P.procedure_ref_of("agent", "org", source) is not None
    for nature in ("page", "table", "execution"):
        assert P.procedure_ref_of(nature, "org", source) is None


def test_la_reference_est_dans_les_schemas_SERVIS():
    """Déclarée dans l'Output → OpenAPI (REST) et output_schema (MCP), pas seulement
    dans un `return`."""
    for modele in (S.RailNode, N.NodeOut):
        schema = modele.model_json_schema()
        # `RailNode` est récursif (`children`) : pydantic le range dans `$defs` et
        # met un `$ref` au sommet.
        props = schema.get("properties") or schema["$defs"][modele.__name__]["properties"]
        assert "procedure" in props, modele.__name__
        assert "ProcedureRef" in schema["$defs"], modele.__name__
    ref = P.ProcedureRef.model_json_schema()
    assert set(ref["properties"]) == {"id", "slug", "scope"}
    assert ref["required"] == ["id", "scope"]
