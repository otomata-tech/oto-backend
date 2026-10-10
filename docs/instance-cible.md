---
title: Instance cible — déployer le tronc sur une autre machine que la nôtre
type: how-to
---

# Instance cible — déployer le tronc sur une autre machine que la nôtre

> **Le fait à retenir** : une instance tierce est un **déploiement du tronc**, jamais une
> copie (ADR 0070). Elle tourne le même code, au tag qu'on choisit pour elle, avec la
> même bibliothèque bleu/vert que notre box — et tout ce qui la distingue de nous est
> une **déclaration**, rangée hors du dépôt. Ce dépôt est public : il ne nomme aucune
> cible, il ne porte que la forme de leur déclaration.

Issue d'origine : #967 (« une chaîne de livraison, N cibles »). Les deux instances sont
**autonomes** : monter une cible est une décision de son propriétaire. Cette décision peut
être de **suivre le tronc** (#1195) — monter, quelques heures après, chaque tag déployé en
prod chez nous, préprod de la cible puis sa prod, par un déclencheur **planifié** dans son
dépôt : rien dans la chaîne n'exige plus de geste humain, migrations comprises (§ Les
migrations, jouées par la montée). Le workflow d'une cible ne consulte qu'elle-même et le
tronc : aucun autre déploiement, aucune autre instance, aucun autre run n'entre dans sa
décision. Rien, dans ce dépôt, ne l'appelle : son déclencheur vit dans le dépôt privé du
propriétaire de la cible (§ Le déclencheur, dans le dépôt du propriétaire).

## La bibliothèque bleu/vert, commune

`deploy/oto-mcp-bluegreen.sh` sert nos deux environnements **et** chaque cible. Tout ce
qu'elle codait en dur pour notre box est devenu une variable que le wrapper **déclare** :
verrou (`BG_LOCK`), Caddyfile validé avant chaque reload (`BG_CADDYFILE`), script et
unité de vidange (`BG_DRAIN`, `BG_DRAIN_UNIT`), mode du lanceur (`BG_LANCEUR`), en plus
des ports, arbres, amont et couleur qu'elle recevait déjà. **Aucune n'a de défaut** : une
variable oubliée refuse le chargement en la nommant, et un pointeur de couleur absent est
une panne nommée, plus une invitation à supposer « bleu ».

Le contrôle du trafic public après le reload de Caddy **attend, borné**, tant qu'aucune
réponse HTTP ne revient (code 000) : au premier déploiement sur un nom d'hôte neuf, Caddy
obtient le certificat ACME quelques secondes après le reload. Un essai toutes les 2 s, au
plus 10 essais et 20 s (`PUBLIC_PAS`, `PUBLIC_ESSAIS`, `PUBLIC_ATTENTE_MAX`), puis échec net
— nombre d'essais, durée, dernier code — et rebascule comme avant. Un code HTTP reçu autre
que 200 échoue tout de suite : la couleur répond mal, l'attente serait servie au public.

**Notre box fait exactement les mêmes gestes qu'avant.** Nos deux wrappers
(`deploy/oto-backend.sh`, `deploy/oto-backend-canari.sh`) déclarent les valeurs qui
étaient en dur, à l'identique. La preuve est un banc, pas une relecture :
`tests/deploy/test_gestes_bleu_vert_967.py` rejoue chaque scénario réel — bascule dans
les deux sens, retour arrière, préproduction, vidange, couleur morte, `caddy validate`
qui refuse, trafic public en échec, lanceur figé — avec des doublures qui journalisent
chaque commande, et compare la trace (commandes, sortie, état final des fichiers) à la
référence enregistrée depuis les scripts d'avant (`tests/deploy/gestes_bleu_vert/`).
Un geste de notre box qui change fait rougir ce test ; le changer exprès, c'est
régénérer la référence (`tests/deploy/_banc_bleu_vert.py`) et relire son diff. Depuis le
lot 5 (30/09/2026), nos scénarios tournent en `BG_LANCEUR=versionne` : le seul geste qui a
changé à la régénération est la propagation du lanceur (`cp -a start-encrypted.sh`, puis
`chmod`), disparue. Le mode historique `propage` reste rejoué (`prod-lanceur-propage`,
`prod-lanceur-fige`) jusqu'au lot 5b.

