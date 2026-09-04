"""Aucune écriture Pennylane ne peut passer pour un succès — oto-backend#872.

Le client d'oto-core rend un refus amont comme une **valeur** (`{"error": "422",
"details": …}`), pas comme une exception. `fetch_all_pages` avait déjà été corrigé
sur ce point (oto-backend#223 : une erreur avalée en liste vide faisait recréer des
avoirs en double), mais le correctif fermait CE chemin, pas la classe : `post`,
`put`, `delete` et `fetch` rendent toujours le dict. Côté outils, la traduction
existait sur trois gestes et manquait sur neuf.

D'où deux épreuves de nature différente :

 * une **statique**, qui lit l'arbre du module et refuse toute écriture non
   enveloppée. Elle vaut surtout pour les écritures qui n'existent pas encore :
   une liste de cas écrite à la main ne voit pas ce qu'on ajoutera demain ;
 * des **empiriques**, qui font répondre au faux client la forme d'erreur réelle
   et exigent une exception — la garde statique prouve l'enveloppe, pas qu'elle
   se déclenche.
"""
import ast
import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError

_TOOLS = pathlib.Path(__file__).resolve().parents[1] / "oto_mcp" / "tools"

# TOUS les modules du connecteur, découverts — pas une liste tenue à la main : le
# connecteur est découpé (`Connector.modules`) et le sera encore. Un tripwire qui
# ne lirait qu'un fichier deviendrait aveugle au prochain module, c'est-à-dire
# exactement là où l'oubli est le plus probable.
# `pennylaneged*` est un AUTRE connecteur (session navigateur, pas clé API) : le
# motif `pennylane_*` ne l'attrape pas, et c'est voulu.
def _modules_du_connecteur() -> list[pathlib.Path]:
    trouves = sorted([_TOOLS / "pennylane.py", *_TOOLS.glob("pennylane_*.py")])
    assert len(trouves) >= 2, (
        "Aucun module frère trouvé : le motif de découverte ne correspond plus "
        f"au découpage du connecteur — {[f.name for f in trouves]}")
    return trouves

# Un appel au client dont le nom commence par l'un de ces verbes ENGAGE la compta
# du client. Le préfixe est le critère parce qu'il survit à l'ajout d'un verbe :
# `create_ledger_entry` sera couvert sans que personne ait à y penser.
VERBES_ECRITURE = ("create_", "update_", "finalize_", "send_", "delete_",
                   "match_", "import_", "link_", "upload_", "letter", "unletter")


def _ecritures_nues(source: str) -> list[tuple[str, int]]:
    arbre = ast.parse(source)

    # Seule la forme DIFFÉRÉE garde quoi que ce soit. Depuis oto-core#77 le client
    # lève sur refus, et `_ecrit(c.create(…))` évaluerait l'appel AVANT d'entrer
    # dans la garde : l'exception passerait à côté. L'ancienne forme est donc
    # refusée, pas tolérée — elle ressemble à une garde sans en être une, ce qui
    # est pire que pas de garde du tout.
    enveloppes = set()
    for n in ast.walk(arbre):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_ecrit" and n.args
                and isinstance(n.args[0], ast.Lambda)):
            for sous in ast.walk(n.args[0].body):
                if isinstance(sous, ast.Call):
                    enveloppes.add(id(sous))

    nues = []
    for n in ast.walk(arbre):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr.startswith(VERBES_ECRITURE)
                and id(n) not in enveloppes):
            nues.append((n.func.attr, n.lineno))
    return nues


def test_aucune_ecriture_ne_contourne_la_traduction_du_refus():
    nues = [(f"{f.name}:{nom}", ligne)
            for f in _modules_du_connecteur()
            for nom, ligne in _ecritures_nues(f.read_text())]
    assert not nues, (
        "Ces appels engagent la comptabilité et rendent un refus de Pennylane comme "
        "une valeur ordinaire : l'agent enchaînera dessus en croyant avoir écrit. "
        "Envelopper dans `_ecrit(lambda: <appel>, \"<le geste, en clair>\")` — "
        + ", ".join(f"{nom} (ligne {ligne})" for nom, ligne in nues))


def test_le_detecteur_voit_une_ecriture_laissee_nue():
    """Sans ça, l'épreuve précédente rendrait du vert même en ne regardant rien.

    On ne l'éprouve pas en amputant le module : le test dépendrait alors de la
    forme exacte d'une ligne, et tomberait au premier reformatage sans que rien
    ne soit cassé. On lui donne un cas fabriqué, minimal et stable."""
    assert _ecritures_nues("def f(c):\n    return c.create_thing(1)\n") == [
        ("create_thing", 2)]


