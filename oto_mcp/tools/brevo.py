"""Brevo — emailing & CRM via l'API PUBLIQUE v3 (clé `api-key`).

Wrappe `oto.tools.brevo.BrevoClient`. Clé résolue par appel via
`access.resolve_api_key("brevo")` — byo (clé du membre ou credential partagé de
l'org). Pas de clé plateforme : un compte Brevo = les contacts de son propriétaire.

⚠️ **Distinct du connecteur `brevoauto`** (automations, API privée + session
navigateur). Même éditeur, surfaces disjointes : la clé v3 n'ouvre pas l'authoring
d'automations, et la session navigateur n'ouvre pas ces tools.

**Écritures dangereuses volontairement absentes** : envoi d'une campagne
(`sendNow` / statut `sent`), suppression de contact / liste / campagne / template,
purge des hard bounces. On conçoit, on mesure, on s'envoie un test — le départ d'un
envoi de masse et les suppressions restent dans l'UI Brevo. `brevo_send_email` reste
exposé : c'est du transactionnel unitaire, destinataires explicites.
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from .. import access, connector_verify


def _verify(fields: dict) -> None:
    """Sonde « tester la connexion » : la clé authentifie-t-elle vraiment ?

    `GET /account` est sans effet de bord et refusé (401) par une clé invalide.
    Lève — le message remonte tel quel à l'UI.
    """
    from oto.tools.brevo import BrevoClient
    BrevoClient(api_key=fields["key"]).get_account()


def register(mcp: FastMCP) -> None:
    from oto.tools.brevo import BrevoClient

    connector_verify.register("brevo", _verify)

    def _client() -> BrevoClient:
        key, _ = access.resolve_api_key("brevo")
        return BrevoClient(api_key=key)

    # --- Compte ---------------------------------------------------------------

    @mcp.tool()
    def brevo_account(senders: bool = True) -> dict:
        """Compte Brevo : société, plan, crédits email/SMS restants.

        Args:
            senders: joindre les expéditeurs vérifiés — leur `email` est requis
                pour envoyer un email ou créer une campagne.
        """
        client = _client()
        out: dict[str, Any] = {"account": client.get_account()}
        if senders:
            out["senders"] = client.list_senders()
        return out

    # --- Contacts -------------------------------------------------------------

    @mcp.tool()
    def brevo_contacts(
        limit: int = 50,
        offset: int = 0,
        list_ids: Optional[list[int]] = None,
        segment_id: Optional[int] = None,
        modified_since: Optional[str] = None,
        created_since: Optional[str] = None,
        filter: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> dict:
        """Liste les contacts (paginé, max 1000 par appel).

        Args:
            list_ids: restreindre à des listes. **Exclusif avec `segment_id`.**
            modified_since: ISO 8601 UTC (`2026-01-31T00:00:00.000Z`).
            filter: filtre sur attributs, opérateur `equals` SEULEMENT —
                ex. `equals(FIRSTNAME,"Alex")`. Pas de `contains` ni `>`.
            sort: `asc` | `desc` (défaut `desc`, par date de création).
        """
        return _client().list_contacts(
            limit=limit, offset=offset, list_ids=list_ids, segment_id=segment_id,
            modified_since=modified_since, created_since=created_since,
            filter=filter, sort=sort)

    @mcp.tool()
    def brevo_get_contact(identifier: str,
                          identifier_type: Optional[str] = None) -> dict:
        """Fiche d'un contact (attributs, listes, statistiques d'envoi).

        Args:
            identifier: email par défaut ; sinon id, téléphone ou EXT_ID.
            identifier_type: `email_id` | `contact_id` | `phone_id` | `ext_id`.
        """
        return _client().get_contact(identifier, identifier_type=identifier_type)

    @mcp.tool()
    def brevo_upsert_contact(
        email: str,
        attributes: Optional[dict] = None,
        list_ids: Optional[list[int]] = None,
        update_enabled: bool = True,
        ext_id: Optional[str] = None,
    ) -> dict:
        """Crée un contact, ou le met à jour s'il existe (`update_enabled`).

        Args:
            attributes: attributs Brevo en MAJUSCULES (`{"PRENOM": "Alex",
                "NOM": "Laporte"}`) — ils doivent exister au compte
                (cf. `brevo_contact_attributes`).
            list_ids: listes auxquelles l'inscrire.

        Renvoie `{"id": …}` à la création, un objet vide sur une mise à jour.
        """
        return _client().upsert_contact(
            email=email, attributes=attributes, list_ids=list_ids,
            update_enabled=update_enabled, ext_id=ext_id)

    @mcp.tool()
    def brevo_update_contact(
        identifier: str,
        attributes: Optional[dict] = None,
        list_ids: Optional[list[int]] = None,
        unlink_list_ids: Optional[list[int]] = None,
        identifier_type: Optional[str] = None,
        email_blacklisted: Optional[bool] = None,
    ) -> dict:
        """Met à jour un contact existant. Renvoie un objet vide au succès.

        La voie pour **désinscrire d'une liste** (`unlink_list_ids`) ou
        **blacklister** (`email_blacklisted=True`, le contact ne recevra plus rien).
        """
        return _client().update_contact(
            identifier, attributes=attributes, list_ids=list_ids,
            unlink_list_ids=unlink_list_ids, identifier_type=identifier_type,
            email_blacklisted=email_blacklisted)

    @mcp.tool()
    def brevo_import_contacts(
        contacts: Optional[list[dict]] = None,
        list_ids: Optional[list[int]] = None,
        file_url: Optional[str] = None,
        update_existing_contacts: bool = True,
        new_list: Optional[dict] = None,
    ) -> dict:
        """Import de masse **asynchrone**. Renvoie `{"processId": …}` (pas les contacts).

        **La voie au-delà de 150 contacts** — `brevo_list_membership` plafonne là.

        Args:
            contacts: `[{"email": …, "attributes": {…}}, …]`.
            file_url: alternative — CSV distant (séparateur `;`).
            new_list: `{"listName": …, "folderId": …}` pour créer la liste au vol.
        """
        return _client().import_contacts(
            json_body=contacts, list_ids=list_ids, file_url=file_url,
            update_existing_contacts=update_existing_contacts, new_list=new_list)

    @mcp.tool()
    def brevo_export_contacts(contact_filter: Optional[dict] = None,
                              export_attributes: Optional[list[str]] = None) -> dict:
        """Export **asynchrone** des contacts. Renvoie `{"processId": …}`, pas les données.

        Args:
            contact_filter: `{"listIds": [1]}` | `{"segmentId": 2}` |
                `{"emailBlacklisted": true}`. Défaut = tous les contacts actifs.

        Pour lire des contacts directement, préférer `brevo_contacts` (paginé).
        """
        return _client().export_contacts(
            contact_filter=contact_filter, export_attributes=export_attributes)

    @mcp.tool()
    def brevo_contact_stats(identifier: str) -> dict:
        """Statistiques de campagnes d'un contact (ouvertures, clics, bounces)."""
        return _client().contact_campaign_stats(identifier)

    @mcp.tool()
    def brevo_contact_attributes() -> dict:
        """Attributs de contact déclarés au compte (nom, catégorie, type).

        À lire avant d'écrire des `attributes` : un attribut inconnu est refusé.
        """
        return _client().list_attributes()

    # --- Listes, dossiers, segments -------------------------------------------

    @mcp.tool()
    def brevo_lists(limit: int = 50, offset: int = 0,
                    folder_id: Optional[int] = None) -> dict:
        """Listes de contacts du compte, ou d'un dossier si `folder_id`."""
        return _client().list_lists(limit=limit, offset=offset, folder_id=folder_id)

    @mcp.tool()
    def brevo_get_list(list_id: int) -> dict:
        """Détail d'une liste : nom, dossier, nombre de contacts et de blacklistés."""
        return _client().get_list(list_id)

    @mcp.tool()
    def brevo_list_contacts(list_id: int, limit: int = 50, offset: int = 0,
                            modified_since: Optional[str] = None) -> dict:
        """Contacts d'une liste (paginé, max 500 par appel)."""
        return _client().list_contacts_of_list(
            list_id, limit=limit, offset=offset, modified_since=modified_since)

    @mcp.tool()
    def brevo_create_list(name: str, folder_id: int) -> dict:
        """Crée une liste. `folder_id` est **obligatoire** (cf. `brevo_folders`)."""
        return _client().create_list(name, folder_id)

    @mcp.tool()
    def brevo_update_list(list_id: int, name: Optional[str] = None,
                          folder_id: Optional[int] = None) -> dict:
        """Renomme une liste, ou la déplace vers un autre dossier."""
        return _client().update_list(list_id, name=name, folder_id=folder_id)

    @mcp.tool()
    def brevo_list_membership(
        list_id: int,
        action: str,
        emails: Optional[list[str]] = None,
        ids: Optional[list[int]] = None,
        all_contacts: bool = False,
    ) -> dict:
        """Ajoute ou retire des contacts EXISTANTS d'une liste.

        ⚠️ **Max 150 contacts par appel**, et un SEUL type d'identifiant
        (`emails` OU `ids`) — au-delà, l'API refuse : utiliser
        `brevo_import_contacts`, qui crée aussi les contacts absents.

        Args:
            action: `add` | `remove`.
            all_contacts: (remove seulement) vide la liste entière.

        Renvoie `{contacts: {success: [...], failure: [...]}}` — **lire `failure`**,
        un contact inconnu échoue sans faire échouer l'appel.
        """
        client = _client()
        if action == "add":
            return client.add_to_list(list_id, emails=emails, ids=ids)
        if action == "remove":
            return client.remove_from_list(
                list_id, emails=emails, ids=ids, all_contacts=all_contacts)
        raise ValueError("`action` doit valoir add | remove.")

    @mcp.tool()
    def brevo_folders(limit: int = 50, offset: int = 0) -> dict:
        """Dossiers de listes. Leur `id` est requis pour `brevo_create_list`."""
        return _client().list_folders(limit=limit, offset=offset)

    @mcp.tool()
    def brevo_segments(limit: int = 50, offset: int = 0) -> dict:
        """Segments (listes dynamiques définies par un filtre). Lecture seule."""
        return _client().list_segments(limit=limit, offset=offset)

    # --- Email transactionnel --------------------------------------------------

    @mcp.tool()
    def brevo_send_email(
        to: list[dict],
        subject: Optional[str] = None,
        html_content: Optional[str] = None,
        sender: Optional[dict] = None,
        template_id: Optional[int] = None,
        params: Optional[dict] = None,
        cc: Optional[list[dict]] = None,
        bcc: Optional[list[dict]] = None,
        reply_to: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        scheduled_at: Optional[str] = None,
    ) -> dict:
        """**Envoie réellement** un email transactionnel. Renvoie `{"messageId": …}`.

        Deux modes exclusifs :
        - **template** : `template_id` + `params` (variables `{{params.NOM}}`) ;
        - **direct** : `subject` + `html_content` + `sender`.

        Args:
            to: `[{"email": "a@b.c", "name": "Alex"}]` — max 99 destinataires.
            sender: `{"email": …, "name": …}`. Doit être un expéditeur **vérifié**
                du compte (cf. `brevo_account`), sinon Brevo refuse l'envoi.
            scheduled_at: ISO 8601 UTC, jusqu'à 72 h dans le futur.

        Pour un envoi de masse à une liste, c'est une campagne — pas ce tool.
        """
        return _client().send_email(
            to=to, subject=subject, html_content=html_content, sender=sender,
            template_id=template_id, params=params, cc=cc, bcc=bcc,
            reply_to=reply_to, tags=tags, scheduled_at=scheduled_at)

    @mcp.tool()
    def brevo_transactional_logs(
        email: Optional[str] = None,
        template_id: Optional[int] = None,
        message_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        uuid: Optional[str] = None,
    ) -> dict:
        """Emails transactionnels envoyés. `uuid` → renvoie le contenu HTML d'un envoi.

        Dates au format `YYYY-MM-DD`. Pour savoir ce qu'un email est DEVENU
        (délivré, ouvert, bounce), c'est `brevo_transactional_events`.
        """
        client = _client()
        if uuid:
            return client.get_transactional_email_content(uuid)
        return client.list_transactional_emails(
            email=email, template_id=template_id, message_id=message_id,
            start_date=start_date, end_date=end_date, limit=limit, offset=offset)

    @mcp.tool()
    def brevo_transactional_events(
        event: Optional[str] = None,
        email: Optional[str] = None,
        template_id: Optional[int] = None,
        days: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Événements de délivrabilité — la source de vérité par email.

        Args:
            event: `delivered` | `opened` | `clicks` | `hardBounces` | `softBounces`
                | `spam` | `blocked` | `unsubscribed` | `invalid` | `deferred` |
                `requests` | `error`. Omis = tous.
            days: fenêtre glissante en jours (alternative aux dates).
        """
        return _client().transactional_events(
            event=event, email=email, template_id=template_id, days=days,
            start_date=start_date, end_date=end_date, limit=limit, offset=offset)

    @mcp.tool()
    def brevo_transactional_report(
        by_day: bool = False,
        days: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict:
        """Compteurs agrégés du transactionnel (envoyés, délivrés, ouverts, clics…).

        `by_day=False` (défaut) = un total sur la période ; `by_day=True` = une
        ligne par jour.
        """
        return _client().transactional_report(
            by_day=by_day, days=days, start_date=start_date, end_date=end_date,
            tag=tag)

    @mcp.tool()
    def brevo_blocked(domains: bool = False, limit: int = 50,
                      offset: int = 0) -> dict:
        """Contacts bloqués (hard bounce, plainte spam, désinscription).

        `domains=True` → les domaines bloqués du compte. Diagnostic de
        délivrabilité : un contact bloqué ne reçoit plus rien, silencieusement.
        """
        return _client().list_blocked(domains=domains, limit=limit, offset=offset)

    @mcp.tool()
    def brevo_templates(template_id: Optional[int] = None, active_only: bool = False,
                        limit: int = 50, offset: int = 0) -> dict:
        """Templates transactionnels. `template_id` → un seul, avec son HTML."""
        return _client().list_templates(
            template_id=template_id, active_only=active_only or None,
            limit=limit, offset=offset)

    @mcp.tool()
    def brevo_create_template(
        template_name: str,
        subject: str,
        sender: dict,
        html_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        tag: Optional[str] = None,
        is_active: bool = True,
    ) -> dict:
        """Crée un template transactionnel. Renvoie `{"id": …}`.

        Args:
            sender: `{"email": …, "name": …}` — expéditeur vérifié du compte.
            html_content: HTML du corps. Variables : `{{params.NOM}}`,
                `{{contact.PRENOM}}`.
        """
        return _client().create_template(
            template_name=template_name, subject=subject, sender=sender,
            html_content=html_content, reply_to=reply_to, tag=tag,
            is_active=is_active)

    @mcp.tool()
    def brevo_update_template(
        template_id: int,
        template_name: Optional[str] = None,
        subject: Optional[str] = None,
        sender: Optional[dict] = None,
        html_content: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> dict:
        """Met à jour un template (champs fournis seulement)."""
        return _client().update_template(
            template_id, template_name=template_name, subject=subject,
            sender=sender, html_content=html_content, is_active=is_active)

    # --- Campagnes email --------------------------------------------------------

    @mcp.tool()
    def brevo_campaigns(
        campaign_id: Optional[int] = None,
        status: Optional[str] = None,
        statistics: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Campagnes email. `campaign_id` → une seule campagne.

        Args:
            status: `draft` | `sent` | `queued` | `suspended` | `archive` | `inProcess`.
            statistics: `globalStats` | `linksStats` | `statsByDomain` |
                `statsByDevice` | `statsByBrowser` — joint les stats.

        Le HTML est exclu des réponses (volume) ; il reste lisible dans l'UI.
        """
        client = _client()
        if campaign_id is not None:
            return client.get_campaign(campaign_id, statistics=statistics)
        return client.list_campaigns(
            status=status, statistics=statistics, limit=limit, offset=offset)

    @mcp.tool()
    def brevo_create_campaign(
        name: str,
        sender: dict,
        subject: Optional[str] = None,
        html_content: Optional[str] = None,
        template_id: Optional[int] = None,
        recipients: Optional[dict] = None,
        preview_text: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Crée une campagne en **brouillon**. Renvoie `{"id": …}`.

        Ne l'envoie pas : l'envoi (`sendNow`) n'est volontairement pas exposé —
        le départ se déclenche depuis l'UI Brevo, après relecture. Utiliser
        `brevo_campaign_test` pour se l'envoyer à soi d'abord.

        Args:
            sender: `{"email": …, "name": …}` — expéditeur vérifié.
            recipients: `{"listIds": [1,2], "exclusionListIds": [3]}`.
            template_id: partir d'un template plutôt que d'un `html_content`.
        """
        return _client().create_campaign(
            name=name, sender=sender, subject=subject, html_content=html_content,
            template_id=template_id, recipients=recipients,
            preview_text=preview_text, reply_to=reply_to)

    @mcp.tool()
    def brevo_update_campaign(campaign_id: int, fields: dict) -> dict:
        """Met à jour une campagne **non encore envoyée**.

        Args:
            fields: clés camelCase Brevo — `name`, `subject`, `htmlContent`,
                `sender`, `recipients`, `previewText`.
        """
        return _client().update_campaign(campaign_id, **fields)

    @mcp.tool()
    def brevo_campaign_test(campaign_id: int, email_to: list[str]) -> dict:
        """Envoie un test de la campagne aux adresses données (pas aux destinataires).

        ⚠️ Ces adresses doivent **exister comme contacts** du compte Brevo, sinon
        l'API refuse.
        """
        return _client().send_campaign_test(campaign_id, email_to)

    @mcp.tool()
    def brevo_campaign_report(campaign_id: int, ab_test: bool = False) -> dict:
        """URL publique de partage d'une campagne envoyée, ou son résultat d'A/B test."""
        client = _client()
        if ab_test:
            return client.campaign_ab_test_result(campaign_id)
        return client.campaign_shared_url(campaign_id)
