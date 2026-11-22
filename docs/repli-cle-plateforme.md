# Le repli sur la clé de plateforme : quand l'org a dit « je paie » sans déposer de clé

> Statut : livré en brouillon (19/11/2026). Section B de la spécification « Alertes de
> capacité » (`alertes-de-capacite.md`, branche de l'alerte), ajustée à ce qui est
> construit. Le repli sur la clé de l'org (OTO-130) est décrit dans
> `runner-et-automatisations.md`, § « Le REPLI vers la clé de l'org ». Élargi le
> 22/11/2026, sur choix de l'org, à un abonnement déconnecté : § « Quand se replier ».

## Le constat

Aujourd'hui le repli d'un forfait au plafond n'a qu'une cible : la clé que l'org a
déposée (`runner_jobs._cle_ok_pour_repli`). C'est la règle la plus stricte du dépôt, et
elle a raison : déplacer une dépense d'un abonnement gratuit vers des jetons facturés
exige que l'org l'ait dit. Une org qui fait tourner tous ses agents sur abonnement, sans
clé `anthropic` déposée, attend donc la réinitialisation de son forfait — des heures,
parfois des jours —, repli API allumé ou non.

## Ce qui est construit

**Une seconde manière de dire « je paie »** : le réglage d'org `repli_cle_plateforme`
(qui l'a posé, quand), posé par un org_admin, distinct de `repli_api` (qui reste un
booléen : le tableau de bord le lit). Allumé, un travail en pause de plafond d'une org
**sans clé déposée** rejoue sur la clé **partagée** de la plateforme, payé
`cle_plateforme` : chaque appel au modèle se réserve puis se règle au budget de crédits
de l'org du travail, comme n'importe quel travail servi sur cette clé
(`runner-et-automatisations.md`, § « La clé de modèle PARTAGÉE »).

### Les trois portes, toutes nécessaires

| porte | qui la tient | fermée |
| --- | --- | --- |
| `OTO_REPLI_CLE_PLATEFORME_PERMIS` | l'instance | le réglage ne s'ouvre pas (409), un réglage ouvert est ignoré |
| `repli_api` (ou `OTO_REPLI_API_PAR_DEFAUT`) | l'org | rien ne replie, quel que soit le payeur |
| `repli_cle_plateforme` | l'org (org_admin) | sans clé d'org, le travail attend, comme avant |

**Pourquoi `repli_api` reste exigé.** Le nouveau réglage choisit le PAYEUR, il ne
remplace pas le choix « se replier ou attendre ». Une org qui a coupé `repli_api` a dit
« mes travaux attendent » : lui faire payer un repli parce qu'elle avait, ailleurs,
accepté la clé de plateforme confondrait deux décisions. Le tableau de bord lit
`repli_api` comme l'interrupteur du repli ; il le reste.

### Qui paie, travail par travail

- **L'org a déposé sa clé `anthropic`** : elle paie sur SA clé (`cle_org`), inchangé, quoi
  que dise le réglage. Même si le coffre ne la rend pas : l'org paie déjà son
  fournisseur, elle ne bascule jamais sur la nôtre (le candidat est sauté, il attend).
- **Elle n'en a pas, et l'a choisi** : la clé PARTAGÉE de la plateforme, à condition que
  la remise puisse la servir — c'est la MÊME lecture à la prise et à la remise
  (`capabilities.runner_jobs._cle_de_plateforme_du_repli`) :
  - la clé partagée est **accordée** à l'org (la clé `anthropic` de plateforme est
    fermée : l'accord est le droit, `oto_admin_key_grant`), résolue au nom du demandeur ;
  - à un palier que l'opérateur **refacture** (`credits.rang_refacture` : la plateforme ou
    le tenant primaire) — la clé d'un tenant non primaire, ce tenant la paie, et il n'a
    rien demandé ;
  - servie à un worker qui **mesure** chaque appel (`meters_usage`), jamais à la ferme ;
  - sous la limite de dépense du jour, au-dessus du plancher de crédits d'un travail, sur
    une instance où les appels au modèle ne sont pas en mode `off` ;
  - le modèle replié **tarifé** au barème (ses quatre lignes, `credits_tours.modele_tarife`) :
    sans prix, il serait servi sans rien régler (`report`) ou arrêté au premier tour
    (`enforce`) — ni l'un ni l'autre n'est le run payé que l'org a choisi ;
  - réglé au compte de l'**org** (`org:<id>`) : une org dont le payeur est la personne
    (`credit_payer_modes = person`) ferait payer le DEMANDEUR, que le réglage d'un
    org_admin n'engage pas — elle attend.
