---
title: Runner hébergé & automatisations
type: reference
description: >-
  L'état du runner d'agents vit ici (fil des runs, file de jobs, déclencheurs), la boucle vi
  t dans `otomata-tech/oto-runner`. Plus le connecteur `routine` qui déclenche une routine C
  laude Code hébergée chez Anthropic.
---

# Runner hébergé & automatisations

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## ⚠️ DIRECTION ARRÊTÉE LE 02/09/2026 — ce qui suit décrit l'ÉTAT, pas la CIBLE

**Le chantier fleet est GELÉ le temps que la nouvelle direction soit portée.** Ce
qui est décrit plus bas fonctionne et reste servi ; ce n'est plus vers quoi on va.
Une carte qui décrirait la cible périmée serait pire que rien : elle est lue avec
confiance.

**Ce qu'on abandonne** : « déclarer une campagne, puis la lancer » comme point
d'entrée produit, et **un worker = un jeton d'org**.

**Ce vers quoi on va, arrêté avec Alexis :**

```
LE POINT D'ENTRÉE   depuis le dashboard, sur une PROCÉDURE ou un NŒUD, un bouton
                    bascule l'objet en agent programmé et récurrent. L'agent
                    autonome est une PROPRIÉTÉ de ce qui existe déjà, pas un
                    objet séparé qu'on déclare.
L'INSTRUCTION       minime — « lis l'objet numéro X ». La boucle agentique fait
                    le reste PAR LE MCP, comme un agent qui travaille avec le
                    connecteur oto branché. ⚠️ Elle cesse d'être un second
                    domicile du métier : celui d'une flotte contredisait la
                    consigne servie depuis six jours sans que personne ne le voie.
LE WORKER           le NÔTRE, mutualisé — puis notre flotte de workers. Il fait
                    partie du back, MÊME NIVEAU DE SÉCURITÉ, donc le même droit
                    de lire les clés que les orgs ont posées. Personne ne pose un
                    worker pour un client ; qu'une org y ait droit est une
                    question de TARIFICATION, pas de déploiement.
```

**Les quatre points tranchés le 02/09, dans l'ordre où ils ont été posés :**

**① L'IDENTITÉ que l'agent porte** = celle du créateur du déclencheur par défaut,
**paramétrable** vers un autre membre. ⚠️ Et un agent dont l'identité n'est plus
valide **s'arrête EN LE DISANT** — ni mort silencieuse, ni poursuite sous un
compte désactivé. C'est le cas qu'on découvre six mois plus tard.

⚠️ **C'est aussi le préalable TECHNIQUE du worker mutualisé** : si le travail
porte son identité, le worker n'a plus besoin d'un jeton par org. Les deux sujets
n'en font qu'un.

**② UN SEUL OBJET**, avec le parallélisme en paramètre. Un déclencheur lance un
agent, une campagne en lance N sur les lignes d'un tableau : c'est la même chose
avec un « combien en parallèle ». Deux mécaniques qui font 90 % la même chose
divergent, et l'une prend du retard sur l'autre.

**③ UNE BORNE PAR DÉFAUT, NON NULLE, IMPOSÉE** — relevable si l'offre le permet.
*Un agent récurrent sans plafond, c'est une facture qu'on découvre au relevé :
65 571 jetons mesurés sur UNE ligne le 01/09.* La borne vit dans l'agent depuis
`oto-runner b37daf6`, donc elle s'applique quel que soit le chemin d'enfilage.

**④ LE MODÈLE : nous décidons de la voie, l'org fournit la clé.** Le modèle
s'expose comme une préférence, jamais comme la mécanique — sinon on vend un
curseur dont l'utilisateur ne peut pas prévoir l'effet. *Mesuré : la voie
Conversations coûte ~26 k jetons par fiche, plate au rang ; en boucle locale la
5ᵉ fiche d'un fil coûte 6,6× la première.*

⚠️ **Et ce que le champ `provider`/`model` de `runner_fleets` promet est FAUX
aujourd'hui** : `fleet.py` lit `OTO_RUNNER_MODEL` et ignore la déclaration. Le
schéma affirme pourtant que ces champs portent l'attribution d'une ligne. Un
champ inerte est un défaut ; un champ qui PROMET ce qui n'arrive pas en est un
autre, plus coûteux.

## Runner hébergé — l'état ici, la boucle dehors (chantier R1-R5, ADR 0064 au blueprint)

Le backend porte l'ÉTAT du runner d'agents hébergé ; la BOUCLE vit dans le repo
public **`otomata-tech/oto-runner`** (worker = client pur MCP+REST, ordonnanceur
de flotte `fleet.py` — AUCUN kind serveur, la file reste uniforme ; déployé
`/opt/oto-runner` sur **`oto-platform`** (⚠️ cette carte a dit « otomata-0 »
jusqu'au 01/09/2026 : c'est faux et constaté sur la machine), gaté par le cran
`OTO_RUNNER_ARMED`). Quatre tables + leurs capacités :
- **fil des runs** `run_messages` — capacité `runs.thread` (MCP `oto_run_thread`
  + REST `/api/me/runs/thread`) : état d'exécution EFFAÇABLE (purge 30 j), append
  = propriétaire seul, read = org_admin en projection neutre (`include_raw` au
  propriétaire) ; la reprise inter-agents lit le JOURNAL, jamais le fil.
- **file de jobs** `runner_jobs` — capacité `runner.jobs` (REST-only
  `/api/me/runner/jobs`) : claim SKIP LOCKED + bail re-claimable, backoff,
  `result` JSONB déclaré à la conclusion (usage_tokens, `tool_counts` — le
  « tour perdu », un agent qui analyse sans écrire, se lit au grain job),
  op=list org-scopé (surveillance dashboard `/automations`), **paginé et
  DISANT sa borne** — voir ci-dessous.
