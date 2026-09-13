## prerequisite — ta clé d'API Monid, et le portefeuille qu'elle débite

crée une clé dans le tableau de bord de [Monid](https://monid.ai) → API keys (elle commence par `monid_`), puis colle-la dans oto.
- **chaque appel est débité du portefeuille Monid** du workspace auquel la clé est rattachée : ce que tu lances se paie là, pas chez oto
- une clé rattachée à aucun workspace répond 403 ; une clé révoquée ou mal copiée, 401 — on en recrée une et on remplace l'ancienne sur la carte
- la clé de la plateforme ne s'ouvre que sur grant explicite : sans grant, c'est ta clé ou celle de ton org
- ⚠️ sous la clé de la plateforme, le workspace Monid est **partagé** par les orgs qui ont un grant : la liste des runs y est refusée (elle montrerait les leurs) ; un run se relit et s'arrête par son identifiant
- ⚠️ sous la clé de la plateforme, le **solde** est refusé aussi : c'est celui du portefeuille partagé de la plateforme, pas le tien. Un lancement qui manque de fonds le dit (402) ; pour voir un solde, pose ta propre clé Monid
- le bouton « tester la connexion » appelle l'identité de la clé (gratuit) : il ne lance rien et ne lit pas le solde

## usage — trouver l'endpoint, lire son prix, le lancer

Monid met derrière une seule clé quelque 2 000 endpoints d'environ 70 fournisseurs : recherche web, scraping, enrichissement de contacts, réseaux sociaux…
- « trouve le mail pro de ce profil LinkedIn » → `monid_endpoint(q="work email from a linkedin profile")` pour trouver l'endpoint, puis `monid_endpoint(op="inspect", provider=…, endpoint=…)` pour son schéma d'entrée et son prix, enfin `monid_run(provider=…, endpoint=…, query_params={…})`
- run long (le lancement rend un run pas encore fini) → `monid_runs(op="get", run_id=…, wait_seconds=30)` jusqu'à `done: true`
- « arrête ça » → `monid_runs(op="stop", run_id=…)` (arrête la dépense ; l'arrêt est asynchrone)
- « qu'est-ce que j'ai lancé ? » → `monid_runs()` (du plus récent au plus ancien) — **avec ta propre clé Monid** (ou celle de ton org) : sous la clé de la plateforme, la liste est refusée
- « combien il reste ? » → `monid_wallet()` : `balance` est le dépensable (il peut être négatif), `held` ce qui est réservé aux runs en cours — **avec ta propre clé Monid** (ou celle de ton org) : sous la clé de la plateforme, le solde est refusé

## note — l'entrée, le prix et ce qui est facturé

- l'entrée d'un run a **trois parties** — `body`, `query_params`, `path_params` — là où le schéma d'`inspect` les place, jamais à plat ; `provider` et `endpoint` passent tels que `discover` les a rendus
- le prix qui fait foi est celui d'`inspect`. Ses types sont ouverts (à l'appel, au résultat avec frais fixe, à l'unité, par paliers, par matrice…), et les paramètres de volume (`maxItems`, `limit`) s'appliquent souvent **par requête** : trois termes × 10 résultats, c'est 30 résultats facturés
- ⚠️ pour un prix par paliers ou par matrice, `amount` n'est qu'une base, parfois 0 sur un endpoint payant : le vrai tarif est dans `default` / `tiers` / `variants`, et un palier lu sur la sortie ne se connaît qu'après le run, donc le prix affiché est un plancher
- ⚠️ **statut du run ≠ statut du fournisseur** : `COMPLETED` veut dire « le fournisseur a répondu », quoi qu'il ait répondu. `COMPLETED` + 404 = « rien trouvé », non facturé ; `provider_ok` le tranche pour toi — et reste vide quand Monid ne donne aucun statut du fournisseur (lis alors `next_step`)
- `BLOCKED` = un plafond du workspace Monid (budget, nombre de runs) a refusé le run avant exécution : rien n'est facturé, et relancer bloque de nouveau tant que le plafond n'est pas changé chez Monid. `FAILED` (panne côté Monid), `TIMED_OUT` et `STOPPED` ne sont pas facturés non plus
- ⚠️ **ne relance jamais un run dont l'issue est inconnue** : Monid n'a pas de clé d'idempotence, relancer peut payer deux fois. C'est le cas quand le lancement ne répond pas à temps — un fournisseur synchrone qui garde la connexion plus de ~35 s rend ce refus, et le run existe sans doute. Le retrouver dans `monid_runs()` demande ta propre clé Monid ; sous la clé de la plateforme, c'est un administrateur qui regarde le workspace de la plateforme
- l'attente d'un lancement est bornée (40 s au plus, ~55 s en délais par socket — tant que la connexion s'établit du premier coup : chaque adresse injoignable ajoute jusqu'à 10 s, et la résolution DNS n'est pas bornée) : au-delà, l'outil rend le run en cours et la marche à suivre plutôt que de faire raccrocher le client
- `cost_usd` est ce que Monid déclare facturé, lu dans la réponse et jamais recalculé ; il reste vide tant que le run n'est pas réglé

## note — état de vérification

écrit sur le contrat OpenAPI `0.1.0` publié par Monid, et éprouvé sur un faux transport qui en rejoue les réponses (runs rendus sous un code HTTP qui recopie celui du fournisseur, enveloppe d'erreur, 202 puis relecture, 409 d'arrêt, portefeuille). **Pas encore exercé contre un vrai compte** : ni la forme exacte de `whoami`, ni le curseur de la liste des runs, ni le format réel de la clé.

**hors d'atteinte ici, à dessein** : les budgets et plafonds de runs du workspace, les ressources, la gestion des clés d'API, la recharge et l'historique du portefeuille, le registre public. Les `hints` de Monid ne sont pas demandés (aucun en-tête de client n'est envoyé) et ne sont pas lus.
