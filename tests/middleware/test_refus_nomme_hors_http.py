"""Un refus que la description d'un connecteur nomme (`tools.decrits.RefusNomme`), sans statut HTTP d'erreur, est
servi tel quel à l'agent, sous son code : son message est celui de la description, écrit par nous. Avant le 12/11/2026,
« la tâche DataForSEO n'est pas encore prête : relis-la plus tard » (un 200 dont le corps refuse) arrivait à l'agent
en « Internal server error. » et partait à Sentry comme un bogue."""
from __future__ import annotations

from fastmcp.exceptions import ToolError
from oto.connectors.engine.errors import Refused

from oto_mcp.error_taxonomy import _is_expected_error, classify
from oto_mcp.tools.decrits import RefusNomme


def _servi(refus: Refused) -> Exception:
    """Le refus tel que fastmcp le remonte : un `ToolError` dont la cause est le `RefusNomme`."""
    try:
        try:
            raise RefusNomme(refus) from refus
        except RefusNomme as nomme:
            raise ToolError("Error calling tool 'dataforseo'") from nomme
    except ToolError as e:
        return e


def test_a_refusal_read_in_a_200_body_is_served_with_its_message_and_code():
    exc = _servi(Refused("task_not_ready", "The DataForSEO task is not done yet: read it again later.",
                         status_code=200, retryable=True))
    info = classify(exc)
    assert (info.code, info.retryable) == ("task_not_ready", True)
    assert info.message == "The DataForSEO task is not done yet: read it again later. (task_not_ready)"
    assert _is_expected_error(exc)


def test_a_refusal_of_the_answer_without_status_keeps_its_message():
    info = classify(_servi(Refused("no_results", "DataForSEO found no results for this request.")))
    assert (info.code, info.retryable, info.hint) == ("no_results", False, None)
    assert info.message.startswith("DataForSEO found no results")


def test_a_refusal_that_marks_the_account_keeps_the_category_the_envelope_reads():
    quota = classify(_servi(Refused("balance_exhausted", "The DataForSEO balance is exhausted.", status_code=200,
                                    marks_account="quota_exhausted")))
    assert quota.code == "quota_exhausted"
    cle = classify(_servi(Refused("key_refused", "DataForSEO refused the key.", status_code=200,
                                  marks_account="credential_rejected")))
    assert cle.code == "not_authorized"


def test_a_refusal_carried_by_an_http_error_status_still_goes_through_the_status():
    info = classify(_servi(Refused("rate_limited", "Too many requests.", status_code=429, retryable=True)))
    assert info.code == "rate_limited"
