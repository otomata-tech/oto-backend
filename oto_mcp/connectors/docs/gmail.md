## prerequisite — autorise Gmail sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Gmail seulement** (scope `gmail.modify`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Gmail
- un compte qui n'a pas autorisé Gmail est refusé par les outils en nommant cette carte — reviens ici pour l'autoriser

## usage — chercher, lire, rédiger, envoyer

`gmail_message(op=search|get|attachment|drafts|archive|trash)` et `gmail_compose` — sous le compte choisi.
- « cherche les mails non lus de cette semaine et archive les newsletters »
- « rédige un brouillon de réponse à ce mail » ou « envoie-le »
- `gmail_compose` appose la **signature Gmail** du compte émetteur (après `--`), comme le client web — l'API Gmail ne le fait jamais seule. `sign=False` compose sans ; la réponse dit `signature` : `appended`, `none_configured` ou `disabled`
- « lis le tableur `.xlsx` joint à ce mail, onglet `devis` » — une pièce jointe (`op=attachment`) revient en CSV par feuille, borné

## note — périmètre de projet (#605)

une pièce jointe `{kind: "url"}` de `gmail_compose` est lue côté serveur : sous un projet à `excluded_url_prefixes`, une url correspondante est refusée en nommant le motif (seam `file_source`). détail : `docs/projects.md`.
