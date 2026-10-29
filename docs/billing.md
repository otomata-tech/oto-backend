---
title: Facturation par org (ADR 0043) — le modèle, la TVA, le consentement, les factures, et le double débit du 25/08
type: reference
---

# Facturation par org (ADR 0043) — le modèle, la TVA, le consentement, les factures, et le double débit du 25/08

> ⚠️ **Depuis la coupure du cœur (#1097), la facturation est tenue par oto-commerce.** Le
> cœur ne vend, n'offre ni ne prélève plus rien, et ne pose aucun droit : les gestes de
> vente et d'offre refusent en 409 `billing_moved`, le runner est gelé et refuse toute
> échéance (§ Les droits déclarés). Ce document décrit le code resté en place — les
> lectures, les factures, le webhook — et l'histoire de ce qui s'y faisait.

## Ce que Mollie voit, et ce qu'il ne voit pas

**Il n'y a pas d'abonnement Mollie.** Chercher `/v2/customers/<id>/subscriptions`
pour comprendre un abonnement oto ne rend rien : ADR 0043 pose le miroir local
`org_subscriptions` comme source de vérité, PSP-agnostique. Mollie ne connaît que
des **paiements** : un `sequenceType=first` au checkout, puis des `recurring` (MIT)
rejoués par `billing_runner.tick()` sur `customerId` + `mandateId`. Deux tables le
reflètent — `org_subscriptions` (PK `org_id`, donc **un** abonnement par org,
structurellement) et `billing_payments` (journal, `kind` ∈ `initial` | `renewal`).
Une troisième, `billing_identities`, ne reflète rien de Mollie : elle dit **qui
paie et depuis quel pays**, et c'est elle qui décide du montant (voir la TVA,
plus bas).

Trois objets Mollie, trois durées de vie :

| objet | naît | vit |
| --- | --- | --- |
| **customer** (`cst_…`) | à la 1ʳᵉ souscription de l'org | **pour toujours** — un seul par org |
| **paiement** (`tr_…`) | à chaque checkout / échéance | jusqu'à son statut terminal |
| **mandat** (`mdt_…`) | **quelques minutes APRÈS** l'encaissement du 1ᵉʳ paiement | jusqu'à révocation |

La troisième ligne est le piège central, et il a coûté 19 € au premier client.

## On ne vend pas sans consentement (#487)

`legal_docs.py` déclarait depuis toujours un contexte **`purchase`** (CGU + CGV +
DPA) que **personne n'appelait** : `billing.subscribe` ne consultait pas
`legal_acceptances`, et le tunnel n'affichait aucune mention légale. Publier des
CGV ne les rend opposables à personne — il faut une **acceptation horodatée**.

`subscribe` prend donc l'appelant (`sub`, **obligatoire** : accepter est un acte de
personne, pas d'organisation) et refuse **409 `legal_required`** tant que les trois
documents ne sont pas acceptés **à leur version courante**. Un bump de version dans
`OTO_LEGAL_DOCS` (les documents que l'instance déclare, `legal_docs.current_docs`) rouvre
le gate ; une acceptation périmée ne vaut pas.

### Deux préalables, un seul aller-retour

**L'ordre est celui du tunnel : identité de facturation, puis consentement.** Le
payeur accepte des CGV *pour un montant*, et le montant n'existe qu'une fois le
pays connu (c'est lui qui décide de la TVA, §#486). Faire consentir d'abord et
chiffrer ensuite ferait accepter un prix qui n'a pas encore été annoncé — le
consentement est le **dernier geste avant la page de paiement**.

Mais ordonner n'est pas refuser un à la fois. Les deux manques sont évalués
ensemble et rendus ensemble :

```json
{ "error": "billing_identity_required",
  "detail": "billing_identity_required: … legal_required: …",
  "details": { "blockers": [
    { "code": "billing_identity_required", "message": "… champs à renseigner : …" },
    { "code": "legal_required", "context": "purchase",
      "message": "… CGU 3.0 (https://oto.cx/terms), CGV 2.0 (…), DPA 2.0 (…) …",
      "documents": [ { "slug": "terms", "label": "CGU", "version": "3.0",
                       "url": "https://oto.cx/terms", "accepted_version": null } ] } ] } }
```

- Le **code de tête** est celui du **premier** manque — les codes historiques
  (`billing_identity_required`, `vat_consumer_unsupported`) sont donc inchangés
  quand ils sont seuls. ⚠️ **Avec deux manques, il n'en nomme qu'un : c'est
  `details.blockers` qu'un client doit lire.**
- `accepted_version` distingue « jamais accepté » (`null`) de « accepté à une
  version périmée » — sans lui, le payeur est renvoyé chercher une case cochée.
- **Rien ne part chez le PSP** tant qu'un préalable manque : un refus après
  création laisserait un customer et une page payable derrière lui.

Le tunnel répare avec `POST /api/me/billing/identity` puis
`POST /api/me/legal/accept {"context": "purchase"}`, et relance `subscribe`.

## Deux façons d'offrir, et une seule était visible (2026-09-02)

⚠️ **Depuis la coupure du cœur (#1097), aucune des deux ne s'offre plus ici** : plan
offert et don d'une option du catalogue refusent en 409 `billing_moved`, et
`granted[]` n'annonce plus un avantage qui est un droit du catalogue (aujourd'hui, tous) :
ce qui est offert se lit dans le service de facturation, oto-commerce, qui le pose.
Ce qui suit est l'histoire du dispositif, et reste vrai de sa mise en forme.

Un droit payant peut s'ouvrir **sans** abonnement, et c'est là que le produit mentait.

| chemin | ce qu'il écrit | ce que `billing.status` en disait |
| --- | --- | --- |
| **abonnement offert** — `admin_set_plan` | une ligne `org_subscriptions` `provider='comp'` | `comp: true`, badge « offert par Otomata », pas de bouton résilier |
| **don d'option** — `admin.option.set` | une ligne `option_comps` | **rien** |

Le second n'écrit aucune ligne d'abonnement, et l'écran lit l'abonnement : son
bénéficiaire voyait donc un catalogue lui vendre, prix affichés et bouton armé,
**exactement ce qu'il possédait déjà**. Mesuré le 2026-09-02 : **32 dons vivants**
(20 orgs, 12 comptes), **un seul abonnement payant** sur toute la plateforme, et
**zéro** org en abonnement offert — donc l'état soigné existait pour un cas qui
n'arrivait jamais, et manquait pour le seul qui arrivait.

`billing.status` porte désormais `granted[]` dans les **deux** branches
(`billing_grants.granted_benefits`). Trois règles qui portent le sens :

- **l'avantage se NOMME** (`label`, dérivé du connecteur porteur) — il n'y a pas que
  la messagerie qui coûte, et un badge « offert par Otomata » sans complément
  deviendrait faux au deuxième avantage ;
- **est un avantage ce qui est VENDU** : le catalogue se dérive des `options` des
  paliers de `PLANS`. Une option qui n'est dans aucun palier (`beta`, un drapeau de
  population) n'est pas un cadeau et ne s'affiche jamais comme tel ;
- **le catalogue de paliers reste servi** à côté du don. Un don n'est pas un
  abonnement ; refermer la voie de souscription serait perdre la conversion.

### L'échéance d'un don (`option_comps.expires_at`)

`NULL` = perpétuel, l'état de tous les dons antérieurs au 2026-09-02 : la colonne
est additive au sens du droit, elle ne retire rien. Une date se pose **ligne par
ligne**, par un acte admin explicite (`oto_admin_set_option expires_at=…`), et
s'efface en repassant une chaîne vide.

- **Elle mord dans le seam** : `has_option_comp` ignore une ligne échue, pour les
  drapeaux hors catalogue (`beta`…) qui se posent encore ici. (Le don d'une option du
  catalogue était recopié en droit déclaré `offered` avec son échéance par la
  réconciliation, retirée à la coupure du cœur, #1097.)
- **`list_option_comps` ne filtre PAS** : une console admin doit voir le don échu,
  sinon il devient invisible donc irrécupérable.
- **Omettre `expires_at` ne l'efface pas** (sentinelle `db.KEEP_EXPIRY`) : deux
  surfaces re-posent un don sans rien savoir des dates, leur geste anodin ne doit pas
  retirer une borne posée ailleurs.
- `YYYY-MM-DD` = **fin** de la journée : « offert jusqu'au 31 octobre » couvre le 31.

### Le périmètre : les clients d'un partenaire ne sont pas les nôtres

⚠️ **Aucun dispositif qui S'ADRESSE au titulaire d'une org — badge, échéance,
compteur, relance — ne touche une org hébergée par un tenant tiers.** Ce sont les
clients d'un partenaire, sur ses données, dans son produit. C'est une limite de
périmètre, donc elle est **mécanique** : `billing_grants.org_is_ours`, et
`tests/test_billing_grants_offert.py` rougit si elle cède.

**Le discriminant n'est PAS `orgs.tenant_id`** — mesuré **inerte** le 2026-09-02 :
les 160 orgs portent le tenant primaire, **y compris les 61 qui vivent chez un
partenaire**, parce que le provisioning ne l'écrit pas. Un filtre bâti dessus
n'aurait rattrapé aucune des **11 orgs gratifiées sur 20** qui appartiennent au
partenaire. `db.org_tenant_slug` prend l'**union de trois axes** (rattachement
déclaré, `orgs.front_brand` dérivé de l'émetteur à la création, préfixe du sub d'un
membre) ; les deux derniers rendent le même ensemble de 61 orgs, **zéro désaccord**,
et se couvrent mutuellement les angles morts. Le refus est **fail-closed** : une
lecture qui échoue referme le dispositif.

### L'usage inclus (`usage`)

**1000 appels d'outil d'agent par mois et par org** (cadre Alexis, 2026-09-02),
servi à tout le monde — abonné ou non, gratifié ou non.

⚠️ **Ce n'est pas un plafond de refus.** Le journal est best-effort et non
transactionnel : bâtir un refus dessus couperait un service sur une donnée qui a le
droit de manquer. Un dépassement s'affiche, il ne coupe pas, et il ne facture pas.

- **La valeur ne mord sur personne, délibérément** : sur août 2026 (clients directs,
  partenaire écarté), 16 orgs actives, maximum 516 appels, **médiane 25**. Le
  compteur rend l'usage visible et pose qu'oto a une limite ; il ne la fait pas sentir.
- **Aucun ratio n'est servi.** À 25 sur 1000, un pourcentage ou une barre dit « c'est
  gratuit et sans fin » — l'inverse de l'effet cherché. On rend le nombre et le
  plafond, on ne les divise pas.
- **`kind='mcp'` et `tool_calls.org_id`** : les appels d'agent, par le rattachement
  RÉEL. Jamais un préfixe de nom d'outil (les noms ne portent pas l'org, et un tenant
  peut les voir préfixés autrement).
