## prerequisite — autorise Google BigQuery sur ton compte Google

depuis cette carte, clique **connecter** : Google te demande d'autoriser **Google BigQuery seulement** (scope `bigquery`) sur le compte que tu choisis. le compte Google lui-même (adresse, jeton) est porté par le connecteur **Compte Google** — un même compte peut autoriser plusieurs services, un service à la fois, sans réautoriser les autres.
- les requêtes voient exactement ce que **tes droits BigQuery** voient : rôle « BigQuery Data Viewer » sur les datasets à lire, « BigQuery Job User » sur le projet qui exécute (et paie) les requêtes
- **lecture seule** : chaque requête est validée à blanc d'abord, tout ce qui n'est pas un SELECT est refusé avant exécution
- **coût borné** : 10 Go lus au plus par requête par défaut (relevable jusqu'à 1 To par requête), refus avec l'estimation au-delà
- plusieurs comptes Google : chaque outil agit sur le compte par défaut, ou sur celui que tu cibles par `account=<email>` ; `google_accounts` dit lesquels ont autorisé Google BigQuery

## usage — explorer et interroger l'entrepôt

`bigquery_catalog` (projets → datasets → tables), `bigquery_table` (schéma + aperçu gratuit), `bigquery_query` (SQL, `dry_run` pour estimer), `bigquery_results` (reprendre une requête longue, page suivante).
- « quelles tables a le dataset `analytics` ? »
- « combien de commandes par mois en 2026, depuis `dwh.sales.orders` ? »
- « estime le coût de cette requête avant de la lancer »
