"""Le retrait des sections d'une app (`capabilities/apps_sections.py`) : tout élément marqué
d'une section que le rôle n'ouvre pas part du HTML servi, et l'on retire TROP plutôt que
pas assez — chaque cas adverse ci-dessous a un témoin `SECRET` qui ne doit jamais
survivre, et un témoin `GARDE` qui doit rester à l'octet."""
from __future__ import annotations

import time

import pytest

from oto_mcp.capabilities.apps_sections import avertissements, retirer


def r(html, sections=()):
    return retirer(html, list(sections))


def test_sans_marque_ou_sans_sections_le_meme_objet():
    html = "<div>GARDE</div>" * 3
    assert retirer(html, []) is html
    marque = '<div data-oto-section="admin">x</div>'
    assert retirer(marque, None) is marque
    assert retirer("", []) == "" and retirer(None, []) is None


def test_le_reste_est_recopie_a_l_octet():
    html = ('<!doctype html>\n<p class=a>GARDE &amp; <b>x</b></p>\r\n'
            '<div data-oto-section="admin">SECRET</div>\n<i>GARDE</i>')
    assert r(html) == '<!doctype html>\n<p class=a>GARDE &amp; <b>x</b></p>\r\n\n<i>GARDE</i>'


@pytest.mark.parametrize("sections, garde", [
    ([], False), (["sales"], False), (["admin"], True), (["x", "admin"], True)])
def test_le_role_ouvre_ou_non(sections, garde):
    out = r('<section data-oto-section="admin">SECRET</section>GARDE', sections)
    assert ("SECRET" in out) is garde and "GARDE" in out


def test_plusieurs_noms_un_seul_suffit():
    html = '<div data-oto-section="admin  sales\tops">X</div>'
    assert "X" in r(html, ["ops"]) and "X" not in r(html, ["other"])


def test_seuls_les_blancs_ascii_separent_les_noms():
    # Une espace insécable ne sépare pas : « admin\u00a0sales » est UN nom.
    assert r('<div data-oto-section="admin\u00a0sales">SECRET</div>GARDE', ["admin"]) == "GARDE"
    assert r('<div data-oto-section="admin&#9;sales">X</div>', ["sales"]) == \
        '<div data-oto-section="admin&#9;sales">X</div>'


def test_marque_vide_n_est_ouverte_a_personne():
    assert r('<div data-oto-section="">SECRET</div>GARDE', ["admin"]) == "GARDE"
    assert r('<div data-oto-section>SECRET</div>GARDE', ["admin"]) == "GARDE"


def test_attribut_en_double_chaque_occurrence_doit_etre_ouverte():
    html = '<div data-oto-section="a" data-oto-section="b">SECRET</div>GARDE'
    assert r(html, ["a"]) == "GARDE"
    assert "SECRET" in r(html, ["a", "b"])


@pytest.mark.parametrize("html", [
    '<DIV DATA-OTO-SECTION="admin">SECRET</DIV>GARDE',
    "<div data-oto-section='admin'>SECRET</div>GARDE",
    "<div data-oto-section=admin>SECRET</div>GARDE",
    "<div\ndata-oto-section\n=\n'admin'\n>SECRET</div>GARDE",
    '<div class="a" data-oto-section="admin" id=x>SECRET</div>GARDE',
    '<div data-oto-section="adm&#105;n">SECRET</div>GARDE',
    '<div title="a>b" data-oto-section="admin">SECRET</div>GARDE',
    "<div title='\"></div>' data-oto-section=admin>SECRET</div>GARDE",
    '<div data-oto-section=admin/>SECRET</div>GARDE',     # `/>` n'est rien en HTML
])
def test_formes_d_attribut(html):
    assert r(html) == "GARDE"


@pytest.mark.parametrize("html", [
    '<div data-oto-sections="admin">GARDE</div>',
    '<div data-oto-section-x="admin">GARDE</div>',
    '<div xdata-oto-section="admin">GARDE</div>',
    '<div title="data-oto-section=admin">GARDE</div>',
    '<p>data-oto-section="admin" GARDE</p>',
    '<!-- <div data-oto-section="admin"> -->GARDE',
])
def test_les_sosies_ne_marquent_rien(html):
    assert r(html) == html


