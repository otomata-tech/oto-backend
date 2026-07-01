"""Doc « how-to » user-facing des connecteurs — CONTENU curé, séparé du registre.

Overlay keyé par nom de connecteur (comme `_CATEGORY_BY_CONNECTOR` etc.), sorti de
`providers.py` pour garder le registre lisible et concentrer le contenu (rempli au
fil de l'eau, potentiellement volumineux) dans un seul fichier. `Connector.doc_sections`
en DÉRIVE (property). Sérialisé au catalogue public + `/api/me/connectors`, rendu
partout où le connecteur s'affiche (carte de connexion, connector-library, vitrine).

Convention par `kind` :
- `prerequisite` — ce qu'il faut AVANT de connecter (où prendre la clé, une autorisation
  à poser côté fournisseur…). Affiché avant connexion.
- `setup`        — étapes de configuration.
- `usage`        — ce que le connecteur permet + exemples concrets. Affiché aussi en
  découverte (library/vitrine).
- `note`         — divers.

`body_md` = markdown léger : `[label](url)` (http(s) seulement, sinon rendu en texte),
`**gras**`, `` `code` ``, listes `- `. Rester FACTUEL : décrire ce que font réellement
les outils, lier la page API/docs de l'éditeur plutôt qu'inventer un chemin d'UI exact.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocSection:
    kind: str            # prerequisite | setup | usage | note
    title: str
    body_md: str


# name de connecteur → sections. Vide pour un connecteur = rien d'affiché.
DOC_SECTIONS: dict[str, tuple[DocSection, ...]] = {
    # ── fédéré (mount) ──────────────────────────────────────────────────────
    "atlassian": (
        DocSection(kind="prerequisite", title="autoriser le callback côté Atlassian", body_md=(
            "avant de connecter, un **admin** de ton org Atlassian doit autoriser "
            "l'URL de callback d'oto dans les réglages Rovo MCP Server (sinon le "
            "consentement OAuth échoue).\n"
            "- url à autoriser : `https://mcp.oto.ninja/api/atlassian/oauth/callback`\n"
            "- où : [admin.atlassian.com → Security → Rovo MCP](https://admin.atlassian.com)\n"
            "- [doc Atlassian](https://support.atlassian.com/security-and-access-policies/docs/control-atlassian-rovo-mcp-server-settings/)"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote **Jira** et **Confluence** en langage naturel. par exemple :\n"
            "- crée un ticket Jira dans un projet\n"
            "- recherche des issues en JQL\n"
            "- lis ou crée une page Confluence"
        )),
    ),
    "folkmcp": (
        DocSection(kind="prerequisite", title="connecter ton compte Folk (OAuth)", body_md=(
            "connecteur **fédéré** : au premier usage, tu es redirigé vers "
            "[Folk](https://folk.app) pour autoriser oto à agir sur ton workspace "
            "(OAuth, pas de clé à copier). tu agis alors **en ton nom**, avec tes "
            "propres droits Folk.\n"
            "- distinct du connecteur `folk` natif (clé API partagée de l'org) : "
            "ici chaque personne connecte **son** compte\n"
            "- url de callback : `https://mcp.oto.ninja/api/folkmcp/oauth/callback`"
        )),
        DocSection(kind="usage", title="piloter ton CRM Folk", body_md=(
            "le MCP officiel de Folk (outils fédérés `folkmcp_*`) : cherche, crée et "
            "mets à jour contacts, sociétés, deals et notes en langage naturel.\n"
            "- « trouve la société Acme dans mon workspace »\n"
            "- « crée un contact et rattache-le au groupe Fundraising »\n"
            "- « fais avancer ce deal dans le pipeline »"
        )),
    ),
    "memento": (
        DocSection(kind="prerequisite", title="rien à faire — déjà connecté", body_md=(
            "memento est **provisionné automatiquement** à la création de ton compte oto "
            "(un compte oto = un compte [Memento](https://mento.cc), joints par ton email). "
            "tu n'as **aucune clé à poser** : c'est déjà branché.\n"
            "- pour des bases privées partagées hors de tes orgs, va sur [mento.cc](https://mento.cc) "
            "et fais-toi inviter (le connecteur les verra)"
        )),
        DocSection(kind="usage", title="interroger et écrire ta base de connaissance", body_md=(
            "une base de connaissance structurée, sourcée, multi-KB (outils fédérés `mem_*`). cherche, lis, et écris via la boucle propose-valide.\n"
            "- « cherche `pricing concurrent` dans toutes mes bases »\n"
            "- « liste mes bases de connaissance et ouvre celle de l'équipe »\n"
            "- « ajoute ce fait sourcé à la KB produit » (propose puis applique)\n"
            "- « montre-moi le voisinage de ce document »"
        )),
    ),
    "planity": (
        DocSection(kind="prerequisite", title="email + mot de passe planity", body_md=(
            "connecteur fédéré : renseigne l'**email et le mot de passe** de ton compte "
            "[Planity](https://planity.com) dans oto (ils servent à rejouer l'auth Planity, jamais stockés en clair).\n"
            "- besoin d'un compte Planity pro actif (agenda + caisse)"
        )),
        DocSection(kind="usage", title="piloter ton agenda et ta caisse planity", body_md=(
            "lis ton agenda Planity, tes clients, ton chiffre d'affaires et tes stats.\n"
            "- « quels rendez-vous j'ai cette semaine ? »\n"
            "- « retrouve la fiche client de Sophie Martin »\n"
            "- « quel est mon CA du mois ? »\n"
            "- « donne-moi les stats de fréquentation des 30 derniers jours »"
        )),
    ),
    # ── google (oauth dashboard, multi-compte) ──────────────────────────────
    "google": (
        DocSection(kind="prerequisite", title="connecter un compte google (oauth)", body_md=(
            "va sur le **dashboard oto**, section Google, et clique **connect** : tu autorises "
            "oto en OAuth (pas de clé manuelle). tu peux connecter **plusieurs comptes** Google ; "
            "chaque outil agit sur le compte par défaut ou sur celui que tu cibles par son email.\n"
            "- couvre Gmail, Tasks, Calendar, Sheets, Drive et Chat en une seule autorisation"
        )),
        DocSection(kind="usage", title="gmail, agenda, tâches, sheets, drive, chat", body_md=(
            "agis sur ton Google Workspace : mails, calendrier, tâches, feuilles de calcul, fichiers Drive et messages Chat.\n"
            "- « cherche les mails non lus de cette semaine et archive les newsletters »\n"
            "- « rédige un brouillon de réponse à ce mail » ou « envoie-le »\n"
            "- « qu'est-ce que j'ai à l'agenda demain ? crée un créneau de relance vendredi 10h »\n"
            "- « ajoute une tâche `relancer X` pour lundi », « lis l'onglet `leads` de cette sheet »\n"
            "- « partage ce dossier Drive en lecture à julien@… »"
        )),
    ),
    # ── prospection & enrichissement ────────────────────────────────────────
    "serper": (
        DocSection(kind="prerequisite", title="obtenir une clé serper", body_md=(
            "crée une clé api sur [serper.dev](https://serper.dev) (inscription, puis la clé est dans ton dashboard).\n"
            "- colle-la dans tes connecteurs oto sur `/account`\n"
            "- les membres peuvent aussi taper la clé plateforme partagée (quota quotidien) ; sans compte, ta propre clé est obligatoire"
        )),
        DocSection(kind="usage", title="recherche google + scraping", body_md=(
            "interroge tout l'univers google (web, news, images, vidéos, lieux, maps, avis, shopping, scholar, brevets, lens) et scrape une page.\n"
            "- `serper_web_search` — recherche web google, filtrable par site/pays/date (ex. profils sur `linkedin.com/in`)\n"
            "- `serper_news_search` — veille signaux sur une cible (levée, recrutement, presse)\n"
            "- `serper_places_search` — prospection b2b locale (titre, adresse, téléphone, site, note)\n"
            "- `serper_scrape` — récupère le contenu d'une page (texte + markdown), gère le js et l'anti-bot léger"
        )),
    ),
    "serpapi": (
        DocSection(kind="prerequisite", title="obtenir une clé serpapi", body_md=(
            "crée une clé api (`private api key`) dans ton dashboard [serpapi](https://serpapi.com).\n"
            "- colle-la dans tes connecteurs oto sur `/account`\n"
            "- les membres peuvent aussi utiliser la clé plateforme (quota quotidien)"
        )),
        DocSection(kind="usage", title="recherche multi-moteurs", body_md=(
            "atteint des moteurs que serper n'a pas : verticaux google (trends, finance, vols, hôtels, events, jobs), bing, youtube et marketplaces.\n"
            "- `serpapi_search` — appel générique vers n'importe quel moteur serpapi (google_play, duckduckgo, yelp…)\n"
            "- `serpapi_search_jobs` + `serpapi_job_details` — sourcing d'offres via google jobs\n"
            "- `serpapi_google_trends` — intérêt dans le temps / par région pour un terme\n"
            "- `serpapi_youtube_search` / `serpapi_amazon_search` — vidéos, produits"
        )),
    ),
    "hunter": (
        DocSection(kind="prerequisite", title="obtenir une clé hunter.io", body_md=(
            "crée une clé api dans les réglages api de ton compte [hunter.io](https://hunter.io).\n"
            "- colle-la dans tes connecteurs oto sur `/account`\n"
            "- les membres peuvent utiliser la clé plateforme (quota quotidien) ; un guest doit poser la sienne\n"
            "- hunter facture en crédits (1 crédit par appel, 1 par tranche de 10 emails sur le domain search)"
        )),
        DocSection(kind="usage", title="trouver et vérifier des emails", body_md=(
            "découvre les emails d'une entreprise, devine celui d'une personne, et vérifie sa délivrabilité.\n"
            "- `hunter_domain_search` — liste les emails publics trouvés sur un domaine + le pattern d'adresse\n"
            "- `hunter_email_finder` — l'email d'une personne précise dans une boîte (nom + domaine)\n"
            "- `hunter_email_verify` — vérifie qu'une adresse est délivrable"
        )),
    ),
    "kaspr": (
        DocSection(kind="prerequisite", title="obtenir une clé kaspr", body_md=(
            "crée une clé api dans les réglages api/intégrations de ton compte [kaspr](https://app.kaspr.io).\n"
            "- colle-la dans tes connecteurs oto sur `/account` — kaspr est **byo** (pas de clé plateforme, chacun la sienne)\n"
            "- kaspr facture en crédits : 1 par email, +1 par téléphone\n"
            "- vérifie ta clé et tes crédits restants avec `kaspr_verify_key`"
        )),
        DocSection(kind="usage", title="enrichir un contact depuis linkedin", body_md=(
            "récupère emails et téléphones d'une personne à partir de son profil linkedin.\n"
            "- `kaspr_enrich_linkedin` — passe le slug (`alexis-laporte`) ou l'url linkedin complète, options `with_phone` pour les numéros\n"
            "- `kaspr_verify_key` — état du compte + crédits restants"
        )),
    ),
    "fullenrich": (
        DocSection(kind="prerequisite", title="obtenir une clé fullenrich", body_md=(
            "crée une clé api dans les réglages api de ton compte [fullenrich](https://app.fullenrich.com).\n"
            "- colle-la dans tes connecteurs oto sur `/account` — fullenrich est **byo** (chacun sa clé)\n"
            "- facturation **au résultat** : 10 crédits/téléphone, 1/email pro, 3/email perso, rien si aucune donnée trouvée"
        )),
        DocSection(kind="usage", title="enrichissement waterfall (20+ sources)", body_md=(
            "trouve téléphones et emails d'un contact en cascade sur 20+ fournisseurs (~70% de taux sur le téléphone).\n"
            "- `fullenrich_enrich_linkedin` — passe le slug linkedin + prénom/nom (et le nom d'entreprise pour mieux matcher)\n"
            "- renvoie téléphones, emails pro et perso, titre et localisation ; appel asynchrone (~30s à quelques minutes)"
        )),
    ),
    "apollo": (
        DocSection(kind="prerequisite", title="obtenir une clé apollo", body_md=(
            "crée une clé api dans les réglages développeur/api de ton compte [apollo](https://app.apollo.io).\n"
            "- colle-la dans tes connecteurs oto sur `/account` — apollo est **byo** (pas de clé plateforme)\n"
            "- la clé hérite des crédits de ton plan apollo"
        )),
        DocSection(kind="usage", title="prospection b2b (entreprises + contacts)", body_md=(
            "recherche et enrichis entreprises et personnes, et repère les signaux de recrutement.\n"
            "- `apollo_search_organizations` — entreprises par nom, domaine, pays\n"
            "- `apollo_search_people` — personnes par domaines, départements, intitulés, séniorités\n"
            "- `apollo_match_person` — enrichit une personne (url linkedin ou email = meilleurs identifiants)\n"
            "- `apollo_job_postings` — offres d'emploi actives d'une entreprise (signal d'embauche)"
        )),
    ),
    "zerobounce": (
        DocSection(kind="prerequisite", title="obtenir une clé zerobounce", body_md=(
            "crée une clé api dans les réglages api de ton compte [zerobounce](https://www.zerobounce.net).\n"
            "- colle-la dans tes connecteurs oto sur `/account` — zerobounce est **byo** (chacun sa clé)\n"
            "- la clé consomme les crédits de vérification de ton compte"
        )),
        DocSection(kind="usage", title="vérifier la délivrabilité d'emails", body_md=(
            "valide une ou plusieurs adresses email avant un envoi (statut valid, invalid, catch-all, spamtrap…).\n"
            "- `zerobounce_verify_email` — vérifie une adresse\n"
            "- `zerobounce_verify_batch` — jusqu'à 200 adresses en un appel\n"
            "- `zerobounce_credits` — crédits de vérification restants"
        )),
    ),
    "lemlist": (
        DocSection(kind="prerequisite", title="clé api lemlist", body_md=(
            "crée une clé API dans [lemlist](https://app.lemlist.com) (Settings → Integrations → API), "
            "puis colle-la dans oto (page compte / connecteurs).\n"
            "- chacun voit SES campagnes : ta propre clé est requise"
        )),
        DocSection(kind="usage", title="suivre tes campagnes de cold outreach", body_md=(
            "lecture seule (les writes passent par l'UI lemlist, pour éviter un envoi involontaire) : campagnes, leads, stats et activités.\n"
            "- « liste mes campagnes lemlist et leur statut »\n"
            "- « stats de la campagne X (envoyés, ouverts, répondus, bounces) »\n"
            "- « quels leads ont répondu sur cette campagne ? »\n"
            "- « montre les dernières activités (ouvertures, clics, réponses) »"
        )),
    ),
    "unipile": (
        DocSection(kind="prerequisite", title="connexion hébergée unipile", body_md=(
            "connecte tes comptes (LinkedIn, WhatsApp, Telegram, Instagram, Messenger, X) en **auth hébergée "
            "[Unipile](https://www.unipile.com)** depuis le dashboard oto — pas de cookie à coller ni d'extension. "
            "la session tourne chez Unipile (vrai Chrome + proxy résidentiel), ce qui évite les blocages d'empreinte.\n"
            "- option **messagerie hébergée** activée par l'admin de ton org (ou ta propre clé Unipile en BYO)"
        )),
        DocSection(kind="usage", title="prospection linkedin + messagerie multi-canal", body_md=(
            "recherche, scrape et messagerie LinkedIn (et messagerie WhatsApp/Telegram/Instagram/Messenger/X), tu agis comme toi-même.\n"
            "- « recherche LinkedIn des DAF en région lyonnaise dans mon réseau N1 »\n"
            "- « ouvre le profil LinkedIn de ce slug et résume sa carrière »\n"
            "- « envoie une invitation à ce prospect avec une note » puis « réponds dans le fil quand il accepte »\n"
            "- « lis mes dernières conversations WhatsApp et réponds à la dernière »\n"
            "- « montre ma home LinkedIn récente » ou « commente ce post »"
        )),
    ),
    "phantombuster": (
        DocSection(kind="prerequisite", title="ta clé api phantombuster", body_md=(
            "- depuis [phantombuster.com](https://phantombuster.com), ouvre les paramètres de ton organisation puis la section api key\n"
            "- copie ta clé api\n"
            "- colle-la dans tes clés de connecteur oto sous `phantombuster`"
        )),
        DocSection(kind="usage", title="lancer des agents et récupérer leurs résultats", body_md=(
            "déclenche un agent (phantom) puis suis son run et récupère ses résultats.\n"
            "- `phantombuster_get_agent` la configuration et le statut d'un agent\n"
            "- `phantombuster_launch_agent` démarre un run (⚠️ consomme des crédits et agit sur des comptes tiers), renvoie le `containerId`\n"
            "- `phantombuster_list_containers` / `phantombuster_get_container` listent et suivent les runs\n"
            "- `phantombuster_container_results` récupère les résultats json d'un run terminé, `phantombuster_container_output` ses logs"
        )),
    ),
    "crunchbase": (
        DocSection(kind="prerequisite", title="capturer ta session crunchbase (cookie)", body_md=(
            "Crunchbase n'a pas de clé API publique : oto rejoue ta **session connectée**. "
            "capture les cookies de session (+ user-agent) de ton compte [Crunchbase](https://www.crunchbase.com) "
            "depuis la page **compte** du dashboard oto.\n"
            "- sans session configurée, les outils `crunchbase_*` renvoient un message qui pointe vers la page compte"
        )),
        DocSection(kind="usage", title="entreprises, financements et personnes crunchbase", body_md=(
            "récupère des données d'entreprises, leurs levées de fonds et des profils de personnes.\n"
            "- « fiche Crunchbase de `anthropic` (effectif, localisation, fondateurs) »\n"
            "- « liste les tours de financement de cette boîte (date, type, montant, investisseurs) »\n"
            "- « cherche des entreprises sur `vector database` »\n"
            "- « profil Crunchbase de cette personne »"
        )),
    ),
    # ── CRM & messagerie ────────────────────────────────────────────────────
    "attio": (
        DocSection(kind="prerequisite", title="ta clé api attio", body_md=(
            "attio expose une clé api par workspace. va dans les [réglages développeur de ton workspace attio](https://app.attio.com), section **api**, et crée une clé (access token).\n"
            "- colle-la dans oto sur ton compte (`/account`), connecteur **attio**\n"
            "- pas de clé plateforme partagée : chacun pose la sienne\n"
            "- pense à cocher les droits records + notes + tasks + lists selon ce que tu veux faire\n"
            "- note : le connecteur mcp attio officiel est souvent préféré ; oto garde le code pour les implems custom"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton crm attio (companies, people, deals) + notes, tasks, lists et comments depuis claude.\n"
            "- « cherche l'entreprise acme » → `attio_search_companies`, puis `attio_get_company` pour le détail\n"
            "- « crée un contact jean dupont chez acme » → `attio_create_person`\n"
            "- « ajoute une note sur ce deal » → `attio_create_note` (titre + markdown, attaché au record)\n"
            "- « liste mes tâches en cours » → `attio_list_tasks`, et `attio_create_task` pour en ajouter une"
        )),
    ),
    "folk": (
        DocSection(kind="prerequisite", title="ta clé api folk", body_md=(
            "folk fournit une clé api personnelle. récupère-la dans les [réglages api/développeur de ton compte folk](https://app.folk.app) (doc : [developer.folk.app](https://developer.folk.app)).\n"
            "- colle-la dans oto sur ton compte (`/account`), connecteur **folk**\n"
            "- byo uniquement : ta clé, ou le credential partagé de ton org — pas de clé plateforme\n"
            "- les **groupes** ne se créent pas via l'api : crée-les dans l'app folk, puis référence-les par leur id"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "gère ton crm folk (personnes, entreprises, deals) + notes, interactions et rappels depuis claude.\n"
            "- « trouve le contact dupont » → `folk_search` (entity `person`), puis `folk_get` pour la fiche\n"
            "- « ajoute jean dupont, cto chez acme » → `folk_create_person`\n"
            "- « log un appel sur ce contact » → `folk_create_interaction` (type, titre, contenu)\n"
            "- « crée un deal dans le groupe X » → `folk_create_deal`, et `folk_list_deals` pour les lister"
        )),
    ),
    "hubspot": (
        DocSection(kind="prerequisite", title="ton token hubspot (private app)", body_md=(
            "hubspot s'authentifie via un **token de private app**. dans les [réglages de ton compte hubspot](https://app.hubspot.com), va dans **integrations → private apps**, crée une private app et donne-lui les scopes crm voulus (contacts, companies, deals, tickets).\n"
            "- copie le **access token** généré\n"
            "- colle-le dans oto sur ton compte (`/account`), connecteur **hubspot**\n"
            "- byo uniquement : ta clé ou celle partagée de ton org, pas de clé plateforme"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "interroge et édite ton crm hubspot (contacts, companies, deals, tickets) depuis claude.\n"
            "- « cherche les contacts de chez acme » → `hubspot_search` (object_type `contacts`)\n"
            "- « crée un deal à 10k€ » → `hubspot_create` (object_type `deals`)\n"
            "- « les deals associés à ce contact » → `hubspot_associations`\n"
            "- « ajoute une note sur ce contact » → `hubspot_create_note`"
        )),
    ),
    "notion": (
        DocSection(kind="prerequisite", title="ton token d'intégration notion", body_md=(
            "notion s'ouvre via une **intégration interne**. crée-la sur [notion.so/my-integrations](https://www.notion.so/my-integrations), récupère l'**internal integration token**.\n"
            "- **partage les pages/databases voulues avec ton intégration** dans notion (menu `...` → connexions) — sinon elle ne voit rien\n"
            "- colle le token dans oto sur ton compte (`/account`), connecteur **notion**"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "lis et écris pages, databases et blocs notion partagés avec ton intégration.\n"
            "- « retrouve la page roadmap » → `notion_search`\n"
            "- « liste les lignes de cette base où statut = à faire » → `notion_query_database` (avec filtre)\n"
            "- « crée une page sous ce projet » → `notion_create_page`\n"
            "- « ajoute ce paragraphe à la page » → `notion_append_blocks`"
        )),
    ),
    "slack": (
        DocSection(kind="prerequisite", title="connecte slack via le dashboard", body_md=(
            "slack se connecte **depuis le dashboard oto** : clique sur **connect** sur le connecteur **slack** et autorise via l'écran oauth de [slack](https://slack.com).\n"
            "- pas de clé à copier à la main : oto récupère ton **user token** (`xoxp-`)\n"
            "- les messages partent **en ton nom** (comme l'humain connecté), pas comme un bot\n"
            "- à défaut, un admin peut te grant la clé plateforme de ton org"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "envoie et lis des messages slack en ton nom depuis claude.\n"
            "- « envoie un message dans #general » → `slack_post_message`\n"
            "- « dm jean par email » → `slack_find_user_by_email` puis `slack_open_dm` puis `slack_post_message`\n"
            "- « lis les derniers messages de ce canal » → `slack_read_history`\n"
            "- « réagis 👍 à ce message » → `slack_add_reaction`"
        )),
    ),
    "zoho": (
        DocSection(kind="prerequisite", title="self-client oauth zoho crm (3 champs)", body_md=(
            "zoho crm utilise un **self-client oauth2** à 3 secrets. dans la [console développeur zoho api](https://api-console.zoho.com), crée un client de type **self client**, puis génère un grant token et échange-le contre un refresh token. tu dois fournir à oto :\n"
            "- **client_id** — l'id du self client\n"
            "- **client_secret** — son secret\n"
            "- **refresh_token** — le refresh token issu de l'échange (scopes `ZohoCRM.*`)\n"
            "renseigne ces 3 champs dans oto sur ton compte (`/account`), connecteur **zoho**. byo uniquement."
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "crud générique sur tes modules zoho crm (contacts, leads, deals, accounts…) depuis claude.\n"
            "- « liste mes modules » → `zoho_modules`, « liste les deals » → `zoho_records`\n"
            "- « trouve le contact dont l'email = a@b.com » → `zoho_search` (criteria zoho)\n"
            "- « crée un lead » → `zoho_create`, « mets à jour ce deal » → `zoho_update`\n"
            "- « ajoute une note sur ce record » → `zoho_create_note`"
        )),
    ),
    "zohodesk": (
        DocSection(kind="prerequisite", title="self-client oauth zoho desk (4 champs)", body_md=(
            "zoho desk utilise un **self-client oauth2** à 4 secrets. dans la [console développeur zoho api](https://api-console.zoho.com), crée un **self client**, génère un grant token avec les scopes `Desk.*`, puis échange-le contre un refresh token. tu dois fournir à oto :\n"
            "- **client_id** et **client_secret** — du self client\n"
            "- **refresh_token** — issu de l'échange\n"
            "- **org_id** — l'id de ton organisation desk (en-tête `orgId` requis par l'api)\n"
            "renseigne ces 4 champs dans oto sur ton compte (`/account`), connecteur **zohodesk**. byo uniquement."
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "gère le support zoho desk (tickets, threads, contacts) depuis claude.\n"
            "- « liste les tickets ouverts » → `zohodesk_tickets` (status `Open`)\n"
            "- « ouvre le ticket #123 avec son contact » → `zohodesk_ticket` (include `contacts`)\n"
            "- « crée un ticket » → `zohodesk_create_ticket` (subject + departmentId + contactId)\n"
            "- « les réponses de ce ticket » → `zohodesk_ticket_threads`"
        )),
    ),
    # ── données entreprise & open-data ──────────────────────────────────────
    "sirene": (
        DocSection(kind="prerequisite", title="clé api insee sirene", body_md=(
            "les outils d'identité, recherche, bilans et événements (`fr_search`, `fr_get`, `fr_bilans`, `fr_events`…) tournent en open data, **sans clé**.\n"
            "une clé n'est requise que pour les appels **insee sirene** (`fr_siret`, `fr_headquarters` — siret/siège à la source officielle).\n"
            "- crée un compte sur le [portail api insee](https://api.insee.fr) et souscris à l'api sirene\n"
            "- récupère ta clé, puis pose-la sur ton dashboard oto (connecteur `sirene`)\n"
            "- les requêtes **sirene stock** (`fr_stock_*`, parquet local) n'ont **pas** besoin de clé"
        )),
        DocSection(kind="usage", title="données entreprise france", body_md=(
            "interroge identité, finances, dirigeants, événements légaux et appels d'offres d'une entreprise française.\n"
            "- `fr_search(query=…, naf=…, departement=…)` — recherche multicritère (secteur, zone, effectifs, CA)\n"
            "- `fr_get(siren)` — fiche complète agrégée : identité + 7 ratios du dernier bilan inpi + événements bodacc\n"
            "- `fr_bilans(siren)` puis `fr_bilan(siren, date_cloture)` — historique des dépôts et bilan détaillé (CA, EBE, endettement…)\n"
            "- `fr_directors(siren)`, `fr_events(siren)`, `fr_tenders_search(query=…)` — dirigeants, événements bodacc, appels d'offres boamp"
        )),
        DocSection(kind="usage", title="sirene stock (enrichissement en masse)", body_md=(
            "le parquet sirene complet (insee, millésime mensuel) pour les lookups ponctuels et l'enrichissement **batch** de milliers de sirens.\n"
            "- `fr_stock_enrich(sirens=[…])` — sièges d'une **liste** de sirens en un seul scan (bulk)\n"
            "- `fr_stock_siege(siren)` / `fr_stock_etablissements(siren)` — siège ou tous les établissements d'une boîte\n"
            "- `fr_stock_search(naf=…, enseigne=…, departement=…)` — énumère tous les sites (ex. tous les « intermarché » d'un département)"
        )),
        DocSection(kind="usage", title="conventions collectives (kali)", body_md=(
            "le droit de la branche en texte intégral (stock kali/dila complet, ~290k articles) : minima, congés, primes, classifications. filtre idcc natif.\n"
            "- `fr_ccn_conventions(idcc=… | query=…)` — résoudre une convention (« quelle est la 3090 ? », « conventions du spectacle »)\n"
            "- `fr_ccn_search(query=…, idcc=…)` — recherche plein-texte dans les articles d'une branche (ou toutes)\n"
            "- `fr_ccn_get(kali_id)` — texte intégral consolidé d'un article + lien légifrance vérifiable\n"
            "- complément : `fr_accords_search(idcc=…)` — les accords d'**entreprise** de la branche (qui a négocié quoi, quand)"
        )),
    ),
    "hithorizons": (
        DocSection(kind="prerequisite", title="clé api hithorizons", body_md=(
            "connecteur **byo** : chacun connecte son propre compte hithorizons.\n"
            "- crée un compte sur [hithorizons](https://www.hithorizons.com) et abonne-toi à l'api (azure api management)\n"
            "- récupère ta clé d'abonnement (`Ocp-Apim-Subscription-Key`)\n"
            "- pose-la sur ton dashboard oto (connecteur `hithorizons`)"
        )),
        DocSection(kind="usage", title="données entreprise européennes", body_md=(
            "recherche et fiches d'entreprises à l'échelle européenne (pays par défaut FR, surchargeable).\n"
            "- `hithorizons_search_company(name=…, city=…, country=…)` — recherche par nom + ville/code postal\n"
            "- `hithorizons_suggestions(query=…)` — autocomplétion sur le nom\n"
            "- `hithorizons_company(company_id)` — fiche complète à partir d'un id hithorizons"
        )),
    ),
    "topograph": (
        DocSection(kind="prerequisite", title="clé api topograph", body_md=(
            "connecteur **byo** facturé à la requête : chacun connecte son propre compte (pas de clé plateforme).\n"
            "- crée un compte et génère ta clé sur [topograph](https://www.topograph.co) ([doc api](https://docs.topograph.co))\n"
            "- pose-la sur ton dashboard oto (connecteur `topograph`)"
        )),
        DocSection(kind="usage", title="kyb registres européens", body_md=(
            "données et documents kyb normalisés depuis les registres publics européens (FR, GB, DE…).\n"
            "- `topograph_search(query=…, country=…)` — trouve une entreprise par nom ou numéro d'immatriculation\n"
            "- `topograph_company(country=…, registration_number=…)` — données normalisées, `mode=\"onboarding\"` (rapide) ou `\"verification\"` (kyb rigoureux)"
        )),
    ),
    "gr": (
        DocSection(kind="usage", title="entreprises grèce", body_md=(
            "identifie une entité grecque dans les registres publics (gemi + vies), sans clé.\n"
            "- `gr_lookup(query=…)` accepte un **nom**, un **n° gemi** ou un **n° de tva** grec (ΑΦΜ, avec ou sans préfixe `EL`)\n"
            "- renvoie les entreprises correspondantes (nom, n° gemi, tva, statut actif/inactif)\n"
            "- pour un résultat unique : ajoute l'adresse et la validité du n° de tva via vies"
        )),
    ),
    "foncier": (
        DocSection(kind="usage", title="site, parcelle & immobilier", body_md=(
            "tout ce qui caractérise un **site** physique en france : géocodage, cadastre, bâti, risques, solaire, prix immobiliers — open data, sans clé.\n"
            "- `foncier_geocode(adresse)` puis `foncier_parcelle(lat, lon)` / `foncier_bati(lat, lon)` — coordonnées, parcelle cadastrale, emprise bâtie et CES réel\n"
            "- `foncier_icpe(siret=… | code_insee=…)` — installations classées (régime, seveso, ied, inspections dreal)\n"
            "- `foncier_prix_m2(code_commune)` / `foncier_comparables_adresse(adresse)` — stats €/m² et ventes comparables dvf\n"
            "- `foncier_productible_solaire(lat, lon, kwc)` / `foncier_conso_elec(annee, dept)` — productible pv et gros consommateurs électriques"
        )),
    ),
    "urba": (
        DocSection(kind="usage", title="urbanisme & territoire", body_md=(
            "l'enveloppe réglementaire et territoriale d'un point ou d'une commune — open data, sans clé. géocode l'adresse d'abord (`foncier_geocode`).\n"
            "- `urba_zonage(lat, lon)` — zonage plu/plui opposable (géoportail de l'urbanisme), avec le règlement pdf si dispo\n"
            "- `urba_risques(code_insee)` / `urba_argiles(lat, lon)` — risques naturels/technologiques et aléa retrait-gonflement des argiles\n"
            "- `urba_qpv(code_insee)` / `urba_qpv_proximite(lat, lon)` — quartiers prioritaires de la ville\n"
            "- `urba_epfif(code_insee)` / `urba_socio(code_insee)` — secteurs epfif (île-de-france) et profil socio-démo insee"
        )),
    ),
    "frenchtech": (
        DocSection(kind="usage", title="écosystème French Tech (annuaire, events, financements)", body_md=(
            "l'écosystème d'une capitale french tech (défaut aix-marseille) — open data live, sans clé.\n"
            "- `frenchtech_search_annuaire(query=…, secteur=…, ville=…)` — entreprises de l'annuaire (startups/structures/prestataires) avec dirigeant, email, téléphone, site, secteurs, besoins : dataset de prospection b2b\n"
            "- `frenchtech_get_annuaire(slug)` — fiche entreprise complète\n"
            "- `frenchtech_evenements()` / `frenchtech_appels()` — événements (meetups, confs) et appels à projet / concours / ami\n"
            "- `frenchtech_financements()` — dispositifs de financement (type, montant, stade, critères)\n"
            "- `frenchtech_ftc_scenarios()` — rdv french tech central bookables (correspondants de l'état : inpi, urssaf, douanes, bpifrance…)"
        )),
    ),
    "sante": (
        DocSection(kind="usage", title="établissements de santé & ESSMS", body_md=(
            "annuaire des établissements sanitaires et médico-sociaux + évaluations qualité has — open data, sans clé.\n"
            "- `sante_finess_search(q=…, departement=…, categorie=…)` — recherche finess par nom ou code (ex. categorie « EHPAD »)\n"
            "- `sante_finess(finess)` — fiche d'un établissement par code exact (ET ou EJ)\n"
            "- `sante_essms_dimensions()` puis `sante_essms_search(region_libelle=…, secteur=…)` — évaluations qualité des essms (référentiel has)"
        )),
    ),
    "infosec": (
        DocSection(kind="usage", title="empreinte numérique d'un domaine", body_md=(
            "recon **passif** / osint d'un domaine (rien d'intrusif, sources publiques) — open data, sans clé.\n"
            "- `infosec_whois(domain)` / `infosec_dns(domain)` — immatriculation rdap et enregistrements dns (avec indices de stack mail/saas)\n"
            "- `infosec_email_security(domain)` — posture spf/dmarc/dkim, signal de maturité it d'un prospect\n"
            "- `infosec_subdomains(domain)` — sous-domaines connus via les logs certificate transparency (crt.sh)\n"
            "- `infosec_tls(domain)` / `infosec_headers(domain)` — certificat tls et en-têtes http de sécurité"
        )),
    ),
    "culture": (
        DocSection(kind="usage", title="spectacle vivant (open data culture)", body_md=(
            "source publique du Ministère de la Culture, sans clé.\n"
            "- `culture_spectacle_search` / `culture_spectacle_get` — recherche d'entreprises et de licences du spectacle vivant\n"
            "- `culture_spectacle_stats` — agrégats (volumes par zone, période…)"
        )),
    ),
    "reddit": (
        DocSection(kind="usage", title="recherche & lecture reddit", body_md=(
            "api publique reddit, sans clé.\n"
            "- `reddit_search` / `reddit_subreddit` — recherche de posts et lecture d'un subreddit\n"
            "- `reddit_search_subreddits` — trouve les subreddits pertinents pour un sujet\n"
            "- `reddit_post` — lit un post et son arbre de commentaires"
        )),
    ),
    # ── recrutement (ATS) ───────────────────────────────────────────────────
    "greenhouse": (
        DocSection(kind="prerequisite", title="ta clé api greenhouse (harvest)", body_md=(
            "il te faut une clé **harvest api** greenhouse.\n"
            "- dans greenhouse, va dans **configure → dev center → api credentials** et crée une clé de type *harvest*\n"
            "- donne-lui les permissions candidats/jobs/applications/users\n"
            "- colle-la dans tes [clés de connecteurs](https://app.oto.ninja/) (ou laisse ton org partager la sienne)\n"
            "- doc éditeur : [greenhouse.io](https://www.greenhouse.io)\n"
            "- ⚠️ les écritures (créer un candidat, ajouter une note) exigent un `on_behalf_of` = l'id d'un utilisateur greenhouse, récupéré via `greenhouse_users`"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton ats greenhouse depuis la conversation : candidats, jobs, candidatures, notes.\n"
            "- « liste les candidats sur le job 123 » → `greenhouse_candidates` (filtres `job_id`, `email`, `created_after`)\n"
            "- « montre-moi le candidat 456 et ses candidatures » → `greenhouse_candidate`\n"
            "- « ajoute une note sur le candidat 456 » → `greenhouse_add_note` (il faut un `user_id` auteur, cf. `greenhouse_users`)\n"
            "- « quels jobs sont ouverts ? » → `greenhouse_jobs` (`status` open/closed/draft)"
        )),
    ),
    "lever": (
        DocSection(kind="prerequisite", title="ta clé api lever", body_md=(
            "il te faut une clé **api lever**.\n"
            "- dans lever, va dans **settings → integrations and API → API credentials** et génère une clé\n"
            "- colle-la dans tes [clés de connecteurs](https://app.oto.ninja/) (ou laisse ton org partager la sienne)\n"
            "- doc éditeur : [lever.co](https://www.lever.co)\n"
            "- ⚠️ les écritures (créer un candidat, ajouter une note) exigent un `perform_as` = l'id d'un utilisateur lever, récupéré via `lever_users`"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton ats lever : un candidat = une **opportunity**, un poste = un **posting**.\n"
            "- « liste les candidats du posting abc » → `lever_opportunities` (filtres `posting_id`, `stage_id`, `email` ; `expand` pour déplier stage/owner)\n"
            "- « détaille l'opportunity xyz » → `lever_opportunity`\n"
            "- « ajoute une note sur cette opportunity » → `lever_add_note` (avec `perform_as`, cf. `lever_users`)\n"
            "- « quels sont mes postes publiés ? » → `lever_postings` (`state` published/closed/draft…) ; les étapes de pipeline → `lever_stages`"
        )),
    ),
    "ashby": (
        DocSection(kind="prerequisite", title="ta clé api ashby", body_md=(
            "il te faut une clé **api ashby**.\n"
            "- dans ashby, va dans **admin → integrations → API** et crée une clé\n"
            "- colle-la dans tes [clés de connecteurs](https://app.oto.ninja/) (ou laisse ton org partager la sienne)\n"
            "- doc éditeur : [ashbyhq.com](https://www.ashbyhq.com)"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton ats ashby : candidats, jobs, candidatures, notes.\n"
            "- « trouve le candidat dont l'email est x@y.com » → `ashby_search_candidates` (par `email` et/ou `name`)\n"
            "- « liste les candidats » → `ashby_candidates`, puis le détail → `ashby_candidate`\n"
            "- « ajoute une note sur ce candidat » → `ashby_add_note`\n"
            "- « quels jobs sont ouverts ? » → `ashby_jobs` (`status` Open/Closed/Draft/Archived) ; les candidatures → `ashby_applications` (filtre `job_id`)"
        )),
    ),
    "teamtailor": (
        DocSection(kind="prerequisite", title="ta clé api teamtailor", body_md=(
            "il te faut une clé **api teamtailor**.\n"
            "- dans teamtailor, va dans **settings → integrations → API keys** et génère une clé\n"
            "- colle-la dans tes [clés de connecteurs](https://app.oto.ninja/) (ou laisse ton org partager la sienne)\n"
            "- doc éditeur : [teamtailor.com](https://www.teamtailor.com)"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton ats teamtailor : candidats, jobs, candidatures.\n"
            "- « liste les candidats » → `teamtailor_candidates` (filtre `email`), détail d'un candidat → `teamtailor_candidate`\n"
            "- « crée un candidat jean dupont » → `teamtailor_create_candidate` (attributs `first-name`, `last-name`, `email`, `phone`, `pitch`, `tags`…)\n"
            "- « quels jobs sont ouverts ? » → `teamtailor_jobs` (`status` open/draft/archived/unlisted)\n"
            "- « montre les candidatures sur le job 99 » → `teamtailor_job_applications` (filtre `job_id`)"
        )),
    ),
    "recruitee": (
        DocSection(kind="prerequisite", title="ton token api + company id recruitee", body_md=(
            "recruitee demande **deux champs** :\n"
            "- `api_token` — ton token api personnel (recruitee, **settings → apps & plugins → personal API tokens**)\n"
            "- `company_id` — l'identifiant de ta société recruitee (visible dans l'url de ton espace, ex. `recruitee.com/c/<company_id>`)\n"
            "renseigne les deux dans tes [clés de connecteurs](https://app.oto.ninja/).\n"
            "- doc éditeur : [recruitee.com](https://www.recruitee.com)"
        )),
        DocSection(kind="usage", title="ce que tu peux faire", body_md=(
            "pilote ton ats recruitee : un poste = une **offer**, un candidat est rattaché à des offers.\n"
            "- « liste les candidats du poste 12 » → `recruitee_candidates` (filtres `offer_id`, `query` par nom/email), détail → `recruitee_candidate`\n"
            "- « crée un candidat et attache-le à l'offer 12 » → `recruitee_create_candidate` (`offer_ids`)\n"
            "- « ajoute une note sur ce candidat » → `recruitee_add_note`\n"
            "- « liste mes offres actives » → `recruitee_offers` (`scope` active/archived, `kind` job/talent_pool), détail → `recruitee_offer`"
        )),
    ),
    # ── finance & paie ──────────────────────────────────────────────────────
    "pennylane": (
        DocSection(kind="prerequisite", title="ta clé api pennylane", body_md=(
            "chaque utilisateur pose sa propre clé pennylane — ta compta n'est visible que par toi.\n"
            "- connecte-toi sur [app.pennylane.com](https://app.pennylane.com)\n"
            "- va dans les paramètres, section api / intégrations, et crée une clé api (token personnel)\n"
            "- colle-la dans tes clés de connecteur oto sous `pennylane`"
        )),
        DocSection(kind="usage", title="lire et lettrer ta compta", body_md=(
            "interroge factures, transactions et balance, et solde les paiements non rapprochés.\n"
            "- `pennylane_trial_balance` la balance comptable sur une période, `pennylane_ledger_accounts` le plan comptable\n"
            "- `pennylane_customer_invoices` / `pennylane_supplier_invoices` les factures, `pennylane_transactions` les mouvements bancaires\n"
            "- `pennylane_match` lettre une transaction avec sa facture (réversible) pour ne pas laisser une facture payée en `late`\n"
            "- flux avoir supervisé : `pennylane_find_invoice_by_reference` (anti-doublon) → `pennylane_create_credit_note` (brouillon) → `pennylane_finalize_invoice` puis `pennylane_send_invoice` **après validation humaine**"
        )),
    ),
    "gocardless": (
        DocSection(kind="prerequisite", title="ta clé api gocardless", body_md=(
            "lecture seule — chaque utilisateur pose sa propre clé, tes prélèvements ne sont visibles que par toi.\n"
            "- depuis le [dashboard gocardless](https://gocardless.com), ouvre developers puis create access token\n"
            "- choisis un token en **lecture** (read-only) — oto n'annule ni ne crée de prélèvement\n"
            "- colle-le dans tes clés de connecteur oto sous `gocardless`"
        )),
        DocSection(kind="usage", title="suivre prélèvements et échecs sepa", body_md=(
            "consulte tes prélèvements, leur timeline et les motifs d'échec pour la réconciliation.\n"
            "- `gocardless_payments` liste les prélèvements (filtre par `status`, mandat, customer, date)\n"
            "- `gocardless_failed` te sort en un appel les prélèvements refusés enrichis (client, montant, cause, `will_attempt_retry`)\n"
            "- `gocardless_failure_reason` donne le motif du dernier échec d'un paiement précis (`PM…`)\n"
            "- `gocardless_payment_party` résout paiement → mandat → client (email, société)"
        )),
    ),
    "silae": (
        DocSection(kind="prerequisite", title="tes accès api silae paie", body_md=(
            "silae paie v1 utilise des identifiants oauth2 à **trois champs** ; chaque cabinet/employeur saisit les siens, sa paie n'est visible que par lui. demande-les à ton contact [silae](https://www.silae.fr) ou via ton espace api.\n"
            "- `client_id` — identifiant de l'application api\n"
            "- `client_secret` — secret associé\n"
            "- `subscription_key` — clé d'abonnement à l'api silae paie\n"
            "renseigne ces trois champs dans tes clés de connecteur oto sous `silae`"
        )),
        DocSection(kind="usage", title="consulter dossiers, salariés et bulletins", body_md=(
            "lecture seule de la paie (les coordonnées bancaires sont masquées avant de t'arriver).\n"
            "- `silae_dossiers` liste les dossiers de paie accessibles, `silae_dossier_current_period` la période ouverte\n"
            "- `silae_employees` les salariés d'un dossier, `silae_employee` le détail d'un salarié par matricule\n"
            "- `silae_payslips` les bulletins d'une période, puis `silae_payslip_header` / `silae_payslip_lines` / `silae_payslip_totals` pour le détail d'un bulletin\n"
            "- `silae_variables_to_enter` les variables de paie (EVP) encore à saisir sur un dossier"
        )),
    ),
    # ── automatisation ──────────────────────────────────────────────────────
    "n8n": (
        DocSection(kind="prerequisite", title="ta clé api n8n + url d'instance", body_md=(
            "n8n s'auto-héberge ou tourne en cloud, donc deux champs sont attendus.\n"
            "- `api_key` — depuis ton instance, ouvre settings puis n8n API et crée une clé api\n"
            "- `base_url` — l'url de ton instance (ex. `https://ton-instance.app.n8n.cloud` ou ton url self-hosted)\n"
            "renseigne les deux dans tes clés de connecteur oto sous `n8n`. plus d'infos sur [n8n.io](https://n8n.io)"
        )),
        DocSection(kind="usage", title="piloter workflows et exécutions", body_md=(
            "liste, active et inspecte tes workflows et leurs runs.\n"
            "- `n8n_list_workflows` liste les workflows (filtre `active`, `tags`), `n8n_get_workflow` détaille un workflow\n"
            "- `n8n_activate_workflow` / `n8n_deactivate_workflow` démarrent ou stoppent ses triggers/cron\n"
            "- `n8n_list_executions` les exécutions (filtre par workflow ou `status` success/error/waiting)\n"
            "- `n8n_get_execution` le détail d'une exécution (avec `include_data` pour les données par nœud)"
        )),
    ),
    "make": (
        DocSection(kind="prerequisite", title="ton token api make + url de zone", body_md=(
            "make est régionalisé (eu1/us1/eu2…), donc deux champs sont attendus.\n"
            "- `api_token` — depuis [make.com](https://www.make.com), ouvre profile puis API/SDK et génère un token\n"
            "- `base_url` — l'url de ta zone make (ex. `https://eu1.make.com`)\n"
            "renseigne les deux dans tes clés de connecteur oto sous `make`"
        )),
        DocSection(kind="usage", title="lister et exécuter tes scénarios", body_md=(
            "un workflow make = un **scénario**, qui appartient à une équipe d'une organisation.\n"
            "- `make_list_organizations` puis `make_list_teams` pour découvrir les ids, `make_list_scenarios` les scénarios d'une équipe\n"
            "- `make_get_scenario` les métadonnées d'un scénario, `make_get_scenario_blueprint` la structure de ses modules\n"
            "- `make_run_scenario` déclenche un run (avec un `data` d'entrée optionnel)\n"
            "- `make_list_scenario_logs` les logs d'exécution d'un scénario"
        )),
    ),
    "zapier": (
        DocSection(kind="prerequisite", title="ta clé api zapier ai actions", body_md=(
            "zapier expose aux agents un catalogue d'**actions** que tu autorises explicitement — pas une api de gestion des zaps.\n"
            "- va sur [actions.zapier.com](https://actions.zapier.com), choisis les actions à exposer\n"
            "- récupère la clé api associée (en-tête `x-api-key`) ; le jeu d'actions exposées est attaché à cette clé\n"
            "- colle-la dans tes clés de connecteur oto sous `zapier`"
        )),
        DocSection(kind="usage", title="exécuter tes actions zapier en langage naturel", body_md=(
            "découvre les actions autorisées et lance-les via une directive en langage naturel.\n"
            "- `zapier_list_actions` liste les actions exposées par ta clé (id, description, champs)\n"
            "- `zapier_execute_action` lance une action via son `action_id` + des `instructions` (zapier remplit les champs laissés en mode « ai guess »)\n"
            "- passe `preview_only=True` pour voir ce qui serait fait sans l'exécuter\n"
            "- `zapier_execution_log` te donne le détail d'une exécution"
        )),
    ),
    # ── dev / design ────────────────────────────────────────────────────────
    "figma": (
        DocSection(kind="prerequisite", title="clé api figma", body_md=(
            "génère un personal access token dans [Figma](https://www.figma.com) "
            "(Settings → Security → Personal access tokens), puis colle-le dans oto.\n"
            "- byo : ta propre clé donne accès à TES fichiers"
        )),
        DocSection(kind="usage", title="fichiers, exports d'images et commentaires", body_md=(
            "inspecte des fichiers Figma/FigJam, exporte des rendus d'images et gère les commentaires.\n"
            "- « donne la structure de ce fichier Figma (clé `abc123`) »\n"
            "- « exporte ces nodes en PNG @2x »\n"
            "- « liste les commentaires du fichier »\n"
            "- « poste un commentaire `à revoir` sur ce fichier »"
        )),
    ),
    "supabase": (
        DocSection(kind="prerequisite", title="clé api supabase (pat)", body_md=(
            "crée un personal access token (`sbp_…`) dans [Supabase](https://supabase.com) "
            "(Account → Access Tokens), puis colle-le dans oto.\n"
            "- c'est un token **Management API** (pas une clé de projet)"
        )),
        DocSection(kind="usage", title="management api : projets, auth, logs", body_md=(
            "pilote tes projets Supabase via la Management API : liste, config d'auth, requêtes de logs.\n"
            "- « liste mes projets Supabase »\n"
            "- « montre la config auth du projet `doeb…` (site_url, redirect allow-list, providers) »\n"
            "- « sors les derniers `auth_logs` du projet »\n"
            "- « requête les `postgres_logs` sur les 2 dernières heures »"
        )),
    ),
    # ── veille ──────────────────────────────────────────────────────────────
    "cloro": (
        DocSection(kind="prerequisite", title="clé api cloro", body_md=(
            "crée une clé API dans [Cloro](https://cloro.dev), puis colle-la dans oto.\n"
            "- les members consomment un quota plateforme si aucune clé perso/org n'est posée"
        )),
        DocSection(kind="usage", title="veille ai-search + serp google en json", body_md=(
            "interroge les moteurs IA (ChatGPT, Gemini, Perplexity, Copilot, Grok, Google AI Mode) et capture leurs réponses + sources — veille de marque « AI SEO » — plus la SERP/News Google en JSON propre. (les appels moteurs IA prennent ~30-45 s.)\n"
            "- « que dit ChatGPT de la marque X ? » (réponse + citations)\n"
            "- « compare ce que disent Gemini et Perplexity sur ce produit »\n"
            "- « SERP Google de `meilleur CRM` avec l'AI Overview »\n"
            "- « Google News sur cette entreprise »"
        )),
    ),
    "brightdata": (
        DocSection(kind="note", title="connecteur bientôt disponible", body_md=(
            "le connecteur **Bright Data** est en cours d'implémentation (coquille vide pour l'instant). "
            "reviens bientôt — voir [brightdata.com](https://brightdata.com)."
        )),
    ),
}