- **flottes** `runner_fleets` (R4, 01/09/2026) — la CONFIGURATION DÉCLARÉE d'un
  passage : procédure, cible (`namespace` + `row_filter`), contexte d'exécution
  (`provider`/`model`, uniforme sur le passage — c'est LUI qui porte l'attribution
  d'une ligne écrite, l'agent ne sait pas ce qui le fait tourner), bornes
  d'exploitation (`max_rows`, `max_tokens`, `max_consecutive_failures`,
  `max_tokens_per_row` — le budget se compte en JETONS, jamais en monnaie : les
  tarifs changent et une valeur monétaire figée en base devient fausse sans que
  rien ne le dise), état + `stop_reason` ÉCRIT. `runner_jobs.fleet_id`
  rattache un travail à son passage — **posé à l'`enqueue`** (`runner.jobs
  op=enqueue fleet_id=`), rendu par `list`/`get`, et c'est lui qui rend
  `op=state` capable d'agréger. ⚠️ **L'APPARTENANCE de la flotte se vérifie, pas
  seulement son existence** : la FK dit qu'une flotte existe, pas à QUI elle est
  — sans garde, le coût d'un travail entrerait dans l'état du passage d'une autre
  org (`fleet_not_found`, même 404 sans oracle qu'un run étranger). ⚠️ Livré
  d'abord SANS écrivain (R4) : la colonne, l'index, la FK et l'agrégat existaient
  pendant que `state` répondait « aucun travail » pour toute flotte — *un harnais
  qui prouve un chemin de lecture ne prouve pas qu'il existe un chemin d'écriture
  pour ce qu'il lit* (#791, 01/09/2026). ⚠️ Une flotte vivait dans un YAML sur la
  machine : rien n'en était visible du dashboard ni atteignable par un agent.
  **Déclarer n'est pas restreindre — c'est donner un domicile aux gardes** : un
  lancement qui prend son tableau en argument n'a nulle part où accrocher une
  cible ni une borne. ⚠️ `heartbeat_at` distingue le VIVANT du RÉSIDU (une flotte
  `running` qui ne bat plus n'est pas une concurrence à attendre), et la table est
  créée AVANT `runner_jobs`, qui la référence.
  ⚠️ **SEPT états, parce que deux d'entre eux séparent une INTENTION d'un FAIT**
  (R4b, 01/09/2026) : `armed` (on a DEMANDÉ que ça tourne, `op=launch`) ≠
  `running` (un ordonnanceur l'a PRISE et donne signe) ; `stopping` (arrêt
  demandé, `op=stop`) ≠ `stopped` (l'ordonnanceur a ACCUSÉ réception). *Une
  intention déclarée et un fait constaté ne partagent jamais une colonne* — sans
  `armed`, une flotte que personne n'a réclamée se lirait « en cours » ; sans
  `stopping`, un arrêt demandé se lirait « arrêté », et **croire qu'on a coupé
  une dépense qui continue est pire que croire qu'on a lancé un passage qui ne
  tourne pas**. L'écart entre les deux EST le diagnostic : un `stopping` qui ne
  devient jamais `stopped` désigne un ordonnanceur mort.
  ⚠️ **Deux planchers, parce que la garde suit ce que le geste ENGAGE** :
  `launch` est réservé aux **admins** de l'org (il engage une dépense et des
  écritures chez un tiers, irréversibles) ; `stop` est ouvert à **tout membre**
  (un passage qui part en vrille doit pouvoir être stoppé par la première
  personne qui le voit). Deux gardes distinctes : *un déroulé ne LANCE pas* (un
  agent qui se relance dépense en boucle) et *un déroulé n'arrête pas CELLE QUI
  L'EXÉCUTE* — nommée, plutôt que de fermer le verbe à tout le monde.
- **déclencheurs** `runner_triggers` — capacité + MCP `oto_trigger`, tick
  backend avec CAS sur `next_due` (prod/preprod partagent la base : un seul
  gagnant par échéance). ⚠️ **Poser (et rallumer) exige un runner ARMÉ pour
  l'org** — voir ci-dessous.
- **workers vus** `runner_workers` — la présence d'un runner pour une org,
  inscrite à CHAQUE sondage de la file (`op=claim`, y compris à vide).

### Ne pas promettre une exécution que personne n'assure (02/09/2026)

Le tick ENFILE, le worker EXÉCUTE. Sans worker armé pour l'org, le job reste
`pending` **pour toujours, sans une erreur** — pendant que le déclencheur rend un
`next_due` que l'agent rapporte comme une promesse tenue. C'est le pire des deux
malentendus : **ça ressemble à un succès.**

**Ce qui l'a daté.** Relevé le 02/09 dans l'org 196 : cinq déclencheurs actifs
enfilent chaque matin (dernier enfilement le jour même à 07:00), et un sixième
porte son autopsie **dans son propre libellé** — « DISABLED 26 Aug, oto_trigger
jobs do not execute ». Quelqu'un a diagnostiqué la panne et n'a eu que le NOM de
l'objet pour l'écrire : le produit ne disait rien, nulle part.

**La garde suit le VERBE, pas l'objet** — le motif que `runner_fleets` a établi
pour `launch`/`stop` :

```
create              REFUSÉ sans runner armé   c'est le geste qui MENT
update enabled=true REFUSÉ sans runner armé   rallumer, c'est promettre à nouveau
list / get          ouverts, + `runner`       et c'est là qu'on cherche la réponse
update (autre) /
delete              TOUJOURS ouverts          ranger un déclencheur mort
```

Fermer aussi la lecture ou la suppression enfermerait l'utilisateur avec l'objet
qui lui ment — or c'est exactement la personne qui a besoin d'agir.

**Le signal, et pourquoi une table.** Un claim sur file VIDE n'écrit rien :
`runner_jobs.claimed_by` ne distingue donc pas « aucun worker » de « un worker
qui n'a rien eu à faire ». Et cette lecture se BOUCLE au démarrage — aucun job ne
peut exister avant un déclencheur, aucun déclencheur ne pourrait alors se poser.
Le **sondage** prouve la présence même à vide : c'est le seul signal qui parle
avant le premier job. D'où `runner_workers`, écrite en tête de `claim_next_job`.

**La fenêtre est asymétrique, et c'est elle qui fixe la valeur**
(`ARME_FENETRE_S`, 15 min). Un refus à tort se répare tout seul — le message dit
quoi faire, et reposer le déclencheur trente secondes plus tard marche. Une
acceptation à tort fabrique une promesse qui ment TOUS LES JOURS jusqu'à ce que
quelqu'un s'aperçoive que le rapport n'arrive pas. On refuse du bon côté, avec
une fenêtre assez large pour qu'un redéploiement ne la morde pas.

⚠️ **`list`/`get` portent `runner` {armed, workers, last_seen} — DÉCLARÉ, pas
déduit.** `last_seen: null` (aucun worker n'est jamais venu) et une date ancienne
(il s'est tu) n'appellent pas le même geste : monter un runner, ou aller voir
pourquoi celui qui existe s'est tu. Un seul booléen les confondrait. C'est aussi
la seule chose qui distingue, pour les déclencheurs **déjà posés**, un vivant
d'un mort — le refus, lui, ne protège que les nouveaux.
⚠️ **« Arrêter » vise DEUX services distincts** (constaté le 01/09/2026) :
l'ordonnanceur (`oto-fleet-<nom>`) cesse d'ENFILER, les agents (`oto-runner@1..N`,
unités séparées) finissent ce qui est pris **et restent ARMÉS sur la file**.
Arrêter le premier laisse les seconds prêts à repartir, et des écritures tombent
jusqu'à plusieurs minutes après un « c'est arrêté » qui n'a regardé que
l'ordonnanceur. **« Rien ne tourne » ne se dit qu'après avoir constaté les deux.**
⚠️ Les jetons de contexte (`_project`…) sont advertisés PAR TOOL : un client
les pose d'après le schéma du tool, jamais à l'aveugle (un jeton non déclaré
fait refuser l'appel entier à la validation). Conception + état des preuves :
blueprint `chantier-runner.md` ; pilote = une campagne cliente (fusion R5, 14/08).

### Un agent programmé se crée DEPUIS l'objet (#860, moitié serveur, 03/09/2026)

**L'agent autonome est une PROPRIÉTÉ de ce qui existe déjà**, pas un objet séparé
qu'on déclare. Une procédure gagne un état « celle-ci tourne toute seule ».

```
les OUTILS   se déduisent de la procédure — ceux qu'elle CITE (`<tool:nom>`)
l'INSTRUCTION est dérivée : « lis la procédure X et applique-la »
un SEUL agent par objet, et le refus NOMME celui qui existe
la LECTURE   se fait depuis l'objet (`list` filtré par `procedure`)
```

⚠️ **Sans la déduction des outils, le bouton demanderait une liste d'outils — donc
ne serait pas un bouton.** La procédure cite déjà ses outils par marqueur, et
c'est ce que lit le compteur « référencé par N guides » : on ne devine rien, on
lit ce que l'auteur a écrit. Une liste fournie explicitement gagne quand même —
*la déduction est un défaut, pas une contrainte*.

⚠️ **L'instruction est dérivée, JAMAIS saisie.** Une instruction rédigée à la main
est un **second domicile du métier** : la même règle vit dans la procédure et
dans l'instruction, et l'une des deux finit par mentir. Une instruction qui
POINTE l'objet ne peut pas diverger de lui.

⚠️ **Un seul agent par objet** : deux agents sur le même objet, c'est deux
réponses à « est-ce que ça tourne ? », et l'écran devrait en choisir une. Le refus
donne l'identifiant et le cadencement de celui qui existe — sinon l'utilisateur ne
peut que réessayer.

⚠️ **Une procédure qui ne cite aucun outil est REFUSÉE, avec les deux issues** :
citer les outils, ou passer `tools`. Un agent sans outil n'exécute rien ; le
laisser se créer produirait un agent qui tourne à vide tous les matins.

**Ce que la moitié tableau de bord doit encore faire** : l'interrupteur, le
cadencement en langage d'utilisateur, l'état lisible — et **afficher le compteur
d'occurrences perdues**, servi depuis le 02/09 et affiché nulle part.

### L'instruction se compose ICI, jamais dans le worker (#873, 04/09/2026)

Le worker est **un client MCP** : il exécute une instruction et **ne sait pas ce
qu'elle contient**. Trois textes de repli disaient le contraire — `DEFAULT_INPUT`
dans l'ordonnanceur de flotte, deux « Exécute la procédure. » dans le worker.
Tous trois inventaient le travail à la place de qui l'avait déclaré, **depuis le
seul étage qui ne connaît pas le métier**. Une instruction inventée là ne se
relit ni ne se corrige depuis le produit : elle se découvre dans le résultat.

```
capabilities/_instruction.py   domicile UNIQUE de la composition
  derivee(slug)                « lis la procédure X et applique-la »
  de_file(slug, ns, filtre)    + la mécanique de réservation, qui est À NOUS
