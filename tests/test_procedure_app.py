"""`oto_procedure_app` — le graphe d'une procédure, peint par le bundle d'un front.

Trois choses à figer :
1. **sans `OTO_PROCEDURE_APP_BUNDLE`, rien ne s'enregistre** — ni outil, ni page ;
2. **la page charge le bundle déclaré**, et sa CSP admet son origine et les logos ;
3. **le tracé arrive à la vue SANS ses fences, le modèle ne lit qu'une ligne**, et le
   canal structuré survit à la chaîne servie (`UnSeulCanal`, signal #1083).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from _modules_recharges import recharger_module
from fastmcp import Client, FastMCP

BUNDLE = "https://app.example.com/mcp-app/process-graph.js"

from oto_mcp.procedure_diagram import client_affiche_les_apps as _VRAIE_DETECTION  # noqa: E402
from oto_mcp.tools.guide_run import procedure_servie  # noqa: E402

TRACE = """┌──────────────────┐
│ 1. Trigger       │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 2. serper_search │
└──────────────────┘"""

CORPS = f"# Veille\n\nLe déroulé.\n\n```\n{TRACE}\n```\n\n## Étapes\n\n1. Chercher.\n"


def _module(monkeypatch):
    return recharger_module(monkeypatch, "oto_mcp.tools.procedure_app")


def _stub_backend(monkeypatch, procedures: dict, *, admin: bool = True, worker: bool = False,
                  prefix: str = "", ui: bool = True):
    from oto_mcp import access, org_store, procedure_diagram, providers, roles, tool_alias
    # The test client advertises no MCP Apps extension: say what the host would.
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: ui)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: admin)
    monkeypatch.setattr(tool_alias, "sert_un_worker", lambda: worker)
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: prefix)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub1")
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda otype, oid, slug, version=None: procedures.get((otype, oid, slug)))
    monkeypatch.setattr(providers, "public_catalog", lambda: [
        {"name": "serper", "label": "Serper", "category": "search",
         "logo_url": "https://img.logo.dev/serper.dev", "namespaces": ["serper"]},
        {"name": "hunter", "label": "Hunter.io", "category": "enrichment",
         "logo_url": "https://img.logo.dev/hunter.io", "namespaces": ["hunter"]},
    ])


def _banc(monkeypatch, pa):
    from _mcp_app import static_mcp

    from oto_mcp.middleware import un_seul_canal
    # `static_mcp` est un catalogue MIS EN CACHE pour toute la suite : construit ici sous
    # le bundle du test, il enregistrait `oto_procedure_app` pour chaque test suivant du
    # même worker, et `test_surface_outils_figee` y voyait un outil ajouté — rouge
    # seulement quand la répartition les rapprochait.
    with monkeypatch.context() as sans_bundle:
        sans_bundle.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
        middleware = static_mcp().middleware
    m = FastMCP("banc")
    for mw in middleware:
        m.add_middleware(mw)
    pa.register(m)
    un_seul_canal.retirer_les_schemas_vides(m)
    return m


def test_sans_bundle_declare_rien_ne_s_enregistre(monkeypatch):
    monkeypatch.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
    pa = _module(monkeypatch)
    m = FastMCP("banc")
    pa.register(m)

    async def lister():
        async with Client(m) as c:
            return await c.list_tools(), await c.list_resources()

    outils, pages = asyncio.run(lister())
    assert outils == [] and pages == []


def test_un_bundle_qui_n_est_pas_un_script_n_enregistre_rien(monkeypatch):
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", "https://app.example.com/mcp-app/")
    pa = _module(monkeypatch)
    m = FastMCP("banc")
    pa.register(m)

    async def lister():
        async with Client(m) as c:
            return await c.list_tools()

    assert asyncio.run(lister()) == []


def test_bout_en_bout_la_vue_recoit_le_trace_et_le_modele_une_ligne(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "veille"): {
        "slug": "veille", "title": "Veille", "version": 3, "body_md": CORPS}})

    async def appel():
        async with Client(m) as c:
            outil = next(t for t in await c.list_tools() if t.name == "oto_procedure_app")
            r = await c.call_tool("oto_procedure_app", {"slug": "veille"}, raise_on_error=False)
            uri = outil.meta["ui"]["resourceUri"]
            ui = (await c.read_resource(uri))[0]
            pages = await c.list_resources()
            return r, ui, next(p for p in pages if str(p.uri) == uri)

    r, ui, page = asyncio.run(appel())
    assert not r.is_error

    # La vue : le tracé nu, et les seuls connecteurs qu'il peut nommer.
    vue = r.structured_content
    assert vue is not None, "la vue n'a rien à peindre"
    assert vue["drawing"] == TRACE
    assert vue["title"] == "Veille" and vue["version"] == 3
    assert [c["name"] for c in vue["connectors"]] == ["serper"]
    assert vue["origin"] == "https://app.example.com"

    # Le modèle : une ligne, qui l'envoie lire les étapes ailleurs — pas le tracé.
    texte = "".join(getattr(b, "text", "") for b in r.content)
    assert "oto_procedure op=get slug=veille" in texte
    assert "┌" not in texte

    # La page : le bundle déclaré et sa feuille, une CSP qui les admet.
    assert ui.mimeType == "text/html;profile=mcp-app"
    assert f'src="{BUNDLE}"' in ui.text
    assert 'href="https://app.example.com/mcp-app/process-graph.css"' in ui.text
    csp = json.dumps(page.meta)
    assert "https://app.example.com" in csp and "https://img.logo.dev" in csp
    assert page.meta["ui"]["prefersBorder"] is True


def test_l_equipe_active_complete_l_org(monkeypatch):
    _stub_backend(monkeypatch, {("group", 7, "veille"): {
        "slug": "veille", "title": "Veille d'équipe", "version": 1, "body_md": CORPS}})
    from oto_mcp import access
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    row, scope = procedure_servie("sub1", "veille")
    assert row["title"] == "Veille d'équipe" and scope == "group"


def test_une_copie_perso_archivee_ne_masque_plus_l_org(monkeypatch):
    """#1156, décision du 14/10/2026 : la cascade par nom saute une copie perso
    ARCHIVÉE et sert l'org — ici comme dans `oto_procedure op=get`."""
    perso = {"slug": "veille", "title": "Veille perso", "version": 3, "body_md": CORPS,
             "archived_at": "2026-10-14 09:00:00+00"}
    org = {"slug": "veille", "title": "Veille d'org", "version": 5, "body_md": CORPS,
           "archived_at": None}
    procedures = {("user", "sub1", "veille"): perso, ("org", 42, "veille"): org}
    _stub_backend(monkeypatch, procedures)
    row, scope = procedure_servie("sub1", "veille")
    assert row["title"] == "Veille d'org" and scope == "org"
    perso["archived_at"] = None                     # désarchivée : elle revient en tête
    row, scope = procedure_servie("sub1", "veille")
    assert row["title"] == "Veille perso" and scope == "user"


