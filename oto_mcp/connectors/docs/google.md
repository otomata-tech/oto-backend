## prerequisite — le compte Google que les services empruntent

ce connecteur est le **compte** : l'adresse Google, son jeton, son compte par défaut. depuis le split du 2026-09-26, chaque service — Gmail, Drive, Sheets, Calendar, Tasks, Chat, BigQuery — est un connecteur à part entière, avec **son** consentement (ses scopes seulement) : autorise-les depuis leur carte, un à un, sur le même compte.
- tu peux connecter **plusieurs comptes** Google ; chaque outil de service agit sur le compte par défaut ou sur celui que tu cibles par `account=<email>`
- « lier un compte » ici demande les six services sous l'app de la plateforme, et seulement l'identité sous l'app d'un partenaire — ses services les ajoutent ensuite

## note — un compte partagé par l'organisation ou l'équipe

un admin de l'organisation (onglet **Org**) ou un chef d'équipe (onglet **Team**) peut connecter UN compte Google au nom de tous — une boîte partagée, un agenda d'équipe. les outils prennent d'abord ton propre compte, puis celui de ton équipe active, puis celui de l'organisation ; `account=<email>` cible un compte précis où qu'il vive. personne n'atteint le compte personnel d'un autre : n'est partagé que ce qu'un admin a posé comme tel.

## usage — quels comptes, avec quels droits

`google_accounts` — les comptes connectés et, pour chacun, les services qu'il a autorisés. c'est la question à poser quand un outil de service refuse : le compte existe mais n'a pas encore autorisé CE service.
- « quels comptes Google ai-je connectés, et lesquels ont Drive ? »

## note — l'app oto n'est pas publiée chez Google (décision du 2026-09-05)

l'écran de consentement OAuth de la plateforme reste en mode **Testing**, et c'est un choix : passer en *published* avec les scopes Gmail, Drive et Chat (RESTRICTED chez Google) impose un audit **CASA Tier 2**, payant et annuel. deux conséquences, à connaître avant de compter dessus :

- **cent comptes Google au maximum** peuvent autoriser oto. au-delà, la connexion est refusée par Google, pas par nous.
- **le jeton de rafraîchissement expire au bout de sept jours.** un compte connecté qui ne revient pas dans la semaine devra se reconnecter — ce n'est pas une panne du connecteur.

un partenaire qui pose **sa propre app** Google (tenant, `/admin` › OAuth apps) n'est pas concerné : ses utilisateurs consentent sous SON projet, dont il choisit les services à faire vérifier — le split existe pour ça.