```

⚠️ **La mécanique de file n'est pas du métier.** « Réserve une ligne, une seule,
rends-la » appartient à la flotte, pas à la procédure : c'est la plateforme qui
distribue le travail entre plusieurs agents. L'écrire ici évite qu'un client
recopie à la main, dans chaque campagne, un protocole que la plateforme est seule
à savoir juste. Sans cible déclarée, **aucune file n'est inventée**.

⚠️ **Les deux surfaces qui déclarent un agent en dépendent** (déclencheur,
flotte). Le déclencheur y a perdu la copie locale posée par #866 : si chacune
rédige sa variante, la même règle vit à plusieurs endroits et l'une d'elles finit
par mentir. Un banc tient la classe — aucune autre capacité ne rédige la sienne.

⚠️ **`launch` répare avant d'armer.** Une campagne sans instruction armée telle
quelle resterait `armed` sans avancer : le worker refuse de démarrer, et le
symptôme lu depuis le produit serait « l'ordonnanceur est mort » — un diagnostic
faux posé sur une cause invisible. Le refus du worker reste, en dernier ressort ;
il n'est plus le seul filet.

### Le verrou du tick porte sur l'ÉLIGIBILITÉ, pas sur l'échéance relue (#839, 03/09/2026)

Le compare-and-swap qui empêche deux environnements de jouer la même échéance
comparait `next_due` à **la valeur que le tick venait de lire**. Or toute date lue
passe par `_normalize_value`, qui retire **les microsecondes ET le fuseau**.

```
microsecondes   une échéance à 19:37:27.482 est relue « 19:37:27 »
fuseau retiré   la chaîne naïve est réinterprétée dans le fuseau de la SESSION
```

⚠️ **Dans les deux cas la comparaison ne matche jamais, et rien n'échoue** :
`consume_due` rend `False`, que le tick lit comme « un pair a déjà consommé cette
échéance » — le cas NORMAL quand preprod et prod partagent la base. Il passe sans
enfiler, sans erreur, sans avertissement. **Le déclencheur reste éternellement
dû** : sélectionné à chaque tour, jamais consommé — *avec l'air parfaitement
sain*, `enabled`, échéance passée, runner armé.

Et le compteur d'occurrences perdues ne le verrait pas non plus : **aucune
occurrence n'est enfilée, il n'y a rien à périmer.**

**Le verrou porte désormais sur `next_due <= NOW()`.** L'exclusion mutuelle est
intacte — deux ticks se sérialisent sur la ligne, et le second ré-évalue son
`WHERE` après le verrou : l'échéance est alors dans le futur. ⚠️ Et ça ferme un
second défaut au passage : l'ancienne forme pouvait consommer une échéance **pas
encore due** (le tick filtrait avant, donc la garde ne tenait pas seule).

**Ça ne se produisait pas** parce que toutes les échéances viennent de croniter,
qui rend des secondes rondes. *Une garantie qui tient par la propriété d'une
bibliothèque tierce n'est pas une garantie.*

### Le worker est un SERVEUR de boucles agentiques (05/09/2026)

Le modèle, dit par Alexis et désormais tenu par le code : **le worker héberge des
boucles agentiques qui impersonnent chacune leur user**. Deux couches, et ne pas
les confondre est ce qui évite les deux défauts trouvés cette semaine :

```
ce que l'AGENT fait      au nom du user — jeton délégué, borné au bail
                         (lire un doc, écrire une ligne, appeler un outil)
