---
title: Rôles + résolution de clé API
type: reference
description: >-
  Référence des 3 paliers de rôles plateforme oto-backend (member < admin < super_admin,
  définis dans access/scope.py + roles.py, bootstrap via OTO_MCP_ADMIN_SUB) et de la cascade
  de résolution de clé API par appel : clé membre BYO scopée (sub, org) [ADR 0033] > grant explicite user_grants
  (quota daily) > McpError actionnable. Détaille les platform_keys en DB uniquement
  (plus de SOPS ; la POSE du secret brut est dashboard-only, le MCP ne porte que les
  droits via oto_admin_key_grant), le gate auth_modes pour
  les providers platform-éligibles (serper/hunter/sirene/kaspr), les providers byo-only
  (attio/lemlist/pennylane), le cas Slack (token xoxp per-user), et le débranchement
  SOPS (OTO_CONFIG_DISABLE_SOPS=1 en prod). À consulter pour diagnostiquer un accès
  refusé, ajouter un grant, ou comprendre qui peut quoi sur la plateforme.
adr:
  - "0016"
---

# Rôles + résolution de clé API

> ⚠️ Le **stockage** des credentials est le **coffre chiffré unique `connector_credentials`** (cf. `docs/connector-vault.md`). Les colonnes legacy `users.<provider>_api_key`/`org_secrets`/`user_google_oauth` ont été **purgées** (DROP, 2026-06-11) ; chiffrement **obligatoire** (plus de plaintext). La résolution ci-dessous reste valide dans sa cascade, lit le coffre via `credentials_store`.

Le rôle (`users.role`) décide de l'accès à l'admin UI, sur **3 paliers**
(`ROLES = (member, admin, super_admin)`, cf. `access/scope.py` + `roles.py`) :

- **super_admin** : le tout-puissant — escalade `org_admin` de TOUTE org +
  `group_admin` de TOUT groupe (`roles.is_platform_admin` = super), gestion des
  rôles plateforme, platform keys, émission de tokens, écriture + guide
  d'orgs tierces, création d'org, bypass namespace grant-only. Bootstrap env
  `OTO_MCP_ADMIN_SUB` → super_admin. Combinateur d'autz `SUPER_ADMIN`.
- **admin** (palier OPÉRATIONNEL intermédiaire) : supervision plateforme —
  monitoring, liste/fiche users, activation des connecteurs, refresh des mounts,
  lectures d'orgs — **SANS** escalade en masse vers les orgs tierces.
  Prédicat `access.is_platform_operator` (admin ∪ super) ; combinateur `PLATFORM_ADMIN`.
- **member** : défaut, pas d'effet sur l'accès aux tools (`guest` retiré
  2026-06-15, migré → member).

> Les `admin` historiques (= tout-puissants) ont été migrés → `super_admin`
> (`scripts/migrate_admin_to_super.py`). Un `admin` aujourd'hui = opérateur.

Résolution par appel (`resolve_api_key` / `resolve_credential`) :