def test_sans_dessin_ou_sans_procedure_la_vue_le_dit(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "prose"): {
        "slug": "prose", "title": "Prose", "version": 1, "body_md": "# Prose\n\n1. Faire.\n"}})

    async def appel(slug):
        async with Client(m) as c:
            return await c.call_tool("oto_procedure_app", {"slug": slug}, raise_on_error=False)

    sans_dessin = asyncio.run(appel("prose"))
    assert sans_dessin.structured_content["drawing"] is None
    assert sans_dessin.structured_content["connectors"] == []
    # Sans dessin, le modèle est envoyé en dessiner un — jamais en Mermaid — puis rappeler.
    consigne = "".join(getattr(b, "text", "") for b in sans_dessin.content)
    assert "REQUIRED NEXT STEP" in consigne and "slug=procedure-flowchart" in consigne
    assert "oto_procedure op=set slug=prose scope=org expected_version=1" in consigne
    assert "Never Mermaid" in consigne and "oto_procedure_app slug=prose again" in consigne
    assert "ONLY THEN answer" in consigne

    absente = asyncio.run(appel("inconnue"))
    assert "inconnue" in absente.structured_content["message"]
    assert "oto_procedure op=list" in "".join(getattr(b, "text", "") for b in absente.content)


def test_oto_procedure_propose_le_graphe_la_ou_il_existe(monkeypatch):
    """La lecture d'une procédure par son slug renvoie l'agent la MONTRER ; une
    instance sans bundle ne propose pas un outil qu'elle n'a pas."""
    from oto_mcp import procedure_diagram, tool_alias
    from oto_mcp.capabilities import procedure_console as pc
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")
    monkeypatch.setattr(tool_alias, "sert_un_worker", lambda: False)
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: True)

    async def lecture():
        return {"slug": "veille", "body_md": "…"}

    def lire(**kw):
        inp = pc.ProcedureInput(op="get", slug="veille", **kw)
        return asyncio.run(pc._avec_le_graphe(type("Ctx", (), {"sub": "s"})(), inp, lecture()))

    monkeypatch.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
    assert "show_graph" not in lire()

    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    assert "oto_procedure_app slug=veille" in lire()["show_graph"]
    # Une version archivée ou une procédure lue par son id : l'outil ne la relirait pas.
    assert "show_graph" not in lire(version=2)
    assert "show_graph" not in lire(guide_id=9)
    assert "show_graph" not in lire(org=7)
    assert "oto_procedure_app slug=<slug>" in procedure_diagram.indice_du_graphe("s")


