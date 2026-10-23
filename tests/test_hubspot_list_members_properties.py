"""`hubspot_list op='members'` + `properties` : la page enrichie en 2 appels.

Sans `properties`, l'op rendait — et rend toujours — des ids d'enregistrements
nus. Une procédure qui veut sept colonnes par membre enchaînait donc un
`hubspot_object op='get'` PAR membre : un N+1 qui tape le plafond d'une private
app (190 requêtes / 10 s) vers la quarantième fiche. `properties` compose la
page d'appartenances avec UN batch read.

⚠️ **LA FORME DU RETOUR D'oto-core EST LE SUJET DE CE FICHIER.**
`batch_read_objects` rend une ENVELOPPE — `{"results": [...], "missing_ids":
[...]}` — et non une liste. Une première version de ces tests posait
`inst.batch_read_objects.return_value = [ … ]` sur un `MagicMock()` NU : 29
tests verts contre un client qui n'existe pas, et un `AttributeError` sur le
premier appel réel (itérer un dict rend ses CLÉS, donc `"results"`, une chaîne).
C'est très exactement le test qui décrit l'intention de son auteur au lieu du
système (`docs/conventions.md`). Deux garde-fous sont posés ici contre le
retour de cette panne :

- `_enveloppe()` ci-dessous est le SEUL endroit de ce fichier où cette forme est
  écrite à la main : tous les mocks passent par lui, donc ils ne peuvent plus
  diverger entre eux ;
- `test_a_bare_list_from_the_client_is_refused_by_name` fait passer au tool la
  forme de l'ANCIEN client et exige un refus NOMMÉ. Sans lui, un client qui
  régresse redonne un `AttributeError` opaque en prod ;
- **les deux tests de la section finale n'utilisent AUCUN mock du client** : ils
  importent le vrai `HubSpotClient`, ne remplacent que le transport HTTP, et
  confrontent la valeur qu'il construit réellement au contrat. C'est ce qui
  rattache `_enveloppe()` à oto-core : sans eux, un RENOMMAGE de `missing_ids`
  côté client laisserait tout ce fichier vert pendant que la prod perdrait son
  relevé d'écart — la panne de ce lot, rejouée un cran plus loin.

Le mock est conformé (`spec=`) à la vraie classe : un `MagicMock()` nu accepte
n'importe quel nom de méthode, donc une faute de frappe sur `get_lst` passerait.

Le reste de ce qui est testé ici est ce qui peut casser SILENCIEUSEMENT :

1. **Le chemin sans `properties`.** C'est le contrat déjà servi. S'il se mettait
   à ré-emballer sa réponse, à faire un GET de plus ou à ajouter une clé, aucun
   appelant ne lèverait — ils liraient juste autre chose. D'où une assertion
   d'IDENTITÉ (`is`), pas d'égalité, et un comptage d'appels.
2. **Le recollage appartenance ↔ enregistrement.** Le batch read peut rendre
   MOINS d'objets qu'on n'en demande (supprimé entre les deux appels, hors des
   droits de la clé). Recoller par index, ou itérer les enregistrements plutôt
   que les appartenances, perdrait des membres sans un mot — dans une population
   de prospection, une ligne muette est pire qu'un refus.
3. **Le type d'objet du batch read.** Une appartenance ne dit pas de quel objet
   elle parle. Le deviner (« contacts ») ne lève pas : ça lit le mauvais objet
   et rend une population plausible et fausse.
4. **Le découpage à 100.** Il appartient au client oto-core. Un second
   découpeur ici serait un miroir que rien ne relie.
5. **La description SERVIE.** FastMCP tronque la `description` au bloc `Args:` :
   la phrase qui fait choisir 2 appels plutôt que 101 doit vivre AVANT.

⚠️ **Ce fichier a DEUX régimes vis-à-vis du pin, et c'est délibéré.** Tout ce qui
précède la dernière section mocke le client : indépendant du pin, donc vert dès
maintenant. Les deux derniers tests, eux, exercent le client INSTALLÉ — ils sont
marqués `exige_pin_oto_core`, donc non concluants en local quand l'écart
venv↔pin est mesurable, et mordants en CI, qui installe AU tag. Sur un venv dont
les métadonnées portent le numéro gelé (`1.100.0`, qui ne dit rien du tag),
l'écart n'est pas mesurable : ils s'exécutent, et contre un client d'avant
l'enveloppe ils sont ROUGES en nommant la bonne pièce. C'est voulu — on ne
neutralise pas un garde-fou sur une mesure qu'on a réellement faite.

`batch_read_objects` n'existe pas sur le tag épinglé aujourd'hui :
`tests/test_tools_client_methods_exist.py` porte cette dépendance et doit rester
rouge jusqu'au bump.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from oto.tools.hubspot.client import HubSpotClient
from oto_mcp.mcp_errors import McpError


def _enveloppe(results, missing_ids=()) -> dict:
    """La forme que `batch_read_objects` rend, écrite UNE fois.

    oto-core : `{"results": [...], "missing_ids": [...]}`. `missing_ids` porte
    les ids demandés que HubSpot n'a pas rendus (son batch read répond 207 sans
    nommer les absents) ; le client est le seul endroit où cet écart se calcule,
    donc le seul à pouvoir le dire.

    Aucun mock de ce fichier n'écrit cette forme à la main : la faire passer
    toute par ici est ce qui empêche quatre mocks de dériver l'un de l'autre —
    et le jour où oto-core change de forme, il y a UNE ligne à changer, qui fera
    tomber tout ce qui en dépend au lieu de rien.
    """
    return {"results": list(results), "missing_ids": list(missing_ids)}


def _spec_du_client() -> list[str]:
    """Les noms de méthodes auxquels le mock est CONFORMÉ.

    Un `MagicMock()` nu répond à tout : `inst.get_lst.return_value = …` passerait
    sans un mot, et le mock ne pourrait alors RIEN réfuter — c'est la moitié de
    ce qui a laissé passer la divergence de forme réparée par ce lot.

    Le spec est l'union de deux ensembles, et la répartition est délibérée :

    - **la vraie classe `HubSpotClient`** — ce qu'oto-core sait faire ;
    - **les méthodes que `oto_mcp/tools/hubspot.py` appelle sur son client**,
      relues à l'AST par la sonde version-skew elle-même.

    Le second terme est là parce qu'un `dir(HubSpotClient)` SEUL couplerait ce
    fichier au core INSTALLÉ, pas au core épinglé : un venv en retard (le cas
    ordinaire en local, cf. `docs/commands.md` §Pin oto-core) retirerait du spec
    des méthodes qui existent bel et bien sur le tag, et ces tests deviendraient
    rouges pour une raison d'environnement — un garde-fou qui crie à tort finit
    ignoré. La question « ces méthodes existent-elles sur le PIN ? » a déjà son
    propriétaire, `tests/test_tools_client_methods_exist.py`, qui la tranche
    statiquement contre le tag ; la poser deux fois n'ajouterait rien et
    brouillerait la lecture des rouges.

    Ce qui reste donc gardé ICI : un mock qui configure une méthode que ni la
    classe ni le tool ne connaissent — une faute de frappe dans ce fichier, ou
    un mock survivant à une méthode disparue des deux côtés.
    """
    import ast
    import importlib.util
    from pathlib import Path

    from oto.tools.hubspot.client import HubSpotClient

    sonde = Path(__file__).with_name("test_tools_client_methods_exist.py")
    spec = importlib.util.spec_from_file_location("_sonde_skew", sonde)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    outil = Path(__file__).resolve().parent.parent / "oto_mcp" / "tools" / "hubspot.py"
    appelees = mod._methods_called_on_client(ast.parse(outil.read_text()))

    return sorted({n for n in dir(HubSpotClient) if not n.startswith("_")}
                  | appelees)


@pytest.fixture
def client(monkeypatch):
    import oto.tools.hubspot.client as hs

    inst = MagicMock(spec=_spec_du_client())
    inst.get_list_memberships.return_value = {
        "results": [{"recordId": "1", "membershipTimestamp": "t1"},
                    {"recordId": "2", "membershipTimestamp": "t2"}],
        "paging": {"next": {"after": "cur"}},
        "total": 2,
    }
    # forme réelle de GET /crm/v3/lists/{listId} : la liste est enveloppée
    inst.get_list.return_value = {
        "list": {"listId": "9", "name": "ICP France", "processingType": "MANUAL",
                 "objectTypeId": "0-1"}}
    inst.batch_read_objects.return_value = _enveloppe([
        {"id": "1", "properties": {"email": "a@b.test"}},
        {"id": "2", "properties": {"email": "d@e.test"}},
    ])
    monkeypatch.setattr(hs, "HubSpotClient", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.auth.hubspot.bearer", lambda *a, **k: "k")
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _described(name: str) -> str:
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    return asyncio.run(m.get_tool(name)).description or ""


# --- la FORME du retour d'oto-core ----------------------------------------------

def test_the_client_mock_is_conformed_to_the_real_class(client):
    """Sans `spec=`, le mock répond à tout et ne peut donc rien réfuter."""
    with pytest.raises(AttributeError):
        client.get_lst  # faute de frappe : le mock doit la refuser


def test_the_batch_read_envelope_is_consumed_as_oto_core_serves_it(client):
    """oto-core rend `{"results": [...], "missing_ids": [...]}`, PAS une liste.

    Le seul test qui exerce l'enveloppe COMPLÈTE — résultats partiels ET verdict
    du client — de bout en bout, de la valeur rendue par le client à la valeur
    servie à l'agent.
    """
    client.batch_read_objects.return_value = _enveloppe(
        [{"id": "2", "properties": {"email": "d@e.test"}}], missing_ids=["1"])

    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    assert [r["recordId"] for r in out["results"]] == ["1", "2"]
    assert out["results"][0]["properties"] is None
    assert "missing" in out["results"][0]
    assert out["results"][1]["properties"] == {"email": "d@e.test"}
    assert out["missing_ids"] == ["1"]
    assert out["missing_count"] == 1
    assert "missing_mismatch" not in out


def test_a_bare_list_from_the_client_is_refused_by_name(client):
    """La forme de l'ANCIEN client (une liste nue) doit lever un refus NOMMÉ.

    C'est le garde-fou qui survit au mock : si le pin oto-core repart en arrière,
    l'appelant reçoit une phrase qui dit quoi faire, pas
    `AttributeError: 'str' object has no attribute 'get'` levé cinq frames plus
    bas dans une compréhension de dict.
    """
    client.batch_read_objects.return_value = [
        {"id": "1", "properties": {"email": "a@b.test"}}]

    with pytest.raises(TypeError, match="batch_read_objects"):
        _tool("hubspot_list")(op="members", list_id="9", properties=["email"])


def test_the_clients_missing_ids_is_surfaced_not_re_derived(client):
    """Le relevé du client est SERVI tel quel — le jeter le rendrait indérivable.

    HubSpot ne nomme pas les absents de son batch read ; ce relevé n'existe que
    parce que le client compare demandé et rendu. Le remplacer par un comptage
    local, c'est perdre le seul énoncé qui vient de l'amont.
    """
    client.get_list_memberships.return_value = {
        "results": [{"recordId": "1"}, {"recordId": "2"}, {"recordId": "3"}]}
    client.batch_read_objects.return_value = _enveloppe(
        [{"id": "2", "properties": {}}], missing_ids=["1", "3"])

    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    assert out["missing_ids"] == ["1", "3"]
    assert out["missing_count"] == 2


def test_no_missing_key_when_the_client_reports_nothing_missing(client):
    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])
    assert "missing_ids" not in out
    assert "missing_count" not in out
    assert "missing_mismatch" not in out


def test_the_two_verdicts_disagreeing_is_named_not_silently_reconciled(client):
    """Le client dit « il manque 3 », la jointure dit « il manque 1 ».

    Fondre les deux en un chiffre choisirait un gagnant en silence. On sert les
    deux et on nomme le désaccord : c'est ce qui rend la dérive future visible.
    """
    client.batch_read_objects.return_value = _enveloppe(
        [{"id": "2", "properties": {"email": "d@e.test"}}], missing_ids=["3"])

    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    assert out["missing_ids"] == ["3"]
    assert out["missing_count"] == 1
    assert out["missing_mismatch"]["reported_by_client"] == ["3"]
    assert out["missing_mismatch"]["absent_from_join"] == ["1"]


def test_a_disagreement_the_client_cannot_see_is_named_too():
    """L'AUTRE sens : la jointure voit une absence dont le client n'a rien dit.

    Une appartenance sans `recordId` n'est jamais demandée au batch read — son
    id ne peut donc PAS figurer dans `missing_ids`. Ne comparer les deux
    ensembles que lorsque le client a parlé (`if missing_ids and …`) servait
    alors `missing_count: 1` tout seul : un chiffre sans nom, assez visible pour
    inquiéter et trop muet pour agir. Le désaccord se lit dans les deux sens.
    """
    from oto_mcp.tools.hubspot import _missing_report

    rows = [{"recordId": None, "missing": "record not returned by the batch read"}]

    out = _missing_report(rows, [])

    assert out["missing_count"] == 1
    assert "missing_ids" not in out          # le client n'a rien à dire
    assert out["missing_mismatch"]["reported_by_client"] == []
    assert out["missing_mismatch"]["absent_from_join"] == ["None"]


def test_the_two_verdicts_agreeing_stays_silent():
    """La symétrie ne doit pas se payer d'un bruit permanent : quand les deux
    ensembles coïncident, aucun désaccord n'est servi."""
    from oto_mcp.tools.hubspot import _missing_report

    rows = [{"recordId": "1", "missing": "…"}, {"recordId": "2", "properties": {}}]

    out = _missing_report(rows, ["1"])

    assert out == {"missing_ids": ["1"], "missing_count": 1}


