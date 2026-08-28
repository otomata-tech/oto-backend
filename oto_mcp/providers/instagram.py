"""Déclaration de registre du connecteur `instagram` — la messagerie Instagram opérée.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme
commune aux six connexions hébergées vit chez le porteur de la clé
(`providers/unipile.channel`) — ici, ce qui distingue CELLE-CI.
"""
from __future__ import annotations

from .unipile import channel

# Instagram : la personne connecte SON compte par un flux hébergé, et l'outil
# `instagram_chat(op=list|read|send)` agit sous cette identité. Dérivé de la factory de
# messagerie commune (`tools/unipile.register_messaging_tools`) — l'API `/chats` du
# fournisseur est channel-agnostic, c'est le canal du compte opéré qui décide de la
# route.
#
# La carte ne nomme pas notre fournisseur : ce qu'on connecte, c'est un compte
# Instagram. Le compte fournisseur (la clé) est un connecteur à part, `unipile`.
CONNECTOR = channel(
    "instagram", "instagram",
    hosted_channel="INSTAGRAM",
    label="Instagram",
    help="Tes DM Instagram — lire et envoyer des messages",
    href="https://www.instagram.com",
)

CATEGORY = "Messagerie"
LOGO_DOMAIN = "instagram.com"
DESCRIPTION = (
    "Les messages privés de ton compte Instagram : lister tes conversations, "
    "lire un fil et envoyer un message. Les DM seulement — ni feed, ni "
    "stories, ni publication."
)
