---
title: Conventions du backend
type: reference
description: >-
  Les règles de travail du backend, chacune née d'un incident daté : ce qu'un test doit
  décrire (le système, pas l'intention), le **cliquet de vocabulaire** (« doctrine » →
  guide/procédure) et la preuve par diff à vide qu'un renommage n'a touché que
  l'interne, le montage réel comme seul banc d'un garde-fou,
  l'interdiction d'écrire une adresse en dur, les jetons de contexte d'appel réservés,
  le budget de ce qu'un outil renvoie, **où vit un fichier** (le dossier = le domaine),
  l'ordre des middlewares MCP, la contrainte
  MONO-LOOP (aucun I/O bloquant), l'interdiction du silence (un `except` large doit
  re-lever, journaliser ou refuser en nommant), et le cycle d'un connecteur (cran
  d'activation, registre `providers/`, credential multi-champs, sonde de connexion,
  doc how-to, aucune résolution de secret hors DB/env). À lire avant d'écrire du code
  ici, et avant d'ajouter un garde-fou ou un connecteur.
---

# Conventions du backend

Extrait de `CLAUDE.md` le 2026-08-19 : 281 lignes qui pesaient le quart de la carte.
Le contenu n'a pas changé — seule sa place a bougé.

## Où vit un fichier — le dossier EST le domaine

Tranché le 2026-08-27. La règle tient en une phrase : **le dossier d'un fichier dit de
quel domaine il relève**, et un préfixe dans un nom de fichier est un dossier qui
s'ignore. `oto_mcp/` s'était mis à porter 127 modules à plat où le rangement se lisait
dans les noms (`fod_*`, `datastore_*`, `api_routes_*`) — un classement réel, mais que
rien ne rendait navigable et que rien ne tenait.

- **Une famille de ≥ 4 fichiers devient un package, et les fichiers y perdent leur
  marqueur de famille** : `datastore/schema.py` → `datastore/schema.py`. Une famille se
  reconnaît au **préfixe** (`fod_*`) ou au **suffixe** quand il nomme le même domaine
  (`*_oauth` → `auth/`). Sous 4, on reste à plat : trois fichiers ne font pas un
  domaine, ils font trois fichiers.
- **Le module « nu » d'une famille devient `<package>/core.py`** — il n'a pas de
  préfixe à perdre (`datastore.py` → `datastore/core.py`). Exception nommée :
  `api/routes.py` → `api/routes.py`, parce que ce module EST la table de routes.
- **Un déplacement est PUR** : `git mv`, imports mis à jour dans le même lot, et
  **aucun ré-export de compatibilité** — ni stub à l'ancien chemin, ni `import *`.
  Un chemin mort qui répond encore est un chemin qui ne meurt jamais.
