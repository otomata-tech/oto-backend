"""Documents légaux — SOURCE DE VÉRITÉ (version/label/url), miroir de
`oto-websites/web/src/legal`.

Le contenu des docs vit sur oto.cx (routes `/terms`, `/cgv`, `/dpa`) ; ici on ne
tient que les MÉTADONNÉES (slug → version courante + libellé + URL) et la carte des
CONTEXTES (quels docs sont requis pour « accéder » vs « acheter »). Le backend
`me.legal` en dérive le reste-à-accepter ; la table `legal_acceptances` ne trace que
le consentement.

⚠️ Tenir aligné avec `web/src/legal` : à chaque bump de `current` d'un doc côté site,
bumper `version` ici — sinon un doc modifié ne redemande pas l'acceptation (ou en
redemande une périmée). Versions au 2026-09-05 : terms 3.1, cgv 2.1, dpa 2.1.

⚠️ **Cet alignement a dérivé une fois, et il a tenu huit jours** (oto-websites#74) :
les textes v2.1/v3.1 étaient écrits et servis sur oto.cx depuis le 28/08, ce fichier
était resté au 09/07. Conséquence pendant cette fenêtre : les clients gardaient
l'acceptation d'une CGV qui nommait **Stancer** comme prestataire de paiement — alors
que le service encaisse par Mollie depuis le 24/07 — et affichait des montants qui ne
sont plus pratiqués. Les textes corrigés existaient, personne n'était invité à les
accepter.

⚠️ **Aucun banc ne peut détecter cette dérive**, et il faut le savoir plutôt que de
croire le contraire : les deux vérités vivent dans deux dépôts (`current` dans
`oto-websites/web/src/legal/*/index.ts`, `version` ici), et la CI du backend ne voit
pas l'autre. La garde est humaine — le bump côté site et le bump ici sont **un seul
geste en deux endroits**, et celui-ci est le second.

**Un tenant tiers a ses PROPRES documents, pas les nôtres** — même besoin
que `orgs.front_base_url`/`front_brand` (invitations) ou `guides` scope `tenant`
(socle d'instructions) : une donnée servie à l'utilisateur qui ne peut pas rester
celle de la plateforme primaire. `docs_for` en est le seam : un override par
(tenant, slug) vit dans `tenant_legal_docs` (table, PAS le registre `tenancy.py` —
lu en LIVE, sans redémarrage) ; absent, le slug garde son défaut `CURRENT_DOCS`
tel quel. Un tenant sans override — le cas de la plupart d'entre eux aujourd'hui — voit donc
exactement les documents d'oto, jusqu'à ce qu'une ligne soit posée pour lui.
"""
from __future__ import annotations

from typing import Optional

from . import db, tenancy

# slug → métadonnées de la VERSION COURANTE (miroir de web/src/legal `current`).
# Défaut plateforme — s'applique à tout tenant sans override déclaré.
CURRENT_DOCS: dict[str, dict[str, str]] = {
    "terms": {"version": "3.1", "label": "CGU", "url": "https://oto.cx/terms"},
    "cgv":   {"version": "2.1", "label": "CGV", "url": "https://oto.cx/cgv"},
    "dpa":   {"version": "2.1", "label": "DPA", "url": "https://oto.cx/dpa"},
}

# Contexte → docs requis. `access` = à l'inscription (CGU) ; `purchase` = à l'achat.
# Un override de tenant ne peut pas AJOUTER de slug à un contexte — seulement
# remplacer version/label/url d'un slug qui y figure déjà.
CONTEXTS: dict[str, list[str]] = {
    "access": ["terms"],
    "purchase": ["terms", "cgv", "dpa"],
}


def docs_for(tenant_slug: str) -> dict[str, dict[str, str]]:
    """`CURRENT_DOCS`, avec les overrides déclarés par `tenant_slug` fusionnés
    par-dessus, slug par slug. Chaque appel relit la table — c'est ce qui rend un
    override effectif sans redéploiement ni redémarrage du process.

    Le tenant PRIMAIRE (oto) court-circuite la lecture : `CURRENT_DOCS` EST son
    défaut, il n'y a jamais de ligne à chercher pour lui — et ça évite un aller PG
    par défaut sur le chemin le plus emprunté (le seul aujourd'hui, tant qu'aucun
    tenant n'a d'override)."""
    if not tenant_slug or tenant_slug == tenancy.PRIMARY_SLUG:
        return CURRENT_DOCS
    overrides = db.get_tenant_legal_docs(tenant_slug)
    if not overrides:
        return CURRENT_DOCS
    return {slug: {**meta, **overrides.get(slug, {})} for slug, meta in CURRENT_DOCS.items()}


# ── ce qui reste à accepter ──────────────────────────────────────────────────
#
# Un SEUL calcul du « reste à accepter », partagé par les deux gates qui s'en
# servent : le gate d'accès du dashboard (`capabilities/me_legal`) et le gate
# d'achat (`billing.subscribe`, #487). Deux calculs auraient divergé au premier
# document ajouté à un contexte — et la divergence se serait vue en production,
# sur un tunnel de paiement qui laisse passer ce que l'écran d'accès refuse.

def is_current(acceptances: dict, docs: dict, slug: str) -> bool:
    """Ce doc est-il accepté À LA VERSION COURANTE ? Une acceptation d'une version
    antérieure ne compte pas — c'est tout l'intérêt du bump de version."""
    accepte = acceptances.get(slug)
    return accepte is not None and accepte["version"] == docs[slug]["version"]


def missing_docs(acceptances: dict, docs: dict, slugs: list[str]) -> list[dict]:
    """Les documents de `slugs` qui restent à accepter, DÉCRITS assez pour être
    présentés à quelqu'un : slug, libellé, version courante, URL — et la version
    déjà acceptée s'il y en a une.

    `accepted_version` est ce qui distingue « jamais accepté » (None) de « accepté
    à une version périmée » : un refus qui ne dit pas lequel des deux enverrait
    l'utilisateur chercher une case qu'il a déjà cochée.

    Fonction PURE : les acceptations et les docs arrivent en argument, la lecture
    est à l'appelant (le statut du dashboard en fait UNE pour tous les contextes)."""
    manquants = []
    for slug in slugs:
        if is_current(acceptances, docs, slug):
            continue
        accepte = acceptances.get(slug)
        manquants.append({
            "slug": slug,
            "label": docs[slug]["label"],
            "version": docs[slug]["version"],
            "url": docs[slug]["url"],
            "accepted_version": accepte["version"] if accepte else None,
        })
    return manquants


def missing_for_sub(sub: Optional[str], context: str) -> list[dict]:
    """`missing_docs` pour CE sub et CE contexte — la version qui lit la base.

    Les documents sont ceux de SON tenant (un sub d'un tenant tiers doit SES CGU,
    pas celles d'oto) ; les acceptations sont les siennes, à la ligne la plus
    récente de chaque doc.

    `sub` absent ⟹ aucune acceptation ⟹ tout est dû : le gate se ferme, il ne
    s'ouvre pas."""
    required = CONTEXTS.get(context)
    if required is None:
        raise ValueError(f"unknown_context: contexte légal inconnu : {context!r}")
    docs = docs_for(tenancy.current().tenant_of(sub))
    acceptances = db.get_legal_acceptances(sub) if sub else {}
    return missing_docs(acceptances, docs, required)