def test_the_verdicts_are_compared_as_strings_not_as_types():
    """HubSpot rend l'id en chaîne, une appartenance peut porter un entier :
    comparer sans normaliser inventerait un désaccord à chaque page."""
    from oto_mcp.tools.hubspot import _missing_report

    rows = [{"recordId": 7, "missing": "…"}]

    assert _missing_report(rows, [7]) == {"missing_ids": [7], "missing_count": 1}


# --- le chemin historique reste OCTET pour OCTET --------------------------------

def test_members_without_properties_still_returns_ids_only(client):
    """Le contrat déjà servi : un appel, sa réponse RENDUE TELLE QUELLE.

    `is` et pas `==` : une égalité passerait encore si le tool reconstruisait
    un dict équivalent — et c'est justement la reconstruction qui, un lot plus
    tard, se met à ajouter ou à renommer une clé.
    """
    attendu = client.get_list_memberships.return_value
    out = _tool("hubspot_list")(op="members", list_id="9", limit=50, after="cur")

    assert out is attendu
    assert client.get_list_memberships.call_args.args == ("9",)
    assert client.get_list_memberships.call_args.kwargs == {
        "limit": 50, "after": "cur"}
    client.batch_read_objects.assert_not_called()
    client.get_list.assert_not_called()


def test_members_without_properties_makes_exactly_one_call(client):
    _tool("hubspot_list")(op="members", list_id="9")
    assert len(client.mock_calls) == 1


