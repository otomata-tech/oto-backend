"""Le bump des versions légales REDEMANDE bien l'acceptation (oto-websites#74).

Les textes CGV v2.1 / CGU v3.1 / DPA v2.1 étaient écrits et servis sur oto.cx depuis
le 28/08/2026 ; ce backend est resté sur les versions du 09/07 pendant huit jours.
Pendant cette fenêtre, les clients gardaient l'acceptation d'une CGV qui nommait
**Stancer** comme prestataire de paiement — alors que le service encaisse par Mollie
depuis le 24/07 — et affichait des montants qui ne sont plus pratiqués.

⚠️ Ce banc ne vérifie pas les NUMÉROS (les figer ici obligerait à toucher deux
endroits au prochain bump, et le second serait oublié comme le premier). Il vérifie
le MÉCANISME : qu'une acceptation d'une version antérieure redevienne due. C'est ce
qui n'avait pas été éprouvé, et ce dont la valeur ne périme pas.

⚠️ Aucun banc ne peut détecter la dérive elle-même : `current` vit dans oto-websites,
`version` vit ici, et la CI du backend ne voit pas l'autre dépôt. La garde est
humaine — c'est écrit dans `legal_docs.py`, pas masqué par un test décoratif.
"""
from __future__ import annotations

from oto_mcp import legal_docs


def _vieille(docs: dict) -> dict:
    """Ce qu'un client avait accepté : la version d'AVANT, pour chaque doc."""
    return {slug: {"version": _precedente(meta["version"])}
            for slug, meta in docs.items()}


def _precedente(v: str) -> str:
    majeur, mineur = v.split(".")
    return f"{majeur}.{int(mineur) - 1}" if int(mineur) else f"{int(majeur) - 1}.0"


def test_une_acceptation_PERIMEE_redevient_due():
    docs = legal_docs.CURRENT_DOCS
    dus = legal_docs.missing_docs(_vieille(docs), docs, legal_docs.CONTEXTS["purchase"])
    assert {d["slug"] for d in dus} == {"terms", "cgv", "dpa"}, dus


def test_l_acceptation_COURANTE_ne_redemande_rien():
    """L'autre moitié : un bump qui redemanderait tout à tout le monde en permanence
    ne serait pas un bump, ce serait une panne."""
    docs = legal_docs.CURRENT_DOCS
    a_jour = {slug: {"version": meta["version"]} for slug, meta in docs.items()}
    assert legal_docs.missing_docs(a_jour, docs,
                                   legal_docs.CONTEXTS["purchase"]) == []


def test_les_trois_documents_d_achat_sont_couverts():
    """⚠️ Le contexte `purchase` est celui qui porte l'engagement commercial : c'est
    lui qui doit exiger la CGV et le DPA, pas seulement les CGU de l'accès."""
    assert set(legal_docs.CONTEXTS["purchase"]) == {"terms", "cgv", "dpa"}
    assert legal_docs.CONTEXTS["access"] == ["terms"]


def test_chaque_document_porte_une_URL_et_un_libelle_servis():
    """Ce que l'utilisateur reçoit pour décider : sans URL, on lui demande d'accepter
    un texte qu'il ne peut pas lire."""
    for slug, meta in legal_docs.CURRENT_DOCS.items():
        assert meta["url"].startswith("https://oto.cx/"), slug
        assert meta["label"] and meta["version"]
