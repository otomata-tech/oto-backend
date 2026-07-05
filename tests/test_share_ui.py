"""UI web navigable d'un projet partagé (`share_ui.py`, ADR 0032) — routeur `build_page`
(gating fail-closed par appartenance au projet), rendus, dérivation de colonnes."""
from oto_mcp import db, org_store, share_ui

# `secret` = partage navigable → les tableaux liés sont montrés (lecture seule).
_PROJECT = {"id": 5, "name": "Projet démo", "brief_md": "Un projet de démonstration.",
            "mcp_access": "secret"}

_LINKS = [
    {"target_type": "procedure", "target_ref": "11", "label": "Enrichir", "title": "Enrichissement"},
    {"target_type": "tableau", "target_ref": "22", "label": "Prospects", "namespace": "prospects"},
    {"target_type": "doc", "target_ref": "33", "label": "Guide"},
    {"target_type": "connecteur", "target_ref": "serper"},  # ignoré (pas navigable)
]


def _wire(monkeypatch, *, links=None):
    monkeypatch.setattr(db, "list_project_links", lambda pid: list(links if links is not None else _LINKS))
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [{"id": 44, "title": "Notes internes"}])


# ── Index ─────────────────────────────────────────────────────────────────────
def test_index_lists_entities(monkeypatch):
    _wire(monkeypatch)
    html, status = share_ui.build_page(_PROJECT, "/", connect_url="https://x.share.oto.cx/mcp")
    assert status == 200
    assert "Enrichir" in html and "/procedures/11" in html
    assert "Prospects" in html and "/data/22" in html
    assert "Notes internes" in html and "/docs/44" in html   # doc de l'arbre projet
    assert "Guide" in html and "/docs/33" in html            # doc lié
    assert "https://x.share.oto.cx/mcp" in html               # carte brancher
    assert "serper" not in html                               # connecteur non navigable


def test_index_shows_connectors_with_tooltip_and_link(monkeypatch):
    # Les tools exposés sont groupés par CONNECTEUR : pastille (logo/monogramme) +
    # tooltip (description) + lien vers la fiche marketplace du dashboard.
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "P", "brief_md": "", "mcp_tools": ["fr_search", "serper_web_search"]}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "Connecteurs" in html
    # serper_web_search → connecteur `serper` ; fr_search → connecteur `sirene`.
    assert "connector=serper" in html
    assert "dashboard.oto.ninja/connectors?tab=marketplace" in html
    assert 'class=conn' in html and 'data-tip=' in html  # pastille + tooltip


def test_connectors_from_tools_groups_and_derives():
    conns, loose = share_ui._connectors_from_tools(["serper_web_search", "serper_news_search",
                                                    "fr_search"])
    names = {c["name"] for c in conns}
    assert "serper" in names
    serper = next(c for c in conns if c["name"] == "serper")
    assert serper["tool_count"] == 2            # deux tools serper regroupés
    assert serper["href"].endswith("connector=serper")
    assert "connectors?tab=marketplace" in serper["href"]


def test_add_to_oto_cta_when_slug_present(monkeypatch):
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "P", "brief_md": "", "mcp_access": "secret", "mcp_slug": "demo-x"}
    html, _ = share_ui.build_page(proj, "/", connect_url="https://demo-x.share.oto.cx/mcp")
    assert "Ajouter à mon Oto" in html
    assert "dashboard.oto.ninja/import?slug=demo-x" in html


def test_index_hides_tables_when_anonymous(monkeypatch):
    # `anonymous` (endpoint-outil listé) ne montre PAS les tableaux du datastore.
    _wire(monkeypatch)
    proj = {**_PROJECT, "mcp_access": "anonymous"}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "/data/22" not in html and "Prospects" not in html
    assert "/procedures/11" in html   # procédures et docs restent


def test_index_escapes_name(monkeypatch):
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "<script>alert(1)</script>", "brief_md": ""}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_index_renders_brief_markdown(monkeypatch):
    # Le brief est rendu en Markdown (titres/gras), pas affiché en brut.
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "P", "brief_md": "## Objet\n\nUn **vivier** de leads.",
            "mcp_access": "secret"}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "<h2>Objet</h2>" in html and "<strong>vivier</strong>" in html
    assert "## Objet" not in html   # plus de markdown brut