# --- la composition -------------------------------------------------------------

def test_members_with_properties_composes_memberships_then_batch_read(client):
    _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    client.get_list_memberships.assert_called_once()
    client.batch_read_objects.assert_called_once()
    assert client.batch_read_objects.call_args.args[1] == ["1", "2"]


def test_the_projection_is_forwarded_to_the_batch_read(client):
    _tool("hubspot_list")(op="members", list_id="9",
                          properties=["email", "firstname"])
    assert client.batch_read_objects.call_args.kwargs["properties"] == [
        "email", "firstname"]


def test_rows_keep_the_hubspot_membership_keys(client):
    """`recordId` n'est PAS renommé en `id` : une procédure qui le lit déjà
    continue de marcher le jour où elle passe `properties`."""
    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    assert out["results"] == [
        {"recordId": "1", "membershipTimestamp": "t1",
         "properties": {"email": "a@b.test"}},
        {"recordId": "2", "membershipTimestamp": "t2",
         "properties": {"email": "d@e.test"}},
    ]


def test_paging_and_total_survive_verbatim(client):
    """C'est sur `paging` que la procédure boucle : le déplacer casse en silence."""
    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])
    assert out["paging"] == {"next": {"after": "cur"}}
    assert out["total"] == 2


def test_chunking_is_the_clients_job_not_the_tool(client):
    """HubSpot plafonne le batch à 100 ; découper est au CLIENT (un découpeur de
    plus ici serait un miroir que rien ne relie, et qui dériverait)."""
    client.get_list_memberships.return_value = {
        "results": [{"recordId": str(i), "membershipTimestamp": "t"}
                    for i in range(250)]}
    client.batch_read_objects.return_value = _enveloppe(
        [{"id": str(i), "properties": {"email": f"{i}@x.test"}}
         for i in range(250)])

    out = _tool("hubspot_list")(op="members", list_id="9", limit=250,
                                properties=["email"])

    client.batch_read_objects.assert_called_once()
    assert len(client.batch_read_objects.call_args.args[1]) == 250
    assert len(out["results"]) == 250


