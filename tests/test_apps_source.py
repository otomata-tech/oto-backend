"""`apps_source` — lire le HTML d'une app par morceaux : plan, recherche, lignes.

Logique pure, sans base : le texte rendu doit être EXACT (un agent en tire un `find`),
les fins du plan suivent les accolades et non le retrait, tout est borné.
"""
import pytest

from oto_mcp.capabilities import apps_source as S


def _plan(html: str) -> list[str]:
    return [l.strip() for l in S.lire(html)["outline"].split("\n")]


APP = "\n".join([
    "<style>",                                   # 1
    "/* main sheet */",                          # 2
    ".a{color:red}",                             # 3
    "</style>",                                  # 4
    '<main id="screen"></main>',                 # 5
    "<script>",                                  # 6
    "function h(t){return document.createElement(t);}",   # 7
    "function screen(){",                        # 8
    "var s = '}}}'; var t = `a ${ {x:1}.x } }`;",  # 9 : accolades en chaîne et gabarit
    "// a } in a comment",                       # 10
    "var r = /[{}]+/g;",                         # 11 : et dans une regex
    "function inner(){",                         # 12
    "return 1;",                                 # 13
    "}",                                         # 14
    "return h('div');",                          # 15
    "}",                                         # 16
    "// ── Modals ──",                           # 17
    "const open = (x) => {",                     # 18
    "  go(x);",                                  # 19
    "};",                                        # 20
    "</script>",                                 # 21
])


def test_le_plan_suit_les_accolades_pas_le_retrait():
    plan = _plan(APP)
    assert any(l.startswith("1-4 style main sheet") for l in plan)
    assert any(l.startswith("5 element main#screen") for l in plan)
    assert any(l.startswith("6-21 script") for l in plan)
    assert any(l.startswith("7 function h ") for l in plan)
    # Ni la chaîne, ni le gabarit, ni le commentaire, ni la regex ne ferment `screen`.
    assert any(l.startswith("8-16 function screen") for l in plan)
    assert any(l.startswith("12-14 function inner") for l in plan)
    assert any(l.startswith("17-20 section Modals") for l in plan)
    assert any(l.startswith("18-20 function open") for l in plan)


def test_le_plan_indente_par_niveau_et_donne_la_taille():
    rendu = S.lire(APP)["outline"].split("\n")
    inner = next(l for l in rendu if "function inner" in l)
    screen = next(l for l in rendu if "function screen" in l)
    assert len(inner) - len(inner.lstrip()) > len(screen) - len(screen.lstrip())
    assert screen.endswith(" ch)")


def test_le_plan_ne_garde_que_les_grandes_declarations():
    courte = "const N = 3;"
    longue = "const CSS = `\n" + "\n".join(f".c{i}{{}}" for i in range(30)) + "\n`;"
    plan = _plan(f"<script>\n{courte}\n{longue}\n</script>")
    assert not any("declaration N" in l for l in plan)
    assert any("declaration CSS" in l and l.startswith("3-34") for l in plan)


def test_au_dela_du_plafond_tombent_les_plus_petites_jamais_la_fin(monkeypatch):
    monkeypatch.setattr(S, "MAX_PLAN", 5)
    corps = [f"function f{i}(){{return {i};}}" for i in range(10)]
    corps.append("function grosse(){\n" + "x();\n" * 50 + "}")
    res = S.lire("<script>\n" + "\n".join(corps) + "\n</script>")
    # Le script et onze fonctions : douze entrées, cinq gardées.
    assert res["omitted"] == 7 and len(res["outline"].split("\n")) == 5
    assert "function grosse" in res["outline"]
    assert "script" in res["outline"]


def test_lignes_numerotees_exactes():
    res = S.lire("a\nb\nc\nd", lines=[2, 3])
    assert res["text"] == "     2\tb\n     3\tc"
    assert (res["from"], res["to"], res["lines"]) == (2, 3, 4)
    assert "next" not in res
    # La dernière au-delà du texte : bornée.
    assert S.lire("a\nb", lines=[2, 99])["to"] == 2


def test_lignes_bornees_et_suite(monkeypatch):
    monkeypatch.setattr(S, "MAX_TEXTE", 30)
    res = S.lire("\n".join(f"line {i}" for i in range(100)), lines=[1, 100])
    assert res["to"] < 100 and res["next"] == res["to"] + 1
    assert len(res["text"]) <= 30


