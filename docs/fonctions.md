---
title: Les fonctions — du code pur, stocké et exécuté par Oto
type: explanation
description: >-
  `oto_function` (ADR 0073) : un calcul propre à un client — entrée JSON, résultat,
  avertissements, fichiers — stocké en versions immuables, publié par un membre de sa
  portée sous la garde de ses tests, et exécuté dans un bac à sable Pyodide sous Deno sans
  réseau. Ce que le bac à sable refuse au code et comment on le prouve, où il vit sur
  une instance, ce que les tests garantissent et ce qu'ils ne garantissent pas. À lire
  avant de toucher `oto_mcp/functions/`, `db/functions.py` ou `capabilities/functions.py`.
---

# Les fonctions

Une **fonction** remplace le micro-service qu'on montait à côté d'Oto pour quelques
centaines de lignes de calcul (le pattern bridge d'ADR 0037). Elle **ne lit rien d'Oto,
n'appelle aucun réseau et ne voit aucun secret** : l'agent lui passe tout ce dont elle a
besoin, elle rend un résultat exact. Décision et arbitrages : ADR 0073 (meta-repo).

## Les verbes, et qui peut quoi

| op | ce qu'elle fait | qui |
|---|---|---|
| `list`, `get`, `versions` | lire (le code n'est rendu que par `get`) | membre du scope |
| `create`, `propose` | écrire une version, toujours PROPOSÉE | membre du scope |
| `run` | exécuter la version PUBLIÉE ; ses fichiers vont dans `project` | membre du scope |
| `test`, `publish`, `refuse` | exécuter du code non publié, et en décider | membre du scope, une **personne** |

Scope : l'org active (défaut) ou `scope='user'`. Surface bêta (`BETA_TOOLS`), REST
`POST /api/me/functions`.

- **Une version est immuable.** Corriger, c'est proposer la suivante (`expected_version`
  refuse une proposition faite sur une lecture périmée). Seul change son statut :
  `proposee` → `publiee` | `refusee`.
- **Publier rejoue les tests dans le bac à sable** et refuse une version sans test
  (`no_tests`) ou avec un test rouge (`tests_failed`, les cas en `details`).
- **Republier une version antérieure est le retour arrière** : le pointeur
  `functions.published_version` revient dessus, rien n'est réécrit.
- **Stockage** : deux tables à part (`functions`, `function_versions`), pas des nœuds.
  ADR 0073 range une fonction comme une page de rôle `fonction` ; les pages natives
  n'ont encore ni versions ni verrou (ADR 0072), le rattachement viendra avec elles.

## Le contrat d'une version

`sources` = `{nom: contenu}` à plat : modules `.py`, tests `test_*.py`, données `.json`
(`functions/contract.py`, qui valide à l'écriture ce que l'exécuteur chargera).
`entrypoint` = `module:fonction`, appelée avec l'entrée, qui rend
`{"result": …, "warnings": [str], "files": [{"name", "content": bytes, "mime"?}]}`.
Dépendances : la liste blanche `contract.DEPENDANCES` (aujourd'hui `openpyxl`), des roues
Python PURES embarquées avec le bac à sable.

Les tests sont des **fonctions `test_*` simples** (pas de pytest) : le harnais les appelle
une à une. Un test peut lire un `.json` posé à côté (`open('cas.json')`).

## Le bac à sable

`functions/executor.py` lance **un processus Deno par exécution** (`functions/sandbox.ts`),
hors de la boucle (`run_in_threadpool`), qui charge Pyodide et joue le harnais
(`functions/harness.pyodide`, lu comme du texte — extension non `.py`, pour que rien ne l’importe). Les sources vivent dans le système de
fichiers EN MÉMOIRE de Pyodide.

| barrière | comment elle tient | preuve |
|---|---|---|
| lecture du disque, écriture, environnement, sous-processus | une seule permission Deno : `--allow-read` sur Pyodide et les roues | `test_le_code_ne_sort_pas_du_bac_a_sable` (éprouvé : ouvrir les permissions le fait rougir) |
| réseau | aucune permission ; paquets résolus en `--cached-only` contre `deno.lock` | lu sur la commande lancée |
| secrets du serveur | environnement du processus VIDÉ (`DENO_DIR`, `NO_COLOR`, `PATH`) | lu sur l'appel |
| réponse forgée | la réponse porte un nonce gardé hors de `globalThis` ; sans lui, échec | `test_une_reponse_forgee_sans_nonce…` |
| boucle sans fin | délai (`DELAI_S`), processus tué | `test_une_boucle_sans_fin…` |
| sortie sans fin | lue EN FLUX, plafond `MAX_REPONSE` : au-delà, processus tué, `ReponseTropLongue` | `test_une_sortie_au_dela_du_plafond…` |
| identité, réseau, mémoire du serveur | l'isolement système déclaré (`functions/isolement.py`, ci-dessous) | lu sur la commande lancée |

### L'isolement du processus

Les permissions de Deno sont une barrière ; le processus en a une seconde, posée par le
système, qui ne dépend pas d'elles. Le mode se DÉCLARE par `OTO_FUNCTIONS_ISOLATION` :

| mode | pour | ce qu'il pose |
|---|---|---|
| `systemd` (défaut) | un service qui peut lancer une unité sous un autre uid | une unité transitoire `systemd-run` (`oto-fn-<hasard>.service`) sous l'utilisateur dédié `OTO_FUNCTIONS_USER` (défaut `oto-fonctions`, jamais privilégié) : `PrivateNetwork`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, aucune capacité, `SystemCallFilter=@system-service @pkey`, `RestrictAddressFamilies=AF_UNIX`, `MemoryMax`, `TasksMax`, `RuntimeMaxSec` (délai de l'appelant + marge). L'entrée et la sortie passent par `--pipe` : le pont parle toujours par ses deux flux. |
| `bwrap` | un service sans privilège | `bwrap --unshare-all` dans un espace de noms utilisateur non privilégié : ni réseau, ni processus, ni IPC de l'hôte ; un système de fichiers qui ne montre que `/usr`, le bac à sable et les scripts, en lecture seule, plus un `/tmp` vide ; environnement vidé ; aucune capacité ; mémoire bornée par `prlimit --data`. Le processus garde l'uid du service mais ne voit aucun de ses fichiers. |
| `direct` | poste de dev, CI — à DEMANDER | Deno lancé tel quel : seules ses permissions bornent le code. |

Une valeur inconnue, un outil absent (`systemd-run`, `bwrap`, `prlimit`) ou un
utilisateur dédié absent ou privilégié lèvent : 503 `sandbox_unavailable`. **Jamais de
repli** vers un mode plus faible. Le mode `systemd` ne s'ouvre pas à un service sans
privilège par une règle polkit : une règle qui autorise un uid à démarrer des unités
transitoires ne borne ni l'uid ni les propriétés de l'unité démarrée — c'est le cas du
mode `bwrap`.

⚠️ **Les tests prouvent la non-régression d'un code HONNÊTE, pas l'innocuité d'un code
hostile** : le code tourne dans le même interpréteur que le harnais et pourrait le
tromper. Depuis le 28/10/2026, `test`/`publish` sont ouverts au membre de la portée :
plus aucune relecture de plateforme ne garde ce point. Ce qui borne un code hostile est
le bac à sable (ni réseau, ni secret, mémoire et durée bornées) ; ce qu'une fonction
rend, son org le tient de ce que ses membres publient.

⚠️ **La mémoire** : `--max-old-space-size` borne le tas V8, pas la mémoire WASM de
Pyodide ; c'est l'isolement qui borne le processus entier (`isolement.MEMOIRE_MO` :
`MemoryMax` en mode `systemd`, `prlimit --data` en mode `bwrap`). En mode `direct`, rien
ne la borne. Plafonds chiffrés : à mesurer sur le pilote (ADR 0073 §5).

⚠️ **Tester, publier, refuser sont des gestes de PERSONNE** : sous un jeton de délégation
(un agent hébergé), ils sont refusés en 403 `code_human_only`, comme l'épreuve et la
publication d'une recette-code (`capabilities/_code_humain.py`). Ce sont les seuls gestes
qui exécutent ou décident du code non publié.

⚠️ **Pyodide 0.26 tourne sous Deno par la compatibilité Node**, donc en `npm:` : importé
depuis un fichier local, il se croit sous Node et appelle `require`. Le cache npm se
remplit par un PREMIER LANCEMENT réel (`deno cache` ne rapatrie pas les dépendances
optionnelles de `ws`, que Deno résout au lancement) et l'exécuteur lance Deno contre le
verrou (`--lock --frozen`) : sans lui, Deno refait une résolution que le cache n'a pas.

## Le pont : du code qui appelle des outils (#1202)

L'exécution ci-dessus est un ALLER SIMPLE : la tâche entre, une réponse sort, le code n'a
rien pu demander. `functions/pont.py` ouvre un second mode, **conversationnel**, à côté —
l'aller simple d'`oto_function` n'en est pas touché. C'est la fondation des recettes en
code (#1202) : `oto_recipe` le sert aux recettes `kind: "code"` (`docs/recettes.md`).

- **Le code** reçoit `oto` et fait `await oto.call(outil, arguments)` : le résultat de
  l'outil, ou `oto.ToolError` (`code`, `message`). Point d'entrée `async def` (une
  fonction ordinaire est acceptée, sans appel d'outil). Harnais : `harness_pont.pyodide`,
  joué par `runPythonAsync`.
- **Le protocole** (`sandbox_pont.ts`) : une ligne JSON par message. Le bac à sable écrit
  `{type: "call", id, tool, args}` puis `{type: "done", …}`, chaque ligne préfixée par le
  nonce que seule sa fermeture JS détient ; le serveur répond `{id, ok, result | code,
  message}` sur l'entrée du processus. Une ligne sans nonce vient du code : ignorée — il
  ne forge ni appel ni fin (`test_le_code_ne_forge_ni_un_appel_ni_une_fin`). Aucun import
  Deno de plus (`--cached-only --frozen`) : le lecteur de lignes est écrit à la main.
- **Le bac à sable ne change pas** : mêmes permissions (`executor.commande`), même
  environnement vidé, même isolement (`executor.lancement`). Tout ce que le code peut
  faire passe donc par le pont. Une ligne au-delà de `MAX_REPONSE` arrête le processus
  (`ReponseTropLongue`) ; de sa sortie d'erreur, seule la fin est gardée.
- **Le pont ne décide d'aucun droit** : il passe chaque demande à `appeler`, que
  l'appelant lui donne. Dans ce lot, le seul est `recipes/pont.appeleur_en_lecture` :
  les outils DÉCLARÉS EN LECTURE, sous les gardes d'`oto_call` (`meta.executer_cible`),
  avec l'identité de l'appelant — un outil qui écrit est refusé avant tout appel. Une
  recette-code y ajoute `data_rows` et `data_write` (`recipes/pont.appeleur_de_recette`),
  par le même `meta.executer_cible` : mêmes gardes, même journal.
- **Le budget** (`pont.Budget`) borne une exécution : appels, unités FACTURÉES
  (`quantity`), durée. Atteint, les appels suivants sont refusés (`budget_exhausted`) et
  le reçu dit lequel (`stopped`). La durée laisse ensuite `GRACE_S` au code pour rendre
  son reçu ; au-delà il est tué et l'exécution lève. Pas de reprise au milieu d'une
  fonction : une relance repart du début, d'où des écritures idempotentes le jour où il y
  en aura. Un résultat au-delà de `MAX_RESULTAT` est refusé APRÈS l'appel (facturé).
  ⚠️ **La durée n'attend aucun appel au-delà de sa borne** : un outil synchrone joue
  dans un fil que Python n'interrompt pas, et une tâche annulée peut tarder à céder ;
  l'appel échu est donc abandonné (annulé, pas attendu) et le code
  reçoit aussitôt `budget_exhausted`, la fin attend au plus `GRACE_S` ce qui vole encore
  puis le journalise et l'abandonne. Au-dessus, `oto_recipe` borne une recette-code à
  `max_seconds + GRACE_S + 15 s` (504 `recipe_timeout`). Un compte de connecteur à
  nommer (plusieurs comptes sans défaut, ou nom introuvable) revient au code sous
  `account_required`, et non `invalid_input` (`meta.executer_cible`).
- **Des appels en parallèle sont servis en parallèle** (`asyncio.gather` dans le code),
  au plus `Budget.en_vol` à la fois (`EN_VOL`, 5) : chaque demande est jugée par le
  budget à sa lecture, sans `await` entre le compte et la décision (`max_calls` exact),
  puis jouée dans sa tâche ; les réponses repartent par `id` dès qu'elles sont prêtes.
  Servis un par un jusqu'au 27/10/2026 — cinq recherches de ~11 s lancées ensemble
  passaient l'une après l'autre et une recette dépassait sa durée. ⚠️ Le budget se
  juge deux fois : à la lecture (`max_calls`), puis quand l'appel obtient sa place
  (unités et durée, sans `await` jusqu'au départ) — un appel qui a attendu en file ne
  part pas sur un plafond épuisé, et sa place dans `calls` est rendue. La quantité
  n'étant connue qu'après, le plafond d'unités peut être dépassé de ce que facturent
  les appels déjà PARTIS : au plus `en_vol`. `max_writes` se réserve avant le premier
  `await` (`recipes/pont.py`), il reste exact.
- **Une fin en erreur n'abandonne pas ce qui est parti** : un code qui lève (un appel
  d'un `gather` a échoué) laisse ses appels partis aller à leur terme, bornés par la
  durée ; seuls ceux encore en file sont annulés, sans partir ni compter ; puis
  l'exécution lève. Un appel annulé n'est jamais réglé comme un succès
  (`meta.executer_cible`).
- **Côté serveur, une pompe asyncio** (`create_subprocess_exec`) : les appels d'outils
  sont attendus sur la boucle, sans fil ni SQL dans ce module.

⚠️ **Auteur ≠ appelant** : le code d'une recette publiée tourne avec les droits de qui
l'exécute, jamais de qui l'a écrite (#1202, tranché le 28/10/2026) : son auteur
l'éprouve et la publie dans sa portée. Le pont en lecture (`appeleur_en_lecture`)
n'appelle que des outils en lecture ; une recette-code y ajoute les tableaux et les
écritures et envois chez un tiers qu'elle DÉCLARE, par tout outil de connecteur classé
par le verbe de son op — jamais une op qui détruit (`recipes/ecritures.py`,
`recipes/effets.py`, `docs/recettes.md`).

## Où vit le bac à sable

`OTO_FUNCTIONS_SANDBOX_DIR` → un répertoire posé par
`python scripts/installer_bac_a_sable.py <dir>` : `deno` (binaire officiel, SHA-256),
`deno_dir/` (cache npm, intégrité SHA-512 du verrou), `wheels/` (SHA-256 PyPI). Le réseau
ne sert qu'à l'installation ; le répertoire peut ensuite passer en lecture seule.
L'installation se termine par une exécution de contrôle.

**En production, c'est le déploiement qui le pose** : `bg_install`
(`deploy/oto-mcp-bluegreen.sh`, commun à notre box et aux instances cibles) lance
l'installateur dans l'arbre de la couleur qu'il installe, `<arbre>/.bac-a-sable`, après
`uv sync`. Le bac à sable suit donc le TAG, comme le code : une couleur porte le Pyodide
de son `deno.lock`, et un retour arrière retrouve le sien. Sans `OTO_FUNCTIONS_SANDBOX_DIR`,
l'exécuteur prend ce répertoire s'il existe (`executor.DEFAUT`). Rejouable : le réseau ne
sert qu'à la première pose dans une couleur et à un changement de version. Un échec
(réseau, empreinte, exécution de contrôle) **arrête l'installation** — la couleur n'est
pas démarrée, l'ancienne reste en service : une couleur qui rendrait
`sandbox_unavailable` en silence est une divergence muette. L'exécution de contrôle
passe par l'isolement déclaré : une machine où il ne s'applique pas (utilisateur dédié
absent, outil manquant) ne sort pas de l'installation en succès. Le processus du bac à
sable ne peut pas écrire le répertoire : il tourne sous un autre utilisateur que son
propriétaire et sur un système de fichiers en lecture seule (mode `systemd`), ou dans
un montage en lecture seule (mode `bwrap`) ; l'utilisateur dédié doit seulement pouvoir
LIRE l'arbre de la couleur (le bac à sable et `oto_mcp/functions/`). ⚠️ La box doit joindre `github.com` (Deno), `registry.npmjs.org` (Pyodide) et
`files.pythonhosted.org` (roues) au moment du déploiement, et être en `x86_64`.

Absent (ni variable ni `<arbre>/.bac-a-sable`), l'instance n'exécute pas de fonction :
`run`/`test`/`publish` d'une fonction et l'épreuve, la publication et l'exécution d'une
recette-code rendent 503 `sandbox_unavailable`, le reste répond. La CI l'installe avant la suite (sinon les bancs
de l'exécuteur ÉCHOUENT en CI ; sur un poste sans bac à sable ils sont passés en le
disant).

**Mettre à jour Pyodide** : la version est dite à trois endroits — `sandbox.ts`
(`npm:pyodide@…`), `executor.PYODIDE`, `deno.lock` — que
`test_la_version_de_pyodide_est_la_meme_partout` garde alignés ; régénérer le verrou par
`deno cache --no-config --lock=oto_mcp/functions/deno.lock oto_mcp/functions/sandbox.ts`.

## Ce que le journal garde

Chaque appel passe par `tool_calls` comme tout outil — arguments compris
(`calllog.truncated_args`, bornés à `MAX_ARG_CHARS` par valeur, la coupe déclarée) : l'entrée d'une fonction n'y échappe pas.
Les fichiers produits vont dans `project_files` (qui, quand, quel projet).