# --- ne JAMAIS perdre un membre -------------------------------------------------

def test_a_member_whose_record_is_missing_is_a_named_row_not_a_dropped_one(client):
    client.batch_read_objects.return_value = _enveloppe(
        [{"id": "2", "properties": {"email": "d@e.test"}}], missing_ids=["1"])

    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    assert [r["recordId"] for r in out["results"]] == ["1", "2"]
    assert out["results"][0]["properties"] is None
    assert "missing" in out["results"][0]
    assert out["results"][1]["properties"] == {"email": "d@e.test"}


def test_missing_count_appears_only_when_something_is_missing(client):
    tool = _tool("hubspot_list")

    assert "missing_count" not in tool(op="members", list_id="9",
                                       properties=["email"])

    client.batch_read_objects.return_value = _enveloppe(
        [{"id": "2", "properties": {"email": "d@e.test"}}], missing_ids=["1"])
    assert tool(op="members", list_id="9", properties=["email"])["missing_count"] == 1


def test_a_requested_property_absent_from_the_record_is_named(client):
    """Sinon un nom interne mal orthographié se lit comme une colonne vide."""
    out = _tool("hubspot_list")(op="members", list_id="9",
                                properties=["email", "firstnme"])
    assert out["results"][0]["missing_properties"] == ["firstnme"]
    assert "missing_properties" not in _tool("hubspot_list")(
        op="members", list_id="9", properties=["email"])["results"][0]


