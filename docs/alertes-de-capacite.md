# Alertes de capacité : quand les agents d'une org ne peuvent plus démarrer

> Statut : spécification (19/11/2026). Deux PR : **A** l'alerte (cette branche), **B** le
> repli sur la clé de plateforme (branche séparée, en brouillon).

## Le constat

Une org fait tourner tous ses agents sur abonnement (`claude_subscription`), en mode
**pool** (`org_model_subscription_modes.mode = pool`), avec **un seul prêteur**. Son
forfait passe le seuil de l'org (80 %) en début d'après-midi : `paused_limit`, échéance
dans plusieurs jours. Pendant huit heures :

- aucun travail de l'org ne démarre — programmés, webhooks, chaînes ;
- **aucune alerte ne part**, ni au client ni à nous ;
- le repli API (`repli_api`, OTO-130) est allumé, mais l'org n'a pas déposé de clé
  `anthropic` : il n'a rien sur quoi rejouer, et c'est voulu (`_cle_ok_pour_repli`) ;
- l'agent « veilleur » que l'org s'était donné tourne… sur le même pool : il s'arrête
  avec les autres.

Le client l'apprend d'un humain, le soir. Le lendemain matin, sa liste d'appels de 7 h 45
ne serait pas sortie.

## Pourquoi les alertes existantes se taisent

Les alertes d'agents (`alerte_agents_bloques.py`, `alertes_slack_agents.py`) partent d'un
**épisode** (`db/episodes_agents.py`) : les travaux **morts** consécutifs d'un agent.

1. **Un travail qui attend n'est pas mort.** Il reste `pending` jusqu'à sa péremption ;
   une péremption (`expired`) n'est pas morte non plus (`issues_des_travaux.mort()` :
   `failed`, ou `done` conclu `blocked`). Aucun épisode ne s'ouvre, jamais.
2. **La cause est celle de l'org, pas d'un agent.** Un forfait au plafond arrête 24 agents
   d'un coup. Une alerte par agent dirait 24 fois la même chose — ou rien.
3. **La raison existe, mais seulement dans le texte de péremption**
   (`_abonnement.raison_de_peremption`), et seulement quand l'occurrence suivante
   arrive : 24 h plus tard pour un agent quotidien.

## A — l'incident de capacité d'une org (cette PR)

### Ce qu'on détecte

Pour chaque org et chaque famille d'abonnement : **des travaux `pending` dus, qu'aucun
abonnement ne peut servir maintenant**, avec les MÊMES prédicats que la réservation
(`PRETEUR_SERVABLE`, `sql_par_le_pool`, `sql_propre_sert`) — jamais l'âge seul : une file
d'une place derrière un forfait servable n'est pas une panne, elle avance.

Un incident est ouvert quand la condition tient depuis `DELAI_MIN` (15 min) sans
interruption, et qu'aucun repli ne la lève (repli API allumé avec une clé lisible : les
travaux repartent, ce n'est pas un incident). La cause est l'une de :

| cause | quand | qui agit |
| --- | --- | --- |
| `plafond` | tous les forfaits utilisables sont `paused_limit` à échéance future | le titulaire attend, ou l'org ajoute de la capacité |
| `reconnexion` | au moins un forfait `needs_login` / `disconnected`, aucun servable | le titulaire se reconnecte |
| `pool_vide` | mode pool, aucun prêt vivant | un membre partage son forfait |

