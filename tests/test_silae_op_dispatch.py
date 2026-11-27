"""Dispatch `op=` des tools `silae_*` (ADR 0047 §Amendement) — `tools/silae.py` et
`tools/silae_documents.py`.

Ce que ce fichier verrouille : la SURFACE. Une op mal câblée appelle silencieusement la
mauvaise méthode du client, et rien ne casse au boot. Les corps de requête (routes,
enveloppes, format des périodes) sont éprouvés côté client, dans oto-core
(`tests/test_silae_client.py`) : ici, le client est un faux.

Quatre familles de garanties :
  1. chaque op → la méthode client attendue, avec les arguments attendus (les
     sélecteurs d'un bulletin passent par mot-clé : deux chaînes côte à côte
     s'inversent sans erreur de typage) ;
  2. les refus : op inconnue nommant les ops valides, argument obligatoire nommant l'op
     ET l'argument, argument fourni mais non utilisé par l'op (le mode de panne propre à
     `op=` : un résultat crédible à côté de la demande) ;
  3. les documents : rendus par `file_content.render_for_agent` (jamais de base64 dans
     le contexte), REFUSÉS sans appel amont tant que la politique de l'org masque un
     champ ;
  4. **le mutisme des écritures DES LECTEURS** : `SilaeClient` porte 4 méthodes qui
     MODIFIENT la paie. Seul `silae_act` les sert (`tests/test_silae_act.py`) — ici on joue
     toutes les ops des lecteurs et on exige qu'aucune ne soit touchée, plus un contrôle
     statique des deux modules lecteurs.
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError

_WRITE_METHODS = ("ajouter_element_variable", "ajouter_prime", "ajouter_heures",
                  "confirmer_saisies")
_D = {"numero_dossier": "001"}
_UN = {**_D, "matricule_salarie": "0001", "identifiant_emploi": 4, "periode": "2026-05"}

# Toute la surface, op par op — sert au dispatch ET au contrôle de mutisme.
_ALL_OPS = [
    ("silae_dossier", {"op": "list"}, "list_dossiers"),
    ("silae_dossier", {"op": "numbers"}, "list_numeros_dossiers"),
    ("silae_dossier", {"op": "collective_agreements"}, "list_conventions_collectives"),
    ("silae_dossier", {"op": "current_period", **_D}, "dossier_periode_en_cours"),
    ("silae_dossier", {"op": "establishments", **_D}, "list_etablissements"),
    ("silae_dossier", {"op": "organisms", **_D}, "list_organismes"),
    ("silae_employee", {**_D, "op": "list"}, "list_salaries"),
    ("silae_employee", {**_D, "op": "get", "matricule_salarie": "0001"},
     "lecture_informations_salarie"),
    ("silae_employee", {**_D, "op": "jobs", "matricule_salarie": "0001"},
     "list_salarie_emplois"),
    ("silae_employee", {**_D, "op": "from_internal_id", "matricule_interne": "I7"},
     "matricule_depuis_interne"),
    ("silae_payslip", {**_D, "op": "pdf_ids", "periode": "2026-05"}, "bulletins_ids_pdf"),
    ("silae_payslip", {**_D, "op": "indices", "matricule_salarie": "0001",
                       "identifiant_emploi": 4, "periode": "2026-05"}, "bulletins_indices"),
    ("silae_payslip", {**_UN, "op": "header"}, "bulletin_entete"),
    ("silae_payslip", {**_UN, "op": "lines"}, "bulletin_lignes"),
    ("silae_payslip", {**_UN, "op": "details"}, "bulletin_details"),
    ("silae_payslip", {**_D, "op": "totals", "matricule_salarie": "0001",
                       "periode_debut": "2026-01", "periode_fin": "2026-12"},
     "bulletin_cumuls"),
    ("silae_variables_to_enter", {**_D}, "list_variables_a_saisir"),
    ("silae_declaration", {**_D, "op": "dsn_list", "periode": "2026-05"},
     "list_dsn_mensuelles"),
    ("silae_declaration", {**_D, "op": "states", "type_declaration": "DUE"},
     "etat_declarations"),
    ("silae_declaration", {**_D, "op": "dsn_content", "periode": "2026-05",
                           "etablissement": "SIEGE", "type_dsn": 1, "fraction": 1},
     "contenu_partiel_dsn"),
    ("silae_report", {**_D, "report": "charges_table", "periode": "2026-05"},
     "edition_tableau_charges"),
    ("silae_report", {**_D, "report": "contributions_detail", "periode": "2026-05"},
     "edition_detail_cotisations"),
    ("silae_report", {**_D, "report": "declaration_summaries", "periode": "2026-05"},
     "recap_declarations"),
    ("silae_report", {**_D, "report": "charges_table", "op": "start", "periode": "2026-05"},
     "lancer_edition_tableau_charges"),
    ("silae_report", {**_D, "report": "contributions_detail", "op": "start",
                      "periode": "2026-05"}, "lancer_edition_detail_cotisations"),
    ("silae_report", {**_D, "report": "declaration_summaries", "op": "start",
                      "periode": "2026-05"}, "lancer_recap_declarations"),
    ("silae_report", {**_D, "report": "charges_table", "op": "status", "task_id": "g"},
     "statut_edition"),
]

_BLOB = {"data": b"%PDF-1.7", "filename": "silae-x.pdf", "mimetype": "application/pdf"}


class _FF:
    def __init__(self, vide=True):
        self._vide = vide

    def is_empty(self):
        return self._vide


@pytest.fixture
def rendus(monkeypatch):
    """`render_for_agent` stubbé (pas de S3) : on capture ce qu'il reçoit."""
    vus = []

    def faux(data, filename, mime, *, sub, prefix, **kw):
        vus.append({"data": data, "filename": filename, "mime": mime, "prefix": prefix})
        return {"filename": filename, "encoding": "text", "content": "…"}

    monkeypatch.setattr("oto_mcp.file_content.render_for_agent", faux)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-test")
    return vus


