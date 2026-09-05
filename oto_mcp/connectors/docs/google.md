## prerequisite — connecter un compte google (oauth)

va sur le **dashboard oto**, section Google, et clique **connect** : tu autorises oto en OAuth (pas de clé manuelle). tu peux connecter **plusieurs comptes** Google ; chaque outil agit sur le compte par défaut ou sur celui que tu cibles par son email.
- couvre Gmail, Tasks, Calendar, Sheets, Drive et Chat en une seule autorisation

## usage — gmail, agenda, tâches, sheets, drive, chat

agis sur ton Google Workspace : mails, calendrier, tâches, feuilles de calcul, fichiers Drive et messages Chat.
- « cherche les mails non lus de cette semaine et archive les newsletters »
- « rédige un brouillon de réponse à ce mail » ou « envoie-le »
- « qu'est-ce que j'ai à l'agenda demain ? crée un créneau de relance vendredi 10h »
- « ajoute une tâche `relancer X` pour lundi », « lis l'onglet `leads` de cette sheet »
- « partage ce dossier Drive en lecture à jane@… »

## note — l'app oto n'est pas publiée chez Google (décision du 2026-09-05)

l'écran de consentement OAuth reste en mode **Testing**, et c'est un choix : passer en
*published* avec le scope `gmail.modify` (RESTRICTED chez Google) impose un audit **CASA
Tier 2**, payant et annuel. deux conséquences, à connaître avant de compter dessus :

- **cent comptes Google au maximum** peuvent autoriser oto. au-delà, la connexion est
  refusée par Google, pas par nous.
- **le jeton de rafraîchissement expire au bout de sept jours.** un compte connecté qui
  ne revient pas dans la semaine devra se reconnecter — ce n'est pas une panne du
  connecteur, et ça ne se répare pas de notre côté.

la décision se rouvre le jour où quelqu'un doit amener ses propres utilisateurs
(oto-backend#6). google tasks est *sensible* et non *restricted* : lui n'exigerait
qu'une vérification, sans audit.

## note — périmètre de projet (#605, 2026-08-29)

une pièce jointe `{kind: "url"}` de `gmail_compose` est lue côté serveur : sous un projet à `excluded_url_prefixes`, une url correspondante est refusée en nommant le motif (seam `file_source`). détail : `docs/projects.md`.
