"""Marque Otomata — source unique du favicon servi côté backend.

Le mark canonique vit dans `oto-websites/web/public/favicon.svg` (le « rond
coupé » 4 accents saffron/terra/cobalt/olive + cœur crème). On le duplique ici
en inline pour que les pages/endpoints auto-portés du backend (share_ui, page de
doc publique, endpoint MCP `mcp.oto.cx`) affichent le MÊME favicon que oto.cx,
sans requête réseau ni asset à déployer. Toute évolution du mark = éditer CE
fichier ET `web/public/favicon.svg` en miroir.
"""
from __future__ import annotations

import base64

# Favicon Otomata — identique byte-à-byte à `oto-websites/web/public/favicon.svg`.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-64 -64 128 128">'
    '<defs>'
    '<radialGradient id="saff" cx="38%" cy="32%" r="78%"><stop offset="0%" stop-color="#ffd24a"/><stop offset="55%" stop-color="#f0b41e"/><stop offset="100%" stop-color="#c4870c"/></radialGradient>'
    '<radialGradient id="terr" cx="38%" cy="32%" r="78%"><stop offset="0%" stop-color="#f56a2d"/><stop offset="55%" stop-color="#d63d0a"/><stop offset="100%" stop-color="#9c2c06"/></radialGradient>'
    '<radialGradient id="oliv" cx="38%" cy="32%" r="78%"><stop offset="0%" stop-color="#c0db4e"/><stop offset="55%" stop-color="#8aa620"/><stop offset="100%" stop-color="#5c7212"/></radialGradient>'
    '<radialGradient id="cob" cx="38%" cy="32%" r="78%"><stop offset="0%" stop-color="#4f9be0"/><stop offset="55%" stop-color="#1f6dba"/><stop offset="100%" stop-color="#124a80"/></radialGradient>'
    '</defs>'
    '<clipPath id="cq"><circle r="58"/></clipPath>'
    '<g clip-path="url(#cq)">'
    '<rect x="-58" y="-58" width="58" height="58" fill="url(#saff)"/>'
    '<rect x="0" y="-58" width="58" height="58" fill="url(#terr)"/>'
    '<rect x="-58" y="0" width="58" height="58" fill="url(#cob)"/>'
    '<rect x="0" y="0" width="58" height="58" fill="url(#oliv)"/>'
    '</g>'
    '<circle r="26" fill="#fefcf5"/>'
    '</svg>'
)

# Balise <link> auto-portée (data-URI) pour les pages HTML server-side.
FAVICON_LINK = (
    '<link rel=icon type="image/svg+xml" href="data:image/svg+xml;base64,'
    + base64.b64encode(FAVICON_SVG.encode("utf-8")).decode("ascii")
    + '">'
)
