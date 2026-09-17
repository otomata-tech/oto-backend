"""« tenant » : 17 occurrences dans le contrat, 0 dans la doc publique, aucune
définition (oto-backend#775). Rectificatif du 06/09/2026 : le mot est juste, il
n'y a rien à renommer — seule la définition manquait. Ce banc vérifie qu'elle est
bien SERVIE (JSON schema Pydantic), pas seulement écrite en commentaire Python.
"""
from __future__ import annotations

from oto_mcp.capabilities.connectors.instances import (
    ConnectorInstance, InstanceOwner, ListInstancesInput,
)
from oto_mcp.capabilities.connectors.verify import VerifyResult

_TERMES_ATTENDUS = ("tenant", "au-dessus", "hébergeur")


def _description(schema: dict, champ: str) -> str:
    return schema["properties"][champ]["description"]


def test_connector_instance_level_definit_le_tenant():
    desc = _description(ConnectorInstance.model_json_schema(), "level")
    assert all(mot in desc.lower() for mot in _TERMES_ATTENDUS), desc


def test_instance_owner_type_definit_le_tenant():
    desc = _description(InstanceOwner.model_json_schema(), "type")
    assert all(mot in desc.lower() for mot in _TERMES_ATTENDUS), desc


def test_verify_result_level_definit_le_tenant():
    desc = _description(VerifyResult.model_json_schema(), "level")
    assert all(mot in desc.lower() for mot in _TERMES_ATTENDUS), desc


def test_list_instances_input_level_definit_le_tenant():
    desc = _description(ListInstancesInput.model_json_schema(), "level")
    assert all(mot in desc.lower() for mot in _TERMES_ATTENDUS), desc


def test_les_trois_champs_partagent_le_meme_texte():
    """Une seule définition, réutilisée — pas trois formulations qui pourraient
    diverger avec le temps."""
    d1 = _description(ConnectorInstance.model_json_schema(), "level")
    d2 = _description(InstanceOwner.model_json_schema(), "type")
    d3 = _description(VerifyResult.model_json_schema(), "level")
    assert d1 == d2 == d3