# --- le type d'objet : dérivé, jamais deviné ------------------------------------

def test_the_object_type_is_derived_from_the_list_when_not_given(client):
    client.get_list.return_value = {"list": {"listId": "9", "objectTypeId": "0-2"}}

    _tool("hubspot_list")(op="members", list_id="9", properties=["name"])

    client.get_list.assert_called_once_with("9")
    assert client.batch_read_objects.call_args.args[0] == "companies"


def test_an_explicit_object_type_skips_the_extra_read(client):
    _tool("hubspot_list")(op="members", list_id="9", object_type="Deals",
                          properties=["dealstage"])

    client.get_list.assert_not_called()
    assert client.batch_read_objects.call_args.args[0] == "deals"
    assert len(client.mock_calls) == 2


def test_a_custom_object_type_id_is_passed_through(client):
    client.get_list.return_value = {"list": {"listId": "9", "objectTypeId": "2-7"}}
    _tool("hubspot_list")(op="members", list_id="9", properties=["x"])
    assert client.batch_read_objects.call_args.args[0] == "2-7"


def test_a_list_without_a_readable_object_type_is_refused_not_guessed(client):
    """Retomber sur « contacts » lirait le mauvais objet et rendrait une
    population plausible et fausse — le pire mode d'échec disponible."""
    client.get_list.return_value = {"list": {"listId": "9", "name": "L"}}

    with pytest.raises(McpError, match="object_type"):
        _tool("hubspot_list")(op="members", list_id="9", properties=["email"])
    client.batch_read_objects.assert_not_called()


def test_the_object_type_used_is_reported_in_the_answer(client):
    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])
    assert out["object_type"] == "contacts"


def test_an_unknown_object_type_is_refused(client):
    with pytest.raises(McpError, match="object_type"):
        _tool("hubspot_list")(op="members", list_id="9", object_type="prospects",
                              properties=["email"])
    client.batch_read_objects.assert_not_called()


# --- refus ----------------------------------------------------------------------

@pytest.mark.parametrize("value", [{"email": "x"}, "email", [1, 2]])
def test_properties_must_be_a_list_of_names(client, value):
    with pytest.raises(McpError, match="properties"):
        _tool("hubspot_list")(op="members", list_id="9", properties=value)
    client.get_list_memberships.assert_not_called()
    client.batch_read_objects.assert_not_called()


def test_an_empty_properties_list_is_refused_by_name(client):
    """`properties=[]` demande zéro colonne, et HubSpot répondrait ses colonnes
    PAR DÉFAUT : trois appels pour ce que personne n'a demandé. Omettre
    l'argument et le remplir ont déjà chacun leur sens ; la troisième forme se
    refuse plutôt que de se deviner."""
    with pytest.raises(McpError, match="NON-EMPTY"):
        _tool("hubspot_list")(op="members", list_id="9", properties=[])

    client.get_list_memberships.assert_not_called()
    client.get_list.assert_not_called()
    client.batch_read_objects.assert_not_called()