@pytest.fixture
def client(monkeypatch, rendus):
    """Faux `SilaeClient` + credential résolu + politique de champs vide.

    `_client()` fait son `from oto.tools.silae import SilaeClient` À L'INTÉRIEUR de la
    fonction : patcher l'attribut du package suffit. Les méthodes à document rendent un
    blob décodé, comme le vrai client."""
    inst = MagicMock()
    for m in ("edition_tableau_charges", "edition_detail_cotisations", "recap_declarations",
              "contenu_partiel_dsn"):
        getattr(inst, m).return_value = dict(_BLOB)
    for m in ("lancer_edition_tableau_charges", "lancer_edition_detail_cotisations",
              "lancer_recap_declarations"):
        getattr(inst, m).return_value = {"guidTache": "abcd"}
    inst.statut_edition.return_value = {"statut": "ETAT_ENCOURS", "progression": 10.0,
                                        "messageErreur": None, "dureeExecution": None,
                                        "document": None}
    monkeypatch.setattr("oto.tools.silae.SilaeClient", lambda **kw: inst)
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider: {"client_id": "id", "client_secret": "sec",
                          "subscription_key": "sub"},
    )
    monkeypatch.setattr("oto_mcp.access.resolve_field_filter", lambda service: _FF())
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import silae as S, silae_documents as SD

    m = FastMCP("t")
    S.register(m)
    SD.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def test_the_surface_is_exactly_the_six_tools(client):
    """Un tool en plus (ou un ancien nom ressuscité) doit se voir ici."""
    assert sorted(t.name for t in asyncio.run(_mcp().list_tools())) == [
        "silae_declaration", "silae_dossier", "silae_employee", "silae_payslip",
        "silae_report", "silae_variables_to_enter",
    ]


