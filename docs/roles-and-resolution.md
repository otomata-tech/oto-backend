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
  (attio/lemlist/pennylane), le cas Slack (token xoxp per-user), et la règle « la lib
  de connecteurs ne lit aucun secret ». À consulter pour diagnostiquer un accès
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
  monitoring, liste/fiche users, activation des connecteurs,
  lectures d'orgs (le **refresh des mounts** en faisait partie jusqu'au 2026-09-09 :
  `oto_admin_refresh_mount` est parti avec la fédération MCP, ADR 0069) — **SANS** escalade en masse vers les orgs tierces.
  Prédicat `access.is_platform_operator` (admin ∪ super) ; combinateur `PLATFORM_ADMIN`.
- **member** : défaut, pas d'effet sur l'accès aux tools (`guest` retiré
  2026-06-15, migré → member).

> Les `admin` historiques (= tout-puissants) ont été migrés → `super_admin`
> (`scripts/migrate_admin_to_super.py`). Un `admin` aujourd'hui = opérateur.

Résolution par appel (`resolve_api_key` / `resolve_credential`), l'ordre d'oto-core
(#1499, `docs/partage-de-compte.md`) :

0. Le compte que l'appel **nomme** (`_account=` / `_instance=`) — seul chemin vers le
   compte d'une autre personne, au niveau que lui donnent ses règles.
1. Clé **membre** (sub, org de contexte — ADR 0033) → directe, sans quota.
2. (retiré par la bascule des comptes : l'instance personnelle d'une autre org, #172.)
3. Secret d'**équipe active**, puis d'**org active** (providers org-shareables).
   À chaque palier 0 à 3, seuls servent les comptes dont le niveau
   (`access/niveaux.niveau`) couvre ce que l'op exige (`tools/effets` : lire → `read`,
   écrire → `write`, envoyer ou détruire → `sensitive`) ; deux au même palier =
   `ambiguous_account` sauf défaut ; des comptes vus tous sous le niveau =
   `insufficient_level`, jamais la clé plateforme à leur place.
4. Secret de **tenant** (coffre `entity_type='tenant'`, ADR 0052 ; barreau livré le
   29/08/2026 avec L-clés) — sert toutes les orgs du tenant qui n'ont rien de plus proche.
5. Instance **plateforme** (grant/free-tier, ADR 0044 §F) avec quota.
6. Rien → McpError actionnable + **instances à portée** (voir walker ci-dessous).

⚠️ **Équipe non désignée** (#1317) : le palier 3 ne lit que l'équipe de l'APPEL (`_group=`) ou l'équipe
active — jamais « une équipe dont je suis membre » prise d'office (l'équipe par défaut est réservée aux
tenants opt-in depuis le 03/10/2026, `docs/groups-and-roles.md` ; une clé à portée est un geste que
l'agent fait pour l'entité pour qui il travaille, décision du 16/07/2026). Quand rien ne résout et
qu'une ou plusieurs équipes de l'appelant portent la clé, le refus **s'ouvre sur elles**, chacune avec
son `_group=<id>` ; plusieurs : toutes listées, aucune choisie (`access/indices.refus_sans_cle`). Une
équipe dont l'appelant n'est pas membre n'est ni servie ni nommée (sauf escalade org_admin, #218).
L'élargissement à toutes les équipes est celui de la chaîne de grants (ADR 0053, `elargissement_equipe`
ci-dessous), qui arrive avec la bascule L7, pas par la cascade.