⚠️ Le dépôt ne se propage pas seul sur notre box : `/opt/deploy/` y est une **copie**.
Un changement de la bibliothèque n'y agit qu'une fois recopié (infra, sur go d'Alexis).
Comparer la box au dépôt se fait sur le code, en-têtes retirés :
`grep -v '^#' <fichier> | sha256sum` des deux côtés.

## Le lanceur, générique

`deploy/lanceur_secrets.py` démarre le serveur d'une cible (`BG_LANCEUR=versionne`). Il
est **dans le tag** : ni propagé, ni édité sur la machine.

- **Ce qu'il tire se dérive de l'inventaire** : `oto_mcp/env_secrets.py` nomme, parmi
  les variables de `env_inventory.py`, celles qui sont des secrets. Les requis sont tirés
  à chaque démarrage ; les facultatifs, l'instance les **déclare**
  (`OTO_SECRETS_OPTIONNELS`). Un secret introuvable refuse le démarrage.
- **Où il le tire se déclare** : le Secret Manager du projet Scaleway **de la cible**
  (`OTO_SECRETS_REGION`, `OTO_SECRETS_PROJET`), sous le chemin de son rôle
  (`OTO_SECRETS_CHEMIN`, `/preprod` ou `/prod` : chaque rôle a sa `DATABASE_URL`). Chaque
  secret porte le **nom de sa variable**. Aucun identifiant n'est écrit nulle part.
- **La clé d'API** arrive par `LoadCredential=scw:…` de l'unité, lisible du seul service.
- **Le `.env` ne porte que le non-secret** : un secret trouvé dans l'environnement
  refuse le démarrage.

Notre box démarre par ce lanceur depuis le 30/09/2026 (lot 5, décrit pas à pas plus bas,
§Passer notre box au lanceur) : nos deux wrappers déclarent `BG_LANCEUR=versionne`.
`deploy/start-encrypted.sh` et `-canari.sh`, l'ancienne déclaration de secrets **propre à
notre cible** (identifiants de notre projet, Mollie, Pennylane), ne servent plus qu'au
retour arrière du lot 5 : ils partent au lot 5b avec le mode `propage`.

Le lanceur démarre le serveur, ou — `--script CHEMIN` — un script de l'arbre sous son
Python (l'archive du journal, un script d'entretien), avec les mêmes secrets ; `--noms`
ne démarre rien et imprime les **noms** de l'environnement final, jamais une valeur. Un
seul mécanisme de secrets pour tout ce qui tourne sur la box (`docs/commands.md` §Un
script d'entretien par le lanceur).

## L'amorce — une instance naît du code

`deploy/cible/amorcer.sh` fait naître un rôle (préprod ou prod) sur une machine nue, et
le remet à sa forme déclarée à chaque déploiement : c'est le déploiement qui l'appelle,
depuis l'arbre du tag, avant la bascule. Idempotente — la seconde fois, elle ne recrée
rien et réécrit seulement ce qui se dérive de la déclaration.

| Ce qu'elle pose | Où (dérivé du nom d'instance `<i>` et du rôle `<r>`) |
|---|---|
| Utilisateur de service, système, sans shell — **jamais root** | `oto-<i>` |
| Python du plancher du `pyproject` (3.10), via uv | `/opt/<i>/python` |
| Deux arbres (clone du tronc) et leur venv | `/opt/<i>/<r>-blue`, `/opt/<i>/<r>-green` |
| Environnement du rôle : `app.env` (non-secret), `lanceur.env`, ports | `/etc/<i>/<r>/` (0700, root) |
| Pointeur de couleur, posé à la naissance seulement (bleu : le 1er déploiement installe la verte) | `/etc/<i>/<r>/active` |
| Unité par couleur, confinée (`NoNewPrivileges`, `ProtectSystem`, `CapabilityBoundingSet=`, lien-local refusé) | `<i>-<r>@.service` (gabarit `deploy/cible/instance@.service`) |
| Amont Caddy initial | `/etc/caddy/upstream-<i>-<r>.conf` |
| Vidange (chemin stable : elle tourne après le déploiement) | `/usr/local/lib/<i>/oto-mcp-drain.sh` |
| Maintenance quotidienne, après la bascule, sur l'arbre qui sert — chaque rôle a sa base, donc la sienne | `<i>-<r>-maintenance.{service,timer}` |

Ce qu'elle **exige et ne pose jamais** — le socle, geste d'opérateur, chacun refusé en
le nommant s'il manque : root ; `uv`, `git`, `caddy`, `python3` ; la clé d'API du
Secret Manager dans `/etc/<i>/scw.key` (0600) ; un Caddyfile qui importe l'amont du rôle
et l'utilise dans le bloc du site :

```
import /etc/caddy/upstream-<i>-<r>.conf          # en tête, avant le bloc global

<hôte public du rôle> {
	handle /p/d/* {
		import <i>_<r>_upstream_docshare
	}
	import <i>_<r>_upstream
}
```

Le service ne peut pas réécrire son code (les arbres sont à root), n'a d'état que dans
son `StateDirectory`, et ne voit sa clé d'API que par `LoadCredential`.

## Les migrations, jouées par la montée (#1163, #1195)

Une montée **amène la base du rôle à la tête des migrations du tag** avant de démarrer quoi
que ce soit — ou refuse en le disant. Constaté avant #1163 : une cible servait le code d'un tag
dont la tête Alembic était 0037, sur une base restée en 0031, sans que rien ne le dise. #1163 a
fait refuser cette montée et jouer la migration à la main ; #1195 la fait jouer par la montée,
pour qu'une cible puisse suivre le tronc sans geste humain : une montée quotidienne se serait
sinon arrêtée à chaque tag porteur d'une migration (une cible a ainsi pris 18 versions de
retard).

**Où.** `deploy/cible/deployer.sh` définit la garde facultative de la bibliothèque bleu/vert
(`bg_garde_avant_demarrage`) : après l'installation du tag dans la couleur **inactive** et
**avant son démarrage**, elle exécute `deploy/cible/migrations_a_jour.py --migrer` sous le
lanceur de cet arbre (mêmes fichiers d'environnement et même clé d'API que l'unité du rôle,
donc la `DATABASE_URL` du rôle). L'arbre installé est le seul endroit de la machine qui porte le
registre **du tag** — la tête attendue en vient, jamais du code qui sert — et c'est depuis lui
que la migration se joue (`oto-mcp migrer upgrade head`, borné à 20 min). La montée tourne en
root par la porte : aucun droit de plus à donner au runner ni à la clé de déploiement.

| La base du rôle | Verdict |
|---|---|
| à la tête du registre du tag | la montée continue |
| neuve (aucune table) | la montée continue : le démarrage crée le schéma et pose la tête (`migrations-versionnees.md` §5.2) |
| en retard sur une révision du registre du tag, **chaîne linéaire** jusqu'à sa tête | **migrée** : `migrer upgrade head` depuis l'arbre du tag, puis relue ; le journal dit « de → vers » et la chaîne jouée. La montée continue seulement si la base est alors à la tête |
| en retard, mais une **fusion de files** sur le chemin | **refus** : la révision de fusion — une montée automatique ne migre pas à travers deux files mêlées ; à jouer à la main après lecture de `migrer history` |
| à une révision retirée par un squash (antérieure à la référence du registre) | **refus** : la révision, la référence, et le tag d'avant le squash qui la monte d'abord (`migrations-versionnees.md` §5.4) |
| sans `alembic_version`, plusieurs révisions, révision inconnue du tag (base plus récente, autre file), injoignable | **refus** : le motif, et les commandes qui lisent l'état (`migrer current`, `migrer heads`) |
| registre du tag à plusieurs têtes ou vide | **refus**, sans lire la base |
| migration en échec, ou pas à la tête une fois jouée | **refus** : la couleur ne démarre pas ; la base peut s'être arrêtée entre deux révisions — lire `migrer current` avant toute relance |