- **Un package neuf a un `__init__.py` SANS CODE** (une docstring, rien d'autre).
  Exception : un hub de DÉCLARATION (`capabilities/`) importe ses modules pour leur
  effet de bord — et il le fait en **import absolu**
  (`import oto_mcp.capabilities.orgs.members`), qui ne lie aucun nom court : sans quoi
  `orgs/core` et `groups/core`, ou les trois `sharing`, se disputeraient un nom dans
  le hub, et l'ORDRE des déclarations (qui fixe celui de la table de routes) serait
  décidé par une collision. Trois façades plates existent et sont
  les seules : `db.<fn>`, `access.<fn>`, `org_store.<fn>` — chacune figée par un test
  de surface (`test_db_surface_frozen.py`, `test_access_surface_frozen.py`,
  `test_org_store_surface_frozen.py`). Elles sont un contrat assumé, pas un précédent.
- **`tests/` est le MIROIR de l'arbre** : un test va dans le dossier du module dont il
  décrit le comportement, et **son nom de fichier ne change pas**
  (`tests/middleware/test_middleware_order.py`). Renommer en plus casserait les dizaines
  de citations `tests/test_<x>.py` qui vivent dans la doc et dans les docstrings — et
  l'unicité des noms de fichiers est ce qui évite une collision de modules pytest
  (`tests/` n'a pas d'`__init__.py`).
- **Un fichier neuf** : s'il rejoint une famille existante, il naît dans son package,
  sans préfixe. Sinon il naît à plat — et c'est le **quatrième** de son préfixe qui
  déclenche la création du package, pas le premier qui en a l'intuition.
- **Plafond de 500 lignes par fichier.** Un module qui le dépasse se découpe — mais
  découper est un refactor, jamais le passager clandestin d'un déplacement.
- ⚠️ **Du code qui s'identifie par NOM DE FICHIER ne survit pas à un rangement.** Un
  repère de code se dit en **chemin relatif au dépôt** — ou mieux, en **PROPRIÉTÉ du
  résultat** : ce que la chose EST, pas où elle habite. Trois cas vécus dans la seule
  vague du 27-28/08, tous rattrapés par un test et aucun par une relecture :
  `datastore.schema._read_keys` relisait son propre source par nom de fichier et
  rendait, déplacé, un vocabulaire VIDE — donc « aucune clé inconnue » sur tout schéma,
  en silence ; le tripwire de scope credential reconnaissait ses trois exceptions par
  `f.name`, or dans un package `folk.py` ne dit plus de quelle famille il relève ; et
  l'allowlist du tripwire d'adresse de tableau de bord, même défaut avec `base.py`. Le
  quatrième, `test_no_new_handwritten_rest_route`, globait `api_routes*.py` — il avait
  DÉJÀ eu un angle mort sur ce motif (#286) : il lit désormais la table SERVIE et
  demande où l'endpoint est **défini**, ce qu'aucun renommage ne peut plus fausser.
- ⚠️ **Un déplacement se vérifie SANS le repli de l'install editable** (vécu 28/08,
  CI rouge / local vert). Le tree `/data/oto/backend` est installé en editable : son
  finder est ajouté à `sys.meta_path`, donc un import resté sur l'ancien chemin
  (`from .. import datastore_schema as dsv2`) continue de résoudre — sur le fichier
  d'AVANT, dans un autre checkout. La suite passe au vert sur du code qui n'existe plus
  dans la branche, et c'est la CI qui l'apprend. La parade est un test, pas une
  discipline : `tests/test_no_editable_fallback.py` vérifie une propriété du RÉSULTAT —
  **tout `oto_mcp.*` chargé vient du répertoire de ce dépôt** — donc il ne connaît aucun
  nom de module et reste vrai au déplacement suivant. Il est inerte en CI (aucun autre
  checkout n'y existe) et mord en LOCAL, là où le défaut vit. À la main, en plus, quand
  on déplace une famille : un **balayage AST** des imports qui la visent encore — le
  grep, lui, rate les listes `from .. import a, x as y, b`, et c'est exactement là que
  le cas de 2026-08-28 se cachait.

## Les règles, chacune née d'un incident daté

- **Le vocabulaire du produit se garde par un CLIQUET, pas par une règle de revue**
  (2026-08-28, #519). « Doctrine » a été remplacé par **guide** (ADR 0042) et
  **procédure** (ce qui s'exécute) dans tout l'interne. Une règle de vocabulaire ne
  survit pas six mois : le mot revient par un copier-coller depuis un fichier voisin,
  ou par un module neuf qui imite son aîné. `tests/test_vocabulaire_guide.py` compte
  les occurrences par fichier dans `oto_mcp/` : **aucun fichier hors allowlist**, et un
  fichier de l'allowlist **ne peut pas en porter plus** — un plafond qui n'est plus
  atteint est refusé, sinon la marge libérée se remplirait en silence.
- **Un renommage de vocabulaire n'a le droit de toucher QUE l'interne, et ça se
  prouve** (2026-08-28, #519). La frontière n'est pas « code vs texte » — beaucoup de
  PROSE sort du serveur : les `description=` de capacité, les descriptions de champ,
  **les docstrings des modèles `Output`** (elles deviennent la `description` du schéma
  200 dans `/openapi.json`, et le NOM de la classe devient
  `#/components/schemas/<Nom>`), le `when` d'un `DeclaredError` (description de la
  réponse 4xx), les blocs d'instructions injectés au `initialize`, les messages
  d'erreur. Idem pour les identifiants : le **nom d'un handler REST** est figé
  dans `tests/api/api_routes_table.txt`, donc le renommer change la table servie. La
  preuve n'est pas une relecture, c'est un DIFF À VIDE : dumper avant/après le document
  OpenAPI, la table de routes, l'inventaire `tools/list` (noms + schémas +
  descriptions), la chaîne des middlewares et le DDL assemblé, puis exiger l'égalité
  octet pour octet. Trois régressions de ce lot n'ont été vues que par ce diff.
- **Un test qui affirme une INTENTION grave le bug.** Trois fois le 13/08 : des tests
  vérifiaient que la découverte annonçait l'émetteur du tenant, que le lien collait notre
  chemin sous leur domaine, que l'adresse valait `dashboard.oto.ninja` — tous verts, tous
  protégeant un défaut qui a cassé la prod ou servi la preprod à un client. Un test doit
  décrire le SYSTÈME (le document servi, la route montée, la dérivation), pas la valeur
  qu'on croit juste. Corollaire : **une chaîne de découverte d'auth se prouve avec un vrai
  client MCP avant la prod**, jamais avec des assertions sur un document.
- **Une adresse rendue à l'utilisateur ne s'écrit jamais en dur** (`config.dashboard_url`,
  tripwire `test_dashboard_url_par_tenant.py`). Trois variables ont coexisté pour la même
  adresse et la prod n'en posait qu'une : tout ce qui lisait les autres servait la
  **preprod**, y compris à un client. Le défaut vise désormais la prod — un environnement
  mal configuré doit dégrader vers le vrai produit, pas vers un bac à sable.

- **Un garde-fou d'inventaire s'exerce sur le MONTAGE RÉEL, jamais sur une fixture
  partielle.** Trois cas en deux jours (11-12/08) où le banc du garde-fou divergeait du
  réel et le garde-fou **mentait par omission** : le glob anti-routes-manuelles voyait
  45 chemins sur 81 (`api_routes_*.py` rate `api/routes.py`) ; `openapi.build()` local
  rend 138 opérations quand le document SERVI en porte 233 (les routes main n'existent
  que servies — **auditer le document servi, jamais le build**) ; l'inventaire des flux
  de connexion montait les tools sans les routes REST, or au boot réel ce sont les
  routes qui importent les modules d'auth. Racine commune : la fixture reproduit une
  PARTIE du démarrage et le test promet le TOUT.
  **Même racine côté DONNÉES (3 cas sur le seul lot M4, 13/08)** : un banc qui
  RECONSTITUE le schéma mesure la représentation qu'on s'en fait, pas le système —
  la table sans son vrai DDL (un `id BIGSERIAL` supposé, inexistant : clé composite),
  `nodes` sans ses deux GIN (99 % du coût d'écriture au banc M0), un peuplement
  uniforme là où la prod est un vivier (la fausse absence de dégradation de la file :
  la table s'épuisait avant que la dégradation n'apparaisse). Et toujours dans le sens
  RASSURANT. Règle : un banc s'exerce sur le VRAI DDL (extrait de `_schema.py`) et une
  population de forme réelle — sinon il rend des verdicts, pas des mesures. Un 4e cas le soir même, dans l'AUTRE
  sens : un test qui importait lui-même le module qu'il inventoriait certifiait une
  couverture inexistante — le flux n'était déclaré nulle part au boot réel, seul le test
  le chargeait (v1.88.0, corrigé v1.88.1). Règle complète : **le banc d'un garde-fou
  d'inventaire charge ce que charge le boot, ni plus NI MOINS** (le vrai boot
  `register_all` + routes, ou le document servi) — et à sa création, **prouver qu'il
  mord** en lui présentant l'anomalie qu'il prétend attraper (retirer la déclaration ⟹
  l'inventaire doit tomber).
  Corollaire sécurité (13/08) : **un signalement de vuln se vérifie en CONSTRUISANT
  l'attaque**, jamais en jugeant les bibliothèques — le XXE signalé sur l'extraction ne
  s'appliquait pas (entités non résolues, prouvé par l'exploit), mais le construire a
  révélé le voisin réel : la bombe de décompression (400 ko → 638 Mo de RSS, mono-loop
  = tout le serveur). La garde se pose sur le CATALOGUE du zip (les tailles annoncées,
  sans décompresser — le contrôle ne peut pas être victime de ce qu'il contrôle), et on
  s'arrête PENDANT la lecture — jamais accumuler-puis-tronquer.
  Variante « chemin jamais emprunté » (13/08 soir) : **la suite ne couvre pas une clause
  de rattrapage que rien n'exerce** — une clause `except` ne s'évalue qu'à la propagation
  (un nom non importé y dort sans erreur : boot vert, suite verte, NameError en prod au
  premier doublon — trouvé par revue adversariale post-découpage, pas par les tests).
  Deux parades posées : le test du CHEMIN DE RATTRAPAGE lui-même (provoquer l'exception,
  vérifier le contrat de l'appelant qui en dépend), et après toute scission de module un
  balayage des noms lus sans être importés ni définis (test grossier niveau module,
  suffisant pour le nom hérité d'un fichier scindé).
- **Une ressource de test qui vit HORS du processus se pose avec son étiquette, sa date
  et son balai — le finalizer n'est qu'un des chemins de sortie (2026-08-30, #640).**
  La fixture `pg_dsn` lançait `docker run --rm` et comptait sur son finalizer : quand
  pytest meurt sans lui (limite de session, timeout, agent coupé — SIGTERM, SIGKILL),
  `--rm` ne joue jamais (postgres ne sort pas de lui-même) et le volume anonyme de
  `PGDATA` reste. Constat sur le poste : dix `oto-test-pg-*` de 7 h à 2 jours,
  ~519 volumes orphelins (~260/jour), 3 G libres. Quatre parades cumulées
  (`tests/_pg_hygiene.py`) : le conteneur porte `oto-test=1` et
  `oto-test-started=<epoch>` (on le retrouve et on le date sans dépendre du nom) ;
  `PGDATA` est un **tmpfs** (rien à laisser, `Mounts` vide, init plus rapide) ; la
  sortie est couverte par `atexit` + SIGTERM/SIGINT **relayés** (le conteneur part,
  puis le signal fait ce qu'il aurait fait — jamais une sortie propre qui masquerait la
  coupure) ; et SIGKILL ne se rattrapant pas, **chaque session pytest balaie** les
  conteneurs étiquetés de plus de deux heures, une ligne par conteneur. Règle : **un
  `oto-test-pg-*` de plus d'une heure est un orphelin** — aucune suite ne dure une
  heure ; on le retire sans se demander à qui il est (`docker rm -f -v`). Le balai
  automatique garde une marge (deux heures) pour ne jamais toucher la session d'à côté.
  Et le chemin NORMAL fuyait aussi : `docker rm -f` sans `-v` laisse le volume anonyme
  d'un conteneur `--rm` (prouvé le 30/08 : il survit au `rm -f` nu, pas au `rm -f -v`)
  — un volume par run propre, c'est ça les ~260/jour, pas les dix orphelins. **Toute
  suppression de conteneur de test passe par `docker rm -f -v`.**
- **Un compte tiré d'une vue filtrée est un PLANCHER tant que la vue ne déclare pas sa
  portée — il se vérifie par un second chemin** (2026-08-29, #630). Trois lectures de
  `op=calls org_id=…` à zéro sur un refus que `op=run` montrait : la vue était exacte
  dans son scope (`tool_calls.org_id` = l'org sous laquelle l'appel a été RÉSOLU), mais
  l'appel d'un run de l'org, résolu sous l'org maison de l'appelant faute d'axe `_org`,
  n'y était pas — et rien ne le disait. Règle : une vue filtrée **dit son périmètre et
  compte ce qu'il exclut** sous les mêmes filtres (jamais un zéro silencieux), et tant
  qu'une vue ne le fait pas, un chiffre qu'on en tire se recoupe par un second chemin
  (`op=run`, le journal brut, la base) avant d'en conclure quoi que ce soit.
- **Le refus est bruyant, la divergence est muette — un `except` large doit DIRE
  quelque chose (27/08).** Un `except Exception` qui ne re-lève pas, ne journalise pas
  et ne rend pas de refus nommé ne rattrape pas la panne : il la traduit en SUCCÈS.
  L'appelant reçoit une valeur de repli et croit avoir été servi. Inventaire par AST du
  27/08 : **333** handlers n'atteignaient jamais un `raise`, dont **dix** produisaient
  un défaut cher — un jeton API **non porté** émis parce que le corps JSON demandant sa
  portée était illisible, un fichier annoncé « privé » resté `public-read` avec son URL
  permanente, un porteur de vieux jeton servi **sous son compte d'avant migration**, un
  client instancié **sans identifiants** parce que le blob du coffre ne se relisait pas.
  Trois d'entre eux avaient, à moins de vingt lignes, un commentaire qui interdisait mot
  pour mot ce qu'ils faisaient : **la règle écrite ne tient pas, le forçage tient**.
  Garde-fou : `scripts/lint_silences.py`, exercé par `tests/test_no_silent_except.py`
  (qui prouve aussi qu'il MORD, sur sept formes de silence et cinq formes de catch-all).
  Un handler passe s'il fait l'une de ces trois choses, n'importe où dans son corps :
  **re-lever**, **journaliser** (`logger.warning/error/exception` — un `print` ne compte
  pas), ou **rendre un refus nommé** (`json_error`/`_json_error`, fabriques DÉCLARÉES —
  un `return JSONResponse(...)` nu ne dit pas s'il refuse ou s'il sert). Un `except`
  ÉTROIT (`except ValueError`) n'est pas concerné : c'est une décision, pas un filet.
  L'unique échappatoire est `# noqa: SILENT — <raison>`, sur la ligne du `except` ou
  juste au-dessus à son indentation ; **la raison est obligatoire** (un `# noqa: SILENT`
  nu est refusé, sinon l'échappatoire devient le chemin par défaut). Les **168** sites
  existants sont annotés : dette DÉCLARÉE, pas permis — les uns nomment un choix
  (fail-open de visibilité dont le backstop dur est au call-time, fail-closed d'un
  callback qui ne distingue jamais les causes d'un refus, ACK de webhook), les autres
  portent « dette déclarée … (#424, verdict C) » et attendent leur lot.
  **Détail : `docs/silences-2026-08-27.md`.**
- **Un test assert sur le CODE d'un refus, jamais sur sa phrase** — et la phrase ne se
  cite que quand c'est ELLE qu'on éprouve (un message qui doit porter une mesure, un
  hint qui doit basculer). Le code est le contrat, il ne bouge pas ; la phrase est de
  l'ergonomie, elle bouge tout le temps.
  **Ce que ça a rapporté, chiffré le 03/09/2026** : décision de passer en anglais les
  messages atteignables par un utilisateur extérieur. Sur 97 fragments de phrase citables
  des modules concernés, **6 seulement sont cités par un test — 21 lignes dans 11
  fichiers**. La même décision sur une suite qui asserte des phrases aurait coûté des
  centaines de réécritures, et se serait probablement soldée par un report. La discipline
  n'a pas été tenue POUR ça ; c'est ce qui la rend démonstrative — **elle a payé un
  changement que personne n'avait anticipé en la posant.** À citer quand on demande
  pourquoi on s'impose des codes nommés.
- **Ce dépôt est PUBLIC : on n'y écrit JAMAIS de donnée réelle de tiers ni d'identifiant réel.**
  Ni nom de client ou de partenaire, ni nom de personne, ni adresse (postale, e-mail), ni
  identifiant de compte, d'organisation, de projet, de run, d'appel, de ligne ou
  d'infrastructure, ni adresse IP de tiers. **Peu importe le contexte** : une preuve datée,
  une note de travail et un exemple sont soumis à la même règle.
  **Ce qu'on écrit à la place, c'est la FORME ou la CLASSE** — « une org cliente », « un
  identifiant de ligne », `org_id=<l'org du run>`, `~/.ssh/<clé>`. Un exemple purement
  illustratif prend `acme`, `Jane Doe`, un TLD `.test` ; mais **on ne fabrique jamais une
  valeur là où la phrase affirme un fait vérifiable** — une donnée fausse donnée pour vraie
  est un autre défaut, pas une correction. Troisième voie quand la valeur est fonctionnelle
  et que la doc mentirait sans elle : écrire le GABARIT (`<tenant>:`, `<slug>`), et garder la
  valeur seulement là où le CODE en a besoin, sous un `# noqa: CLIENT — <raison>` motivé.
  C'est une **règle d'écriture, pas un contrôle** : il n'existe aucun garde-fou automatique,
  et aucun n'est souhaité — **rien ne l'attrapera à votre place.**
- **Tree partagé entre sessions : deux sessions ne partagent JAMAIS un fichier — le
  séquencement prime, le staging n'est qu'un filet.** Vécu 13/08 (main rouge) : un
  `git add <chemin>` EXPLICITE a absorbé ~148 lignes du WIP d'une session voisine dans
  un commit poussé — le chemin explicite ne protège que du FICHIER voisin, pas du
  **HUNK** voisin dans le même fichier ; le commit appelait une fonction restée dans
  le stash de l'autre session (AttributeError sur les chemins d'écriture, CI rouge).
  Règle : le superviseur séquence les fichiers contendus (un seul occupant à la fois) ;
  à défaut, staging au grain hunk ; et un commit dont le diff dépasse son périmètre
  annoncé ne se pousse pas. Corollaire (13/08 soir) : **un WIP qui ne compile pas n'est
  pas un WIP, c'est une panne pour tout le monde** (l'import du package échoue ⟹ plus
  aucun test ne tourne sur le tree, hotfix prod bloqué inclus) — découper en édits qui
  laissent chacun le module IMPORTABLE.
- **Jetons de contexte d'appel = noms RÉSERVÉS, préfixés `_`** (ADR 0038 amendée 29/07,
  oto-backend#250) : `_org`, `_project`, `_group`, `_account`, `_instance`, `_run_id`
  (`call_axes.py`). Ils sont advertisés sélectivement au schéma des tools concernés, lus
  des args bruts, posés en ContextVar, puis **retirés avant le dispatch**. Le préfixe est
  ce qui rend ce retrait sûr : un tool peut déclarer `account`/`org`/`project` en argument
  MÉTIER sans risque. Tant qu'ils portaient les noms NUS, le retrait mangeait de vrais
  arguments **en silence** — `oto_use_org(org=)` (l'org cible, 04/07) puis
  `aiark_company_search(account=)` (le filtre société, 28/07 : AI Ark renvoyait sa base
  entière, 72M lignes, sans la moindre erreur). Ne JAMAIS nommer un argument de tool
  `_<quelque chose>` (tripwire `test_call_axes_business_param_collision.py`). ⚠️ La prose
  du bloc A prescrit ces jetons (en résumé — la forme longue vit dans le guide
  `notice`) : les sources sont `instructions.py` et `oto_mcp/guides/notice.md`
  (seeds versionnés).
  **PAS d'override DB (`platform_instructions['secret_sauce']`) sauf divergence
  DÉLIBÉRÉE** — un override qui recopie le seed est une MINE : il fige la prose au jour
  de sa pose et toute évolution du code cesse de se propager sans que rien ne le
  signale. Vécu 12-14/08 : la copie DB a survécu deux jours au retrait d'`abandoned`
  (#311) — le texte le plus lu de la plateforme prescrivait une valeur que
  `run_finish` REFUSAIT. Purgé le 14/08 (l'override est VIDE, le seed sert seul ;
  vider l'override = « rétablir le défaut » depuis v1.117.0). Si un jour on diverge
  pour de vrai : mettre les deux à jour, la DB **après** le déploiement prod — et
  savoir que cette règle repose sur la mémoire, pas sur un garde-fou.
- **Ce qu'un outil DÉCRIT a un budget aussi : toute PR qui allonge une description
  servie donne son delta en caractères (29/08, #517).** Au même titre qu'un diff
  d'empreinte, et pour la même raison : la description part au modèle à chaque
  connexion, donc la rallonger est un changement de comportement, pas un ajout de
  documentation. La mesure qui l'impose est datée du 27/08 — la longueur des
  descriptions d'outils est le **seul** changement sur le chemin d'une campagne dont
  les appels malformés sont passés de **21 % à 62 %** (`docs/datastore.md`). Le chiffre
  sort de `scripts/empreinte_servie.py --diff <ref>` — **jamais d'un comptage à la
  main** — et s'annonce dans le format de sa sortie, une ligne par outil touché.
  ⚠️ Le delta ne s'annonce
  pas pour être approuvé, il s'annonce pour être **pesé** : trois outils à +100 chacun
  sur le chemin d'une flotte se discutent, un +20 isolé non. Et quand il gonfle, le
  remède est connu — le contrat minimal dans la description, le détail dans les refus
  et dans `docs/`, puisque c'est le refus que l'agent lit au moment où il se trompe.
  ⚠️ **Le chiffre se MESURE, il ne se compte pas.** Le harnais ne sert pas la docstring :
  il retire le bloc `Args:` et désindente — 2 776 caractères de docstring pour 2 058
  servis sur `data_write`. La PR #573 a d'abord annoncé `+162` pour un `+75` réel,
  et l'écart a rendu aveugle l'outil de la flotte : il ne distinguait plus « compté
  autrement » de « la moitié du correctif manque ». *Une convention qui demande un
  chiffre sans dire d'où il sort produit des chiffres différents chez chacun.* Le
  script mesure aussi le **schéma d'entrée**, qui pèse autant qu'une phrase.
  ⚠️ **Et un rapport d'empreinte NOMME CE QU'IL NE REGARDE PAS.** Tous les outils ne
  viennent pas du code : les connecteurs fédérés sont montés d'après la base, donc
  absents d'un relevé pris sans elle — **en silence**. Le script coiffe sa sortie d'une
  ligne de portée qui les nomme, et **refuse de soustraire deux relevés de portées
  différentes** : un côté avec la base et l'autre sans rendrait des outils « RETIRÉS »
  que personne n'a retirés. *Un rapport qui délimite sa portée vaut plus qu'un rapport
  « complet » — le second n'existe pas, il se contente de ne pas dire où il s'arrête.*
- **Un CRAN s'éprouve sur une table jetable, dans le sens du GESTE RÉEL, avant de
  partir sur une campagne qui tourne (29/08, #586).** Deux appels suffisent : **la
  fiche entière réémise** — avec ses couches, avec ses colonnes légitimes, avec sa
  colonne d'état — puis la même sur le cas de bord que le cran vise. *Trois gardes
  posées le même jour ont cassé le geste réel, et les deux appels les auraient
  attrapées toutes les trois en trente secondes* : un refus branché sur un booléen qui
  couvrait deux situations opposées (campagne coupée à 5 fiches sur 100), un refus de
  l'écriture identique qui aurait rejeté chaque fiche réémise (arrêté avant le
  déploiement), et un cran d'origine posé sur une colonne d'état, qui a fait refuser à
  la plateforme les lignes qu'elle venait elle-même de compléter (0 fiche sur 100).
  ⚠️ **Les trois ont été raisonnées sur l'INTENTION du cran, jamais jouées sur le
  geste** — et les trois étaient justes en intention. *Un cran ne se relit pas, il
  s'exécute : ce qu'il refuse ne se déduit pas de ce qu'on voulait qu'il refuse.*
- **Deux gestes voisins qui lisent la même colonne doivent la LIRE PAREIL (29/08,
  #586).** Une colonne du datastore est soit un scalaire nu, soit une enveloppe à
  couches, et **la plateforme elle-même peut l'envelopper** en cours d'écriture (cran
  `origine: "system"`). Tout ce qui JUGE une valeur la déballe donc — c'est déjà le
  cas des contrôles de champ, chacun corrigé après un défaut du même genre (#329 les
  couches, #347 `required_when`), et le contrôle de cycle de vie ne le faisait pas.
  ⚠️ Le mode de panne se décline en deux : **bruyant** quand le contrôle refuse
  (« état inconnu » sur un objet), **silencieux** quand il se contente de se tromper —
  l'état terminal non reconnu n'émet plus son avertissement, le journal enregistre
  l'enveloppe au lieu de l'état. *Le second est le plus coûteux : le geste réussit et
  le relevé ment.* Un déballage se pose **à la source** (dans la fonction qui juge),
  jamais chez ses appelants, sinon il diverge à la première main suivante.
- **Ce qu'un préambule d'outil AUTORISE se répète dans la description du PARAMÈTRE
  concerné (29/08, #517).** Les deux ne sont pas lus par le même agent au même moment,
  et **le texte le plus proche du geste gagne**. Vécu dans les deux sens sur la même
  campagne : `_run_id` disait « le run ACTIF s'applique déjà » à côté du paramètre, et
  **37,5 %** des écritures partaient sans jeton ; `data_write` annonçait
  `namespace="@claimed"` dans son préambule pendant que la description de `namespace`
  n'offrait que le nom de tableau — un agent qui lit le paramètre ne pouvait pas le
  savoir. *Une permission écrite loin du champ où elle s'exerce n'est pas une
  permission, c'est une note de bas de page.* Une phrase par paramètre, et le delta
  passe par le script comme le reste — **le schéma d'entrée porte ces descriptions**,
  donc l'ajout se voit dans la colonne « schéma », pas dans « description ».
  ⚠️ **Et la règle a été inapplicable à la moitié des outils pendant deux jours (#627).**
  Un outil porté par une **capacité** perdait la description de ses paramètres : l'
  adaptateur MCP ne recopiait qu'annotation et défaut, un `Field(description=…)` était
  **accepté-inerte** — mesuré sur `data_patch_schema`, schéma servi 621 caractères avant,
  621 après. La face REST, elle, la publiait : *la même consigne était un contrat d'un
  côté et un commentaire de code de l'autre.* Corrigé le 2026-09-01 (`apply_flat_signature`
  recopie la `description`, **et elle seule** — `tools/list` n'est pas le document REST) ;
  cliquet `tests/test_param_description_servie.py`. **Un vide qui ne fait pas rougir un
  test rend une convention décorative sans que personne ne le voie**, parce que la façon
  normale de la vérifier — relire le code où la description est écrite — montre
  exactement ce qu'on voulait écrire.
- **Une description ou un refus ne prescrit pas un outil que le jeu servi peut ne pas
  contenir (29/08, #613 → #632).** Un endpoint publié sert un jeu d'outils à l'inclusion
  (`mcp_tools`) ; une flotte a lu « release with `data_release` » sans `data_release`
  (#613), puis « se lit avec les outils `unipile_*` » sans qu'aucune règle ne sache ce
  que son jeu contenait (#632). *Un agent qui a une intention et pas de destination s'en
  fabrique une* — dans la fiche du client, la fois d'avant. Critère, appliqué au relevé
  complet des descriptions servies et des chaînes sous un `raise` (backend + oto-core,
  relevé dans la PR de #632) : **se corrige** le texte qui redirige le geste EN COURS vers
  un outil d'un autre connecteur ou d'une autre famille sans dire quoi faire s'il manque ;
  **reste** ce qui nomme un outil du même connecteur (activés ensemble), ce qui n'est qu'un
  pointeur pour obtenir une valeur que l'appel lui-même accepte (`_account=`, `_run_id`,
  `org=`), ce qui compare ou signale un voisin pour un AUTRE besoin, et ce qui est déjà
  conditionnel (« if you have it », « ou passe `org` »). La forme qui marche : le FAIT
  (« cette page ne se lit pas par extraction »), puis au plus une condition (« si ton jeu
  d'outils porte un compte connecté, c'est par lui »). Et quand deux refus se disputent une
  entrée, **celui qui n'ouvre aucune porte parle en premier** — le périmètre de projet
  (#605) avant toute règle interne du connecteur, en tête du handler, cliquet
  `tests/test_url_perimeter_order_632.py`.
- **Ce qu'un outil RENVOIE a un budget, et il se mesure — pas une consigne (14/08).** Sept
  signaux d'usage en six jours, tous le même défaut : un payload qu'un agent ne peut pas
  lire (`linkedin_aiark_search` 3 M caractères, `oto_doc op=list` 201 K, `linkedin_unipile_post
  op=feed` 67 K, `oto_project op=list` 73 K). Chaque fois, le client déverse en fichier puis
  reparse au `jq` — et **un agent sans shell (client MCP nu, n8n) cale tout court** : pour lui
  un tool trop verbeux n'est pas cher, il est inutilisable. Quatre règles en sortent :
  - **Une LISTE rend son index, jamais les corps.** Elle sert à choisir quoi ouvrir : de quoi
    adresser, trier, et écarter sans se tromper. Seam partagé `output_projection.summarize()`
    — les colonnes-corps deviennent `<champ>_length` et la réponse **NOMME** ce qu'elle a
    écarté (bloc `projection`). Le brut reste atteignable (`fields=["*"]`), un `fields=[]` est
    **refusé** plutôt qu'avalé. Fait sur `oto_doc`/`oto_project` ; `guides` et `org_instructions`
    le faisaient déjà.
  - **Projeter ≠ tronquer.** Retirer des colonnes est réversible et annoncé ; couper un texte à
    N caractères est une mutilation silencieuse — l'agent croit avoir lu. D'où la TAILLE, jamais
    un extrait (mesuré le 11/08 : un feed coupé à 600 c. tombait pile avant la chute qui
    départage un post de fond d'une pub, 2 cas limites sur 5 tranchés à l'aveugle).
    ⚠️ **Ce remède ne vaut qu'en LECTURE — borne posée le 03/09.** Rendre la taille laisse
    l'appelant redemander : le savoir perdu reste récupérable, et le drapeau le rend
    récupérable. À l'**ÉCRITURE** il ne répare rien — la fin ne survit nulle part et celui
    qui l'avait s'en va. Donc **une coupe sur une écriture se REFUSE**, en disant la longueur
    reçue ET la borne, tant que le détenteur du texte peut encore raccourcir lui-même.
    Cas fondateur : le motif d'une mise en pause de compte — exigé précisément pour être relu
    dans six mois par quelqu'un qui n'était pas là — coupé à 500 c. en silence. La coupe prend
    la FIN, c'est-à-dire la condition de réveil, qu'on écrit en dernier : le texte garde sa
    forme et perd sa fonction. Le journal n'aide pas, il écrit la valeur APRÈS la coupe.
    Et le critère de décision : la colonne était en `TEXT` — **quand la borne ne vient pas du
    stockage, c'est un choix de surface, et un choix se dit** (oto#42, 4ᵉ règle).
  - **Denylist de clés nommées, jamais une allowlist** (leçon `fr_get`/`liste_idcc` : un champ
    oublié disparaît en silence). Le seam ne connaît aucun outil — chaque connecteur déclare
    ce qu'il coupe, là où il sait ce que ses champs valent (`full=True` rend le brut).
  - **Le handshake aussi a un budget.** Les 6 jetons `_*` sont recopiés dans ~400 schémas : une
    phrase écrite dans `call_axes.py` est payée 400 fois, à chaque tour, par chaque agent. Ils
    pesaient **48,2 % des 880 K caractères servis** par `tools/list` ; ramenés à 36,2 % en
    cessant de redire le bloc A (-41 400 tokens). Bornes gardées par `test_call_axes_budget.py`
    et `test_list_view_budget.py` — **rallonger devient un choix visible**, pas une dérive.
  ⚠️ **Aucune de ces tailles n'est instrumentée** : `tool_calls` n'a pas de colonne de taille de
  réponse, donc « quel connecteur rend le plus gros payload ? » reste sans réponse et le 8ᵉ cas
  sera découvert par l'utilisateur qui s'y cogne (oto-backend#340).
- Nouveau connecteur = (1) un fichier `tools/<service>.py` exposant `register(mcp)`,
  (2) un fichier **`providers/<service>.py`** portant sa **déclaration de registre**
  (`CONNECTOR = _c(…)` + ses constantes curées `CATEGORY`/`PUBLISHER`/`DESCRIPTION`/
  `LOGO_DOMAIN`), (3) **une ligne dans `_DECLARATIONS`** (`providers/__init__.py`),
  qui fixe l'ordre. ⚠️ **`PUBLISHER` est OBLIGATOIRE depuis le 2026-09-02** et son
  absence rougit (`tests/test_connector_publisher.py`) : la question qui donne sa
  valeur est « **à qui l'appel arrive-t-il ?** » — une passerelle tierce se nomme,
  un service qu'on opère aussi (`PUBLISHER = "Otomata"`, écrit, jamais sous-entendu).
  Il n'y a **plus de défaut** : il valait « Otomata » et nous a fait servir le produit
  d'un tiers sous notre nom. `docs/connector-vault.md` §« Ce que la fiche DIT ». ⚠️ **La déclaration ne va PAS dans `tools/<service>.py`** : ce
  module importe `..access` (qui importe le registre — cycle) et `register_all` le
  charge en try/except, donc une dép optionnelle manquante retirerait le connecteur
  du CATALOGUE, pas seulement ses outils. Le registre reste pur, sans dépendance ;
  `providers/<service>.py` l'est. Le module de déclaration porte le nom du
  connecteur (refusé à l'import sinon) et son domicile est verrouillé dans les deux
  sens par `tests/test_providers_registry_snapshot.py` (fichier sans ligne = dort
  invisible ; ligne sans fichier = import cassé). `register_all` (`tools/__init__.py`)
  **DÉRIVE le chargement du registre** (otomata-tech/oto-private#24, fin de la liste
  hardcodée) : il boucle
  sur les providers `kind="tools"` et importe `Connector.modules` (défaut = nom du
  provider ; renseigner `modules` si module ≠ nom, ou plusieurs modules par provider —
  ex. `sirene`→`fr`, `google`→`gmail`/`datastore`/`tasks`). Chaque import en
  try/except (un connecteur cassé ne fait pas tomber le serveur). `meta`/`orgs`
  (spine) + `remote`/`mount` (génériques) restent chargés explicitement. ⚠️ Le
  namespace déclaré doit matcher `namespace_of(tool)` (1er token avant `_`) — pas de
  namespace multi-mot (`culture_spectacle`→`culture`), sinon fail-open du gate.
  Le garde-fou `test_tools_module_derivation_matches_filesystem` (`tests/test_capabilities_drift.py`)
  est **auto-maintenu** (croise `tools/*.py` au registre) — ajouter un connecteur
  (fichier + entrée registre) le garde vert SANS rien y toucher ; il casse seulement
  sur un **fichier orphelin** (connecteur posé mais pas déclaré → dort invisible) ou un
  **module fantôme** (faute dans `modules=`/nom). Seul un **module spine** chargé
  explicitement (rare) s'ajoute à `_EXPLICIT_TOOL_MODULES`. Le job `test` tourne
  **sur les PR ET sur push main** (`deploy-canari.yml` « Deploy preprod », `on:
  pull_request` + `push` sur main ; required check de branch protection sur main) et
  au **tag** (`deploy.yml` « Deploy prod »), et installe oto-core **au tag épinglé**
  (runner neuf → pin du pyproject) : un test rouge bloque le merge ET le deploy (les
  deux jobs `deploy` ont `needs: test`). Garde-fou anti-version-skew : `test_tools_client_methods_exist.py`
  vérifie STATIQUEMENT que les méthodes appelées sur le client existent sur la classe
  oto-core épinglée (un tool en avance de phase sur son oto-core casse la PR au lieu
  d'atteindre la prod — leçon `folk_get_user`). Portée élargie le **31/07** : `_client()`
  annoté `-> tuple[Classe, …]` compte comme `-> Classe`, et les variables qui REÇOIVENT
  le client (`client, _ = _client()`) sont suivies — `tools/apollo.py` cumulait les deux
  et sortait ENTIÈREMENT de la couverture, en silence. Seuls les attributs **appelés**
  comptent (un client à sous-objets — `client.companies.list()`, Attio — porte ses
  namespaces en attributs d'instance : les compter produirait un faux positif, et un
  garde-fou qui crie à tort finit ignoré). Un module avec un `_client()` hors portée fait
  désormais échouer `test_no_module_silently_uncovered`, sauf s'il est déclaré dans l'une
  des deux catégories nommées (sous-objets ; **dispatch dynamique** `getattr(client, m)()`
  — serper, serpapi, brightdata, cloro, spott, statiquement invérifiables et donc à
  découvert, ce qui est assumé et visible plutôt qu'implicite).
- **Ordre des middlewares MCP = contrat, pas un détail (02/08).** fastmcp exécute
  `instance.middleware` dans l'**ordre d'ajout** : le PREMIER ajouté est le plus
  **EXTERNE** (`_run_middleware` wrap en `reversed()`, vérifié empiriquement). Deux
  commentaires historiques croyaient l'inverse (« ajouté en dernier pour envelopper ») →
  `CallContextMiddleware` et `FieldRedactionMiddleware` tournaient au plus INTERNE, donc
  la ContextVar `_CALL_ORG` d'un appel épinglé `_org=` était **reset avant** que la
  rédaction de champs et le calllog (plus externes) ne relisent `current_org` : politique
  de rédaction et `org_id` d'audit de l'org **maison**, pas de celle de l'appel. Invisible
  quand les deux coïncident (le cas courant), faux sinon. Ordre correct (extern→interne) :
  `CallContext` → `FieldRedaction` → `ErrorEnvelope` → `UserDisabledTools` →
  `DynamicInstructions` → `ToolCallLogger` → `Sentry` (innermost : traceback brut au plus
  près du handler, et son `event_id` est posé AVANT que le calllog n'écrive la ligne).
  Figé par `tests/middleware/test_middleware_order.py` — le changer demande de relire ses invariants.
- **PERF — le serveur est MONO-LOOP : aucun I/O bloquant dans la boucle.** Un handler
  de tool qui n'`await` rien doit être `def` sync (threadpool) ; du DB sync dans un
  middleware = même règle (`run_in_threadpool`) ; et **une capacité passe par le seam
  des adaptateurs**, qui range autz + handler sync en threadpool (ne pas rapiécer une
  capacité à la main : le seam le fait pour toutes). Quatre modes de gel vécus +
  garde-fous CI, pool borné (`timeout=5`), **DDL à chaud borné**
  (`lock_timeout`/`statement_timeout` sur `_connect_autocommit`), observabilité
  (loop_watch/aiodebug, py-spy box, Kuma timeout 30s).
  ⚠️ **TROIS garde-fous, de natures différentes, parce que chacun échappe aux
  précédents** : `test_no_blocking_async_handlers` lit le source des `@mcp.tool` (async
  sans `await` = rejeté) — or un middleware n'est pas un tool ET doit `await
  call_next`, donc il passe deux fois à côté ; `test_no_blocking_db_in_middleware`
  **observe le thread** qui emprunte une connexion (mouchard sur `db._conn._get_pool`)
  et refuse tout accès DB depuis la boucle ; `test_capacites_hors_boucle` observe le
  thread lui aussi, mais sur **le seam qui FABRIQUE les tools** — un tool fabriqué est
  `async def` et `await`, donc il passe le premier critère alors que les 285 handlers
  sync qu'il appelle tournaient dans la boucle (gel de prod du 01/09, 12 min 48 s de
  silence). Gel du 15/08 : le handshake composait l'artefact de session — la cascade de
  statut de TOUS les connecteurs — dans la boucle. Un chemin de la même classe reste à
  traiter (un `async def` qui bloque AVANT son premier `await`), listé dans le doc.
  **Détail (incidents, recettes de diagnostic) : `docs/event-loop-perf.md`**.
- **Un 502 en rafale n'est pas forcément un gel** — deuxième cause, distincte (#352,
  nuit du 15-16/08) : un POST `/mcp` en vol quand la session streamable-http se termine
  laisse une réponse ASGI **incomplète** (le SDK MCP pousse dans un stream mort), uvicorn
  ferme le transport, et Caddy — qui tenait la connexion pour réutilisable — rend des 502
  sur elle **et sur les requêtes voisines de son pool keep-alive** (des `/api/*` de
  workers qui n'ont jamais parlé à `/mcp`). Le discriminant tient en un chiffre : ces
  502-là durent **~0,2 s** (le gel, lui, fait attendre). ⚠️ **Ce qui remonte à uvicorn
  n'est PAS `BrokenResourceError`** — le SDK l'attrape et la logue ; ce qui s'échappe est
  le `RuntimeError … after response already completed` de son 500 écrit par-dessus le 202
  (mesuré : 1433/1433). Chercher `BrokenResourceError` en haut de pile ne trouve rien.
  Garde : `client_disconnect_guard.py`,
  posée par `server.build_root_app` en couche la plus EXTERNE — elle complète la réponse
  à la place du client parti et n'attrape QUE `error_taxonomy._is_client_disconnect`
  (même prédicat que le drop Sentry, une seule source) ; toute autre exception traverse,
  figé par `tests/test_client_disconnect_guard.py`. ⚠️ **Rien à attendre d'un bump `mcp`** :
  le site fautif est identique de 1.27.2 à `main`/2.0.0, la PR upstream qui le garderait
  n'est pas mergée et le backport 1.x est refusé (`not_planned`).
- **Cran d'activation (ADR 0010/0011)** : déclarer un connecteur ne l'expose PAS —
  gate DB `connectors/activation.py` (master global ± override org, deny-by-default).
  Gate à la **VISIBILITÉ par session** (`UserDisabledToolsMiddleware` + `connector_
  activation`, **fail-open**) : `register_all` charge tout inconditionnellement, le
  middleware masque les tools d'un connecteur non activé pour l'org → (dés)activer
  prend effet à la session suivante **sans restart**, override par org OK. Filtre
  aussi `/api/connectors` (catalogue) ; overlays catalogue `family` (dérivée) +
  `category` (curée) + `publisher` (curé, `_PUBLISHER_BY_CONNECTOR` **ou** le champ
  `publisher` de l'entrée — deux chemins, et l'absence des deux ne retombe plus sur
  rien depuis le 2026-09-02 : le champ servi est vide, et le cliquet le refuse) + `logo_url`
  (dérivé du **CDN logo.dev** par `Connector.logo_url_for` : domaine de marque curé
  `_LOGO_DOMAIN_BY_CONNECTOR` + token publishable `LOGODEV_TOKEN` en env ; pas de S3,
  pas de seed. L'absence est DÉCLARÉE dans `_SANS_LOGO_DE_MARQUE` (générique/maison :
  monogramme côté UI) + tripwire — sinon un oubli se confond avec un choix).
  Surface admin `/api/admin/connectors/activation`
  (`capabilities/platform_connectors.py`) + écran dashboard « connector activation ».
- **Connecteur client-sensible = JAMAIS de code ici** : pont via le connecteur
  **`http` générique** (ADR 0037, amende 0034/0003/0011). Le connecteur historique
  **`bridge`** (`kind="remote"`, tools `bridge_describe`/`bridge_call`,
  `tools/remote.py`) a été **RETIRÉ le 2026-07-16** (oto-backend#108) : un bridge
  n'est qu'une **API HTTP** que le service distant re-expose → l'org configure sur
  la carte `http` son `base_url` (endpoint du bridge) + `auth_mode=bearer` + `token`
  M2M (`credential_fields`, jamais dans le namespace → catalogue sans nom client),
  et l'agent appelle `http_get`/`http_post`. Le service distant détient le credential
  métier (contrat ADR 0003 §4 : bearer M2M, politique bornée côté bridge, audit
  `X-Oto-Sub`). Visibilité = régime commun (activation × sélection 0019/0050 — hors
  socle, installable). Pilote : le **bridge back-office d'un client pilote** (repo privé),
  migré `bridge`→`http` le 2026-07-16 (credential au groupe finance, réseau VPC
  privé). Le concept « remote data-driven » (base_url sur un provider hors registre)
  subsiste dans `org_secret_meta`, mais **sans entrée de catalogue** `kind="remote"`.
- **Tool API-keyé = déclarer le connecteur dans le registre** (`providers/<nom>.py`)
  (avec `keyed=True` + `auth_modes`) — `KEY_PROVIDERS` et tout le reste en
  dérivent. Le coffre `connector_credentials` est générique (pas de colonne
  par provider) : aucune migration de schéma à ajouter. Sinon `resolve_api_key`
  lève `Unknown provider` à l'appel. Puis poser la clé plateforme en DB via
  `oto_admin_set_platform_key` (plus de bootstrap SOPS — le provider sans clé
  DB n'a simplement pas de mode plateforme).
- **Credential = champs déclarés (modèle générique multi-champs, ADR 0011)** : un
  provider porte `credential_fields` (`CredentialField` name/label/secret) ou
  les dérive de `secret_kind` (`api_key`=1 champ, `basic_auth`=2). Le coffre encode
  les champs dans l'unique `secret_enc` via `credentials_store.pack_secret`/
  `unpack_secret` (3 formats : valeur brute 1 champ / base64 `email:password` /
  json ≥2). L'endpoint `/api/settings/api-keys/{provider}`, le formulaire dashboard
  et `status_for` bouclent sur `secret_fields` — **zéro branche par connecteur** ;
  `secret=True` ⟹ la valeur ne sort par AUCUNE surface (#671, 2026-08-31 : le cran
  `reveal`, qui l'ouvrait par défaut sur 55 connecteurs, est retiré du dataclass) ;
  un nouveau connecteur multi-secrets = une déclaration. Résolution : `resolve_api_key`
  (1 clé keyed + platform/quota) **ou** `resolve_credential_fields` (byo multi-champs
  sans quota, ex. `silae` : client_id/client_secret/subscription_key). `cookie`/`oauth`
  (linkedin/google) ont des flux dédiés → `secret_fields` vide.
- **Sonde « tester la connexion » par connecteur** (`connectors/verify.py`, registre
  calqué sur `browser_session.register`) : un connecteur enregistre une `_verify(fields)`
  qui **lève sur échec** (le message d'exception = le retour d'erreur). Capacité unique
  `connectors.verify` (MCP `oto_instance(op="verify")` — console ADR 0047 + REST `POST /api/me/connectors/{provider}/verify`,
  `authz=ORG_MEMBER`, `level` auto|org) → `{ok, error, elapsed_ms, level, ref}`, jamais un 500 ;
  `level`/`ref` (ex. `org:2:salesforce`) DÉRIVÉS de la même entité, sinon un `ok` sous
  `auto` ne dit pas quel cran de la cascade a répondu. `run()` transporte aussi
  `instance=(entity_type, entity_id, account)` aux sondes qui le DÉCLARENT — vital dès
  qu'une sonde a un effet de bord (rotation : cf. `docs/connector-vault.md`) ;
  `providers.public_catalog` expose `verifiable: connector_verify.supports(name)` (front
  gate le bouton). **Une bonne sonde teste l'auth ET les scopes**, pas juste l'auth :
  seed Zoho (`tools/zoho.py::_verify`) fait un refresh OAuth brut (valide client/secret/
  refresh/région d'un coup + capte le `scope` accordé) PUIS une **lecture réelle**
  (`ZohoClient.list_records` sur Contacts/Deals/Accounts/Leads, `per_page=1`) — une clé
  qui authentifie mais n'a **aucun scope CRM** (ex. clé Zoho **Analytics** posée par erreur
  sur le connecteur CRM) est rejetée avec le scope réel dans le message. ⚠️ Gotchas Zoho
  empiriques : le refresh renvoie **HTTP 200 + body `{"error":"invalid_client"}`** (région/
  client faux) ou `invalid_code`/`invalid_grant` (refresh mort) ; l'API CRM **v7 exige un
  param `fields`** (une lecture nue → 400, pas un scope-mismatch) → sonder via `list_records`
  (qui fournit les `DEFAULT_FIELDS`), pas un `GET /crm/v7/{module}` brut.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
  ⚠️ **TOUTE la prose vit AVANT le bloc `Args:`, qui se place en DERNIER — et aucun
  titre de section Google (`Returns:` multi-lignes, `Examples:`, `Note:`)** (2026-09-01,
  #761). Le parsing fastmcp ne sert que la PREMIÈRE section de prose + les params :
  un paragraphe après `Args:` ou une section `Returns:` est JETÉ en silence — 61
  outils amputés (~9 600 c.), dont le paragraphe des jetons `_*` d'`oto_call`, jamais
  parvenu à un agent. Un « Returns — `{…}` » en prose sert la même information sans
  être découpé (`Returns: …` sur UNE ligne passe, avec continuation indentée non).
  Tripwire sur le montage réel : `tests/test_docstring_prose_served.py`.
- **Doc how-to d'un connecteur = un markdown**, `oto_mcp/connector_docs/<nom>.md`
  (nommé comme son module), sections `## <kind> — <titre>`, servie au catalogue et à
  toutes les fiches. Une URL de rappel ne s'y écrit JAMAIS en dur — marqueur
  `{{callback:/chemin}}` résolu à la lecture, car elle diffère prod/preprod (tripwire).
  C'était un dict de 850 lignes de chaînes Python : la prose y devenait intouchable, et
  la fiche Salesforce a fini par décrire un modèle d'app que Salesforce avait désactivé.
- **Aucune résolution de secret côté serveur hors DB/env de process** : pas de
  `get_secret`/`require_secret` oto.config dans le code serveur (l'unit pose
  `OTO_CONFIG_DISABLE_SOPS=1`, tout résidu échoue fort).
- LinkedIn nécessite le **vrai Google Chrome système** (`google-chrome-stable`, apt)
  sur l'host — PAS le Chromium bundlé Patchright (empreinte TLS ≠ Chrome de bureau
  → bloqué par LinkedIn). `_require_chrome_channel` (`tools/linkedin.py`) force
  `channel="chrome"` et lève une erreur si absent.
- WhatsApp/Telegram/Instagram = messagerie **Unipile** (cf. §WhatsApp) — aucune dép
  Node côté backend. Le Baileys Node (`oto-core/.../whatsapp/node/`) ne sert plus
  qu'à la CLI `oto whatsapp` (fallback archivé).
- Attio (`tools/attio.py`) expose CRUD complet : records (companies/people/deals),
  notes (sauf update body, limite API), tasks, lists, entries, workspace_members,
  comments, threads, meetings, call_recordings + meta (objects, attributes). Pas
  de quota plateforme — chaque user pose sa clé sur `/account`. **Gotcha** :
  `attio_comment op="threads"` renvoie 400 sans `parent_object`/`parent_record_id`
  — toujours filtrer par parent (le nom `attio_list_threads` cité ici jusqu'au
  27/08 n'existe plus depuis la consolidation ADR 0047 du 11/08).
  ⚠️ **Attio AVALE les query params qu'il ne connaît pas en rendant 200** — un
  filtre inventé y ressemble à un filtre qui marche. Vérifié le 27/08 par
  différentiel (valeur absurde : 400 = param reconnu, 200 = param avalé) après
  quatre signaux d'usage : deux paramètres ne filtraient RIEN en prod
  (`tasks.list` envoyait `completed` là où Attio attend `is_completed` ;
  `meetings.list` envoyait un `offset` qui n'existe pas dans son contrat — seul
  `cursor` pagine). Corollaire tenu depuis : on n'expose que ce que l'amont
  honore, `/v2/notes` n'a donc **ni tri ni filtre de date**, et sa page par
  défaut (10, les plus ANCIENNES) est annoncée dans la docstring au lieu d'être
  subie.

## Les rows PG sont des DICTS, jamais positionnels (vécu 2026-06-25)

- `psycopg[binary]` + `psycopg-pool` (PostgreSQL managed Scaleway `otomata-main`, DB `oto_mcp`) pour le state par utilisateur — migré depuis SQLite le 2026-05-20. Row factory custom dans `db/_conn.py` (`_str_dict_row`) qui normalise `datetime`/`date` → strings "YYYY-MM-DD HH:MM:SS" : sinon `JSONResponse` crash sur `/api/me` car le code historique attend des strings comme avec SQLite. ⚠️ **Les rows sont des DICTS (accès par nom de colonne `r["col"]`), JAMAIS positionnel `r[0]`** (→ `KeyError: 0`). Vécu 2026-06-25 : deux fonctions RBAC en `r[0]` plantaient à chaque appel, **masqué** par leur fail-open + des tests qui stubbaient ces fonctions → bug invisible jusqu'à un seed réel. Leçon : un **fail-open silencieux + des tests stubbés cachent un bug de forme de row** ; exercer le vrai chemin (cf. [[feedback_verify_empirically]]).