def test_une_ligne_trop_longue_est_coupee_en_le_disant():
    res = S.lire("x" * (S.MAX_LIGNE + 50), lines=[1])
    assert "+50 characters on this line" in res["text"]


@pytest.mark.parametrize("lines", [[0, 2], [3, 2], [1, 2, 3], ["1"], [True], [9]])
def test_lignes_refusees(lines):
    with pytest.raises(S.SourceRefusee):
        S.lire("a\nb", lines=lines)


def test_recherche_sans_casse_et_compte_exact():
    html = "Alpha\nbeta\nalpha beta\nx\ny\nz\nALPHA"
    res = S.lire(html, search="alpha", context=0)
    assert res["matches"] == 3 and res["exact"] == 1
    assert [h["from"] for h in res["hunks"]] == [1, 3, 7]
    assert res["hunks"][1]["text"] == "     3\talpha beta"


def test_recherche_fusionne_les_contextes():
    html = "\n".join(str(i) for i in range(1, 21)).replace("5", "hit5").replace("7", "hit7")
    res = S.lire(html, search="hit", context=2)
    # « 5 », « 7 », « 15 », « 17 » : deux fenêtres, chacune fusionnée.
    assert [(h["from"], h["to"]) for h in res["hunks"]] == [(3, 9), (13, 19)]


def test_recherche_dans_une_longue_ligne_rend_un_texte_exact():
    ligne = "a" * 10_000 + "NEEDLE(1)" + "b" * 10_000
    res = S.lire(ligne, search="needle(1)")
    texte = res["hunks"][0]["text"].split("\t", 1)[1]
    morceau = texte.strip("…")
    assert "NEEDLE(1)" in morceau and morceau in ligne
    assert len(texte) < 1_000


def test_recherche_bornee():
    res = S.lire("hit\n" * 200, search="hit", context=0)
    assert res["matches"] == 200 and res["truncated"] is True
    assert len(res["hunks"]) <= S.MAX_OCCURRENCES


def test_la_recherche_est_litterale():
    # Une regex d'agent n'est jamais compilée : `.*` cherche « .* ».
    res = S.lire("a.*b\naxxb", search=".*b", context=0)
    assert res["matches"] == 1 and res["hunks"][0]["from"] == 1


@pytest.mark.parametrize("kw", [dict(search="a"), dict(search="x" * 301),
                                dict(search="ab", context=11), dict(search="ab", context=-1),
                                dict(search="ab", lines=[1]), dict(context=2)])
def test_requetes_refusees(kw):
    with pytest.raises(S.SourceRefusee):
        S.lire("ab\nab", **kw)


def test_recherche_bornee_sur_des_lignes_longues_voisines():
    """Cinquante occurrences sur des lignes voisines de 3 000 caractères : une seule
    fenêtre, qui dépasserait le budget — coupée à sa dernière ligne ENTIÈRE, `to` exact."""
    lignes = [f"hit{i:02d}" + "x" * 3_000 for i in range(50)]
    res = S.lire("\n".join(lignes), search="hit", context=0)
    assert res["truncated"] is True
    (h,) = res["hunks"]
    rendues = h["text"].split("\n")
    assert int(rendues[-1].split("\t", 1)[0]) == h["to"] < 50
    assert all(r.split("\t", 1)[1] == lignes[int(r.split("\t", 1)[0]) - 1] for r in rendues)
    assert len(h["text"]) <= S.MAX_TEXTE


def test_une_balise_d_un_autre_bloc_ne_ferme_pas_le_script():
    html = "<script>\nvar css = '</style>';\nfunction f(){\nreturn 1;\n}\n</script>"
    plan = _plan(html)
    assert any(l.startswith("1-6 script") for l in plan)
    assert any(l.startswith("3-5 function f") for l in plan)


def test_un_million_de_caracteres():
    bloc = "function f%d(){var s='{';return `${'}'}`;}\n"
    html = "<script>\n" + "".join(bloc % i for i in range(25_000)) + "</script>"
    assert len(html) > 1_000_000
    assert S.lire(html)["omitted"] > 0
    assert S.lire(html, search="f24999(")["matches"] == 1