**Un index trop gros pour la montée.** Une révision qui pose un index CONCURRENTLY
(`migrations-versionnees.md` §5.1 — 0049 par exemple) refuse de le construire au-delà de son
seuil de taille : la montée s'arrête sur `ConstructionManuelleRequise`, la couleur ne démarre
pas. Le geste est une commande de l'arbre, jouée par le même lanceur que `migrer current`
ci-dessous, sur le rôle concerné (sa préprod d'abord, puis sa prod) : `lanceur_secrets.py
maintenance index-concurrents <révision>`, puis relancer la montée — la révision constate
ses index et passe. Un index **invalide** est nommé avec son `DROP` à jouer à la main, jamais
retiré par la commande ni par la montée.

Sur un refus, ce qui sert n'a pas été touché : rien n'a démarré, rien n'a basculé. Une base
injoignable est nommée par la classe de l'erreur seulement : son message peut porter l'hôte ou
l'utilisateur, et ce journal remonte jusqu'au run du workflow.

**La préprod répète la prod.** Chaque rôle a sa base, donc sa migration : la préprod de la
cible monte la première et joue les révisions du tag sur sa propre base ; sa prod ne monte que
si la préprod **sert déjà** ce tag (§ La chaîne). Une migration qui casse s'arrête donc sur la
préprod.

**Les migrations restent additives** (`live-migrations.md`) : pendant la vidange, l'ancienne
couleur tourne encore sur la base migrée. Une révision qui retire (`migrations-versionnees.md`
§5.3) se joue en deux tags, le code qui cesse de lire d'abord.

**Le retour arrière vérifie la base.** `action=retour` redémarre la couleur précédente sans rien
installer ; avant, `deployer.sh` exécute `migrations_a_jour.py --retour` dans l'arbre de cette
couleur (garde `bg_garde_avant_retour` de la bibliothèque bleu/vert). La base doit être
**exactement à la tête que connaît ce code** : une révision qu'il ne connaît pas — une montée l'a
migrée depuis — refuse, et on corrige vers l'avant, par un nouveau tag ; une base en retard sur
lui, neuve ou illisible refuse aussi. Le refus nomme la révision de la base et la tête du code ;
rien n'a démarré, la couleur en service continue.

Pour **lire** l'état sans rien jouer : `migrations_a_jour.py` sans argument (constat : 0 à
jour, 3 en retard sur une chaîne linéaire, 1 sinon), ou `migrer current` / `migrer heads`, par le
lanceur de l'arbre voulu :

```bash
systemd-run --pipe --wait --quiet --collect -p WorkingDirectory=/opt/<i>/<r>-<couleur> \
  -p EnvironmentFile=/etc/<i>/<r>/app.env -p EnvironmentFile=/etc/<i>/<r>/lanceur.env \
  -p LoadCredential=scw:/etc/<i>/scw.key \
  /opt/<i>/<r>-<couleur>/.venv/bin/python /opt/<i>/<r>-<couleur>/deploy/lanceur_secrets.py migrer current
```

Les bancs : `tests/deploy/test_migrations_cible_1163.py` (constat et refus communs sur un
registre Alembic réel, lecture d'une base PostgreSQL réelle quand il y en a une) et
`tests/deploy/test_cible_suit_le_tronc_1195.py` (migration d'une chaîne linéaire puis relecture,
fusion, squash et révision inconnue refusés sans migrer, retour arrière, et la chaîne en root
simulé : la garde avant le démarrage d'une montée comme d'un retour).

## La chaîne — monter une cible de version

`.github/workflows/deploy-cible.yml`, **à la main seulement** : depuis le dépôt privé du
propriétaire de la cible, qui l'appelle (`workflow_call`, § Le déclencheur, dans le dépôt
du propriétaire) — ou, pour une cible dont l'environnement serait dans ce dépôt, par
`gh workflow run deploy-cible.yml -f cible=<environnement> -f tag=vX.Y.Z -f etape=… -f acces=…`.
Dans les deux cas, le code exécuté vient du **tronc au tag demandé** (checkout de
`otomata-tech/oto-backend`, jamais du dépôt appelant) :

1. **Entrées** validées avant tout usage (elles finissent dans un `ref:` et un nom
   d'environnement ; appelé, rien ne les borne d'avance), puis **l'environnement de la
   cible doit exister et être protégé** (`deploy/cible/protection.sh`, par l'API de
   GitHub, dans le dépôt du run — le dépôt appelant quand il y en a un) : il exige un
   relecteur, ou ses déploiements sont limités à des branches et il déclare ses
   déclencheurs (§ Sans relecteurs requis) ; sinon, refus.
2. **La décision** : l'approbation du relecteur requis, ou — premier contrôle du job qui
   nomme l'environnement, seul à en lire les secrets — l'acteur du run figure dans la
   liste des déclencheurs. Un seul job derrière elle, qui porte toute la montée
   demandée : une montée, une décision.
3. **Le tag est sur la branche principale du tronc** (la porte le revérifie sur la
   machine).
4. **La déclaration** est jugée par l'inventaire du tag.
5. **Compatibilité inverse, facultative** : si l'environnement de la cible déclare un
   consommateur de son API (`CIBLE_CONSOMMATEUR`), le contrat que **ce tag** servirait
   chez elle est confronté au contrat qu'il épingle (`scripts/contrat-front.py`). Le tronc
   ne s'interdit rien pour une cible ; c'est la cible qui juge le tag au moment de
   monter. Contrat illisible = pas de montée.
6. **Préprod** de la cible, puis constat de ce qu'elle sert (`GET /api/version`). Sa base
   est amenée à la tête des migrations du tag avant tout démarrage — chaîne linéaire, sinon
   refus (§ Les migrations, jouées par la montée).
7. **Prod** de la cible — seulement si sa préprod **sert déjà** ce tag, constaté de
   l'extérieur. `etape=prod` seul est donc le geste du lendemain.

Un test (`tests/test_workflow_deploy_cible_967.py`) rougit si ce workflow, ou un script
qu'il exécute, se met à consulter un autre déploiement que celui de la cible.

`action=retour` rebascule un rôle sur sa couleur précédente, sans rien installer — si sa
base est à la tête que connaît ce code (§ Les migrations, jouées par la montée).

**L'accès** (`deploy/cible/appeler.sh`) est l'entrée `acces`, **choisie, sans défaut** —
absente ou inconnue, la montée refuse en la nommant ; jamais de repli d'un mode sur
l'autre :

- `tunnel` : runner hébergé par GitHub → `cloudflared` (dépôt APT signé) → tunnel
  Cloudflare Access de la cible, authentifié par **jeton de service** (sans jeton : refus
  nommé) → SSH ;
- `ssh` : runner → **`:22` de la machine**, directement, sans `cloudflared`, sans jeton,
  sans mandataire — le mode d'attente, tant que le tunnel n'existe pas.

Puis, dans les deux : SSH par la clé de déploiement, hôte **épinglé**
(`StrictHostKeyChecking=yes`, seul `CIBLE_SSH_KNOWN_HOSTS` fait foi) → **commande forcée**
vers la porte. La déclaration part sur l'entrée standard.

**La porte** (`deploy/cible/porte.sh`, posée une fois sur la machine à
`/usr/local/sbin/oto-cible-porte`) est le seul fichier de la chaîne qui y vit. Elle
n'accepte que `deployer|retour <preprod|prod> <vX.Y.Z>`, vérifie que le tag existe et
qu'il est **sur la branche principale du tronc** (miroir local du dépôt, dont l'URL est
écrite dans la porte et jamais reçue), extrait le `deploy/` **de ce tag** et lui passe
la main (`deploy/cible/deployer.sh` : amorce, bleu/vert avec la garde des migrations,
maintenance). L'amorce, la
bibliothèque et le lanceur sont donc toujours ceux de la version qu'on monte.

## Déclarer une cible, pas à pas

Chaque étape est un geste d'opérateur, hors de ce dépôt ; la chaîne refuse en le nommant
tout ce qui manque. Rien de ce qui suit ne s'écrit dans le dépôt : ni nom, ni hôte, ni
identifiant.

### 1. La déclaration

Partir du gabarit `deploy/cible/declaration.gabarit.json` : la forme complète, sans
aucune valeur. Il liste, pour chaque rôle, les variables que l'inventaire exige dans le
`.env` (identité, requises non secrètes) ; `OTO_ENV` vaut le rôle, et rien d'autre.
Ajouter les variables non secrètes voulues (inventoriées : `oto_mcp/env_inventory.py`),
les secrets facultatifs portés (`secrets_optionnels`), puis juger le document avec le
code du tag qu'on montera :

```
python3 deploy/cible/declaration.py verifier declaration.json
```

Un test (`tests/deploy/test_gabarit_cible_967.py`) garde le gabarit en phase avec
l'inventaire : une variable qui devient exigée y apparaît, ou le test rougit.

Le gabarit porte aussi `OTO_MCP_CLAUDE_APP_ID`, exigée **non vide** par le bleu/vert et non
par l'inventaire : la santé d'une couleur (`HEALTH_PATH`, en local puis en public) lit
`/.well-known/oauth-authorization-server`, que seule la façade DCR sert, et la façade n'est
montée que si cette variable est posée. Sans elle, la couleur démarre mais répond 404 et
la montée échoue en « couleur pas devenue saine » ; `verifier` la refuse donc en le disant.
La règle lit `HEALTH_PATH` dans la bibliothèque du tag : elle tombe si la santé change de
chemin.

Un rôle dont le MCP sert la façade (`OTO_MCP_CLAUDE_APP_ID`) devant **notre annuaire
administrable** — `OTO_MCP_LOGTO_M2M_ID` non vide dans `env` **et**
`OTO_MCP_LOGTO_M2M_SECRET` dans `secrets_optionnels` — porte aussi
`OTO_MCP_OAUTH_RELAY_HOSTS`, et l'hôte de son `OTO_MCP_PUBLIC_URL` y figure (#1164). C'est
exactement là que le serveur consulte la liste : la façade y cherche l'hôte de son URL
publique (`relay.relais_actif`), et le relais n'agit que si l'annuaire est administrable
(`docs/auth-logto.md` § Le relais d'autorisation). Hôte absent : le relais est éteint, la
métadonnée renvoie l'échange de jeton chez l'annuaire, et un client qui avait lu celle du
relais — un hôte qui change d'instance, par exemple — voit ses rafraîchissements refusés
jusqu'à sa prochaine découverte ; un client strict (RFC 9207) n'aboutit pas à sa première
autorisation. `verifier` refuse donc en nommant le rôle, l'hôte et la variable. La liste se
compare comme le serveur la lit : noms d'hôte nus séparés par des virgules, casse et point
final ignorés ; un schéma (`https://`) ou un port n'y correspond à rien. Un rôle sans ce
credential (le relais y répondrait 503) n'est pas concerné. Hors de portée de la
déclaration : un hôte réclamé par un **tenant** (`tenants.hosts`, en base) relève de
l'annuaire de ce tenant ; son inscription se constate avec
`oto-mcp maintenance oauth-relay-callbacks` (`docs/commands.md`).

### 2. Le Secret Manager du projet de la cible

Un secret par variable, **nommé comme la variable**, sous le chemin du rôle (`/preprod`,
`/prod`) : les requis
(`python3 -c "from oto_mcp import env_secrets; print(*env_secrets.secrets_requis())"`),
dont chaque `DATABASE_URL` vers la base de son rôle, et les facultatifs déclarés. Une
clé d'API dédiée, limitée à la lecture des secrets de ce projet, lue par la machine.
La valeur de référence de chaque secret est gardée par l'opérateur dans son
gestionnaire de mots de passe, jamais dans un dépôt ni dans une page.

### 3. La machine (socle)

Sur une Ubuntu 24.04 du projet de la cible, montée selon le socle d'une box tierce :

- `git`, `caddy`, `python3`, `uv` ;
- l'accès : en attendant le tunnel, le `:22` ouvert, **par clé seulement**
  (`PasswordAuthentication no`, `PermitRootLogin no`) — accès `ssh` ; puis le tunnel
  Cloudflare Access de la machine, une application SSH, et un **jeton de service dédié à
  cette cible** dans la politique de l'application — accès `tunnel` ;
- un utilisateur de déploiement **non root**, avec la clé CI en commande forcée :
  ```
  # ~deploy/.ssh/authorized_keys
  restrict,command="sudo /usr/local/sbin/oto-cible-porte \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 AAAA… ci-<cible>
  # /etc/sudoers.d/oto-cible
  deploy ALL=(root) NOPASSWD: /usr/local/sbin/oto-cible-porte *
  ```
- la porte, depuis un tag : `install -m 0755 deploy/cible/porte.sh /usr/local/sbin/oto-cible-porte` ;
- la clé d'API du Secret Manager : `install -d -m 0700 /etc/<i>` puis
  `/etc/<i>/scw.key` (0600, root) ;
- le Caddyfile qui importe l'amont de chaque rôle (§ L'amorce), et le DNS des hôtes
  publics.

