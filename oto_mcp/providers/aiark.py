"""Déclaration de registre du connecteur `aiark`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# aiark : connecteur classique (kind="tools", ex-mount fédéré #152 → requalifié
# #160). Client REST synchrone dans oto-core (`oto.tools.aiark`), tools curés
# dans `tools/aiark.py` (contrat LLM), cascade de clé standard
# (`resolve_api_key`) + `record_platform_usage` → mode plateforme possible.
# v1 = endpoints synchrones (company/people search, single-person export+email,
# reverse-lookup, mobile phone) ; les exports en lot d'AI Ark sont async
# (webhook) → hors périmètre.
# ⚠️ Namespace des tools = `linkedin_aiark` (ADR 0010 §Amendement 2026-08-10) :
# le nom porte la CAPACITÉ (LinkedIn) suffixée du FOURNISSEUR, parce que deux
# fournisseurs NON SUBSTITUABLES la rendent — AI Ark = donnée ACHETÉE au crédit
# (aucun compte connecté), Unipile = la session OPÉRÉE (`linkedin_*`, nom nu).
# `namespace_of` résout au plus long préfixe déclaré : les deux gardent un gate
# distinct. Le connecteur, lui, garde le nom du fournisseur — c'est l'unité
# d'activation et de credential.
# **Absorbe l'ex-connecteur `linkedin`** (#231, déposé le 2026-08-10,
# oto-backend#279) : même vendeur, même client `AiArkClient`, mêmes 5 fonctions —
# il n'en différait que par le mode d'auth (`platform` seul vs BYO), ce qui est
# une distinction d'INSTANCE (ADR 0038/0044 §F), pas de connecteur. Le doublon
# coûtait de poser DEUX FOIS la même clé pour un seul pool de crédits vendeur
# (ADR 0024 : chaque connecteur résout SON nom). Le packaging « offert par oto »
# survit tel quel : c'est le grant plateforme sur `aiark`. Rien à migrer côté
# coffre — aucun grant n'était posé sous `linkedin`, dont les 5 tools étaient
# montés et inopérants depuis leur mise en service.
CONNECTOR = _c(
    "aiark", ["linkedin_aiark"],
    auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key",
    label="AI Ark",
    help="people & company search via LinkedIn (données achetées au crédit)",
    href="https://ai-ark.com",
)

CATEGORY = "Prospection"
PUBLISHER = "AI Ark"
LOGO_DOMAIN = "ai-ark.com"