ce que le RUNTIME        au nom de personne — la clé de modèle, ressource
consomme pour tourner    d'exécution payée par l'org, jamais un droit du user
```

⚠️ **Le serveur n'a AUCUNE identité métier**, et c'est ce qui rend un repli
inacceptable. Un travail sans porteur était servi nu, et le worker retombait sur
son propre jeton : une boucle agissant au nom du compte qui héberge le runner,
tout ce qu'elle écrit signé par lui. Le défaut est silencieux **par
construction** — les écritures aboutissent, seule l'attribution est fausse, et
rien ne la contredit. Un travail sans porteur est donc REFUSÉ, en base, avec la
sortie nommée (le reprogrammer).

⚠️ Et c'est la même distinction qui explique la garde de la clé : le user ne peut
pas relire un secret du coffre — personne ne le peut. Si le worker obtient la
clé, ce n'est donc pas par l'impersonation, c'est par un droit d'infrastructure.
Un droit d'infrastructure exige une identité d'infrastructure : d'où la marque
`runner_worker`, et d'où le fait qu'aucun compte de personne ne doit la porter.

### Un travail porte l'identité de qui l'a demandé (02/09/2026)

**Premier barreau du chantier « agents autonomes », et le préalable de tout le
reste.** Aujourd'hui un agent s'authentifie avec le jeton d'une ORGANISATION :
c'est ce qui impose mécaniquement **un worker par organisation**. Ce n'est pas un
choix d'architecture qu'on pourrait discuter — c'est un empêchement, et c'est lui
qui a laissé 41 travaux programmés sans personne pour les prendre.

**Un travail qui porte son identité dispense le worker d'en avoir une par
organisation.** `runner_jobs.sub` répond donc à « au nom de QUI l'agent agira »,
pas à « qui a cliqué » : ce n'est pas une trace d'audit.

```
déclencheur   →  l'identité est celle de son CRÉATEUR
                 (le tick n'a pas d'identité propre : c'est une horloge,
                  pas un acteur)
appel direct  →  l'identité vient de `ctx.sub`, l'état SERVEUR
                 ⚠️ jamais d'un champ d'entrée — un travail dont l'appelant
                 choisirait le porteur serait une usurpation en une ligne de
                 JSON, et elle passerait inaperçue puisque le travail
                 s'exécuterait normalement, sous un autre nom
```

⚠️ **NULLABLE, et ça le reste.** Les travaux enfilés avant le 02/09 n'ont pas de
créateur connu. Leur en inventer un — le premier admin, un compte de service —
donnerait un nom qui **se lirait comme un fait**. Un « je ne sais pas » explicite
vaut mieux qu'une réponse fausse : c'est celui-là qu'on pourra corriger.

**Ce que ce barreau ne fait PAS encore** : rien n'est changé à l'authentification.
Le worker présente toujours son jeton et le claim reste scopé à son organisation.
La délégation — le worker agissant AU NOM du porteur — est le barreau suivant, et
c'est lui qui rendra le worker mutualisable. ⚠️ Le paramétrage de l'identité vers
un autre membre (validé le 02/09) passera par une garde d'appartenance, jamais
par la confiance faite au corps de la requête.

### Le worker porte l'identité du demandeur — il n'a aucun pouvoir propre (02/09/2026)

**Barreau 2, et il est plus court que prévu.** J'allais concevoir une primitive de
délégation ; Alexis a tranché : *« rien, il est juste un client MCP qui porte
l'identité du user »*. ⚠️ **Le mécanisme existait déjà** — `user_api_tokens` porte
des jetons par personne, avec échéance et portée.

```
à la RÉSERVATION    le serveur vérifie que le porteur est encore valide, émet un
                    jeton À SON NOM (durée du bail + 2 min) et le rend au worker