@pytest.mark.parametrize("tool,kwargs,method", _ALL_OPS)
def test_every_op_routes_to_its_client_method_and_no_write_is_ever_reached(
        client, tool, kwargs, method):
    """Invariant central : chaque op appelle SA méthode, et les 4 méthodes qui modifient
    la paie restent muettes — une op mal câblée sur `ajouter_prime` ou
    `confirmer_saisies` toucherait des bulletins réels."""
    _tool(tool)(**kwargs)
    getattr(client, method).assert_called_once()
    for m in _WRITE_METHODS:
        getattr(client, m).assert_not_called()


# --- dossiers -----------------------------------------------------------------

def test_dossier_defaults_to_the_reachable_list(client):
    _tool("silae_dossier")()
    client.list_dossiers.assert_called_once_with()


def test_dossier_wide_reads_refuse_a_dossier_filter(client):
    """`silae_dossier(numero_dossier="001")` sans op rendrait la liste COMPLÈTE, et les
    conventions collectives sont rendues pour TOUS les dossiers : un filtre ignoré serait
    un résultat crédible à côté de la demande."""
    for op in ("list", "numbers", "collective_agreements"):
        with pytest.raises(McpError, match=f"op='{op}' does not use numero_dossier"):
            _tool("silae_dossier")(op=op, numero_dossier="001")
    client.list_dossiers.assert_not_called()
    client.list_conventions_collectives.assert_not_called()


def test_organisms_pass_their_filters(client):
    _tool("silae_dossier")(op="organisms", numero_dossier="001", code_nature="SSOC",
                           etablissement="SIEGE")
    client.list_organismes.assert_called_once_with(
        "001", code_organisme=None, code_nature="SSOC", etablissement="SIEGE")


# --- salariés -----------------------------------------------------------------

def test_employee_list_passes_the_activity_filters(client):
    _tool("silae_employee")(numero_dossier="001", periode="2026-05")
    client.list_salaries.assert_called_once_with(
        "001", actif_sur_periode="2026-05", actif_a_la_date=None)


def test_employee_get_reads_the_record_not_the_internal_id_translation(client):
    """L'ancien `get` appelait `MatriculeSalarie`, qui ne fait que traduire un matricule
    interne : il disait « fiche salarié » et rendait un matricule."""
    _tool("silae_employee")(numero_dossier="001", op="get", matricule_salarie="0001")
    client.lecture_informations_salarie.assert_called_once_with("001", "0001")
    client.matricule_depuis_interne.assert_not_called()


def test_employee_jobs_requires_a_matricule_and_defaults_to_current_jobs(client):
    """L'API exige le matricule (OpenAPI : `matriculeSalarie` requis) : plus de
    « matricule vide = tous les salariés »."""
    with pytest.raises(McpError, match="op='jobs' requires matricule_salarie"):
        _tool("silae_employee")(numero_dossier="001", op="jobs")
    _tool("silae_employee")(numero_dossier="001", op="jobs", matricule_salarie="0001")
    client.list_salarie_emplois.assert_called_once_with("001", "0001", 0)


def test_employee_list_refuses_arguments_it_would_ignore(client):
    with pytest.raises(McpError, match="op='list' does not use matricule_salarie"):
        _tool("silae_employee")(numero_dossier="001", matricule_salarie="0001")
    with pytest.raises(McpError, match="type_emplois"):
        _tool("silae_employee")(numero_dossier="001", op="get", matricule_salarie="0001",
                                type_emplois=1)
    client.list_salaries.assert_not_called()


# --- bulletins ----------------------------------------------------------------

def test_payslip_pdf_ids_default_to_the_whole_dossier_and_every_job(client):
    _tool("silae_payslip")(numero_dossier="001", periode="2026-05")
    client.bulletins_ids_pdf.assert_called_once_with(
        "001", periode_debut="2026-05", periode_fin="2026-05", matricule_salarie="",
        identifiant_emploi=0, originaux_seulement=None, etablissement=None)