@pytest.mark.parametrize("op,kwargs,method", [
    ("get", {"list_id": "9"}, "get_list"),
    ("add_members", {"list_id": "9", "record_ids": ["1"]}, "add_list_memberships"),
    ("clear_members", {"list_id": "9"}, "delete_all_list_memberships"),
    ("copy_from", {"list_id": "9", "source_list_id": "8"},
     "add_memberships_from_list"),
])
def test_properties_on_a_non_members_op_is_refused(client, op, kwargs, method):
    """Ignorer l'argument serait la divergence MUETTE que le dépôt refuse :
    l'appelant croirait avoir demandé des colonnes."""
    with pytest.raises(McpError, match="members"):
        _tool("hubspot_list")(op=op, properties=["email"], **kwargs)
    getattr(client, method).assert_not_called()


def test_an_empty_page_does_not_call_the_batch_read(client):
    """`POST /batch/read` avec `inputs: []` est un 400 chez HubSpot."""
    client.get_list_memberships.return_value = {"results": [], "total": 0}

    out = _tool("hubspot_list")(op="members", list_id="9", properties=["email"])

    client.batch_read_objects.assert_not_called()
    assert out["results"] == []
    assert out["total"] == 0
    assert "missing_ids" not in out


# --- la surface SERVIE ----------------------------------------------------------

def test_the_new_arguments_are_documented_in_args():
    """Un paramètre sans entrée `Args:` est servi avec un schéma nu."""
    src = _tool("hubspot_list").__doc__ or ""
    args = src.split("Args:", 1)[1]
    assert "properties:" in args
    assert "object_type:" in args


def test_the_empty_list_refusal_is_documented_where_it_is_read():
    """Un refus non écrit dans `Args:` se découvre par l'échec, en production."""
    args = (_tool("hubspot_list").__doc__ or "").split("Args:", 1)[1]
    assert "EMPTY list is refused" in args


def test_the_fast_path_is_advertised_before_the_args_block():
    """FastMCP tronque la `description` au bloc `Args:` : la phrase qui fait
    choisir 2 appels plutôt que 101 doit vivre AVANT, sinon l'agent ne la voit
    jamais et refait le N+1."""
    servie = _described("hubspot_list")
    assert "Args:" not in servie
    tete = servie.split('- **"add_members"**', 1)[0]
    assert "properties" in tete
    assert "190 requests per" in tete


# --- l'enveloppe et le recollage, exercés directement ----------------------------

def test_the_envelope_reader_refuses_a_list_by_name():
    from oto_mcp.tools.hubspot import _batch_read_envelope

    with pytest.raises(TypeError, match="batch_read_objects"):
        _batch_read_envelope([{"id": "1"}])


@pytest.mark.parametrize("lecture, absente", [
    ({"results": [{"id": "1"}]}, "missing_ids"),
    ({"results": [{"id": "1"}], "missing": ["2"]}, "missing_ids"),   # renommage
    ({"missing_ids": []}, "results"),
    ({"results": [{"id": "1"}], "missing_ids": None}, "missing_ids"),
])
def test_the_envelope_reader_refuses_a_mapping_missing_a_contract_key(lecture, absente):
    """Un mapping qui n'a PAS les deux clés est refusé, nommément.

    C'est le cas dangereux, et il ne ressemble pas à une panne : lire la clé
    avec un défaut (`.get("missing_ids") or []`) laisserait un renommage côté
    oto-core passer en SILENCE — la page servie perdrait son relevé d'écart,
    donc une page de 250 membres revenue à 247 s'annoncerait complète. Le refus
    est bruyant, la divergence est muette (`docs/conventions.md`) : une dérive
    inter-dépôts doit tomber ici, avec le nom de la clé qui manque.

    `missing_ids: None` est logé au même endroit : le contrat dit « toujours
    présente, jamais None », et un None rétrécirait la page tout autant.
    """
    from oto_mcp.tools.hubspot import _batch_read_envelope

    with pytest.raises(TypeError, match=absente):
        _batch_read_envelope(lecture)


def test_the_envelope_reader_accepts_the_empty_envelope():
    """Vide n'est pas absent : `{"results": [], "missing_ids": []}` est ce que le
    client rend quand il n'y a rien à lire, et il doit passer."""
    from oto_mcp.tools.hubspot import _batch_read_envelope

    assert _batch_read_envelope({"results": [], "missing_ids": []}) == ([], [])


