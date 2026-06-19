# ADR 0018 — Procédures (répétables) et Exécutions (instances) au centre d'oto

- **Statut** : Proposé (draft)
- **Date** : 2026-06-19
- **Décideur** : Alexis
- **Repo canonique** : `otomata-tech/oto/docs/adr/` (à recopier/renuméroter là-bas,
  à côté de 0009/0012/0017 — rédigé d'abord dans `oto-backend` faute d'accès au
  meta-repo dans la session de travail).
- **Relations** : précise et étend **ADR 0017** (déroulés + feedback volontaire) ;
  s'appuie sur **ADR 0006** (harnais vs substrat), **ADR 0009** (couche capacité),
  **ADR 0012** (groupes & hiérarchie de droits), **ADR 0016** (datastore : modèle
  de partage DB-only de référence).

## Contexte

Le déclencheur est un cas concret (mission « enrichissement de leads » avec un
partenaire externe, Julien) qui a révélé deux manques et une confusion de
vocabulaire :

1. **On ne peut pas partager / transférer une trace d'exécution.** Les déroulés
   ADR 0017 (`doctrine_start`/`doctrine_finish`, `tool_calls.run_id`) existent,
   mais :
   - les projections de lecture (`usage.runs`, `usage.run` dans
     `oto_mcp/capabilities/usage.py:131-134`) sont gardées **`PLATFORM_ADMIN`** ;
     un membre d'org ne voit **jamais** un run, même de sa propre org ;
   - un run **n'a pas d'`org_id`** : `tool_calls` ne porte que `sub`, `run_id`,
     `session_id` (cf. `db.py`, schéma `tool_calls`). `db.list_doctrine_runs` /
     `db.get_doctrine_run` ne filtrent par aucun scope ; ils s'appuient
     entièrement sur l'autz `PLATFORM_ADMIN` ;
   - aucun mécanisme de **partage** ni de **transfert** d'un run (contrairement au
     datastore et son `data_share` / `datastore_shares`, ADR 0016).
   - Asymétrie révélatrice : **`usage_signals` porte déjà `org_id`**
     (`usage.py:57,67` passe `org_id` à `db.insert_usage_signal`) ; seuls les
     **runs** sont restés hors-org.

2. **Une « doctrine » est forcément répétable, or on veut parfois tracer un
   one-shot.** Aujourd'hui un déroulé **doit référencer un `slug` existant**
   (`doctrine_start(slug)`, `tools/doctrine_run.py:23`). Impossible de « lancer
   et tracer » une chose qu'on fait **une seule fois** sans d'abord l'inscrire
   dans la bibliothèque (`org_instructions`) — donc polluer l'index
   (`oto_get_doctrine` sans slug) avec des entrées jetables.

3. **Chargement cross-org.** `oto_get_doctrine` / `oto_list_doctrines` sont
   strictement scopés à l'**org active** (`tools/orgs.py:46-55`,
   `_resolve_org_read` : `org_id` explicite réservé au platform admin). Il n'existe
   pas de vue « toutes mes orgs ». Pour exécuter une procédure qui vit dans l'org
   d'un partenaire, il faut **basculer l'org active** (`oto_use_org`).

### Confusion de vocabulaire à lever

« Doctrine » mélange deux choses : (a) un **template documenté réutilisable**
(workflow validé, skill) et (b) le **fait de l'exécuter une fois** (le déroulé,
la trace). On introduit un vocabulaire explicite :

| Terme | Définition | Support actuel |
|---|---|---|
| **Doctrine de base** | prose opératoire d'org servie d'office en début de session | `org_instructions[BASE_SLUG]` — **inchangé** |
| **Procédure** (répétable) | template nommé, versionné, dans la bibliothèque/index | `org_instructions[slug]` — **inchangé**, juste renommé conceptuellement |
| **Exécution / run** (instance) | « on l'a fait une fois » — une trace datée, avec un résultat, rattachée à une org | aujourd'hui dérivé de `tool_calls`, **non first-class** → c'est ce que cet ADR change |

Une **procédure** est un cas particulier d'intention réutilisable ; une
**exécution** peut référencer une procédure (répétable) **ou** être **ad hoc**
(one-shot, non inscrite dans la bibliothèque). La primitive centrale d'oto
devient « **démarrer une exécution** », répétable ou unique.

## Décision

