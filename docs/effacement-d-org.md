---
title: Effacer une org — entière, confirmée, vérifiée
type: reference
description: >-
  L'effacement définitif d'une org (RGPD art. 17) : demandé par un admin de l'org ou
  par un super admin pour le compte du client, confirmé par le titulaire depuis un lien
  à usage unique, suspendu sur-le-champ, purgé au terme d'un délai de grâce de 7 jours
  par le travail de fond `oto-mcp maintenance effacements`. Ce qui part (chaque table
  du classement de l'export, plus une décision écrite pour chacune de celles qu'il
  laisse), ce qui reste et pourquoi (pièces comptables, preuve d'acceptation des CGU
  sans IP), les comptes qui partent avec l'org (ceux dont c'était la seule), les
  gestes hors base (stockage, Unipile, Logto, archives froides du journal), le reçu, et
  la face du commerce.
---

# Effacer une org

`oto_mcp/effacement/`, capacité `org.erasure` (`capabilities/org_erasure.py`). Décisions
du 12/11/2026 : déclenché par un admin de l'org **ou** par un super admin pour le compte du
client ; confirmé par le titulaire, par mail ; **7 jours** de grâce ; un compte dont c'était
la **seule** org part avec elle ; l'effacement d'une PERSONNE seule (ADR 0062-D2) est un
autre geste, à venir.

⚠️ Ce geste **renverse** la règle « aucun hard-delete, les FK restent pour l'audit » —
pour ce seul chemin, et sous ses gardes. Archiver (`org.archive`), suspendre
(`docs/orgs-suspendues.md`), mettre un compte en pause (`docs/comptes-en-pause.md`)
restent des gestes qui ne détruisent rien.

## Le cycle

| état | qui | ce qui se passe |
|---|---|---|
| `requested` | admin de l'org (`via=owner`) ou super admin (`via=operator`, `reason` exigé) | le périmètre est vérifié, un lien `/erasure/<jeton>` (72 h, usage unique, seule l'EMPREINTE est gardée) part chez le **titulaire** : le créateur de l'org s'il en est admin, sinon l'admin le plus ancien. Rien n'est suspendu. |
| `scheduled` | le titulaire, connecté sous SON compte | il voit le plan (`op=preview`), confirme (`op=confirm`) : les refus se rejouent, le périmètre est **figé**, l'org est **suspendue**, la purge est fixée à `confirmed_at + 7 j`. |
| `cancelled` | admin de l'org ou super admin | possible jusqu'au début de la purge ; la suspension posée par la confirmation est levée (et elle seule : une suspension d'une autre origine reste). |
| `expired` | — | lien non utilisé en 72 h. |
| `running` | `oto-mcp maintenance effacements` | les étapes ci-dessous, chacune notée au reçu ; un échec garde l'état et rejoue au passage suivant. |
| `failed` | — | 5 échecs : un opérateur lit `last_error`. |
| `done` | — | reçu écrit ; nom de l'org, adresse de confirmation et périmètre retirés de la demande ; mail « c'est fait ». |

Surfaces (REST seule — le jeton est un secret porteur, aucune face MCP ne le reçoit) :
`POST /api/orgs/{id}/erasure` (`op=plan|request|cancel|status`),
`POST /api/org-erasures/confirmation` (`op=preview|confirm`, jeton dans le CORPS),
`GET /api/service/org-erasures`, `POST /api/service/org-erasures/{id}/commerce-erased`.
`org.erasure` et `org.erasure_confirm` restent servies à une org **suspendue**
(`org_suspension.OUVERTES`) : c'est suspendue qu'on suit ou annule, et une org arrêtée en
fin d'essai est la première à vouloir partir.

## Refus (avant le lien, et rejoués à la confirmation)

`subscription_active` (un abonnement qui prélève : résilier d'abord), `operator_account`
(un compte d'opérateur n'a que cette org), `tenant_admin_account` (un admin de tenant n'a
que cette org), `no_owner` (aucun admin joignable), `already_erased`, `erasure_pending`.

## Le périmètre (`effacement/perimetre.py`)

L'org, ses équipes, et toute ligne de l'org quel que soit le membre (`ParOrg`, `member`
`'<org>:<sub>'`). Les **partants** — les membres dont c'est la seule org — partent
entiers (`ParSub`, `user`, `ParSubSansOrg`) ; les **restants** gardent leur compte et ce
qui est à eux hors de l'org (leurs objets personnels, même créés dans l'org, restent à
eux). Le tenant n'est jamais du périmètre. Le périmètre est figé à la confirmation dans
`org_erasures.snapshot` : la purge efface les appartenances qui le définissent.

## Ce que devient chaque table (`effacement/disposition.py`)

Le classement de l'export (`export_perimetre/classement.py`) dit déjà quelles lignes
appartiennent à une org, et refuse une table non classée. L'effacement y ajoute UNE
décision : une table exportée est **effacée** (sauf `EXCEPTIONS`) ; une table que
l'export laisse (`exclue`, `instance`) est tranchée **une à une** dans `NON_EXPORTEES` —
une table sans décision REFUSE l'effacement (`disposition.verifier`, banc
`tests/effacement/test_purge_org.py`). ⚠️ **Ajouter une table au schéma, c'est la classer
ET, si l'export la laisse, trancher son effacement, dans le même commit.**

Ce qui reste, à dessein :