⚠️ Le `:22` reste ouvert jusqu'à ce qu'**un déploiement réel soit passé par le
tunnel** ; on ne le ferme qu'après (socle, gestes J+2/J+7 ; § Passer au tunnel, puis
fermer le :22).

### 4. L'environnement GitHub de la cible

Un environnement **du dépôt qui déclenche** — le dépôt privé du propriétaire de la
cible (§ Le déclencheur) — au nom de la cible (le nom ne s'écrit que là, et dans l'entrée
`cible` du workflow) :

| Nom | Sorte | Contenu |
|---|---|---|
| `CIBLE_DECLARATION` | secret | la déclaration (JSON, sur une ligne : `jq -c`) |
| `CIBLE_SSH_HOTE` | secret | accès `tunnel` : l'hôte SSH de l'application Access ; accès `ssh` : le nom (ou l'adresse) de la machine |
| `CIBLE_SSH_UTILISATEUR` | secret | l'utilisateur de déploiement |
| `CIBLE_SSH_KNOWN_HOSTS` | secret | la clé d'hôte de la machine, sous le nom de l'hôte SSH (épinglée) — sous les deux noms pendant la bascule vers le tunnel (`hote-access,machine ssh-ed25519 …`) |
| `CIBLE_CONSOMMATEUR` | secret, facultatif | `{"nom", "depot": "owner/repo", "chemin", "cle": bool}` (sur une ligne) — le front qui consomme l'API de la cible |
| `CIBLE_SSH_CLE` | secret | la clé privée de déploiement |
| `CIBLE_CF_ACCESS_CLIENT_ID` | secret, accès `tunnel` | le jeton de service Access (identifiant) |
| `CIBLE_CF_ACCESS_CLIENT_SECRET` | secret, accès `tunnel` | le jeton de service Access (secret) |
| `CIBLE_CONSOMMATEUR_CLE` | secret, si `cle` | clé de lecture seule du dépôt du consommateur |
| `CIBLE_DECLENCHEURS` | secret, sans relecteur requis | les logins GitHub autorisés à lancer une montée, séparés par des virgules ou des espaces, à la casse exacte (§ Sans relecteurs requis) |

**Tout en secret, aucune variable** : l'environnement ne porte AUCUNE variable
(`vars.`). Ce dépôt est public, les journaux de ses runs aussi, et ils ne montrent
jamais ce qui désigne la cible — hôte, domaine, nom d'instance, slug, aucune valeur de
sa déclaration (D5). GitHub imprime l'`env:` de chaque step en tête de son journal et ne
masque d'office que les secrets : une variable s'y lirait en clair. Les secrets
structurés (déclaration, consommateur) se posent sur une ligne, pour être masqués
entiers. Ce qu'on en dérive (hôtes et leurs URL, domaines, instance, valeurs du `.env`,
dépôt du consommateur) est masqué (`::add-mask::`) par le premier geste de chaque job,
et les scripts parlent de « la préprod de la cible », jamais de son hôte
(`tests/test_workflow_deploy_cible_masquage_967.py`). Le nom de l'environnement, seul,
est public par nature (entrée `cible`, page des déploiements du dépôt) : le choisir
neutre.

