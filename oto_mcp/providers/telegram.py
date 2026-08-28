"""Déclaration de registre du connecteur `telegram` — la messagerie Telegram opérée.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme
commune aux six connexions hébergées vit chez le porteur de la clé
(`providers/unipile.channel`) — ici, ce qui distingue CELLE-CI.
"""
from __future__ import annotations

from .unipile import channel

# Telegram : la personne connecte SON compte par un flux hébergé, et l'outil
# `telegram_chat(op=list|read|send)` agit sous cette identité. Dérivé de la factory de
# messagerie commune (`tools/unipile.register_messaging_tools`) — l'API `/chats` du
# fournisseur est channel-agnostic, c'est le canal du compte opéré qui décide de la
# route.
#
# La carte ne nomme pas notre fournisseur : ce qu'on connecte, c'est un compte
# Telegram. Le compte fournisseur (la clé) est un connecteur à part, `unipile`.
CONNECTOR = channel(
    "telegram", "telegram",
    hosted_channel="TELEGRAM",
    label="Telegram",
    help="Ton Telegram — lire tes conversations et envoyer des messages",
    href="https://telegram.org",
)

CATEGORY = "Messagerie"
LOGO_DOMAIN = "telegram.org"
DESCRIPTION = (
    "Ton compte Telegram : lister tes conversations, lire un fil et envoyer "
    "un message, sous ton propre compte."
)
