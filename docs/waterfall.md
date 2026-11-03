---
title: Les waterfalls natifs — email, téléphone, société, personne, gens d'une société, pile technique
type: explanation
description: >-
  Six connecteurs (`waterfall_email`, `waterfall_phone`, `waterfall_company`,
  `waterfall_person`, `waterfall_people`, `waterfall_tech`), un outil chacun, servis par un seul module : une cascade de
  fournisseurs DÉJÀ connectés, arrêtée au premier trouvé, où chaque étape est un appel
  ordinaire de l'outil du fournisseur (`meta.executer_cible`). D'où vient l'ordre (appel,
  réglage `steps` de la carte, défaut) et pourquoi les clés de l'appelant passent devant ;
  ce que la cascade garde de chaque étape (garde, clé, facturation, rédaction) ; l'étape
  asynchrone (lemlist) ; ce qu'elle refuse d'être. À lire avant de toucher
  `oto_mcp/waterfall/` ou `oto_mcp/tools/waterfall*.py`.
---

# Les waterfalls natifs

## Le problème

Trouver l'email d'une personne, c'est essayer Hunter, puis Apollo, puis Kaspr… jusqu'au
premier qui répond. Un agent le fait à la main : un appel par fournisseur, une lecture
de chaque réponse dans son contexte, une décision « trouvé ou pas » à chaque fois — et il
paie des jetons pour un travail sans jugement. FullEnrich vend cette cascade, mais chez
lui, sur ses fournisseurs, à son prix ; l'org qui a déjà ses propres clés n'en profite pas.

## Le principe

Six connecteurs, un outil `<ns>` chacun (plus un `<ns>_act` qui écrit dans un tableau,
plus bas), une table d'étapes chacun (`EMAIL_STEPS`, `PHONE_STEPS`,
`COMPANY_STEPS`, `PERSON_STEPS`, `PEOPLE_STEPS`, `TECH_STEPS`), un domaine (`oto_mcp/waterfall/` :
le moteur, l'ordre, l'appel d'une étape, la qualification, le vivier des gens, une table
d'étapes par cascade, le plan) servi par deux modules d'outils
(`Connector.modules=("waterfall", "waterfall_lignes")`, que `register_all` ne charge
qu'une fois chacun). Une étape = un outil de connecteur existant,
les entrées qu'il exige, la façon de construire ses arguments et de lire sa réponse, et
une ligne qui dit ce qu'elle apporte (`Step.explains`, en anglais : ce que sa source voit et
quand elle vaut sa place, jamais un prix), que le plan sert en `explains` — un test exige
cette ligne de chaque étape de chaque table. La
cascade s'arrête au **premier** trouvé (choix produit : le moins cher, pas le plus sûr —
l'email n'est pas vérifié ici).

⚠️ **Un nom de connecteur multi-token qui ne sert que `<ns>`** : `namespace_of` excluait
le nom ENTIER des candidats, si bien que `waterfall_email` se résolvait à `waterfall` —
hors de tout connecteur, donc hors gate d'activation, en silence (et
`test_convention_act` l'aurait laissé passer : il écarte les outils sans connecteur). Le
nom entier est désormais un candidat ; aucun outil existant ne change de namespace.

**Chaque étape passe par `meta.executer_cible`**, la couture d'`oto_call` et des
recettes. Elle garde donc tout ce qu'un appel direct a : la garde d'activation du
connecteur pour l'org et l'équipe de l'appel, la cascade de clé, le niveau d'accès, la
liste d'outils d'un travail hébergé, **sa** ligne dans `tool_calls` (donc sa `quantity`
et son `key_mode` : la facturation reste sur la ligne du fournisseur, jamais sur celle
du waterfall), et la rédaction de **son** namespace. Le waterfall n'a ni clé ni
facturation propres.

## Ce que l'ordre par défaut optimise

Un ordre NEUTRE, qui ne doit rien à la grille de prix d'une instance : gratuit → clé de
l'appelant → clés partagées, par coût et rendement des fournisseurs → les asynchrones en
dernier (lentes, facturées même quand elles finissent `pending`, et une étape `pending`
ARRÊTE la cascade : une étape synchrone rangée après elle ne tournerait jamais). Une
instance qui revend sur ses propres clés pose SON ordre par le réglage `steps` de la
carte, au palier tenant. On ne demande jamais à une étape plus d'unités facturées qu'elle
n'en garde (AI Ark : `size` = la limite ; TheirStack : une société ; une recherche AI Ark
par domaine : une).

⚠️ **Le tronc ne porte aucun prix** (décision du 20/10/2026) : le plan sert des FAITS
NEUTRES — les outils qu'une étape appelle (`tool`, `collect_tool`, `reveal`), ce qu'elle
leur demande (`asks` : `work_emails`, `phones`…) et ses bornes (`max_units`, `per_item`).
Un front d'instance y lit sa propre grille ; un prix écrit ici serait celui d'une seule
instance, servi à toutes.

## D'où vient l'ordre

Trois couches, la plus spécifique gagne :