Il est **clos** quand un travail de cette famille de l'org est réservé (ou que plus rien
n'attend), et on dit alors combien d'occurrences ont été manquées pendant l'incident
(péremptions `expired` de l'org entre ouverture et clôture).

### Ce qu'on dit, et à qui

- **Un message par incident et par org** sur le Slack de l'org, à la destination des
  alertes d'agents (`alertes_slack`, réglage existant), par la même app ; à défaut, le
  courriel aux admins (comme `alerte_agents_bloques`). Contenu : depuis quand, combien
  d'agents allumés attendent, la cause **au niveau de discrétion de
  `raison_de_peremption`** (« le forfait partagé est au plafond », jamais le nom du
  prêteur, son palier ni son usage — `preteurs()` ne les sert pas, l'alerte non plus),
  l'heure de reprise quand elle est connue (`pool_au_plafond.reprise`), et les gestes :
  « Partager un forfait », « Ajouter une clé d'API » (page Agents › Modèles).
- **Au titulaire du forfait**, par courriel de plateforme : le détail que lui seul peut
  voir (plafond et échéance, ou reconnexion) et le lien de reconnexion. C'est la seule
  personne qui peut lever `reconnexion`.
- **Une ligne à la clôture** : « Les agents ont repris à HH:MM. N occurrences manquées
  pendant l'arrêt. »

Pas d'IA dans ce texte (comme `alertes_slack_agents`) : il est fixe, traduit (fr/en),
sous la marque du tenant.

### Conventions reprises

Réglage d'instance (`OTO_ALERTES_CAPACITE_ENABLED`, lu à chaque passage), boucle de fond
`tiers=True`, `org_a_nous` (une org d'un tenant tiers est écartée), heures calmes,
`verrou_de_boucle`, **marque après l'envoi** (un envoi raté se rejoue), schéma par le
patron fichier-pour-fichier (constante DDL, boot DDL, migration, `tests/schema_gele/`).

### Ce que fait le code, là où il s'écarte de ce qui précède (19/11/2026)

Le code : `oto_mcp/alertes_capacite.py` (la boucle, les textes, les envois),
`db/incidents_capacite.py` (la table `org_incidents_capacite`, révision
`20261119_2240_incidents_capacite`), `db/runner_jobs.travaux_sans_forfait` (le constat),
`_abonnement.cause_du_forfait` (la cause). Bancs : `tests/test_alertes_capacite.py`.

- **La clôture se lit sur le constat, pas sur « un travail est réservé ».** Un worker qui
  sonde le forfait (`forfait_sonde`) prend le travail d'un forfait au plafond toutes les
  dix minutes (`VERIFICATION_S`), le sonde et le rend à la file : une réservation qui
  clôt ferait que l'incident ne tiendrait jamais le quart d'heure. Et en voie
  personnelle, un propriétaire servi pendant qu'un autre est au plafond rouvrirait et
  refermerait l'incident en boucle. L'incident est donc clos quand plus aucun travail
  n'attend sans forfait, **constaté deux passes de suite** (`DELAI_CLOTURE_S`) : une passe
  qui tombe pendant la sonde ne voit rien attendre sans que rien soit reparti. Un travail
  rendu à la file par la sonde (dû dans `VERIFICATION_S`) compte comme dû.
- **« Plus rien n'attend » n'est pas une reprise.** Un webhook périme à sa fraîcheur, une
  file se vide à la main, un agent s'éteint : si la cause tient encore (le pool n'a
  toujours aucun prêteur servable, un titulaire est toujours au plafond ou à
  reconnecter), l'incident se clôt `repris = false` et aucune ligne « les agents ont
  repris » ne part. Le travail suivant qui attend ouvre un incident neuf.
- **Sans mode réglé, le pool qui complète compte.** Un demandeur dont l'abonnement ne sert
  pas ici passe par le pool dès qu'un prêteur est servable, et la prise le fait attendre
  tant qu'aucun ne l'est : des prêts vivants dont aucun ne sert, c'est un incident de la
  voie du pool (sa cause, ses prêteurs). ⚠️ Le texte de péremption, lui, choisit la voie
  par `par_le_pool`, qui dit « personnelle » quand aucun prêteur n'est servable : pour ce
  cas il dit « pas ouvert dans cette organisation ». Même classement
  (`cause_du_forfait`), voie différente ; non retouché ici.
- **Un pool mêlé est `plafond`.** Un prêteur au plafond et un à reconnecter : le tableau
  ci-dessus dirait `reconnexion` ; le texte de péremption dit `plafond`, et il n'y a qu'un
  classement (`cause_du_forfait`, sorti de `raison_de_peremption`). `reconnexion` en pool,
  c'est qu'aucun prêteur n'est connecté ni au plafond. Le prêteur à reconnecter reçoit
  quand même SON courriel (« reconnectez votre forfait »).
- **Quand les travaux d'une org n'attendent pas tous la même chose** (voie personnelle,
  plusieurs propriétaires), le message dit une cause : d'abord celle qui ne cède pas seule
  (`pool_vide`, puis `reconnexion`), puis `plafond`.
- **Ce qui n'est pas un incident** : un abonnement qui n'est pas ouvert dans l'org, hors
  du cas ci-dessus (le travail attend un geste d'un autre ordre, que le texte de
  péremption nomme), un
  demandeur sans ligne d'abonnement (la remise arrête son travail), le travail d'un agent
  éteint, une org suspendue (son incident reste tel quel, ni libre ni clos). Le repli API
  se juge sans sa limite de 25 candidats ni « un repli en vol par forfait » : un repli qui
  tourne, c'est une file qui avance.
- **« Depuis »** = la première passe qui a vu l'attente ; les occurrences manquées sont les
  péremptions de cette famille dans l'org entre cette passe et la reprise. Une attente qui
  cède avant `DELAI_MIN` n'a jamais été un incident : sa ligne s'efface.
- **Les heures calmes retiennent les envois, pas le constat** : une panne de 21 h
  s'annonce à 8 h en disant « depuis 21 h ». Un incident ouvert et clos dans la nuit
  n'envoie rien ; la ligne de reprise ne part qu'après une alerte partie.
- **Les gestes** sont des boutons vers un type de lien DÉCLARÉ, `agent_models` (la page
  Agents › Modèles, `{section}` = `plans` ou `key` si le patron le nomme) : non déclaré,
  le message part sans bouton. « Ajouter une clé d'API » n'est proposé que pour un
  `plafond` : le seul repli qui existe ne prend qu'un forfait au plafond.
- **Slack** : le réglage de l'org (`agent_alert_settings`) et son app, comme les alertes
  d'agents, sans dépendre de leur réglage d'instance ; un Slack absent, éteint ou qui
  refuse fait partir le courriel au premier admin joignable. La reprise suit la même règle.
- **Le titulaire** est prévenu une fois par incident, titulaire par titulaire
  (`titulaires_prevenus`) ; la passe relève qui tient le forfait qui manque
  (`titulaires`), et un prêteur qui arrive en cours d'incident est prévenu à la passe
  suivante. Son adresse vient de son compte (`users.email`) ; sans adresse, rien.

### Hors de cette PR

- Le bouton « Tourner sur les crédits de plateforme » : il dépend de B.
- La bannière de la page Agents et le texte de Réglages › Alertes : côté tableau de bord.

## B — le repli sur la clé de plateforme (branche séparée, brouillon)

Aujourd'hui le repli d'un forfait au plafond n'a qu'une cible : la clé que l'org a
déposée. C'est la règle la plus stricte du dépôt, et elle a raison : déplacer une
dépense d'un abonnement gratuit vers des jetons facturés exige que l'org l'ait dit.

B ajoute **une seconde manière de le dire** : un réglage d'org explicite,
`repli_cle_plateforme` (qui, quand), posé par un org_admin, distinct de `repli_api`
(qui reste un booléen : le tableau de bord le lit). Allumé, un travail en pause de
plafond peut rejouer sur la clé de plateforme, payé `cle_plateforme` — ce que
l'instance facture déjà au coût. Bornes :

- même cas étroit que le repli actuel : `paused_limit` à échéance future, jamais une
  reconnexion ni un pool vide ;
- seulement sur une instance qui le **permet** (`OTO_REPLI_CLE_PLATEFORME_PERMIS`,
  éteint par défaut) : une instance qui ne facture pas la clé de plateforme ne doit pas
  offrir ce réglage ;
- même plafond de jetons par run (`REPLI_MAX_TOKENS`), un seul repli en vol par forfait ;
- chaque repli écrit son payeur dans la charge du travail, comme aujourd'hui.

Reste brouillon tant qu'un test ne montre pas, de bout en bout, que le run replié est
compté `cle_plateforme` pour l'org du travail.
