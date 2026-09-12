"""UI web navigable d'un projet partagé (`share_ui.py`, ADR 0032) — routeur `build_page`
(gating fail-closed par appartenance au projet), rendus, dérivation de colonnes."""
from oto_mcp import db, org_store, share_ui

# `secret` = partage navigable, ET les deux opt-ins posés : c'est le projet dont le
# propriétaire a consenti à publier ses tableaux ET ses pages. Les flags sont dans la
# fixture parce qu'ils sont la CONDITION de ce que la page montre — la face web les lit
# dans `project_exposure`, exactement comme la face MCP (#557).
_PROJECT = {"id": 5, "name": "Projet démo", "brief_md": "Un projet de démonstration.",
            "mcp_access": "secret", "mcp_expose_datastore": True, "mcp_expose_docs": True}

_LINKS = [
    {"target_type": "procedure", "target_ref": "11", "label": "Enrichir", "title": "Enrichissement"},
    {"target_type": "tableau", "target_ref": "22", "label": "Prospects", "datastore": "prospects"},
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
    assert "https://x.share.oto.cx/mcp" in html               # carte brancher
    assert "serper" not in html                               # connecteur non navigable


def test_index_shows_connectors_with_tooltip_and_link(monkeypatch):
    # Les tools exposés sont groupés par CONNECTEUR : pastille (logo/monogramme) +
    # tooltip (description) + lien vers la fiche marketplace du dashboard.
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "P", "brief_md": "", "mcp_tools": ["fr_search", "serper_search"]}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "Connecteurs" in html
    # serper_search → connecteur `serper` ; fr_search → connecteur `sirene`.
    assert "connector=serper" in html
    from oto_mcp import share_ui as _su
    assert f"{_su._DASHBOARD}/connectors?tab=marketplace" in html
    assert 'class=conn' in html and 'data-tip=' in html  # pastille + tooltip


def test_connectors_from_tools_groups_and_derives():
    conns, loose = share_ui._connectors_from_tools(["serper_search", "serper_scrape",
                                                    "fr_search"])
    names = {c["name"] for c in conns}
    assert "serper" in names
    serper = next(c for c in conns if c["name"] == "serper")
    assert serper["tool_count"] == 2            # deux tools serper regroupés
    assert serper["href"].endswith("connector=serper")
    assert "connectors?tab=marketplace" in serper["href"]


def test_no_add_to_oto_cta_even_with_a_slug(monkeypatch):
    """Le bouton « Ajouter à mon Oto » menait à l'écran `/import` du dashboard, retiré
    (oto#192) : il retombait sur l'accueil. Retiré à son tour ; le hero reste."""
    _wire(monkeypatch, links=[])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    proj = {"id": 5, "name": "P", "brief_md": "", "mcp_access": "secret", "mcp_slug": "demo-x"}
    html, status = share_ui.build_page(proj, "/", connect_url="https://demo-x.share.oto.cx/mcp")
    assert status == 200 and "https://demo-x.share.oto.cx/mcp" in html
    assert "Ajouter à mon Oto" not in html and "/import" not in html


def test_index_hides_tables_and_pages_when_anonymous(monkeypatch):
    """`anonymous` = endpoint-outil LISTÉ dans l'annuaire public : ni tableaux, ni pages.

    Ce test disait l'inverse pour les pages (« procédures et docs restent ») et gravait
    ainsi la divergence de #557 : la face MCP refusait les pages hors `secret` + opt-in,
    la face web les rendait à quiconque ouvrait l'URL trouvée dans l'annuaire. Les
    procédures, elles, restent : elles sont LIÉES au projet par un acte explicite du
    propriétaire, alors qu'une page appartient à l'arbre du projet sans qu'il ait rien
    fait pour la publier.
    """
    _wire(monkeypatch)
    proj = {**_PROJECT, "mcp_access": "anonymous"}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "/data/22" not in html and "Prospects" not in html
    assert "/docs/44" not in html and "Notes internes" not in html
    assert "/procedures/11" in html   # les procédures liées restent