1. Clé **membre** (sub, org de contexte — ADR 0033) → directe, sans quota.
2. Instance **personnelle cross-org** (#172, providers `personal_cross_org`).
3. Secret d'**équipe active**, puis d'**org active** (providers org-shareables).
4. Instance **plateforme** (grant/free-tier, ADR 0044 §F) avec quota.
5. Rien → McpError actionnable + **instances à portée** (voir walker ci-dessous).

> **Multi-compte par défaut (2026-08-26, reprise #399 ; étendu #409 le 27/08).** Tout
> connecteur dont le credential se **POSE** (`method=secret` — clé simple
> `api_key`/`basic_auth` **ou** multi-champs `fields`) est **multi-compte**
> (`Connector.auth_multi_account`) : des comptes **nommés** peuvent coexister aux
> paliers **membre, équipe et org** (segment `account` du coffre ; la ligne legacy
> `''` migre vers « principal » au premier compte nommé, `ensure_named_coexistence`).
> Sélection = param `account` / axe d'appel `_account=` / épinglage projet > compte
> unique auto > défaut `is_default` (`oto_identity op='set'`, scopes member/org/group).
> L'axe `_account=` est **accepté à l'appel partout** (`axes_for_call`, `oto_call`
> compris) et **annoncé** dans le schéma seulement où l'appelant détient ≥ 2 clés
> (`axes_for_listing`). Invariant : un compte NOMMÉ introuvable à un palier **passe
> la main** au suivant ; s'il n'existe à aucun palier à clé, la résolution **lève
> « introuvable »** après la marche — jamais un repli plateforme silencieux. La
> résolution **anonyme** (`<slug>.mcp.oto.cx`) sélectionne le compte d'org comme le
> chemin réel (unique/défaut), jamais `''` en dur.

> **Le nombre de champs ne dit rien de la cardinalité — et un compte nommé refusé vaut
> mieux qu'un compte nommé ignoré (#409, 27/08).** Deux acquis d'un même défaut.
> **(1)** `fields` était hors de la règle : Slack (`bot_token` + `user_token`) en tombait
> mono-compte, alors qu'un token Slack est émis par INSTALLATION dans un workspace (N
> installations = N tokens indépendants, auth par requête sans état) et que la lib
> `oto.tools.slack` sert déjà N workspaces — c'était la couche résolution qui figeait la
> cardinalité à 1. La règle couvre donc les credentials multi-champs ; un compte du coffre
> = un workspace, choisi par `_account=`. **(2)** Le coffre stocke N lignes par (entité,
> connecteur, compte) pour TOUS les connecteurs, mais seule la résolution d'un
> multi-compte va les lire : poser un compte nommé sur un mono-compte écrivait une ligne
> parfaitement valide que rien n'irait chercher. C'est désormais un **refus nommé** —
> `credentials_store.guard_account_write`, appelée sans condition par les trois surfaces
> déclaratives (membre `/api/settings/api-keys`, `org.secret.set`, `group.secret.set`),
> qui tranche sur le registre AVANT toute lecture du coffre : multi ⟹ cohérence des noms
> (409 `account_required`), mono ⟹ 400 `single_account_connector`. Un connecteur qui
> aurait une vraie raison de fournisseur d'être mono s'exclut par `single_account` dans
> son entrée de registre — jamais par une liste transverse, et aujourd'hui sans porteur
> (tripwire `test_single_account_write_guard`).

## Où vit quoi — le package `access/` (découpe du 2026-08-27)

`oto_mcp/access.py` (2 000 lignes, quatre sujets, 67 commits en 60 jours) est
devenu le package `oto_mcp/access/`. **Rien n'a changé de ce qui est servi** : le
découpage est un déplacement pur, la surface `access.<nom>` est identique au nom
près (cliquet `tests/test_access_surface_frozen.py`).

| module              | ce qu'il porte                                                        |
| ------------------- | --------------------------------------------------------------------- |
| `access/scope.py`   | rôle plateforme (`get_user_role`, `is_super_admin`, `is_platform_operator`), contexte de l'appel (`current_org`/`current_group`/`current_project`, `_UNSET`), appartenance à un scope de partage, ce que le projet ÉPINGLE (`project_pinned_identity`/`_instance`, slots) |
| `access/quotas.py`  | `quota_for`, `_org_unmetered`, `record_platform_usage`, `paid_option_for`, `has_option` |
| `access/cascade.py` | `walk_cascade`/`cascade_winner`, `CascadeRung`/`CascadeProbe`, `PRESENCE_PROBE`/`FETCH_PROBE`/`preloaded_presence_probe`, `group_secret_map`, le palier plateforme, `ORG_SHAREABLE_PROVIDERS` |
| `access/rbac.py`    | `rbac_denied_connectors` (+ équipe), `org_admin_hidden_tools` (+ équipe), `require_connector_access`, `guard_instance_access`, `reachable_instances`(+`_map`, `_team_key`), `resolve_field_filter` |
| `access/resolve.py` | `ResolvedCredential`, `resolve_credential` et son `_impl`, la résolution d'une instance épinglée, `_resolve_credential_anon`, `platform_quota_hint` (sonde en lecture seule du quota jour, sans consommer — oto-backend#710) |
| `access/views.py`   | `resolve_api_key`, `resolve_credential_fields`, `resolve_mount_token`, `credential_mode_for`, `option_open`, `connector_resolvable_for_org`, `BYO_MODES` |
| `access/status.py`  | `status_for` (le snapshot `/api/me`) et ses trois préchargements |

Les dépendances **descendent**, sans cycle (garde dans le même fichier de test) :

```
scope  ←  quotas ,  cascade  ←  rbac  ←  resolve ,  status  ←  views
```

Deux règles internes, et elles ne sont pas cosmétiques :

- **un frère s'appelle par son MODULE** (`scope.current_org(...)`), jamais par un
  nom importé. C'est ce qui dit au lecteur d'où vient la fonction, ET ce qui garde
  un point de patch unique : la façade propage toute écriture
  (`monkeypatch.setattr(access, "current_org", …)`, l'idiome d'environ deux cents
  endroits de la suite) au sous-module qui définit le nom ;
- **une locale ne porte pas le nom d'un frère**. Vécu à la découpe : `status_for`
  tenait ses compteurs du jour dans une locale `quotas`, et l'appel
  `quotas.quota_for(...)` est parti chercher la méthode sur un dict.

> **Ce que l'AGENT voit du choix de compte (27/08).** Le multi-compte n'existe pour lui
> que par quatre surfaces, et chacune avait un trou :
> - **le schéma** — l'axe `_account=` apparaît sur les outils du connecteur dès qu'il
>   détient ≥ 2 clés (`account_axis_advertised_for`), pas avant : recopier l'axe partout
>   doublerait le handshake. Il reste ACCEPTÉ partout (`accepts_account_axis`).
> - **l'aide d'`oto_identity`** — elle disait de passer `account=<id>`, le nom NU, alors
>   que les jetons ont été préfixés justement parce qu'il entrait en collision avec les
>   arguments métier (28/07). Un agent qui la suivait échouait. Elle nomme `_account`,
>   avec un exemple qui marche ; tripwire `test_identity_hint_names_the_axis` (il compare
>   à `call_axes.ACCOUNT.param`, pas à une constante recopiée).
> - **le refus** — « Plusieurs **workspaces** `slack` … précise lequel avec `_account=` »
>   au lieu de « plusieurs comptes » : le mot vient du registre (`access.account_noun`,
>   dérivé de `Connector.account_noun`), et le message porte le geste qui débloque.
>   Même chose pour « Workspace `X` introuvable ». `oto_identity op=list` rend `noun`.
> - **l'écho** — la réponse d'un appel servi par un compte NOMMÉ porte `_account`
>   (`middleware.call_context._echo_account`, posé par `CallContextMiddleware` au retour). Sans lui,
>   l'agent postait sur l'un de ses deux workspaces sans jamais savoir lequel : l'identité
>   effective ne vivait que dans le journal, qu'il ne lit pas. Trois gardes : compte nommé
>   seulement (mono ⟹ aucun bruit), même connecteur que l'outil appelé (un tool composite
>   résout des credentials auxiliaires), payload dict. L'écho est injecté **au-dessus de
>   la rédaction** — sinon il serait redacté, et la capture passive de schéma
>   l'enregistrerait comme un champ du connecteur.
> - `oto_identity` porte enfin `scope` côté agent (member/org/group), comme la face REST.