⚠️ **Projet partagé à un non-membre** : depuis la bascule des comptes (#1499), sous `_project=` d'une org dont l'appelant n'est pas membre, il travaille avec
SES comptes, dans son org ; aucun compte de l'org ou de l'équipe propriétaire ne lui est
prêté (l'héritage #480 et l'option `credentials="inherit"` sont retirés). Seul un compte
qu'une règle NOMME ouvre à la personne sert hors de son org, nommé
([`partage-de-compte.md`](partage-de-compte.md), D1).

> **Cet énoncé est le seul.** La source est le walker `access/cascade.py` ; la cascade
> était recopiée dans trois autres documents, et **trois sur quatre ignoraient l'étage
> tenant** cinq semaines après sa livraison — dont celui qui s'intitule « résolution ».
> Ajouter un barreau = éditer le walker, puis cette liste. Nulle part ailleurs.

> **Multi-compte par défaut (2026-08-26, reprise #399 ; étendu #409 le 27/08).** Tout
> connecteur dont le credential se **POSE** (`method=secret` — clé simple
> `api_key`/`basic_auth` **ou** multi-champs `fields`) est **multi-compte**
> (`Connector.auth_multi_account`) : des comptes **nommés** peuvent coexister aux
> paliers **membre, équipe et org** (segment `account` du coffre ; la ligne legacy
> `''` migre vers « principal » au premier compte nommé, `ensure_named_coexistence`).
> Sélection = param `account` / axe d'appel `_account=` / épinglage projet > compte
> unique auto > défaut `is_default` (`oto_identity op='set'`, scopes member/org/group).
> L'axe `_account=` est **accepté à l'appel partout** (`axes_for_call`, `oto_call`
> compris) et **annoncé** dans le schéma seulement où l'appelant atteint ≥ 2 comptes,
> **tous paliers confondus** (`axes_for_listing`). Un compte se **renomme**
> (`oto_identity op='rename'`, `PATCH /api/connectors/{c}/identities/{id}`) — le nom est
> l'identifiant de `_account`, donc c'est la ligne du coffre qui change
> (`rename_account`), et un nom déjà pris est refusé (l'upsert écraserait l'autre clé). Invariant : un compte NOMMÉ introuvable à un palier **passe
> la main** au suivant ; s'il n'existe à aucun palier à clé, la résolution **lève
> « introuvable »** après la marche (`CompteIntrouvable`, typé et porteur du nom) —
> jamais un repli plateforme silencieux. La
> résolution **anonyme** (`<slug>.mcp.oto.cx`) sélectionne le compte d'org comme le
> chemin réel (unique/défaut), jamais `''` en dur.
> **Connecteur délégué** (`credential_of`) : l'épinglage projet se lit sur le
> connecteur appelé, puis sur son **porteur** — un lien posé sur la carte du compte
> Google épingle le compte de tous ses services, celui de la carte du service l'emporte.
> Les services Google (`gmail`, `drive`…) passent par cette sélection depuis
> oto-backend#1160 (`auth/google.credentials_for`) : leur paramètre `account` et l'axe
> `_account=` sont un seul choix (deux adresses différentes : refus `account_conflict`).

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
| `access/quotas.py`  | `quota_for`, `debiter_plateforme` (appelé par le seul règlement, `reglement.py`), `paid_option_for`, `has_option` (option payante = `entitlements.has_right`, org ou personne), `plafond_du_jour`/`quotas_leves` (levée `platform_unmetered`) |
| `access/entitlements.py` | `value_for` (le point de lecture unique : la valeur de l'org — ses lignes, sinon le défaut déclaré par l'instance —, qu'une ligne de personne ne fait que relever), `has_right`, `org_has`, `verifier_defauts` — les droits déclarés (ADR 0070 §7, `docs/droits-declares.md`), jamais `billing` |
| `access/cascade.py` | `walk_cascade`/`cascade_winner`, `CascadeRung`/`CascadeProbe`, `PRESENCE_PROBE`/`FETCH_PROBE`/`preloaded_presence_probe`, `group_secret_map`, le palier plateforme, `ORG_SHAREABLE_PROVIDERS` |
| `access/rbac.py`    | `org_admin_hidden_tools` (+ équipe), `guard_instance_access`, `reachable_instances`(+`_map`, `_team_key`), `resolve_field_filter` |
| `access/indices.py` | le texte des refus « rien ne résout » : `_revoked_hint` (clé retirée), `_reachable_hint` (instances à portée, et le projet LISIBLE qui épingle déjà une instance → `_project=<id>`, #499) — lecture seule, fail-soft |
| `access/resolve.py` | `ResolvedCredential`, `resolve_credential` et son `_impl`, la résolution d'une instance épinglée, `_resolve_credential_anon`, `platform_quota_hint` (sonde en lecture seule du quota jour, sans consommer — oto-backend#710) |
| `access/views.py`   | `resolve_api_key`, `resolve_credential_fields`, `credential_mode_for`, `option_open`, `connector_resolvable_for_org`, `BYO_MODES` (⚠️ `resolve_mount_token` y a vécu jusqu'au 2026-09-09 — retiré avec la fédération MCP, ADR 0069) |
| `access/status.py`  | `status_for` (le snapshot `/api/me`) et ses trois préchargements |
| `access/reglement.py` | `regler_appel` : le règlement d'un appel RÉUSSI, au bord qui a posé son relevé (middleware de contexte, cible d'`oto_call`, worker jev) — débite les `units` notées par l'outil sur la clé notée par le résolveur, et les arêtes de tenant ; `SANS_DEBIT`, les outils d'un connecteur à clé plateforme qui ne débitent rien (`docs/credits-au-moment-de-l-appel.md`) |

Les dépendances **descendent**, sans cycle (garde dans le même fichier de test) :

```
scope  ←  quotas ,  cascade  ←  rbac  ←  indices  ←  resolve ,  status  ←  views
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

Et une règle pour tout ce qui est DEHORS (#896), la même pour `org_store` : la
propagation descend, elle ne remonte jamais. Une doublure posée sur un sous-module
(`access.scope.current_org`) atteint les lecteurs internes et **rate en silence** les
quelque 680 appels qui lisent la façade. D'où la cible, gardée à zéro par
`tests/test_facades_lecteurs_cible.py` :

- un lecteur hors du paquet lit la façade (`access.<nom>`), jamais un sous-module
  qu'elle ré-exporte, et n'importe jamais une **fonction** par son nom (le lien serait
  figé à l'import) — types et constantes, oui ;
- un test patche la façade, jamais un sous-module qu'elle ré-exporte ;
- un sous-module que la façade ne ré-exporte pas (`chain_shadow`,
  `chain_resolution`) n'a pas d'autre adresse : il se lit et se patche en direct.

La garde ne voit pas une assertion que tient une capture trop large quand la doublure
n'est pas atteinte (`pytest.raises(Exception)`, un chemin best-effort) : cette classe
se rejoue en remplaçant la règle d'écriture de la façade, pas en lisant le code.

> **Ce que l'AGENT voit du choix de compte (27/08).** Le multi-compte n'existe pour lui
> que par quatre surfaces, et chacune avait un trou :
> - **le schéma** — l'axe `_account=` apparaît sur les outils du connecteur dès qu'il
>   atteint ≥ 2 comptes (`account_axis_advertised_for`), pas avant : recopier l'axe partout
>   doublerait le handshake. Il reste ACCEPTÉ partout (`accepts_account_axis`). ⚠️ Le
>   relevé ne comptait que le palier MEMBRE : des clés posées par l'org (une par société
>   d'un groupe) n'annonçaient jamais l'axe, et l'agent ne le découvrait qu'en échouant.
>   Il compte désormais membre + équipe active + org, et la description de l'axe NOMME
>   les comptes atteints (palier, défaut) — elle n'est dynamique que là.
> - **l'aide d'`oto_identity`** — elle disait de passer `account=<id>`, le nom NU, alors
>   que les jetons ont été préfixés justement parce qu'il entrait en collision avec les
>   arguments métier (28/07). Un agent qui la suivait échouait. Elle nomme `_account`,
>   avec un exemple qui marche ; tripwire `test_identity_hint_names_the_axis` (il compare
>   à `call_axes.ACCOUNT.param`, pas à une constante recopiée).
> - **le refus** — « Plusieurs **workspaces** `slack` … précise lequel avec `_account=` »
>   au lieu de « plusieurs comptes » : le mot vient du registre (`access.account_noun`,
>   dérivé de `Connector.account_noun`), et le message porte le geste qui débloque.
>   Même chose pour « Workspace `X` introuvable ». `oto_identity op=list` rend `noun`.
>   Le refus **nomme les comptes** en présence et s'écrit sans accord (« sans défaut
>   unique ») : le mot du registre peut être féminin (« société »).
> - **la fiche** — chaque connecteur multi-compte porte une section « plusieurs <mot>s »
>   GÉNÉRÉE (`docs_reader.multi_account_section`, jointe par `Connector.doc_sections`) :
>   la mécanique est celle de la plateforme, une fiche n'écrit que ce qui lui est propre
>   (pourquoi il faut une clé par société, par installation…).
> - **l'écho** — la réponse d'un appel servi par un compte NOMMÉ porte `_account`
>   (`middleware.call_context._echo_account`, posé par `CallContextMiddleware` au retour). Sans lui,
>   l'agent postait sur l'un de ses deux workspaces sans jamais savoir lequel : l'identité
>   effective ne vivait que dans le journal, qu'il ne lit pas. Trois gardes : compte nommé
>   seulement (mono ⟹ aucun bruit), même **porteur** que l'outil appelé (un tool composite
>   résout des credentials auxiliaires ; comparer les noms nus taisait l'écho d'un compte
>   Microsoft d'autrui, résolu sous `microsoft` pour un outil `outlook_*`), payload dict.
>   Un compte d'**une autre personne** (ligne membre d'un autre sub, lue dans le relevé
>   `credential_row`) ajoute `lent_by` = e-mail (ou nom) de son propriétaire
>   (`access.rappel_du_compte`, `access/comptes.py`). L'écho est injecté **au-dessus de
>   la rédaction** — sinon il serait redacté, et la capture passive de schéma
>   l'enregistrerait comme un champ du connecteur.
> - **le compte d'autrui ne sert que nommé** (15/10/2026, repris par la bascule des
>   comptes #1499) — `_account=` (ou `_instance=`) le nomme, et il sert au niveau que lui
>   donnent ses règles ; jamais choisi d'office, ni remplacé par la clé plateforme. Les
>   comptes de l'org ou de l'équipe ELLES-MÊMES servent sans nom, au niveau de leurs
>   règles. Mesuré : une emprunteuse sans compte propre opérait la boîte d'une collègue
>   sans le savoir. Une même ligne du coffre a UNE réf d'instance, celle du porteur,
>   qu'on l'atteigne par la carte ou par le porteur (`_pin_instance` et la résolution
>   comparent les porteurs, la trace du journal l'écrit sous le porteur ; `instance_id`
>   y dit l'instance servie).
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
membre, lues en lot par `group_store.groups_holding`, jamais une requête par équipe ; autres
orgs) avec le geste per-call en tête (`_group=`/`_org=`/`_instance=`) — et une équipe qui porte
la clé ouvre le refus (#1317) ;
le drawer reçoit le même signal via `status_for.team_key_group`.

### Fenêtre de double lecture L7 (2026-08-29, `access/chain_shadow.py`)

Le walker **décide encore**, mais il n'est plus seul à calculer : depuis PR 1 du lot
L7 (blueprint ADR 0053), la **chaîne de grants** résout en parallèle et les deux
verdicts sont comparés, classés et comptés dans `access_shadow_l7`. Rien de ce qui
est servi ne change — l'observation ne peut ni lever, ni modifier un verdict, et
`OTO_L7_SHADOW=0` l'éteint sans déploiement.

Le point à retenir pour qui lit la cascade : **ces divergences sont ATTENDUES**,
et ce sont des décisions de l'ADR, pas des bugs — la chaîne lit **toutes** les
équipes du sujet quand la cascade ne lit que l'**active** (`elargissement_equipe`) ;
elle n'a pas
de bénéficiaire « tout le monde » pour une clé plateforme ouverte
(`free_tier_hors_modele`, comblé par une arête explicite en PR 2) ; et son ensemble
atteignable est scopé à l'org, comme la cascade depuis la bascule des comptes
(`perso_cross_org` n'a plus de cas). Seule la classe `inconnu` doit rester à zéro : c'est elle qui
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
La restriction `connector_acl` (et sa classe `restriction_acl`) a disparu le 24/09/2026,
des deux côtés du drapeau : restreindre, c'est placer la clé au bon niveau (0053-D1).

**L'arête « tout le monde »** (`grants_chain.EVERYONE`, le scope `platform`) dit ce
qu'une clé plateforme ouverte disait et qu'aucune arête ne savait exprimer. Elle est
posée par une commande explicite — `scripts/seed_everyone_edges.py`, dry-run par défaut,
`--apply` — **jamais au boot** (ADR 0065). ⚠️ Elle n'est lue que sous l'autorité de la
chaîne : `grants_chain.platform_rung`, qui décide encore aujourd'hui pour les
connecteurs basculés de L5, reste aveugle à elle — sans quoi un accès individuel révoqué
sur une clé ouverte serait ressuscité. Et sous la chaîne, **une arête qui NOMME
l'appelant prime sur « tout le monde »**, et une révocation prime sur tout (règle
ci-dessous) : une clé ouverte posée après une révocation, avec son arête « tout le
monde », ne sert pas l'appelant révoqué.

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access/quotas.py`. User key bypass quota.
⚠️ **Quota EXIGÉ à l'octroi** pour un connecteur qui déclare `grant_quota_unit` (registre,
`providers/<nom>.py` ; aujourd'hui `elevenlabs`, en crédits ElevenLabs) : un accord sans
`daily_quota`, ou à 0 (= illimité), est refusé (`400 grant_quota_required`) par TOUT chemin —
`oto_admin_key_grant` / `platform.{key,org,group}.grant_key` (qui portent le quota),
`platform.connector.access_set` et `platform.option.set` (qui n'en portent pas : refusés
avant toute écriture, le message renvoie au grant). La règle vit dans
`credentials_store.exiger_quota_d_octroi`, jouée aussi par `platform_grant` lui-même ; la
console la traduit par `capabilities/_quota_d_octroi.py`.

**Les platform keys vivent en DB uniquement** (coffre `platform_keys` — plus de
bootstrap SOPS/env au boot, oto-mcp#12). Poser/roter une clé = surface admin :
REST `POST /api/admin/platform-keys` ou meta-tool `oto_admin_set_platform_key`
(rotation = re-poser même provider+label ; label historique servi par
`resolve_api_key` = `env`). Poser ≠ granter : l'admin accorde l'accès au cas
par cas. Modèle : clé membre (sub, org de contexte — ADR 0033, prio, no quota) OU platform key + grant + quota OU
erreur. **Seuls les providers `platform`-éligibles au registre (`auth_modes`
inclut `platform` : `serper/hunter/sirene/kaspr`) peuvent avoir une clé
plateforme** — `resolve_api_key` **gate** le chemin platform-grant sur
`auth_modes` (audit 2026-06-11).
**Révoquer COUPE, re-poser ne touche pas aux accords (F-046).** `platform_revoke` inscrit
le scope (`org:<id>` | `user:<sub>`) dans `meta.share_denied` de **chaque** clé
plateforme du connecteur, et la lecture refuse dès qu'**une** clé porte le refus : une
autre clé ouverte, ou une clé posée après la révocation, ne sert pas l'appelant révoqué.
Accorder, révoquer et re-poser verrouillent les lignes lues (`FOR UPDATE`) : deux gestes
concurrents ne s'effacent plus. Le refus
prime sur tout (`access.platform_grant._platform_denied` — palier ouvert, `share_down`), y compris sur une clé `open` à `share_down` vide qu'un simple retrait de
liste ne coupait pas ; seul `platform_grant` l'efface, et sur une clé ouverte à tous il
n'y pose que le quota, sans la fermer. Retirer le dernier bénéficiaire d'une clé `open` à
liste la passe `closed`, jamais « ouverte à tous ». Côté chaîne, révoquer un scope sans
arête pose une arête née archivée (la chaîne refuse au lieu de se taire), et révoquer
sans libellé archive les arêtes du scope sur **toutes** les clés du connecteur.
**La révocation prime sur tout accord, quel que soit le grain** — chemin non chaîné
(`_platform_denied`), L5 (`grants_chain.platform_rung`) et L7
(`chain_resolution._platform_pick`) : un scope de l'appelant (`user:<sub>` ou
`org:<org active>`) révoqué sur le connecteur le refuse, même s'il a un accord vivant
sur l'autre scope. Côté chaîne, « révoqué » = des arêtes du scope, **aucune vivante**,
sur l'ensemble des clés (`grants_chain.revoked_scopes`) : un `grant` archive l'arête
qu'il remplace mais en pose une vivante du même scope, donc un remplacement n'est
jamais lu comme une révocation, et seul un `grant` sur ce même scope lève le refus.
La priorité `user:` > `org:` ne sert qu'à choisir le **quota** de l'arête gagnante. Une
re-pose sous le même libellé est une **rotation** : `share_mode` et les clés d'accord de
`meta` (`rate_limit`, `rate_limit_by`, `share_denied`) restent ; seule une clé NEUVE
payante naît `closed`. **Une clé payante naît fermée et s'ouvre org par org** (décision du
13/10/2026) ; seule une clé à palier gratuit (`platform_key_open`) naît ouverte. Une
rotation ne rouvre jamais une clé fermée, y compris vidée par la révocation de son
dernier bénéficiaire (liste vide = personne).
**Une org ne vide pas seule une clé commune (F-051).** Au palier plateforme d'une
exécution (`check_usage`), `access/platform_rate.enforce` compte un appel pour l'org
dont la clé est consommée (l'org de l'appel ; `user:<sub>` sans org ; l'org du
projet pour l'endpoint anonyme) dans la table `platform_key_rate`, par fenêtre FIXE
sur l'horloge de la base. Au-delà de `OTO_PLATFORM_KEY_ORG_RATE_LIMIT` (120) appels par
`OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S` (60 s), surchargeable par porteur
(`OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES=serper=600,aiark=0`) :
`OTO_PLATFORM_KEY_ORG_RATE=report` (défaut) journalise une fois par fenêtre,
`enforce` refuse (`platform_key_org_rate_limited`, « réessayez, ou posez votre clé »),
`off` ne compte rien. Un appel, pas des `units` : la limite protège le débit du
fournisseur, le volume reste au quota du jour. Compteur en panne ⟹ appel servi, journalisé.
Les comptes **privés / byo-only**
(`attio/lemlist/pennylane/fullenrich/slack`) **n'ont PAS de clé plateforme** :
les clés résiduelles du seed SOPS ont été supprimées, et le compte partagé de
l'**équipe Otomata** (attio/lemlist) vit en **credentials de l'org Otomata
(byo_org, org id 2)** — accès par appartenance, pas par grant plateforme.
**Slack** : pas de `SLACK_API_KEY`, le provider porte le **user token**
(`xoxp`) per-user — `slack_*` postent en `as_user` (mode bot viendra avec
l'OAuth install, issue #4).

**La lib de connecteurs ne lit aucun secret (oto-core v1.148.0)** : plus de
`oto.config` ni de résolution SOPS/fichier — le serveur passe chaque credential au
constructeur du client, qui lève `MissingCredential` s'il manque. L'infra bootstrap (DATABASE_URL, Logto, OAuth Google, state secret)
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
> pas à `_init.py`). Les **mounts oauth fédérés** (atlassian/folkmcp) restaient scope
> `('user', sub)` jusqu'au **2026-09-09**, où ils sont partis avec la fédération MCP
> (**ADR 0069**) : ⚠️ **plus aucun connecteur n'écrit à ce scope** — mais des lignes y
> DORMENT toujours, jamais migrées ni purgées. Le tripwire
> `test_member_credential_scope.py` interdit toujours toute écriture scope user. Migration coffre = `credentials_store.
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
> + unipile inclus ; les mounts oauth fédérés restaient scope user jusqu'à leur retrait
> le 2026-09-09 — plus aucun écrivain depuis, des lignes dormantes toujours). L'org de scope
> = seam `current_org`, à la pose comme à la résolution.
> **Détail (helpers db, state HMAC google, migration) : `docs/roles-and-resolution.md` §Scope MEMBRE**.

## Seam substrat `access.resolve_credential` (ADR 0024)

**Seam substrat (ADR 0024)** : `access.resolve_credential(provider, want, sub?)` marche la cascade UNE fois → `ResolvedCredential{key, is_platform, mode, config, fields}` ; `resolve_api_key`/`resolve_credential_fields` = vues minces dessus (les ~15 tools keyed inchangés). `config` = **config non-secrète appariée à la clé gagnante** (endpoint/host : `dsn` unipile, `base_url` n8n/make, `data_center` zoho — `config_fields` `secret=False` ∪ meta public) → ne JAMAIS recâbler un résolveur d'endpoint par-connecteur. `access.credential_mode_for(sub, provider)` = le `mode` sans déchiffrer (détection BYO = `mode ∈ {user,group,org}`, jamais un check user-only). **La cascade elle-même = walker unique `access.walk_cascade`** (sonde présence /api/me vs fetch résolution) — ne jamais la recopier dans un call-site, contrat gardé par `test_cascade_walker.py` ; détail : `docs/roles-and-resolution.md` §Walker. ⚠️ **Le dashboard en porte un MIROIR d'affichage** (`lib/keyStack.ts`, oto-dashboard — il annonce à l'utilisateur quelle clé prendrait le relais s'il retire la sienne) : aucun test ne relie les deux repos, donc changer l'ordre des paliers ou ce qui est lu à chacun (ex. le groupe **actif** seul) ne casse rien — ça fait **mentir l'UI**. Vécu 04/08 : la pile annonçait comme relais des clés d'équipes non actives, que la cascade ne lit jamais.
