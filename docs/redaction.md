# Rédaction de champs (anonymisation des sorties connecteurs)

Masquer/pseudonymiser des champs des **réponses d'outils** avant qu'elles atteignent
l'agent (use-case d'origine : analyser un profil/CV candidat sans son identité).

## Principe : un middleware unique (pas de câblage par connecteur)

`middleware.FieldRedactionMiddleware` (`on_call_tool`, **enregistré en dernier** dans
`server._build_mcp` → enveloppe les autres, retouche le **résultat final**). Pour tout
tool : `access.resolve_field_filter(namespace_of(name))` → applique le `FieldFilter`
(oto-core) au résultat. Donc la rédaction est **disponible sur TOUS les connecteurs**
sans code par-connecteur (≠ l'ancien filtrage client-level de folk/silae/pennylane,
retiré).

- **Deux canaux réémis** depuis la version redactée : `structured_content` **ET**
  `content` (TextContent JSON) — sinon le canal brut fuit (l'agent lit surtout `content`).
- **Fail-closed** : si une policy existe et que `apply` lève (ex. Faker absent) → on
  **retient** la sortie (`_withheld`), jamais le brut. `is_empty` (pas de policy) =
  passe-through. Échec de *résolution* (aléa DB) → passe-through, sauf service à défaut
  serveur (aucun aujourd'hui).
- `FieldFilter` matche par **nom de clé feuille, récursif** (à toute profondeur). ⚠️
  aveugle au contexte : une règle sur `name` touche aussi `skills[].name` — d'où
  l'importance du schéma observé + dry-run pour ne pas corrompre.

## Rien par défaut + templates 1-clic

`field_filter_defaults.SERVER_DEFAULTS = {}` — **aucune** rédaction par défaut (la PII
n'est pas toujours un risque : CRM/inbox/annuaire = c'est le but ; un défaut large
casserait ces connecteurs). L'org **active explicitement** ce qu'elle veut.
`TEMPLATES` (`candidate`, `bank_details`) = jeux de règles **applicables en 1 clic**
depuis le dashboard (≠ défaut imposé).

## Schéma OBSERVÉ = source de vérité (pas déclaré)

Les sorties connecteurs sont des **passthrough d'API tierces qu'on ne possède pas**
(Unipile, ATS, Apollo…) — leur réponse passe quasi telle quelle à l'agent. Donc :
- on **ne peut pas déclarer** un schéma fiable (il dérive ; vérifié : les API ne
  publient pas le schéma de **réponse** — Unipile = « Try It! »).
- le schéma juste = **ce qui transite** → `connector_schema_store` extrait, de chaque
  réponse, un **squelette clés+types** (JAMAIS de valeurs/PII : feuilles scalaires +
  listes de scalaires, avec leurs chemins) et le persiste par service (table
  `connector_schemas`, fusion incrémentale, cache process anti-write-par-appel).
- Multi-chemins gardés (`name → skills[].name · languages[].name`) → rend l'ambiguïté
  du matching par clé **visible** dans l'UI.
- **Garde-fou anti-empilement** : union-only donc monotone, mais converge (clés nommées,
  tableaux collapsés en `[]`) ; cap `_MAX_KEYS=1000` / `_MAX_PATHS_PER_KEY=50` contre les
  réponses à **clés dynamiques** (map keyée par id). Spine/données user (`oto`/`run`/
  `feedback`/`data`/`scout`) exclus de la capture. Pas de purge par fraîcheur (clé
  retirée par l'API = persiste, inoffensif : règle no-op).

Le bundle `GET /api/orgs/{id}/field-filters` fusionne **observé + curé**
(`connector_field_schema`, libellés/sensibilité) → l'UI affiche le vrai schéma sans
dry-run dès qu'un peu de trafic a coulé. Cold-start (connecteur jamais appelé) = vide →
le dry-run charge depuis un échantillon.

## Dry-run (preview)

Capacité `org.field_filters.preview` (MCP `oto_preview_org_field_filter` + REST
`POST /api/orgs/{id}/field-filters/{service}/preview`) : passe un échantillon réel dans
le filtre, renvoie le redacté → on **voit** ce qui est masqué (clés imbriquées incluses),
sans deviner. Alimente le panneau « tester le filtrage » du dashboard.

## Moteur (oto-core `FieldFilter`)

Actions : `mask` (preserve email/phone/iban, keep_first/last), `pseudonym` (kind, **Faker**
→ extra `oto-core[anonymize]`), `generalize`, `hash`, `anonymize`, `drop`. ⚠️ une clé
matchée à valeur **liste de scalaires** (`emails: [...]`) est masquée **élément par
élément** (corrigé v1.10.0/1.10.1 — sinon fuite ; couvre aussi les listes mixtes).

## Surfaces & fichiers
- backend : `middleware.py` (FieldRedactionMiddleware), `connector_schema_store.py`,
  `field_filter_defaults.py` (SERVER_DEFAULTS vide + TEMPLATES), `connector_field_schema.py`
  (curé, libellés), `capabilities/orgs_field_filters.py` (get/set/preview), `db.py`
  (`connector_schemas`).
- oto-core : `oto/tools/common/field_filter.py`.
- dashboard : `ConnectorTransforms.vue` (schéma + toggle on/off + éditer + templates),
  `FieldRuleDialog.vue`, `RedactionPreview.vue` (dry-run).