- **Jamais la clé d'environnement du worker** : le `return job` sans clé de la remise
  ordinaire la ferait payer à la plateforme sans que rien ne la mesure.

Une org qui a ouvert le réglage mais à qui la clé partagée n'est pas accordée **attend**
(elle ne s'arrête pas) : le réglage ne donne pas l'accès. Voir les questions ouvertes.

### Bornes, les mêmes que le repli sur la clé de l'org

- même cas que le repli sur la clé de l'org : `paused_limit` à échéance future, en
  personnel comme en pool (tous les prêteurs) ; `needs_login` / `disconnected` seulement
  si l'org a choisi `plafond_ou_deconnexion` (§ « Quand se replier »), jamais un pool
  vide ;
- même plafond de jetons par run (`REPLI_MAX_TOKENS`, sauf plafond déclaré par l'agent),
  un seul repli en vol par forfait, même modèle au même palier ;
- même pré-filtrage SQL avant la limite des candidats (`candidats_repli_abonnement`) :
  l'interrupteur `repli_api`, ce qui paie, le forfait déjà en vol. L'accès à la clé
  partagée (accord, palier, prix du modèle, crédits), lui, passe par la cascade des clés,
  qui n'a pas d'écriture SQL : il se juge après la limite, candidat par candidat. D'où
  **deux lectures, chacune sous sa limite** : les orgs qui paient sur leur clé, puis —
  instance qui permet — les orgs opt-in, réduites au plus ancien travail par forfait,
  demandeur et modèle (un seul repli vole par forfait). Un opt-in que la plateforme ne
  peut pas payer ne cache donc jamais une org qui paie sur sa clé. Limite connue : au-delà
  de 25 forfaits opt-in rejetés plus anciens, un autre opt-in payable attend son tour ;
- la prise (`claim_fallback_job`) revérifie dans son `WHERE` la pause, et pour ce payeur
  `repli_api`, le réglage et l'absence de clé d'org : un admin qui referme, ou une org qui
  dépose sa clé, entre la lecture et la prise, gagne.

### Le stamp

Chaque repli écrit son payeur dans la charge du travail, à côté du reste du repli :
`payload._plateforme.repli.payeur` = `cle_org` | `cle_plateforme`. Un repli pris avant le
champ se lit `cle_org` (`runner_jobs.payeur_du_repli`).

### La remise : servi comme décidé, ou défait

Un travail replié sur la clé de plateforme a exactement deux issues à la remise
(`_servir_le_repli_sur_la_plateforme`) :

- **servi** sur la clé partagée, estampillé (`_plateforme.cle_partagee`), `meter_usage` ;
- **défait** (`db.defaire_le_repli`) dans tout autre cas — accès retiré, permission
  retirée, worker qui ne mesure pas, ferme, plancher, limite du jour, coffre illisible, ou
  une clé que l'org a déposée depuis la prise (le stamp mentirait : le sondage suivant le
  rejoue sur elle). Le travail retrouve son abonnement et la file, tentative rendue ; le
  repli défait reste lisible (`_plateforme.repli_defait`, avec sa raison).

Aucune des autres issues de la remise ordinaire ne lui va : l'arrêter tuerait un travail
qui avait de quoi attendre ; le rendre à la file le laisserait estampillé `anthropic` ; le
servir sans clé le ferait tourner sur la clé du worker.

## La preuve de facturation

Le test `tests/test_repli_cle_plateforme.py::test_PREUVE_le_run_replie_est_compte_cle_plateforme_pour_l_org_du_travail`
joue, contre une vraie base, les verbes du worker — `claim` (en promettant la mesure),
`reserve`, `meter`, `complete` — sur un travail d'abonnement au plafond d'une org sans
clé, avec une clé de plateforme posée et accordée et un barème réel. Il lit :

- au **grand livre de crédits** : un règlement au payeur `org:<org du travail>`, rang
  `platform`, fonction `messages:<modèle replié>`, rapproché du travail
  (`meta.runner_job_id`), et le solde de l'org débité d'autant — c'est ce que le
  commerce facture ;
- au **relevé de la clé de plateforme** (`credits.jetons_du_jour`, ce que
  `platform_model_spend` rapproche de l'Admin API d'Anthropic, `anthropic_usage`) : les
  jetons sous le rang `platform` ;
- à la **lentille `org.usage.tokens`** : le travail conclu, `paid_by = cle_plateforme`.

⚠️ Ce dernier point a demandé un changement : `result.paye_par` est DÉCLARÉ par le
worker (oto-runner) et rien dans ce dépôt ne l'écrit. `jetons_des_travaux` lit désormais,
pour un travail replié, le payeur que la PRISE a stampé (`_plateforme.repli.payeur`)
avant la déclaration du worker : c'est le serveur qui a choisi la clé et l'a remise.

## Surfaces

- REST : `GET /api/orgs/{id}/model-subscriptions/{family}` rend `platform_key_fallback`,
  `platform_key_fallback_permitted`, `platform_key_fallback_set_by`,
  `platform_key_fallback_set_at` (le réglage TEL QU'IL EST, même si l'instance ne le
  permet plus : l'écran dit les deux). `PUT …/{family}/platform-key-fallback`
  `{"platform_key_fallback": true|false}`, org_admin ; `true` refusé
  `409 platform_key_fallback_not_permitted` sur une instance qui ne le permet pas, `false`
  toujours accepté.
- MCP : `oto_org_settings domain=model_subscriptions platform_key_fallback=true|false`,
  seul dans son appel (`one_setting_per_call`).
- Diagnostic d'une péremption (`_abonnement.raison_de_peremption`, pool au plafond) : sur
  une instance qui le permet, et sans clé d'org, le texte dit si le repli sur la clé de
  plateforme était fermé, ou ouvert sans avoir servi à temps (un repli en vol par
  forfait, l'accès, les crédits). Ailleurs, les textes d'avant, mot pour mot.

## Schéma

Fragment `db/schema/runs.py::MODEL_SUBSCRIPTION_REPLI_PLATEFORME` (dans `_SCHEMA`, joué
au démarrage sous garde de catalogue, figé dans `tests/schema_gele/`), révision
`20261122_1100_repli_plateforme` : trois colonnes sur `org_model_subscription_modes`,
`repli_cle_plateforme BOOLEAN NOT NULL DEFAULT FALSE`, `repli_cle_plateforme_by TEXT`
(colonne de compte, repointée par `migrate_sub`), `repli_cle_plateforme_at TIMESTAMPTZ`.
Ordre avec le code indifférent. Une ligne que le réglage fait NAÎTRE a `mode` NULL et
`repli_api` au défaut de l'instance — naître à `FALSE` sur une instance ouverte
fermerait le repli de l'org par effet de bord.

## Quand se replier (22/11/2026)

### Le constat

Le repli ne partait que d'un forfait AU PLAFOND — décision délibérée d'OTO-130 et de sa
revue (#1086) : un abonnement à reconnecter attend la personne, le repli « ne répare rien
de ça ». Le 19/11/2026, le seul abonnement prêté d'une org s'est déconnecté : tous ses
agents se sont arrêtés pendant des heures, repli allumé. Pour l'org, un
abonnement qui ne peut pas servir est un abonnement qui ne peut pas servir, plafond ou
déconnexion.

### Ce qui est construit

Un réglage d'org, posé par un org_admin, **à part** des deux autres :
`org_model_subscription_modes.repli_quand` (qui `_by`, quand `_at`), révision
`20261122_1200_repli_quand`.

| `repli_quand` | servi (`fallback_when`) | le repli part de |
| --- | --- | --- |
| `plafond` (défaut, toute org d'avant) | `limit` | la règle d'avant, à l'identique |
| `plafond_ou_deconnexion` | `limit_or_signed_out` | la règle d'avant, ET un abonnement déconnecté |

Il n'ouvre pas le repli (`repli_api` reste la porte : fermée, rien ne replie) et ne
choisit pas le payeur (la clé de l'org ; sinon, si l'org l'a choisi et que l'instance le
permet, la clé de plateforme — mêmes portes que ci-dessus). Il n'a donc besoin d'aucune
permission d'instance. Les trois réglages composent le choix que l'écran présente d'un
seul geste : « quand les abonnements ne peuvent pas tourner (déconnectés ou au plafond) :
attendre / la clé de l'org / les crédits de la plateforme ».

### Ce qui devient repliable sous `plafond_ou_deconnexion`

« Déconnecté » = `needs_login` ou `disconnected`, avec un sandbox posé (une ligne sans
sandbox n'a jamais été connectée : rien à reconnecter, la lecture de
`_abonnement.reparable`), **depuis au moins dix minutes** (`DECONNEXION_TENUE_S`, sur
`updated_at`) : le repli paie des jetons, un `needs_login` le temps d'un jeton qui se
renouvelle ou une déconnexion défaite dans la minute ne doivent pas acheter un run.

- **voie personnelle** : l'abonnement du demandeur OUVERT dans l'org du travail
  (`_ABONNEMENT_OUVERT_ICI`) est déconnecté ;
- **voie du pool** : au moins un prêteur vivant (`PRET_VIVANT`), et CHACUN est en pause
  pour l'org (la règle d'avant) OU déconnecté — un pool mélangé plafond + déconnexion se
  replie, un pool tout déconnecté aussi ;
- **sans mode réglé** (le pool complète) : un demandeur SANS abonnement ouvert ici n'a que
  le pool. Quand aucun prêteur ne peut servir, la réservation le renvoie sur une voie
  personnelle où il n'a rien, et la règle d'avant n'y voyait aucun plafond — il attendait
  donc toujours, plafond compris. Le repli, lui, remplace le pool qu'il aurait emprunté
  (`voiep`, forfait `org:`), au plafond comme déconnecté. ⚠️ C'est le seul endroit où la
  voie du REPLI diffère de celle de la RÉSERVATION (`_par_le_pool`, inchangée).

### Ce qui attend toujours, dans les deux réglages

- un pool sans aucun prêteur vivant (une absence, pas une panne) ;
- un demandeur sans abonnement, hors du cas « sans mode réglé, son pool » : en mode
  `personnel`, ou sans prêteur, il n'y a rien à remplacer ;
- un prêteur SERVABLE : le pool n'est pas bloqué, il sert ;
- un modèle ÉTEINT par l'org (`orgs.disabled_models`) — celui du travail ou celui
  d'arrivée (`_modeles_allumes`) : un modèle éteint s'arrête, il ne se replie jamais.
  ⚠️ Ce garde-fou vaut aussi sous `plafond` : avant lui, le repli reroutait un `sub:*`
  éteint vers un modèle allumé, ou arrêtait à la remise un travail replié vers un modèle
  éteint au lieu de le laisser attendre ;
- `repli_api` fermé, une org sans payeur, et les bornes d'avant (`REPLI_MAX_TOKENS`, un
  repli en vol par forfait, même modèle au même palier).

### La prise, la reconnexion, le stamp

- **Une seule écriture** (`runner_jobs._jointures_de_repli`, `_REPLIABLE`,
  `_modeles_allumes`), lue par les candidats ET revérifiée dans le `WHERE` de la prise,
  pour TOUT payeur : un admin qui repasse à `plafond`, une personne qui se reconnecte, un
  modèle éteint entre la lecture et la prise, gagnent.
- **Reconnecté**, l'abonnement n'est plus déconnecté : les travaux suivants ne sont plus
  candidats et repartent sur lui. Un travail déjà replié est un travail `anthropic`
  ordinaire : il finit sur le payeur stampé. `defaire_le_repli` rend la charge d'avant à
  l'octet, quelle que soit la cause.
- **Le stamp dit pourquoi** : `_plateforme.repli.cause` = `plafond` exactement quand la
  règle d'avant l'aurait replié, `deconnexion` sinon (un pool mélangé est une
  `deconnexion`) ; `reason` = `paused_limit` | `signed_out` ; `reset_at` = l'échéance du
  plafond, nulle pour une déconnexion, qui n'en a pas. Un repli pris avant le champ se lit
  `plafond` (`runner_jobs.cause_du_repli`).
- **Sous `plafond`, chaque clause se réduit à celle d'avant** : `quand` est faux, la voie
  du repli est celle de la réservation, et une pause porte toujours son échéance — les
  bancs d'avant (`test_repli_plafond_api.py`, `test_repli_cle_plateforme.py`) passent sans
  retouche.

### Surfaces

- REST : `GET /api/orgs/{id}/model-subscriptions/{family}` rend `fallback_when`
  (`limit` | `limit_or_signed_out`), `fallback_when_set_by`, `fallback_when_set_at` —
  membres compris. `PUT …/{family}/fallback-when` `{"when": "limit" |
  "limit_or_signed_out"}`, org_admin.
- MCP : `oto_org_settings domain=model_subscriptions fallback_when=limit|limit_or_signed_out`,
  seul dans son appel (`one_setting_per_call`).
- Diagnostics : la péremption d'une occurrence (`_abonnement.raison_de_peremption`) et
  l'alerte des agents qui attendent un abonnement à reconnecter ne parlent que de travaux
  qui n'ont PAS tourné — une occurrence périmée, un travail en file depuis trente minutes
  sans succès depuis : un travail replié est réservé, sa famille devient `anthropic`, il
  n'y figure pas. Textes inchangés.

## Questions ouvertes

- **Le réglage doit-il valoir accord ?** Aujourd'hui, non : la clé `anthropic` de
  plateforme est fermée, et l'org qui ouvre le réglage sans accord attend. Le bouton
  « Tourner sur les crédits de plateforme » de l'alerte de capacité (A) devra soit
  vérifier l'accord, soit le demander.
- **Le diagnostic en mode personnel** ne parle d'aucun repli (ni clé d'org, ni
  plateforme) : seul le pool au plafond le fait, comme avant.
- **Les orgs à payeur « personne »** attendent : faut-il plutôt un consentement de la
  personne (son propre réglage), ou laisser le réglage de l'admin engager chaque membre ?
- **Dix minutes de déconnexion** avant de replier (`DECONNEXION_TENUE_S`) : le bon
  délai entre « un jeton qui se renouvelle » et « des heures sans un passage » n'est pas
  mesuré ; c'est une constante, pas un réglage.
- **`enforce` sans plancher** (`OTO_CREDITS_JOB_FLOOR` absent ou 0) : une org au solde
  nul passe la prise, puis son premier tour est refusé (`insufficient_credits`) et le
  travail s'arrête au lieu d'attendre — comme tout travail sur la clé partagée. Un
  plancher posé ferme ce cas (la prise ne le prend pas, la remise le défait).
