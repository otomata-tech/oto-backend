"""Documents légaux — SOURCE DE VÉRITÉ (version/label/url), miroir de
`oto-websites/web/src/legal`.

Le contenu des docs vit sur oto.cx (routes `/terms`, `/cgv`, `/dpa`) ; ici on ne
tient que les MÉTADONNÉES (slug → version courante + libellé + URL) et la carte des
CONTEXTES (quels docs sont requis pour « accéder » vs « acheter »). Le backend
`me.legal` en dérive le reste-à-accepter ; la table `legal_acceptances` ne trace que
le consentement.

⚠️ Tenir aligné avec `web/src/legal` : à chaque bump de `current` d'un doc côté site,
bumper `version` ici — sinon un doc modifié ne redemande pas l'acceptation (ou en
redemande une périmée). Versions au 2026-07-09 : terms 3.0, cgv 2.0, dpa 2.0.
"""
from __future__ import annotations

# slug → métadonnées de la VERSION COURANTE (miroir de web/src/legal `current`).
CURRENT_DOCS: dict[str, dict[str, str]] = {
    "terms": {"version": "3.0", "label": "CGU", "url": "https://oto.cx/terms"},
    "cgv":   {"version": "2.0", "label": "CGV", "url": "https://oto.cx/cgv"},
    "dpa":   {"version": "2.0", "label": "DPA", "url": "https://oto.cx/dpa"},
}

# Contexte → docs requis. `access` = à l'inscription (CGU) ; `purchase` = à l'achat.
CONTEXTS: dict[str, list[str]] = {
    "access": ["terms"],
    "purchase": ["terms", "cgv", "dpa"],
}
