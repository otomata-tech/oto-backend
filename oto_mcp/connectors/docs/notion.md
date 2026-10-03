## prerequisite — ton token d'intégration notion

notion s'ouvre via une **intégration interne**. crée-la sur [notion.so/my-integrations](https://www.notion.so/my-integrations), récupère l'**internal integration token**.
- **partage les pages/databases voulues avec ton intégration** dans notion (menu `...` → connexions) — sinon elle ne voit rien
- pour les **commentaires**, coche « read comments » et « insert comments » dans les capacités de l'intégration (décochées par défaut)
- colle le token dans oto sur ton compte (`/account`), connecteur **notion**

## usage — ce que tu peux faire

lis et écris pages, databases et blocs notion partagés avec ton intégration.
- « retrouve la page roadmap » → `notion_search` — un zéro peut vouloir dire « rien n'est partagé avec l'intégration » : la réponse porte alors un `warning` qui dit comment trancher (relancer avec `query=""`). une réponse rend au plus 100 objets : `has_more: true` → repasse son `next_cursor` en `cursor` pour la page suivante
- « qu'est-ce qui a changé hier dans notion ? » → `notion_search` avec `query=""` et `edited_on="AAAA-MM-JJ"` (jour UTC) : tous les objets édités ce jour-là, en une réponse
- « liste les lignes de cette base où statut = à faire » → `notion_query_database` (avec filtre)
- « crée une page sous ce projet » → `notion_create_page`
- « ajoute ce paragraphe à la page » → `notion_append_blocks` (`position="start"` pour écrire en haut)
- « corrige cette phrase / réécris la section » → `notion_get_markdown` puis `notion_edit_markdown` (rechercher-remplacer)
- « range cette page dans Archives » → `notion_move_page`
- « commente / réponds au commentaire » → `notion_get_comments`, `notion_add_comment`
- « ajoute une colonne Échéance à la base » → `notion_update_database` ; « crée une base » → `notion_create_database`
- « crée une vue tableau filtrée sur À faire » → `notion_view`

une base a une ou plusieurs **data sources** (API notion 2025-09-03) : `search` les rend sous l'objet `data_source`. les outils base acceptent l'id de la base OU de sa data source — jamais celui d'une **vue liée** (copie d'une base posée dans une autre page) : prends l'id de la base d'origine.
