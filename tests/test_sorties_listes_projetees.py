"""Cliquet : un outil NEUF qui rend une liste déclare ce que son défaut retire.

Ce que rend un outil est le poste dominant d'un workflow agentique. Mesuré le
2026-09-10 sur trois jours d'une org cliente en production (`tool_calls.result_size`) :
un seul outil de liste pesait 68 % de tous les caractères servis. Il projetait déjà,
mais seulement des blocs entiers, et il a fallu une PR après coup (#918) pour que son
défaut cesse de rendre ce qu'aucun appelant ne lit : −46 % par enregistrement.

La leçon qui fonde le cliquet est écrite dans `output_projection.py` : **une économie
qu'il faut connaître pour en bénéficier ne bénéficie à personne**. Un agent ne passe
pas un opt-in qu'il ne connaît pas. L'économie doit donc être dans le DÉFAUT de
l'outil — et le seul moment où ça ne coûte rien de l'y mettre, c'est quand l'outil
s'écrit.

**Rattrapage progressif, pas de grand soir** — même patron que
`test_capability_outputs.py` : les 200 outils qui n'ont rien sont nommés dans
`tool_output_debt.txt`. La liste ne peut que rétrécir ; un outil hors liste doit
projeter, sinon la CI casse. Ce test ne corrige rien de l'existant : il empêche le
prochain.

Ce que la règle mesure et ne mesure pas est écrit en tête du fichier de dette, pour
que personne ne lui fasse dire plus qu'elle ne dit.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import textwrap

import pytest

from oto_mcp.tools.decrits import OutilDecrit

_DEBT_FILE = pathlib.Path(__file__).resolve().parent / "tool_output_debt.txt"

# Un de ces paramètres ⟹ l'outil rend une liste par construction.
_PAGINATION = frozenset({"page", "size", "limit", "cursor", "page_size", "per_page",
                         "offset"})

# `full` ⟹ l'appelant peut projeter, et le défaut est la vue resserrée (`full=True` rend
# le brut, ADR 0047).
# `fields` ⟹ seulement s'il NOMME des clés à garder (une liste, ou une chaîne de noms).
# ⚠️ Jusqu'au 2026-10-12 le seul NOM `fields` suffisait, et il blanchissait 17 outils
# qui ne retirent rien : chez eux `fields` est le CORPS d'une écriture (un objet
# `{clé: valeur}` — github, leexi, productlane, lemlist_lead, brevo_campaign) ou les
# champs où CHERCHER (pipedrive_search). Un homonyme n'est pas une projection : c'est
# la forme du paramètre qui le dit, pas son nom.
_FIELDS_QUI_NE_PROJETTENT_PAS = {
    # Une chaîne de noms, comme une vraie projection — mais ce sont les champs où
    # l'amont CHERCHE le terme, pas ceux qu'il rend. La forme ne peut pas le dire.
    "pipedrive_search": "champs où chercher, pas champs rendus",
}

# Un de ces appels dans le CORPS de l'outil ⟹ il retire quelque chose avant de rendre.
# ⚠️ Confrontés à l'APPELÉ (`_appeles`), jamais au texte de la source : cf. `_projette`.
_PROJECTION_APPELS = ("output_projection", "_shape(", "summarize(", "_project(",
                      "_slim")

# Plafond de la dette : il ne peut que BAISSER. Mesuré le 2026-09-10 sur le montage
# réel — 653 outils servis, 248 qui paginent, 47 qui projettent par `fields`/`full`.
# ⚠️ Il est passé de 198 à 200 le 2026-09-10, et ce n'est PAS la dette qui a grossi :
# le critère d'origine lisait la source en entier, prose comprise, et blanchissait
# deux outils qui ne projettent rien (cf. `_appeles`). Un plafond posé sur un critère
# faux garantit la décroissance d'un chiffre qui ne décrit personne.
#
# ⚠️ **Seconde et dernière remontée, 196 → 213 le 2026-10-12** : même cause qu'en
# septembre, un critère qui blanchissait. Le paramètre `fields` comptait comme projection
# par son seul nom ; 17 outils où il désigne un corps d'écriture ou un filtre de
# recherche sont désormais nommés dans la dette (cf. `_fields_projette`).
# 213 → 210 le même jour : github_repos, github_issues, github_pulls payés (index par
# défaut, `full=True` le brut — `tools/github_index.py`).
# 210 → 209 le 2026-10-16 : zohodesk_articles payé (vue légère par défaut, `full=True`
# le brut — `tools/zohodesk.py`).
# 209 → 208 le 2026-10-27 : linkedin_unipile payé (URL de profil ramenée au slug, fils
# allégeables — `tools/unipile.py`).
# 208 → 210 le 2026-11-09, décision d'Alexis : seranking et seranking_act, servis par
# leur description, entrent avec leur motif (cf. `tool_output_debt.txt`). Ce n'est pas un
# critère qui blanchissait : c'est une exception tranchée, la seule à ce jour.
# 210 → 208 le même jour : seranking et seranking_act payés (seranking 1.1.0, oto-core
# v1.217.0 : chaque liste paginée déclare `output.projection` ou `strip`).
# 208 → 207 le 2026-11-13 : lemlist_get_activities payé (`fields` et `compact`, la vue
# du client d'oto-core — lemlist 1.4.0).
_PLAFOND = 207


def _debt() -> set[str]:
    lines = _DEBT_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def _servis():
    """Les outils du montage RÉEL — un garde-fou d'inventaire qui s'exerce sur une
    fixture partielle ment par omission (`docs/conventions.md`)."""
    from fastmcp import FastMCP

    from oto_mcp.tools import register_all

    m = FastMCP("cliquet-sorties")
    try:
        register_all(m)
    except Exception as e:  # deps optionnelles absentes en CI minimal
        pytest.skip(f"register_all indisponible : {e}")
    return asyncio.run(m.list_tools(run_middleware=False))


def _pagine(tool) -> bool:
    return bool(_PAGINATION & set((tool.parameters or {}).get("properties", {})))


def _nom_pointe(noeud) -> str:
    """`output_projection.project` pour `output_projection.project(...)`, `""` sinon."""
    if isinstance(noeud, ast.Name):
        return noeud.id
    if isinstance(noeud, ast.Attribute):
        gauche = _nom_pointe(noeud.value)
        return f"{gauche}.{noeud.attr}" if gauche else noeud.attr
    return ""


def _appeles(fn) -> set[str] | None:
    """Ce que le corps de l'outil APPELLE, sous la forme `nom.pointé(` — ou `None` si
    la source n'est pas lisible.

    ⚠️ Le critère d'origine cherchait les marqueurs dans `inspect.getsource` **en
    entier** : il jugeait donc aussi la prose et la signature. Deux outils étaient
    blanchis à tort le 2026-09-10 — `ahrefs_rank_tracker`, dont une docstring cite
    `ahrefs_project(op="list")` pour dire d'où vient un identifiant, et
    `posthog_project`, que sa propre ligne `def posthog_project(` faisait matcher.
    Ni l'un ni l'autre ne retire quoi que ce soit. **Un marqueur cité, ou porté par le
    nom de l'outil, n'est pas un marqueur appelé** : seul l'appelé est du code qui agit.

    Passer par l'AST plutôt que par un `str.replace` des docstrings règle les deux d'un
    coup, et referme au passage les commentaires — un marqueur en commentaire ne
    projette pas davantage."""
    try:
        arbre = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        # Source illisible ou non ré-analysable (outil généré, lambda) : on ne peut pas
        # prouver qu'il projette.
        return None
    return {_nom_pointe(n.func) + "(" for n in ast.walk(arbre)
            if isinstance(n, ast.Call)}


def _fields_projette(tool) -> bool:
    """`fields` NOMME-t-il des clés à garder ? Un objet est un corps à écrire, jamais une
    projection ; une liste ou une chaîne de noms en est une, sauf homonyme déclaré."""
    schema = (tool.parameters or {}).get("properties", {}).get("fields")
    if schema is None or tool.name in _FIELDS_QUI_NE_PROJETTENT_PAS:
        return False
    variantes = schema.get("anyOf") or [schema]
    return not any(v.get("type") == "object" for v in variantes)


def _projette(tool) -> bool:
    if "full" in (tool.parameters or {}).get("properties", {}) or _fields_projette(tool):
        return True
    if isinstance(tool, OutilDecrit):
        return _decrit_projette(tool)
    appeles = _appeles(tool.fn)
    if appeles is None:
        return False
    return any(marqueur in cible for cible in appeles for marqueur in _PROJECTION_APPELS)


def _decrit_projette(tool) -> bool:
    """Un outil servi par le lecteur générique (`tools/decrits.py`) n'a pas de corps à
    lire : sa sortie est dite par sa description. Il projette si CHAQUE fonction qui
    pagine déclare `output.projection` (les clés gardées de chaque enregistrement) ou
    `output.strip` (les clés retirées), que la lib applique avant de rendre.
    ⚠️ Assouplissement du 2026-11-09 : `strip` compte aussi. Il est la seule forme quand
    la liste est à la racine de la réponse (le format ne la projette pas) ou quand
    projeter ferait perdre ce qui l'entoure (le `total` d'une recherche) ; une fonction
    qui ne déclare ni l'un ni l'autre rend toujours le brut, et l'outil reste jugé."""
    paginent = [f for f in tool.fonctions
                if _PAGINATION & set(f.get("input", {}).get("properties", {}))]
    return all({"projection", "strip"} & set(f.get("output") or {}) for f in paginent)