- **pièces comptables** (`org_subscriptions`, `billing_*`) : 10 ans (Code de commerce
  L123-22). Elles désignent l'org par clé étrangère, d'où la **pierre tombale** : la ligne
  `orgs` reste, renommée `org effacée`, toute colonne hors `PIERRE_TOMBALE_GARDE` remise à
  son défaut (une colonne ajoutée demain est vidée sans y penser), archivée, suspendue ;
- **preuve d'acceptation de nos CGU** (`legal_acceptance_events`) : la ligne reste sous
  le seul identifiant du compte, **sans IP ni navigateur** (prescription, 5 ans) ;
- **écritures du budget d'unités** (`credit_balances`, `credit_movements`) : sous un
  identifiant de payeur opaque ;
- **la demande elle-même** (`org_erasures`) : la preuve (RGPD art. 5 §2), qui ne garde à
  la fin que des comptes et des dates.

Un PARTAGE (`destinataire` du classement) part si sa ressource **ou** son destinataire est
de l'org — l'export exige les deux.

## La purge (`effacement/purge.py`, `effacement/travail.py`)

Hors du processus MCP : le timer quotidien de maintenance (**prod seulement** — la préprod
partage la base et ne doit jamais purger), **fermé par `OTO_EFFACEMENTS`** : absente, le
travail liste ce qu'il ferait et n'écrit rien. Dans l'ordre, chaque étape notée au reçu :

0. **périmètre revu** : un partant qui a rejoint une autre org pendant le délai de grâce
   devient restant et garde son compte (la liste ne fait que rétrécir) ; les refus sur
   les comptes (opérateur, admin de tenant) se rejouent ;
   **répétition** : la purge entière jouée puis ANNULÉE dans une seule transaction
   (journal sauté) — une clé étrangère qu'une ligne restante pose sur une ligne effacée
   lève ici, nommée, AVANT tout geste irréversible hors base ;
1. **objets** du stockage que l'org désigne (clés de fichiers, pièces jointes de l'agent
   intégré, avatar d'un partant, logo) — `media_store.supprimer_objet`, qui LÈVE ;
2. **Unipile** : un siège sur la clé plateforme que plus aucune org ne sert est rendu
   (supprimé chez Unipile — on le paierait, la session y survivrait) ; servi ailleurs, il
   est seulement délié ; sur la clé propre de l'org, il est à elle ;
3. **organisation Logto** miroir (double authentification), si l'org en a une ;
4. **comptes de connexion** des partants (`annuaire.supprimer_compte`) et leurs jetons
   `oto_` ; un compte d'un annuaire de tenant tiers n'est pas le nôtre : compté
   (`a_faire_par_le_tenant`). ⚠️ Puis **attente d'une heure** (`ATTENTE_APRES_COMPTES`) :
   un JWT déjà émis vit encore, et son premier appel recréerait la ligne `users` — la
   purge se termine donc au tir du lendemain ;
5. **base** : un restant dont l'org active était l'org effacée bascule sur sa plus
   ancienne org non archivée (le geste d'`archive_org`) ; la pierre tombale EN PREMIER
   (elle vide des colonnes qui désignent des lignes effacées, `kb_project_id`) ; puis les
   ENFANTS avant leurs parents (FK et `Via`), par lots de 5 000 (`ctid`), chaque lot sa
   transaction ; enfin le recomptage — un résidu relance une passe, un résidu après deux
   passes est un échec nommé. Une table qui se désigne ELLE-MÊME est tranchée dans
   `AUTO_REFERENCES` (refus sinon) : un partage dérivé d'un partage effacé part aussi
   (il élargirait un accès), une instance de connecteur héritée est détachée ;
6. **archives froides du journal** (`effacement/archives.py`) : chaque mois archivé depuis
   la création de l'org est relu en flux et redéposé SANS ses lignes, sous une clé neuve
   (une archive n'est jamais écrasée), recompté, repointé dans `journal_archives`, puis
   l'ancien objet retiré — sous le verrou consultatif de l'archivage ;
7. **done**.

Les objets du stockage sont ceux des colonnes de clé, des colonnes d'URL, ET de toute URL
de notre stockage citée dans le TEXTE d'une ligne effacée (une image déposée par un agent
n'a d'autre trace) ; les archives froides sont recopiées OCTET POUR OCTET pour les lignes
gardées (le CSV de PostgreSQL distingue NULL de `""`, le module `csv` non).

Pendant le délai de grâce, la suspension posée par la confirmation **ne se lève que par
l'annulation** : `op=resume` (super admin, commerce) refuse en 409 `erasure_scheduled`.

Ce que l'effacement ne touche pas, et le dit : les **sauvegardes** de la base expirent selon
leur rétention (dépôt `infra`) ; les événements **Sentry** selon la leur ; le **bac à
sable** d'un abonnement de modèle (`user_model_subscriptions.sandbox_id`) vit hors base et
n'est pas détruit par ce geste. Un passage en échec est journalisé (Sentry) et rejoué ;
au-delà de 5, la demande passe en `failed` et attend un opérateur — aucune alerte dédiée.

## Le commerce

`service.org.erasures` liste ce qu'il n'a pas acquitté : `scheduled` → rien (encore
restaurable) ; `running` → résilier ; `done` → effacer sa part (ses tables par org, le client Stripe — les
factures émises restent chez Stripe), puis `service.org.erasure_ack`.