def test_la_liste_des_procedures_propose_le_graphe(monkeypatch):
    """`op=list` porte la ligne qui envoie MONTRER une procédure, posée après le
    dispatch : la liste elle-même (et `include_archived`) n'y est pas touchée."""
    from oto_mcp import procedure_diagram, tool_alias
    from oto_mcp.capabilities import _execution, procedure_console as pc
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")
    monkeypatch.setattr(tool_alias, "sert_un_worker", lambda: False)
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: True)

    async def execute(_dispatch, _args):
        return None, {"items": [{"slug": "veille"}]}
    monkeypatch.setattr(_execution, "execute", execute)

    def lister():
        inp = pc.ProcedureInput(op="list")
        return asyncio.run(pc._procedure(type("Ctx", (), {"sub": "s"})(), inp))

    monkeypatch.delenv("OTO_PROCEDURE_APP_BUNDLE", raising=False)
    assert lister() == {"items": [{"slug": "veille"}]}
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    assert "oto_procedure_app slug=<slug>" in lister()["show_graph"]


def test_un_bundle_qui_n_est_pas_un_script_est_refuse(monkeypatch):
    from oto_mcp import procedure_diagram
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", "ftp://x/process-graph.js")
    assert procedure_diagram.bundle_du_graphe() is None
    assert procedure_diagram.indice_du_graphe("s", "veille") is None


def _appeler(monkeypatch, procedures, slug, **stub):
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, procedures, **stub)

    async def appel():
        async with Client(m) as c:
            return await c.call_tool("oto_procedure_app", {"slug": slug}, raise_on_error=False)

    r = asyncio.run(appel())
    return r, "".join(getattr(b, "text", "") for b in r.content)


PROSE = {"slug": "prose", "title": "Prose", "version": 1, "body_md": "# Prose\n\n1. Faire.\n"}