def test_rows_from_memberships_refuses_the_envelope_itself():
    """Le helper prend la LISTE `results`, pas l'enveloppe. Lui passer le dict
    l'itérerait sur ses CLÉS — `'str' object has no attribute 'get'`, cinq
    frames plus bas. Il refuse, et il dit quoi."""
    from oto_mcp.tools.hubspot import _rows_from_memberships

    with pytest.raises(TypeError, match="_rows_from_memberships"):
        _rows_from_memberships([{"recordId": "1"}],
                               {"results": [], "missing_ids": []})


def test_rows_from_memberships_keeps_the_page_order():
    from oto_mcp.tools.hubspot import _rows_from_memberships

    membres = [{"recordId": "3"}, {"recordId": "1"}, {"recordId": "2"}]
    records = [{"id": "1"}, {"id": "2"}, {"id": "3"}]  # HubSpot ne garantit rien

    rows = _rows_from_memberships(membres, records)
    assert [r["recordId"] for r in rows] == ["3", "1", "2"]


def test_rows_from_memberships_matches_on_id_not_on_position():
    from oto_mcp.tools.hubspot import _rows_from_memberships

    membres = [{"recordId": "1"}, {"recordId": "2"}]
    records = [{"id": "2", "properties": {"email": "deux@x.test"}}]

    rows = _rows_from_memberships(membres, records, ["email"])
    assert rows[0]["properties"] is None
    assert rows[1]["properties"] == {"email": "deux@x.test"}


def test_rows_from_memberships_stringifies_the_record_id():
    """HubSpot rend l'id en chaîne côté objet et l'appartenance peut, elle,
    porter un entier : comparer sans normaliser perdrait TOUTE la page."""
    from oto_mcp.tools.hubspot import _rows_from_memberships

    rows = _rows_from_memberships([{"recordId": 1}],
                                  [{"id": "1", "properties": {"a": "b"}}])
    assert rows[0]["properties"] == {"a": "b"}


def test_the_missing_report_is_empty_when_nothing_is_missing():
    from oto_mcp.tools.hubspot import _missing_report

    assert _missing_report([{"recordId": "1", "properties": {}}], []) == {}


# --- LE CONTRAT INTER-DÉPÔTS, exercé sur le VRAI client -------------------------
#
# Tout ce qui précède mocke `batch_read_objects`. Un mock ne peut pas réfuter la
# forme qu'il écrit lui-même : `_enveloppe()` centralise cette forme, donc les
# mocks ne peuvent plus diverger ENTRE EUX — mais rien ne les rattache à oto-core.
# Le jour où le client renomme `missing_ids`, tous les tests ci-dessus restent
# verts et la prod perd son relevé d'écart. C'est la panne exacte de ce lot,
# reprise un cran plus loin.
#
# Ces deux tests-ci ne mockent donc PAS le client : ils l'importent, ne
# remplacent que le TRANSPORT (`requests.Session.request`), laissent le vrai
# `batch_read_objects` fabriquer sa valeur et la font entrer telle quelle dans
# les fonctions du tool. Un changement de forme côté oto-core tombe ici.
#
# ⚠️ **Ce que ces tests font dans un venv en retard sur le pin.** Ils portent
# `exige_pin_oto_core` : quand l'écart venv↔pin est MESURABLE (installation git,
# `direct_url.json`), `tests/_oto_core_pin.py` les rend non concluants en local
# et mordants en CI, qui installe AU tag. Quand il ne l'est pas — un venv dont
# les métadonnées portent le numéro gelé `1.100.0`, qui ne dit rien du tag —
# l'instrument ne peut pas se prononcer et ces tests s'exécutent : contre un
# client d'avant l'enveloppe, ils sont ROUGES, et ce rouge nomme la bonne pièce
# (« le client rend une liste »). C'est le prix assumé d'un garde-fou qui refuse
# de se taire sur une mesure qu'il a réellement faite.


class _Reponse:
    """Le strict nécessaire pour qu'oto-core lise une réponse HubSpot.

    Écrite ici plutôt que `MagicMock` : ce qu'on veut prouver est que le vrai
    client SAIT LIRE un corps HubSpot réel, et un mock qui répond à tout ne
    prouverait rien de plus que le mock du haut de ce fichier.
    """

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.headers: dict = {}

    def json(self) -> dict:
        return self._body