- **Lu sur les totaux par jour** (`journal_jour.source`, `docs/monitoring.md`) : les
  jours clos du mois consolidés, plus le journal direct pour la veille et le jour
  courant. Le compte direct d'un mois relisait au heap toutes les lignes de l'org,
  REST du tableau de bord comprises (l'index `(org_id, created_at)` ne porte pas
  `kind`) : la fiche d'org (`GET /api/orgs/{id}`, `/api/admin/orgs/{id}`), qui
  l'appelle à chaque vue, finissait au `statement_timeout`. Un jour clos absent du
  registre lève `AgregatIncomplet` ; le compteur se tait alors (`usage: null`),
  comme sur toute lecture qui échoue.
- **Mois en cours SEULEMENT** : la purge du journal ne garde qu'environ 35 jours (la
  politique en annonce 90 — écart corrigé le 2026-08-28). Le mois précédent n'est pas
  calculable ; ne pas bâtir de comparaison dessus.

### « Cette org a-t-elle l'option » : une question, trois réponses (corrigé)

Trois fonctions y répondaient avec trois règles. Conséquence mesurée : **une org qui
PAYAIT s'affichait « non souscrite »** dans son cockpit d'activation, dont la lecture
ne regardait que le don admin et jamais le plan. **Il n'y a plus qu'une règle** (lot 2
de #806) : pour une option payante, `access.has_option` = `access.has_right(sub, org
courante)`, le droit déclaré de l'org ou une ligne posée sur la personne (« Les droits
déclarés », plus bas), et le cockpit, qui parle de l'org, lit `access.org_has`.
`access.views.option_open` reste au-dessus (il croise avec le BYO). Le cœur n'importe
plus `billing` (cliquet `tests/test_access_sans_billing.py`). ⚠️ **Le don fait à une
personne (sa marque de compte) n'ouvre pas d'option payante**, et `status.granted[]` ne
l'annonce plus.

### Ce qui ne demande PAS de consentement

Une **échéance** : le consentement a été donné à la souscription, `billing_runner`
ne le rejoue pas — `_charge_one` ne prend d'ailleurs pas de `sub`, et un test le
fige.

### Changer de moyen de paiement, et annuler une résiliation (#845)

Deux gestes que l'écran annonçait sans les offrir. **Le premier coûtait un abonné
payant** : carte morte, toutes les relances en échec, et aucun moyen d'y remédier
pendant les trois tentatives et les quatorze jours de grâce que les conditions de vente
chiffrent.

**Annuler une résiliation** (`POST /api/me/billing/resume`) est purement local :
résilier ne révoque pas le mandat et laisse l'abonnement `active` jusqu'à l'échéance, on
défait donc deux écritures — `canceled_at` et `next_billing_at`, qu'on **restaure** au
lieu de recalculer. ⚠️ Refusé si la période est échue : le sweep a basculé le statut, et
reprendre là rouvrirait l'entitlement sans qu'aucune échéance ne soit tirée — c'est un
réabonnement, il passe par `subscribe`.

**Changer de moyen** (`POST /api/me/billing/method` puis `…/method/confirm`) passe par un
**premier paiement à 0,00 EUR**, seul chemin possible : Mollie n'a pas de portail de
changement de carte, et `POST /mandates` refuse les cartes (« your customers need to
perform a first payment »). Le zéro-montant est documenté pour carte et PayPal, et
**aucun mouvement d'argent ⟹ aucun remboursement ⟹ aucun avoir** — c'est ce qui a fait
écarter le montant symbolique.

    first à 0,00 → mandat au retour → bascule → révocation de l'ancien

⚠️ **La révocation vient après la bascule, et elle est best-effort.** Si elle échoue, le
prochain encaissement prend quand même le nouveau mandat : un ancien mandat qui traîne
coûte moins cher qu'une bascule annulée parce que le ménage a raté. ⚠️ **L'ancien moyen
reste actif tant que le nouveau n'est pas confirmé**, et la réponse le DIT — sans cette
phrase, qui abandonne le checkout croit s'être coupé.

⚠️ **Ce qui n'a jamais été mesuré en réel, et que le banc simulé ne peut pas dire** : si
un premier paiement à 0,00 apparaît dans les règlements Mollie (donc dans un
rapprochement comptable), et ce qu'un remboursement fait au mandat. Il n'existe pas de
clé de test ici (décision d'Alexis, 05/09/2026) : le premier vrai changement se fera en
production, sous son œil. **Le banc est un contrat sur notre séquence, pas une preuve du
comportement du prestataire.**

### La trace est un JOURNAL, et elle situe l'acte

`legal_acceptances` portait une ligne par `(sub, doc_slug)`, écrasée à chaque
acceptation : accepter les CGV 2.0 **effaçait** la trace de l'acceptation des CGV
1.0. Une acceptation prouvée par une ligne mutable n'est pas une preuve — c'est le
dernier état d'une preuve.

La source de vérité est désormais **`legal_acceptance_events`** : une ligne par
acceptation, jamais écrasée, avec `context`, `org_id` (l'org de session = le
**payeur**, ADR 0043), `ip` et `user_agent`. **C'est la seule table que les gates
lisent** — le refus `legal_required` comme le statut de `me.legal` — via la ligne la
plus récente de chaque document (`DISTINCT ON`, départagée par `id` : `accepted_at`
vaut `NOW()`, l'horloge de la *transaction*, et les trois documents d'un achat
portent la même).

L'IP et le user-agent viennent de la requête via `client_trace`, posé par
l'adaptateur REST autour du handler (un handler ne voit pas la requête, ADR 0004) ;
l'IP réelle suit la règle unique `client_trace.pick_ip` : le pair TCP, sauf s'il est
un proxy déclaré dans `OTO_PROXIES_DE_CONFIANCE` — alors `CF-Connecting-IP`, sinon
l'adresse la plus à droite de `X-Forwarded-For` qui n'est pas de confiance. Hors requête REST, les deux valent `NULL` — une trace absente reste absente.

**Et la preuve se SORT** : `oto_admin_legal_proof` / `GET /api/admin/users/{sub}/legal/
acceptances` (palier plateforme) rend l'historique entier d'un compte — chaque
acceptation avec sa date, son IP, son agent, son contexte et son org payeuse. Jusqu'au
05/09/2026 ces colonnes étaient écrites et **aucune surface ne les rendait** : en cas de
contestation, la preuve était en base et il fallait un accès à la production pour la
lire (oto#42 lot 2). `me.legal.get` ne la remplace pas — il répond « est-il à jour ? »
et ne garde qu'une ligne par document, ce qui est l'état, pas la preuve.

⚠️ **Deux limites que cette surface affiche au lieu de les masquer.** D'abord, `ip` /
`user_agent` / `context` / `org_id` à `NULL` signifient « aucune trace enregistrée » et
**jamais** « ligne recopiée d'avant le journal » : le DDL pose cette équivalence, mais
elle ne tient que dans un sens — la recopie de la projection laisse bien ces colonnes
nulles, et une acceptation ordinaire arrivée hors requête REST aussi (paragraphe
ci-dessus). L'origine d'une ligne ne se déduit donc pas. Ensuite, `legal_docs.
current_docs()` ne garde que la version **courante** de chaque document : une acceptation
d'une version passée ne peut pas être reliée au texte qu'elle a accepté, et l'`url` reste
`null` plutôt que de pointer le texte d'aujourd'hui. Retrouver le texte d'époque se fait
dans le dépôt du site, pas ici.

### Le pont : `legal_acceptances` devient une projection, et elle a une date de fin

**Rien n'est retiré à la production.** `legal_acceptances` garde sa PK
`(sub, doc_slug)` : le code servi en prod avant ce lot y fait son
`INSERT … ON CONFLICT (sub, doc_slug)`, et prod et preprod partagent la base
(`docs/live-migrations.md`). La lui retirer casserait son
`POST /api/me/legal/accept` — le gate CGU de l'**inscription** — pendant toute la
fenêtre entre le déploiement preprod et le tag. Un journal et cette unicité ne
pouvant pas coexister, le journal est une table **neuve**, et celle-ci devient une
**projection** que le nouveau code continue d'écrire.

Trois propriétés à ne pas confondre avec un fallback :

1. **L'écriture est double, dans la MÊME transaction** — pendant la fenêtre, journal
   et projection ne peuvent pas diverger.
2. **La lecture est unique** : rien ne consulte plus la projection. Si elle disait
   autre chose, aucune réponse ne changerait (un test le fige).
3. **La recopie tourne à CHAQUE boot**, pas une fois. Pendant la fenêtre, la prod
   écrit dans la projection **seule** ; sans reprise, une acceptation donnée en prod
   entre le boot preprod et le tag ne rejoindrait jamais le journal — et comme le
   journal est ce que le gate lit, on redemanderait ses CGU à quelqu'un qui vient de
   les accepter. Le boot du tag rattrape tout ce que la fenêtre a produit.
   Idempotente par anti-jointure sur `(sub, doc, version, accepted_at)`.

⚠️ **Ce pont a une date de démolition : l'issue #507**, à faire au tag **suivant**
celui qui embarque ce lot, avec sa garde — refus d'exécuter tant que la production ne
sert pas le code qui lit le journal. C'est ce drop-là qui sera destructif, et à ce
moment-là il ne cassera plus rien.

Les lignes recopiées de la projection ont leurs quatre satellites à `NULL` :
`context IS NULL` veut dire « acceptation d'avant le journal », surtout pas
« access ». Leur inventer un contexte ferait mentir la trace là où elle sert de
preuve.

## Les droits déclarés : oto-commerce les écrit, le cœur les relit (ADR 0070 §7)

Le cœur relit `org_entitlements` (`access.has_right`) et ne sait pas qui paie.
**Depuis la coupure du cœur (#1097), il n'y écrit plus rien** : le service de
facturation, oto-commerce, pose et retire seul les droits, par l'API de service
(`docs/droits-declares.md` §L'API du commerce).

Ce qui a été retiré avec la coupure :

- la réconciliation interne `billing_droits` (qui dérivait des abonnements, plans
  offerts, contrats et dons d'org des lignes `subscription`, `offered`, `partner`,
  `contract`), à ses trois appels : le démarrage (`reprise_droits`), le travail
  `oto-mcp maintenance droits` du timer quotidien, et les gestes (`confirm`,
  `cancel`, `resume`, le runner, les gestes d'admin, les dons d'org) ;
- ses lectures (`db/billing.subscription_rights_state`, `org_option_comp_bounds`,
  `orgs_with_commercial_rights`).

⚠️ **Les lignes posées avant la coupure restent en base** et ne sont plus réalignées :
une ligne `subscription` expire à la date qu'elle portait (fin de période + 21 j), une
ligne sans échéance (plan offert, contrat reconduit, don perpétuel) vaut jusqu'à ce
qu'oto-commerce la repose ou la retire. Se relit par
`SELECT source, right_key, count(*) FROM org_entitlements GROUP BY 1, 2`.

**La date ferme le droit toute seule** : aucune boucle n'a besoin de passer le jour
venu. `access.entitlements.value_for` filtre en SQL sur l'horloge de la base, et le
droit est **relu à chaque usage** de la clé plateforme (`quotas.exiger_option_payante`,
au palier plateforme de la résolution).

### Ce qui refuse désormais — 409 `billing_moved`

Souscrire, confirmer, changer de moyen de paiement, résilier et reprendre
(`/api/me/billing/{subscribe,confirm,method,method/confirm,cancel,resume}`), offrir ou
retirer un plan (`oto_admin_set_plan`),
déclarer ou résilier un contrat (`oto_admin_set_contract`, `oto_admin_cancel_contract`),
offrir une option qui est une clé du catalogue (`oto_admin_set_option`,
`platform.connector.access_set`). Le refus tombe à la capacité, avant le domaine, et
dit que la facturation est tenue par oto-commerce. Le runner, gelé par
`OTO_BILLING_RUNNER_ENABLED=0`, refuse de toute façon chaque échéance due
(`_refuser_l_echeance`, une erreur au journal). Restent servis : `billing.status`, le
catalogue, les paiements, les factures (PDF), et le webhook Mollie (monté).

⚠️ **Un checkout ouvert avant la coupure et payé après** arrive encore à `confirm`, par
le webhook ou le rattrapage du runner. L'encaissement est gravé (`paid`) et tracé,
puis `confirm` refuse (`billing_moved`) : aucun abonnement, et une ERREUR au journal
nomme l'org et le paiement — l'encaissement orphelin se voit, et se reprend dans
oto-commerce. Le rattrapage du runner le redit à chaque passage pendant sa fenêtre
(48 h). Les fonctions de domaine `cancel`, `resume` et la pose du plafond de sièges
par un plan (`apply_plan_entitlements`) sont retirées.

## L'abonnement réglé hors plateforme (`provider='contract'`)

⚠️ **Depuis la coupure du cœur (#1097), un contrat ne se déclare ni ne se résilie plus
ici** : `oto_admin_set_contract` et `oto_admin_cancel_contract` refusent en 409
`billing_moved`, oto-commerce tient les contrats et leurs droits. Ce qui suit décrit un
contrat DÉJÀ en base, qui reste lu (`billing.status`, fiche admin) sans rien ouvrir.

Un achat réglé AILLEURS (contrat, virement) se déclarait par un admin plateforme.
**Ce n'est pas un don** : c'est un abonnement payé ailleurs.

- **Paramètres** : `plan` (ce qu'il ouvre), `seats` (licences), `unit_amount` (HT, en
  centimes, pour mémoire), `starts_at`, `ends_at` **facultative**, `interval`
  (`month` par défaut), `reference` libre. Ils vivent dans `billing_contracts` ; la
  ligne `org_subscriptions` porte `provider='contract'`, `next_billing_at` NULL.
- **Il ne prélève jamais** : le runner le saute (`_charge_one` → `skipped`), il n'est
  jamais dû, et il n'empêche pas d'archiver l'org (`ABONNEMENT_QUI_PRELEVE`).
- **Droits** : le cœur n'en pose aucun (oto-commerce les tient). Les lignes `contract`
  posées avant la coupure restent jusqu'à leur échéance — aucune pour un contrat
  reconduit tacitement.
- L'org ne résilie pas elle-même un contrat (`contract_subscription`).
- `billing.status` et la fiche admin d'org l'affichent : `provider`, `contract`
  (licences, prix, période, fin, référence), TVA `null` (rien n'est prélevé ici).
- Table neuve : née au démarrage (`CREATE TABLE IF NOT EXISTS`) comme par la révision
  `0008_billing_contracts`, dans n'importe quel ordre.

## Les paliers, et d'où ils viennent

**La grille vit dans `billing.PLANS`, et nulle part ailleurs.** Le dashboard peint
`plans[].amount` tel que servi par l'API, la page d'accueil d'oto.cx n'annonce que
le point d'entrée (« à partir de 19 € ») et renvoie à cette grille, et les CGV n'en
portent pas de copie non plus (décision du 2026-08-29 : elles renvoient à
`https://oto.cx/#pricing`). Une deuxième liste, où qu'elle soit, est un mensonge
en attente.

