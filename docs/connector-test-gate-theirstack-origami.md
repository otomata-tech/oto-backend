# Porte de test locale — connecteurs TheirStack + Origami

Ce document est la **porte de test** qu'un connecteur *keyed* doit passer en local avant d'être poussé.
Elle a été écrite pour la paire TheirStack + Origami ; les quatre couches et la plupart des assertions
valent pour n'importe quel connecteur keyed à outils mutants. Rien ici n'exige un serveur en marche :
les outils sont montés sur un FastMCP nu, exactement comme le runner hébergé les appellera.

## 0. Environnement (une fois)

```bash
cd <ton checkout d'oto-backend>
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"                   # backend + oto-core pinné sur son tag GitHub
pip install -e <ton checkout d'oto-core>  # puis ÉCRASE avec la branche locale d'oto-core qui porte les nouveaux clients
python -c "import oto.tools.theirstack.client, oto.tools.origami.client; print('local clients resolve')"
```

⚠️ Le second `pip install -e` n'est pas facultatif : sans lui le pin GitHub gagne et la suite tourne
verte contre l'**ancien** client (`docs/conventions.md` → garde anti-version-skew).

Relever la **baseline avant le nouveau code** — le compte de verts sur les fichiers de tests voisins.
C'est la seule lecture utile du « après » : tout ce qui était vert doit le rester.

## 1. Couche unitaire (clients mockés) — doit être verte

```bash
pytest -q tests/test_theirstack.py tests/test_origami.py
pytest -q tests/test_tools_client_methods_exist.py   # anti-version-skew : chaque tool → méthode client existe
pytest -q                                            # suite entière ; rien de vert ne passe au rouge
```