## Walker de cascade — source unique (2026-07-16, `access/cascade.py`)

La cascade ci-dessus vit dans **`access.walk_cascade`** (générateur paramétré
par sonde : `PRESENCE_PROBE` sans déchiffrement pour `/api/me`, `FETCH_PROBE`
qui ne déchiffre que le gagnant). Les 6 consommateurs (`_resolve_credential_impl`,
`credential_mode_for`, `status_for` ×2, `_resolve_credential_anon`,
`connector_resolvable_for_org`) en sont des traductions minces — **ne jamais
recopier la cascade dans un call-site** : chaque copie divergeait et faisait
mentir une surface (vécu 16/07). Contrat gardé par `tests/test_cascade_walker.py`
(ordre des barreaux, gates, accord présence/fetch). Les pins (`_instance=`/
`_project=`, ADR 0038) court-circuitent AVANT la marche ; `_group` se passe en
lazy (callable) côté fetch. Échec « rien ne résout » : l'erreur remonte les
**instances à portée** (`access.reachable_instances` — équipes dont le sub est
membre, autres orgs) avec le geste per-call en tête (`_group=`/`_org=`/`_instance=`) ;
le drawer reçoit le même signal via `status_for.team_key_group`.

### Fenêtre de double lecture L7 (2026-08-29, `access/chain_shadow.py`)

