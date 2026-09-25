## prerequisite — autorise Google Tasks sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google Tasks seulement** (scope `tasks`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google Tasks
- un compte qui n'a pas autorisé Google Tasks est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — listes de tâches

`tasks_lists` et `tasks_task(op=list|get|create|update|complete|delete)` — sous le compte choisi.
- « ajoute une tâche `relancer X` pour lundi »
- « quelles tâches sont en retard ? »
