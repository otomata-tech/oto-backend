"""Client HTTP mince vers le service FOD — capacité « rupture conventionnelle ».

Le contrôle d'une demande d'homologation (salaire de référence neutralisé, planchers
légal et conventionnel, calendrier) est un calcul pur servi par FOD
(`POST /api/rupture-conventionnelle/controle`). Les montants partent dans le CORPS de
la requête, jamais dans le chemin ; FOD n'en conserve rien.
"""
from __future__ import annotations

from typing import Any

from .http import post as _post


def controler(dossier: dict[str, Any]) -> dict:
    return _post("/api/rupture-conventionnelle/controle", dossier)