def test_index_hides_pages_without_the_optin(monkeypatch):
    """Partage `secret` SANS `mcp_expose_docs` : les pages ne sont ni listées, ni LUES.

    Le titre et le chapô d'une page en disent déjà trop (« Notes — négo Dupont »), donc
    la garde tombe avant l'I/O, pas au moment du rendu."""
    _wire(monkeypatch)
    monkeypatch.setattr(db, "list_docs_for_project",
                        lambda pid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_expose_docs": False}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "/docs/44" not in html and "Notes internes" not in html
    assert "/procedures/11" in html and "/data/22" in html   # le reste est intact


def test_index_hides_tables_without_the_datastore_optin(monkeypatch):
    """`secret` seul ne suffit plus non plus pour les tableaux : `mcp_expose_datastore`
    est l'opt-in que la face MCP exige déjà pour les tools `data_*` (constat annexe #557)."""
    _wire(monkeypatch)
    proj = {**_PROJECT, "mcp_expose_datastore": False}
    html, _ = share_ui.build_page(proj, "/", connect_url="u")
    assert "/data/22" not in html and "Prospects" not in html
    assert "/procedures/11" in html and "/docs/44" in html


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
    # Le titre affiché est le `label` DU LIEN (« Enrichir »), pas celui du guide :
    # c'est le nom que le projet lui donne (oto-dashboard#119).
    assert status == 200 and "Enrichir" in html and "Étapes" in html


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
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: {"datastore": "prospects", "schema": None})
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
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_access": "anonymous"}
    html, status = share_ui.build_page(proj, "/data/22", connect_url="u")
    assert status == 404


def test_data_denied_without_the_datastore_optin(monkeypatch):
    """Même refus quand l'opt-in `mcp_expose_datastore` n'est pas posé : 404, sans lecture."""
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_expose_datastore": False}
    _, status = share_ui.build_page(proj, "/data/22", connect_url="u")
    assert status == 404


def test_data_not_linked_is_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("hors allowlist")))
    html, status = share_ui.build_page(_PROJECT, "/data/99", connect_url="u")
    assert status == 404


# Lien tableau par NOM (legacy, avant normalisation nom→id) : la page web doit le
# résoudre contre le datastore de l'org propriétaire — sinon il était jeté par `isdigit()`
# et le datastore n'apparaissait pas (régression vécue projet Marché #8).
_PROJECT_OWNED = {**_PROJECT, "owner_type": "org", "owner_id": "81"}
_LINKS_BY_NAME = [
    {"target_type": "tableau", "target_ref": "accords_dormants", "label": "Vivier national"},
]


def test_index_lists_tableau_linked_by_name(monkeypatch):
    _wire(monkeypatch, links=_LINKS_BY_NAME)
    monkeypatch.setattr(db, "get_datastore",
                        lambda ot, oid, name: {"id": 65} if name == "accords_dormants" else None)
    html, _ = share_ui.build_page(_PROJECT_OWNED, "/", connect_url="u")
    assert "Vivier national" in html and "/data/65" in html


def test_data_allowed_via_name_link(monkeypatch):
    _wire(monkeypatch, links=_LINKS_BY_NAME)
    monkeypatch.setattr(db, "get_datastore",
                        lambda ot, oid, name: {"id": 65} if name == "accords_dormants" else None)
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: {"datastore": "accords_dormants", "schema": None})
    monkeypatch.setattr(db, "datastore_count_rows", lambda rid: 1)
    monkeypatch.setattr(db, "datastore_list_rows", lambda rid, **kw: [{"data": {"siren": "123"}}])
    html, status = share_ui.build_page(_PROJECT_OWNED, "/data/65", connect_url="u")
    assert status == 200 and "accords_dormants" in html and "siren" in html


# ── Doc ──────────────────────────────────────────────────────────────────────
def test_doc_allowed_via_project_ownership(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: {"title": "Notes internes", "body_md": "Contenu", "project_id": 5})
    html, status = share_ui.build_page(_PROJECT, "/docs/44", connect_url="u")
    assert status == 200 and "Notes internes" in html and "Contenu" in html


def test_doc_foreign_unlinked_is_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: {"title": "Secret", "body_md": "s", "project_id": 999})
    html, status = share_ui.build_page(_PROJECT, "/docs/77", connect_url="u")
    assert status == 404 and "Secret" not in html


def test_doc_denied_without_the_optin(monkeypatch):
    """Une page d'un projet `secret` SANS `mcp_expose_docs` : 404, et son corps n'est
    même pas lu. C'est le cœur de #557 — l'URL était devinable (`/docs/<id>` séquentiel)
    et rendait `body_md` en entier."""
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_expose_docs": False}
    html, status = share_ui.build_page(proj, "/docs/44", connect_url="u")
    assert status == 404 and "Notes internes" not in html


def test_doc_denied_when_anonymous(monkeypatch):
    """Un endpoint `anonymous` est ANNONCÉ par l'annuaire public : ses pages ne sont
    jamais servies, opt-in ou pas (l'opt-in lui-même est réservé à `secret`)."""
    _wire(monkeypatch)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda rid: (_ for _ in ()).throw(AssertionError("ne doit pas lire")))
    proj = {**_PROJECT, "mcp_access": "anonymous"}
    _, status = share_ui.build_page(proj, "/docs/44", connect_url="u")
    assert status == 404


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


