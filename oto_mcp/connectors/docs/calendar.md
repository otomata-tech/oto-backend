## prerequisite — autorise Google Calendar sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google Calendar seulement** (scope `calendar`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google Calendar
- un compte qui n'a pas autorisé Google Calendar est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — agenda

`calendar_calendars` et `calendar_event(op=list|get|create|update|delete)` — sous le compte choisi.
- « qu'est-ce que j'ai à l'agenda demain ? »
- « crée un créneau de relance vendredi 10h avec [contact] »
- « décale la réunion de lundi à 15h »
