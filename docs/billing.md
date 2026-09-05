# Facturation par org (ADR 0043) — le modèle, la TVA, le consentement, les factures, et le double débit du 25/08

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
`legal_docs.CURRENT_DOCS` rouvre le gate ; une acceptation périmée ne vaut pas.

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

- **Elle mord dans le seam** : `db.has_option_comp` ignore une ligne échue, donc les
  surfaces d'entitlement tombent d'accord sans qu'aucune connaisse la règle. Une
  échéance qu'aucun chemin n'applique serait pire que pas d'échéance.
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
- **Mois en cours SEULEMENT** : la purge du journal ne garde qu'environ 35 jours (la
  politique en annonce 90 — écart corrigé le 2026-08-28). Le mois précédent n'est pas
  calculable ; ne pas bâtir de comparaison dessus.

### « Cette org a-t-elle l'option » : une question, trois réponses (corrigé)

Trois fonctions y répondaient avec trois règles. Conséquence mesurée : **une org qui
PAYAIT s'affichait « non souscrite »** dans son cockpit d'activation, dont la lecture
ne regardait que le don admin et jamais le plan. La moitié org du seam est désormais
nommée — `access.org_has_option` — et `capabilities/connectors/activation` l'appelle.
`access.has_option` reste le seam complet (comp user > comp org > plan) ;
`access.views.option_open` reste au-dessus (il croise avec le BYO). Un nouveau chemin
passe par l'un des trois, **jamais par les sources**.

### Ce qui ne demande PAS de consentement