def test_le_detecteur_laisse_passer_une_ecriture_enveloppee():
    """L'autre sens : un détecteur qui crie sur tout ne dit rien non plus."""
    assert _ecritures_nues(
        "def f(c):\n    return _ecrit(lambda: c.create_thing(1), 'x')\n") == []


def test_le_detecteur_refuse_la_forme_non_differee():
    """`_ecrit(c.create(…))` évalue l'appel AVANT d'entrer dans la garde : le
    refus levé par le client passe à côté. Ça ressemble à une garde, ça n'en est
    pas une — et une fausse garde est pire que pas de garde, parce qu'elle se
    relit comme protégée."""
    assert _ecritures_nues(
        "def f(c):\n    return _ecrit(c.create_thing(1), 'x')\n") == [
        ("create_thing", 2)]


def test_le_detecteur_ne_confond_pas_une_lecture_avec_une_ecriture():
    """`find_…` et `get_…` ne s'enveloppent pas : les lectures paginées lèvent
    déjà côté client, et exiger l'enveloppe partout la banaliserait."""
    assert _ecritures_nues(
        "def f(c):\n    return c.find_customer_by_external_reference('x')\n") == []


# --- l'enveloppe se déclenche, et son message oriente ----------------------

@pytest.fixture
def client(monkeypatch):
    """Même gréement que les autres tests du connecteur : `register()` capte
    `PennylaneClient` depuis le PACKAGE, c'est donc l'attribut du package qu'il
    faut remplacer, avant l'appel à `register()`."""
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _tool(nom: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane as P

    m = FastMCP("t")
    P.register(m)
    return asyncio.run(m.get_tool(nom)).fn


def _refus(statut: int, detail: str):
    """Un refus tel que le VRAI client le produit : une exception portant son
    statut. Le double doit échouer comme l'original, sinon l'épreuve valide un
    chemin qui n'existe pas."""
    from oto.tools.common.errors import UpstreamHTTPError

    return UpstreamHTTPError(statut, detail, service="pennylane")


GESTES = [
    ("pennylane_invoice", {"op": "finalize", "invoice_id": 1}, "finalize_invoice"),
    ("pennylane_invoice", {"op": "send", "invoice_id": 1}, "send_invoice"),
    ("pennylane_invoice", {"op": "update", "invoice_id": 1, "fields": {"label": "x"}},
     "update_invoice"),
    ("pennylane_supplier", {"op": "create", "name": "ACME"}, "create_supplier"),
    ("pennylane_match", {"invoice_id": 1, "transaction_id": 2}, "match_transaction"),
]


@pytest.mark.parametrize("tool,kwargs,methode", GESTES)
def test_un_refus_remonte_traduit_et_non_brut(client, tool, kwargs, methode):
    """Le client LÈVE désormais (oto-core#77). La garde doit donc capter cette
    exception et la traduire pour l'agent — sinon elle remonte telle quelle,
    classée mais muette sur ce qu'il faut faire."""
    getattr(client, methode).side_effect = _refus(422, "Entry lines are not balanced")
    with pytest.raises(McpError, match="Entry lines are not balanced"):
        _tool(tool)(**kwargs)


@pytest.mark.parametrize("tool,kwargs,methode", GESTES)
def test_un_succes_traverse_inchange(client, tool, kwargs, methode):
    """La garde ne doit pas se déclencher sur autre chose qu'un refus : une
    réponse qui porte un champ nommé `error` à `None` reste un succès."""
    getattr(client, methode).return_value = {"id": 7, "error": None}
    assert _tool(tool)(**kwargs) == {"id": 7, "error": None}


def test_un_droit_manquant_dit_ou_lire_les_droits_de_la_cle(client):
    """403 : le périmètre d'une clé est propre à qui l'a posée, et rien ne le
    montre avant l'échec. Le message doit envoyer l'agent le lire, sinon il
    rejoue à l'identique ou conclut à une panne."""
    client.match_transaction.side_effect = _refus(403, "insufficient scope")
    with pytest.raises(McpError) as e:
        _tool("pennylane_match")(invoice_id=1, transaction_id=2)
    msg = str(e.value)
    assert "scopes" in msg and 'pennylane_ref(kind="company")' in msg, msg
    assert "DROIT" in msg, "le message doit dire que ce n'est pas un argument à corriger"
