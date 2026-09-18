"""Yousign — signature électronique (créer une demande, l'activer, suivre son
statut, récupérer le document signé).

Clé résolue par appel via `access.resolve_api_key("yousign")` : modèle
clé-per-user/org (comme gocardless/pennylane), pas de clé plateforme.

⚠️ À la différence d'un connecteur en lecture (gocardless), `yousign_envoyer`
ÉCRIT réellement : elle notifie des personnes réelles par email. Le cycle est
volontairement scindé en deux appels (`yousign_creer` puis `yousign_envoyer`)
plutôt qu'un seul geste qui enverrait sans confirmation — un agent qui
composerait une demande peut la relire avant de déclencher l'envoi.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from fastmcp import FastMCP

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `list_signature_requests` est la lecture la moins chère qui exige une clé
    valide sans effet de bord (contrairement à `create_signature_request`, qui
    créerait un brouillon réel)."""
    from oto.tools.common import UpstreamHTTPError
    from oto.tools.yousign import YousignClient

    try:
        YousignClient(api_key=fields["key"]).list_signature_requests()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Yousign HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Yousign: HTTP {e.status_code}: {e.body}")


def register(mcp: FastMCP) -> None:
    from oto.tools.yousign import YousignClient

    connector_verify.register("yousign", _verify)

    def _client() -> YousignClient:
        key, _is_platform = access.resolve_api_key("yousign")
        return YousignClient(api_key=key)

    @mcp.tool()
    def yousign_creer(
        nom: str,
        pdf_base64: str,
        nom_fichier: str,
        signataires: list[dict[str, Any]],
        message_livraison: str = "email",
    ) -> dict:
        """Compose une demande de signature : crée le brouillon, y attache le
        PDF et les signataires. N'ENVOIE RIEN — `yousign_envoyer` déclenche
        l'envoi réel des invitations.

        Args:
            nom: nom de la demande (1-128 caractères), visible par les
                signataires dans l'email.
            pdf_base64: le PDF à faire signer, encodé en base64.
            nom_fichier: nom du fichier tel qu'il apparaîtra (ex. "nda.pdf").
            signataires: `[{"prenom", "nom", "email", "langue"?}, …]` —
                `langue` ∈ en|fr|de|it|nl|es|pl|pt|ro, défaut "fr". L'ordre de
                la liste devient l'ordre de signature si plusieurs signataires
                sont déclarés.
            message_livraison: "email" (Yousign envoie le lien) ou "none"
                (le lien est à distribuer soi-même, via `yousign_statut`).

        Returns:
            `{"demande_id", "document_id", "signataires": [{"id", "email"}]}`
            — à repasser tels quels à `yousign_envoyer`/`yousign_statut`/
            `yousign_document_signe`.
        """
        c = _client()
        demande = c.create_signature_request(nom, delivery_mode=message_livraison)
        demande_id = demande["id"]

        fichier = c.add_document(demande_id, base64.b64decode(pdf_base64), nom_fichier)
        document_id = fichier["id"]

        signataires_crees = []
        for s in signataires:
            info = {"first_name": s["prenom"], "last_name": s["nom"],
                    "email": s["email"], "locale": s.get("langue", "fr")}
            reponse = c.add_signer(demande_id, info)
            signataires_crees.append({"id": reponse["id"], "email": s["email"]})

        return {"demande_id": demande_id, "document_id": document_id,
                "signataires": signataires_crees}

    @mcp.tool()
    def yousign_envoyer(demande_id: str) -> dict:
        """Active une demande composée par `yousign_creer` : quitte l'état
        brouillon, notifie les signataires (sauf `message_livraison="none"`).

        ⚠️ Envoie réellement des emails à des personnes réelles — pas
        d'annulation propre une fois envoyé (seulement `cancel`, qui clôt la
        demande sans en effacer la trace).
        """
        return _client().activate_signature_request(demande_id)

    @mcp.tool()
    def yousign_statut(demande_id: str) -> dict:
        """Statut d'une demande de signature et de ses signataires.

        `statut` ∈ draft (composée, pas envoyée) | ongoing (envoyée, en
        attente) | done (tous les signataires ont signé) | expired | canceled
        | declined | rejected | paused | approval. Seul `done` signifie que
        le document signé est prêt (`yousign_document_signe`)."""
        demande = _client().get_signature_request(demande_id)
        return {"statut": demande.get("status"), "nom": demande.get("name"),
                "signataires": demande.get("signers"), "documents": demande.get("documents")}

    @mcp.tool()
    def yousign_document_signe(demande_id: str, document_id: str) -> dict:
        """Télécharge le PDF signé d'UN document d'une demande `done`.

        Avant `done`, rend le document ORIGINAL non signé (Yousign ne
        distingue pas les deux par cet appel — vérifier `yousign_statut`
        d'abord). Une demande à plusieurs documents demande un appel PAR
        document (pas de téléchargement groupé).

        Returns:
            `{"nom_fichier", "pdf_base64"}` — le PDF encodé en base64 (le
            transport MCP ne porte pas de binaire brut).
        """
        pdf_bytes = _client().download_document(demande_id, document_id)
        return {"nom_fichier": f"{document_id}.pdf",
                "pdf_base64": base64.b64encode(pdf_bytes).decode()}