| plan | HT / mois |
| --- | --- |
| `standard` | 19 € |
| `premium` | **99 €** — 49 € du 2026-07-06 au 2026-08-28, 99 € depuis le 2026-08-29 (#490) |
| `business` | 249 € |
| `enterprise` | 499 € |

Le passage de 49 à 99 € n'a touché personne : au 2026-08-29, un seul abonnement
actif, sur `standard` — aucun sur `premium`, donc aucun effet rétroactif ni
notification. Et rien à changer chez Mollie : il n'y a pas d'objet « plan » ni
« prix » chez le PSP (voir la première section), chaque paiement porte son montant
explicite, calculé à l'échéance par `tax_for_org` sur le HT du palier.

## Le montant débité est un TTC, et le pays le décide (#486)

**Le prix d'un palier est un HORS TAXES.** Jusqu'au 28/08/2026 c'était ce HT qui
partait au PSP : un client « à 19 € » était débité de 19,00 € alors que la TVA
française de 20 % est due par Otomata quoi qu'il arrive. Sur l'encaissement réel,
aucune facture correcte n'était émettable.

Le taux dépend du **pays du payeur** — donc il faut le connaître **avant** de
débiter. D'où l'ordre imposé : identité de facturation d'abord, paiement ensuite.

### La règle (cadre du 28/08/2026)

| client | régime (`vat_scheme`) | taux | mention portée sur la facture |
| --- | --- | --- | --- |
| **France** | `fr_ttc` | 20 % | — |
| **UE hors FR, n° de TVA** | `reverse_charge` | 0 % | autoliquidation, art. 196 dir. 2006/112/CE |
| **UE hors FR, SANS numéro** | *refus* `vat_consumer_unsupported` | — | guichet OSS non en place |
| **hors UE** | `export` | 0 % | hors champ, art. 259-1 du CGI |

Le refus du particulier européen hors France est un **choix**, pas un trou : le
guichet OSS impose de collecter la TVA du pays du client, de la déclarer et de la
reverser. Tant qu'il n'existe pas, encaisser serait une TVA due et non collectée —
on refuse de souscrire plutôt que de facturer faux.

⚠️ **La forme d'un numéro de TVA n'est pas sa validité.** `billing_vat` contrôle le
préfixe du pays et la grammaire nationale ; il ne dit pas que le numéro existe.
**La vérification VIES est un TODO nommé sur #486** — c'est un appel réseau tiers,
hors du lot. D'ici là, un numéro bien formé mais inexistant fait passer un client en
autoliquidation à tort, et la régularisation est manuelle.

⚠️ **La Grèce est `GR` en ISO-3166-1 et `EL` en TVA intracommunautaire.** C'est la
seule divergence des 27, et un contrôle naïf « le numéro commence par le code pays »
refuserait tout numéro grec valide.

⚠️ **Un code pays inconnu est REFUSÉ, jamais traité en export.** « FR » mal tapé
sortirait de l'Union et passerait un client français à 0 % — un manque à gagner
fiscal parfaitement silencieux. D'où la liste ISO-3166-1 en dur.

### Un seul calcul, deux chemins de débit

`billing.tax_for_org` est le **seam unique** : la souscription
(`billing.subscribe`) et l'échéance (`billing_runner._charge_one`) l'appellent tous
les deux. Deux calculs auraient divergé au premier changement de règle, et la
divergence se serait vue sur une facture, pas dans un test — un client ne peut pas
payer 22,80 € le premier mois et 19,00 € les suivants.

Une identité devenue incalculable au moment d'une échéance ne fait **pas** retomber
le runner sur le HT : il rend `blocked:<code>`, ne prélève rien et ne décale pas le
cycle (l'échéance reste due). Un montant approximatif serait pire qu'un mois non
prélevé.

## Une échéance qu'on ne peut pas tirer laisse un ÉTAT (#829)

⚠️ **Le refus ne suffit pas : il doit se voir.** Jusqu'au 02/09/2026, trois branches
de `_charge_one` abandonnaient sans rien écrire — TVA incalculable, palier disparu du
catalogue, mandat perdu. Rien n'était prélevé, mais **rien n'avançait non plus** : ni
le cycle, ni l'impayé, ni la fermeture du droit. Le seul témoin était une `log.error`
dans un journal dont la fenêtre est d'environ 24 h. Passé ce délai, plus aucune
donnée ne disait qu'une org consommait sans payer, **ni depuis quand** — le service
continuait gratuitement, indéfiniment, sans que personne en soit averti.

`org_subscriptions` porte désormais quatre colonnes écrites par `_block` :

| colonne | ce qu'elle dit |
| --- | --- |
| `block_code` | `billing_identity_required` \| `vat_consumer_unsupported` \| `plan_unknown` \| `no_mandate` |
| `block_detail` | le diagnostic (exploitation, pas le payeur) |
| `block_since` | **la date à partir de laquelle on sert sans encaisser** — ne bouge pas d'un tick à l'autre |
| `block_seen_at` | dernier tick qui a reconstaté le blocage (le runner tourne, et voit toujours) |

`block_since` est la colonne qui compte : la réécrire à chaque passage rendrait un
blocage vieux d'un mois indiscernable d'un blocage né il y a une heure. Le motif qui
change fait repartir la date — ce n'est plus le même blocage.

L'état s'efface **dans `schedule_next_billing`** : une échéance encaissée est la
preuve qu'il n'y a plus de blocage, quel qu'ait été le motif. L'effacer côté runner
l'aurait laissé traîner sur tout chemin futur qui fait avancer un cycle sans passer
par lui. Un `comp` (jamais prélevé) le remet à zéro aussi.

Lectures : `db_billing.blocked_subscriptions()` (« qui sert-on sans encaisser, et
depuis quand ? ») et `billing.status` → `block_code`/`block_detail`/`block_since`,
servis au client sur son propre écran.

⚠️ **`block_code` n'est pas `vat_blocked`.** `vat_blocked` est une **prévision**
recalculée à chaque lecture (« au taux d'aujourd'hui, on ne saurait pas quoi
prélever ») ; `block_code` est un **fait daté** (« l'échéance n'a PAS pu être
tirée »). Une identité réparée une heure après l'échéance efface la prévision et
laisse le fait — c'est précisément la différence utile.

**Ce que ce mécanisme ne fait volontairement pas** : il ne prélève pas quand même
(il n'y a pas de montant correct à prendre), et il **ne ferme pas le droit** — le
préavis de 5 jours promis par l'Art 9.4 n'existe toujours pas (#768), et suspendre
sans avoir prévenu serait pire que le défaut réparé ici. Au bout de combien de temps
un blocage devient un impayé reste une **décision produit**, pas un réglage.

### Les autres abandons muets du même tick, corrigés en même temps

- une ligne non terminale **sans référence PSP** repartait en file à chaque tick sans
  même un log : elle est irréconciliable, et le dit ;
- un `confirm` de rattrapage qui échoue était un `warning` — donc sous le seuil
  Sentry — alors qu'un payeur est **débité sans droits ouverts** : c'est une `error` ;
- **un tick entier qui lève** était un `warning`. Il arrête pourtant tout le cycle
  (échéances, dunning, réconciliation, factures) sans rien changer d'observable :
  c'était le plus silencieux des arrêts de ce module.

### Ce qui est journalisé, et ce qui ne l'est PAS

`billing_payments.amount` porte ce qui a **réellement** été passé au PSP, donc le
TTC ; `amount_ht`, `vat_rate_bps`, `vat_amount`, `country_code` et `vat_scheme`
figent la décomposition **à l'instant du débit** — elle ne suit pas un déménagement
ultérieur de l'org.

⚠️ **Les deux encaissements du 25/08 ne sont pas réécrits.** Ils ont réellement été
débités de 19,00 € sans TVA, et `amount_ht IS NULL` est ce qui les distingue d'une
ligne calculée. **Un `null` ici veut dire « ligne d'avant la règle », jamais
« zéro »** — un zéro affirmerait une exonération qui n'a pas eu lieu.

Le taux est en **points de base** (`vat_rate_bps`, 2000 = 20 %) et jamais en
flottant : il sert à calculer des centimes, et une colonne `NUMERIC` ressortirait en
`Decimal`, que le sérialiseur JSON des réponses refuse — 500 à la lecture.

### Ce que voient les surfaces

`subscribe` rend la décomposition **avant** d'envoyer sur la page hébergée (sinon le
payeur découvre le TTC chez Mollie) ; `confirm` la relit du journal ; `status`
annonce le TTC de la **prochaine** échéance, dérivé de l'identité courante, et pose
`vat_blocked` quand il ne peut pas le calculer — un abonnement `active` avec un
`vat_blocked` posé signale une échéance que le runner ne pourra pas prélever.
`me.billing.identity` (GET/PUT `/api/me/billing/identity`) lit et pose la fiche, et
rend toujours `missing` : la même liste que celle nommée par le refus
`billing_identity_required`.

⚠️ **Sur un abonnement OFFERT (`comp`), les champs de TVA de `status` valent tous
`null`, `vat_blocked` compris** : rien n'y sera jamais prélevé, donc il n'y a ni
TTC à annoncer ni alerte à lever — et poser `vat_blocked` sur une org offerte
sans identité serait une fausse alerte sur l'écran dont c'est justement le rôle
de signaler les échéances en danger.

⚠️ **Enregistrer une identité et pouvoir souscrire sont deux choses.** L'identité
d'une société allemande est parfaitement valide et s'enregistre (`missing` vide) ;
c'est le DÉBIT qui est refusé sans numéro de TVA. `vat_blocked` prévient donc
l'écran avant le tunnel, plutôt que de faire remplir un formulaire pour refuser
au paiement.

⚠️ **Point de droit resté ouvert** (conseil, pas code) : le « hors UE = 0 % » du
cadre ne distingue pas le professionnel du particulier, alors que les services
électroniques rendus à un particulier peuvent relever du pays de consommation. La
règle appliquée est celle du cadre.

## Les factures (#488) — nous traçons, plus personne n'émet tout seul

⚠️ **Depuis le 2026-09-09, la plateforme ne crée plus RIEN chez Pennylane** : ni
facture, ni avoir, ni fiche client. Décision d'Alexis, appliquée le jour même. Ce
qui suit décrit donc deux choses : ce qui continue (la trace, la liste, le PDF), et
ce qui a été retiré — pas débranché.

**Pourquoi.** Trois faits, dans cet ordre :

1. **le doublon.** Une facture d'une org cliente existait DÉJÀ chez Pennylane,
   créée à la main ; l'émission automatique en a produit une seconde ;
2. **les données de facturation peuvent être fausses** — identité du client,
   adresse. Rien ne les vérifie avant qu'elles soient gravées ;
3. **une facture finalisée n'est pas rattrapable.** C'est une pièce comptable : elle
   ne se supprime pas, elle ne se corrige que par un avoir. Un document faux émis
   tout seul coûte deux pièces et une explication au client.

L'ADR 0043 nommait déjà les deux formes possibles — « émises par Otomata (Pennylane
ou **manuel au début**) » : la coupure revient à la seconde, elle ne la contredit
pas.

**Ce qui a été retiré** (commit du 2026-09-09, `Changelog: removed`) : le seam
fournisseur `billing_invoices/pennylane.py` en entier — client, rapprochement,
création, finalisation, liaison d'avoir, téléchargement du PDF — et la composition
du document dans `billing_invoices/emission.py`. Un appel qu'on se contente de ne
plus faire revient au premier nettoyage qui « répare » un appel manquant ; il est
donc parti avec son module. Tout est dans l'historique git si la reprise en main
veut s'en servir.

### Ce que devient un encaissement : `held`

Dès qu'une ligne de `billing_payments` passe à `paid`, une ligne de
`billing_invoices` naît et passe aussitôt en **`held`** — le troisième statut,
ajouté ce jour-là — avec `error_code = 'manual_issuance_required'` et une cause qui
DATE la décision. Elle porte l'org, la ligne de journal, la référence `tr_…` du
paiement ; pour un remboursement, elle porte en plus le montant remboursé en
négatif (le webhook Mollie qui l'a vu ne repasse pas, rien d'autre ne le porte).

| chemin | quand |
| --- | --- |
| `billing.confirm` | retour navigateur, webhook d'un premier paiement, rattrapage |
| `billing.process_webhook` | échéance encaissée, remboursement constaté |
| `billing_runner` (balayage, en fin de tick) | **le filet** — tout ce que les deux premiers ont raté |

Le balayage n'est pas une redondance de confort : c'est lui qui rend vraie la phrase
**« jamais un paiement sans trace de facture »**. Les deux appels en ligne ne font
que raccourcir le délai.

⚠️ **`held` est ce qui distingue un ARRÊT d'une PANNE.** Le seul levier qui existait
avant lui était de retirer la clé plateforme : chaque tick horaire aurait alors
journalisé une erreur par paiement, sur une file qui ne se vide jamais — une coupure
qui hurle, et qu'on aurait fini par ignorer. Une ligne tenue sort du prédicat de la
file (`pending_billing_invoices` ne retient que `status='pending'`) : elle reste
VISIBLE dans le tableau des factures du client, et silencieuse. Rien n'a été ajouté
au balayage pour l'écarter, donc rien ne sera à retirer le jour où l'émission
reviendra.

Les trois statuts se lisent donc ainsi : `issued` (document réel — d'avant la
coupure, ou posé par une main), `held` (tracé, à poser), `pending` (une tentative
d'émission d'AVANT la coupure avait échoué ; le premier tick du runner les tient à
leur tour, et la file converge vers le vide).

⚠️ **`attempts` n'est pas incrémenté sur une ligne tenue** : il compte les appels
réellement passés au fournisseur. L'incrémenter ferait lire un fournisseur en panne
là où il y a une décision.

⚠️ **La trace ne fait jamais échouer un paiement.** Laisser une exception remonter
dans `confirm` rendrait une erreur au payeur **sur un paiement réussi** — la faute
exacte de #493, celle qui a fait repayer un client. Elle est absorbée, et la trace
est déjà écrite quand elle l'est.

### Ce qui reste à trancher : où se valide la facture

Deux formes étaient en balance le 2026-09-09 : (a) un **brouillon** chez Pennylane
validé à la main, (b) **rien** chez Pennylane, la facture restant « à valider »
côté oto. La coupure n'engage ni l'une ni l'autre — mais elle ferme un piège qui
condamnait (a) : le balayage horaire FINALISAIT tout brouillon qu'il retrouvait
(`_est_brouillon` → `finalize`), donc un brouillon posé pour relecture humaine
aurait été gravé dans l'heure par notre propre runner. Ce chemin est parti avec le
seam ; il ne pourra pas revenir par inadvertance.

Ce qu'il faut pour reprendre la main, et qui est là : la ligne `held` avec son
montant et son paiement, `mark_billing_invoice_issued` et `set_billing_invoice_pdf`
côté store, la liste servie et la route PDF.

### Le client Pennylane se pose, il ne se devine pas (#917)

Ce doublon n'était pas un défaut d'heuristique : le seam retiré
rapprochait le client par une **référence frappée par oto** (`oto-org-<id>`), qu'un
client créé à la main chez le comptable ne porte pas — « introuvable », donc créé une
seconde fois, mécaniquement, même TVA sur les deux fiches. Décision d'Alexis
(2026-09-09) : **pas de rapprochement** par TVA ni SIREN (sur des pièces comptables,
une fusion à tort est pire qu'un doublon visible). L'identifiant est **posé à la
main** par un admin plateforme, et le code le respecte.

Ce qui est en place :

- **la colonne** `billing_identities.pennylane_customer_id` (BIGINT, NULL = aucun
  client désigné), additive, posée par `_init.py` sur la base partagée ;
- **la surface admin** `GET/PUT /api/admin/orgs/{org_id}/billing-identity`
  (`capabilities/billing_identity_admin.py`, `PLATFORM_ADMIN`) : la fiche entière
  plus l'id, dans le même geste — le formulaire admin est prérempli et reposte tout,
  `pennylane_customer_id` omis RETIRE la désignation. Côté dashboard : la carte
  « identité de facturation » de la fiche org admin (`/platform/orgs/:id`) ;
- **le piège fermé** : `me.billing.identity.set` (formulaire côté org) remplace la
  fiche en bloc et **ne connaît pas** la colonne — `upsert_billing_identity` ne la
  nomme pas dans son `SET`. Un org_admin qui resauvegarde son formulaire laisse l'id
  intact. `tests/test_billing_identity_pennylane_917.py` le prouve **par sa chute** :
  mettre la colonne dans le `SET` rougit le test. L'org ne voit pas l'id (c'est un
  lien vers la compta d'Otomata, pas une donnée du client).

Ce qui reste, et où : **la lecture de l'id par le chemin d'émission**. Aujourd'hui
aucun code ne crée ni ne cherche de client chez Pennylane, la règle « renseigné ⇒
on l'utilise, absent ⇒ on n'émet pas » n'a donc pas encore de seam où se brancher.
Elle se branche dans la capacité « émettre maintenant » quand elle naîtra (forme
(b), tranchée le 2026-09-09 au soir) : première branche `pennylane_customer_id`
renseigné ⇒ l'utiliser, **ne rien chercher ni créer** ; absent ⇒ la ligne reste
`held` avec le motif `pennylane_customer_required`, jamais une création. Le manque
doit rester visible dans la liste à valider, pas comblé en silence.

### La clé de la compta d'Otomata — plus lue par le backend

⚠️ **`OTO_PENNYLANE_API_KEY` n'a plus aucun lecteur dans ce dépôt** depuis le
2026-09-09 : elle est partie avec le seam. Elle reste posée dans l'environnement des
déploiements ; la retirer est un geste d'infrastructure, pas de code.

Ce qu'il faut savoir le jour où elle resservira, parce que c'est ce qui se
redécouvre le plus cher : le connecteur `pennylane` du catalogue est
**clé-par-utilisateur** (`auth_modes = {byo_user, byo_org}`) — chacun pose la
sienne sur `manage.oto.cx/api-keys` et ne voit que sa propre compta. Cette clé-là ne
peut pas facturer un abonnement Otomata, et `access.resolve_api_key` résout de toute
façon dans le contexte de l'appelant, que le webhook du PSP et la boucle de fond
n'ont pas. La clé de facturation vient donc de l'**environnement du process**,
exactement comme `MOLLIE_API_KEY` : deux comptes fournisseurs d'Otomata, résolus au
boot depuis **Scaleway Secret Manager**, jamais SOPS, jamais le coffre. La ranger au
coffre en scope `PLATFORM` aurait fait entrer la compta d'Otomata dans la mécanique
de partage du marketplace (`platform_grant`, `share_down`) — un mécanisme conçu pour
PRÊTER une clé, sur la seule clé qu'on ne prêtera jamais.

### La numérotation appartient à Pennylane

`billing_invoices` est une table de **trace**, pas un registre de factures : le
document, son numéro (`invoice_number`) et sa valeur probante vivent chez Pennylane.
Numéroter ici aurait créé une **seconde série** sur les mêmes recettes — deux séries
concurrentes est exactement ce qu'un contrôle reproche. Une ligne `held` n'a donc
pas de numéro, et c'est normal : un numéro n'existe pas avant le document.

### Les codes de TVA envoyés à Pennylane

| régime (`vat_scheme`) | code Pennylane | mention portée sur le PDF |
| --- | --- | --- |
| `fr_ttc` | `FR_200` (dérivé du taux : 2000 points de base → `FR_200`) | — |
| `reverse_charge` | `crossborder` | autoliquidation, art. 196 dir. 2006/112/CE |
| `export` | `extracom` | hors champ, art. 259-1 du CGI |

⚠️ **Le rapprochement des deux codes à 0 % reste à confirmer avec le conseil.**
L'énumération Pennylane porte `crossborder` (transfrontalier) et `extracom` (hors
Union) sans les définir ; celui retenu est celui des termes. Les deux étant à 0 %,
le **total de la facture est juste dans les deux cas** — c'est le compte de produit
qui dépend du bon code. Aucun contrôle de montant ne peut donc attraper une erreur
ici : seule la relecture du plan comptable le peut. Le rapprochement vivait dans
`billing_invoices/pennylane.py`, retiré le 2026-09-09 — cette table est désormais
le seul endroit où il est écrit, et c'est elle qu'il faudra corriger si le conseil
tranche autrement.

### Où est le PDF

**Dans notre base**, colonne `pdf` (`BYTEA`), téléchargé à l'émission — plus rien ne
l'y range depuis le 2026-09-09, mais tout ce qui y est reste servi. L'URL rendue
par Pennylane (`public_file_url`) **expire en 30 minutes** : la conserver comme
« lien vers la facture » aurait donné un lien mort une demi-heure plus tard, sans
que rien chez nous ne le signale. Elle est gardée comme trace de provenance,
jamais servie.

Deux surfaces, et la seconde n'est pas une capacité :

- `GET /api/me/billing/invoices` — capacité `me.billing.invoices.list` (membre de
  l'org active) : factures et avoirs, avec `pdf_path` quand un fichier existe ;
- `GET /api/me/billing/invoices/{id}/pdf` — route **écrite à la main**
  (`api/billing.py`), parce qu'un handler de capacité rend un `dict` que
  l'adaptateur emballe en JSON : il ne peut pas servir `application/pdf`. Même
  exception, même précédent que l'export ZIP d'un projet. Son autorisation porte sur
  l'org **qui porte la facture**, pas sur l'org active — le lien s'ouvre hors de tout
  contexte de session (un onglet rouvert, une URL collée), où rien ne garantit l'org
  courante. Un id d'une autre org rend **404**, jamais 403 : un « interdit »
  confirmerait l'existence du document.

⚠️ Les deux réponses passent par `api/base.py::_file`, **jamais** par une `Response`
construite à la main : le CORS de ce serveur se pose réponse par réponse, et un 200
qui porte un fichier sans en-tête CORS est refusé par le navigateur alors que le
serveur, lui, a bien répondu (mesuré en production le 2026-09-09).

**Pourquoi la base et non l'objet.** `media_store` ne sert que des images, et il
produit des URL **publiques** — inadapté à une facture. Un document pèse quelques
dizaines de kilo-octets et il en naît un par org et par mois : l'ordre de grandeur
est la centaine de méga-octets par an sur la RDB managée, ce qui ne justifie pas un
second système de stockage aujourd'hui. Le jour où il le justifiera, la bascule est
locale : seuls `set_billing_invoice_pdf` / `get_billing_invoice_pdf` la connaissent.

⚠️ **Aucune lecture ne fait `SELECT *` sur `billing_invoices`.** Le row factory ne
normalise que les dates : des octets remontés dans un dict servi en JSON feraient
une 500 à la sérialisation, sur le chemin le moins emprunté de la surface. Le PDF a
son getter dédié, et la liste ne le voit jamais. Même famille de piège que le
`NUMERIC` qui ressort en `Decimal` (#486).

### Aucun e-mail — la facture se met à disposition

**Depuis le 2026-09-09, la plateforme n'envoie plus d'e-mail de facture.** Elle
sert ce qu'elle a ; le client le télécharge depuis son espace facturation. Aucun destinataire n'est calculé, aucun envoi n'est tenté, aucun renvoi
n'existe — `billing_invoices/mail.py` a été retiré, pas neutralisé.

**Pourquoi.** Une facture est partie **une fois, au créateur de l'org, et
jamais au client** : `billing_identities.billing_email` valait la chaîne vide, donc
fausse au sens de Python, et le repli prenait le premier `org_admin` par ancienneté
— à l'onboarding, c'est Otomata, arrivé quatre minutes avant l'admin du client.
Réparer le repli n'était pas la bonne correction : plusieurs des adresses qu'il
aurait servies n'avaient pas à recevoir ces documents.

⚠️ **`emailed_at` et `email_to` n'ont plus d'écrivain.** Les colonnes restent (la
base est PARTAGÉE prod/preprod : aucun DDL ne se joue pour ça) et la liste continue
de les servir, mais elles datent désormais **les seuls envois d'avant le
2026-09-09** ; tout document postérieur les porte à `NULL`. Elles se lisent comme
une archive, jamais comme un état à rattraper — c'est ce que dit la description
servie du champ dans `capabilities/billing_invoices.py`.

Reste au backlog, non tranché : **qui, dans une org, a le droit de voir et de
télécharger les factures**. Aujourd'hui c'est tout membre de l'org qui porte le
document (`roles.is_org_member`).

### L'avoir sur remboursement

Mollie **n'a pas d'URL propre aux remboursements** : c'est le webhook du **paiement**
qui rappelle quand un remboursement est créé ou change d'état, et le paiement reste
`paid` — c'est `amountRefunded`, absent tant que rien n'est remboursé, qui porte
l'information. `process_webhook` le lit et **trace** une ligne d'avoir `held`, qui
porte le montant remboursé en négatif. Il n'émet plus rien : l'avoir se pose à la
main, comme la facture.

⚠️ **Le montant remboursé n'est écrit qu'à la création de la ligne**, et c'est la
seule occasion : le webhook qui l'a vu ne repasse pas, et Mollie ne porte que l'id
du paiement et son `amountRefunded` cumulé. Sans lui, la pièce posée à la main
aurait perdu son montant. La ventilation HT/TVA d'un remboursement partiel, elle,
n'est plus calculée par le code — elle l'était au prorata du remboursé, la TVA étant
le **reste** et jamais recalculée au taux, sinon la somme des deux ne retombe pas
sur ce qui a été rendu au client. La règle vaut pour la main qui posera l'avoir.

⚠️ **Un seul avoir par paiement** (clé `(paiement, kind)`). Un **second**
remboursement partiel sur le même paiement ne produira donc pas un second document :
le cas est journalisé en `error` en nommant l'écart, et demande un avoir manuel. Une
clé par remboursement supposerait de suivre les objets `refund` de Mollie, que le
webhook ne porte pas.

### Idempotence

`UNIQUE (payment_row_id, kind)` sur `billing_invoices`. C'est la **contrainte** qui
garantit qu'un webhook rejoué ne crée pas une seconde facture — pas une lecture
préalable, que deux webhooks simultanés franchiraient tous les deux. Côté Pennylane,
la référence externe `oto-payment-<tr_…>` (et `oto-refund-<tr_…>`) jouait le même
rôle — elle est partie avec le seam, et une émission manuelle a tout intérêt à la
reprendre : c'est elle qui empêche un second document sur le même encaissement.

⚠️ La colonne s'appelle `payment_row_id` et non `payment_id` : elle porte l'id de la
**ligne de journal** (`billing_payments.id`), alors que `billing_payments.payment_id`
porte, lui, l'identifiant Mollie `tr_…`. Le nom dit lequel est lequel.

### Les deux encaissements du 25/08/2026 ne sont PAS facturés automatiquement

Ils ont été débités du **HT sans TVA**, avant que la règle n'existe, et `amount_ht
IS NULL` est ce qui les distingue (§#486). **Sans décomposition fiscale, aucune
facture conforme n'est calculable** : en fabriquer une reviendrait à inventer une TVA
qui n'a jamais été collectée. La file de reprise les exclut par ce prédicat, et
aucune ligne `pending` n'est créée pour eux — elle sonnerait pour toujours.

**Le geste manuel, à faire une fois** (Alexis, dans l'interface Pennylane) :

1. retrouver ou créer le client de l'org payeuse, avec la référence externe
   `oto-org-<id>` — c'est la clé sur laquelle les factures suivantes se
   rapprocheront, et deux fiches client pour la même org sépareraient l'historique ;
2. émettre **une facture par encaissement**, datée du jour du débit, d'un montant
   **TTC de 19,00 €** — le montant réellement pris. Il faut donc le traiter comme un
   TTC et faire ressortir la TVA à l'intérieur (15,83 € HT + 3,17 € de TVA à 20 %),
   et non ajouter 20 % par-dessus : le client n'a jamais payé 22,80 €, et une facture
   qui l'affirmerait serait fausse ;
3. l'un des deux est le **double débit** de l'incident : il appelle un remboursement,
   donc un avoir, et non une facture à conserver. Trancher l'ordre avec le
   remboursement (cf. §« L'incident du 2026-08-25 », dont le remboursement et la
   révocation du mandat orphelin restent dus) ;
4. rien à écrire dans `billing_invoices` : la table trace ce que le serveur a émis.
   Y poser une ligne à la main ferait croire à une émission automatique.

## Le mandat est une COURSE, pas un état

> ⚠️ Histoire : depuis la coupure du cœur (#1097), `confirm` n'attend plus de mandat —
> un encaissement est gravé puis refusé (`billing_moved`). Ce qui suit décrit #493.

Le mandat réutilisable ne naît pas avec l'encaissement : chez Mollie il apparaît
une à cinq minutes plus tard. `confirm` le constatait absent 1,4 s après le paiement
et rendait un **409 définitif** (`no_mandate`) — un échec annoncé sur un paiement
réussi.

Depuis #493, la fenêtre `billing.PENDING_WINDOW` (30 min, mesurée depuis le `paidAt`
du PSP, pas depuis l'ouverture du checkout) sépare les deux lectures :

- **dans la fenêtre** → `{"status": "pending_mandate", "payment_status": "paid",
  "retry_after": …}` en **200**. L'argent est pris, l'accès s'ouvrira seul ; le
  client re-sonde et ne repropose surtout pas de payer.
- **au-delà** → `no_mandate` (409), le code historique, dont c'est le seul sens
  vrai : encaissé, récurrence impossible, reprise manuelle. `logger.error` posé.

⚠️ **Un paiement RÉUSSI ne produit jamais de code d'erreur sur `confirm`.** Les
branches d'avancement sont toutes des 200 discriminées par `status` ; `confirm` ne
refuse que lorsque l'APPEL est fautif (`unknown_payment`, `no_pending_subscription`).

## Une org qui prélève ne s'archive pas (#400)

`archive_org` est un soft-delete : l'org sort de tous les listings et plus personne ne peut
en résilier l'abonnement, qui continuait d'être prélevé. Décision du 21/09/2026 (piste 1
de l'issue) : **l'archivage est refusé** — `409 org_has_active_subscription`, sur les deux
portes (`DELETE /api/orgs/{id}` / `oto_org op=archive` et la console admin
`org.admin.archive`), avec un message qui dit de résilier d'abord. Le refus est déclaré
(`Capability.errors`) donc publié dans `/openapi.json`.

- **Ce qui « prélève »** (`db.billing.ABONNEMENT_QUI_PRELEVE`) : `active` ou `past_due`,
  ET ni résilié à fin de période (`canceled_at` posé : le statut reste `active` jusqu'à
  `current_period_end` mais `next_billing_at` est coupé), ET ni offert (`provider='comp'` :
  jamais tiré, aucun PSP derrière — l'utilisateur n'a rien à « résilier d'abord »).
- **Atomique.** `archive_org` prend `FOR NO KEY UPDATE` sur la ligne `orgs`, compte les
  abonnements, puis pose `archived_at` — une transaction. Ce qui fait entrer un abonnement
  dans « prélève » (`upsert_org_subscription`, `set_subscription_status` vers
  `active`/`past_due`) prend `FOR SHARE` sur la même ligne : l'une attend l'autre. Ordre
  perdant possible : l'archivage passe avant la souscription — l'org archivée porte alors
  un abonnement, que le filtre ci-dessous ne tirera pas.
- **Le filet, pour les orgs DÉJÀ archivées.** `due_subscriptions` et la relecture sous
  verrou de `reserver_echeance` partagent `_ECHEANCE_DUE`, qui exclut toute org archivée :
  aucun prélèvement, l'abonnement reste `active` chez nous comme chez Mollie, l'échéance
  échue est prélevée à la désarchivation. Les orgs archivées AVANT ce refus qui portent
  encore un abonnement qui prélève se dénombrent par
  `SELECT count(*) FROM orgs o JOIN org_subscriptions s ON s.org_id = o.id WHERE
  o.archived_at IS NOT NULL AND s.status IN ('active','past_due') AND s.canceled_at IS NULL
  AND s.provider <> 'comp'` ; leur sort (résilier chez Mollie, ou désarchiver) reste à
  arbitrer.

## Trois invariants que le code tient maintenant

1. **L'encaissement se grave avant tout le reste.** `status='paid'` est écrit dès
   que le PSP le dit — avant le mandat, avant le plan, avant le miroir. Le journal
   doit dire ce que le PSP a fait, pas ce que nous avons su en faire. Il restait
   `open` sur un paiement réellement débité, ce qui a rendu l'enquête du 25/08
   trompeuse : **ne pas lire le statut du journal comme l'état réel chez le PSP**
   pour les lignes antérieures au correctif.
2. **Une seule souscription en vol à la fois.** `subscribe` refuse (`payment_pending`,
   409) tant qu'un `initial` de moins de 30 min n'a pas *définitivement* échoué —
   `open` comme `paid`. Corollaire assumé : résilier puis re-souscrire dans la
   demi-heure est refusé le temps que la fenêtre s'écoule, le refus nommant le
   paiement qui occupe la place.
3. **Un seul customer Mollie par org.** Il se lit sur le miroir quand il existe,
   **sinon sur le journal** (`billing_payments.customer_id`) : le miroir n'est posé
   qu'à `confirm`, donc au deuxième clic il n'y a encore rien à relire. C'est là
   qu'un second customer naissait, avec son propre mandat — celui que le rejeu MIT
   ne tirerait jamais.

## Qui confirme, et comment il sait QUEL paiement

Quatre appelants, un seul verbe :

| appelant | connaît le `payment_ref` ? |
| --- | --- |
| **webhook** Mollie | oui, c'est celui qu'il vient de recevoir |
| **retour navigateur** | oui depuis #493 — `?payment_ref=tr_…` est posé sur l'URL de retour |
| **polling** du dashboard | non → le plus récent non conclu (correct pour lui) |
| **`billing_runner`** | oui, il l'a lu dans le journal |

Mollie n'ajoute rien à `redirectUrl`, et cette URL se fixe à la **création** du
paiement — où l'id n'existe pas encore. D'où la ré-écriture juste après
(`mollie_client.update_payment`, paiement encore `open`). Un refus de Mollie n'est
pas fatal : on retombe sur « le plus récent », avec un `logger.warning`.

## Les deux files de reprise du runner

Un encaissement journalisé `paid` est **terminal** : il quitte
`open_billing_payments`. Le `billing_runner` a donc **deux** files, et pas une :

- `open_billing_payments()` — les paiements en vol (checkout fermé post-paiement,
  prélèvement SEPA qui met des jours, TTL 48 h du premier paiement) ;
- `paid_initials_awaiting_subscription()` — les encaissements dont l'abonnement
  n'est **pas** ouvert. Sans elle, un payeur qui ferme son onglet pendant la course
  au mandat resterait débité et sans droits, personne ne re-interrogeant le mandat.

## L'incident du 2026-08-25 (38 € pour un abonnement à 19 €)

Premier et seul encaissement réel de la plateforme à cette date. Chronologie
vérifiée en base, rejouée par `tests/test_billing_double_debit_493.py` :

| heure (UTC) | fait |
| --- | --- |
| 10:29:44 | l'org ouvre un checkout |
| 10:31:0x | elle paie ; Mollie encaisse |
| 10:31:05 | retour navigateur **1,4 s** plus tard : `valid_mandate()` vide → 409, et `status='paid'` jamais écrit |
| 10:31:44 | le payeur, qui a vu un échec, reclique → **second checkout ET second customer** |
| 10:36 | le mandat apparaît ; le 2ᵉ paiement est encaissé lui aussi |

L'enchaînement n'est pas exotique — payer, voir un échec, recliquer : c'est le
chemin nominal. **Restent hors code, décision du responsable** : le remboursement
du 2ᵉ paiement et la révocation du mandat orphelin né du second customer.

## Où c'est écrit

`billing.py` (le cycle), `billing_vat.py` (la règle de TVA, **pure** : ni base,
ni réseau, ni horloge), `db/billing.py` (les trois tables + les files),
`billing_invoices/` (le paquet de la FACTURE : `pennylane.py` = le seam fournisseur
et la clé de la compta d'Otomata, `emission.py` = le cycle facture/avoir/reprise —
et **aucun e-mail**), `db/billing_invoices.py` (la table
de trace), `capabilities/billing_invoices.py` (la liste) et la route de
téléchargement du PDF dans `api/billing.py`,
`mollie_client.py` (la surface PSP), `capabilities/billing.py` (les six capacités
REST-only — payer est un acte humain, pas d'URL de paiement dans un contexte LLM),
`capabilities/billing_identity.py` (l'identité de facturation, même régime),
`billing_consent.py` + `legal_docs.py` (le consentement d'achat et la source de
vérité des documents), `db/legal.py` (le journal, la projection transitoire et le
pont), `capabilities/me_legal.py` (l'acceptation, REST-only),
`billing_runner.py` (échéances, dunning, sweeps, reprises — **le balayage des
factures y est le dernier geste du tick**). La surface entière est gatée par
`OTO_BILLING_ENABLED=1` (dark launch ADR 0043) et la boucle de fond par
`OTO_BILLING_RUNNER_ENABLED` (défaut : allumée dès que le billing l'est) — **et en
production seulement** : la préprod partage la base et porte la clé Mollie de TEST, elle
ne compose pas la boucle quel que soit l'interrupteur (`boucles_de_fond.py`, 10/09/2026 :
tirée par la préprod, une échéance live échouait et la relance repoussait le vrai
prélèvement de trois jours). Deux processus de PRODUCTION à la fois (bascule bleu/vert,
ancienne unité simple relancée) ne tirent qu'une fois la même échéance : elle est
**réservée** avant tout appel au prestataire (`db/billing_reservation.py` — verrou
consultatif de session sur une connexion hors pool, puis relecture de l'échéance sous
verrou), et c'est la ligne relue qui est tirée. La clé d'idempotence se **dérive de la
ligne**, `org<id>-<période>-d<instant dû>` (`billing_runner._cle_echeance`) — elle
portait le numéro de tentative compté sur le journal, qui voyait la tentative en vol de
l'autre processus et lui donnait une autre clé, donc un second débit réel. C'est le filet
si la réservation manque, dans l'heure où Mollie garde une clé ; une réponse jumelle (409
ou paiement déjà journalisé) ne touche ni au cycle ni à l'impayé. Banc :
`tests/test_billing_echeance_deux_processus.py`, deux vrais processus sur une vraie base
(10/09/2026). **Un paiement n'ouvre de droit que s'il est réel et constaté par la
production** (`billing_mode.py`, même jour) : `confirm` et le changement de moyen lisent le
`mode` rendu par Mollie, avant toute écriture ; en production un paiement `test` est refusé
(`payment_mode_mismatch`), et hors production aucun paiement n'ouvre de droit
(`billing_not_production`) — la préprod partage la base, un droit qu'elle ouvrirait serait
réel. Les deux
clés fournisseur — `MOLLIE_API_KEY` (le PSP) et `OTO_PENNYLANE_API_KEY` (la compta
d'Otomata) — viennent de l'**env du process** (Scaleway Secret Manager au boot),
jamais de SOPS ni du coffre.