**Protection de l'environnement — obligatoire** : dans ses réglages, « Required
reviewers » avec au moins un relecteur (celui qui décide des montées) — ou, là où GitHub
ne les offre pas (dépôt privé sans Enterprise), la liste `CIBLE_DECLENCHEURS` (§ Sans
relecteurs requis) — et les branches de déploiement limitées à la branche par défaut du
dépôt qui déclenche (celle d'où l'on lance le workflow). Le workflow le vérifie à chaque
montée et refuse de partir sans l'un ni l'autre — y compris quand l'environnement
n'existe pas encore, que GitHub créerait sinon à la volée, sans protection.

### 5. Monter

Depuis le dépôt du propriétaire (son workflow appelant, § Le déclencheur) :

```
gh workflow run monter-instance.yml -f tag=vX.Y.Z -f etape=preprod
gh workflow run monter-instance.yml -f tag=vX.Y.Z -f etape=prod
```

Une base en retard sur le tag est migrée par la montée elle-même ; une montée qui refuse
pour ses **migrations** nomme le motif et les commandes qui lisent l'état (§ Les migrations,
jouées par la montée).

La première montée d'un rôle le fait naître (amorce) et installe la couleur verte. Puis,
sur la machine, les valeurs **effectives** : `systemctl show <i>-<r>@green -p User -p
NoNewPrivileges -p Restart`, et `curl https://<hôte>/api/version`. Les données arrivent
à part, par l'export par périmètre (`docs/export-perimetre.md`), dans la base née.

⚠️ **L'import précède le premier démarrage de l'app** (#1161) : ce démarrage sème des
lignes (les guides plateforme, `nodes` et `blocks`) dans des tables que l'import écrit,
sous des identifiants qu'il préserve. L'import refuse alors la base dès son contrôle
préalable, en nommant chaque table et son nombre de lignes, sans option pour passer
outre : une base où l'app a déjà démarré ne se rattrape pas, on repart d'une base neuve.

Pour une cible qui reçoit des données, la séquence est donc, par rôle, sur sa base
**vide** et AVANT sa première montée :

1. **Naître** — `oto-mcp perimetre naitre`, depuis l'arbre du tag que la montée
   installera (la tête du registre posée est la sienne), avec l'environnement du rôle :
   le schéma et la version Alembic, sans rien démarrer. Une base qui a déjà des tables
   est refusée. Sur la machine, par le lanceur, comme `migrer current` (§ Les migrations), depuis
   un arbre où ce tag est installé, sous l'environnement du rôle (`DATABASE_URL`,
   et `OTO_TENANT_PRIMAIRE_SLUG` et `OTO_BRAND_NAME`, qui sèment le tenant primaire) :
   ```bash
   systemd-run --pipe --wait --quiet --collect -p WorkingDirectory=/opt/<i>/<r>-<couleur> \
     -p EnvironmentFile=/etc/<i>/<r>/app.env -p EnvironmentFile=/etc/<i>/<r>/lanceur.env \
     -p LoadCredential=scw:/etc/<i>/scw.key \
     /opt/<i>/<r>-<couleur>/.venv/bin/python /opt/<i>/<r>-<couleur>/deploy/lanceur_secrets.py perimetre naitre
   ```
2. **Importer** — `oto-mcp perimetre import perimetre.jsonl` (et les tranches du
   journal), par le même lanceur : `docs/export-perimetre.md`.
3. **Démarrer** — la première montée du tag. Sa garde des migrations trouve la base à la
   tête du registre du tag et continue ; le démarrage sème alors ses guides à côté des
   lignes importées.

Retour arrière d'un rôle : `-f action=retour -f etape=<rôle>`.

## Le déclencheur, dans le dépôt du propriétaire

Ce dépôt est public, les journaux de ses runs aussi. Le **déclencheur** des montées d'une
cible vit donc dans le dépôt **privé** de son propriétaire : un workflow d'une dizaine de
lignes qui appelle celui-ci (`workflow_call`). Appelé, `github.repository`,
`github.token` et `environment:` sont ceux du dépôt appelant : c'est **son** environnement
qui est vérifié (`protection.sh`) puis approuvé — ou dont la liste des
déclencheurs est consultée —, **ses** secrets d'environnement qui sont
lus, **ses** journaux qui gardent la trace. Le code exécuté, lui, vient toujours du tronc,
au tag demandé.

### Le workflow appelant

`.github/workflows/monter-instance.yml`, dans le dépôt privé :

```yaml
name: Monter l'instance

on:
  workflow_dispatch:
    inputs:
      tag:
        description: "Tag du tronc à monter (vX.Y.Z)"
        required: true
      etape:
        type: choice
        options: [preprod, prod, preprod-puis-prod]
        default: preprod
      action:
        type: choice
        options: [deployer, retour]
        default: deployer

jobs:
  monter:
    # Le plafond du jeton : l'appelé ne peut que l'abaisser. `actions: read` lit
    # l'environnement pour vérifier sa protection ; sans lui, refus.
    permissions:
      contents: read
      actions: read
    uses: otomata-tech/oto-backend/.github/workflows/deploy-cible.yml@<SHA de 40 caractères> # vX.Y.Z
    with:
      cible: <environnement>
      tag: ${{ inputs.tag }}
      etape: ${{ inputs.etape }}
      action: ${{ inputs.action }}
      acces: ssh          # tunnel, une fois le tunnel prouvé (§ Passer au tunnel)
```

- **La référence épinglée** est le **SHA d'un tag** du tronc, avec le tag en commentaire
  — jamais `main` (chaque poussée sur `main` changerait ce qui s'exécute contre la prod
  de la cible, sans relecture de son propriétaire), et plutôt le SHA que le tag (un tag
  se déplace, un SHA non). La changer est un commit relu dans le dépôt du propriétaire.
  Elle fixe le **workflow** ; l'entrée `tag` fixe le **code monté** (`deploy/` compris).
  Les deux sont indépendants, mais tous deux doivent porter l'accès choisi : un tag
  antérieur à l'entrée `acces` exige le jeton Access et refuse en accès `ssh`.
- **Aucun `secrets:`** dans l'appel : les secrets sont ceux de l'environnement de la
  cible, que GitHub ne transmet pas par l'appel mais donne au job qui nomme
  l'environnement. Le workflow appelé les déclare tous, facultatifs ; chacun est exigé
  en le nommant par le script qui s'en sert.
- **Pas de `concurrency`** du même nom dans l'appelant (`deploy-cible-<cible>`) : le
  workflow appelé en pose une, les deux s'attendraient l'un l'autre.
- `cible` s'écrit ici, en dur : le dépôt est privé, ses journaux aussi. Le masquage (D5)
  reste actif appelé : il cache aussi le nom du dépôt appelant.

### Ce qu'il faut côté propriétaire

