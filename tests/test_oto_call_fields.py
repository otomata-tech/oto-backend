"""`oto_call fields=` — le filtrage de réponse côté serveur, pour TOUT outil dispatché.

Un outil qui rend 50 champs par ligne quand l'agent en lit 5 fait payer les 45 autres à
chaque appel. `fields` réduit la réponse AVANT qu'elle n'arrive à l'agent, sans câbler
l'outil cible : le tri vit dans `output_projection.select_fields`, appliqué par `oto_call`
APRÈS la rédaction. On vérifie : (1) où porte le tri sans rien savoir de l'outil, (2) que
ce qui est écarté ou absent est NOMMÉ, (3) qu'un champ masqué par la politique de l'org
ne revient pas par `fields`, (4) qu'une sélection vide est refusée AVANT l'exécution.
"""
import asyncio
import json

import pytest
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from oto.tools.common import FieldFilter

from oto_mcp import redaction
from oto_mcp.field_filter_defaults import _CANDIDATE_PII
from oto_mcp.mcp_errors import McpError
from oto_mcp.output_projection import select_fields
from _mcp_app import static_mcp

mcp = static_mcp()


# --- 1. select_fields, pur ---------------------------------------------------

_SALARIES = {
    "total": 2,
    "next_cursor": "abc",
    "data": {"employees": [
        {"name": "A", "email": "a@example.com", "ssn": "1", "salary": 10},
        {"name": "B", "email": "b@example.com", "ssn": "2", "salary": 20},
    ]},
}


def test_une_liste_de_lignes_garde_les_champs_et_l_enveloppe():
    out, notice = select_fields(_SALARIES, ["name", "email"])
    assert out["data"]["employees"] == [
        {"name": "A", "email": "a@example.com"},
        {"name": "B", "email": "b@example.com"},
    ]
    # l'enveloppe reste : sans `total` ni curseur, l'agent croirait avoir tout vu
    assert out["total"] == 2 and out["next_cursor"] == "abc"
    assert notice["items_path"] == "data.employees"
    assert notice["omitted"] == ["salary", "ssn"]
    assert "missing" not in notice


def test_le_payload_d_origine_n_est_pas_mute():
    avant = json.dumps(_SALARIES, sort_keys=True)
    select_fields(_SALARIES, ["name"])
    assert json.dumps(_SALARIES, sort_keys=True) == avant


def test_un_champ_demande_absent_partout_est_nomme():
    _, notice = select_fields(_SALARIES, ["name", "phone"])
    assert notice["missing"] == ["phone"]


def test_un_objet_unique_garde_ses_seules_cles_demandees():
    out, notice = select_fields({"name": "A", "ssn": "1", "salary": 10}, ["name"])
    assert out == {"name": "A"}
    assert notice["omitted"] == ["salary", "ssn"]


def test_une_liste_au_sommet_est_triee():
    out, notice = select_fields([{"name": "A", "ssn": "1"}, "brut"], ["name"])
    assert out == [{"name": "A"}, "brut"]
    assert notice["omitted"] == ["ssn"]


def test_une_liste_vide_garde_la_forme_de_la_reponse():
    # lue comme un objet unique, `{"results": [], "total": 0}` perdrait `total`
    out, notice = select_fields({"results": [], "total": 0}, ["name"])
    assert out == {"results": [], "total": 0}
    assert notice is None


def test_plusieurs_listes_ne_trient_rien_et_nomment_les_chemins():
    payload = {"people": [{"name": "A", "x": 1}], "companies": [{"name": "C", "y": 2}]}
    out, notice = select_fields(payload, ["name"])
    assert out == payload
    assert notice["ambiguous"] == ["people", "companies"]


def test_etoile_rend_le_brut():
    assert select_fields(_SALARIES, ["*"]) == (_SALARIES, None)


def test_rien_d_ecarte_pas_de_notice():
    out, notice = select_fields({"name": "A"}, ["name"])
    assert out == {"name": "A"} and notice is None


# --- 2. par oto_call --------------------------------------------------------

