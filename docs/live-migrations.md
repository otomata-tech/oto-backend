# Migrations vivantes sur la DB partagée canari/prod — le playbook

> Extrait des chantiers du cadrage objets/visibilité (2026-07-10) : Phase H datastore,
> fusion des procédures d'équipe, unification des ACL connecteurs. À relire AVANT toute
> migration qui renomme/droppe une table ou une contrainte.

**Le fait structurel** : canari (preprod) et prod partagent LA MÊME base. Un DDL exécuté
au boot canari s'applique instantanément à la prod — qui tourne encore l'ANCIEN code.
Toute migration destructive se découpe donc en **lots promus séparément**, chaque lot ne
détruisant que ce que le code prod COURANT ne référence plus.

## La danse en N lots

1. **Lot A (additif)** — nouvelles colonnes/tables + backfill + le code bascule dessus.
   Zéro DDL destructif. Les objets legacy restent en place, encore écrits par la prod.
2. **Promotion A** (PR canari→main). La prod cesse de référencer les objets legacy.
3. **Lot B (bascule/destruction partielle)** — ce que le Lot A a rendu orphelin peut
   tomber ; ce que la prod (Lot A) lit encore attend le lot suivant.
4. **Promotion B**, puis **Lot C** (drops finaux), etc. Un lot = un boot canari vérifié
   (deploy vert + smoke + lecture d'une surface réelle) AVANT sa promotion.

## Les techniques (toutes vécues, toutes nécessaires)

- **Copie legacy→cible à CHAQUE boot, gardée `to_regclass`** : tant que la table legacy
  existe, on recopie (la prod y écrit pendant la fenêtre) ; après le DROP, no-op — un
  boot ne casse jamais, quel que soit l'ordre des déploiements. **Newer-wins** sur
  `set_at`/`updated_at` (ON CONFLICT DO UPDATE … WHERE EXCLUDED.x > cible.x) pour les
  données mutables ; DO NOTHING suffit pour les grants immutables (une révocation prod
  pendant la fenêtre ressuscite — assumé si la fenêtre est courte, le dire dans le commit).
- **DROP au même boot que la copie finale** : le DROP suit la copie dans le même
  `_init` → la dernière écriture prod de la fenêtre est rattrapée.
- **Basculer l'ARBITRE `ON CONFLICT` avant de dropper une contrainte** : la prod fait
  `ON CONFLICT (ancienne_clé)` → il faut d'abord poser l'index unique cible + promouvoir
  le code qui arbitre dessus (Lot A), et seulement ensuite dropper la PK legacy (Lot B).
  Deux index uniques coexistent pendant la fenêtre, les deux arbitres marchent.
- **Nommer les nouvelles PK** (`CONSTRAINT x_owner_pkey PRIMARY KEY …`) : le
  `DROP CONSTRAINT IF EXISTS x_pkey` de la migration ciblerait sinon la PK toute neuve
  d'une install fraîche (même nom par défaut).
- **Ids fusionnés = la MÊME séquence** : des lignes migrées vers une table à id
  surrogate prennent `nextval` de la séquence EXISTANTE — jamais une séquence neuve ni
  un offset (collision garantie avec les refs déjà distribuées : project_links, grants).
- **Fusion de tables jumelles → prédicats de scope PARTOUT** : quand des lignes d'un
  autre grain entrent dans une table, chaque requête existante doit gagner son
  `owner_type='…'` — chercher en priorité les requêtes SANS filtre (list_all, by_id).
- **Seed gardé sur le sous-ensemble sémantique** (ex. lignes `scope='platform'`), pas
  sur `COUNT(*)` global : une table unifiée non vide n'implique pas que le seed a tourné.

## Les pièges

- **Fail-open silencieux sur les gates** : `require_connector_access` et
  `session_visibility` avalent les erreurs DB (fail-open voulu par palier). Pendant une
  fenêtre de migration ratée, le deny se dégrade en allow SANS erreur visible — vérifier
  les surfaces RBAC en lecture réelle après chaque boot, pas seulement le smoke HTTP.
- **`gh pr merge` juste après `pr create`** : le check `guard` n'est pas encore rapporté
  → GitHub répond « add --admin » et NE merge PAS (silencieux dans un script). Attendre
  `gh pr checks | grep 'guard.*pass'` avant de merger ; re-vérifier `state=MERGED` et
  `git merge-base --is-ancestor <tip> origin/main` (avec le SHA POST-rebase, pas le
  SHA du commit local d'avant `pull --rebase`).
- **Les one-shots du boot qui lisent une table vouée au drop** (backfills historiques) :
  les retirer (ou les garder `to_regclass`) DANS le lot qui précède le drop — sinon le
  premier boot prod post-drop crashe sur un backfill spent.
- **Tree partagé** : une session parallèle peut committer TON `_init.py` en vol dans son
  propre commit (absorption). Avant de diagnostiquer un diff stagé « incomplet »,
  vérifier si HEAD contient déjà tes hunks (`git log -S <marqueur>`).
