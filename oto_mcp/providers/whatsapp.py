"""Déclaration de registre du connecteur `whatsapp` — la messagerie WhatsApp opérée.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme
commune aux six connexions hébergées vit chez le porteur de la clé
(`providers/unipile.channel`) — ici, ce qui distingue CELLE-CI.
"""
from __future__ import annotations

from .unipile import channel

# WhatsApp : la personne connecte SON compte par un flux hébergé, et l'outil
# `whatsapp_chat(op=list|read|send)` agit sous cette identité. Dérivé de la factory de
# messagerie commune (`tools/unipile.register_messaging_tools`) — l'API `/chats` du
# fournisseur est channel-agnostic, c'est le canal du compte opéré qui décide de la
# route.
#
# La carte ne nomme pas notre fournisseur : ce qu'on connecte, c'est un compte
# WhatsApp. Le compte fournisseur (la clé) est un connecteur à part, `unipile`.
CONNECTOR = channel(
    "whatsapp", "whatsapp",
    hosted_channel="WHATSAPP",
    label="WhatsApp",
    help="Ton WhatsApp — lire tes conversations et envoyer des messages",
    href="https://www.whatsapp.com",
)

CATEGORY = "Messagerie"
LOGO_DOMAIN = "whatsapp.com"
DESCRIPTION = (
    "Ton compte WhatsApp, connecté en appareil lié (linked device) comme "
    "WhatsApp Web : lister tes conversations, lire un fil et envoyer un "
    "message, sous ton propre numéro."
)