### D1 — L'exécution (run) devient un objet de premier ordre, scopé à l'org

Nouvelle table `procedure_runs` (palier org, `db._SCHEMA`) :

```
procedure_runs(
  run_id        TEXT PRIMARY KEY,      -- mint par doctrine_run.new_run_id()
  org_id        INTEGER,               -- org active au démarrage (NULL = perso/sans org)
  sub           TEXT NOT NULL,         -- acteur
  slug          TEXT,                  -- procédure répétable référencée, ou NULL si ad hoc
  title         TEXT,                  -- libellé (obligatoire si ad hoc)
  intent_md     TEXT,                  -- snapshot de l'intention (one-shot : ce qu'on voulait faire)
  outcome       TEXT,                  -- done|abandoned|failed|blocked (NULL tant qu'ouvert)
  note          TEXT,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at   TIMESTAMPTZ
)
```

La **timeline** reste portée par `tool_calls.run_id` (inchangé : un appel
attribué au run par `server._calllog_sink`). `procedure_runs` est l'**en-tête**
durable du déroulé ; `tool_calls` en est le détail (et reste soumis au prune 30j —
acceptable : l'en-tête + l'`intent_md` + l'`outcome` survivent ; cf. Conséquences).

`org_id` est **gelé au `doctrine_start`** (org active à cet instant), pas relu
dynamiquement — un run est l'archive d'un contexte donné.

### D2 — `doctrine_start` accepte une procédure répétable OU une exécution ad hoc

```
doctrine_start(slug=None, title=None, intent=None) -> {run_id, slug|None}
```

- `slug` fourni → run rattaché à la procédure répétable (comportement actuel) ;
- `slug` omis + `title` (et idéalement `intent`) → **exécution unique** : on écrit
  une ligne `procedure_runs` avec `slug=NULL`, **sans rien écrire dans
  `org_instructions`** → n'apparaît **pas** dans l'index `oto_get_doctrine()` /
  `oto_list_doctrines`. C'est « juste une trace qu'on a faite une fois ».

`doctrine_finish(run_id, outcome, note=None)` écrit `outcome`/`finished_at`/`note`
sur la ligne `procedure_runs` (en plus de dépiler le run de l'état de session,
inchangé).

> Nommage des outils : on conserve `doctrine_start`/`doctrine_finish` pour ne pas
> casser le harnais `_SERVER_INSTRUCTIONS` ni les docstrings (contrat LLM), mais
> leurs docstrings sont réécrites en vocabulaire « procédure / exécution ». Un
> alias `procedure_start`/`procedure_finish` est **différé** (cosmétique).

### D3 — Surface de lecture des exécutions, scopée org et ouverte aux membres

Nouvelles capacités (`capabilities/` — ADR 0009, montage auto MCP+REST) :

- `runs.list` — **`ORG_MEMBER`** — runs de l'**org active** (en-tête + n_calls +
  outcome). MCP `oto_list_runs` + REST `GET /api/me/runs`.
- `runs.get` — **`ORG_MEMBER`** (du run, via `org_id` de la ligne) — timeline
  complète d'un run de **mon org**. MCP `oto_get_run` + REST `GET /api/me/runs/{run_id}`.

Les projections **`/api/admin/usage/runs`** existantes restent en `PLATFORM_ADMIN`
(supervision plateforme transverse, non scopée). On **n'élargit pas** leur autz ;
on **ajoute** la face membre scopée. Ainsi Julien, membre de l'org du projet, voit
les exécutions **de cette org** sans devenir opérateur plateforme.

### D4 — Transfert d'une procédure et de ses exécutions vers une autre org

- **Procédure** (template) : déjà faisable — `oto_set_doctrine` dans l'org cible
  (org_admin de la cible, ou platform admin). Pas de nouveau code.
- **Exécution** : nouvelle capacité `runs.transfer` — **`ORG_ADMIN_OF`** (org
  source) **ET** `ORG_ADMIN_OF` (org cible), ou platform admin → repose l'`org_id`
  de la ligne `procedure_runs`. MCP `oto_transfer_run` + REST
  `POST /api/orgs/{id}/runs/{run_id}/transfer`. Sémantique = **move** (un run a un
  seul propriétaire-org), aligné sur le besoin « transférer proprement à Julien
  dans son org » plutôt que « partager une copie ».