Le walker **décide encore**, mais il n'est plus seul à calculer : depuis PR 1 du lot
L7 (blueprint ADR 0053), la **chaîne de grants** résout en parallèle et les deux
verdicts sont comparés, classés et comptés dans `access_shadow_l7`. Rien de ce qui
est servi ne change — l'observation ne peut ni lever, ni modifier un verdict, et
`OTO_L7_SHADOW=0` l'éteint sans déploiement.

Le point à retenir pour qui lit la cascade : **quatre divergences sont ATTENDUES**,
et ce sont des décisions de l'ADR, pas des bugs — la chaîne lit **toutes** les
équipes du sujet quand la cascade ne lit que l'**active** (`elargissement_equipe`) ;
elle ignore `connector_acl`, que 0053-D1 dissout (`restriction_acl`) ; elle n'a pas
de bénéficiaire « tout le monde » pour une clé plateforme ouverte
(`free_tier_hors_modele`, comblé par une arête explicite en PR 2) ; et son ensemble
atteignable est scopé à l'org, là où la cascade suit une clé personnelle cross-org
(`perso_cross_org`). Seule la classe `inconnu` doit rester à zéro : c'est elle qui
ouvre la porte de l'inversion.

Lecture sans SQL : `oto_admin_access_shadow(op='read')` / `GET /api/admin/access-shadow`
(plancher opérateur) — le bloc `verdict` dit `porte_ouverte` seulement si le
dénominateur est non nul ET qu'aucun `inconnu` n'est tombé.

**Qui décide se lit dans un drapeau** (PR 2) : `OTO_L7_DECIDE=chain` retourne l'autorité
— la chaîne décide, le walker calcule et se compare, avec les MÊMES classes qu'à
l'aller. Toute autre valeur, l'absence comprise, vaut `legacy` : le comportement
d'aujourd'hui à l'octet près. Le retour arrière est le drapeau et un redémarrage, jamais
un revert ; c'est par-process, donc basculer la préprod ne bascule pas la prod.
L'inversion ne réécrit **pas** le fetch — elle réutilise la sonde que `resolve` a déjà
composée, avec sa sélection de compte et sa suspension : seule la traversée change. Sous
`chain`, la restriction `connector_acl` ne refuse plus (D1 la dissout) mais reste comptée.