def test_ma_procedure_perso_passe_avant_l_org_et_s_ecrit_chez_moi(monkeypatch):
    """`op=get` lit chez SOI d'abord (ADR 0068) : l'app aussi, et la consigne d'écriture
    vise le palier lu — pas l'org, qui ferait une copie ailleurs."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    r, texte = _appeler(monkeypatch, {("user", "sub1", "prose"): PROSE,
                                      ("org", 42, "prose"): {**PROSE, "title": "Celle de l'org"}},
                        "prose", admin=False)
    assert r.structured_content["title"] == "Prose"
    assert "scope=user" in texte and "REQUIRED NEXT STEP" in texte


def test_un_travail_du_runner_n_est_jamais_envoye_dessiner(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): PROSE}, "prose", worker=True)
    assert "rendering hosts only" in texte
    assert "op=set" not in texte and "REQUIRED" not in texte
    from oto_mcp import procedure_diagram
    assert procedure_diagram.indice_du_graphe("sub1", "prose") is None


def test_un_membre_qui_ne_peut_pas_ecrire_n_est_pas_envoye_a_un_refus(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): PROSE}, "prose", admin=False)
    assert "needs an admin" in texte and "op=set" not in texte


def test_un_dessin_sous_un_tag_se_detague_il_ne_se_redessine_pas(monkeypatch):
    """La boucle sans plancher : un tracé écrit en ```text n'est pas un dessin pour la
    page — le redemander ajouterait une version à chaque appel."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    tague = {**PROSE, "body_md": f"# Prose\n\n```text\n{TRACE}\n```\n"}
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): tague}, "prose")
    assert "```text fence" in texte and "remove the `text` tag" in texte
    assert "REQUIRED NEXT STEP" not in texte


def test_la_consigne_de_dessin_ne_se_joue_qu_une_fois(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): PROSE}, "prose")
    assert "Draw at most ONCE" in texte and "stop and tell the user" in texte


def test_les_outils_cites_portent_le_nom_du_produit(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): PROSE}, "prose", prefix="acme")
    assert "acme_guide op=read" in texte and "acme_procedure op=set" in texte
    assert "acme_procedure_app slug=prose again" in texte
    assert "oto_procedure" not in texte


def test_un_dessin_ecrit_a_nu_se_met_dans_un_bloc(monkeypatch):
    """Vu le 12/10 : un modèle a écrit le tracé sans fence — la page ne le dessine pas,
    `diagram_check` n'en dit rien. L'outil demande de l'encadrer, pas de le redessiner."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    nu = {**PROSE, "body_md": f"# Prose\n\n{TRACE}\n\n## 1. Faire\n"}
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): nu}, "prose")
    assert "NOT inside a ``` fence" in texte and "REQUIRED NEXT STEP" not in texte


def test_un_flanc_decale_se_repare_pour_l_utilisateur_et_se_recopie_une_fois(monkeypatch):
    """Le tracé existe mais un flanc glisse d'une colonne : l'utilisateur voit le tracé
    RÉPARÉ tout de suite, le modèle reçoit le bloc juste à recopier — une fois, sans
    redessiner (vu : cinq réécritures sans jamais aligner)."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    decale = TRACE.replace("│ 1. Trigger       │", "│ 1. Trigger      │")
    casse = {**PROSE, "body_md": f"# Prose\n\n```\n{decale}\n```\n"}
    r, texte = _appeler(monkeypatch, {("org", 42, "prose"): casse}, "prose")
    assert r.structured_content["drawing"] == TRACE
    assert "REPAIRED" in texte and "ONCE" in texte and f"```\n{TRACE}\n```" in texte
    assert "REQUIRED NEXT STEP" not in texte

    _, lecteur = _appeler(monkeypatch, {("org", 42, "prose"): casse}, "prose", admin=False)
    assert "op=set" not in lecteur and "the graph is drawn" in lecteur


def test_un_dessin_qu_on_ne_sait_pas_reparer_se_corrige_une_fois(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    # Du texte déborde du flanc : on ne coupe pas un mot.
    deborde = TRACE.replace("│ 1. Trigger       │", "│ 1. Trigger and more words │")
    casse = {**PROSE, "body_md": f"# Prose\n\n```\n{deborde}\n```\n"}
    r, texte = _appeler(monkeypatch, {("org", 42, "prose"): casse}, "prose")
    assert r.structured_content["drawing"] == deborde
    assert "RAW CHARACTERS" in texte and "ONCE" in texte


def test_la_consigne_de_dessin_donne_la_forme_a_recopier(monkeypatch):
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): PROSE}, "prose")
    assert "exactly the width of its top line" in texte
    # The shape arrives as lines to copy, not as an escaped one-liner.
    assert "\n┌─────" in texte and "│  <Step title>" in texte and "\\n" not in texte


