"""Le rendu markdown → HTML d'un corps de message, ASSAINI par liste blanche (`nh3`).

Une seule fonction pour tous les chemins qui composent un message en markdown
(`gmail` op=draft, `gmail_act` op=send et ses réponses, `email_send`) : le rendu
d'oto-core (`_markdown_to_html_fragment` — tableaux, blocs de code, listes) laisse
passer le HTML brut, les attributs posés par `attr_list` (`{onclick=…}`) et les liens
`javascript:`/`data:`. Ce qui part est le HTML assaini, jamais le rendu brut.

Ce qui reste : la mise en forme d'un message, rien qui s'exécute ni se déguise —
balises de texte, de liste, de titre et de tableau, liens `http(s)`/`mailto`, et
l'alignement d'une colonne de tableau (seule propriété de style admise). Une image
n'est gardée que sur demande (`images=True`), en `http(s)`.
"""
from __future__ import annotations

BALISES = frozenset({
    "p", "br", "strong", "em", "b", "i", "del", "code", "pre", "blockquote", "hr",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "a",
    "table", "thead", "tbody", "tr", "th", "td",
})
_ATTRIBUTS = {"a": {"href", "title"}, "th": {"align", "style"}, "td": {"align", "style"}}
_ATTRIBUTS_IMAGE = {"img": {"src", "alt", "title"}}
_SCHEMAS = frozenset({"http", "https", "mailto"})
_STYLES = frozenset({"text-align"})


def rendu_markdown_assaini(body: str, *, images: bool = False) -> str:
    """Le HTML d'un corps écrit en markdown, rendu comme oto-core le rend puis assaini.
    `images=True` garde `<img>` (src `http(s)`, alt, title) ; sinon l'image est retirée."""
    import nh3
    from oto.tools.google.gmail.lib.gmail_client import _markdown_to_html_fragment

    balises = set(BALISES) | ({"img"} if images else set())
    attributs = {k: set(v) for k, v in _ATTRIBUTS.items()}
    if images:
        attributs.update({k: set(v) for k, v in _ATTRIBUTS_IMAGE.items()})
    return nh3.clean(_markdown_to_html_fragment(body or ""), tags=balises,
                     attributes=attributs, url_schemes=set(_SCHEMAS),
                     filter_style_properties=set(_STYLES),
                     link_rel="noopener noreferrer")