pendant le travail  le worker appelle avec ce jeton — client ORDINAIRE, aucun
                    chemin d'autorisation particulier, aucun droit propre
porteur invalide    le travail passe `failed` AVEC SA RAISON, et le refus est
                    servi : l'agent s'arrête EN LE DISANT
```

⚠️ **Ce que ça évite** : pas de nouvelle primitive de sécurité, pas de liste de
workers habilités (dont la compromission ouvrirait tous les comptes), pas de
paramètre « agis en tant que » (usurpation en une ligne de JSON). Le pouvoir est
**borné par l'échéance du jeton**, sans qu'on ait eu à l'inventer.

**La marge de 2 minutes au-delà du bail** : un agent qui conclut à la dernière
seconde doit pouvoir écrire. Couper au bail exact tuerait un travail abouti juste
avant sa conclusion — le pire moment, puisqu'il a déjà tout coûté.

⚠️ **`create_api_token` fait un `upsert_user`** : il CRÉE le compte s'il n'existe
pas. L'existence se vérifie donc AVANT — sinon on ressusciterait un compte
supprimé et on lui délivrerait un accès dans la foulée.

⚠️ **Le refus n'est pas un `complete_job(ok=False)`** : celui-là refile avec
backoff jusqu'au plafond, donc rejouerait trois fois le même verdict. Et surtout
pas un relâchement silencieux — le travail repartirait au worker suivant
indéfiniment, *une file qui tourne sans jamais aboutir*. `failed` et non
`expired` : celui-ci a bien été PRIS.

**Ce qui se vérifie, et ce qui ne se vérifie pas** : les trois cas arrêtés le
02/09 sont compte supprimé, sortie de l'organisation, rôle retiré. ⚠️ **Les deux
derniers ne se distinguent pas** dans le modèle — être membre, c'est avoir un
rôle, `org_members` porte les deux en une ligne. La raison rendue le dit en une
phrase plutôt que d'inventer une distinction que la base ne fait pas.

⚠️ **Vérifié à la RÉSERVATION seulement** (arbitrage explicite) : un travail long
continue avec un droit retiré en cours de route. C'est assumé, pas oublié.

### La clé de modèle de l'org part avec le travail réservé (#874, 04/09/2026)

La clé de modèle **vit avec les autres secrets de connecteurs de l'org**, et le
worker — qui fait partie du backend — a le droit de la lire. Ce droit s'exerce
**à la réservation, une fois, avec le travail**. Le runner n'interroge jamais le
coffre : un worker qui saurait l'interroger pourrait y lire autre chose que ce
travail-ci. Avant ce lot, toutes les orgs tournaient sur la clé de la plateforme,
prise dans l'environnement du worker.

```
claim(provider="anthropic")  le worker NOMME le dépôt qu'il sait consommer
  → job["model_key"]         la clé de l'ORG DU TRAVAIL, si elle en a déposé une
