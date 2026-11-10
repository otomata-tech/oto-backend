"""Réglages ▸ Alertes d'une org : où elle est prévenue qu'un agent ne fonctionne plus.

Quatre routes REST, pour la page Alertes d'un tableau de bord :

- `GET  /api/orgs/{id}/agent-alerts`          — membres : le réglage, et si l'org a Slack ;
- `PUT  /api/orgs/{id}/agent-alerts`          — admins : allumé ou non, le canal (`null`
  = message privé à la personne qui a connecté Slack) ;
- `GET  /api/orgs/{id}/agent-alerts/channels` — membres : les canaux publics que l'app voit ;
- `POST /api/orgs/{id}/agent-alerts/test`     — admins : un message de test, là où iront
  les alertes.

Les messages eux-mêmes partent de la boucle `alertes_slack_agents`. Le réglage naît
allumé à la connexion de Slack (`auth/slack.finish`).

Sous le réglage d'instance des alertes (`alertes_slack_agents.actives`) : éteint, chaque
route répond 404 `agent_alerts_off`, comme si elle n'existait pas — le tableau de bord
dit alors « pas disponible ».
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class AgentAlertsInput(BaseModel):
    org_id: int


class AgentAlertsSaveInput(BaseModel):
    org_id: int
    enabled: bool
    channel_id: Optional[str] = Field(default=None, max_length=40, description=(
        "The Slack channel id; null = a direct message to the person who connected Slack."))


class AgentAlerts(BaseModel):
    enabled: bool
    channel: Optional[dict] = None
    slack_connected: bool
    slack_workspace: Optional[str] = None
    dm_to: Optional[dict] = None


class AlertChannels(BaseModel):
    channels: list[dict]


class AlertTest(BaseModel):
    ok: bool
    channel_name: Optional[str] = None


def _exiger_les_alertes() -> None:
    from .. import alertes_slack_agents
    if not alertes_slack_agents.actives():
        raise AuthzDenied(404, "agent_alerts_off",
                          "Les alertes d'agents ne sont pas disponibles sur cette instance.")


def _vue(org_id: int) -> dict:
    from .. import alertes_slack_agents
    from ..db import alertes_slack
    r = alertes_slack.reglage(org_id) or {}
    bot = alertes_slack_agents.bot_de_l_org(org_id)
    canal = ({"id": r["channel_id"], "name": r.get("channel_name") or r["channel_id"]}
             if r.get("channel_id") else None)
    return {"enabled": bool(r.get("enabled")) and bot is not None,
            "channel": canal,
            "slack_connected": bot is not None,
            "slack_workspace": (bot[1] or None) if bot else None,
            "dm_to": ({"name": r.get("dm_name") or r.get("dm_user")}
                      if not canal and r.get("dm_user") else None)}


def _canaux(org_id: int) -> list[dict]:
    from oto.tools.slack.client import SlackClient
    from .. import alertes_slack_agents
    bot = alertes_slack_agents.bot_de_l_org(org_id)
    if bot is None:
        return []
    lus = SlackClient(bot_token=bot[0]).list_channels(types="public_channel")
    return sorted(({"id": c["id"], "name": c.get("name") or c["id"]} for c in lus
                   if not c.get("is_archived")), key=lambda c: c["name"])


def _lire(ctx: ResolvedCtx, inp: AgentAlertsInput) -> dict:
    _exiger_les_alertes()
    return _vue(inp.org_id)


def _poser(ctx: ResolvedCtx, inp: AgentAlertsSaveInput) -> dict:
    from ..db import alertes_slack
    _exiger_les_alertes()
    nom = None
    if inp.channel_id:
        connus = {c["id"]: c["name"] for c in _canaux(inp.org_id)}
        if inp.channel_id not in connus:
            raise AuthzDenied(400, "unknown_channel",
                              "Ce canal n'est pas un canal public que l'app Slack voit.")
        nom = connus[inp.channel_id]
    alertes_slack.poser(inp.org_id, enabled=inp.enabled, channel_id=inp.channel_id,
                        channel_name=nom)
    return _vue(inp.org_id)


def _lister_canaux(ctx: ResolvedCtx, inp: AgentAlertsInput) -> dict:
    _exiger_les_alertes()
    return {"channels": _canaux(inp.org_id)}


def _tester(ctx: ResolvedCtx, inp: AgentAlertsInput) -> dict:
    from .. import alertes_slack_agents
    from ..db import alertes_slack
    _exiger_les_alertes()
    r = alertes_slack.reglage(inp.org_id)
    if not r or not alertes_slack_agents.destination(r):
        raise AuthzDenied(400, "no_destination",
                          "Aucun canal ni personne à prévenir : connecte Slack ou choisis un canal.")
    texte = alertes_slack_agents.message_de_test(inp.org_id)
    if not alertes_slack_agents.poster(inp.org_id, r, texte):
        raise AuthzDenied(502, "slack_refused", "Slack a refusé le message de test.")
    return {"ok": True, "channel_name": r.get("channel_name") if r.get("channel_id") else None}


CAPABILITIES += [
    Capability(key="org.agent_alerts.get", handler=_lire, Input=AgentAlertsInput,
               Output=AgentAlerts, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("GET", "/api/orgs/{id}/agent-alerts", _ID)),
    Capability(key="org.agent_alerts.set", handler=_poser, Input=AgentAlertsSaveInput,
               Output=AgentAlerts, authz=ORG_ADMIN_OF("org_id"), mcp=None,
               rest=RestBinding("PUT", "/api/orgs/{id}/agent-alerts", _ID)),
    Capability(key="org.agent_alerts.channels", handler=_lister_canaux,
               Input=AgentAlertsInput, Output=AlertChannels,
               authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("GET", "/api/orgs/{id}/agent-alerts/channels", _ID)),
    Capability(key="org.agent_alerts.test", handler=_tester, Input=AgentAlertsInput,
               Output=AlertTest, authz=ORG_ADMIN_OF("org_id"), mcp=None,
               rest=RestBinding("POST", "/api/orgs/{id}/agent-alerts/test", _ID)),
]