def test_deux_dessins_ne_sont_pas_des_caracteres_bruts(monkeypatch):
    """La page dessine le PREMIER : l'utilisateur voit un graphe, rien à réparer."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    deux = {**PROSE, "body_md": f"# P\n\n```\n{TRACE}\n```\n\n```\n{TRACE}\n```\n"}
    _, texte = _appeler(monkeypatch, {("org", 42, "prose"): deux}, "prose")
    assert "shown to the user" in texte and "RAW" not in texte


def test_un_client_texte_ne_recoit_ni_trace_ni_consigne(monkeypatch):
    """Claude Code, oto-runner : rien n'est peint et `structuredContent` va au MODÈLE.
    Le tracé (~1 000 jetons), le roster et la consigne de dessin n'y partent pas — à
    travers la vraie détection : le client de test n'annonce pas l'extension."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "veille"): {
        "slug": "veille", "title": "Veille", "version": 3, "body_md": CORPS},
        ("org", 42, "prose"): PROSE})
    from oto_mcp import org_store, procedure_diagram
    monkeypatch.setattr(org_store, "org_front", lambda org_id: ("https://app.example.com/", None))
    # The REAL detection: the test client advertises no MCP Apps extension.
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", _VRAIE_DETECTION)

    async def appel(slug):
        async with Client(m) as c:
            return await c.call_tool("oto_procedure_app", {"slug": slug}, raise_on_error=False)

    for slug in ("veille", "prose"):
        r = asyncio.run(appel(slug))
        texte = "".join(getattr(b, "text", "") for b in r.content)
        assert set(r.structured_content) == {"slug", "title", "version", "message", "url"}
        # Such a client hands structuredContent to the MODEL: the sentence must be there.
        assert "does NOT display the graph" in r.structured_content["message"]
        assert r.structured_content["url"] == f"https://app.example.com/org/42/processes/{slug}"
        assert r.structured_content["url"] in r.structured_content["message"]
        assert "┌" not in texte and "REQUIRED" not in texte
    assert procedure_diagram.indice_du_graphe("sub1", "veille") is None


def test_un_client_texte_sans_front_connu_n_a_pas_de_lien(monkeypatch):
    """Aucun front connu pour l'org ni pour le compte : pas de lien deviné."""
    pytest.importorskip("fastmcp.apps")
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", BUNDLE)
    pa = _module(monkeypatch)
    m = _banc(monkeypatch, pa)
    _stub_backend(monkeypatch, {("org", 42, "veille"): {
        "slug": "veille", "title": "Veille", "version": 3, "body_md": CORPS}}, ui=False)
    from oto_mcp import config, org_store
    monkeypatch.setattr(org_store, "org_front", lambda org_id: (None, None))
    monkeypatch.setattr(config, "dashboard_url_for", lambda sub: "")

    async def appel():
        async with Client(m) as c:
            return await c.call_tool("oto_procedure_app", {"slug": "veille"}, raise_on_error=False)

    r = asyncio.run(appel())
    assert set(r.structured_content) == {"slug", "title", "version", "message"}
    assert "http" not in r.structured_content["message"]