class _FakeTool:
    def __init__(self, name, result):
        self.name = name
        self.description = f"fake {name}"
        self.parameters = {"type": "object", "properties": {}}
        self.output_schema = None
        self._result = result
        self.runs = 0

    async def run(self, arguments):
        self.runs += 1
        return self._result


class _FakeFastMCP:
    transforms: list = []

    def __init__(self, tools):
        self._tools = tools

    async def _list_tools(self):
        return self._tools


class _FakeCtx:
    def __init__(self, tools):
        self.fastmcp = _FakeFastMCP(tools)


def _tool_result(payload) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                      structured_content=payload if isinstance(payload, dict) else None)


@pytest.fixture
def oto_call_fn():
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return next(t.fn for t in tools if t.name == "oto_call")


@pytest.fixture
def sans_politique(monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())


def _call(fn, tools, **kw):
    return asyncio.run(fn(ctx=_FakeCtx(tools), **kw))


def test_oto_call_filtre_les_deux_canaux(oto_call_fn, sans_politique):
    cible = _FakeTool("fr_ccn_search", _tool_result(_SALARIES))
    out = _call(oto_call_fn, [cible], name="fr_ccn_search", arguments={},
                fields=["name"])
    for vue in (out.structured_content, json.loads(out.content[0].text)):
        assert vue["data"]["employees"] == [{"name": "A"}, {"name": "B"}]
        assert vue["selection"]["omitted"] == ["email", "salary", "ssn"]


def test_oto_call_sans_fields_rend_le_resultat_intact(oto_call_fn, sans_politique):
    resultat = _tool_result(_SALARIES)
    cible = _FakeTool("fr_ccn_search", resultat)
    out = _call(oto_call_fn, [cible], name="fr_ccn_search", arguments={})
    assert out is resultat


def test_la_notice_n_ecrase_pas_un_champ_selection_de_la_cible(oto_call_fn,
                                                               sans_politique):
    payload = {"selection": "de la cible", "name": "A", "ssn": "1"}
    cible = _FakeTool("fr_ccn_search", _tool_result(payload))
    out = _call(oto_call_fn, [cible], name="fr_ccn_search", arguments={},
                fields=["name", "selection"])
    assert out.structured_content["selection"] == "de la cible"
    assert out.structured_content["oto_call_selection"]["omitted"] == ["ssn"]


def test_un_texte_libre_repart_tel_quel(oto_call_fn, sans_politique):
    resultat = ToolResult(content=[TextContent(type="text", text="pas du JSON")])
    cible = _FakeTool("fr_ccn_search", resultat)
    out = _call(oto_call_fn, [cible], name="fr_ccn_search", arguments={},
                fields=["name"])
    assert out is resultat


def test_un_champ_masque_par_la_politique_ne_revient_pas_par_fields(oto_call_fn,
                                                                     monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter",
                        lambda _s: FieldFilter(rules=_CANDIDATE_PII))
    profil = {"first_name": "Jane", "last_name": "Doe",
              "photo_url": "https://example.com/jane.jpg", "headline": "Head of Talent"}
    cible = _FakeTool("unipile_profile", _tool_result(profil))
    out = _call(oto_call_fn, [cible], name="unipile_profile", arguments={},
                fields=["photo_url", "last_name", "headline"])
    sc = out.structured_content
    assert "photo_url" not in sc                 # supprimé par la politique, pas rendu
    assert sc["last_name"] != "Doe"              # masqué AVANT le tri, il le reste
    assert sc["headline"] == "Head of Talent"
    assert "Doe" not in out.content[0].text


@pytest.mark.parametrize("fields", [[], [""], ["name", 3]])
def test_une_selection_vide_ou_invalide_est_refusee_avant_l_appel(oto_call_fn,
                                                                  sans_politique, fields):
    cible = _FakeTool("fr_ccn_search", _tool_result(_SALARIES))
    with pytest.raises(McpError) as ei:
        _call(oto_call_fn, [cible], name="fr_ccn_search", arguments={}, fields=fields)
    assert "fields" in str(ei.value)
    assert cible.runs == 0                       # aucun appel facturé pour rien