1. `providers=[…]` sur l'appel — validé : une clé inconnue est la faute de l'appelant ;
2. le réglage **`steps`** de la carte du connecteur — un `CredentialField(secret=False)`
   sur une carte `secret_kind="settings"` (19/10/2026 : « cette carte ne porte que des
   réglages FACULTATIFS, elle marche sans rien » — même formulaire que `fields`, méthode
   d'auth `secret`, mais un front y lit « inclus, rien à connecter » au lieu de « non
   connecté »)
   (un réglage, jamais un secret), posé par un admin d'org (défaut de l'org) ou par un
   membre pour lui-même (sa surcharge), résolu par la même cascade qu'une clé
   (`access.resolve_credential(ns, want="byo", emit_on_failure=False)` : sans réglage,
   aucun événement de monitoring n'est émis). Rendu par le formulaire générique des
   champs de connecteur : aucun travail de front. Une clé inconnue y est IGNORÉE et
   nommée (`ignored_steps`) : celui qui appelle ne l'a pas écrite ;
3. la table par défaut.

Sur l'ordre PAR DÉFAUT seulement, quatre groupes, dans cet ordre : les étapes GRATUITES
(`Step.free` : registre français, session LinkedIn de l'appelant — que
`credential_mode_for` dit `platform` et qu'un tri par clé aurait placées derrière une clé
payante) ; une étape `quota_only` (la base de lemlist : aucun crédit, un appel du quota de
la clé) sur la clé de l'APPELANT ; **les autres fournisseurs pour lesquels l'appelant
détient SA clé** (utilisateur, équipe, org — `access.credential_mode_for`, lu une fois par
porteur et par appel, AVANT toute dépense) ; les clés partagées ensuite — celle du tenant
comprise : du point de vue de l'org, c'est une clé payée par l'opérateur de l'instance, et
refacturée sur le tenant primaire, pas la sienne,
chaque groupe dans l'ordre de la table (tri stable). Un ordre ÉCRIT — celui de l'appel ou
du réglage — est gardé tel quel : quelqu'un l'a choisi. Un fournisseur sans clé utilisable
(`forbidden`, `over_quota`) est sauté sans être appelé. ⚠️ Ce mode est une lecture
proche de la résolution réelle, pas exacte (il ignore un `_instance` épinglé, un compte
nommé, l'option payante) : il ORDONNE, il ne garde rien — la garde reste l'appel de
l'étape.

Une étape « clé de l'appelant seulement » (`Step.own_key_only` : Forager pour l'email et le
profil, la révélation de téléphone Apollo) ne part que sur une clé PROUVÉE de l'appelant —
utilisateur, équipe ou org. Ni la clé du tenant, ni celle de la plateforme, ni un mode
illisible (`unknown` : pas d'appelant, ou la lecture a échoué) : l'étape est sautée sans
appel (`own_key_required`), et le plan la dit `usable: false`.

Sauf si l'instance ouvre ce geste sur sa clé partagée (`Step.shared_serves`) : pour Forager,
le réglage `platform.ops` (`tools/forager.py::platform_ops`, `oto_admin_connector_setting`)
— `forager_person:work_emails` ouvre l'étape email, `forager_person:detail` l'étape profil.
`ordre._plan` le lit une fois par appel, hors boucle, et lève `own_key_only` pour cet appel :
le plan et la cascade disent la même chose. Un réglage illisible n'ouvre rien.

## Ce qu'une étape qui échoue fait : rien d'autre que passer la main

Une garde qui refuse (`McpError` levée par `executer_cible` : connecteur coupé, hors
liste du travail), un échec du fournisseur (`IssueCible(ok=False)` : clé refusée —
classée `invalid_input`, il n'existe pas de code « non connecté » —, compte vide, limite
de débit, panne), un résultat retenu par la rédaction, une réponse sans valeur : tout est
écrit dans `attempts` (statut, code, message, mode de clé) et l'étape suivante est
essayée. Un `not_found` (404) est un non-trouvé, pas un échec. Seules les fautes de
l'appelant lèvent : aucune étape possible avec ces entrées, une clé de `providers`
inconnue, un SIREN malformé. Le tout tient en **40 s** (`DEADLINE_S`) : sous les 45 s du
chemin REST d'invocation (`capabilities/tools_me.py`, la borne la plus serrée qu'un
appelant rencontre — MCP : 120 s) ; une étape qui partirait après est sautée
(`time_budget`).

⚠️ **Un trouvé se juge à la FORME de la valeur** : une adresse qui a la forme d'un email,
un numéro d'au moins six chiffres, un profil qui nomme un titre ou une société. Un
fournisseur qui renvoie l'entrée qu'on lui a donnée n'a rien trouvé ; un `person._stub`
d'Apollo (fiche vide, pourtant facturée) non plus ; une ligne Lusha porteuse d'`error`
non plus. ⚠️ **Une recherche rend toujours quelque chose** : le registre par nom, AI Ark par nom,
TheirStack, Cognism ne comptent que si le domaine correspond ou si le nom demandé est
contenu dans celui du résultat — un homonyme arrêtant la cascade cacherait la bonne
réponse plus bas.

## Les étapes asynchrones : lemlist, Dropcontact, FullEnrich, la révélation Apollo

`lemlist_enrich` / `dropcontact_enrich` / `fullenrich_enrich_linkedin` /
`apollo_reveal_phone` soumettent et rendent un identifiant ; leur outil de relève
(gratuit) lit le résultat. ⚠️ FullEnrich met d'ordinaire 30 s à 4 min : dans un appel,
il finit le plus souvent `pending`. L'étape soumet, attend ~8 s, relève toutes les ~6 s pendant au plus 25 s
(`POLL_*`). Fini à temps → lu comme une étape ordinaire ; un `done` à digest vide qui
porte un `warning` n'est pas final (lemlist demande une relève de plus). Pas fini → l'étape
est `pending`, la réponse donne l'appel exact à faire plus tard (`pending[]`), et la
cascade S'ARRÊTE là (décision du 20/10/2026) : la soumission est payée et sa réponse arrive
encore, aucune étape suivante n'est payée pour la même recherche — pas même une étape de
complétion.
lemlist est BYO seulement : sans clé de l'appelant, l'étape est sautée sans appel.

**L'exception qui passe la main : Pipecorn** (en tête du téléphone ; à l'email, après les
étapes synchrones — qui trouvait déjà l'email au premier fournisseur n'attend ni ne paie
Pipecorn en plus).
`pipecorn op=enrich` soumet, `pipecorn op=result` relève (gratuit). L'étape a SA propre
cadence (`Step.poll` : première relève à 2 s, puis toutes les 3 s, **20 s au plus**),
**dans** le budget de la cascade (`DEADLINE_S`, 40 s) : l'appel entier tient sous les
45 s de la face REST, et les étapes suivantes ont ce qui reste. Pas finie à temps, elle
**n'arrête pas** la cascade (`Step.falls_through`) : les fournisseurs suivants sont
essayés, puis elle est relue UNE fois en fin de cascade s'il reste du temps — même quand
un autre a trouvé : finie, cette relecture règle la recherche à ce qu'elle a trouvé et sa
trouvaille sert si rien d'autre n'a trouvé (`found_late` sinon, **payée tout de même** :
Pipecorn débite un mobile ou un email trouvé). Toujours en cours, ou plus de temps, elle reste dans
`pending[]`, à relever plus tard ; jamais un appel coupé après dépense. Sur une clé
refacturée, une recherche jamais relue est réglée au pire (un résultat par sorte
demandée) à l'échéance de sa réservation (`credits_echeances`) : d'où la relecture.

