"""Déclaration de registre du connecteur `twitter` — la messagerie X (Twitter) opérée.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme
commune aux six connexions hébergées vit chez le porteur de la clé
(`providers/unipile.channel`) — ici, ce qui distingue CELLE-CI.
"""
from __future__ import annotations

from .unipile import channel

# X (Twitter) : la personne connecte SON compte par un flux hébergé, et l'outil
# `twitter_chat(op=list|read|send)` agit sous cette identité. Dérivé de la factory de
# messagerie commune (`tools/unipile.register_messaging_tools`) — l'API `/chats` du
# fournisseur est channel-agnostic, c'est le canal du compte opéré qui décide de la
# route.
#
# La carte ne nomme pas notre fournisseur : ce qu'on connecte, c'est un compte
# X (Twitter). Le compte fournisseur (la clé) est un connecteur à part, `unipile`.
CONNECTOR = channel(
    "twitter", "twitter",
    hosted_channel="TWITTER",
    label="X (Twitter)",
    help="Tes DM X (Twitter) — lire et envoyer des messages",
    href="https://x.com",
)

CATEGORY = "Messagerie"
LOGO_DOMAIN = "x.com"
DESCRIPTION = (
    "Les messages privés de ton compte X (Twitter) : lister tes "
    "conversations, lire un fil et envoyer un message. Les DM seulement — ni "
    "timeline, ni publication."
)