def _sans_projection(tools) -> set[str]:
    return {t.name for t in tools if _pagine(t) and not _projette(t)}


def test_un_outil_neuf_qui_pagine_declare_sa_projection():
    intrus = sorted(_sans_projection(_servis()) - _debt())
    assert not intrus, (
        f"Outils de LISTE sans projection, hors dette connue : {intrus}. Chacun rend "
        "chaque enregistrement tel que l'amont le livre, et un agent paie ce poids à "
        "chaque tour. Deux issues : exposer `full: bool = False` et resserrer le "
        "défaut (cf. `tools/aiark.py::_shape`), ou appeler `output_projection.project` "
        "/ `summarize` dans le corps de l'outil, ou, pour un outil servi par sa description, "
        "déclarer `output.projection` (ou `strip`) sur chaque fonction qui pagine. Ne PAS l'ajouter à "
        "`tool_output_debt.txt` : la dette ne grossit pas.")


def test_la_dette_ne_ment_pas():
    """Une ligne PAYÉE doit quitter la liste — sinon la marge libérée se remplirait en
    silence, et la liste cesserait de décrire le réel.

    ⚠️ Seuls les outils SERVIS ICI sont jugés. Le catalogue dépend des extras installés
    (un connecteur dont le cœur oto-core manque n'est pas monté, cf. `docs/commands.md`
    §Pin oto-core) : un poste de dev en retard sur le pin en voit moins que la CI. Une
    ligne dont l'outil n'est pas monté ici n'est donc NI « disparue » NI « payée » —
    la juger rendrait ce test rouge sur tout venv en retard, pour une raison qui n'a
    rien à voir avec la dette. La garantie de décroissance est portée par `_PLAFOND`,
    qui ne dépend d'aucun catalogue."""
    tools = _servis()
    servis = {t.name for t in tools}
    payes = sorted((_debt() & servis) - _sans_projection(tools))
    assert not payes, (
        f"Ces outils projettent désormais, ou ne paginent plus : {payes}. Retire-les "
        f"de {_DEBT_FILE.name} ET baisse `_PLAFOND` d'autant — c'est ce qui rend la "
        "baisse définitive.")