## Ce que le waterfall refuse d'être

- **Un appel d'outil arbitraire** : `providers` et `steps` ne nomment que des clés de la
  table, jamais un outil. C'est ce qui tient une écriture (ou un waterfall lui-même) hors
  d'une cascade.
- **Un lot** : une personne par appel. Une liste passe par une recette (`oto_recipe`,
  `for_each`) — d'où la déclaration `LECTURE` des outils : ils ne changent rien chez
  aucun tiers, même si leurs étapes dépensent des crédits. ⚠️ La recette ne juge que le
  waterfall ; ses étapes, appelées par `executer_cible`, ne sont pas rejugées « en
  lecture » — ce sont toutes des lectures d'enrichissement (`lemlist_enrich` écrit une
  demande d'enrichissement chez lemlist, rien qu'un tiers voit), et c'est la table fermée
  qui le garantit.
- **Hors `BETA_TOOLS`** : des noms neufs, mais des connecteurs fermés par défaut
  (`default_active=False`) — l'activation par la plateforme fait office de population.

## Les étapes

- **Email** : hunter → aiark (sur une vraie liste, les mêmes emails qu'Apollo, qui facture
  sa correspondance même sans email) → apollo → kaspr → lusha → forager (clé de l'appelant
  seulement, sauf si l'instance ouvre `forager_person:work_emails` sur sa clé partagée) →
  pipecorn (`pipecorn op=enrich find=["email"]`, par le profil LinkedIn, sinon prénom + nom
  + domaine ou société : sa propre cascade, ASYNCHRONE comme au téléphone — 20 s au plus
  dans le budget de l'appel puis repli ; facturée à l'email trouvé, même si un autre a
  trouvé ; absente du mode tableau ; APRÈS les synchrones, pour qu'un email trouvé au
  premier fournisseur ne coûte ni attente ni second paiement) → dropcontact → lemlist →
  fullenrich (les asynchrones en dernier).
- **Téléphone** : pipecorn (`pipecorn op=enrich find=["phone"] phone_type="mobile"`,
  depuis un profil LinkedIn : sa propre cascade de fournisseurs, ASYNCHRONE, attendue
  20 s au plus dans le budget de l'appel puis repli sur la suite — voir « Les étapes
  asynchrones » ; facturée au seul mobile trouvé, à la relève, même si un autre a trouvé ;
  aucune clé partagée n'est promise (connecteur fermé, clé de l'appelant ; octroyée, elle
  sert le téléphone et l'email pro, dans le budget d'unités) ;
  absente du mode tableau comme toute étape asynchrone) → kaspr →
  lusha → forager → apollo (`apollo_reveal_phone`, asynchrone, clé de l'appelant
  seulement) → lemlist → dropcontact → fullenrich. Pas d'AI Ark. En tête de la table, mais
  l'ordre par défaut reste trié (propres clés d'abord, `ordre._plan`) : sur la clé
  partagée, pipecorn passe derrière une clé Kaspr ou Lusha de l'appelant.