# ── Procédure ───────────────────────────────────────────────────────────────
def test_procedure_allowed(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(org_store, "get_instruction_by_id",
                        lambda rid: {"title": "Enrichissement", "body_md": "# Étapes\n\n1. Chercher"})
    html, status = share_ui.build_page(_PROJECT, "/procedures/11", connect_url="u")
    assert status == 200 and "Enrichissement" in html and "Étapes" in html


def test_procedure_not_linked_is_404(monkeypatch):
    _wire(monkeypatch)
    # get_instruction_by_id ne doit PAS être appelé pour un id hors périmètre (fail-closed).
    monkeypatch.setattr(org_store, "get_instruction_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("hors allowlist")))
    html, status = share_ui.build_page(_PROJECT, "/procedures/99", connect_url="u")
    assert status == 404 and "Introuvable" in html


# ── Datastore ────────────────────────────────────────────────────────────────
def test_data_allowed_on_secret(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_datastore_namespace_by_id",
                        lambda rid: {"namespace": "prospects", "schema": None})
    monkeypatch.setattr(db, "datastore_count_rows", lambda rid: 2)
    monkeypatch.setattr(db, "datastore_list_rows",
                        lambda rid, **kw: [{"data": {"nom": "Alice", "email": "a@x.fr"}},
                                           {"data": {"nom": "Bob"}}])
    html, status = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert status == 200
    assert "prospects" in html and "nom" in html and "email" in html and "Alice" in html
    assert "1–2 sur 2" in html


def test_data_denied_when_anonymous(monkeypatch):
    # `anonymous` ne sert pas les lignes du datastore (même si le tableau est lié).
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_datastore_namespace_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_access": "anonymous"}
    html, status = share_ui.build_page(proj, "/data/22", connect_url="u")
    assert status == 404


def test_data_not_linked_is_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_datastore_namespace_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("hors allowlist")))
    html, status = share_ui.build_page(_PROJECT, "/data/99", connect_url="u")
    assert status == 404


# ── Doc ──────────────────────────────────────────────────────────────────────
def test_doc_allowed_via_project_ownership(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: {"title": "Notes internes", "body_md": "Contenu", "project_id": 5})
    html, status = share_ui.build_page(_PROJECT, "/docs/44", connect_url="u")
    assert status == 200 and "Notes internes" in html and "Contenu" in html


def test_doc_allowed_via_link(monkeypatch):
    _wire(monkeypatch)
    # Doc d'un AUTRE projet mais explicitement lié → autorisé.
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: {"title": "Guide", "body_md": "g", "project_id": 999})
    html, status = share_ui.build_page(_PROJECT, "/docs/33", connect_url="u")
    assert status == 200 and "Guide" in html


def test_doc_foreign_unlinked_is_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: {"title": "Secret", "body_md": "s", "project_id": 999})
    html, status = share_ui.build_page(_PROJECT, "/docs/77", connect_url="u")
    assert status == 404 and "Secret" not in html


# ── Routes non-UI (retombe sur le MCP) ───────────────────────────────────────
def test_non_ui_paths_fall_through(monkeypatch):
    _wire(monkeypatch)
    assert share_ui.build_page(_PROJECT, "/mcp", connect_url="u") == (None, 0)
    assert share_ui.build_page(_PROJECT, "/.well-known/oauth-authorization-server", connect_url="u") == (None, 0)
    assert share_ui.build_page(_PROJECT, "/procedures/not-an-id", connect_url="u") == (None, 0)


# ── Dérivation de colonnes / cellules ────────────────────────────────────────
def test_derive_columns_from_schema():
    schema = {"fields": [{"name": "nom"}, {"name": "ville"}]}
    assert share_ui._derive_columns(schema, [{"data": {"nom": "A", "autre": 1}}]) == ["nom", "ville"]


def test_derive_columns_union_of_rows():
    rows = [{"data": {"a": 1, "b": 2}}, {"data": {"b": 3, "c": 4}}]
    assert share_ui._derive_columns(None, rows) == ["a", "b", "c"]


def test_cell_rendering():
    # `_cell` = valeur TEXTE (title de survol + recherche/tri DOM) — inchangé.
    assert share_ui._cell(None) == ""
    assert share_ui._cell("x") == "x"
    assert share_ui._cell(42) == "42"
    assert share_ui._cell({"k": "v"}) == '{"k": "v"}'


def test_cell_html_renders_json_as_key_value():
    # Un dict/list d'objets est rendu en clé/valeur lisible, PAS en JSON brut échappé.
    html_dict = share_ui._cell_html({"nom": "Régis", "email": "r@x.fr"})
    assert "class=kv" in html_dict
    assert "nom" in html_dict and "Régis" in html_dict and "r@x.fr" in html_dict
    assert "{" not in html_dict and '"' not in html_dict   # plus de soupe JSON

    html_list = share_ui._cell_html([{"nom": "Régis"}, {"nom": "Bob"}])
    assert "class=jlist" in html_list and html_list.count("class=jitem") == 2


def test_cell_html_scalars_and_urls():
    assert share_ui._cell_html(None) == "" and share_ui._cell_html("") == ""
    assert share_ui._cell_html("hello") == "hello"
    link = share_ui._cell_html("https://example.com/a")
    assert link.startswith("<a href=") and 'rel="noopener nofollow"' in link
    # liste de scalaires → puces
    chips = share_ui._cell_html(["a", "b"])
    assert "class=chips" in chips and chips.count("class=chip>") == 2


def test_cell_html_escapes_values():
    out = share_ui._cell_html({"x": "<script>alert(1)</script>"})
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_cell_td_wraps_and_marks_rich():
    # Cellule structurée : classe `rich` (colonne large) + wrapper `.cell` borné + title complet.
    td = share_ui._cell_td({"nom": "Alice"})
    assert '<td class="rich">' in td and 'class="cell rich-cell"' in td
    assert "title=" in td
    # Scalaire court : td nu, wrapper `.cell` simple, pas de title.
    td2 = share_ui._cell_td("court")
    assert td2.startswith("<td>") and 'class="cell"' in td2 and "title=" not in td2