def test_a_range_is_one_month_or_start_and_end_never_both(client):
    _tool("silae_payslip")(numero_dossier="001", op="totals", matricule_salarie="0001",
                           periode_debut="2026-01", periode_fin="2026-12")
    client.bulletin_cumuls.assert_called_once_with(
        "001", "0001", periode_debut="2026-01", periode_fin="2026-12")
    with pytest.raises(McpError, match="not both"):
        _tool("silae_payslip")(numero_dossier="001", periode="2026-05",
                               periode_debut="2026-01", periode_fin="2026-05")
    with pytest.raises(McpError, match="requires periode_fin"):
        _tool("silae_payslip")(numero_dossier="001", periode_debut="2026-01")


def test_a_single_payslip_is_addressed_by_job_and_index_by_keyword(client):
    """Les intermittents ont plusieurs bulletins par mois : `identifiant_emploi` et
    `indice_periode` sont exposés et transmis."""
    _tool("silae_payslip")(**_UN, op="header", indice_periode=2)
    client.bulletin_entete.assert_called_once_with(
        "001", "0001", identifiant_emploi=4, periode="2026-05", indice_periode=2)


@pytest.mark.parametrize("op", ["indices", "header", "lines", "details"])
def test_a_single_payslip_op_needs_the_job(client, op):
    with pytest.raises(McpError, match=f"op='{op}' requires identifiant_emploi"):
        _tool("silae_payslip")(numero_dossier="001", op=op, matricule_salarie="0001",
                               periode="2026-05")


def test_lines_switch_to_the_filtered_function_only_when_a_filter_is_given(client):
    from oto_mcp.tools.silae import LineFilters

    _tool("silae_payslip")(**_UN, op="lines")
    client.bulletin_lignes.assert_called_once()
    client.bulletin_lignes_filtrees.assert_not_called()
    _tool("silae_payslip")(**_UN, op="lines",
                           line_filters=LineFilters(zone="contributions", code_ducs="100%"))
    assert client.bulletin_lignes_filtrees.call_args.kwargs["filtres"] == {
        "zone": 3, "code_ducs": "100%"}


def test_details_maps_the_type_and_the_filters(client):
    from oto_mcp.tools.silae import LineFilters

    _tool("silae_payslip")(**_UN, op="details", type_details="lines",
                           line_filters=LineFilters(exclure_lignes_neutres=True))
    kw = client.bulletin_details.call_args.kwargs
    assert kw["type_details"] == 2 and kw["filtres"] == {"exclure_lignes_neutres": True}


def test_line_filters_refuse_an_unknown_key():
    from pydantic import ValidationError
    from oto_mcp.tools.silae import LineFilters

    with pytest.raises(ValidationError):
        LineFilters(codeDucs="100")


def test_empty_matricule_counts_as_missing_on_a_single_payslip_op(client):
    """Silae traite `""` comme « tous les salariés »."""
    with pytest.raises(McpError, match="matricule_salarie"):
        _tool("silae_payslip")(**{**_UN, "matricule_salarie": ""}, op="header")
    client.bulletin_entete.assert_not_called()


# --- documents ----------------------------------------------------------------

def test_a_report_is_rendered_for_the_agent_never_as_base64(client, rendus):
    out = _tool("silae_report")(numero_dossier="001", report="charges_table",
                                periode_debut="2026-01", periode_fin="2026-03", format="xlsx")
    client.edition_tableau_charges.assert_called_once_with(
        "001", periode_debut="2026-01", periode_fin="2026-03", format="xlsx")
    assert rendus[0]["data"] == b"%PDF-1.7" and rendus[0]["prefix"] == "silae-files"
    assert out["encoding"] == "text"


