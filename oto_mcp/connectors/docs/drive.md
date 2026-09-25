## prerequisite — autorise Google Drive sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google Drive seulement** (scope `drive`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google Drive
- un compte qui n'a pas autorisé Google Drive est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — lister, lire, ranger, partager

`drive_file(op=list|get|download|move|delete…)` et `drive_access` — sous le compte choisi.
- « liste les fichiers modifiés cette semaine dans le dossier `clients` »
- « lis ce `.xlsx` de mon Drive, onglet `devis` » — un tableur revient en CSV par feuille, borné ; `sheet` choisit l'onglet, `max_rows` la borne
- « partage ce dossier en lecture à jane@… »