def test_la_detection_lit_l_extension_annoncee_par_le_client(monkeypatch):
    from fastmcp.server import dependencies
    from oto_mcp import procedure_diagram

    class Ctx:
        def __init__(self, oui):
            self.oui = oui

        def client_supports_extension(self, ext):
            return self.oui and ext == "io.modelcontextprotocol/ui"

    monkeypatch.setattr(dependencies, "get_context", lambda: Ctx(True))
    assert _VRAIE_DETECTION() is True
    monkeypatch.setattr(dependencies, "get_context", lambda: Ctx(False))
    assert _VRAIE_DETECTION() is False
    monkeypatch.setattr(dependencies, "get_context", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert _VRAIE_DETECTION() is False


# ── À la naissance : une procédure créée sans dessin repart avec la consigne ──

def _ecrire(monkeypatch, *, op="create", version=1, body="# Veille\n\n1. Chercher.\n",
            worker=False, ui=False, bundle=True, **kw):
    from oto_mcp import access, org_store, procedure_diagram, tool_alias
    from oto_mcp.capabilities import _execution, procedure_console as pc
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")
    monkeypatch.setattr(tool_alias, "sert_un_worker", lambda: worker)
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: ui)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(org_store, "org_front", lambda org_id: ("https://app.example.com/", None))
    # Le bundle se simule à sa lecture, jamais par l'env : une écriture réelle monte le
    # registre d'outils (mis en cache), qui enregistrerait l'app pour toute la suite.
    monkeypatch.setattr(procedure_diagram, "bundle_du_graphe", lambda: BUNDLE if bundle else None)

    async def execute(_dispatch, _args):
        return None, {"ok": True, "slug": "veille", "version": version, "set": True}
    monkeypatch.setattr(_execution, "execute", execute)
    inp = pc.ProcedureInput(op=op, slug="veille", body_md=body, **kw)
    return asyncio.run(pc._procedure(type("Ctx", (), {"sub": "s"})(), inp))


def test_une_procedure_nee_sans_dessin_repart_avec_la_consigne(monkeypatch):
    out = _ecrire(monkeypatch, scope="org")
    consigne = out["draw_next"]
    assert "procedure-flowchart" in consigne
    assert "op=set slug=veille scope=org expected_version=1" in consigne
    # Client texte (Claude Code) : pas d'appel à l'app, le lien de la page.
    assert "oto_procedure_app" not in consigne
    assert "https://app.example.com/org/42/processes/veille" in consigne
    assert "┌" in consigne  # la forme à recopier


def test_un_client_qui_peint_les_apps_est_renvoye_montrer_le_graphe(monkeypatch):
    consigne = _ecrire(monkeypatch, ui=True, scope="group", group=7)["draw_next"]
    assert "op=set slug=veille scope=group group=7 expected_version=1" in consigne
    assert "oto_procedure_app slug=veille" in consigne


def test_un_set_sur_un_slug_libre_est_une_naissance(monkeypatch):
    assert "draw_next" in _ecrire(monkeypatch, op="set")
    # Le palier omis est `user` : la consigne vise la ligne écrite, pas l'org.
    assert "scope=user" in _ecrire(monkeypatch, op="set")["draw_next"]


def test_pas_de_consigne_hors_naissance_hors_instance_ou_pour_le_runner(monkeypatch):
    assert "draw_next" not in _ecrire(monkeypatch, op="set", version=2)
    assert "draw_next" not in _ecrire(monkeypatch, op="set", from_version=3)
    assert "draw_next" not in _ecrire(monkeypatch, bundle=False)
    assert "draw_next" not in _ecrire(monkeypatch, worker=True)


def test_pas_de_consigne_quand_un_trace_est_deja_la(monkeypatch):
    assert "draw_next" not in _ecrire(monkeypatch, body=CORPS)
    tague = CORPS.replace("```\n┌", "```text\n┌", 1)
    assert "draw_next" not in _ecrire(monkeypatch, body=tague)
    a_nu = f"# Veille\n\n{TRACE}\n\n1. Chercher.\n"
    assert "draw_next" not in _ecrire(monkeypatch, body=a_nu)