def test_contributions_detail_maps_its_options(client):
    _tool("silae_report")(numero_dossier="001", report="contributions_detail",
                          periode="2026-05", detail_salaries=True, grouper_par="monthly")
    client.edition_detail_cotisations.assert_called_once_with(
        "001", periode_debut="2026-05", periode_fin="2026-05", format="pdf",
        detail_salaries=True, grouper_par_organismes=None, grouper_par="mensuel")


def test_a_started_report_returns_its_task_and_the_status_call(client):
    out = _tool("silae_report")(numero_dossier="001", report="charges_table", op="start",
                                periode="2026-05")
    assert out["task_id"] == "abcd" and "op='status'" in out["next"]
    etat = _tool("silae_report")(numero_dossier="001", report="charges_table", op="status",
                                 task_id="abcd")
    client.statut_edition.assert_called_once_with("tableau_charges", "abcd",
                                                  numero_dossier="001")
    assert etat["status"] == "ETAT_ENCOURS" and etat["document"] is None


def test_report_options_of_another_report_are_refused(client):
    with pytest.raises(McpError, match="does not use detail_salaries"):
        _tool("silae_report")(numero_dossier="001", report="charges_table",
                              periode="2026-05", detail_salaries=True)
    with pytest.raises(McpError, match="does not use format"):
        _tool("silae_report")(numero_dossier="001", report="declaration_summaries",
                              periode="2026-05", format="xlsx")


def test_documents_are_refused_before_any_call_while_the_org_masks_fields(
        client, monkeypatch):
    """Un filtre de champs ne voit pas l'intérieur d'un fichier : tant que la politique
    `silae` de l'org masque quelque chose, aucun document ne sort — et rien n'est
    téléchargé."""
    monkeypatch.setattr("oto_mcp.access.resolve_field_filter", lambda s: _FF(vide=False))
    with pytest.raises(McpError, match="document not served"):
        _tool("silae_report")(numero_dossier="001", report="charges_table", periode="2026-05")
    with pytest.raises(McpError, match="document not served"):
        _tool("silae_declaration")(numero_dossier="001", op="dsn_content", periode="2026-05",
                                   etablissement="SIEGE", type_dsn=1, fraction=1)
    client.edition_tableau_charges.assert_not_called()
    client.contenu_partiel_dsn.assert_not_called()
    # Les réponses JSON, elles, restent servies (filtrées par le middleware).
    _tool("silae_declaration")(numero_dossier="001", periode="2026-05")
    client.list_dsn_mensuelles.assert_called_once()


def test_an_unreadable_policy_refuses_the_document(client, monkeypatch):
    def boom(service):
        raise RuntimeError("db down")

    monkeypatch.setattr("oto_mcp.access.resolve_field_filter", boom)
    with pytest.raises(McpError, match="could not be read"):
        _tool("silae_report")(numero_dossier="001", report="charges_table", periode="2026-05")
    client.edition_tableau_charges.assert_not_called()


def test_dsn_content_is_decoded_as_text_when_it_is_text(client, rendus):
    client.contenu_partiel_dsn.return_value = {
        "data": "S20.G00.05.001,'01'\nS21.G00.06.003,'Société'\n".encode("cp1252"),
        "filename": "silae-dsn-001-2026-05-f1.dsn", "mimetype": "application/octet-stream"}
    out = _tool("silae_declaration")(numero_dossier="001", op="dsn_content",
                                     periode="2026-05", etablissement="SIEGE", type_dsn=1,
                                     fraction=1, segments=["S21.G00.06"])
    assert client.contenu_partiel_dsn.call_args.kwargs["segments"] == ["S21.G00.06"]
    assert out["source_encoding"] == "cp1252"
    assert rendus[0]["data"].decode("utf-8").endswith("'Société'\n")
    assert rendus[0]["filename"].endswith(".txt")