absente                      → le worker retombe sur la clé de la plateforme
```

⚠️ **La garde porte sur le TYPE du dépôt, pas sur son nom.** Si le worker pouvait
nommer n'importe quel connecteur, *réserver un travail suffirait à faire sortir le
secret Folk ou Salesforce de l'org*. Seuls les `kind="credential"` passent — ceux
dont porter une clé est la seule raison d'être, sans aucun outil derrière. **C'est
la raison d'être du type distinct** plutôt que d'un connecteur ordinaire aux
namespaces vides : le type EST la liste d'autorisation.

⚠️ **La clé est celle de l'org du travail**, jamais d'une org que le worker
nommerait : il choisit le dépôt, jamais à qui il appartient. Un travail déjà
refusé pour identité n'en reçoit aucune — lui en remettre une armerait un travail
qui ne doit pas tourner.

⚠️ **Ce que les journaux en voient : rien**, et c'est tenu par des cliquets, pas
par une promesse. `tool_calls` ne garde aucune réponse (la clé part dans la
réponse au claim, pas dans ses arguments — le masque de #558/#564 ne la couvre
donc pas et n'a pas à le faire) ; `_avec_cle` rend une copie ; Sentry a
`include_local_variables=False` (#564) ; le runner n'a pas de Sentry et ne
journalise que `job["id"]`.

⚠️ **La clé n'est remise qu'à un compte MARQUÉ worker** (option de compte
`runner_worker`, posée par un admin plateforme). Sans cette garde — c'est le
défaut trouvé par dev 1 le 04/09, avant tout dépôt réel — n'importe quel membre
de l'org faisait `enqueue` puis `claim provider=…` et recevait la clé EN CLAIR :
la capacité est `ORG_MEMBER`, et **rien dans le protocole ne distingue un worker
d'un membre**, ils portent le même genre de jeton sur la même route. Un membre
reçoit désormais son travail SANS clé, sans refus explicite (le refus
apprendrait qu'il y a une clé à obtenir) mais avec une ligne de journal : un
membre qui nomme un dépôt cherche quelque chose.

⚠️ **Le refus ne se journalise que s'il y a quelque chose à refuser.** Les
workers nomment leur dépôt à CHAQUE réservation — trois workers, toutes les
15 secondes : sans ce filtre, la garde écrivait ~17 000 lignes par jour tant que
la marque n'était pas posée, c'est-à-dire un journal que plus personne ne lit et
une sonde qui fabrique son propre signal. La présence du dépôt se lit sans
déchiffrer (`has_credential`, `secret_enc IS NOT NULL`) : le secret n'est jamais
touché pour décider d'écrire une ligne.

⚠️ **La marque se lit par `access.user_has_option`, jamais `has_option`.** Ce
dernier répond vrai dès que l'ORG ACTIVE porte le don ou que son plan inclut
l'option : l'employer ici aurait servi la clé à **tous les membres** de cette
org — la fuite même que la garde ferme. `user_has_option` est le miroir de
`org_has_option` : la moitié COMPTE du seam, pour les questions qui portent sur
l'acteur et sur lui seul.

**Ce qui n'est pas ici** : la grille d'offre — qui a droit à la clé de la
plateforme, qui doit déposer la sienne. Elle appartient au chantier « qui a le
droit de quoi et pourquoi », au blueprint. Aujourd'hui, une org sans dépôt
continue sur la clé de la plateforme.

### Une occurrence que personne ne prend PÉRIME, et ça se dit (#814, 02/09/2026)

Le refus de poser un déclencheur sans agent ferme la porte d'entrée. **Il ne fait
rien pour ceux qui sont déjà dedans** — et c'est là qu'était le vrai trou.

**Ce qui l'a daté.** 41 travaux programmés attendaient dans la file, sur quatre
organisations, `attempts = 0` : jamais pris, pas même une fois pour échouer. Le
plus ancien datait de treize jours, le plus récent du matin même — donc *ça
continuait*. Les déclencheurs enfilaient, les agents prenaient ce qu'ils
pouvaient voir, le périmètre par organisation protégeait : **chaque pièce faisait
exactement son travail, et leur composition fabriquait le trou.** Rien ne le
disait, parce qu'**un travail « en attente » ressemble à un travail qui va
partir**.

⚠️ **Et le pire cas n'était pas l'attente, c'était la réparation naïve** : le jour
où quelqu'un pointe des agents sur cette organisation, treize jours d'occurrences
partent d'un coup, avec la procédure et le contexte de leur époque. **Un travail
qui attend n'est pas gratuit, il est daté** — une veille quotidienne jouée treize
jours plus tard ne rend pas un résultat en retard, elle rend un résultat FAUX.

**La règle : une occurrence périme quand la SUIVANTE arrive.** Le tick périme les
`pending` du déclencheur juste avant d'enfiler.

⚠️ **La définition vient du cadencement, jamais d'un délai choisi.** Un délai fixe
serait faux des deux côtés à la fois — trop court pour une veille mensuelle,
absurde pour une horaire — et surtout, *un réglage est une chose qui se périme
elle-même*. Ici il n'y a rien à tenir à jour : c'est un garde-fou sans gardien.

**Périmer ne SUPPRIME rien** — nouvel état `expired`, distinct de `failed` :

```
pending   enfilé, personne ne l'a encore pris
claimed   un agent l'a réservé
done      exécuté
failed    a TOURNÉ et a échoué        ⟹ va lire l'erreur
expired   n'a JAMAIS tourné            ⟹ va voir qui dessert cette org
```

⚠️ Les confondre coûte un faux aiguillage : « échoué » envoie chercher une erreur
d'exécution **qui n'existe pas**. Et purger au lieu de marquer remplacerait un
trou silencieux par un pire — *il effacerait la preuve du premier*. Ces 41
travaux ont été le seul indice qu'une automatisation ne tournait pas ; purgés à
mesure, personne n'aurait jamais rien vu.

**La perte se lit sur le DÉCLENCHEUR** (`list`/`get` portent `expired_count`,
`expired_since`, `expired_last`) — là où on la cherche, et non dans une file que
personne n'ouvre. Ces 41 occurrences ont été découvertes **par hasard**, en
préparant autre chose : une perte que seule une requête manuelle révèle n'est pas
une perte connue. `expired_count: 0` est un vrai zéro, servi, pas une absence de
mesure. **Deux dates et pas une** : une perte ancienne qui a cessé n'appelle pas
le même geste qu'une perte qui continue ce matin.

⚠️ **Un déclencheur qui ne TIQUE plus ne périme plus** — et les deux gestes qui
l'arrêtent laissaient donc leurs occurrences éternelles. **Éteindre** (`enabled =
false`) le sort de la boucle du tick ; **supprimer** est pire encore, puisque le
compteur de pertes se lit SUR le déclencheur : elles devenaient *invisibles en
même temps qu'éternelles*, tout en restant réclamables le jour où des agents
arrivent — pour un déclencheur que plus personne n'a.

> ⚠️ **Le geste de réparation aggravait la panne.** Quelqu'un qui constate qu'une
> automatisation ne tourne pas l'éteint : c'est exactement ce qu'a fait
> l'utilisateur du 26/08, et c'était le seul geste à sa portée. Il figeait la file
> au lieu de la vider.

Les deux périment donc avant d'agir, avec **leur propre raison** — « le cycle a
tourné », « le déclencheur a été supprimé » et « il a été désactivé » n'envoient
pas au même geste. ⚠️ Et seul le passage à ÉTEINT périme : périmer aussi au
rallumage effacerait une occurrence fraîche, et *une garde qui mord dans les deux
sens ne se distingue pas d'une purge*.

**Et RALLUMER reprend le rythme, ça ne rembobine pas** (arbitré par Alexis le
02/09, #826). Le rallumage recalcule l'échéance ; sans ce recalcul, celle qui
avait été figée pendant l'extinction est restée dans le passé, et le tick voyait
le déclencheur dû **à la seconde du rallumage** — donc une exécution que personne
n'a demandée, déclenchée par le geste de quelqu'un qui répare.

⚠️ **C'est la cohérence qui l'impose, pas le confort** : puisque éteindre périme
les occurrences en attente, *un système qui dit « ce qui a attendu pendant
l'extinction est mort » ne peut pas dire « sauf l'échéance »*. Une échéance
manquée pendant une extinction VOULUE n'a pas été manquée.

⚠️ Et seul le **passage** à allumé recalcule — même motif que la péremption, qui
ne mord qu'au passage à éteint : recalculer sur un déclencheur déjà allumé
donnerait un moyen de repousser son échéance indéfiniment, en répétant un geste
qui n'est pas censé rien changer.

⚠️ **L'ordre des refus est un contrat** : l'état du déclencheur se lit APRÈS la
garde « aucun runner armé », jamais avant. Le lire d'abord ferait répondre
« déclencheur inconnu » là où le serveur répond « aucun runner » — deux
diagnostics opposés, et celui qu'on retirerait est le seul qui dit quoi faire.

**La forme générale du piège**, qui vaut au-delà d'ici : *« ne pas toucher » n'est
une conservation que si quelque chose garantit la cible.* Rien ne la garantit
entre un travail et son déclencheur — il n'y a pas de clé étrangère, seulement un
identifiant recopié dans la charge.

⚠️ **L'hygiène ne coupe jamais le service** : une péremption qui échoue est
journalisée et l'enfilage continue. L'inverse ferait qu'un défaut d'entretien
arrête les automatisations de tout le monde — et le pire qu'on risque en la
ratant est ce qu'on avait déjà, un travail de trop en attente. *(Défaut trouvé
par son propre test avant d'être servi.)*

### `op=list` : la page dit ce qu'elle laisse dehors (#469, 01/09/2026)

**Mesuré le 28/08** : `POST /api/me/runner/jobs {op: list, limit: 1000}` rendait
**200** lignes. La borne était appliquée dans le `LIMIT` du SQL
(`db/runner_jobs.py`), sans être déclarée nulle part et sans que la réponse ne
l'annonce : ni total, ni curseur. Un poste de flotte qui faisait le bilan d'une vague
de 150+ jobs lisait donc `len(jobs)` comme le compte de la file — et lisait faux.

⚠️ **Un relevé plafonné SOUS-déclare : il rend moins d'anomalies que la réalité,
jamais plus.** C'est la classe de défaut qui rassure exactement quand il ne faut pas,
et c'est pour ça qu'elle vaut mieux qu'une gêne d'ergonomie. Le runner s'en était
affranchi par un bilan natif côté client — une rustine qui masque le défaut au lieu
de le fermer, et qui ne protège aucun autre consommateur de la route.

La page porte donc deux champs, et ils vont ensemble :
- **`total`** — le nombre de jobs de la file sous les MÊMES filtres (org + `status`),
  indépendant de `limit` et de la position du curseur. C'est le dénominateur d'un
  bilan ; il ne bouge pas d'une page à l'autre.
- **`next_cursor`** — opaque, à renvoyer tel quel dans `cursor` pour lire la page
  suivante (plus ancienne) ; `null` = fin de la file. **Une page pleine AVEC un
  `next_cursor` dit que la lecture est tronquée ici.**

Le curseur est un **keyset** sur l'ordre servi (`id DESC`), pas un OFFSET : une file
bouge sous la marche, et un job enfilé entre deux pages décalerait tout un OFFSET —
donc ferait sauter une ligne, c'est-à-dire recréerait la sous-déclaration qu'on
ferme. Un curseur illisible est un **refus nommé** (`400 invalid_cursor`), jamais un
repli muet sur le début de la file : rejouer la première page en boucle est
indiscernable d'une marche qui progresse.

La borne (`JOBS_PAGE_MAX = 200`) reste appliquée dans le SQL en dernier ressort, mais
celle qui ENGAGE est désormais au contrat (`capabilities/runner_jobs.py`, patron
`cap_limit` : on écrête, on ne refuse pas) — et l'écrêtage n'est plus muet.

### Le BAIL est le seul mécanisme qui libère — décidé le 05/09/2026 (oto-backend#324)

Il n'existe **aucun ramassage périodique des runs abandonnés**, et c'est un choix, pas
un oubli. Un run qui meurt sans conclure laisse ses lignes réservées jusqu'à
l'expiration de son bail, qui finit par les rendre à la file. C'est lent, et ça marche.

**Pourquoi ne pas ajouter un ramasseur** : ce serait un **second mécanisme sur le même
objet**. Deux gardes qui libèrent la même ligne peuvent diverger — sur le délai, sur ce
qu'elles considèrent comme mort, sur ce qu'elles écrivent en partant — et la journée où
elles divergeraient, personne ne saurait laquelle a agi. Le bail a déjà cette
responsabilité et il la remplit ; la question n'est pas d'en ajouter une seconde, mais
de raccourcir le bail si l'attente devient le problème.

⚠️ **Ce que ce choix coûte, et qu'il faut savoir** : entre la mort d'un run et
l'expiration de son bail, ses lignes sont invisibles pour la file — un poste de flotte
qui les attend croit la file vide. La maintenance compte bien des objets périmés
(`maintenance.py`), mais ce sont des **nœuds**, pas des runs : rien ne rend visible un
run mort.

**Ce qui rouvrirait le sujet** : une flotte qui laisse assez de runs morts pour que
l'attente du bail se voie — c'est-à-dire un volume, pas une inquiétude.

### `complete` libère les baux du run et rend le compte — `0` écrit (#633, 29/08/2026)

**Mesuré sur une campagne** : un poste de flotte lit « le témoin que la clôture du
travail rend » — or `op=complete` ne libérait aucune ligne du datastore et rendait
`{"ok", "status"}` sans compte. La libération ne jouait que sur `run_finish`, l'appel
de l'**agent** — qui rendait `rows_released` seulement s'il y avait au moins une ligne
(absent = zéro). Un agent mort sans `run_finish` laissait sa ligne au bail jusqu'à
expiration ; le **worker**, lui, survit à l'agent et conclut le job : c'est là que la
libération manquait.

**Depuis #633**, `complete` libère les baux du run que le job connaît — le `run_id` de
l'appel d'abord, sinon celui posé par `bind_run` (ou un `continue`) — par
`datastore_release_by_run`, **quel que soit `ok`** (un job qui repart en file avec
backoff ne travaille plus non plus ; la ligne revient dans la file, la reprise la
reprendra). Best-effort et HORS de la clôture, comme `run_finish` : le job est conclu
d'abord, la libération est un service rendu ensuite. La réponse porte trois champs
déclarés dans l'`Output` (donc dans l'OpenAPI) :

| forme | sens |
|---|---|
| `run_id: "…", rows_released: 2, release: "ok"` | le run tenait 2 lignes, rendues |
| `run_id: "…", rows_released: 0, release: "ok"` | le run ne tenait rien — **le 0 est écrit** |
| `run_id: null, rows_released: null, release: "no_run"` | aucun run connu du job : rien à libérer par run, rien n'est fabriqué |
| `run_id: "…", rows_released: null, release: "failed"` | la libération a échoué (journal serveur) ; le job est conclu, les baux expirent seuls |

`run_finish` écrit lui aussi `rows_released` **toujours** (`0` explicite ; `null` si la
libération a échoué) — sa description ne change pas, c'est la réponse. Preuves :
`tests/test_complete_releases_633.py` (chemin réel : réservation par le middleware +
`data_claim_next` monté, capacité `runner.jobs` telle que la route l'appelle, PostgreSQL)
et `tests/test_run_finish_releases_613.py`. ⚠️ `runner_jobs.run_id` référence `runs`
(FK) : un job ne se lie qu'à un run qu'un `run_start` a ouvert.

### Ce qu'un écran de surveillance lit d'un travail (01/09/2026)

Deux manques de la même famille — une donnée que la plateforme détenait déjà et qui ne
sortait pas.

**Le bail, sur `list` et `get`.** `lease_until` n'était rendu que par `op=claim`,
c'est-à-dire au seul worker qui vient de prendre le job ; les deux verbes de
surveillance ne le sélectionnaient pas. Un écran ne pouvait donc pas dire « ce bail a
expiré », seulement « ce travail traîne depuis longtemps » — un **seuil dérivé** de
l'ancienneté, qui range dans la même case un travail lent et un travail mort. La
colonne porte la DATE ; c'est au lecteur de la comparer à l'heure qu'il est, **contre
le statut** :

| statut | `lease_until` |
|---|---|
| `pending` jamais pris | `null` |
| `claimed` | la fin du bail en cours — passée = le worker est parti, le job est re-claimable (`attempts` compte chaque prise) |
| `done` | le bail qui ÉTAIT tenu, laissé tel quel |
| échec re-filé | `null` — la prise est rendue en même temps que le job |

**Les postes de garde du harnais, au contrat.** `result` est ouvert (`extra=allow`) :
le worker y déclare bien plus que les quatre champs du socle, et tout est **servi**.
Mais servi n'est pas **déclaré** — un client typé (les types générés du dashboard,
dérivés de l'OpenAPI) ne voit que ce que le schéma nomme, et rien ne garantit la forme
de ce qu'il ne nomme pas. Trois champs sont désormais nommés sur `JobResult`, parce que
leur forme porte un sens qu'un client peut se tromper en lisant :

| champ | forme | ce que `null` veut dire |
|---|---|---|
| `valeurs_cliente_reparees` | liste de colonnes remises en place depuis `<colonne>.origine` | — (`[]` = rien à réparer) |
| `contacts_fabriques_retires` | liste de contacts fabriqués RETIRÉS de la ligne | — (`[]` = aucun) |
| `valeurs_cliente_detruites` | liste de colonnes détruites, **ou `null`** | ⚠️ **NON MESURÉ** : le harnais n'a pas pu identifier la ligne travaillée, la garde n'a pas tourné |

⚠️ `valeurs_cliente_detruites: null` **n'est pas** `[]`. Le lire comme « aucune
destruction » afficherait un travail propre là où personne n'a regardé — et c'est le cas
FRÉQUENT, pas le cas limite : sur le chemin « conversations » le harnais retrouve sa
ligne par alias, et ce recours échoue dès qu'elle est relâchée. Preuves :
`tests/test_runner_jobs_travail_servi.py`.

**Les autres champs de `result` restent indéclarés**, et c'est un manque connu, pas un
choix : `writes`, `claims`, `model`, le détail de coût (`usage_input`/`usage_output`/
`usage_cache_read`/`usage_cache_write`), `hors_schema`, `hors_perimetre`, `claims_mesures`, `claim_vide`,
`faux_depart`, `estampille`, `renvois`, `abandon_enregistre`, `rappel_contact_mesure`,
`rappels_contact`, `effectif_non_atteste`, `contact_rattrape`, `contact_arbitre`,
`ligne_abandonnee`. Ils traversent par `extra=allow` et un client typé ne les voit pas.

## Automatisations — déclencher une routine Claude Code (v1.73.0)

Connecteur `routine` (`routine_fire.py` + capacité `me.automation.fire`, MCP
`routine_fire` / REST `POST /api/me/automations/fire`) : **une instance = une routine**
hébergée chez Anthropic (`routine_id` + jeton de déclenchement en `credential_fields`),
parce que le jeton `/fire` est scopé par Anthropic à une seule routine. L'appel ne bloque
pas — il crée la session et rend son URL ; le résultat se lit **dans la session**.
Le `text` arrive à l'agent enveloppé `<routine-fire-payload>` étiqueté DONNÉE NON FIABLE
(le prompt de la routine doit opter pour le lire) ⟹ passer une **référence**, jamais
l'enregistrement. Montage complet côté utilisateur = guide plateforme
**`procedure-en-routine`**.

⚠️ **Ce connecteur relaie, il n'apporte rien d'autre** : un tiers qui sait faire un POST
appelle `/fire` en direct. Son seul cas réel est *un agent en conversation qui déclenche
une automatisation*. Il ne vaudra plus que ça tant qu'oto ne fait rien entre les deux
(tracer les tirs, router selon l'événement, dédupliquer). **Aucune API publique de
création de routine ni de génération de jeton** — le provisionnement reste manuel, par
construction ; l'état vide de la page Automatisations du dashboard l'explique.