def test_la_dette_ne_fait_que_baisser():
    assert len(_debt()) <= _PLAFOND, (
        f"la dette de sortie a grossi ({len(_debt())} outils pour un plafond de "
        f"{_PLAFOND}). Elle doit DÉCROÎTRE : projette plutôt que d'élargir la liste.")


# ── Le critère lui-même : ce qu'il doit refuser de lire ────────────────────────
class _Outil:
    """Un outil minimal : `_projette` ne lit que `name`, `parameters` et `fn`."""

    def __init__(self, fn, *, name="outil_de_banc", **props):
        self.name = name
        self.parameters = {"properties": {"limit": {}, **props}}
        self.fn = fn


def _shape(ligne):
    """Cible d'appel des faux outils ci-dessous — jamais exécutée en vrai."""
    return ligne


def test_le_critere_ne_lit_ni_la_prose_ni_le_nom_de_l_outil():
    """Deux outils servis étaient blanchis par leur seule prose ou leur seul nom.

    `ahrefs_rank_tracker` citait `ahrefs_project(op="list")` dans sa docstring pour
    dire d'où vient un identifiant ; `posthog_project` portait `_project(` dans sa
    propre ligne `def`. Aucun des deux ne retire quoi que ce soit du résultat, et le
    critère textuel les comptait comme payés — c'est ce qui faisait lire 198 là où la
    dette réelle est 200."""
    def _liste_project(limit: int = 10):
        """Le `project_id` vient de `ahrefs_project(op="list")`."""
        # summarize( en commentaire ne retire rien non plus
        return {"rows": []}

    def _liste_projetee(limit: int = 10):
        return {"rows": [_shape(l) for l in []]}

    assert not _projette(_Outil(_liste_project)), (
        "un marqueur cité en docstring ou en commentaire, ou porté par le NOM de "
        "l'outil, n'est pas une projection : seul l'appelé est du code qui agit")
    assert _projette(_Outil(_liste_projetee)), (
        "un appel réel à une projection doit rester reconnu")