| Où | Quoi |
|---|---|
| Réglages → Actions → General | autoriser les workflows réutilisables de `otomata-tech/oto-backend` (si la politique restreint les actions et workflows tiers) |
| Réglages → Environments → `<environnement>` | branches de déploiement : **la branche par défaut** (exigé) ; **Required reviewers** si l'offre les donne, sinon la liste des déclencheurs (ci-dessous) |
| Environnement, secrets (jamais de variables) | `CIBLE_DECLARATION`, `CIBLE_SSH_HOTE`, `CIBLE_SSH_UTILISATEUR`, `CIBLE_SSH_KNOWN_HOSTS`, `CIBLE_SSH_CLE` ; sans relecteur requis : `CIBLE_DECLENCHEURS` ; en accès `tunnel` : `CIBLE_CF_ACCESS_CLIENT_ID`, `CIBLE_CF_ACCESS_CLIENT_SECRET` ; facultatifs : `CIBLE_CONSOMMATEUR`, `CIBLE_CONSOMMATEUR_CLE` (§ 4) |
| Réglages → Branches | la branche par défaut protégée (fusion par relecture) : qui peut y écrire peut changer le déclencheur |
| Job appelant | `permissions: {contents: read, actions: read}` — le `GITHUB_TOKEN` suffit, aucun jeton dédié |

### Sans relecteurs requis : la liste des déclencheurs

Sur un dépôt **privé**, GitHub donne les environnements et leurs secrets aux offres Pro,
Team et Enterprise, mais les **relecteurs requis qu'à Enterprise**. En offre Team, la
décision n'est donc pas une approbation : **lancer vaut décider**. L'environnement
déclare qui peut lancer — le secret `CIBLE_DECLENCHEURS`, des logins GitHub séparés par
des virgules ou des espaces, à la **casse exacte** — et le workflow vérifie, en premier
contrôle du job qui nomme l'environnement, que l'acteur du run y figure.

- **L'acteur est `github.triggering_actor`**, pas `github.actor` : relancer un run garde
  l'`actor` d'origine, mais c'est celui qui relance qui décide. Avec `actor`, quiconque
  peut relancer un run pourrait rejouer la montée décidée par un autre.
- **Refus nommés, jamais de repli** : ni relecteur ni liste (une liste vide vaut une
  absence), acteur absent de la liste (il est nommé ; la liste, jamais), liste illisible.
- **Les branches de déploiement doivent être limitées** (la branche par défaut) : sans
  cela, n'importe quel job d'une autre branche nommant l'environnement en lirait les
  secrets, liste ou pas. `protection.sh` refuse un environnement sans relecteur dont les
  déploiements ne sont pas limités à des branches.
- **Ce que la liste ne protège pas** : qui peut écrire sur la branche par défaut du dépôt
  peut changer le workflow appelant, donc ce qui s'exécute avec les secrets de
  l'environnement. La liste désigne qui décide d'une montée ; la protection de la branche
  par défaut désigne qui peut changer la manière de monter. Les deux se tiennent.
- La liste est masquée dans les journaux (D5), login par login.
- Relecteur requis **et** liste : le relecteur suffit, la liste n'est pas consultée.

### En attendant le tunnel : l'accès `ssh`

`acces: ssh` va droit au `:22` de la machine : `CIBLE_SSH_HOTE` est la machine,
`CIBLE_SSH_KNOWN_HOSTS` sa clé d'hôte sous ce nom (relevée sur la machine,
`ssh-keyscan` comparé à `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), aucun jeton.
La clé de déploiement n'ouvre que la porte (`restrict,command=…`) ; le `:22` n'accepte
que des clés. Les runners hébergés n'ayant pas d'adresse fixe, une restriction par
source (`from=`, pare-feu) n'est pas tenable : c'est un état d'attente, pas une fin.

### Passer au tunnel, puis fermer le :22

1. Le propriétaire active Zero Trust : tunnel de la machine, application SSH, **jeton de
   service** dédié dans sa politique.
2. Dans l'environnement : `CIBLE_CF_ACCESS_CLIENT_ID` et `_SECRET` ; `CIBLE_SSH_HOTE` =
   l'hôte de l'application Access ; `CIBLE_SSH_KNOWN_HOSTS` porte la même clé d'hôte
   sous les deux noms (`hote-access,machine ssh-ed25519 …`). Tant que l'appelant dit
   `ssh`, un avertissement signale le jeton inutilisé.
3. Commit dans le dépôt du propriétaire : `acces: tunnel`.
4. Une montée **réelle** par le tunnel (préprod), constatée (`/api/version`).
5. Alors seulement, fermer le `:22` au public (pare-feu du projet), et retirer l'ancien
   nom de `CIBLE_SSH_KNOWN_HOSTS`. Un appel resté en `ssh` échouerait désormais,
   bruyamment : il n'y a pas de repli.

## Juger un tag sans le monter (#1195)

Le contrôle inverse du contrat (le contrat que **ce tag** servirait chez la cible, confronté
à celui qu'épingle son consommateur, `CIBLE_CONSOMMATEUR`) se lance aussi **seul**, pour un
tag, sans rien monter ni contacter la machine : `.github/workflows/contrat-cible.yml`. Un
déclencheur du propriétaire juge ainsi un tag avant de décider de le monter, ou un front
garde sa branche contre le prochain tag du tronc.

- **Même verdict que la montée** : les deux passent par `scripts/contrat-cible.sh`, la
  source unique du contrôle (`consommateur` puis, le jeu du verrou du tag installé,
  `juger <declaration.json>`). Compatible → vert ; une rupture d'appel ou de lecture, un
  contrat illisible ou non lu → rouge, nommé. Sans consommateur déclaré : vert, et dit.
- **Mêmes secrets**, ceux de l'environnement de la cible : `CIBLE_DECLARATION` (exigé : elle
  dit si la facturation est servie, donc quel contrat), `CIBLE_CONSOMMATEUR`,
  `CIBLE_CONSOMMATEUR_CLE`. Le job nomme l'environnement : s'il exige un relecteur, ce
  contrôle attend l'approbation lui aussi.
- **Le runner se choisit** (`runner`, en JSON) : hébergé par GitHub par défaut, ou le runner
  auto-hébergé du propriétaire (`'["self-hosted", "<label>"]'`), qui doit porter bash, git
  et jq ; Python et uv s'installent par le verrou du tag.

Dans le dépôt privé, à côté du workflow de montée :

```yaml
jobs:
  contrat:
    permissions:
      contents: read
    uses: otomata-tech/oto-backend/.github/workflows/contrat-cible.yml@<SHA de 40 caractères> # vX.Y.Z
    with:
      cible: <environnement>
      tag: ${{ inputs.tag }}
      runner: '["self-hosted", "<label>"]'   # facultatif ; défaut : "ubuntu-latest"
