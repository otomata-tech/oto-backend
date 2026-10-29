# Crédits comptés au moment de l'appel

Le chantier : compter ce qu'un appel consomme sur une clé partagée (plateforme ou tenant)
AU MOMENT de l'appel, dans la base du cœur, et non plus après coup en relisant le journal
`tool_calls`, qui est best-effort par conception (`docs/monitoring.md`). Doctrine
inchangée : **le commerce écrit, le cœur relit**. Le cœur ne tient ni prix en monnaie, ni
devise, ni facture, ni moyen de paiement (#1104) : seulement des unités.

Trois étapes :

1. **L8 — le règlement unique** (fait) : un seul point débite, au bord de l'appel.
2. **R6 — l'autorité du refus** (fait) : sous une arête `user` d'un connecteur chaîné
   (`grants_chain.CHAIN_CONNECTORS`), le refus lit le compteur d'arête (`grant_counters`).
   La fenêtre de double lecture est close là où la chaîne a un avis.
3. **Le budget d'unités** : un barème posé par le commerce, des mouvements idempotents
   versés à un payeur, une réservation au pire cas avant l'appel et un règlement au réel
   après. Il suppose les deux formes de `cost` du format des connecteurs (pire cas par
   élément de lot, quantité relue dans la réponse) et le délai de relève d'un travail
   asynchrone. Modes `off | report | enforce`, `report` par défaut.

## L8 : le règlement d'un appel (`access/reglement.py`)

Un outil ne débite plus rien lui-même. Trois rôles, trois endroits :

| qui | quoi | où |
| --- | --- | --- |
| l'outil | ce qu'il consomme : `units` au relevé d'appel | `session_org.note_call_trace(units=N)` |
| le résolveur | sous quelle clé l'appel part : `platform_carrier` (clé plateforme, hors connexion), `tenant_grant_ids` (arête de tenant) | `access/resolve.py`, `access/tenant_budget.py` |
| le bord | règle une fois, au SUCCÈS, sous le contexte de l'appel | `regler_appel` |

Les trois bords sont ceux qui posent un relevé d'appel : `middleware/call_context.py`
(appel MCP direct), `tools/meta.executer_cible` (cible d'`oto_call` et des recettes, avec
son propre relevé ; l'enveloppe ne reçoit jamais `CLES_REGLEMENT`) et `jev_jobs_worker.py`
(une tranche de fond). Une garde de test interdit tout autre appelant du débit.

- **`units` n'est pas `quantity`.** `quantity` est l'axe de facturation (les crédits du
  fournisseur, posés aussi sous la clé propre du client) ; `units` est l'axe du quota de la
  clé partagée. Les deux diffèrent volontairement sur plusieurs outils (le match de
  personne Apollo : quota 1, quantité = crédits ; la recherche AI Ark : quota 1,
  quantité = lignes rendues).
- **Débit constant.** L8 débite ce qui était débité avant, au même montant (un plancher à 1
  là où l'ancien compteur en avait un). Les outils qui ne débitaient rien sont déclarés
  dans `SANS_DEBIT` : passer l'un d'eux au débit est une décision, avec ses `units`.
- **Succès seulement.** Un appel qui lève ne règle rien.
- **Rien en silence.** Une clé plateforme servie sans `units`, par un outil absent de
  `SANS_DEBIT`, est journalisée (`units_absentes`, avec l'outil et le connecteur) et rien
  n'est débité. `tests/test_reglement_appel.py` tient chaque outil d'un connecteur à clé
  plateforme dans l'un des deux cas.
- **Un règlement ne défait pas un appel servi.** Un débit qui échoue est journalisé avec ce
  qu'il faut pour rattraper le compte à la main.
- **L'arête de tenant** est vérifiée à la résolution et débitée au succès, au même point.
  Hors relevé d'appel (face REST, maintenance), il n'y a pas de bord : elle est débitée à
  la résolution, comme avant.

## R6 : le compte que lit le refus (`quotas.platform_used_today`)

| l'arête qui sert | le compte du refus |
| --- | --- |
| `user` (connecteur chaîné) | son compteur d'arête, arêtes archivées comprises |
| `org` ou `group` | `usage(sub, tool, day)`, par personne |
| aucune (chaîne muette, connecteur non chaîné) | `usage(sub, tool, day)`, par personne |

Le refus, la sonde (`platform_quota_hint`), le mode affiché et l'instantané `/api/me`
lisent ce même nombre. Là où la chaîne accorde ou refuse, l'ancien chemin n'est plus lu,
et le journal d'écart (accès, quota, compteur) disparaît avec lui. Le compteur d'une
arête d'org continue d'être débité : c'est la consommation de l'org entière, lue par
l'administration des arêtes.

## Le budget d'unités : le port du commerce (`capabilities/service_credits.py`)

Le commerce écrit et relit par l'API de service (`/api/service/…`, identité commerce,
REST seule) ; le stockage est `db/credits.py`, sur les tables de `db/schema/credits.py`
(révision `0068_credits`).

| route | ce qu'elle fait |
| --- | --- |
| `PUT /api/service/credits/scales/{version}` | pose une version du barème, d'un bloc ; une version ne se réécrit pas (409 `scale_version_conflict`), la plus haute fait foi |
| `GET /api/service/credits/scales/current` | la version qui fait foi et ses lignes |
| `PUT /api/service/orgs/{id}/credits/payer-mode` | qui paie dans l'org : `org` (défaut) ou `person` |
| `POST /api/service/credits/movements` | verse un montant signé à un payeur, une fois par `(payeur, idempotency_key)` ; même clé, autre contenu : 409 `idempotency_conflict` |
| `GET /api/service/credits/balance?payer=` | `balance` (versé − réglé), `holds_open` (réservé, pas encore réglé), `available` |
| `GET /api/service/credits/movements?after=&limit=` | les mouvements de tous les payeurs, 500 par défaut, 1 000 au plus ; `as_of` et `settled_as_of` sur chaque page |

- **Une ligne de barème** : `(connector, function, unit)` → `budget` unités par paquet de
  `per_n` unités de la fonction. Réservation et règlement s'arrondissent au paquet
  supérieur.
- **Un payeur** : `org:<id>` ou `user:<sub>`, qui doit exister.
- **Les clés du barème que le cœur lit aujourd'hui**, `(connector, function, unit)`, en
  texte exact. Une clé absente rend la fonction `credits_unpriced` :

  | connector | function | unit | ce qu'une unité compte |
  | --- | --- | --- | --- |
  | `fullenrich` | `submit` | `work_emails` | un contact pour qui un e-mail pro est TROUVÉ |
  | `fullenrich` | `submit` | `personal_emails` | un contact pour qui un e-mail perso est trouvé |
  | `fullenrich` | `submit` | `phones` | un contact pour qui un téléphone est trouvé |
  | `anthropic` | `messages:<modèle>` | `input_tokens` | un jeton d'entrée hors cache |
  | `anthropic` | `messages:<modèle>` | `output_tokens` | un jeton de sortie |
  | `anthropic` | `messages:<modèle>` | `cache_creation_input_tokens` | un jeton écrit au cache (5 min et 1 h ensemble) |
  | `anthropic` | `messages:<modèle>` | `cache_read_input_tokens` | un jeton lu au cache |

  `<modèle>` est l'identifiant SANS date de version du catalogue des agents hébergés
  (`runner_models`) : aujourd'hui `claude-sonnet-5`, `claude-opus-5`, `claude-haiku-4-5`,
  soit douze lignes `anthropic`. Un identifiant daté (`…-20261001`) se lit sans sa date :
  un instantané garde le prix de son modèle, un modèle nouveau est un identifiant
  nouveau, donc des lignes nouvelles.
- **Le curseur** est opaque (`<xid>.<id>`). Un mouvement porte l'identifiant de la
  transaction qui l'a écrit (`xid8`), et la route ne sert que ce qui est sous l'horizon
  `pg_snapshot_xmin(pg_current_snapshot())`, trié par `(xid, id)` : une transaction
  encore ouverte retient tout ce qui suit, et rien ne peut plus être validé en dessous
  d'un curseur servi. Relu jusqu'à `next_cursor == after`, il n'en manque aucun.
  L'horizon est celui de TOUT le serveur PostgreSQL : une transaction longue, dans
  n'importe quelle base de l'instance, retarde le curseur — un retard, jamais une perte.
- **`as_of`** (#1290), sur chaque page, vide comprise : l'instant sous lequel aucun
  mouvement ne peut plus apparaître. Tout mouvement qui n'est pas DÉJÀ sous l'horizon
  du curseur sera daté (`created_at`) d'au moins `as_of` ; une période qui s'achève à
  `as_of` ou avant est donc complète une fois le flux lu jusqu'au bout (une page plus
  courte que `limit`). Une page pleine laisse derrière elle des mouvements déjà
  visibles, plus anciens que `as_of` : il faut lire la suite.
  - **Le calcul** (`db/credits.page_de_mouvements`) : le plus ancien de trois instants.
    (1) L'instant de lecture, moins une seconde. (2) Le début de toute transaction
    ouverte de la base courante dont le rôle peut insérer dans `credit_movements` ou
    dans `credit_holds` (pour `settled_as_of`, ci-dessous), lu dans
    `pg_stat_activity` AVANT l'instantané de la page. `created_at` vaut `NOW()`,
    le DÉBUT de la transaction qui écrit : une transaction qui n'a encore que lu (sans
    xid, donc invisible au curseur) peut valider plus tard un mouvement daté de son
    début — elle compte, xid ou non. (3) Le plus ancien mouvement déjà validé que
    l'horizon retient encore (`xid >= pg_snapshot_xmin`), lu DANS l'instantané de la
    page.
  - **Pourquoi cet ordre** : lue après l'instantané, `pg_stat_activity` raterait une
    transaction validée entre les deux, invisible à la page et retenue par l'horizon.
    Lue avant, toute transaction en cours à l'instantané y figure déjà, ou a commencé
    après la lecture. La seconde de marge couvre la fenêtre où une transaction qui
    démarre a déjà pris son horodatage sans l'avoir publié.
  - **Les autres bases** ne peuvent pas écrire ici : elles ne retiennent `as_of` qu'à
    travers le curseur, au plus ancien mouvement de cette base qu'il retient. Une
    transaction qui ne fait que lire, ailleurs, ne retient rien (sans xid, elle ne
    retient même pas l'horizon).
  - **Ce que cela coûte** : une transaction ouverte de la base courante, même de
    lecture, même inactive (`idle in transaction`), retient `as_of` à son début tant
    qu'elle court — celles du pool sont coupées à 60 s
    (`idle_in_transaction_session_timeout`), une session manuelle n'a pas de borne. Le
    rôle d'une session qui ne peut pas insérer dans la table ne retient rien. Deux
    requêtes par page au lieu d'une ; `pg_stat_activity` se lit sans verrou.
  - **Monotone**, quel que soit le curseur et l'appelant : chaque terme n'avance
    jamais d'une lecture à l'autre (une transaction absente de la lecture précédente a
    commencé après elle, un mouvement retenu maintenant l'était déjà ou vient d'une
    transaction qui courait alors).
  - **`null`** quand une transaction de la base a un début illisible : une session
    d'un autre rôle quand le rôle du cœur n'a pas `pg_read_all_stats` (elle n'expose
    alors ni son état ni son début), une session au suivi coupé (`disabled`), ou une
    transaction préparée. Une session lisible qui démarre (`state` encore NULL, une
    connexion qu'un pool ouvre) n'en est pas : elle est bornée par son `backend_start`,
    qu'aucune de ses transactions ne précède. Le journal nomme la raison. Le cœur ne promet
    rien plutôt qu'un horizon faux, et le journalise. **Prérequis de production** : le
    rôle applicatif porte `pg_read_all_stats` (ou `pg_monitor`).
- **`settled_as_of`**, sur chaque page, vide comprise : l'instant sous lequel tout appel
  est réglé ou libéré. `as_of` dit qu'aucun MOUVEMENT daté d'avant ne peut plus
  apparaître ; mais une réservation encore ouverte n'a pas de mouvement — une soumission
  asynchrone attend sa relève, ou son échéance de 24 h (`OTO_CREDITS_HOLD_MAX_S`) — et
  son règlement, à venir, sera daté de plus tard. Juger réglés les appels d'une
  période se fait donc sur `settled_as_of`, pas sur `as_of`, sans connaître l'échéance.
  - **Le calcul** : le plus ancien de `as_of` et du début (`created_at`) de la plus
    ancienne réservation — tous payeurs confondus, ou ceux du seul `payer` passé en
    paramètre — encore OUVERTE ou réglée par un
    mouvement `settle` que l'horizon du curseur retient encore (`xid >=
    pg_snapshot_xmin`) — lu dans le MÊME instantané que la page et le terme (3)
    d'`as_of`. Toute réservation posée avant est close (réglée, libérée ou échue), et
    son mouvement, s'il en a un, est servi par le flux. Une échéance est un mouvement
    `settle` ; une libération n'en écrit aucun, rien ne reste à servir. Une transaction
    longue du serveur, qui retient le curseur, retient donc aussi `settled_as_of` au
    début des réservations qu'elle empêche de servir.
  - **Ce que cela coûte** : deux lectures bornées, l'une par l'index partiel des
    réservations ouvertes, l'autre par l'index du curseur (les seuls mouvements
    retenus), puis la clé de leur réservation.
  - **Monotone**, comme `as_of`. Une réservation close ne se rouvre pas, et un
    règlement retenu à une lecture l'était déjà à la précédente s'il était visible
    (l'horizon ne recule pas) ; une réservation retenue à une lecture et invisible à la
    précédente vient d'une transaction qui n'était pas validée à l'instantané
    précédent : elle courait à la
    lecture de `pg_stat_activity` d'alors, qui l'a comptée à son début — c'est-à-dire à
    son `created_at` (`NOW()`, le début de la transaction) —, ou elle a commencé après.
    D'où le terme (2), qui compte aussi les rôles qui peuvent insérer dans
    `credit_holds`.
  - **Par payeur** (`?payer=org:<id>`) : le même calcul sur les seules réservations de
    ce payeur, et la même preuve de monotonie, restreinte à lui. Le relevé d'un payeur
    n'attend plus l'appel asynchrone d'un autre (24 h au pire) ; la page et `as_of`
    restent ceux du flux entier.
  - **`null`** quand `as_of` l'est.
- Le curseur porte les règlements (échéances comprises), les versements et les transferts.
  Les réservations en cours se lisent dans le solde (`holds_open`).
- **Rapprocher un règlement de ce qui a consommé** : le `call_uid` d'un mouvement `settle`
  est la clé de sa réservation, et sa `meta` le redit :

  | champ de `meta` | quand | ce que c'est |
  | --- | --- | --- |
  | `call_id` | tout appel d'outil | le `tool_calls.call_uid` de l'appel (la cible elle-même pour `oto_call` et une recette, sa tranche pour un travail de fond) ; égal au `call_uid` du mouvement |
  | `key_mode` | tout règlement, sauf l'échéance d'une réservation SYNCHRONE | le rang de la clé refacturée : `platform` ou `tenant` |
  | `job_id` | un travail asynchrone (FullEnrich : l'`enrichment_id`) | l'identifiant du travail chez le fournisseur, gardé avec la relève |
  | `runner_job_id`, `attempt`, `turn` | un tour d'agent hébergé | le travail, la tentative, le tour ; la clé du mouvement est `runner:<travail>:<tentative>:<tour>`, sans ligne `tool_calls` |
  | `from`, `exceeded`, `hold_expired`, `units`, `tokens`, `model`, `scale_version` | comme avant | d'où vient le montant, et sous quelle version |

  L'échéance d'une réservation synchrone ne dit pas `key_mode` : rien ne le garde sans
  colonne, et une réservation synchrone n'a pas de `job` (il entrerait dans l'index de
  la relève). Son `call_id` reste lu dans sa clé.

  Côté appels, la lentille du relevé `org.usage.calls` rend `call_uid` à côté de `call_id`
  (l'`id` entier de la ligne) : c'est le `call_id` de la `meta` des mouvements, la clé par
  laquelle le commerce apparie un mouvement à l'appel qui l'a consommé.
- **Un règlement réel après l'échéance** : une réservation échue se règle au montant
  réservé ; le règlement réel qui arrive ensuite est seulement journalisé
  (`credits_hold_expired_before_settle`), sans remboursement. Au commerce de verser un
  ajustement s'il le veut.
- **Deux comptes d'une même personne fusionnés** : le solde `user:<ancien>` passe à
  `user:<nouveau>` par deux mouvements `transfer` (`users.repointer_patrimoine`) ; le
  journal ne se réécrit pas.
- **Rétention** : tout est gardé.

## Le budget d'unités : réserver, régler (`access/credits.py`)

**Le périmètre est déclaré**, à l'enregistrement de l'outil : `credits.declarer(outil,
connector, function, arguments)` nomme la fonction de la bibliothèque des connecteurs
dont l'outil paie le coût déclaré (`oto.connectors.declared`), et comment ses arguments
se lisent comme ceux de la fonction. Une fonction sans coût déclaré lève à
l'enregistrement. Seul un appel servi par une clé REFACTURÉE réserve
(`credits.rang_refacture`, le même prédicat au résolveur et au runner) : celle de la
plateforme, ou celle du tenant PRIMAIRE de l'instance (`tenancy.primary_slug()`) — toutes
deux payées par l'opérateur. Un tenant non primaire paie sa propre clé, comme une org la
sienne : jamais de réservation dessus. Sur la clé du tenant primaire, l'arête
tenant→org (son quota) est vérifiée d'abord : son refus ne laisse aucune réservation. Un outil du périmètre dit quand il ENVOIE la requête facturée
(`credits.envoi()`) et ce qu'il a reçu (`credits.reponses([...])`) — la garde
`test_credits_appel.py` l'exige.

| qui | quoi | où |
| --- | --- | --- |
| le bord | ouvre l'appel (outil, arguments ; le `call_uid` d'une cible) au relevé | `credits.ouvrir_appel`, aux trois bords de L8 |
| le résolveur | réserve, sur une clé refacturée, après toutes les autres gardes, sous le `call_uid` de l'appel | `credits.reserver` dans `resolve.py` |
| l'outil | dit l'envoi, puis les réponses | `credits.envoi`, `credits.reponses` |
| le bord | règle la réussite, ou l'échec | `regler_appel`, `regler_echec` |

- **Réserver** : `cost.worst_case` × la ligne du barème, au paquet `per_n` supérieur,
  sous la version du barème qui fait foi. Une fonction **tarifée par unité trouvée**
  (asynchrone seulement : `credits.declarer(..., unites=…)`) réserve, unité par unité, son
  pire cas lu dans les arguments de l'outil, et la somme des paquets ; sa réservation
  note ses unités jointes par `+` (`work_emails+phones`). Qu'une ligne manque, et
  l'appel est `credits_unpriced`. **Échéance** d'un appel synchrone :
  max(`OTO_BORNE_OUTIL_S`, `timeout_s` déclaré) + `OTO_CREDITS_SYNC_MARGIN_S` — la borne
  d'un appel d'outil (#1232) est lue quel que soit `OTO_DEBIT_MODE` : mesurée ou
  appliquée, c'est la durée qu'un appel est censé tenir. Borne désarmée (`0`) et sans
  `timeout_s` : `OTO_CREDITS_HOLD_MAX_S`, et le démarrage le dit
  (`credits.verifier_reglages`). Une soumission asynchrone et une tranche de fond
  (`jev_rows`, hors de la borne MCP) : `OTO_CREDITS_HOLD_MAX_S` (24 h).
  Une transaction de 2 écritures ; le barème et les modes de payeur sont relus au plus
  toutes les 30 s.
- **La clé de la réservation** est le `tool_calls.call_uid` de l'appel, lu au geste à la
  réservation : le bord MCP direct (`call_context`) est plus externe que le journal
  d'appels qui frappe ce `call_uid`, et ne l'a pas encore ; l'outil, lui, tourne dessous.
  Une tranche de fond a le sien (son geste). Une cible d'`oto_call` ou d'une recette
  frappe le sien (`executer_cible`) et le pose sur sa ligne : sous le geste de
  l'enveloppe, une recette joue plusieurs cibles, chacune sa réservation. Sans geste, ou
  sous un `call_uid` qui tient déjà une réservation, rien n'est posé
  (`spend_ledger_unavailable`, journalisé) : jamais un identifiant inventé.
- **Qui paie** : `org:<id>` (l'org des clés de l'appel), `user:<sub>` si l'org est en
  `payer_mode=person` ou s'il n'y a pas d'org.
- **Régler la réussite** : `cost.settled_quantity` sur les réponses dites (relue dans la
  réponse, requêtes envoyées, ou pire cas par élément), au paquet supérieur ; le surplus
  réservé revient au solde. Au-delà du pire cas : réglé tel quel, `exceeded: true`, et
  le solde peut passer sous zéro — jamais à la réservation. Un outil qui n'a pas dit ses
  réponses est réglé au pire cas (`from: unreported`, journalisé). Une soumission
  ASYNCHRONE ne se règle pas : la réservation attend sa relève (`cost.collection_of`),
  échéance `within_s` ou `OTO_CREDITS_HOLD_MAX_S`.
- **Régler l'échec** : un refus NET libère — levé avant l'envoi, ou un 4xx du
  fournisseur autre que 408 et 429 (`error_taxonomy.upstream_status_in_chain`) ; tout
  autre échec se règle au montant réservé (`from: failure`).
- **Le quota de L8 ne change pas** : il compte les réussites, à côté.
- **Le relevé du jour** : le palmarès des limiteurs (`limite_refus_top`, table
  `limite_refus_jour`, lu par `oto_admin_monitoring op=transport`, champ
  `rate_limit_top`), sous le limiteur `credits`, clé = empreinte du payeur. Deux classes,
  qui sont des SIGNAUX : `unpriced:<connector>.<function>` — une fonction du périmètre
  absente du barème (refusée en `enforce`, servie sans réservation en `report`) ;
  `unreported:<outil>` — un outil du périmètre réglé au pire cas faute d'avoir dit ses
  réponses (jamais un refus). Pas de table neuve.

**Modes** (`OTO_CREDITS_MODE`) :

| mode | réserve et règle | refuse |
| --- | --- | --- |
| `report` (défaut) | oui ; la réservation est posée même au-delà du disponible | jamais : journalise ce qu'`enforce` refuserait |
| `enforce` | oui | `insufficient_credits` (non rejouable), `credits_unpriced` (fonction sans ligne au barème, non rejouable), `spend_ledger_unavailable` (registre illisible ou non écrit, rejouable) |
| `off` | non | jamais |

**Un mode par fonction** (`OTO_CREDITS_MODE_FUNCTIONS`, #1376 E) : une fonction du barème
peut tourner dans son propre mode, qui l'emporte sur `OTO_CREDITS_MODE` pour elle
(`credits_modes.mode_de`). Liste `<connecteur>.<fonction>=<mode>` séparée par des virgules ; un
`*` final nomme toute fonction qui commence ainsi ; un nom exact l'emporte toujours sur un
motif (l'étoile ne compte pas), et entre motifs le préfixe le plus long. Le cas pour
lequel il existe : `anthropic.messages:*=enforce` — les appels au modèle d'un agent
hébergé ou de l'agent intégré refusent (`credits_tours.mode_du_modele`), pendant que les
outils du périmètre restent en `report`. Illisible, nommée deux fois, ou qui ne nomme aucune
fonction que le barème sait tarifer — coût déclaré d'un connecteur (`oto.connectors.declared`),
ou `anthropic.messages:<modèle>` du catalogue des agents hébergés (`runner_models`) — :
refuse le démarrage (`credits_modes.verifier`), jamais un retour silencieux au mode de
l'instance. La garde de la borne d'outil désarmée (`OTO_BORNE_OUTIL_S=0`) se lit dès
qu'UNE fonction réserve, même sur une instance `off`.

**Le plancher d'un travail** (`OTO_CREDITS_JOB_FLOOR`, #1376 B) : avant qu'un travail
d'agent hébergé parte sur une clé refacturée, son payeur doit avoir ce disponible, en
unités (solde − réservations ouvertes, `credits_tours.sous_le_plancher`) — l'entrée du
premier tour ne se réserve pas, et un travail ne doit pas partir pour mourir au premier
appel. Lu à la réservation du travail, APRÈS les gardes ferme et mesure, AVANT
l'estampille (`capabilities/runner_jobs._avec_cle`) : en `enforce` (mode de la fonction du
modèle), le travail s'arrête, raison écrite `insufficient_credits` — « l'agent repartira à sa
prochaine échéance » pour l'occurrence d'un agent à horloge allumé, « relance ce travail »
sinon (`runner_jobs._agent_a_horloge`) ; en `report`, journalisé. Un solde illisible, en
`enforce`, n'est pas un solde insuffisant : `spend_ledger_unavailable`, rejouable — le travail
est rendu à la file sans compter la tentative, ni servi ni arrêté. Absent ou 0 : aucun
plancher. Un dépassement d'un tour sous zéro reste admis (l'entrée et le cache se règlent `exceeded: true`), visible au
grand livre.

Le TEXTE de `insufficient_credits` ne porte aucun montant : l'unité de budget est celle de
l'instance, et un nombre nu tromperait le lecteur. Le pire cas réservé et le disponible
restent dans les données du refus (`needed`, `available`), en unités de budget, à traduire
par l'instance.

Un réglage illisible refuse le démarrage (`credits.verifier_reglages`). Un règlement qui
échoue ne défait jamais l'appel : la réservation reste ouverte, et son échéance la règle.

**Un travail asynchrone** : la soumission réussie laisse sa réservation ouverte, avec sa
relève (`credit_holds.job` : la fonction de relève, ses arguments, et la `meta`
d'appariement — `call_id`, `key_mode`, `job_id` — que tout règlement de la réservation, la
relève comme l'échéance, verse au mouvement). L'outil
de relève, quand le travail est fini, règle la réservation au réel, unité par unité
(`credits_releve.regler_releve`, sous les lignes du barème de la réservation ; une unité
lue que la réservation n'a pas prise n'est pas réglée : elle n'était pas demandée) ;
quand le fournisseur dit qu'aucun résultat ne viendra, il la libère
(`credits_releve.liberer_releve`). Un outil dont le client refuse la requête AVANT de l'envoyer,
alors que l'envoi était annoncé, le dit (`credits.rien_envoye`) : l'échec libère.

**Le périmètre aujourd'hui** :

| outil | fonction | pire cas | réglé à |
| --- | --- | --- | --- |
| `fullenrich_enrich_linkedin` | `fullenrich.submit`, par nature | par nature demandée (`enrich_fields`, ou le défaut du client) : tous les contacts | la relève `fullenrich_result` finie : par nature, les contacts où elle est TROUVÉE (`found_work_emails`, `found_personal_emails`, `found_phones`) ; annulée : libérée ; refusée net (400, 422) : libérée |

FullEnrich se tarife par nature trouvée, pas aux crédits qu'il décompte lui-même (1, 3,
10) : un prix par résultat n'en est pas un multiple. La bibliothèque des connecteurs
garde sa déclaration (`fullenrich.submit`, en crédits FullEnrich, et sa relève) ; le
budget n'en lit que la relève. Les crédits de FullEnrich restent tracés (`quantity`
de la relève) pour le rapprochement.

**Les appels au modèle d'un agent hébergé, sur une clé REFACTURÉE** (plateforme ou
tenant PRIMAIRE, `credits.rang_refacture` ; `capabilities/runner_jobs.py`. Depuis le
20/10/2026, la clé d'un tenant non primaire n'est plus refacturée : elle est servie sans
mesure, à tout worker, comme la clé de l'org) : pas un outil, le worker du runner appelle le
modèle lui-même. Chaque appel est un tour (`runner:<travail>:<tentative>:<tour>`) :
`op=reserve` réserve son `max_tokens` de sortie (`credits_tours.reserver_tour`), `op=meter`
le règle à ses quatre comptes de jetons (`credits_tours.regler_tour`), la conclusion règle au
montant réservé les tours jamais rapportés (`credits_tours.clore_les_tours`). **Le modèle
est dans la clé** : le barème porte, PAR MODÈLE, quatre lignes `anthropic` /
`messages:<modèle>`, une par unité (`input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`). Le modèle est celui que nomme
la charge du travail (`payload.model`, lu par le cœur : le worker n'a pas à le redire) ;
le règlement suit la fonction de la réservation. Qu'une ligne manque, ou que le travail
ne nomme aucun modèle, et l'appel est `credits_unpriced` (au relevé du jour sous
`unpriced:anthropic.messages:<modèle>`). L'entrée et le cache ne se réservent pas (aucun
comptage avant l'appel) : ils se règlent `exceeded: true`. Échéance :
`OTO_CREDITS_HOLD_MAX_S` (le runner n'est pas sous la borne d'un appel d'outil MCP).
Le rapprochement avec le relevé d'Anthropic se fait en jetons, au total et par modèle
(`oto_admin_model_spend`, `by_model`).

**Les appels au modèle de l'agent intégré** (`capabilities/copilot/cle.py`, `docs/copilot.md`) : le même chemin que les tours d'un agent hébergé (`reserver_tour` avant, `regler_tour` après), clé `copilot:<conversation>:<rang>` (lue `call_id` au rapprochement). Deux différences : une réservation que le registre n'a pas tenue hors du mode `off` (barème sans les lignes du modèle en `report`) REFUSE l'appel au lieu de le servir ; une erreur HTTP du fournisseur LIBÈRE la réservation (rien n'a été consommé).

**Les échéances** (`credits_echeances.py`, boucle de fond, `OTO_CREDITS_ECHEANCES_ENABLED`) :
toutes les 60 s, les réservations ouvertes passées leur échéance se règlent au montant
réservé, état `expired`, mouvement marqué `hold_expired: true` — un processus arrêté en
plein appel, un travail asynchrone dont personne n'a relevé le résultat. La boucle
n'écrit qu'en base et tourne dans tous les environnements : deux processus sur la même
échéance, un seul la règle.

## Limites assumées

- **Sous une arête d'org ou de groupe, le refus reste lu dans `usage`**, compté par
  personne, comme avant : un compteur d'arête compte pour tous ses bénéficiaires, et le
  plafond d'une arête d'org est un plafond par personne. `grant_counters` n'a pas de
  colonne de bénéficiaire (`sub`) et n'en reçoit pas : le pot commun des crédits (étape 3)
  remplacera ce compte.

## Écarts connus, laissés pour l'étape 3

- **tavily** porte un plafond quotidien (100) que rien ne débite : ses outils sont dans
  `SANS_DEBIT`, le plafond n'arrête donc jamais rien. À trancher avec le barème.
- **Les comptes de `cognism_entitlement`, `cognism_filter_values` et
  `linkedin_aiark_credits`** : des lectures gratuites chez le fournisseur, qui débitent 1
  comme avant parce qu'elles passent par le même `_run`.
- **Une page vide de TheirStack** débite 1 sur le quota, alors que le fournisseur ne
  facture rien : l'ancien compteur plafonnait tout débit à 1 au minimum. Le montant est
  gardé tel quel.
- **Un outil composite** qui résout deux clés plateforme n'est réglé que sur la dernière
  (`platform_carrier` est une clé du relevé, pas une liste).
