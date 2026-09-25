## prerequisite — autorise Google Chat sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google Chat seulement** (scopes `chat.spaces.readonly` et `chat.messages`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google Chat
- un compte qui n'a pas autorisé Google Chat est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — espaces et messages

`chat_spaces` et `chat_message(op=list|read|post)` — sous le compte choisi.
- « liste mes espaces Google Chat »
- « résume le fil de l'espace `#ventes` depuis lundi »
- « poste ce message dans `#ventes` »