- **Société** : `fr` (le registre français, UNE ligne à deux entrées : `fr_get` par SIREN,
  sinon `fr_search` par nom, France seulement, gratuit) → lemlist (`lemlist_database
  op=companies`, par `currentCompanyWebsiteUrl` ; voir « La base de lemlist ») → aiark
  (`linkedin_aiark_search op=companies`, domaine ou nom) → pipecorn (`op=company`, domaine,
  sinon nom vérifié contre la société demandée ; clé de l'appelant seulement) → apollo
  (domaine) → theirstack
  (domaine) → cognism (domaine ou nom).
- **Personne** : linkedin (`linkedin_unipile_profile`, le compte LinkedIn connecté de
  l'appelant, gratuit) → `lemlist_db` (`lemlist_database op=people`, par
  `leadLinkedInSlug`, sinon `username` + `currentCompanyWebsiteUrl` ; la clé `lemlist` reste
  l'enrichissement asynchrone) → pipecorn (`op=person`, profil LinkedIn, sinon prénom + nom
  + société ; clé de l'appelant seulement) → aiark (export) → apollo → forager (`op=detail`, clé de l'appelant
  seulement, sauf `forager_person:detail` ouvert sur la clé partagée) → cognism →
  lusha (recherche sans `reveal`, le mode bon marché) → lemlist (`linkedin_enrichment`).
- **Lus par des noms de clés connus, faute de forme figée par un test ici** : Kaspr,
  Forager, Lusha (email/téléphone par extraction profonde), et pour la personne et la
  société Forager, Cognism, Lusha, Unipile, le profil LinkedIn brut de lemlist, le domaine
  d'une société AI Ark. À confirmer sur une réponse réelle avant de s'y fier — et à figer
  alors par un test de son extraction.
- **Pipecorn hors de la clé partagée** (personne, société, technologies) : une recherche
  synchrone n'entre pas au budget d'unités (`access/credits.py` : une fonction par outil,
  et un règlement synchrone lit la quantité dans la réponse, que Pipecorn ne donne pas) —
  l'étape est `own_key_only`, sautée sans appel sur une clé partagée. Son appel est
  borné au temps qui reste dans le budget de la cascade (`Step.bounded_by_deadline` :
  `timeout_s` posé juste avant l'appel, au plus 60 s ; moins de 3 s restantes → `skipped`
  `time_budget`, sans appel), le mode tableau compris ; sans réponse à temps, refus nommé
  `pipecorn_timeout` (la recherche a pu être facturée), jamais un succès.
- **Absents, et pourquoi** : HitHorizons (deux appels par société, aucune forme connue),
  Topograph et Crunchbase (un « introuvable » ne s'y distingue pas d'une panne :
  `internal`).

## La pile technique d'une société (`waterfall_tech`)

Deux sortes de sources, mesurées sur un même domaine (20/10/2026) : ce que le SITE fait
tourner (CMS, framework, hébergement, analytics — l'index DataForSEO, ou un crawl) — 6 à
7 noms ; et ce que les OFFRES D'EMPLOI nomment (CRM, ERP, pile data, outils internes —
TheirStack) — une centaine de slugs, mais plus cher et maigre sur les petites sociétés.
D'où l'ordre : lemlist (la fiche société de sa base, sans crédit — ~200 technologies sur
une société de 300 personnes, scorées CONFIRMED / LIKELY / POTENTIAL : les POTENTIAL
écartées, elles ne répondent pas à « l'utilise-t-elle ? ») → dataforseo (~0,01-0,02 \$) → apify (`kfirs/tech-stack-detector`,
HTTP simple sans navigateur, ~0,01 \$ par site analysé, un site en échec n'est pas
facturé ; une URL par exécution, sans `max_items` — même raison que la page LinkedIn ;
une ligne par fournisseur, chacune avec son logo, pour qu'un écran dise QUI regarde) →
apollo puis aiark (la fiche société de chaque fournisseur de données, mesurée le 20/10 :
~100 technologies catégorisées chacune, site ET outils métier — CRM, pile data, paiement ;
source `vendor_database` ; une fiche par domaine, `max_units=1`) →
theirstack (~3 crédits par société ; par domaine `limit=1`, par nom `limit=3` et la garde
d'homonyme) → pipecorn (`op=stack`, sa fiche technologies avec une confiance ; 3 crédits
Pipecorn par requête, trouvé ou non, d'où la DERNIÈRE place : ce qui facture sans résultat
passe en dernier ; clé de l'appelant seulement). Le reçu dit `source` : `website`, `vendor_database` ou `job_postings`.
⚠️ L'étape du crawl s'appelait `website` jusqu'au 20/10/2026 : aucun alias, un réglage
`steps` qui la nomme encore la voit ignorée (`ignored_steps`), un appel qui la passe en
`providers` est refusé — l'écrire `apify`.

⚠️ **`technologies=[…]` change ce qu'est un trouvé** : une source qui ne détecte aucune
des technologies demandées n'arrête PAS la cascade (la suivante voit autre chose — le site
ne voit pas un CRM, les offres si), `matched` dit lesquelles. La comparaison est lâche
(`_tech_key` : `Next.js` = `next-js` = `nextjs`, `spendesk1` = `spendesk`) parce que les
sites rendent des noms et TheirStack des slugs. Un `found: false` dit « aucune source ne
l'a vue », jamais « ne l'utilise pas ». Sur un tableau, `waterfall_tech_act` prend la même
liste pour toutes les lignes.

Écartés pour l'instant (comparatif du 20/10/2026) : BuiltWith et Wappalyzer (abonnements
mensuels de 250-995 \$ pour ce que l'index DataForSEO et le crawl donnent à quelques
centimes ; seul l'historique d'adoption de BuiltWith n'a pas d'équivalent ici),
PredictLeads (pas encore de connecteur).

## Les étapes de complétion : la page LinkedIn d'une société

**Une ligne, plusieurs entrées** (`Step.variants`, `_one_row`) : le registre (SIREN, sinon
nom) et la page LinkedIn (site, sinon nom) sont UNE étape chacun — un écran les montre une
fois, un réglage les nomme une fois ; les variantes tournent dans l'ordre, chacune quand
ses entrées sont là, et le reçu dit laquelle a répondu (`via`, `tried`).

Une étape `Step(fills="linkedin_url")` ne concourt pas au trouvé : après la cascade, elle
ne tourne que si la valeur trouvée n'a PAS ce champ, et y fusionne ce qu'elle trouve
(sans écraser). Une page trouvée alors que rien d'autre n'avait identifié la société fait
encore un trouvé, mince (`linkedin_url`, et ce que la recherche rend). Une ligne `linkedin`,
deux variantes, deux acteurs Apify, chacun une exécution bornée par `apify_run_sync` avec `timeout_secs` (25 s,
côté Apify) et un plafond de coût — un acteur coupé rend encore ses éléments partiels :

- `linkedin_site` (`vdrmota/contact-info-scraper`) : les liens LinkedIn du site de la
  société. ⚠️ Le site d'un grand groupe lie souvent la page du GROUPE (`linkedin_source`
  le dit : `website`).