def test_un_nom_qui_contient_un_marqueur_ne_suffit_pas():
    """Le symétrique : juger l'AST sans borner à l'appelé rouvrirait le même trou.

    `resolve_project_id(`, `list_hiring_projects(` et `_shape_submissions_page(` sont
    des appels RÉELS de trois outils servis — aucun n'est une projection. Le marqueur
    se confronte à l'appelé entier, parenthèse comprise (`_shape(`), pas au nom nu."""
    def _liste_voisine(limit: int = 10):
        return {"rows": resolve_project_id(_shape_submissions_page(limit))}

    assert not _projette(_Outil(_liste_voisine))


def resolve_project_id(x):
    """Cible d'appel du banc ci-dessus — un voisin lexical, pas une projection."""
    return x


def _shape_submissions_page(x):
    """Idem : `_shape_submissions_page(` ne contient pas `_shape(`."""
    return x


def _liste_brute(limit: int = 10, fields=None):
    """Cible des bancs `fields` ci-dessous : ne retire rien."""
    return {"rows": []}


def test_un_fields_homonyme_n_est_pas_une_projection():
    """`fields` comptait par son seul nom : 17 outils servis étaient blanchis alors que
    leur `fields` est le corps d'une écriture ou le champ d'une recherche."""
    corps = {"anyOf": [{"type": "object", "additionalProperties": True},
                       {"type": "null"}]}
    noms = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
    assert not _projette(_Outil(_liste_brute, fields=corps)), (
        "un `fields` objet est un corps à écrire, pas une projection")
    assert _projette(_Outil(_liste_brute, fields=noms)), (
        "un `fields` qui nomme des clés reste une projection")
    assert not _projette(_Outil(_liste_brute, name="pipedrive_search",
                                fields={"type": "string"})), (
        "un homonyme déclaré ne projette pas, même sous la forme d'une chaîne de noms")
    assert _projette(_Outil(_liste_brute, full={"type": "boolean"}))


def test_un_outil_decrit_projette_par_sa_description():
    """Le lecteur générique n'a pas de corps à lire : c'est `output.projection` ou
    `output.strip` de chaque fonction qui pagine qui en tient lieu. Sans l'un ni
    l'autre, la liste sort telle que l'amont la livre, et l'outil est jugé comme un
    outil écrit à la main qui ne projette pas."""
    from fastmcp import FastMCP

    import _decrit_d_essai as essai
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import decrits

    def lecteur(description):
        try:
            outils = {t.name: t for t in decrits.monter(FastMCP("essai"), "essai", description)}
        finally:
            connector_verify._REGISTRY.pop("essai", None)
            connector_verify._COUVERTURE.pop("essai", None)
        return outils["essai"]

    assert _pagine(lecteur(essai.description()))
    assert _projette(lecteur(essai.description())), "list_things déclare sa projection"
    strip_seul = essai.description()
    for f in strip_seul["functions"]:
        (f.get("output") or {}).pop("projection", None)
    assert _projette(lecteur(strip_seul)), "strip seul retire des clés : il compte (2026-11-09)"
    sans = essai.description()
    for f in sans["functions"]:
        f.pop("output", None)
    assert not _projette(lecteur(sans)), "ni projection ni strip : la liste sort brute"
