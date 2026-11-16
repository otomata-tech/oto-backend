---
title: Boucle d'usage (ADR 0017 — déroulés + feedback volontaire)
type: reference
description: >-
  Référence du flux d'événements de session unifié dans oto-backend (ADR 0017) :
  corrélation session_id + run_id sur tool_calls (colonnes OTO-locales, index dans
  ALTER pas dans _SCHEMA), tools spine run_start/run_finish (tools/guide_run.py,
  pile session FastMCP, runs imbriqués OK), et capacité feedback (signal
  tool_feedback|gap → table durable usage_signals hors prune 30j). Détaille les
  projections admin /api/admin/usage/* (runs, gaps, tool-quality, signals filtrables
  par les quatre états d'arbitrage) et l'arbitrage de signaux via oto_admin_signal
  (op=set_status). À charger
  pour comprendre comment tracer un déroulé d'agent, remonter un gap d'outil, ou
  déboguer un run_id manquant sur les appels.
adr:
  - "0017"
---

# Boucle d'usage (ADR 0017 — déroulés + feedback volontaire)

Un **flux d'événements de session** unifie le calllog (involontaire) + le feedback
volontaire d'agent + les runs / déroulés. Détail : ADR 0017 (repo public
`otomata-tech/oto`). Surfaces livrées (B1–B6) :

- **Corrélation** : `tool_calls` gagne 2 colonnes **OTO-LOCALES** (hors contrat canonique
  calllog/otomata-mcp) `session_id` (session mcp transport) + `run_id` (déroulé). Stampées
  par `server._calllog_sink` qui lit `get_context().session_id` + le run actif. ⚠️ piège
  rattrapé : l'index sur `run_id` va dans le bloc **ALTER** d'`init_db` (après l'ADD COLUMN),
  **jamais** dans `_SCHEMA` (no-op sur table existante → `UndefinedColumn` au boot).
- **Runs / déroulés** : tools spine `run_start`/`run_finish` (`tools/guide_run.py`) ;
  `run_start(label, guide?)` ouvre un guide nommé (`guide`=slug ; l'ancien nom du
  paramètre reste accepté jusqu'à sa date, `docs/alias-deprecies.md`) **ou** un run
  one-shot (sans `guide`), même trace. Le `run_id` vit dans une **pile en état de
  session FastMCP** (`guide_run.py`, runs imbriqués OK), stampé sur chaque appel côté
  serveur — l'agent ne thread rien. Le sommet de la pile est aussi posé par
  `CallContextMiddleware` en **run de session** (`session_org.current_session_run`, #830)
  quand l'appel ne nomme aucun `_run_id` : le datastore, le journal des gestes et les
  jobs `jev` le lisent ; la résolution d'org du run (#639) et les gardes d'`oto_fleet`
  restent sur le seul `_run_id` explicite (`docs/org-context.md` §L'org du run).
  - **Une session neuve d'agent hébergé reprend son run** (27/10/2026) : la pile ne
    survit pas à la session, et le relais du runner, relancé après un appel coupé côté
    client, en ouvre une autre — les appels suivants partaient avec `run_id` NULL, la
    timeline s'arrêtait là (le relais retire `_run_id` des schémas servis : l'agent ne
    peut pas le repasser). `CallContextMiddleware` appelle
    `guide_run.reprendre_run_du_jeton` : pour un jeton de **délégation** seulement (un
    par travail), le run que CE jeton a ouvert et qui n'est pas clos
    (`db.run_ouvert_du_jeton`, lu du journal : `run_start.token_id`, clôture par
    `_run_closure`, borné à l'org du travail et à la création du jeton — les têtes de
    `idx_tool_calls_run_start_sub`) est poussé sur la pile de la session neuve.
    Cherché une fois par session, échec compris ; jamais un jeton d'utilisateur, jamais
    un autre compte.
  - **Retrouver un `run_id` perdu** (#473, 28/08) : `oto_project op=runs` **sans**
    `project_id` rend MES déroulés encore OUVERTS, chacun avec son `run_id` et son état
    lisible (`db.my_runs`). Sans ça, un agent qui perd le fil ne peut plus clore ce
    qu'il a ouvert — `run_finish` n'accepte qu'un `run_id`, le bloc de contexte annonce
    les déroulés par leur INTITULÉ, un run ouvert hors projet n'est rattaché à aucun, et
    la seule surface qui portait l'identifiant (`oto_org_monitoring op=runs`) est une
    lentille d'org_admin. Le déroulé restait « en cours » pour toujours — le régime
    dominant, pas le cas rare (cf. §run silencieux).
    ⚠️ **Scopé au `sub`, PAS à l'org** — délibérément, à la différence de toutes les
    lentilles voisines : un run s'ouvre dans l'org active et l'agent en change en route,
    donc borner à l'org courante cacherait exactement le run qu'on ne retrouve plus. La
    propriété, elle, borne dur (la règle même de `finish_run`).
- **Usage d'une procédure** (membre) : `GET /api/me/instructions/{slug}/usage` (une
  procédure, séries 30 j) et `GET /api/me/instructions-usage` (toutes les procédures de
  l'org en un appel, pour les listes — hors de `/api/me/instructions/…`, où `usage` se
  lirait comme un slug). Les deux lisent le journal sous deux verbes — chargements
  `oto_procedure` (clé `slug`), déroulés `run_start` (clé `doctrine`) — émis SOUS l'org,
  réussis, sur **une seule fenêtre de 30 jours UTC** (`count == sum(series)`), en UNE
  requête servie par `idx_tool_calls_org_tool_ok`, sous la borne de 10 s des lectures
  d'agrégat. ⚠️ Le couple verbe → clé est une liste FERMÉE (`usage._VERBES_USAGE`) : la
  clé est interpolée dans le SQL, et un verbe lu sous la clé d'un autre rendrait zéro.
- **Signaux volontaires** : capacité MCP+REST unique (`capabilities/usage.py`) `feedback`
  — axe explicite `signal` ∈ `tool_feedback | gap` → table **durable** `usage_signals`
  (hors prune 30j). `gap` = cas d'usage non couvert (l'agent capte la demande non satisfaite).
- **Un sujet en attente ne se redépose pas, il se rattache** (06/10/2026) : un signal de
  même org, même `signal` et même `target` (casse et blancs ignorés) déposé alors qu'un
  signal de cette clé attend son arbitrage (open | acknowledged) devient une **occurrence**
  de celui-ci (`usage_signal_occurrences` : auteur, session, genre, texte — rien n'est
  perdu). L'agent reçoit `already_reported` (état, décision, nombre d'occurrences) et sait
  qu'il n'a pas à redéposer. Mesuré ce jour-là : une quarantaine de redites sur 338
  signaux en attente. ⚠️ Un sujet **clos** (resolved | declined) qui revient crée un signal
  **neuf** (`regression_of` cite le dernier clos) : une régression doit se voir. ⚠️ Sans
  cible, rien ne se rattache. ⚠️ L'org fait partie de la clé (#684/#685). Le **rejeu**
  (même texte, même auteur, < 10 min) ne s'écrit toujours pas du tout.
- **Un dépôt qui ne signale rien est refusé** (13/11/2026) : 400 `feedback_placeholder`
  (`McpError` au même message côté MCP), rien n'est écrit, quand `text` EST un mot de
  remplissage (`noop`, `no-op`, `placeholder`, `ignore`, `test`, `n/a`, `accidental call`,
  `internal-note-skip`, `noop-wait`… — liste fermée `usage._PLACEHOLDERS`), ou qu'il est vide
  et que `target` est vide ou un mot de remplissage. Comparaison sur la valeur ENTIÈRE
  (minuscules, blancs et ponctuation de bord ignorés) : un vrai retour qui contient « test »
  dans une phrase passe. ⚠️ Un `text` vide avec une vraie cible passe aussi : `text` reste
  optionnel. Aucune longueur minimale n'est exigée.
- **Projections** (opérateur) : `/api/admin/usage/{runs,runs/{id},gaps,tool-quality,signals}`
  (`capabilities/usage.py`, PLATFORM_ADMIN) → vue dashboard `UsageView.vue` (« usage & déroulés »).
  `signals` filtrable par `status`, chaque ligne avec `occurrences` et `last_seen_at`, triée
  sur la dernière occurrence ; la réponse porte AUSSI `counts` (la pile entière par
  état — une page ne dit pas si le stock fait 203 ou 2 000). Face MCP
  `oto_admin_signal(op='list')` (console ADR 0047).
- **Arbitrage — QUATRE états** (#450, 27/08) : `POST /api/admin/usage/signals/{id}/status`
  (MCP `oto_admin_signal(op='set_status')`, PLATFORM_ADMIN).
  - `open` — reçu, personne ne l'a regardé. Le retour à cet état EFFACE la trace
    d'arbitrage : un signal remis dans la pile n'a plus été arbitré, et garder l'ancienne
    note ferait lire une décision qui n'a plus cours.
  - `acknowledged` — lu, décision pas prise. **C'est l'état qui manquait**, et où vit
    l'essentiel d'une pile de retours.
  - `declined` — décidé de ne pas traiter. **`note` obligatoire** : sans motif, un refus
    est indistinguable d'un oubli.
  - `resolved` — traité. Aucune note exigée (le travail livré parle de lui-même).
  Le backlog vivant = `signals?status=pending` (= open ∪ acknowledged). `pending` est un
  FILTRE, pas un état.
- **Ré-aiguillage — corriger l'ORG d'un signal** (#471, 28/08) :
  `POST /api/admin/usage/signals/{id}/org` (PLATFORM_ADMIN, `org_id` requis, `null` =
  plateforme). Pour un signal écrit AU SUJET d'un espace et déposé sur un AUTRE, parce
  qu'un appel avait omis son jeton d'org — il y restait à jamais, compté dans les
  lentilles d'un espace qui n'aurait jamais dû le voir. Seule l'ADRESSE bouge : corps,
  état et arbitrage sont intacts, et la réponse porte `previous_org_id` (de quoi
  vérifier le geste, et le défaire si c'est la destination qu'on a mal tapée).
  - ⚠️ **Un signal ne se SUPPRIME pas, il se ré-aiguille** — décision du 28/08, tenue
    par un test. Un signal est un FAIT (l'agent a réellement buté sur ce manque) et sa
    ligne en est l'unique copie ; le déplacer le retire du mauvais espace ET le rend au
    bon (les deux lentilles d'org comptent par `org_id`), là où une suppression ferait
    la première moitié, perdrait la seconde, et rouvrirait sous un autre nom la porte
    que `declined` referme en exigeant un motif.
  - **Face MCP `oto_admin_signal(op='reroute')`** (28/08) : la route avait été livrée
    seule, donc l'agent qui venait de constater l'erreur — souvent celui dont l'appel
    avait omis le jeton d'org — n'avait rien pour la réparer et repassait par le
    dashboard. ⚠️ **La destination s'y écrit `to_org`, une CHAÎNE** : `<id>` ou
    `platform` en toutes lettres, jamais l'`org_id` de la route. Sur une console tous
    les champs sont optionnels et **fastmcp remplit les défauts avant d'appeler le
    handler** — un `org_id` omis et un `org_id: null` y arrivent rigoureusement
    identiques, `model_fields_set` compris. Le mot rétablit l'invariant de la route
    (« l'écriture est toujours un choix ») : absent = refusé en nommant ce qui manque,
    `platform` = voulu. Un mot de travers est refusé, jamais lu comme la plateforme.
- **Le retour à celui qui a signalé (#451, 27/08)** : `POST /api/admin/usage/notify-reporters`
  (MCP `oto_admin_signal(op='notify_preview'|'notify_send')`, PLATFORM_ADMIN). ⚠️ **Ces
  deux `op` n'ont réellement existé que le 28/08** : le lot du 27 les avait ajoutés à la
  description du tool en oubliant le `Literal` et l'aiguillage — annoncés à l'agent,
  refusés par le schéma, et rien pour comprendre que c'était la description qui mentait.
  Les deux listes sont désormais tenues égales dans les deux sens par un test. Colonne
  `usage_signals.notified_at` — NULL = son auteur ne sait pas encore ; effacée à chaque
  changement d'état, pour qu'un signal ré-arbitré soit re-annoncé.
  - **UN mail par PERSONNE, jamais un par signal.** Ce n'est pas une commodité : mesuré le
    27/08, 3 personnes portaient 168 des 204 signaux en attente, dont deux externes à 51 et
    53. Un envoi par signal aurait expédié cinquante mails d'affilée à un partenaire le jour
    où l'on vide la pile — la seule chose pire que le silence. Arbitrer 1 signal ⟹ un mail
    d'une ligne ; en arbitrer 53 ⟹ un mail de 53 lignes. Un seul chemin, les deux régimes,
    et le rattrapage d'une pile tombe du regroupement sans mode à part.
  - **`preview` est le défaut et ne touche à rien.** Ces mails partent chez des tiers sous
    notre marque : l'envoi est un ACTE (`notify_send`), et `only` permet de sortir par
    paliers. Deux `op` plutôt qu'un booléen `send=` — envoyer chez des tiers ne doit pas
    tenir à un drapeau qu'on oublie de mettre à False.
  - **Un envoi raté reste dû** : on marque APRÈS l'envoi, jamais avant. L'inverse ferait
    disparaître le retour au premier hoquet du mailer, sans que personne le sache. Une
    adresse inconnue (compte supprimé) se VOIT dans la réponse au lieu de se perdre.
  - **Seuls les états terminaux font un retour** : « on l'a lu » n'est pas une réponse, et
    l'annoncer userait le canal avant d'avoir rien dit.
  - ⚠️ **Le mail dit « vos agents », jamais « vous ».** Ces retours sont écrits par des
    agents en session sous le compte de quelqu'un qui n'a le plus souvent jamais su qu'ils
    existaient ; « votre signalement » lui attribuerait des mots qu'il n'a pas écrits.
    Et un `declined` s'écrit **« non retenu »** : le rapporteur a rendu service, on veut
    qu'il signale encore.
  - ⚠️ La marque est celle du DESTINATAIRE (`config.front_for`) — écrire « oto » à
    l'utilisateur d'un partenaire est un faux, même quand tout le reste est juste.
  - ⚠️ **Lien de désinscription porté depuis le 15/09/2026 (oto#150).** Un compte
    rattaché à un tenant tiers reste une adresse légitime pour ce mail (déclaré dans
    les CGU, décision d'Alexis) ; ce qui manquait était le REFUS. Le pied du mail
    porte désormais un lien signé (`outreach_optout.lien_digest(sub)`, route
    `/o/d/<token>`) qui écrit dans `signal_digest_optouts` — table **distincte** de
    `outreach_optouts` (les relances de plateforme, `docs/relance-comptes.md`) : les
    deux canaux ont chacun leur refus, se désinscrire de l'un ne désinscrit pas de
    l'autre, même forme de jeton (HMAC scellant un `sub`), `typ` et route différents.
    `pending_signal_notices()` exclut EN AMONT qui s'en est servi ; le signal reste
    DÛ (jamais marqué notifié), donc lever le refus le fait réapparaître.
  - Les 331 signaux arbitrés AVANT ce lot sont marqués annoncés par la migration : leurs
    auteurs n'ont rien reçu, mais leur envoyer des nouvelles de décisions vieilles de deux
    mois n'aiderait personne. Le retour vaut pour ce qu'on arbitre à partir de là.

  ⚠️ **L'état se lit dans la colonne `status`, jamais dans `resolved_at IS NULL`.** Le trio
  `resolved_at/resolved_by/resolution` porte désormais le DERNIER ARBITRAGE, quel qu'il soit
  — un signal refusé porte lui aussi une date. Le dériver de la date rendrait un refus
  indistinguable d'un traitement, et le compteur mentirait dans le sens qui arrange.

  **Pourquoi ce lot** : deux états ne suffisaient pas, et la mesure du 27/08 le montre —
  534 signaux reçus depuis le 19/06, **203 ouverts dont 125 de plus d'une semaine**, et
  **zéro arbitrage depuis le 16/08** pendant que 118 arrivaient. Un stock où le refus est
  indicible ne peut que monter : on n'y distingue plus le retard du désaccord, donc on
  cesse de le lire — et la boucle d'usage devient muette tout en continuant d'enregistrer.
  (327 des 331 arbitrages passés sont d'une seule personne : la boucle s'arrête avec elle.)
- **Harnais impératif** : `_SERVER_INSTRUCTIONS` pousse l'agent à réflexer oto, encadrer
  par `run_start/finish` et émettre `feedback`.
- Déféré (otomata#32) : `why`-par-appel.

## Un run silencieux ne s'annonce plus « en cours » (13/08, #311)

> **Un run silencieux ne s'annonce plus « en cours » (13/08, #311 ; seuil re-daté le
> 31/08, #666).** 15 des 16 runs « ouverts » de prod n'avaient plus donné signe de vie
> depuis 1 jour à 1 mois : des conversations terminées sans clôture déclarée. Le silence
> est **dérivé à la lecture** (`run_status`, **24 h** sans appel rattaché) — jamais
> stocké : une colonne d'état écrite par un démon pourrait mentir à son tour, ce qui est
> le défaut qu'on ferme. `last_seen_at` remonte de `_runs_from_journal`, donc les 4
> lectures en héritent d'un coup.
> ⚠️ **La justification écrite à côté du seuil a été fausse pendant 18 jours** : elle
> affirmait qu'« aucun run silencieux » ne se trouvait entre 1 jour et 1 mois alors que
> le recensement qu'elle citait en comptait 15 sur 16. Recensé le 31/08 sur 10 755 runs :
> **143 des 148 ouverts** sont dans cette bande, le creux réel est **6-24 h** (vide), et
> le risque de couper du vivant est identique à 24 h et à 48 h (2 runs dans les deux cas).
> **Le seuil nomme la pathologie, il ne la soigne pas** : 18,3 % des runs conversationnels
> restent ouverts, contre 0,41 % côté runner hébergé.
> ⚠️ **Le vocabulaire d'issue vient de l'ADR, pas de la mesure** : `abandoned` est retiré
> (absent d'ADR 0058-D5) ; `failed` RESTE bien qu'il n'ait jamais servi non plus, parce que
> D5 le porte. **La mesure tranche ce que l'ADR laisse ouvert, jamais ce qu'elle a fermé.**

## Runs persistés (#50, amende le « state-only » d'ADR 0017)

> **Runs persistés (#50, amende le « state-only » d'ADR 0017).** La métadonnée
> sémantique d'un run (label / guide / outcome) vit désormais dans la table `runs`
> (`db.insert_run`/`finish_run`/`recent_runs`) — la pile session-scopée de
> `guide_run.py` reste la **source du run actif** (stampe `tool_calls.run_id`),
> `run_start`/`run_finish` y ajoutent la trace durable (best-effort, off-loop). Sert
> l'anticipation du contexte injecté (instructions bloc C) + la boucle d'usage dashboard.

- **Un responsable d'ORG lit le CORPS de ses signaux (27/08)** :
  `oto_org_monitoring(op='signals', org_id=…)` + `GET /api/orgs/{id}/monitoring/signals`,
  autz `ORG_ADMIN_OF`. Les lentilles `gaps`/`tool_quality` rendaient l'intitulé et le
  NOMBRE ; la cause est dans la prose, et elle n'était servie que par la capacité
  PLATEFORME. **Le manque a coûté cinq jours à cinq clients d'un revendeur** : leur
  ingestion quotidienne échouait chaque matin, les agents le signalaient fidèlement, et
  les responsables voyaient « 8 manques » sans jamais pouvoir savoir lesquels — alors
  que la prose disait « le projet de destination a été archivé le 21/08 », une cause
  qu'aucun compteur ne peut porter.
  - Scope = `usage_signals.org_id`, ce qui a été **ÉMIS SOUS** l'org — jamais
    l'appartenance du rapporteur. Un prestataire qui travaille pour trois clients ne
    verse pas ses retours dans les trois.
  - `resolved_by` est **retiré** : qui a tranché chez nous est notre conduite interne.
    La `resolution`, elle, descend — c'est la réponse qu'on lui doit.
  - ⚠️ Pas de vue TENANT : un revendeur consulte org par org. Elle se fera quand l'étage
    tenant (ADR 0052) portera le rattachement — la bâtir aujourd'hui sur `front_brand`
    reviendrait à s'appuyer sur une colonne qui doit remonter d'un cran.

## Journal du handshake MCP : ce qui a été SERVI (`handshake_log`)

Une ligne de log (`INFO`, logger `oto_mcp.handshake_log`) par `initialize` et par requête de
liste (`tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`) :

    mcp.handshake initialize client=<nom>/<version> protocol_requested=<v> protocol_negotiated=<v> host=<hôte|projet|inconnu>
    mcp.handshake list method=<méthode> count=<n> client=<nom>/<version> host=<hôte|projet|inconnu>

Pour savoir pourquoi un client ne reçoit « aucun outil » : `count=0` (ou un compte bas) est
ce que le serveur a servi, pas ce que le client en a fait. Hébergé dans `ToolAliasMiddleware`,
le plus externe de nos middlewares : son compte est **la liste finale** (visibilité de
session, alias dépréciés et préfixe de tenant compris). Aucun contenu — ni nom d'outil, ni
`sub`, ni e-mail, ni jeton — jamais que des `len()`. Pas de table, pas de variable d'env.

⚠️ **`host=` est une liste blanche.** Il n'est journalisé tel quel que s'il est **connu** : l'hôte
public de l'instance, `mcp.<domaine de projet>` (préprod comme prod), les audiences alt, ou un
hôte **déclaré** d'un tenant (`tenants.hosts`). Le sous-domaine d'un projet publié
(`<slug>.mcp.<D>`, `<slug>.share.<D>`) devient `host=projet`, tout autre hôte `host=inconnu` :
en mode `secret` ce sous-domaine est une URL-capacité (ADR 0032), le slug EST le secret, et les
logs applicatifs sont lisibles bien au-delà de ceux qui le connaissent. Le filtre porte sur la
valeur retenue (`x-forwarded-host` prime sur `host`, première valeur, port retiré) : aucun des
deux en-têtes ne le contourne. En cas de panne du classement : `host=inconnu`, jamais la valeur.
Complète `kind='protocol'` (cadence de re-handshake), qui ne porte ni protocole négocié ni hôte.