- `linkedin_search` (`harvestapi/linkedin-company`, `searches` — pas `companies`, qui
  attend des URL) : le premier résultat d'une recherche par nom est souvent un homonyme ;
  gardé seulement si son site est le domaine, ou, sans domaine, si son nom est celui
  demandé.

Avant elles, sa propre ligne `lemlist_linkedin` (`fills="linkedin_url"`) : la fiche
société de la base de lemlist, sans crédit — la page manque surtout quand le registre
français a gagné. Une ligne à part, pas une variante : les variantes d'une ligne partagent
UN mode de clé, et celui de lemlist n'est pas celui d'Apify. `Step.same_as="lemlist"` la
saute quand l'étape `lemlist` a déjà répondu (trouvé ou non) : un second appel paierait le
quota pour la même réponse.

Appris sur une vraie liste (un salon professionnel, octobre 2026) : ces deux étapes ont
retrouvé les pages que les fournisseurs de données n'avaient pas, pour quelques centimes.

## Les gens d'une société (`waterfall_people`)

⚠️ **Des noms masqués ne stoppent pas la cascade** (`Step.weak`) : la recherche d'Apollo
est gratuite mais rend des noms de famille masqués ; elle est gardée comme REPLI, la
cascade continue (google → aiark → employés LinkedIn), et ce repli n'est rendu que si rien
de mieux ne vient. Le reçu marque l'étape `weak`.

Une société en entrée (domaine, nom, page LinkedIn), des `roles` facultatifs, une
`limit` (≤ 25) ; la première source qui rend au moins une personne gagne. Une personne
n'est gardée que si elle a un nom et si son titre correspond à l'un des `roles` (quand il
y en a) : apollo (recherche gratuite ; les noms y sont masqués — on révèle par
`apollo_id`, JAMAIS par nom + société, qui facturerait une fiche vide) → lemlist
(`lemlist_database op="people"`, la base partagée de lemlist, par `currentCompanyWebsiteUrl`
sinon `currentCompanyLinkedInUrl`, et `currentTitle` = les `roles` ; voir ci-dessous) → aiark (domaine ;
pas de filtre de titre côté AI Ark : il est appliqué ici) → `linkedin_employees`
(`harvestapi/linkedin-company-employees`, les employés de la page LinkedIn — titres
actuels, souvent plus frais que les fournisseurs ; ⚠️ une page tire des gens dont le
poste ACTUEL est ailleurs, et des faux profils : gardés seulement si
`currentPosition[0].companyUniversalName` est la page demandée) → google (`serper_search`
sur `linkedin.com/in`, un titre « Nom – Poste – Société | LinkedIn », gardé seulement s'il
nomme la société).

### La base de personnes de lemlist (21/10/2026)

Mesuré sur une vraie société (~360 personnes en base) :

- **Gratuite en crédits** (solde inchangé après une dizaine de recherches) mais **chaque appel
  décompte un quota d'appels de la clé** (`limitation`, −1 par appel quelle que soit la
  `size`) : l'étape est `quota_only` — elle ne compte pas dans `max_paid_steps` et ses
  pages se lisent comme une recherche gratuite, mais elle GARDE sa place dans l'ordre (un
  `free` passerait devant l'Apollo de la clé partagée : un quota partagé n'est pas rien) —
  et des pages de 100 sous `qualify` (moins d'appels). **Sur la clé de l'appelant** (perso,
  équipe ou org), l'ordre par défaut la met **en tête** des clés de l'appelant (derrière
  les seules étapes gratuites, aucune chez les gens), devant Apollo même sur sa propre
  clé : des noms complets qui ne facturent rien, sur un quota qui est le sien (décision
  produit du 21/10/2026). Un ordre écrit n'est pas retrié.
- **Pagination à partir de 1** (`page=0` est servi comme la page 1). Sur un résultat étroit
  (7 personnes, `size=3`), les pages 1 → 2 → 3 rendent les 7, sans doublon. ⚠️ Sur un résultat
  large aux scores proches, **deux appels identiques ne rendent pas le même ordre** : des
  pages successives peuvent se recouvrir ou sauter quelqu'un — le dédoublonnage par URL
  LinkedIn absorbe les doublons, un saut est une perte de rappel. Sous `qualify`, toutes
  les pages sont lues dans le vivier (`vivier._every_free_page`, `total` lu au premier
  appel).
- **Une fiche pèse ~60 Ko** : `experiences` (descriptions et technos de chaque société) en
  fait ~90 %. `excludes` retire des champs de premier niveau (`education`, `skills`,
  `languages`, `interests`, `summary`…), jamais un chemin imbriqué. ⚠️ **Ne jamais y mettre
  `experiences`** : la liste n'est pas retirée mais TRONQUÉE (16 postes servis sur 26,
  mesuré le 22/10/2026 — le poste actuel d'un dirigeant manquait), et c'est d'elle que se
  lit le titre. Sous `qualify`, chaque page est donc **réduite avant d'être cumulée**
  (`Step.slim`, `gens._lemlist_slim`) : seules les personnes en poste dans la société
  demandée, et de chacune ce que l'extraction lit (quelques centaines d'octets) — sinon
  le vivier tiendrait jusqu'à `POOL_MAX` fiches entières par ligne d'une table.
- **Une page LinkedIn sans slug est refusée** (`linkedin_page_without_slug`), jamais
  cherchée comme `…/company/None` ; une page `school` ou `showcase` est passée telle
  quelle, jamais réécrite en `/company/`.