def test_binary_dsn_content_goes_out_as_a_file(client, rendus):
    client.contenu_partiel_dsn.return_value = {
        "data": b"\x00\x01\x02binary", "filename": "silae-dsn-001-2026-05-f1.dsn",
        "mimetype": "application/octet-stream"}
    out = _tool("silae_declaration")(numero_dossier="001", op="dsn_content",
                                     periode="2026-05", etablissement="SIEGE", type_dsn=1,
                                     fraction=1)
    assert out["source_encoding"] is None
    assert rendus[0]["mime"] == "application/octet-stream"


def test_declaration_states_require_a_type(client):
    with pytest.raises(McpError, match="type_declaration"):
        _tool("silae_declaration")(numero_dossier="001", op="states")
    _tool("silae_declaration")(numero_dossier="001", op="states",
                               type_declaration="DECLARATION", periode="2026-05")
    client.etat_declarations.assert_called_once_with(
        "001", type_declaration="DECLARATION", periode="2026-05")


# --- erreurs ------------------------------------------------------------------

def test_a_silae_error_becomes_a_named_tool_error_with_silae_message(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.list_etablissements.side_effect = UpstreamHTTPError(
        400, {"errors": [{"code": "1011", "message": "la valeur de la liste de dossiers "
                          "est nulle ou vide"}], "recoverable": True, "source": "Api"},
        service="silae")
    with pytest.raises(McpError, match="1011: la valeur de la liste"):
        _tool("silae_dossier")(op="establishments", numero_dossier="001")


def test_a_client_validation_error_becomes_a_tool_error(client):
    client.bulletin_cumuls.side_effect = ValueError("the range covers 13 months")
    with pytest.raises(McpError, match="13 months"):
        _tool("silae_payslip")(numero_dossier="001", op="totals", matricule_salarie="0001",
                               periode_debut="2025-01", periode_fin="2026-01")


@pytest.mark.parametrize("tool,kwargs,expected", [
    ("silae_dossier", {}, "'list', 'numbers', 'collective_agreements', 'current_period', "
                          "'establishments' or 'organisms'"),
    ("silae_employee", _D, "'list', 'get', 'jobs' or 'from_internal_id'"),
    ("silae_payslip", _D, "'pdf_ids', 'indices', 'header', 'lines', 'details' or 'totals'"),
    ("silae_declaration", _D, "'dsn_list', 'dsn_content' or 'states'"),
    ("silae_report", {**_D, "report": "charges_table"}, "'generate', 'start' or 'status'"),
])
def test_unknown_op_is_refused_with_the_allowed_list(client, tool, kwargs, expected):
    """Une op inconnue lève en nommant les ops valides — jamais de repli sur le défaut,
    et l'ancienne `info` (qui ne prenait pas de dossier) ne ressuscite pas."""
    with pytest.raises(McpError, match="op must be") as e:
        _tool(tool)(op="nope", **kwargs)
    assert expected in str(e.value)
    with pytest.raises(McpError, match="op must be"):
        _tool("silae_dossier")(op="info", numero_dossier="001")


@pytest.mark.parametrize("tool,kwargs", [
    ("silae_dossier", {}),
    ("silae_employee", _D),
    ("silae_payslip", {**_D, "periode": "2026-05"}),
    ("silae_declaration", {**_D, "periode": "2026-05"}),
])
def test_every_default_op_is_a_read(client, tool, kwargs):
    _tool(tool)(**kwargs)
    called = {c[0] for c in client.method_calls}
    assert called and called <= {"list_dossiers", "list_salaries", "bulletins_ids_pdf",
                                 "list_dsn_mensuelles"}


def test_the_modules_never_name_a_write_method():
    """Contrôle STATIQUE, complémentaire du dynamique : si quelqu'un ajoute une op
    d'écriture, ce test tombe — il faut alors un cas de test dédié et une décision
    produit, pas un simple vert."""
    racine = Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"
    for nom in ("silae.py", "silae_documents.py"):
        code = "\n".join(l for l in (racine / nom).read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        for m in _WRITE_METHODS:
            assert f".{m}(" not in code, f"{m} est une ÉCRITURE Silae ({nom})"
