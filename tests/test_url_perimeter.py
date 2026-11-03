"""Périmètre d'URL d'un projet (#605) — UN seam, lu par tous les outils concernés.

Un contrat client exclut la consultation de certaines pages ; la consigne le disait et
deux fiches sur cent consultaient quand même. Ce fichier tient les quatre bouts :

1. la GRAMMAIRE d'un motif (hôte + préfixe de chemin, normalisés ; un domaine entier
   n'est jamais implicite — un hôte nu est refusé à la pose, `hôte/*` l'écrit) ;
2. les deux EFFETS (écarter en sortie de recherche en le disant, refuser en entrée
   d'extraction en nommant la raison) et l'IDENTITÉ sans périmètre ;
3. la RÉSOLUTION du projet de l'appel (jeton `_project=`, endpoint publié, aucun) ;
4. un cliquet STRUCTUREL : le nom de l'option ne se relit pas hors du seam, et chaque
   module couvert appelle bien le seam — sans le pendant positif, l'interdit serait
   satisfait par un module qui ne garde rien (leçon de #557).
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, session_org, subdomain_project, url_perimeter as up

_PKG = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"

PER = up.Perimeter(project_id=12, project_name="Campagne",
                   prefixes=(up.parse_prefix("linkedin.com/in/"),))


# ── 1. grammaire ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,canon", [
    ("linkedin.com/in/", "linkedin.com/in/"),
    ("linkedin.com/in", "linkedin.com/in/"),
    ("https://www.LinkedIn.com/in/", "linkedin.com/in/"),
    ("http://linkedin.com:443/in", "linkedin.com/in/"),
    ("  linkedin.com/in/  ", "linkedin.com/in/"),
    ("exemple.com/a/B/c", "exemple.com/a/b/c/"),
    ("exemple.com/*", "exemple.com/*"),
    ("https://exemple.com/*", "exemple.com/*"),
])
def test_a_prefix_is_normalized_to_one_canonical_form(raw, canon):
    assert up.parse_prefix(raw).canonical == canon


@pytest.mark.parametrize("raw,code", [
    ("", "empty_prefix"),
    ("   ", "empty_prefix"),
    ("linkedin.com", "bare_host"),              # un hôte nu = tout le domaine, implicite
    ("linkedin.com/", "bare_host"),
    ("https://linkedin.com", "bare_host"),
    ("www.linkedin.com", "bare_host"),
    ("/in/", "bad_host"),                        # pas d'hôte
    ("localhost/x", "bad_host"),                 # un hôte a un point
    ("linkedin/in/", "bad_host"),
    ("ftp://exemple.com/x", "bad_scheme"),
    ("exemple.com/in/?x=1", "query_in_prefix"),
    ("exemple.com/in/#frag", "query_in_prefix"),
    ("exemple.com/in/*", "wildcard"),            # `/in/` couvre déjà tout ce qui est dessous
    ("exemple.com/*/x", "wildcard"),
    ("exemple .com/in/", "prefix_has_space"),
    ("user@exemple.com/in/", "bad_host"),
    ("exemple.com/" + "a" * 200, "prefix_too_long"),
])
def test_a_too_broad_or_malformed_prefix_is_refused_at_pose(raw, code):
    with pytest.raises(up.PerimeterError) as e:
        up.parse_prefix(raw)
    assert e.value.code == code


def test_bare_host_refusal_says_the_explicit_form():
    with pytest.raises(up.PerimeterError) as e:
        up.parse_prefix("exemple.com")
    assert "exemple.com/*" in e.value.message and "exemple.com/in/" in e.value.message


def test_normalize_dedups_and_bounds():
    assert up.normalize_prefixes(["linkedin.com/in/", "https://www.linkedin.com/in",
                                  "fr.linkedin.com/in/"]) == ["linkedin.com/in/",
                                                              "fr.linkedin.com/in/"]
    assert up.normalize_prefixes(None) == [] and up.normalize_prefixes([]) == []
    with pytest.raises(up.PerimeterError) as e:
        up.normalize_prefixes(["a.com/x/"] * (up.MAX_PREFIXES + 1))
    assert e.value.code == "too_many_prefixes"
    with pytest.raises(up.PerimeterError) as e:
        up.normalize_prefixes("linkedin.com/in/")
    assert e.value.code == "not_a_list"


def test_one_bad_prefix_refuses_the_whole_lot():
    """Rien n'est stocké d'un lot dont un élément est faux — une pose partielle serait
    une exclusion partielle que personne ne verrait."""
    with pytest.raises(up.PerimeterError) as e:
        up.normalize_prefixes(["linkedin.com/in/", "viadeo.com"])
    assert e.value.code == "bare_host"


# ── correspondance ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/jane-doe",
    "https://linkedin.com/in/jane-doe/",
    "http://fr.linkedin.com/in/jane-doe",          # sous-domaine = même hôte
    "https://LinkedIn.com/IN/Jane-Doe",             # casse
    "https://www.linkedin.com/in/jane-doe?trk=x#top",
    "linkedin.com/in/jane-doe",                     # sans schéma
    "https://www.linkedin.com/in/jane%2Ddoe",       # percent-encoding
])
def test_personal_profile_urls_match(url):
    assert PER.match(url) is not None


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/acme",        # la page ENTREPRISE reste rendue
    "https://www.linkedin.com/company/acme/people/",
    "https://www.linkedin.com/inbox/",              # `/in/` ≠ `/inbox` : segment, pas chaîne
    "https://www.linkedin.com/",
    "https://notlinkedin.com/in/x",                 # suffixe d'hôte sur une frontière de label
    "https://linkedin.com.evil.io/in/x",
    "https://acme.fr/in/x",
    "",
    "pas une url",
])
def test_company_pages_and_neighbours_do_not_match(url):
    assert PER.match(url) is None


def test_whole_host_is_only_the_explicit_star_form():
    whole = up.Perimeter(1, "p", (up.parse_prefix("viadeo.com/*"),))
    assert whole.match("https://www.viadeo.com/") is not None
    assert whole.match("https://fr.viadeo.com/profile/x") is not None
    assert whole.match("https://viadeo.fr/") is None


# ── 2. effets ─────────────────────────────────────────────────────────────────

_SERP = {
    "searchParameters": {"q": "jane doe"},
    "organic": [
        {"title": "Jane Doe — CTO", "link": "https://fr.linkedin.com/in/jane-doe", "position": 1},
        {"title": "ACME", "link": "https://www.linkedin.com/company/acme", "position": 2},
        {"title": "Jane sur son site", "link": "https://janedoe.fr/", "position": 3,
         "sitelinks": [{"title": "Profil", "link": "https://linkedin.com/in/jane-doe"}]},
    ],
    "peopleAlsoAsk": [{"question": "?", "link": "https://www.linkedin.com/in/john"}],
    "credits": 1,
}


def test_without_perimeter_the_payload_is_the_same_object():
    """L'identité, pas une copie égale : sans projet ou sans option, le seam ne touche
    à rien — c'est ce que le différentiel outil par outil vérifie ensuite."""
    assert up.filter_results(_SERP, None) is _SERP


