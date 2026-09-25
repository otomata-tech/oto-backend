## prerequisite — autorise Google Sheets sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google Sheets seulement** (scope `spreadsheets`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google Sheets
- un compte qui n'a pas autorisé Google Sheets est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — lire et écrire un tableur

`sheets_spreadsheet(op=describe|read|write|clear)` et `sheets_create` — sous le compte choisi.
- « lis l'onglet `leads` de cette sheet »
- « écris ces lignes à la suite de l'onglet `suivi` »
- « crée un tableur `Prospects octobre` et remplis-le »