**L'arête « tout le monde »** (`grants_chain.EVERYONE`, le scope `platform`) dit ce
qu'une clé plateforme ouverte disait et qu'aucune arête ne savait exprimer. Elle est
posée par une commande explicite — `scripts/seed_everyone_edges.py`, dry-run par défaut,
`--apply` — **jamais au boot** (ADR 0065). ⚠️ Elle n'est lue que sous l'autorité de la
chaîne : `grants_chain.platform_rung`, qui décide encore aujourd'hui pour les
connecteurs basculés de L5, reste aveugle à elle — sans quoi un accès individuel révoqué
sur une clé ouverte serait ressuscité. Et sous la chaîne, **une arête qui NOMME
l'appelant prime, révoquée comprise** : c'est ce qui garde « la révocation est vraie ».

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access/quotas.py`. User key bypass quota.

**Les platform keys vivent en DB uniquement** (coffre `platform_keys` — plus de
bootstrap SOPS/env au boot, oto-mcp#12). Poser/roter une clé = surface admin :
REST `POST /api/admin/platform-keys` ou meta-tool `oto_admin_set_platform_key`
(rotation = re-poser même provider+label ; label historique servi par
`resolve_api_key` = `env`). Poser ≠ granter : l'admin accorde l'accès au cas
par cas. Modèle : clé membre (sub, org de contexte — ADR 0033, prio, no quota) OU platform key + grant + quota OU
erreur. **Seuls les providers `platform`-éligibles au registre (`auth_modes`
inclut `platform` : `serper/hunter/sirene/kaspr`) peuvent avoir une clé
plateforme** — `resolve_api_key` **gate** le chemin platform-grant sur
`auth_modes` (audit 2026-06-11). Les comptes **privés / byo-only**
(`attio/lemlist/pennylane/fullenrich/slack`) **n'ont PAS de clé plateforme** :
les clés résiduelles du seed SOPS ont été supprimées, et le compte partagé de
l'**équipe Otomata** (attio/lemlist) vit en **credentials de l'org Otomata
(byo_org, org id 2)** — accès par appartenance, pas par grant plateforme.
**Slack** : pas de `SLACK_API_KEY`, le provider porte le **user token**
(`xoxp`) per-user — `slack_*` postent en `as_user` (mode bot viendra avec
l'OAuth install, issue #4).

**Débranchement SOPS (oto-mcp#12)** : l'unit pose `OTO_CONFIG_DISABLE_SOPS=1`
→ côté serveur, `oto.config.get_secret` ne résout QUE l'env du process (ni
SOPS ni `~/.otomata/secrets.env`), et tout `require_secret` résiduel échoue
fort. L'infra bootstrap (DATABASE_URL, Logto, OAuth Google, state secret)
reste en env de process (`/opt/oto-mcp/.env`).

Tous les tools API-keyed (`serper_*`, `hunter_*`, `sirene_*`, `fr_*`,
`attio_*`, `pennylane_*`, `slack_*`…) appellent `resolve_api_key(provider)`.
LinkedIn et WhatsApp ne sont pas concernés (cookie/session per-user) ; le
datastore non plus (spine PG, aucun credential — ADR 0016).

## Scope MEMBRE (ADR 0033, 2026-07-02)

> **Scope MEMBRE (ADR 0033, 2026-07-02).** Plus de credential per-user org-agnostique :
> la clé BYO d'un membre est keyée **(sub, org)** — coffre `entity_type='member'`,
> `entity_id="{org}:{sub}"` (AAD dérivé → liée crypto à son org). Posée dans l'org A,
> elle ne résout PAS depuis l'org B (fini « ta clé perso te suit partout et écrase la
> clé d'org »). L'org de scope = seam `current_org` (0023), à la pose (`/api/settings/
> api-keys`, sessions browser) comme à la résolution ; helpers `db.{get,has,set,clear}_
> member_api_key(sub, org_id, provider)` — la couche db ne lit JAMAIS `current_org`
> elle-même. Valeurs de contrat inchangées (`mode="user"`, `user_key_configured`) —
> seule la sémantique change. **B3 (google)** : comptes Google multi-comptes scopés
> (sub, org) — `db/google.py` en entité member, l'org du DÉMARRAGE du flow OAuth voyage
> dans le **state HMAC** jusqu'au callback (qui vient de Google, sans headers de
> consultation). **B4 (unipile)** : `unipile_accounts` au grain **(sub, org_id, provider)**
> — `org_id` = org de CONTEXTE du binding (la facturation des sièges plateforme a sa
> colonne `platform_seat` ; les BYO ne comptent pas dans le plafond) ; migration PK
> one-shot `db.backfill_unipile_member_scope()` (⚠️ le cycle de vie du PK lui appartient,
> pas à `_init.py`). **Seuls les mounts oauth fédérés** (atlassian/folkmcp)
> restent scope `('user', sub)` ; tripwire `test_member_credential_scope.py` interdit
> toute autre écriture scope user. Migration coffre = `credentials_store.
> backfill_member_scope()` au boot (re-chiffrement — l'AAD change, pas d'UPDATE ;
> destination = org maison ; ligne indéchiffrable laissée inerte).

## Multi-compte par défaut (2026-08-26, reprise #399 ; étendu #409 le 27/08)

> **Multi-compte par défaut (2026-08-26, reprise #399 ; étendu #409 le 27/08)** : tout
> connecteur dont le credential se **POSE** (`method=secret` — clé simple
> `api_key`/`basic_auth` **ou** multi-champs `fields`) est multi-compte — comptes
> **nommés** possibles aux paliers **membre/équipe/org**, sélection par `_account=` (axe
> d'appel, accepté partout — `oto_call` compris — annoncé seulement où ≥ 2 clés)
> ou param `account` ou défaut `is_default`. Un compte nommé introuvable à un
> palier passe la main ; introuvable partout ⇒ « introuvable » après la marche,
> jamais un repli plateforme silencieux. L'endpoint anonyme sélectionne le compte
> d'org comme le chemin réel (jamais `''` en dur). Détail : doc ci-dessus.
>
> ⚠️ **Le nombre de champs du credential ne dit RIEN de la cardinalité** (#409). `fields`
> était hors règle jusqu'au 27/08 : Slack (`bot_token` + `user_token`) en tombait
> mono-compte, alors qu'un token Slack est émis par INSTALLATION dans un workspace — et
> **poser un 2ᵉ compte écrivait une ligne que la résolution n'allait jamais lire**, sans
> refus. D'où deux acquis : la règle couvre `fields` (Slack sert N workspaces, un compte
> = un workspace), et **la pose d'un compte nommé sur un connecteur mono-compte est
> REFUSÉE** (`credentials_store.guard_account_write`, source unique des trois surfaces
> déclaratives membre/org/équipe → 400 `single_account_connector`). Un connecteur qui
> aurait une vraie raison de fournisseur d'être mono s'exclut par `single_account` DANS
> son entrée de registre, jamais par une liste transverse — aujourd'hui sans porteur.

## Scope MEMBRE — le résumé qu'en donnait la carte

> **Scope MEMBRE (ADR 0033)** : plus de credential per-user org-agnostique — la clé
> BYO est keyée `(sub, org)` (coffre `entity_type='member'`, AAD lié à l'org ; google
> + unipile inclus, seuls les mounts oauth fédérés restent scope user). L'org de scope
> = seam `current_org`, à la pose comme à la résolution.
> **Détail (helpers db, state HMAC google, migration) : `docs/roles-and-resolution.md` §Scope MEMBRE**.

## Seam substrat `access.resolve_credential` (ADR 0024)

**Seam substrat (ADR 0024)** : `access.resolve_credential(provider, want, sub?)` marche la cascade UNE fois → `ResolvedCredential{key, is_platform, mode, config, fields}` ; `resolve_api_key`/`resolve_credential_fields` = vues minces dessus (les ~15 tools keyed inchangés). `config` = **config non-secrète appariée à la clé gagnante** (endpoint/host : `dsn` unipile, `base_url` n8n/make, `data_center` zoho — `config_fields` `secret=False` ∪ meta public) → ne JAMAIS recâbler un résolveur d'endpoint par-connecteur. `access.credential_mode_for(sub, provider)` = le `mode` sans déchiffrer (détection BYO = `mode ∈ {user,group,org}`, jamais un check user-only). **La cascade elle-même = walker unique `access.walk_cascade`** (sonde présence /api/me vs fetch résolution) — ne jamais la recopier dans un call-site, contrat gardé par `test_cascade_walker.py` ; détail : `docs/roles-and-resolution.md` §Walker. ⚠️ **Le dashboard en porte un MIROIR d'affichage** (`lib/keyStack.ts`, oto-dashboard — il annonce à l'utilisateur quelle clé prendrait le relais s'il retire la sienne) : aucun test ne relie les deux repos, donc changer l'ordre des paliers ou ce qui est lu à chacun (ex. le groupe **actif** seul) ne casse rien — ça fait **mentir l'UI**. Vécu 04/08 : la pile annonçait comme relais des clés d'équipes non actives, que la cascade ne lit jamais.