def test_search_results_are_dropped_and_counted_never_in_silence():
    out = up.filter_results(_SERP, PER)
    assert [r["link"] for r in out["organic"]] == [
        "https://www.linkedin.com/company/acme", "https://janedoe.fr/"]
    # …à toute profondeur : le sitelink d'un résultat conservé et le PAA aussi.
    assert out["organic"][1]["sitelinks"] == []
    assert out["peopleAlsoAsk"] == []
    assert out["excluded_by_perimeter"] == {
        "count": 3, "project_id": 12, "project": "Campagne",
        "prefixes": {"linkedin.com/in/": 3}}
    # l'enveloppe et le payload d'origine sont intacts
    assert out["credits"] == 1 and len(_SERP["organic"]) == 3


def test_zero_excluded_still_says_the_perimeter_is_in_force():
    out = up.filter_results({"organic": [{"link": "https://acme.fr/"}]}, PER)
    assert out["excluded_by_perimeter"] == {
        "count": 0, "project_id": 12, "project": "Campagne",
        "prefixes": {"linkedin.com/in/": 0}}


def test_nested_pages_and_url_strings_are_filtered_too():
    # firecrawl_search : `data.web[]` ; firecrawl_crawl_status : `data[].metadata.sourceURL` ;
    # tavily_map : `results[]` = chaînes URL.
    fc = {"data": {"web": [{"url": "https://www.linkedin.com/in/x"}, {"url": "https://a.fr/"}]}}
    assert up.filter_results(fc, PER)["data"]["web"] == [{"url": "https://a.fr/"}]
    st = {"data": [{"markdown": "…", "metadata": {"sourceURL": "https://linkedin.com/in/y"}},
                   {"markdown": "…", "metadata": {"sourceURL": "https://a.fr/p"}}]}
    assert len(up.filter_results(st, PER)["data"]) == 1
    tm = {"results": ["https://linkedin.com/in/z", "https://a.fr/", "pas-une-url"]}
    assert up.filter_results(tm, PER)["results"] == ["https://a.fr/", "pas-une-url"]


def test_extraction_url_is_refused_naming_prefix_and_project():
    with pytest.raises(McpError) as e:
        up.refuse_if_excluded("https://www.linkedin.com/in/jane-doe", PER)
    msg = str(e.value)
    assert "linkedin.com/in/" in msg and "Campagne" in msg and "#12" in msg
    assert up.OPTION in msg
    # la page entreprise passe, et sans périmètre rien ne lève
    up.refuse_if_excluded("https://www.linkedin.com/company/acme", PER)
    up.refuse_if_excluded("https://www.linkedin.com/in/jane-doe", None)