- **Le titre se lit sur le poste ACTUEL** (sans `date_to`) dans la société demandée, pas sur
  le `headline` (un « … @ Autre Société » chez quelqu'un toujours en poste, mesuré). Les
  `roles` sont le filtre `currentTitle` de lemlist (mots dans le désordre, titre normalisé :
  « Head of Enterprise Sales » pour « Head of Sales », « Directeur des ressources humaines »
  pour « director ») et ne sont PAS re-filtrés ici : la correspondance stricte des autres
  étapes les perdait (1 sur 7 sur l'essai).
- `potential_email` est un email deviné par motif : rendu avec `email_status="guessed"` (le mot d'Apollo pour la même chose).
- `country` n'est pas envoyé : filtre à autocomplétion, une forme qu'il ne connaît pas
  (`FR`) vide la recherche sans erreur.

### La base de lemlist dans les autres cascades (22/10/2026)

La même base, mesurée sur une vraie société et une vraie personne :

- **Société et pile technique** : `op="companies"` par `currentCompanyWebsiteUrl`, une
  fiche (`size=1`), gardée seulement si son `company_domain` est le domaine demandé. Elle
  porte effectifs (chiffre ET tranche), secteur à trois niveaux, siège, année, type,
  marché (B2B/B2C), page LinkedIn, et `technologies` (`{name, scoring, first_seen_at}`).
  ⚠️ **~0,6 Mo par fiche** dont ~0,5 Mo d'offres d'emploi des 6 derniers mois
  (`job_post_last_6m_array`) : `op="companies"` ne prend pas d'`excludes` côté oto-core
  (à ajouter là, si lemlist l'accepte). Les chiffres arrivent en chaînes (`"454"`).
- **Personne** : `leadLinkedInSlug` (le slug décodé : immunisé contre `www`, l'encodage
  pourcent d'Apollo) — `leadLinkedInUrl` répond aussi. Sans URL : `username` (nom complet)
  + `currentCompanyWebsiteUrl`, 3 fiches, l'homonyme écarté par le nom. Le titre est le
  poste ACTUEL chez l'employeur actuel (`current_exp_company_name`), sinon le `headline`.
- ⚠️ `experiences` n'est jamais dans `excludes` (voir plus haut : la liste serait tronquée,
  le poste actuel avec) — vérifié sur une fiche complète : 26 postes servis sur 26.
- **Pas dans l'email ni le téléphone** : la base n'a pas de numéro, et `potential_email`
  est un motif deviné — une étape « premier trouvé gagne » s'arrêterait sur une
  devinette, devant Hunter. Il reste rendu, `email_status="guessed"`, par la personne et
  les gens.
- Les filtres à autocomplétion (`currentCompany`, `country`) ne sont jamais envoyés : une
  forme inconnue vide la recherche sans erreur. Seuls des filtres texte.

## Essai réel (19/10/2026, exposants d'un salon professionnel)

Ce que les vraies réponses ont appris, et qui est désormais figé par des tests
(`test_reel_*`, réponses anonymisées) :

- ⚠️ **Apify refuse une exécution dont le plafond est sous 0,50 \$**
  (`max-total-charge-usd-below-minimum`). Décision (19/10) : **aucun plafond de coût par
  défaut** — une exécution est bornée par sa TAILLE (pages, résultats, profils = la
  `limit` de l'appel) et son DÉLAI (`timeout_secs`, côté Apify), bornes figées par les
  tests.
- ⚠️ **Sur un acteur facturé à l'événement, `max_items` DEVIENT un plafond** : Apify en tire
  un plafond de coût (quelques éléments × un prix d'un centime) sous son minimum, et refuse.
  La page par le site (`linkedin_site`) part donc sans `max_items`, bornée par ses 4 pages
  et sa profondeur ; la recherche et les employés l'acceptent (mesuré le 19/10).
- Les employés d'une page prennent ~27 s pour quelques profils complets (le mode « court »
  n'est pas plus rapide et n'a pas de titre) : délai propre de 35 s, dernière étape des
  gens. Une exécution Apify qui ne tient plus dans ce qui reste de l'appel n'est PAS lancée
  (`skipped / time_budget`, rien de facturé). Sans plafond de notre part, Apify prend le
  reste du budget du compte comme plafond d'exécution.
- AI Ark écrit le statut d'un email en capitales (`VALID`).
- Apollo trouve des personnes sans email (`email_status: unavailable`) — facturées quand
  même — et rend leur URL LinkedIn encodée en pourcents : la personne rendue garde l'URL
  de l'appelant.
- Google : le `subtitle` (« Lieu · Titre · Société ») est fiable, le `title` ne l'est pas ;
  un extrait qui MENTIONNE les mots de la société n'en fait pas un employé. Sur un nom peu
  distinctif, la recherche ne rend que du bruit — d'où la société exigée dans le résultat.
- Une recherche LinkedIn par nom rend d'abord un homonyme (vérifié : une cimenterie pour
  une société d'IA) : la garde par domaine est indispensable.
- Les sources rendent les gens dans leur ordre (un ingénieur avant le CTO) : la cascade
  les range par séniorité avant d'appliquer la limite.
- lemlist a répondu en ~25 s (dans la fenêtre), FullEnrich en ~80 s (donc `pending`).
- Votre LinkedIn non connecté : l'étape gratuite est `skipped / not_connected`, pas un échec.

## Limites : aucune par défaut, l'agent en fixe une s'il le faut

Décision du 19/10/2026 : la cascade ne plafonne rien de son propre chef. Deux paramètres
optionnels, sur chacun des outils, pour l'agent qui veut une limite :

- `max_paid_steps` — au plus N appels PAYANTS par recherche (les étapes gratuites ne
  comptent pas) ; les suivantes sont `skipped / max_paid_steps`.
- `apify_max_usd` — un plafond de coût par exécution Apify (≥ 0,50 \$, le minimum
  d'Apify), transmis tel quel ; au-delà du plafond par lancement de l'org (25 \$ par
  défaut, borné par la plateforme, `docs/monitoring.md`), l'exécution est refusée
  (`max_charge_above_cap`).

Restent les bornes de forme, pas de dépense : la réponse (`limit` des gens, jusqu'à 100),
le délai de la cascade (40 s, la route REST coupe à 45 s) et celui d'une exécution Apify.

## Qualifier par Jev avant de dépenser

`waterfall_company`, `waterfall_person` et `waterfall_people` prennent `qualify` (une
rubrique Jev, la forme des `questions` de `jev_ask`) et `qualify_min`. Après la cascade,
UN appel `jev_items` (via `executer_cible` : gardes, facturation au coût réel) juge ce qui a
été trouvé : chaque résultat porte `qualification: {answers, model}` ; avec `qualify_min`,
les gens sous le seuil sont écartés (`dropped`), une société ou une personne reste avec
`passed`. Le but : qu'un agent ne lise pas 25 profils pour en garder 3, et qu'on ne paie
de révélation ou d'enrichissement que pour ceux qui passent.

- ⚠️ Jev est un tiers (OpenRouter puis TypeSafe) : seuls partent les champs d'un jugement
  (`_QUALIFY_FIELDS` — titre, séniorité, lieu, secteur, taille…), jamais un nom, un email,
  un téléphone ni une URL de profil. Figé par un test.
- ⚠️ **`qualify` demandé sans Jev utilisable est REFUSÉ avant toute dépense**
  (`qualification_unavailable`, décision du 20/10/2026) : pas de clé partagée (tenant ou
  plateforme), ou une clé plus proche (membre, équipe, org) qui la masque et que Jev
  refuse. Des résultats non jugés se liraient comme jugés — et le mode tableau les
  écrirait `found`. Le plan le dit d'avance (`qualification.usable`).
- Jev qui refuse ou manque de temps (moins de 5 s) APRÈS la cascade → résultats non
  qualifiés, et pourquoi (`qualification.status = "skipped"`).
- Ne compte pas dans `max_paid_steps`.
- Gens avec `qualify_min` (décision du 19/10) : qualifiés étape par étape ; un fournisseur
  dont personne ne passe ne clôt pas la cascade, le suivant est essayé. Un écarté retrouvé
  plus loin n'est pas rejugé. Les écartés reviennent TOUJOURS, en compact (`unqualified` :
  nom, titre, URL, réponses) — vérifier la rubrique ne repaie jamais une cascade ;
  `show_unqualified=true` rend les fiches entières. Limite connue : quand la société n'a
  simplement personne qui convienne, les fournisseurs suivants retrouvent les mêmes gens
  et sont payés pour rien — `max_paid_steps` est le frein.
- Moins de qualifiés que `limit` (décision du 19/10) : la cascade S'ARRÊTE et
  `continue_with` nomme les fournisseurs pas encore essayés — l'agent appelant décide s'ils
  valent d'être payés.
- Une société ou une personne est jugée une fois, à la fin (`passed`) : le fournisseur
  suivant ne trouverait que la même.

## Gens : juger la recherche gratuite, ne révéler que les retenus

Mesuré le 19/10/2026 sur une grande enseigne de distribution (le cas le plus cher d'un
sourcing par AI Ark : ~660 profils facturés pour en garder 10) : la recherche Apollo est
GRATUITE et montre les TITRES (noms masqués) — 239 personnes pour les rôles visés. Jev a
jugé 100 titres pour 0,0019 \$ ; les deux premiers révélés (`apollo_match_person` par id,
1 crédit Apollo chacun) étaient le directeur marketing-communication-omnicommerce du siège
et le responsable expérience client, avec URL LinkedIn ET email vérifié.

D'où, sous `qualify` : la recherche gratuite est lue sur TOUTES ses pages (le vivier,
jusqu'à `POOL_MAX` = 1 000 personnes ; elle s'arrête à la dernière page, au plafond, ou
quand il ne reste que le temps de juger et révéler — `pages`, `total`, `pool_truncated`
au reçu), Jev juge le vivier sur les titres par lots de 200 en parallèle, et seuls les `limit` meilleurs qui passent sont révélés
(`Step.reveal`, `_reveal_qualified`) — avant tout fournisseur payé au profil. Sans
`qualify`, les noms masqués restent un repli (`weak`), comme avant. Les révélations
comptent chacune dans `max_paid_steps` ; la recherche, gratuite, non. Jev indisponible →
le vivier redevient le repli.

## Les pages (décision du 20/10/2026)

- **Sources gratuites** (la recherche Apollo) : toutes les pages, sous `qualify`
  (ci-dessus).
- **Listes payées** (Google, AI Ark) : sous `qualify_min`, une liste qui rend moins de
  `limit` personnes retenues achète sa page SUIVANTE, une à la fois, jugée à son arrivée,
  jusqu'à `limit` retenus — ou la fin des résultats, une page sans personne nouvelle, le
  temps qu'il faut encore pour juger, ou `max_paid_steps` (chaque page est une étape
  payée). Le reçu dit `pages` et `stopped`. Encore court ensuite : `continue_with`, comme
  avant. Sans seuil, une page : `limit` personnes y sont déjà.
- Les employés d'une page LinkedIn (Apify) ne se paginent pas : une exécution par page,
  bornée par `limit`.
- **Les outils des fournisseurs eux-mêmes ne changent pas** (une page par appel,
  l'appelant pagine) : un défaut changé changerait sans bruit le coût de chaque agent et
  de chaque recette existants.

## Sur un tableau : `waterfall_<x>_act(op="update_rows")`

Un tableau s'enrichissait un appel par ligne : l'agent lisait la ligne, appelait la
cascade, relisait la trouvaille et la retapait — chaque ligne deux fois dans un contexte
de modèle, et chaque tour renvoyait la conversation. Le mode tableau
(`tools/waterfall_lignes.py`) prend les LIGNES (`row_ids` ou `filter`) et les COLONNES
(`inputs`, `output`, `status_column`) une fois ; le serveur lit, lance la MÊME cascade
(`_run_cascade`), écrit en retour, et ne rend que des comptes et des codes — le contrat
de `datastore/par_reference` (`docs/donnees-par-reference.md`), comme `lemlist_push_rows`.

- **Écrire, c'est `<ns>_act`** (la règle `<ns>`/`<ns>_act`, `tests/test_convention_act.py`) :
  op unique `update_rows`, annotations d'acte.
- **Une ligne se paie une fois** : seules les lignes à `status_column` vide sont prises ;
  chacune reçoit `found` (le fournisseur en commentaire), `no_match`, `unqualified` (gens
  sous `qualify_min`) ou `failed` (`invalid_input`). Vider l'état remet la ligne en jeu.
  Une ligne sans entrée exploitable n'est pas marquée : elle attend qu'on la remplisse.
- ⚠️ **Un échec n'est pas un verdict** (décision du 20/10/2026) : `no_match` seulement
  quand une étape au moins a RÉPONDU sans trouver et qu'aucune n'a échoué. Une étape en
  échec (clé refusée, erreur du fournisseur, résultat retenu par la rédaction) →
  `step_failed` ; aucune étape qui ait tourné → `no_step_answered` ; une limite qui a coupé
  la cascade → `time_budget` / `max_paid_steps` ; une trouvaille que Jev devait juger et
  n'a pas jugée → `qualification_skipped`. La ligne reste VIDE, le code va au reçu
  (`skipped_<code>`, `errors`), et l'appel suivant la retente.
- ⚠️ **Une étape hors de la liste d'un agent hébergé** (27/10/2026) : la garde du travail
  la saute (`appel._call`), comme toute garde. Avant, la ligne revenait
  `no_step_answered` — ou `no_match` si une autre étape avait répondu, fermée pour de
  bon — et l'agent concluait à un quota. Désormais : `tool_not_in_job`, la ligne reste
  vide, et le reçu nomme les outils (`missing_tools`) avec le geste qui les donne
  (`fix`, la forme du refus d'une recette-code) et `next_step` pour l'agent. Un `dry_run`
  le dit aussi des étapes qu'il planifie : le plan les disait `planned` sans un mot. Avant le run,
  `missing_tools` de l'agent les nomme déjà quand sa procédure écrit les fournisseurs
  (`waterfall/cites.py`, `docs/runner-et-automatisations.md`).
- `qualify` sans Jev utilisable : refusé avant de lire une ligne ; une clé de `providers`
  inconnue : refusée une fois, avant la première ligne.
- **Mêmes entrées, une recherche** : les lignes dont les entrées normalisées coïncident
  partagent UNE cascade, écrite sur chacune (`unique_lookups`, `rows_sharing_a_lookup`).
- **En parallèle** (`PARALLELE` = 8), dans la borne REST : aucune cascade ne PART après
  `DEPART_S`, et leurs étapes cessent d'être tentées à `CASCADE_S` ; ce qui n'est pas parti
  reste en attente (`stopped: "time_budget"`).
- **Les colonnes se déclarent avant de s'écrire** (une colonne non déclarée est refusée) :
  état en texte, listes (`all`, `people`) en json, le reste en texte ; une valeur va au
  TYPE de sa colonne (un nombre dans `number`, une liste jointe dans `text`), une valeur
  qui ne s'y range pas reste vide. `output_fields_never_found` nomme un champ qui n'a
  rien écrit (une faute de frappe).
- ⚠️ **Les étapes asynchrones sont laissées de côté** (lemlist, FullEnrich, Dropcontact,
  la révélation de téléphone Apollo) : payées maintenant, elles répondraient après
  l'écriture de la ligne. Le reçu les nomme (`left_out_async_steps`). Suite naturelle :
  écrire la poignée de collecte sur la ligne et une op `collect_rows`.
- **Pas encore de travail de fond** : un appel traite ce qui tient dans sa fenêtre
  (jusqu'à `batch_size` lignes, 50 au plus) ; l'agent rappelle avec les mêmes arguments.
  Un travail de fond sur le modèle de `jev_rows(background=true)` se justifiera si la
  mesure des lignes par appel ne suffit pas.
- `qualify` (company, person, people) juge par recherche — un appel Jev par recherche.

## Ajouter une étape

Une ligne `Step(...)` dans la table : la clé, l'outil (déjà monté — vérifié par
`tests/test_waterfall.py`), les groupes d'entrées exigées, `build`, `extract` (qui lit le
résultat SERVI, après rédaction), une note de coût, et `collect` si le fournisseur répond
plus tard ; `asks`, `max_units`, `per_item` si l'outil facture ce qu'on lui demande ou
par résultat (le plan les sert). Une étape asynchrone se range APRÈS les synchrones
(`tests/test_waterfall.py`), sauf si elle passe la main (`falls_through`, avec son
attente bornée `poll`). Un test de son extraction sur une réponse réelle du
fournisseur, anonymisée (`tests/waterfall/`).
La clé devient aussitôt utilisable dans le réglage `steps` de la carte.
