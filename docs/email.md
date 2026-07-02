# Email — envoi per-org, par connecteur

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Envoi d'email modélisé **par connecteur** (la config/gestion email s'exprime comme
celle d'un connecteur, pas une page à part). **Deux connecteurs** (`providers.py`) :
`scaleway` (**BYO-org depuis le 2026-07-01** : `auth_modes={byo_org}`,
`secret_kind="fields"` — `secret_key`+`project_id`+`region` du compte Scaleway TEM
de L'ORG ; transport = API TEM en direct `email.send_via_scaleway_tem`, plus de
service mailer ni de clé plateforme ; master ON **sûr** car la propriété du domaine
est garantie PAR Scaleway — l'API refuse un `from` dont le domaine n'est pas vérifié
dans le compte de l'org, ce qui rend #64 caduque) + `resend` (BYOK,
`auth_modes={byo_org}`). **Le transport DÉRIVE du connecteur** :
`providers.EMAIL_CONNECTOR_TRANSPORT={scaleway:scaleway, resend:resend}` (pas de
champ transport sur l'expéditeur).

- `email_send` (`tools/email.py`) = **spine** (pas un connecteur) : route
  `sender→connecteur→transport` ; autz dynamique (membre d'org pour une adresse
  déclarée ; super_admin pour le repli marque `oto@otomata.tech`). `email.py` =
  `send_composed_email` (mailer.oto.zone, env `OTO_MAILER_SEND_BEARER`) +
  `send_via_resend` (httpx direct, clé org). `scaleway`/`resend` = providers
  credential/config-only (`tools/{scaleway,resend}.py` = `register()` no-op).
- **Config = `orgs.email_settings` JSONB keyé PAR CONNECTEUR** :
  `{<connector>:{senders:[{email,name?,reply_to?}], quiet_hours?}}` (calqué sur
  `field_filters`). `org_store.get/set_org_email_settings(org, connector)`,
  `resolve_sender(org, from)→(sender, connector)`, `org_email_quiet_hours`. Capacité
  `orgs_email_settings` : GET bundle + `PUT /api/orgs/{id}/email-settings/{connector}`.
- **Envoi différé** : params `send_at`/`force_now` + garde-fou **quiet hours par
  connecteur** (défaut Europe/Paris 20h–8h). `scheduler.py` : `compute_scheduled_at`
  (pure, testée) + boucle asyncio démarrée via le lifespan (`server.py`), batch isolé
  en `asyncio.to_thread` (ne bloque pas l'event loop) ; table `scheduled_emails`
  (claim `FOR UPDATE SKIP LOCKED`, retry ×3). Gestion : `oto_list/cancel_scheduled_emails`.
- **Vérif de domaine d'envoi = déléguée au provider** (les deux connecteurs sont
  BYO) : Scaleway TEM comme Resend refusent un `from` hors domaine vérifié dans le
  compte de l'org → pas de vérif côté oto (#64 sans objet depuis le passage BYO).
  Otomata (org 2) envoie avec sa clé TEM dédiée (app IAM `oto-email-scaleway`,
  vault `SCW_TEM_*`).

> **Invariant connecteurs (corrigé 2026-06-24)** : `_org_list` (vue ORG
> `/org/connectors`) ne liste QUE les connecteurs **activés par la plateforme**
> (master ON, ou forcé par l'override d'org), comme la surface USER
> (`_visible_catalog`). Master-OFF non accordé → invisible (fin du levier inerte
> « coupé par la plateforme »). Filtre sur le **cap master**, pas sur `effective`
> (un override OFF d'org doit rester réactivable).