> Le partage-copie (façon `data_share`, lecture croisée multi-org) est **différé** :
> le besoin exprimé est un transfert de propriété, pas une lecture partagée
> persistante. On garde la porte ouverte (table `run_shares` analogue à
> `datastore_shares`) mais on ne la construit pas maintenant (YAGNI, ADR 0006
> « pas de discipline sans force »).

### D5 — Cross-org reste un switch d'org active (pas d'agrégation)

On **ne** charge **pas** les doctrines de toutes les orgs d'un coup. Une org = un
contexte de travail (secrets, doctrine, quotas, billing y sont tous scopés). Pour
exécuter une procédure d'une autre org : `oto_use_org` puis `oto_get_doctrine`.
C'est explicite, cohérent avec tout le reste du modèle, et évite une fuite de
contexte entre orgs. `_SERVER_INSTRUCTIONS` documente ce geste.

## Conséquences

**Positives**
- Le besoin déclencheur est couvert : un membre (Julien) voit les exécutions de
  son org ; on transfère proprement procédure **et** traces dans son org.
- One-shot tracé sans polluer la bibliothèque → l'index reste un catalogue de
  choses **réutilisables**, l'exécution unique est une archive.
- Recentrage conceptuel net : « démarrer une exécution » est la primitive ; la
  bibliothèque de procédures n'est qu'un cas particulier.
- Asymétrie corrigée : runs et signaux portent désormais tous deux `org_id`.

**Coûts / risques**
- **Migration** : table neuve + backfill optionnel des runs historiques depuis les
  lignes `tool_calls` `doctrine_start` (org_id inférable via l'org active de l'époque
  — non fiable rétroactivement → on backfill `org_id=NULL`, acceptable).
- **Rétention** : `tool_calls` est prune à 30j → la timeline détaillée d'un vieux
  run disparaît, mais l'en-tête `procedure_runs` (hors prune) subsiste. Si on veut
  garder la timeline des runs importants, prévoir un flag `pinned` (différé).
- **Autz `runs.get`** : bien gater sur l'`org_id` **de la ligne** (pas l'org active
  du lecteur) — sinon IDOR cross-org (rappel de l'incident scout cité en ADR 0009).
- Surface qui grandit (nouveaux outils MCP) — mitigé par « moins d'outils, plus
  d'args » et par le fait qu'ils sont spine (hors gate d'activation).

## Alternatives écartées

1. **Élargir `usage.runs` à `ORG_MEMBER` en filtrant par org active.** Rejeté :
   les projections admin sont transverses (non scopées) par conception ; mélanger
   les deux autz sur la même capacité = retour du drift de surface qu'ADR 0009
   combat. On sépare face-opérateur et face-membre.
2. **Inscrire les one-shots dans `org_instructions` avec un flag `ephemeral`.**
   Rejeté : un one-shot n'est pas un template ; le mettre dans la table des
   procédures brouille à nouveau template↔instance et complique l'index.
3. **Agrégation cross-org des doctrines.** Rejeté (D5) : casse la frontière d'org
   qui structure secrets/quotas/billing/visibilité.
4. **Partage-copie des runs (run_shares) tout de suite.** Différé : le besoin est
   un transfert de propriété, pas une lecture partagée.

## Plan de mise en œuvre (esquisse, hors-périmètre de cet ADR)

1. `db.py` : table `procedure_runs` dans `_SCHEMA` ; helpers
   `create_run/finish_run/list_runs_for_org/get_run/transfer_run` ; (index
   éventuel sur `org_id`/`started_at` dans le bloc **ALTER** d'`init_db`, jamais
   dans `_SCHEMA` — gotcha ADR 0017).
2. `doctrine_run.py` + `tools/doctrine_run.py` : `doctrine_start` écrit l'en-tête
   (slug **ou** title/intent) ; `doctrine_finish` clôt la ligne.
3. `capabilities/runs.py` : `runs.list` / `runs.get` (`ORG_MEMBER`) +
   `runs.transfer` (`ORG_ADMIN_OF` source+cible).
4. Dashboard `oto-dashboard` : vue « exécutions » de l'org active (liste + timeline).
5. CLAUDE.md (§Boucle d'usage) + `_SERVER_INSTRUCTIONS` : vocabulaire procédure/
   exécution, geste one-shot, switch cross-org.