def test_imbrication_du_meme_nom():
    html = ('<div>GARDE<div data-oto-section="a">SECRET<div>SECRET<div>SECRET</div>'
            'SECRET</div>SECRET</div>GARDE</div>')
    assert r(html) == "<div>GARDEGARDE</div>"


def test_vides_et_auto_fermants():
    assert r('<img data-oto-section=a src="SECRET.png">GARDE') == "GARDE"
    assert r('<br data-oto-section=a>GARDE<br>') == "GARDE<br>"
    assert r('<svg><g data-oto-section="a"/>GARDE</svg>') == "<svg>GARDE</svg>"
    # Sans guillemets, `/` appartient à la valeur : `<g>` reste ouvert, tout part.
    assert r('<svg><g data-oto-section=a/>SECRET</svg>') == "<svg>"


def test_jamais_ferme_tout_part_jusqu_a_la_fin():
    assert r('GARDE<p data-oto-section=a>SECRET<p>SECRET') == "GARDE"
    assert r('GARDE<ul><li data-oto-section=a>SECRET<li>SECRET</ul>SECRET') == "GARDE<ul>"


def test_la_fermeture_d_un_ancetre_ne_ferme_pas():
    # Le navigateur ferme le <p> à l'ouverture du div marqué : SECRET reste DANS le div.
    assert "SECRET" not in r('<div><p>a<div data-oto-section=x>b</p>SECRET</div>GARDE')
    assert "SECRET" not in r('<div><span>a<div data-oto-section=x>b</span>SECRET</div>')


@pytest.mark.parametrize("html", [
    '<div data-oto-section=a><table></div>SECRET</table></div>',
    '<div data-oto-section=a><table><tr><td></div>SECRET</td></tr></table></div>',
    '<div data-oto-section=a><template></div>SECRET</template></div>',
    '<div data-oto-section=a><select></div>SECRET</select></div>',
    '<div data-oto-section=a><select><option></div>SECRET</select></div>',
    '<button data-oto-section=a><select></button>SECRET</select></button>',
    '<form data-oto-section=a><div></form>SECRET</div>',
    '<ul><li data-oto-section=a><ul></li>SECRET</ul></li></ul>',
    '<table><tr><td data-oto-section=a><table></td>SECRET</table></td></tr></table>',
    '<table><tr data-oto-section=a><td><table></tr>SECRET</table></td></tr></table>',
    '<button data-oto-section=a><table></button>SECRET</table></button>',
    '<object data-oto-section=a><table></object>SECRET</table></object>',
    '<form data-oto-section=a><span></form>SECRET</span>',
    '<div data-oto-section=a><p data-oto-section=a>x</div>SECRET',     # p marqué, ouvert
    '<span data-oto-section=a><p></span>SECRET</p></span>',
    '<p data-oto-section=a><b data-oto-section=a></p>SECRET',     # b recloné après </p>
])
def test_une_fermeture_que_le_navigateur_ignore(html):
    assert "SECRET" not in r(html)


def test_une_fermeture_que_le_navigateur_suit():
    # `</div>` traverse un <button>, un <p>, un <span> ouverts : le div finit là.
    assert r('<div data-oto-section=a><button>x</div>GARDE') == "GARDE"
    assert r('<div data-oto-section=a><p><span>x</div>GARDE') == "GARDE"
    assert r('<div data-oto-section=a><table><tr><td><div></table>x</div>GARDE') == "GARDE"


def test_cellule_et_tableau_marques():
    html = '<table><tr><td data-oto-section=a>SECRET</td><td>GARDE</td></tr></table>'
    assert r(html) == "<table><tr><td>GARDE</td></tr></table>"
    assert r('<table data-oto-section=a><tr><td>SECRET</table>GARDE') == "GARDE"


def test_commentaires():
    assert r('<div data-oto-section=a>SECRET<!-- </div> -->SECRET</div>GARDE') == "GARDE"
    assert r('<div data-oto-section=a>SECRET<!-- </div> --!>SECRET</div>GARDE') == "GARDE"
    # Commentaires abrupts : `<!-->` et `<!--->` sont déjà fermés.
    assert r('<!--><div data-oto-section=a>SECRET</div>GARDE') == "<!-->GARDE"
    assert r('<!---><div data-oto-section=a>SECRET</div>GARDE') == "<!--->GARDE"
    # Commentaire jamais fermé dans l'élément : tout part.
    assert r('GARDE<div data-oto-section=a><!-- SECRET</div>SECRET') == "GARDE"
    # Faux commentaires : `<!x>`, `<?x>`, `</ x>` finissent au premier `>`.
    assert r('<div data-oto-section=a><![CDATA[</div>]]>SECRET</div>GARDE') == "GARDE"
    assert r('<?x <div data-oto-section=a>?>GARDE') == '<?x <div data-oto-section=a>?>GARDE'
    assert r('<div data-oto-section=a></ div>SECRET</div>GARDE') == "GARDE"