Assertions que les nouveaux tests doivent porter (refuser la PR s'il en manque une) :
- entrée au registre présente, `keyed=True`, `auth_modes == {"byo_user","byo_org"}`, `secret_kind == "api_key"`, `in_default_bundle is False`
- chaque tool `theirstack_*` / `origami_*` enregistré a une `.description` NON VIDE (le piège de la docstring en f-string)
- Origami : chaque tool mutant honore `dry_run` — la validation tourne, l'appel final est sauté, la réponse renvoie `dry_run: true`
- Origami : `origami_campaign_launch` a `dry_run=True` **par défaut**
- Origami : `origami_campaign_delete` après `confirm=True` re-GET la campagne et dit si elle a réellement disparu
- Origami : `origami_campaign_create` expose `block_prior_contacts` / `block_active_duplicates` et les passe en `settings`

## 2. Couche MCP avec de VRAIES clés, en lecture seule — doit réussir

Les clés viennent d'un `.env` local (`THEIRSTACK_API_KEY`, `ORIGAMI_API_KEY`) — ne jamais les imprimer.
Patcher `oto_mcp.access.resolve_api_key` pour rendre la clé d'env, monter avec `register_all`, appeler les `fn` des tools.

TheirStack :
- `theirstack_companies_search(company_names=[…], company_country_code_or=["FR"])` → 200, enveloppe présente ;
  une liste **vide n'est pas un échec** (la couverture de la source est partielle)
- `theirstack_jobs_search(company_names=[…], posted_at_max_age_days=365)` → 200 ; une annonce donnée peut avoir
  expiré entre deux passages — n'asserter que la **forme**, jamais un contenu daté
- projection : le résultat par défaut ne porte que les champs de sourcing ; `full=True` rend l'enregistrement brut

Origami (LECTURE SEULE — aucun create / upsert / launch / delete contre la production) :
- `origami_workspaces(op="list")` → contient l'espace de travail attendu
- `origami_tables(op="list")` → contient les tables attendues
- `origami_tables(op="columns", table_id=<table>)` → expose les slugs de colonnes et leur `kind`
- `origami_rows(op="list", table_id=<table>, max_pages=2)` → suit `nextCursor`. **Choisir une table de plus de
  50 lignes** : la page 1 en rend 50, et c'est exactement là que le défaut de pagination se voit
- `origami_campaigns(op="stats", campaign_id=<campagne>)` → les compteurs sont servis
- `origami_sequences(workspace_id=<espace>)` → plusieurs **pages** et plusieurs `campaignId` distincts
- `origami_campaign_launch(campaign_id=<table>)` avec les args **par défaut** → doit être un dry run :
  réponse `dryRun: true`, `wouldLaunch: true`, et statut de campagne inchangé au re-GET

## 3. Couche MCP, chemin d'ÉCRITURE, contre une table jetable uniquement

Créer un espace de travail de test `smoke-<date>`, y faire toute la boucle, puis le laisser
(il n'existe pas d'endpoint de suppression de table) :
- `origami_upload_csv` d'un CSV de 2 lignes avec une colonne d'entrée → `table_id` + `table_slug` au
  **premier niveau** de la réponse. La table créée vit en `result.results[0].table.id` ; un harnais l'a
  manquée à cette profondeur, d'où la remontée par l'outil. Une entrée `kind: "error"` revient en
  `error`, jamais en succès silencieux
- `origami_rows(op="upsert", dry_run=True)` → aperçu seul, table inchangée (re-list)
- `origami_rows(op="upsert")` avec un slug **faux** → refusé AVANT l'appel API, en nommant le slug inconnu
  et les slugs d'entrée valides (`clés refusées — slugs inconnus [...] ; slugs d'entrée valides : [...]`) ;
  jamais un 400 avalé
- `origami_rows(op="upsert")` avec le bon slug → 201 ; le re-list montre la valeur
- `origami_campaign_create(dry_run=True)` → aperçu, aucun run d'agent démarré (re-list des campagnes : toujours 0)
- `origami_campaign_delete(confirm=True)` sur un id inexistant → 404 traduit (`ressource introuvable`), jamais un succès affirmé
- **Ne pas** créer de vraie campagne dans le smoke test : le create agentique coûte un run et ne se supprime pas proprement par API

## 4. Comportements que les outils ne doivent PAS avoir

- Aucun outil n'imprime ni ne rend la clé d'API — vérifier la sortie des tools, le texte de l'erreur 401
  (le préfixe de clé seul) et les deux sondes `_verify` (elles rendent `None`)
- Pas de docstring en f-string ; chaque tool `origami_*` / `theirstack_*` enregistré a une description non vide
- Les tools Origami n'appellent jamais `/launch` sans dry run à moins que `dry_run=False` ait été passé
  explicitement — vérifier le défaut du schéma **et** l'appel client (`launch_campaign("c1", dry_run=True)`
  avec les args par défaut)
- `origami_campaign_delete` ne revendique jamais un succès sur le `200` de l'étape 1 : avec un 200 mocké et
  la campagne encore lisible → `really_deleted: false` et une note

## 5. Ce que la porte a effectivement attrapé

Deux défauts que seule la couche 2 (vraies clés) pouvait rendre :

- **`origami_sequences` ne rendait qu'une page** — 50 séquences, une seule campagne, là où l'espace en
  portait plusieurs centaines réparties sur plusieurs campagnes. Corrigé en suivant `nextCursor`
  (`max_pages`, `campaign_ids`) puis revérifié en live. Une couche unitaire mockée ne l'aurait jamais vu :
  le mock rend une page, et une page est toujours complète.
- **Le crédit épuisé revient en 402**, pas en erreur réseau — traduit en `crédits épuisés (402)` plutôt que
  remonté brut. Corollaire de méthode : un compte de test à sec ne prouve rien sur la couche 2, il faut
  distinguer « la porte échoue » de « le compte est vide ».

## 6. Sign-off

Consigner dans la PR : les comptes de tests unitaires (avant / après), les résultats de la couche MCP en
lecture seule, le résultat de la boucle sur table jetable, et les questions laissées au mainteneur —
typiquement : un mount capable d'écrire est-il acceptable pour ce connecteur ? quel tag oto-core épingler ?

⚠️ Les rouges de la suite complète se lisent **par différentiel avec le commit de base**, jamais en absolu :
sur un poste sans Docker, la fixture testcontainers et `DATABASE_URL not set` produisent une grappe
d'erreurs qui n'a rien à voir avec le lot. Ce qui compte est que les identifiants des tests en échec soient
**identiques à l'octet** entre la base et la branche.
