"""Garde-fou : aucune réponse MCP d'une capacité ne porte un secret frappé par le serveur.

Miroir, en SORTIE, de « un secret brut ne passe jamais en argument MCP ». Un secret que
le serveur fabrique (le porteur d'un webhook, un jeton d'API, une clé) se remet à une
PERSONNE, sur l'écran de son type — jamais dans la réponse d'un outil, qui finit dans le
contexte du modèle. Le seul secret qu'une capacité MCP fabrique aujourd'hui est le
porteur d'un webhook (`oto_trigger op=create` webhook, `op=rotate_secret`), remis par
lien (`_secret_par_lien`) ; les jetons d'API, les clés d'org, le secret de signature et
l'adresse d'un webhook sans preuve n'ont PAS de face MCP.

Ce test lit les modèles de SORTIE de toutes les capacités qui ont une face MCP. Un champ
dont le nom désigne un secret casse la CI tant qu'il n'est pas listé ici avec sa raison :
le réflexe attendu, pour un NOUVEAU type de secret, est de le remettre par lien vers
l'écran de son type (comme `hook_secret_url`), pas d'allonger cette liste.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from oto_mcp import capabilities  # noqa: F401 — remplit le registre
from oto_mcp.capabilities.registry import CAPABILITIES

# Le NOM désigne un secret (pas un compteur de jetons du modèle : `max_tokens`).
_SECRET = re.compile(r"(?<![a-z])(secret|password|passwd|api_key|bearer|private_key)"
                     r"(?![a-z])|(^|_)token$", re.I)

_CONNUS: dict[str, str] = {
    "oto_trigger:TriggerOut.hook_secret":
        "None sur la face MCP — le porteur se remet par lien (`_secret_par_lien`)",
    "oto_trigger:TriggerOut.hook_secret_url": "un lien vers l'écran du porteur",
    "oto_trigger:TriggerOut.hook_secret_kind": "le TYPE de porteur (`bearer`/`body_secret`)",
    "oto_trigger:TriggerOut.hook_secret_note": "une consigne",
    "oto_trigger:TriggerOut.hook_secret_missing": "pourquoi il n'y a pas de lien",
    "oto_trigger:Trigger.signing_secret_set": "un booléen : posé ou non, jamais la valeur",
    "oto_admin_model_spend:OtherKeySpend.api_key_id": "l'identifiant d'une clé, pas la clé",
    "oto_admin_model_spend:PlatformKeySpend.api_key_id": "l'identifiant d'une clé, pas la clé",
}


def _champs(modele: type[BaseModel], chemin: str, vus: set) -> list[str]:
    if modele in vus:
        return []
    vus.add(modele)
    out = []
    for nom, champ in modele.model_fields.items():
        ici = f"{chemin}.{nom}"
        if _SECRET.search(nom):
            # Clé STABLE : l'outil + le modèle qui porte le champ (un modèle partagé
            # par deux branches serait sinon nommé par la première visitée).
            out.append(f"{chemin.split('.')[0]}:{modele.__name__}.{nom}")
        for a in getattr(champ.annotation, "__args__", None) or (champ.annotation,):
            for b in getattr(a, "__args__", None) or (a,):
                if isinstance(b, type) and issubclass(b, BaseModel):
                    out += _champs(b, ici, vus)
    return out


def test_aucune_sortie_MCP_ne_porte_un_secret_non_declare():
    trouves = set()
    for c in CAPABILITIES:
        if c.mcp and getattr(c, "Output", None):
            trouves.update(_champs(c.Output, c.mcp, set()))
    inconnus = sorted(trouves - set(_CONNUS))
    assert not inconnus, (
        f"champ(s) de sortie MCP nommés comme un secret : {inconnus} — un secret frappé "
        "par le serveur se remet à une personne par lien vers l'écran de son type "
        "(cf. `capabilities/_secret_par_lien.py`), jamais dans la réponse d'un outil")


def test_la_liste_ne_garde_que_ce_qui_existe():
    trouves = set()
    for c in CAPABILITIES:
        if c.mcp and getattr(c, "Output", None):
            trouves.update(_champs(c.Output, c.mcp, set()))
    perimes = sorted(set(_CONNUS) - trouves)
    assert not perimes, f"retirer de _CONNUS, plus servis : {perimes}"