@pytest.fixture
def transport_hubspot(monkeypatch):
    """Le VRAI client, dont seul le transport HTTP est remplacé.

    HubSpot répond **207** à un batch read partiel : les enregistrements trouvés
    dans `results`, et RIEN qui nomme les absents — c'est ce silence qui rend
    `missing_ids` nécessaire, et le client est le seul endroit d'où il peut
    sortir. On retient donc l'id `"3"`, sans le mentionner nulle part dans le
    corps, et on exige de le retrouver nommé dans l'enveloppe.
    """
    import oto.tools.hubspot.client as hs

    appels: list = []

    def _faux(self, method, url, **kw):
        demandes = [e["id"] for e in (kw.get("json") or {}).get("inputs") or []]
        appels.append((method, url, demandes))
        return _Reponse(207, {
            "results": [{"id": i, "properties": {"email": f"{i}@x.test"}}
                        for i in demandes if i != RETENU],
            "numErrors": 1,
        })

    monkeypatch.setattr(hs.requests.Session, "request", _faux)
    return appels


#: L'id que l'amont simulé ne rend PAS, et qu'aucun corps de réponse ne nomme.
RETENU = "3"


@pytest.mark.exige_pin_oto_core
@pytest.mark.skipif(not hasattr(HubSpotClient, "batch_read_objects"),
                    reason="`batch_read_objects` absent du core installé : la "
                           "forme n'est pas mesurable ici (cf. le pin)")
def test_the_real_client_returns_the_envelope_this_tool_reads(transport_hubspot):
    """La forme d'oto-core, lue sur oto-core — pas sur `_enveloppe()`.

    `_enveloppe()` est une COPIE du contrat écrite à la main. Ce test la rend
    vérifiée au lieu de déclarée : il exige les DEUX clés, et rien d'autre, sur
    la valeur que le vrai `batch_read_objects` construit.

    L'égalité d'ensembles est délibérément stricte dans les deux sens. Un
    `in` laisserait passer un renommage accompagné d'un alias (les deux clés
    coexisteraient, le test resterait vert, et l'ancienne finirait par
    disparaître un tag plus tard, silencieusement).
    """
    from oto_mcp.tools.hubspot import _CLES_ENVELOPPE

    lu = HubSpotClient(api_key="pat-test").batch_read_objects(
        "contacts", ["1", "2", RETENU], properties=["email"])

    assert isinstance(lu, dict), (
        f"batch_read_objects rend {type(lu).__name__} : c'est la forme d'AVANT "
        "l'enveloppe. `_batch_read_envelope` la refuse — bumper le pin oto-core "
        "(pyproject.toml) et réinstaller le venv.")
    assert set(lu) == set(_CLES_ENVELOPPE)
    assert RETENU in [str(i) for i in lu["missing_ids"]], (
        "HubSpot répond 207 sans nommer les absents : si le client ne les "
        "calcule plus, personne d'autre ne le peut.")
    assert [str(r["id"]) for r in lu["results"]] == ["1", "2"]
    assert len(transport_hubspot) == 1, "une tranche = un appel HTTP"


@pytest.mark.exige_pin_oto_core
@pytest.mark.skipif(not hasattr(HubSpotClient, "batch_read_objects"),
                    reason="`batch_read_objects` absent du core installé : la "
                           "forme n'est pas mesurable ici (cf. le pin)")
def test_the_real_clients_value_feeds_the_tool_helpers_unmodified(transport_hubspot):
    """Le raccord, bout à bout : la valeur du VRAI client entre telle quelle.

    C'est la séquence exacte du site d'appel `op='members'`, sans une ligne de
    massage entre les deux repos — parce qu'un massage dans le test serait très
    précisément l'endroit où la divergence se cacherait.

    La ligne retenue survit NOMMÉE (`properties: None` + `missing`) et le relevé
    servi porte l'id, pas seulement un compte : dans une population de
    prospection, une ligne muette est pire qu'un refus.
    """
    from oto_mcp.tools.hubspot import (_batch_read_envelope, _missing_report,
                                       _rows_from_memberships)

    membres = [{"recordId": "1"}, {"recordId": "2"}, {"recordId": RETENU}]
    lu = HubSpotClient(api_key="pat-test").batch_read_objects(
        "contacts", [m["recordId"] for m in membres], properties=["email"])

    records, absents = _batch_read_envelope(lu)
    rows = _rows_from_memberships(membres, records, ["email"])
    releve = _missing_report(rows, absents)

    assert [r["recordId"] for r in rows] == ["1", "2", RETENU]
    assert rows[0]["properties"] == {"email": "1@x.test"}
    assert rows[2]["properties"] is None
    assert "missing" in rows[2]
    assert releve == {"missing_ids": [RETENU], "missing_count": 1}