def test_a_batch_with_one_excluded_url_is_refused_whole_and_named():
    with pytest.raises(McpError) as e:
        up.refuse_if_any_excluded(["https://a.fr/", "https://linkedin.com/in/a",
                                   "https://linkedin.com/in/b"], PER)
    msg = str(e.value)
    assert "2 URL refusées" in msg and "/in/a" in msg and "/in/b" in msg
    up.refuse_if_any_excluded(["https://a.fr/", "https://b.fr/"], PER)
    up.refuse_if_any_excluded(["https://linkedin.com/in/a"], None)


# ── 3. résolution ─────────────────────────────────────────────────────────────

ROW = {"id": 12, "name": "Campagne", "excluded_url_prefixes": ["linkedin.com/in/"]}


def test_perimeter_of_project_is_none_without_option():
    assert up.perimeter_of_project(None) is None
    assert up.perimeter_of_project({"id": 1, "name": "x"}) is None
    assert up.perimeter_of_project({"id": 1, "name": "x", "excluded_url_prefixes": []}) is None
    per = up.perimeter_of_project(ROW)
    assert per.project_id == 12 and [p.canonical for p in per.prefixes] == ["linkedin.com/in/"]


def test_call_perimeter_comes_from_the_project_token(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: dict(ROW, id=pid))
    tok = session_org.set_call_project(12)
    try:
        per = up.perimeter_of_call()
    finally:
        session_org.reset_call_project(tok)
    assert per is not None and per.project_id == 12


def test_call_perimeter_comes_from_the_published_endpoint(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: dict(ROW, id=pid))
    ctx = subdomain_project.AnonContext(project_id=44, org_id=2, tools=frozenset())
    tok = subdomain_project._CTX.set(ctx)
    try:
        per = up.perimeter_of_call()
    finally:
        subdomain_project._CTX.reset(tok)
    assert per is not None and per.project_id == 44


def test_no_project_means_no_perimeter_and_no_db_read(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "get_project_by_id",
                        lambda pid: (_ for _ in ()).throw(AssertionError("DB lue sans projet")))
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    assert access.current_project() is None
    assert up.perimeter_of_call() is None


# ── 4. cliquet structurel ─────────────────────────────────────────────────────

# Une entrée ici est une DÉCISION, avec sa raison.
_ALLOWED = {
    "url_perimeter.py": "le seam — la seule définition de la règle",
    "db/projects.py": "persistance : la colonne relue et écrite",
    "db/_init.py": "migration : l'ALTER qui pose la colonne",
    "capabilities/projects.py": "contrat d'entrée d'`oto_project` et vue `_view`",
    "tools/effets.py": "déclaration d'effet : nomme les listes d'`oto_project` qui ne sont "
                       "pas des lots (`PAS_UN_LOT`), sans juger aucune URL",
}

# Les modules COUVERTS : chacun appelle le seam. Un outil de recherche filtre sa sortie
# (`filter_results`), un outil d'extraction refuse son entrée (`refuse_*`). Un module qui
# fait les deux appelle les deux. La liste est celle du tableau de `docs/projects.md`.
_COVERED = {
    "tools/serper.py": {"filter_results", "refuse_if_excluded"},
    "tools/serpapi.py": {"filter_results"},
    "tools/searchapi.py": {"filter_results"},
    "tools/tavily.py": {"filter_results", "refuse_if_excluded", "refuse_if_any_excluded"},
    "tools/firecrawl.py": {"filter_results", "refuse_if_excluded", "refuse_if_any_excluded"},
    "tools/cloro.py": {"filter_results"},
    "tools/web.py": {"refuse_if_excluded"},
    "tools/browser.py": {"refuse_if_excluded"},
    "file_source.py": {"refuse_if_excluded"},
}


def _string_constants(tree: ast.AST) -> set[str]:
    bare = {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in bare}


def test_only_the_seam_and_its_persistence_name_the_option():
    offenders = {}
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        if rel in _ALLOWED:
            continue
        hits = {s for s in _string_constants(ast.parse(path.read_text(encoding="utf-8")))
                if up.OPTION in s}
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        f"ces modules nomment l'option `{up.OPTION}` hors du seam : {offenders}. La règle "
        f"« cette URL est-elle exclue ? » a UNE définition (`oto_mcp/url_perimeter.py`) ; "
        f"la relire ailleurs, c'est deux gardes qui divergeront. Accès légitime "
        f"(persistance, contrat d'entrée) → `_ALLOWED`, avec sa raison.")


def _calls_on_seam(rel: str) -> set[str]:
    tree = ast.parse((_PKG / rel).read_text(encoding="utf-8"))
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "url_perimeter"}


@pytest.mark.parametrize("rel,expected", sorted(_COVERED.items()))
def test_every_covered_module_calls_the_seam(rel, expected):
    called = _calls_on_seam(rel)
    assert expected <= called, (
        f"`{rel}` n'appelle pas {sorted(expected - called)} sur `url_perimeter` — un outil "
        f"couvert qui ne consulte pas le périmètre est un outil qui consulte quand même.")


def test_the_option_name_is_a_single_constant():
    assert up.OPTION == "excluded_url_prefixes"
    # et le bloc de réponse est sérialisable tel quel (aucun objet du seam ne fuit)
    json.dumps(up.filter_results(_SERP, PER))