def test_cdata_en_svg():
    html = '<svg><g data-oto-section=a><![CDATA[ </g> ]]>SECRET</g>GARDE</svg>'
    assert r(html) == "<svg>GARDE</svg>"


def test_scripts_et_styles_marques_partent():
    assert r('<script data-oto-section=a>const SECRET = 1</script>GARDE') == "GARDE"
    assert r('<style data-oto-section=a>.SECRET{}</style>GARDE') == "GARDE"
    assert r('<template data-oto-section=a><b>SECRET</b></template>GARDE') == "GARDE"
    # Un script échappé : `</script>` dans `<!-- <script> … -->` ne le ferme pas.
    html = '<script data-oto-section=a><!-- <script> </script> SECRET --></script>GARDE'
    assert r(html) == "GARDE"
    # Un script marqué jamais fermé : tout part.
    assert r('GARDE<script data-oto-section=a>SECRET') == "GARDE"


def test_le_script_interieur_part_avec_son_element():
    html = '<div data-oto-section=a><script>x = "</div>"; SECRET</script>SECRET</div>GARDE'
    assert r(html) == "GARDE"
    html = '<div data-oto-section=a><textarea></div>SECRET</textarea>SECRET</div>GARDE'
    assert r(html) == "GARDE"
    html = '<div data-oto-section=a><style>/* </div> */ SECRET</style></div>GARDE'
    assert r(html) == "GARDE"


def test_le_texte_d_un_script_non_marque_n_est_pas_touche():
    html = "<script>const t = '<div data-oto-section=\"a\">GARDE</div>';</script>"
    assert r(html) == html


def test_contenus_jamais_executes_relus():
    assert r('<noscript><p data-oto-section=a>SECRET</p>GARDE</noscript>') == \
        "<noscript>GARDE</noscript>"
    tpl = '<script type="text/x-template"><div data-oto-section=a>SECRET</div>GARDE</script>'
    assert r(tpl) == '<script type="text/x-template">GARDE</script>'


def test_html_head_body_marques_tout_part():
    assert r('GARDE<body data-oto-section=a>SECRET</body>SECRET') == "GARDE"


def test_balise_coupee_par_la_fin():
    assert r('GARDE<div data-oto-section=a title="SECRET') == "GARDE"
    assert r('GARDE<div title="x') == 'GARDE<div title="x'


def test_majuscules_et_fermante_avec_blancs():
    assert r('<DIV data-oto-section=a>SECRET</Div >GARDE') == "GARDE"