Un **abonnement offert** (`admin_set_plan`, `comp`) : rien n'y est vendu ni débité.
Une **échéance** : le consentement a été donné à la souscription, `billing_runner`
ne le rejoue pas — `_charge_one` ne prend d'ailleurs pas de `sub`, et un test le
fige.

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
l'IP réelle se lit `CF-Connecting-IP` > **premier** hop de `X-Forwarded-For` >
socket. Hors requête REST, les deux valent `NULL` — une trace absente reste absente.

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
CURRENT_DOCS` ne garde que la version **courante** de chaque document : une acceptation
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

## Les factures (#488) — Pennylane émet, nous traçons

**Chaque encaissement produit une facture.** Jusqu'au 28/08/2026 la plateforme
débitait sans jamais émettre de document : un client professionnel — a fortiori un
cabinet comptable — n'avait ni facture ni PDF, et le tableau de bord ne montrait
qu'un journal de tentatives (`billing.payments`), qui n'est ni une facture ni un
reçu. Mollie n'y pouvait rien : il facture Otomata pour ses frais, il n'émet aucun
document au client final.

### Ce qui déclenche une facture : l'ENCAISSEMENT, pas l'abonnement

Dès qu'une ligne de `billing_payments` passe à `paid`, un document est dû — que le
mandat soit né ou non, que le miroir d'abonnement soit posé ou non. Faire dépendre
la facture de l'ouverture des droits laisserait sans document exactement le cas du
25/08 : de l'argent pris, un abonnement pas encore ouvert.

| chemin | quand |
| --- | --- |
| `billing.confirm` | retour navigateur, webhook d'un premier paiement, rattrapage |
| `billing.process_webhook` | échéance dont Mollie annonce l'encaissement |
| `billing_runner` (balayage, en fin de tick) | **le filet** — tout ce que les deux premiers ont raté |

Le balayage n'est pas une redondance de confort : c'est lui qui rend vraie la phrase
**« jamais un paiement sans trace de facture »**. Les deux appels en ligne ne font
que raccourcir le délai entre le paiement et le document.

⚠️ **L'émission ne fait jamais échouer un paiement.** Un appel Pennylane peut
refuser, expirer, ou n'avoir pas de clé ; laisser l'exception remonter dans
`confirm` rendrait une erreur au payeur **sur un paiement réussi** — la faute exacte
de #493, celle qui a fait repayer un client. Elle est donc absorbée, et ce n'est pas
un repli silencieux : la tentative est écrite (`billing_invoices`, `status='pending'`
— c'est le `invoice_pending` de l'issue), sa cause est **nommée** (`error_code`),
elle est journalisée en `error`, et la reprise horaire la rejoue jusqu'à ce qu'elle
aboutisse. **Un `pending` qui dure est un incident visible, pas un oubli.**

### Qui émet, avec quelle clé

**Pennylane**, sur la comptabilité d'**Otomata** — et c'est un point à ne pas
confondre. Le connecteur `pennylane` du catalogue est **clé-par-utilisateur**
(`auth_modes = {byo_user, byo_org}`) : chacun pose sa clé sur
`manage.oto.cx/api-keys` et ne voit que sa propre compta. Cette clé-là ne peut pas
servir ici, et `access.resolve_api_key` résout de toute façon dans le contexte de
l'appelant — que le webhook du PSP et la boucle de fond n'ont pas.

La clé de facturation vient donc de l'**environnement du process**,
`OTO_PENNYLANE_API_KEY`, exactement comme `MOLLIE_API_KEY` : deux comptes
fournisseurs d'Otomata, résolus au boot depuis **Scaleway Secret Manager**, jamais
SOPS, jamais le coffre. La ranger au coffre en scope `PLATFORM` aurait fait entrer
la compta d'Otomata dans la mécanique de partage du marketplace (`platform_grant`,
`share_down`) — un mécanisme conçu pour PRÊTER une clé, sur la seule clé qu'on ne
prêtera jamais.

⚠️ **Clé absente ⟹ `pennylane_unconfigured`**, journalisé sur la ligne, avec le nom
de la variable à poser. Aucun encaissement n'est perdu pour autant.

### La numérotation appartient à Pennylane

`billing_invoices` est une table de **trace**, pas un registre de factures : le
document, son numéro (`invoice_number`) et sa valeur probante vivent chez Pennylane.
Numéroter ici aurait créé une **seconde série** sur les mêmes recettes — deux séries
concurrentes est exactement ce qu'un contrôle reproche.

### Le geste, dans l'ordre

1. **le client** — retrouvé chez Pennylane par sa référence externe `oto-org-<id>`
   (filtre serveur, un seul appel), sinon créé depuis `billing_identities` : raison
   sociale, adresse, pays. Le **n° de TVA intracommunautaire** est posé juste après
   (`update_customer`) : sans lui, la mention d'autoliquidation ne vaut rien ;
2. **le brouillon** — une ligne libre « Abonnement `<palier>` — période du … au … »,
   prix unitaire HT en décimal, code de TVA dérivé du régime, et la mention légale
   en texte libre imprimé sur le PDF ;
3. **le contrôle** — le total du brouillon doit être **celui qui a été débité**.
   C'est la seule raison d'être du passage par un brouillon, et le seul contrôle
   capable d'attraper un code de TVA qui ferait calculer 20 % là où le régime est à
   0 %. Un écart ⟹ **rien n'est finalisé** (`amount_mismatch`) : une facture
   finalisée ne se supprime plus, elle ne se corrige que par un avoir ;
4. **la finalisation** — c'est elle qui donne le numéro et le PDF. Le document servi
   n'est jamais un brouillon ; sa date est celle de l'encaissement et son échéance
   le même jour (il constate un règlement déjà fait, il n'en appelle aucun).

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
ici : seule la relecture du plan comptable le peut. C'est une ligne à changer
(`billing_invoices/pennylane.py`).

### Où est le PDF

**Dans notre base**, colonne `pdf` (`BYTEA`), téléchargé à l'émission. L'URL rendue
par Pennylane (`public_file_url`) **expire en 30 minutes** : la conserver comme
« lien vers la facture » aurait donné un lien mort une demi-heure plus tard — dans
un e-mail, on ne s'en apercevrait qu'en le voyant échouer chez le client. Elle est
gardée comme trace de provenance, jamais servie.

Deux surfaces, et la seconde n'est pas une capacité :

- `GET /api/me/billing/invoices` — capacité `me.billing.invoices.list` (membre de
  l'org active) : factures et avoirs, avec `pdf_path` quand un fichier existe ;
- `GET /api/me/billing/invoices/{id}/pdf` — route **écrite à la main**
  (`api/billing.py`), parce qu'un handler de capacité rend un `dict` que
  l'adaptateur emballe en JSON : il ne peut pas servir `application/pdf`. Même
  exception, même précédent que l'export ZIP d'un projet. Son autorisation porte sur
  l'org **qui porte la facture**, pas sur l'org active — ce lien s'ouvre depuis un
  e-mail, où rien ne garantit l'org de session. Un id d'une autre org rend **404**,
  jamais 403 : un « interdit » confirmerait l'existence du document.

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

### L'e-mail — et pourquoi le PDF n'y est PAS joint

Le document part au contact de facturation (`billing_identities.billing_email`,
sinon le premier org_admin par ancienneté) via le relais transactionnel
`otomata-mailer`. Best-effort : un e-mail non parti ne remet rien en cause, il se lit
à `emailed_at IS NULL`.

⚠️ **Le relais n'accepte pas de pièce jointe** : `POST mailer.oto.zone/api/send` ne
prend que `{from, to, cc, replyTo, subject, html}` et son `sendMail` ne passe aucun
`attachments` à nodemailer (`otomata-tech/otomata-auth-mailer`, `src/send.ts` +
`src/index.ts`). L'e-mail porte donc le numéro, les montants, la période et un lien
vers l'espace facturation. **Joindre le PDF demande une décision**, pas une
correction au passage : ouvrir `attachments` sur le mailer (autre dépôt), ou laisser
**Pennylane** l'envoyer lui-même (`POST customer_invoices/{id}/send_by_email`, déjà
exposé par oto-core) au prix d'un expéditeur et d'un gabarit qui ne sont pas les
nôtres.

### L'avoir sur remboursement

Mollie **n'a pas d'URL propre aux remboursements** : c'est le webhook du **paiement**
qui rappelle quand un remboursement est créé ou change d'état, et le paiement reste
`paid` — c'est `amountRefunded`, absent tant que rien n'est remboursé, qui porte
l'information. `process_webhook` le lit et émet un **avoir Pennylane lié** à la
facture (le lien se pose par `link_credit_note` ; l'attribut `credited_invoice_id`
de la création est cassé côté fournisseur, changelog Pennylane).

Un avoir est une facture aux montants **négatifs** (convention Pennylane) et notre
ligne les porte négatifs aussi. Sur un remboursement **partiel**, la ventilation
suit la proportion remboursée et la TVA est le **reste** — jamais recalculée au
taux, sinon la somme des deux ne retomberait pas sur ce qui a été rendu au client.

⚠️ **Un seul avoir par paiement** (clé `(paiement, kind)`). Un **second**
remboursement partiel sur le même paiement ne produira donc pas un second document :
le cas est journalisé en `error` en nommant l'écart, et demande un avoir manuel. Une
clé par remboursement supposerait de suivre les objets `refund` de Mollie, que le
webhook ne porte pas.

### Idempotence

`UNIQUE (payment_row_id, kind)` sur `billing_invoices`. C'est la **contrainte** qui
garantit qu'un webhook rejoué ne crée pas une seconde facture — pas une lecture
préalable, que deux webhooks simultanés franchiraient tous les deux. Côté Pennylane,
la référence externe `oto-payment-<tr_…>` (et `oto-refund-<tr_…>`) joue le même rôle :
une reprise après un crash retrouve le brouillon déjà créé et le finalise, au lieu
d'émettre un second document.

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

## L'incident du 2026-08-25 (org 219, 38 € pour un abonnement à 19 €)

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
et la clé de la compta d'Otomata, `emission.py` = le cycle facture/avoir/reprise,
`mail.py` = l'e-mail au contact de facturation), `db/billing_invoices.py` (la table
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
`OTO_BILLING_RUNNER_ENABLED` (défaut : allumée dès que le billing l'est). Les deux
clés fournisseur — `MOLLIE_API_KEY` (le PSP) et `OTO_PENNYLANE_API_KEY` (la compta
d'Otomata) — viennent de l'**env du process** (Scaleway Secret Manager au boot),
jamais de SOPS ni du coffre.