# ── Le rôle d'une entité liée (oto-dashboard#119) ─────────────────────────────
# Un lien de projet porte un `role` — « pourquoi cette entité est là » (ADR 0032 §2) —
# saisi par le propriétaire et jamais rendu ici. Sous la rubrique « Procédures », le
# lecteur d'un partage ne voyait donc qu'un titre : ni ce que la procédure fait, ni
# pourquoi elle est là. C'est la pièce la plus importante d'un projet pour comprendre le
# chantier, et il n'a aucun autre moyen de le savoir — il n'a pas accès au projet.

_LINKS_WITH_ROLE = [
    {"target_type": "procedure", "target_ref": "11", "label": "Enrichir",
     "title": "Enrichissement", "role": "Ce que chaque agent worker exécute, une ligne à la fois."},
    {"target_type": "tableau", "target_ref": "22", "label": "Prospects",
     "datastore": "prospects", "role": "Périmètre sourcé par convention collective."},
]


def test_index_shows_the_role_of_each_linked_entity(monkeypatch):
    _wire(monkeypatch, links=_LINKS_WITH_ROLE)
    html, _ = share_ui.build_page(_PROJECT, "/", connect_url="https://x/mcp")
    assert "Ce que chaque agent worker exécute" in html
    assert "Périmètre sourcé par convention collective." in html


def test_the_label_still_wins_over_the_procedure_title(monkeypatch):
    """Le `label` du lien est le nom choisi POUR CE PROJET ; le titre du guide
    n'est qu'un repli quand le lien n'en porte pas."""
    _wire(monkeypatch, links=_LINKS_WITH_ROLE)
    html, _ = share_ui.build_page(_PROJECT, "/", connect_url="https://x/mcp")
    assert "Enrichir" in html


def test_a_link_without_role_renders_no_empty_line(monkeypatch):
    _wire(monkeypatch, links=[{"target_type": "procedure", "target_ref": "11",
                               "label": "Enrichir", "role": None}])
    html, _ = share_ui.build_page(_PROJECT, "/", connect_url="https://x/mcp")
    assert "class=r" not in html


def test_the_role_is_escaped(monkeypatch):
    """Le `role` est saisi par un humain et rendu sur une page PUBLIQUE."""
    _wire(monkeypatch, links=[{"target_type": "procedure", "target_ref": "11",
                               "label": "P", "role": "<script>alert(1)</script>"}])
    html, _ = share_ui.build_page(_PROJECT, "/", connect_url="https://x/mcp")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_a_page_uses_its_chapo_as_role(monkeypatch):
    """Une page n'est pas LIÉE (elle appartient au projet) donc n'a pas de `role` — son
    chapô dit la même chose : ce qu'on va lire."""
    monkeypatch.setattr(db, "list_project_links", lambda pid: [])
    monkeypatch.setattr(db, "list_docs_for_project",
                        lambda pid: [{"id": 44, "title": "Veille", "description": "Signaux de rachat"}])
    html, _ = share_ui.build_page(_PROJECT, "/", connect_url="https://x/mcp")
    assert "Signaux de rachat" in html


def test_procedure_page_shows_the_project_label_and_role(monkeypatch):
    """La page d'une procédure affichait le `title` de la procédure et rien d'autre : un
    déroulé opératoire livré sans dire ce qu'on vient y chercher. Le nom qui compte est
    celui donné DANS ce projet (`label`), et le `role` dit pourquoi elle est là."""
    _wire(monkeypatch, links=_LINKS_WITH_ROLE)
    monkeypatch.setattr(org_store, "get_instruction_by_id",
                        lambda i: {"title": "Titre canonique du guide",
                                   "body_md": "# Déroulé"})
    html, status = share_ui.build_page(_PROJECT, "/procedures/11", connect_url="https://x/mcp")
    assert status == 200
    assert "Enrichir" in html                                   # le label du lien
    assert "Ce que chaque agent worker exécute" in html         # le role
    assert "Titre canonique du guide" not in html         # le title n'est qu'un repli


def test_procedure_page_falls_back_to_the_guide_title(monkeypatch):
    """Un lien posé sans label (l'agent lie souvent ainsi) doit rester lisible."""
    _wire(monkeypatch, links=[{"target_type": "procedure", "target_ref": "11"}])
    monkeypatch.setattr(org_store, "get_instruction_by_id",
                        lambda i: {"title": "Titre canonique", "body_md": "x"})
    html, _ = share_ui.build_page(_PROJECT, "/procedures/11", connect_url="https://x/mcp")
    assert "Titre canonique" in html


def test_an_unlinked_procedure_is_still_404(monkeypatch):
    """Le gate d'appartenance ne doit pas s'être perdu dans la reprise du lien."""
    _wire(monkeypatch, links=_LINKS_WITH_ROLE)
    monkeypatch.setattr(org_store, "get_instruction_by_id",
                        lambda i: {"title": "Secrète", "body_md": "x"})
    _, status = share_ui.build_page(_PROJECT, "/procedures/999", connect_url="https://x/mcp")
    assert status == 404
