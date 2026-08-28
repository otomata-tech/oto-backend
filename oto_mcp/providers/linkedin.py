"""Déclaration de registre du connecteur `linkedin` — la session LinkedIn opérée.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme
commune aux six connexions hébergées vit chez le porteur de la clé
(`providers/unipile.channel`) — ici, ce qui distingue CELLE-CI.
"""
from __future__ import annotations

from .unipile import channel

# linkedin : TA session LinkedIn — recherche, profils, posts, réseau, offres
# d'emploi, messagerie. 8 tools à `op=` sous le namespace `linkedin`.
#
# ⚠️ **Le nom NU est ici depuis le 2026-08-28**, et il ne l'a pas pris à AI Ark.
# Il avait été LIBÉRÉ le 2026-08-10 par le dépôt de l'ex-connecteur `linkedin` (AI
# Ark en app-credits, #231) ; il revient à ce qui EST LinkedIn : la session qu'on
# opère. `aiark`, lui, n'est pas touché — il garde son namespace `linkedin_aiark`,
# son credential, son grant plateforme et ses tools tels qu'en production.
#
# Les deux cohabitent par la résolution au plus long préfixe DÉCLARÉ
# (`tool_visibility.namespace_of`) : `linkedin_aiark_person` va à `aiark`,
# `linkedin_post` va ici. C'est le cas pour lequel cette règle existe — sans elle,
# les tools d'AI Ark seraient gatés par la session (mauvaise clé, mauvaise
# activation, mauvaise sélection). Verrouillé par `tests/test_linkedin.py`.
#
# Et ce sont bien deux CHOSES différentes, pas deux fournisseurs interchangeables :
# `linkedin_chat` n'a pas d'équivalent AI Ark, `linkedin_aiark_person(op="mobile")`
# pas d'équivalent dans la session. AI Ark VEND de la donnée dont LinkedIn n'est
# qu'une source ; sa famille, c'est dropcontact et fullenrich.
#
# Ses tools vivent dans `tools/unipile.py` (avec la factory de messagerie partagée
# et `unipile_connect_start`) — d'où le `modules=("unipile",)` explicite : le module
# ne porte pas le nom du connecteur, et `register_all` dédoublonne les modules.
CONNECTOR = channel(
    "linkedin", "linkedin",
    hosted_channel="LINKEDIN",
    label="LinkedIn",
    help="Ta session LinkedIn — recherche, profils, posts, réseau, jobs, messagerie",
    href="https://www.linkedin.com",
    modules=("unipile",),
)

CATEGORY = "Prospection"
LOGO_DOMAIN = "linkedin.com"
DESCRIPTION = (
    "Ta session LinkedIn, opérée pour toi : recherche de personnes et "
    "d'entreprises, profils, posts et commentaires, réseau (invitations, "
    "relations), offres d'emploi et messagerie. Tu connectes TON compte et tu agis "
    "comme toi-même. Pour un email ou un mobile qu'un profil ne publie pas, c'est "
    "un connecteur d'enrichissement qu'il faut (AI Ark, Dropcontact, FullEnrich…)."
)