def _naissance_banc(monkeypatch, ui=False):
    from oto_mcp import access, org_store, procedure_diagram, tool_alias
    monkeypatch.setattr(procedure_diagram, "bundle_du_graphe", lambda: BUNDLE)
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")
    monkeypatch.setattr(tool_alias, "sert_un_worker", lambda: False)
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: ui)
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(org_store, "org_front", lambda org_id: ("https://app.example.com/", None))


def test_la_vraie_reponse_d_une_creation_declenche_la_consigne(monkeypatch):
    """Sur la VRAIE réponse de l'écriture (seul le store est simulé) : la v1 née sans
    dessin porte la consigne, la v2 non. Le pivot `version == 1` est celui du store
    (`cur + 1 if cur else 1`)."""
    from oto_mcp.capabilities import procedure_console as pc
    from oto_mcp.capabilities.orgs import instructions as oi
    from test_procedure_form import _Ctx, _Inp
    _naissance_banc(monkeypatch)
    corps = "# Veille\n\n1. Chercher.\n"
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda *a, **k: {"slots": [], "body_md": corps})

    async def _wc(body_md, **k):
        return {"referenced_tools": [], "unresolved_tools": []}
    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    inp = pc.ProcedureInput(op="create", slug="ma-procedure", body_md=corps, scope="org")
    for version, attendu in ((1, True), (2, False)):
        monkeypatch.setattr(oi.org_store, "set_instruction", lambda *a, v=version, **k: v)
        ecrit = asyncio.run(oi._set_instruction(_Ctx(), _Inp(corps)))
        consigne = pc._a_dessiner_a_la_naissance(_Ctx(), inp, ecrit)
        assert (consigne is not None) is attendu, (version, ecrit)
    assert "op=set slug=ma-procedure scope=org expected_version=1" in (
        pc._a_dessiner_a_la_naissance(_Ctx(), inp, {**ecrit, "version": 1}))


def test_un_fork_sans_dessin_repart_avec_la_consigne(monkeypatch):
    """Le fork de la bibliothèque est une naissance : la consigne relit le corps forké,
    au palier que le fork a choisi, et fait porter le dessin par l'adaptation."""
    from oto_mcp.capabilities import guide_library as dl, procedure_console as pc
    from test_procedure_form import _Ctx
    _naissance_banc(monkeypatch)
    lu = {}

    def relire(otype, oid, slug, version=None):
        lu["owner"] = (otype, oid, slug)
        return {"body_md": lu.get("corps", "# Veille\n\n1. Chercher.\n")}
    monkeypatch.setattr(dl.org_store, "get_instruction", relire)
    monkeypatch.setattr(dl.org_store, "get_library_entry",
                        lambda **k: {"id": 1, "body_md": "# Veille\n\n1. Chercher.\n"})
    monkeypatch.setattr(dl.org_store, "fork_library_entry",
                        lambda **k: {"owner_type": k["owner_type"], "owner_id": k["owner_id"],
                                     "slug": "veille", "version": 1, "guide_id": 11,
                                     "forked_from": 1, "source_title": "t"})
    monkeypatch.setattr(dl.roles, "is_org_admin", lambda sub, org_id: True)

    class _F:
        slug = "veille"; new_slug = None; scope = None; project_id = None

    ecrit = dl._fork(_Ctx(), _F())
    inp = pc.ProcedureInput(op="fork", slug="veille")
    consigne = pc._a_dessiner_a_la_naissance(_Ctx(), inp, ecrit)
    assert lu["owner"] == ("org", 7, "veille")
    assert "forked" in consigne and "NEXT write" in consigne
    assert "op=get slug=veille scope=org" in consigne
    lu["corps"] = CORPS
    assert pc._a_dessiner_a_la_naissance(_Ctx(), inp, ecrit) is None


def test_la_consigne_ne_renvoie_qu_au_dessin_du_guide(monkeypatch):
    """Le guide servi en base porte encore des consignes retirées (digest) : la
    consigne n'en retient que la grammaire et l'exemple."""
    consigne = _ecrire(monkeypatch)["draw_next"]
    assert "only the drawing" in consigne