def test_lineaire_au_plafond():
    n = 1_000_000
    cas = ['<a b c d e f g h i j k l>' * (n // 25) + '<i data-oto-section=a>x</i>',
           '<' * n + '<i data-oto-section=a>x</i>',
           '<div data-oto-section=a>' + '<table>' * 400 + '</td>' * (n // 5),
           '<div data-oto-section=a>' + ('<span data-oto-section=a>' + '<table>' * 500) * 31,
           '<svg>' * 600 + '</svg>' * (n // 6) + '<i data-oto-section=a>x</i>',
           '<p data-oto-section=a>x</p>' * (n // 27),
           '<script><!--<script>' * (n // 20) + '<b data-oto-section=a>',
           ''.join(f'<i data-oto-section="n{k}">x</i>\n' for k in range(n // 40))]
    for html in cas:
        t = time.perf_counter()
        r(html)
        avertissements({"roles": {"r": {"sections": ["z"]}}}, html)
        assert time.perf_counter() - t < 5, html[:40]


# ── Les avertissements à l'écriture ──────────────────────────────────────────────

ROLES = {"roles": {"admin": {"sections": ["admin", "pipeline"]},
                   "user": {"sections": ["pipeline"]}}}


def test_avertit_une_section_que_rien_ne_porte():
    w = avertissements(ROLES, '<div data-oto-section="pipeline">x</div>')
    assert len(w) == 1 and '"admin"' in w[0] and "admin" in w[0]


def test_avertit_une_marque_qu_aucun_role_ne_declare():
    html = ('<div data-oto-section="pipeline">x</div><div data-oto-section="admin">y</div>\n'
            '<div data-oto-section="billing">z</div>')
    w = avertissements(ROLES, html)
    assert w == ['Line 2: data-oto-section="billing" names a section no role declares: '
                 "it is removed for every role that lists `sections`."]


def test_avertit_un_element_marque_jamais_ferme():
    html = '<div data-oto-section="pipeline">x</div>\n<p data-oto-section="admin">y'
    w = avertissements(ROLES, html)
    assert any("Line 2: <p data-oto-section> is never closed" in x for x in w)


def test_avertissements_bornes():
    html = "".join(f'<i data-oto-section="n{k}">x</i>\n' for k in range(500))
    assert len(avertissements(ROLES, html)) <= 10
    assert len(avertissements({}, html)[0]) < 400


def test_sans_roles_les_marques_n_ont_aucun_effet():
    w = avertissements({}, '<div data-oto-section="a">x</div>')
    assert len(w) == 1 and "declares no `roles`" in w[0]
    assert avertissements({}, "<div>x</div>") == []
    assert avertissements(ROLES, None) == []


def test_tout_va_bien():
    html = '<div data-oto-section="pipeline">x</div><div data-oto-section="admin pipeline">y</div>'
    assert avertissements(ROLES, html) == []


# ── Le mode strict ───────────────────────────────────────────────────────────────

from oto_mcp.capabilities import apps_sections as _S  # noqa: E402

_ROLES = {"roles": {"a": {"sections": ["admin"]}, "u": {}}, "default_role": "u"}


def test_strict_suit_les_sections_et_s_eteint_explicitement():
    assert _S.strict(_ROLES)
    assert not _S.strict({**_ROLES, "strict_sections": False})
    assert not _S.strict({"roles": {"u": {}}, "default_role": "u"})
    assert not _S.strict({})


def test_un_script_ou_un_style_non_classe_est_nomme():
    for brut in ('<script>let x = 1</script>', '<style>.a{}</style>',
                 '<SCRIPT type="module">x</SCRIPT>'):
        assert _S.non_classes("<main>" + brut + "</main>"), brut


def test_le_commun_et_les_sections_passent():
    html = ('<script data-oto-section="*">const t = "<style>"; el.innerHTML = '
            '"<div onclick=go()>"</script>'
            '<section data-oto-section="admin"><script>admin()</script>'
            '<button onclick="x()">X</button></section>'
            '<style data-oto-section="admin">.secret{}</style><p>Texte commun</p>')
    assert _S.non_classes(html) == []


def test_un_gestionnaire_non_classe_est_nomme():
    assert _S.non_classes('<button onclick="go()">Go</button>')
    assert _S.non_classes('<img src=x ONERROR=go()>')
    assert _S.non_classes('<button data-oto-section="*" onclick="go()">Go</button>') == []


def test_une_balise_jamais_fermee_ne_se_classe_pas():
    assert _S.non_classes('<p>ok</p><script src="x.js"')
    # Marquée et coupée : le retrait l'emporte déjà jusqu'à la fin, rien ne part.
    assert _S.non_classes('<p>ok</p><script data-oto-section="admin"') == []


def test_le_commun_reste_meme_sans_aucune_section_ouverte():
    html = '<script data-oto-section="*">a()</script><div data-oto-section="admin">S</div>'
    out = _S.retirer(html, [])
    assert 'a()' in out and "S</div>" not in out


def test_etoile_reservee_et_jamais_signalee():
    from oto_mcp.capabilities.apps_manifeste import forme
    _, problems = forme({"roles": {"u": {"sections": ["*"]}}, "default_role": "u"})
    assert any("reserved" in p for p in problems)
    w = _S.avertissements(_ROLES, '<div data-oto-section="*">x</div><div data-oto-section="admin">y</div>')
    assert not any('"*"' in x and "no role declares" in x for x in w)


def test_une_section_nommee_dans_le_commun_avertit():
    w = _S.noms_dans_le_commun(_ROLES, '<script data-oto-section="*">'
                                       'if (access.sections.includes("admin")) secret()</script>')
    assert w and "admin" in w[0]
