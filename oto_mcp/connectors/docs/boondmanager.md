## prerequisite — client token + client key + user token boondmanager

un admin Boond copie le **client token** et la **client key** depuis l'interface administrateur (tableau de bord, espace développeur / API), puis l'utilisateur pour qui les appels agiront copie son **user token** (paramètres → sécurité). Les trois se collent dans oto. Référence : [authentification de l'API Boond](https://doc.boondmanager.com/api-externe/).
- ⚠️ **l'accès à l'API REST doit être autorisé** dans le compte Boond (tableau de bord admin ou paramètres → sécurité), sinon tout appel est refusé
- les appels agissent avec **les droits de l'utilisateur** du user token : ce qu'il ne voit pas dans Boond, le connecteur ne le voit pas
- byo-only : c'est le CRM de ta société, chaque organisation pose ses jetons
- ⚠️ Boond compte les appels d'API **par mois** (500 par manager sur l'offre Core, davantage sur les offres supérieures) : chaque appel d'un tool en consomme un

## usage — le CRM d'une ESN

- « retrouve ce contact » → `boondmanager_search(entity="contacts", keywords="dupont")`, ou par email : `keywords_type="emails"`
- « les contacts de cette société » → `boondmanager_search(entity="contacts", keywords="CSOC<id>")`
- « l'historique avec ce contact » → `boondmanager_search(entity="actions", keywords="CCON<id>", sort="startDate", order="desc")`
- « les opportunités ouvertes » → `boondmanager_dictionary(path="setting.state.opportunity")` pour l'id de l'état, puis `boondmanager_search(entity="opportunities", filters={"opportunityStates": [id]})`
- « la fiche complète » → `boondmanager_get(entity="contacts", record_id=…)`
- « ajoute ce contact » → chercher d'abord (Boond ne dédoublonne pas), trouver ou créer la société, puis `boondmanager_create(entity="contacts", attributes={…}, relationships={"company": {"type": "company", "id": …}})` — **dry-run par défaut**, `dry_run=false` pour créer
- « note cet échange » → `boondmanager_create(entity="actions", attributes={"typeOf": <id>, "text": "…"}, relationships={"dependsOn": {"type": "contact", "id": …}})`, l'id du type venant de `boondmanager_dictionary(path="setting.action.contact")` ; dates d'action au format `2026-10-02T09:30:00+0200`
- le connecteur **ne modifie ni ne supprime jamais** rien dans Boond

## note — ce qui trompe

- ⚠️ **pas de contact sans société** dans Boond : la société d'abord
- les états, types, origines et types d'action sont des **ids propres à chaque compte** : toujours les lire dans le dictionnaire, ne jamais les deviner
- un préfixe d'id que l'entité ne connaît pas ferait répondre 0 ligne à Boond sans erreur : le connecteur le refuse et dit lesquels sont acceptés
- `max_results` au-delà de 500 (100 pour les actions) ferait retomber Boond à 30 sans le dire : le connecteur le refuse
- des jetons invalides, ou un accès API non autorisé, reviennent de Boond en **422** (« unable to load agency key »), pas en 401 : le connecteur le traduit en refus d'accès
- tout attribut ou relation que Boond ne connaît pas est refusé **avant** l'appel, avec la liste acceptée : les règles du connecteur sont celles des schémas de création de la référence Boond
- une action se rattache à un contact, une opportunité, un projet… mais **pas à une société** (`dependsOn` n'accepte pas `company`)