```

Comme pour la montée : la référence épinglée est le SHA d'un tag du tronc, **aucun
`secrets:`** dans l'appel (le job qui nomme l'environnement les reçoit), et le code exécuté
vient du tronc au tag jugé. Le tag est validé avant de servir de référence. Hors de GitHub
Actions, le script se lance pareil depuis l'arbre du tag, ses entrées en variables
d'environnement (en-tête de `scripts/contrat-cible.sh`).

## Hors de ce dépôt : les workers runner

Les workers `oto-runner` vivent dans leur propre dépôt et y suivent `main`. Pour une
cible, ils doivent tourner **au même tag** que son back-end : il faudra, dans ce dépôt-là,
une montée pilotée par tag (même déclenchement manuel, même approbation), et une
déclaration de l'URL du back-end de la cible. Rien de ce chantier ne le fait.

## Passer notre box au lanceur (#967, lot 5)

**Fait le 30/09/2026**, préproduction puis production. Reste le lot 5b.

Notre production (`oto-mcp`) et notre préproduction (canari) quittent `start-encrypted.sh` et
`start-encrypted-canari.sh` pour `deploy/lanceur_secrets.py` : les secrets se lisent **par
nom** dans notre Secret Manager, sous `/prod` et `/preprod`, et plus aucun identifiant de
secret n'est écrit dans ce dépôt. Décisions : **Mollie strict** (un facultatif déclaré mais
introuvable bloque le démarrage : le bleu/vert garde l'ancienne couleur) ; `OTO_PENNYLANE_API_KEY`
disparaît (plus de lecteur) ; `STRIPE_SECRET_KEY` ne passe pas (retirée du `.env`).

**Découpage.** Lot 5a (code, sans effet sur la box) : `--script`, `--noms`, le scénario
`BG_LANCEUR=versionne` du banc, les docs. Lot 5c (à déployer AVEC la bascule prod, jamais
avant : `install_timers` de `deploy/oto-backend.sh` pose la maintenance à chaque déploiement
prod) : `oto-mcp-maintenance.service` et `oto-journal-archive.service` par le lanceur. Lot 5b
(après) : retrait de `start-encrypted*.sh`, suppression des anciens secrets par identifiant.

**Règle** : canari d'abord, prod ensuite, chaque rôle en une seule séance (les étapes B se
suivent sans pause). **Aucune valeur de secret ne s'affiche, ni ne passe en argument** : les
contrôles se font par noms ou par code retour. Sur la box, en root. `<r>` = `prod` ou
`canari` ; `<c>` = `/prod` ou `/preprod` ; `<env>` = `/opt/oto-mcp/.env` (prod) ou
`/opt/oto-mcp-canari/.env` (canari) ; `<u>` = `oto-mcp` ou `oto-mcp-canari`.

### A. Préparer — aucun effet sur ce qui sert

1. **Recenser les lecteurs du `.env`** (noms de fichiers seulement) :
   `grep -rlE '/opt/oto-mcp(-canari)?/\.env' /etc/systemd/system /etc/cron* /var/spool/cron /usr/local/sbin /opt/deploy 2>/dev/null`.
   Chacun doit passer par le lanceur ou renoncer au secret (unités de maintenance et d'archive :
   lot 5c ; crontabs d'ingestion : `--script`).
2. **Poser la clé d'API** : `( umask 077; . /etc/oto-mcp/scw.env; printf '%s' "$SCW_SECRET_KEY" > /etc/oto-mcp/scw.key )`
   puis `stat -c '%a %U' /etc/oto-mcp/scw.key` → `600 root`. Une seule clé pour les deux rôles
   (lecture des secrets du projet).
3. **Poser `/etc/oto-mcp/lanceur-<r>.env`** (0644, aucun secret) :
   ```
   OTO_SECRETS_REGION=fr-par
   OTO_SECRETS_PROJET=<identifiant du projet Scaleway de la box>
   OTO_SECRETS_CHEMIN=<c>
   OTO_SECRETS_OPTIONNELS=<facultatifs portés par CE .env, séparés par des espaces>
   ```
4. **Lister ce que le `.env` porte de secret** (noms seulement, avec le code du tag à monter —
   extraire `oto_mcp/` et `deploy/` du tag : `git -C <arbre inactif> fetch --tags -q &&
   rm -rf /root/lanceur-essai && mkdir /root/lanceur-essai &&
   git -C <arbre inactif> archive <tag> oto_mcp deploy | tar -x -C /root/lanceur-essai`) :
   ```
   cd /root/lanceur-essai && python3 - <<'PY'
   import re, sys
   sys.path.insert(0, '.')
   from oto_mcp import env_secrets as e
   requis = set(e.secrets_requis()); vus = []; gardees = []
   for ligne in open('<env>', encoding='utf-8'):
       m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', ligne)
       if m and (e.est_secret(m.group(1)) or m.group(1) == 'STRIPE_SECRET_KEY'):
           vus.append(m.group(1))
       else:
           gardees.append(ligne)
   open('<env>.purge', 'w', encoding='utf-8').writelines(gardees)   # le .env sans ses secrets
   print('requis dans le .env    :', *sorted(set(vus) & requis))
   print('OTO_SECRETS_OPTIONNELS :', *sorted(set(vus) - requis - {'STRIPE_SECRET_KEY'}))
   PY
   ```
   Reporter la seconde ligne dans `OTO_SECRETS_OPTIONNELS` (étape 3), **plus** le secret de
   Mollie (prod : clé live ; canari : clé de test) s'il n'était pas dans le `.env` — il vient
   aujourd'hui de `start-encrypted*.sh`. Il n'y a pas de Pennylane.
5. **Créer les secrets, un par variable, nommé comme elle**, sous `<c>` — depuis un poste qui
   peut ÉCRIRE dans le Secret Manager (la clé de la box est en lecture) : `scw secret secret
   create name=<NOM> path=<c> project-id=<projet> region=fr-par`, puis `scw secret version create`
   avec la valeur lue d'un fichier 0600 supprimé aussitôt (`scw secret version create --help`
   pour la syntaxe ; jamais la valeur en argument visible). Requis (les huit de
   `env_secrets.secrets_requis()`, à vérifier contre le tag) : `DATABASE_URL`,
   `OTO_MCP_S3_ACCESS_KEY`, `OTO_MCP_S3_SECRET_KEY`, `OTO_MCP_MASTER_KEY`,
   `OTO_MCP_OAUTH_STATE_SECRET`, `GOOGLE_WORKSPACE_CLIENT_SECRET`, `FOD_API_TOKEN`,
   `OTO_FERME_TOKEN`, puis les facultatifs de l'étape 4. Origine des valeurs : le `.env` du rôle,
   sauf `OTO_MCP_MASTER_KEY` et le secret Mollie, lus une dernière fois dans leurs anciens
   secrets. ⚠️ **La base est partagée** : `DATABASE_URL` et `OTO_MCP_MASTER_KEY` ont la
   MÊME valeur sous `/prod` et `/preprod` — les autres peuvent différer (Mollie : live/test).
6. **Vérifier l'égalité par code retour** (rien ne s'affiche) :
   ```
   lire() { curl -fsS -G -H "X-Auth-Token: $(cat /etc/oto-mcp/scw.key)" \
     "https://api.scaleway.com/secret-manager/v1beta1/regions/fr-par/secrets-by-path/versions/latest_enabled/access" \
     --data-urlencode "project_id=<projet>" --data-urlencode "secret_name=$1" --data-urlencode "secret_path=$2" |
     python3 -c 'import sys,json,base64; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)["data"]))' | tr -d '\r\n'; }
   for n in DATABASE_URL OTO_MCP_MASTER_KEY; do
     cmp -s <(lire $n /prod) <(lire $n /preprod) && echo "$n : identique" || echo "$n : DIFFÉRENT"; done
   ```
7. **Noms d'avant** (le service qui sert, avant tout changement) :
   ```
   PID=$(systemctl show -p MainPID --value <u>@$(cat /etc/oto-mcp/active-<r>))
   tr '\0' '\n' < /proc/$PID/environ | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' | sort > /root/noms-<r>.avant
   ```
8. **Essai à blanc du lanceur** sur la copie purgée du `.env` (`<env>.purge`, écrite par l'étape 4) :
   ```
   systemd-run --pipe --wait --quiet --collect -p EnvironmentFile=<env>.purge \
     -p EnvironmentFile=/etc/oto-mcp/lanceur-<r>.env -p EnvironmentFile=/etc/oto-mcp/port-<r>-blue.env \
     -p LoadCredential=scw:/etc/oto-mcp/scw.key \
     python3 /root/lanceur-essai/deploy/lanceur_secrets.py --noms | sort > /root/noms-<r>.essai
   comm -3 /root/noms-<r>.avant /root/noms-<r>.essai
   ```
   Un refus nomme ce qui manque (secret introuvable, `.env` encore secret, clé illisible).
   **Écart attendu** — colonne de gauche (avant seul) : `SCW_*`, `OTO_PENNYLANE_API_KEY` (prod),
   `STRIPE_SECRET_KEY` si le service la portait, et les variables de session de systemd
   (`INVOCATION_ID`, `JOURNAL_STREAM`, `SYSTEMD_EXEC_PID`…) ; colonne de droite (essai seul) :
   `CREDENTIALS_DIRECTORY`, `OTO_SECRETS_REGION`, `OTO_SECRETS_PROJET`, `OTO_SECRETS_CHEMIN`,
   `OTO_SECRETS_OPTIONNELS`. Tout autre écart est un secret oublié : le déclarer, ne pas continuer.

### B. Basculer — d'une traite, un rôle à la fois

9. **Sauvegardes** (suffixe `.avant-lot5`, hors git) : `<env>`, l'unité
   `/etc/systemd/system/<u>@.service`, et `/opt/deploy/{oto-mcp-bluegreen.sh,oto-backend.sh,oto-backend-canari.sh,oto-mcp-drain.sh}`.
10. **Purger le `.env`** : `chmod --reference=<env> <env>.purge && chown --reference=<env> <env>.purge
    && mv <env>.purge <env>` (le lien de la couleur verte reste valide).
11. **Unité** `/etc/systemd/system/<u>@.service` : ajouter `EnvironmentFile=/etc/oto-mcp/lanceur-<r>.env`
    (entre `<env>` et le fichier de port) et `LoadCredential=scw:/etc/oto-mcp/scw.key`, puis remplacer
    l'`ExecStart` par
    `ExecStart=/opt/<arbre>-%i/.venv/bin/python /opt/<arbre>-%i/deploy/lanceur_secrets.py`
    (`<arbre>` = `oto-mcp` ou `oto-mcp-canari`). Reporter le même changement dans le miroir de
    `/data/infra/scripts/oto-backend-bluegreen/`.
12. **Copies de `/opt/deploy/`** depuis le tag (`/root/lanceur-essai/deploy/`) : la bibliothèque
    `oto-mcp-bluegreen.sh`, le wrapper du rôle, `oto-mcp-drain.sh`. Le wrapper du dépôt déclare
    `BG_LANCEUR=versionne` : il se pose tel quel. Contrôle : `grep -v '^#' <fichier> | sha256sum`
    du fichier posé et du dépôt, identiques. Puis `systemctl daemon-reload`.
13. **Déployer par le pipeline habituel** (canari : push sur main ; prod : tag) : la nouvelle couleur
    démarre par le lanceur, le bleu/vert garde l'ancienne si elle ne devient pas saine. Le tag doit
    porter `deploy/lanceur_secrets.py` (la bibliothèque refuse sinon). ⚠️ Entre les étapes 10 et 13,
    l'ancienne couleur sert encore mais ne saurait plus redémarrer seule : ne pas s'attarder.
14. **Contrôles après**, par noms : refaire le relevé de l'étape 7 sur la couleur qui sert →
    `/root/noms-<r>.apres` ; `comm -3 /root/noms-<r>.avant /root/noms-<r>.apres` ne montre que l'écart
    attendu de l'étape 8 ; `journalctl -u <u>@<couleur> -n 5 --no-pager` contient « secret(s) tiré(s) de
    <c> » (les noms, pas les valeurs) ; `GET /api/version` sert le tag. Refaire l'étape 6.
15. **Prod seulement — les travaux planifiés (lot 5c)**, livrés par le même tag : le déploiement pose
    `oto-mcp-maintenance.service` (arbre de la couleur qui sert écrit à la pose) ; contrôler
    `systemctl cat oto-mcp-maintenance.service | grep ExecStart`, puis l'essai sans effet :
    `systemd-run --pipe --wait --quiet --collect -p WorkingDirectory=/opt/oto-mcp-<couleur> -p
    EnvironmentFile=/opt/oto-mcp/.env -p EnvironmentFile=/etc/oto-mcp/lanceur-prod.env -p
    LoadCredential=scw:/etc/oto-mcp/scw.key /opt/oto-mcp-<couleur>/.venv/bin/python
    /opt/oto-mcp-<couleur>/deploy/lanceur_secrets.py maintenance check-boot`. L'archive du journal se
    pose à la main (le déploiement ne la pose pas) : `test -f /opt/oto-mcp/deploy/lanceur_secrets.py`
    (l'arbre bleu doit porter le lanceur), `install -m 0644 <arbre>/deploy/oto-journal-archive.service
    /etc/systemd/system/`, `systemctl daemon-reload`, essai `… lanceur_secrets.py --script
    deploy/archive_tool_calls.py --dry-run` avec les mêmes `-p` que ci-dessus.

### Retour arrière, par rôle

Remettre dans l'ordre : `<env>.avant-lot5` → `<env>`, l'unité et les copies de `/opt/deploy/`
(sauvegardes de l'étape 9 : elles déclarent `BG_LANCEUR=propage` ; les wrappers du dépôt,
en `versionne`, ne servent pas au retour arrière), `systemctl daemon-reload`, puis `/opt/deploy/oto-backend{,-canari}.sh
--rollback` : l'ancienne couleur porte encore son `start-encrypted.sh`. Ne pas rebasculer avant
d'avoir remis le `.env` : l'ancien lanceur (`start-encrypted.sh`) attend les secrets dans le `.env`.
Les secrets créés par nom restent : ils ne gênent personne. Les sauvegardes `.env.avant-lot5`
portent des secrets en clair : les détruire à la fin du lot 5b, pas avant.
