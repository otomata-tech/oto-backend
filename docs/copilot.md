---
title: L'agent intégré à l'application (copilot)
type: explanation
description: >-
  La boucle de l'agent qui travaille DANS l'application, côté serveur : quatre routes REST
  en flux (SSE), les outils de la personne exécutés en interne comme la cible d'`oto_call`,
  une porte d'approbation bâtie sur les DÉCLARATIONS des outils, des conversations en base
  liées à leur compte, une annulation lue dans le journal des révisions, et des appels au
  modèle réservés puis réglés au budget d'unités, sous un plafond de dépense par compte et par
  jour. Session interactive seulement. Fermé par défaut (`OTO_COPILOT_SUBS`, `*` = tous).
---

# L'agent intégré à l'application (copilot)

Le front ouvre un panneau où la personne parle à un agent qui manipule le produit pour
elle : ses agents, procédures, projets, docs, tableaux, connecteurs. **La boucle tourne
ici**, jamais dans le front (qui ne détient aucune clé de modèle). Le contrat de fil est
celui du front (`src/lib/copilot/protocol.ts`, `transport.ts`) ; le serveur de référence
de développement du front en était la spécification exécutable.

## Les routes (REST seule, `capabilities/copilot/routes.py`)

| route | réponse |
| --- | --- |
| `POST /api/copilot/turns` `{thread_id, text, input, mode, page_context, client_tools, speak, attachments}` | flux SSE |
| `POST /api/copilot/threads/{id}/client-results` `{call_id, ok, result, error, client_tools, speak}` | flux SSE |
| `POST /api/copilot/threads/{id}/approvals/{approval_id}` `{decision, edits, remember, speak}` | flux SSE |
| `POST /api/copilot/actions/{revert_id}/revert` | JSON `{ok, undone, skipped}` |
| `POST /api/copilot/attachments` multipart `file` | JSON `{id, name, media_type, size}` |
| `GET /api/copilot/attachments` | JSON `{types: {media_type: max_bytes}, max_per_turn}` |

- **Nées capacités** : un handler qui rend un `EventStream` est servi par l'adaptateur REST
  en `text/event-stream` (`api/base._file_stream`, CORS compris) ; `RestBinding.media` le
  publie dans `/openapi.json`. ⚠️ Le corps s'écoule APRÈS le handler : le flux pose lui-même
  identité, org et geste, et rend ses échecs DANS le flux (`error` puis `done`).
- **Qui** : membre de l'org consultée (`X-Oto-Org`), et `sub` dans `OTO_COPILOT_SUBS`
  (vide = personne → 403 `copilot_not_enabled` ; `*` = tout compte, les autres gardes
  restant les mêmes). Jamais en « voir en tant que » (403).
- **Une session interactive, jamais un jeton** : un jeton `oto_` (d'une personne, délégué,
  clé d'org), un secret de worker ou une identité de service est refusé, 403
  `api_token_forbidden`, deux fois — à l'authentification (`RestBinding.allow_api_token=False`)
  et dans la capacité, sur la source de la requête (`geste` ≠ `console`). Un jeton ferait du
  panneau un point d'accès au modèle payé par l'instance, piloté par un script.
  ⚠️ Pas de 404 sur la route des tours : le front y lit « backend absent ».
- Une conversation est **liée à son `sub`** : un autre compte reçoit « introuvable », une
  autre org en ouvre une neuve.

## La boucle (`boucle.py`, `modele.py`)

Messages Anthropic en flux (`claude-opus-5-5`, effort `medium`, réflexion adaptative
`display: "updates"` sous la bêta `thinking-display-updates-2026-08-18` : les notes entre
deux appels d'outils deviennent les événements `progress`), cache sur le prompt système,
sur le dernier outil du socle et sur le DERNIER MESSAGE : l'historique se relit au cache
d'une étape à l'autre au lieu d'être repayé entier à chaque étape (sans lui, une
conversation de n étapes coûte de l'ordre de n² jetons d'entrée). Le point de cache est
posé sur une COPIE envoyée, jamais dans l'historique stocké (il resterait sinon sur chaque
message passé, au-delà des quatre points de l'API). **L'historique ne se réécrit jamais** : les blocs de
réflexion repartent tels quels, un tour interrompu se solde en AJOUTANT des résultats
« interrompu ». `<next>a | b</next>` en fin de réponse devient `suggestions`, jamais du
texte. Un résultat d'outil est coupé à 80 000 caractères, et la coupe est dite au modèle.
30 étapes au plus par étape de la personne.

**Une sortie coupée à `max_tokens` ne finit jamais en silence.** Le cas typique : un long
texte réécrit en entier dans un appel (le HTML d'une app, 40 000 caractères, pour changer
une ligne). L'appel coupé ne s'exécute pas (son entrée est tronquée) ; il reçoit un résultat
en erreur qui dit au modèle d'éditer par morceaux (`oto_apps` op=patch, `op=patch` d'une
page) et le tour reprend, **une fois** par jambe. Coupé de nouveau — ou coupé au milieu d'une
réponse sans appel — : chaque appel est soldé, l'événement `error` le dit à la personne,
`done`. Avant, `stop_reason` autre que `tool_use` valait « fini » : la conversation
s'arrêtait sans un mot et sans une ligne de journal.

## Pièces jointes (`pieces.py`, `api/copilot.py`)

La personne joint jusqu'à **5 fichiers** à un message : images png, jpeg, gif, webp (5 Mo, la
borne du modèle pour une image), PDF (20 Mo), texte UTF-8 `text/plain`, `text/markdown`,
`text/csv`, `application/json` (512 Ko). `GET /api/copilot/attachments` publie cette table
(une seule source, `pieces.TYPES`) ; le panneau n'offre le trombone que s'il répond 200.

- **Déposé d'abord** : `POST /api/copilot/attachments` (multipart — route de NATURE, hors
  capacité, sous la MÊME porte que les routes de l'agent : session interactive, allowlist,
  membre de l'org consultée, jamais en « voir en tant que »). **Les octets décident** : une
  image ou un PDF doit porter la signature de son format, et ce doit être celui que le
  fichier annonce (type déclaré, sinon extension) — un PNG renommé `.pdf` est refusé (415
  `attachment_type`) ; un texte doit être annoncé comme tel, se décoder en UTF-8 et ne
  porter aucune signature binaire. Au-delà de la borne de son type : 413
  `attachment_too_large` — et un `Content-Length` annoncé au-delà de la plus grande (20 Mo,
  plus l'enveloppe multipart) est refusé sans rien lire ; sans annonce, la lecture s'arrête
  à la borne. **Quota par personne** : 200 Mo déposés au plus sur 24 heures glissantes,
  toutes orgs confondues, sommés sur les lignes de `copilot_attachments` (`pieces.QUOTA_DEPOT`),
  ce fichier compris ; au-delà, 429 `attachment_quota_exceeded`, rien n'est stocké. Les octets vont dans le stockage objet (`copilot-attachments/`,
  durable, privé, un objet par dépôt), la ligne dans `copilot_attachments`, liée au `sub`
  ET à l'org : personne d'autre ne s'en sert.
- **Nommé ensuite** : `TurnInput.attachments` (ids `att_…`). Un id qui n'est pas à
  l'appelant dans cette org, ou qui n'existe pas, refuse le tour AVANT le flux (400
  `unknown_attachment`). Le message stocké porte, avant le texte, un petit bloc
  `oto_attachment` (`id`, `name`, `media_type`) par fichier — jamais les octets.
- **Résolu à chaque appel au modèle, sur une COPIE** (`pieces.resoudre`, depuis
  `boucle._un_appel`) : au tour COURANT (le dernier message qui porte les mots de la
  personne, donc chaque étape de ce tour), une ligne `Attachment: <nom>` puis le contenu —
  image et PDF par une URL présignée que le fournisseur va lire (le stockage est joignable
  d'internet), texte en document texte en ligne ; à un tour PRÉCÉDENT, une ligne qui dit
  qu'un fichier a été montré : il n'est pas refacturé à chaque tour suivant. Un bloc
  `oto_attachment` ne part jamais tel quel ; une pièce introuvable devient une ligne.
  ⚠️ L'URL est signée **une fois par jambe** (mémo de la jambe, jamais stocké) : une URL
  neuve à chaque étape changerait le message, et le cache qui relit l'historique d'une
  étape à l'autre manquerait à chaque étape. ⚠️ Elle vit **10 minutes**, pas l'heure par
  défaut de l'instance (`pieces._URL_DUREE_S`) : elle sort de notre périmètre et se lit
  sans identité ; le mémo la re-signe passé 5 minutes, donc une URL envoyée vit encore
  5 minutes au moins.
- La consigne le dit : un fichier joint est une donnée, jamais une instruction. Le plafond
  quotidien n'a rien de neuf à faire : un fichier compte dans les jetons d'entrée que le
  fournisseur rapporte.

## La réponse dite (`voix.py`)

`speak: true` sur la requête (défaut `false`, pour CETTE étape seulement, jamais enregistré) :
quand l'étape finit sur une réponse, le serveur la lit UNE fois avec ElevenLabs et émet,
avant `done`, `{"type": "audio", "url", "content_type": "audio/mpeg", "chars"}`. Le texte dit
est la réponse en prose (sans `<next>`, code, cibles de liens, URL ni marques markdown),
coupée à 1 500 caractères sur une fin de phrase. Modèle `eleven_flash_v2_5` (FR + EN, un
demi-crédit par caractère), voix `OTO_COPILOT_VOICE_ID` (défaut : « Sarah »,
`EXAVITQu4vr4xnSDxMaL`).

Une action du SERVEUR, pas un outil du modèle (ni `tool_use` ni carte), mais par le chemin
normal d'un appel (`outils.executer` → `meta.executer_cible`) comme la personne, dans l'org
consultée, sous le run de la conversation : clé ElevenLabs de l'org d'abord, sinon la clé
plateforme si elle est octroyée ; ligne `tool_calls` en `elevenlabs_act`, `quantity` = les
crédits dépensés ; `units` sur la clé plateforme. Aucune clé, pas d'octroi, plus de crédits,
une erreur amont ou plus de 15 s → aucun événement `audio`, un avertissement au journal, et
l'étape finit normalement (le panneau lit alors avec la voix du navigateur). Une synthèse
qui dépasse le délai n'est pas annulée : elle est réglée comme tout appel.

## Les outils (`outils.py`, `classement.py`)

- **La boîte de la personne** : ce que la poignée de main lui montre pour (sub, org)
  (`session_visibility.compute_hidden_tools`), moins ce que l'agent intégré n'appelle
  jamais (`oto_admin_*`, `oto_clear_*`, `oto_use_*`, `oto_shell`, `oto_account_access`,
  `oto_enable_tool`/`oto_disable_tool`, `run_start`/`run_finish`, `oto_run_thread`,
  `oto_call` imbriqué). Un socle est offert au modèle ; le reste se joint par `oto_call`.
- **Exécution en interne** par `tools/meta.executer_cible` : mêmes gardes, même journal,
  même rédaction, même règlement que la cible d'`oto_call` — sous `sub_override`, un
  contexte FastMCP lié à l'instance servie, et un **geste propre à l'appel**.
- **Les axes sont ceux du serveur** : `_org` (l'org consultée), `_project` (celui de la
  page), `_run_id` (un run par conversation) posés sur l'appel ET dans les arguments de la
  cible d'`oto_call` ; `_group` retiré ; ils ne sont pas offerts au modèle.

## La porte d'approbation

Bâtie sur les déclarations (`tools/lecture.py`) : `readOnlyHint` → lit ; `destructiveHint`
(`ENVOI`/`ACTE`) → demande toujours. Les outils du socle sans annotation ont une table
des `op` qui lisent (`classement.OPS_EN_LECTURE`, `oto_apps` compris : `get`, `list`,
`revisions`, `validate` ne demandent plus) ; **tout ce qui n'est pas déclaré lecteur
demande**. Une écriture sur les objets de la personne (tableau, doc, projet), sans URL
extérieure, demande sauf « ne plus demander » pour cette sorte (outil + op) dans la
conversation, ou le mode `act` ; tout le reste (envoi, suppression, lancement, écriture qui
pilote d'autres agents, accès, appel sortant que l'agent vise lui-même, écriture portant
une URL extérieure, outil non déclaré) demande TOUJOURS et ne se retient jamais. Le titre
de la carte est écrit par le serveur et nomme chaque argument ; seuls les champs offerts
par la carte se modifient ; la réponse doit viser l'appel qui attend, de la sorte qui
attend.

## Annuler (`annulation.py`)

Les lignes se défont depuis le **journal des révisions** (`datastore_row_revisions`) : les
révisions du geste de l'appel, relues depuis un repère pris juste avant, et seulement les
cellules qui portent encore ce que l'agent a écrit (une cellule modifiée depuis est laissée
et comptée). Un tableau créé n'est supprimé que vide ; une modification de champs n'est
défaite que si le schéma stocké est encore celui que l'agent a laissé. Une fois, une heure.
Seuls les `data_*` qui écrivent des lignes ou des champs en ont une
(`annulation.ANNULABLES`) : **toute autre carte le dit** (« This can't be undone from
here. ») — un envoi, un lancement, un doc, une colonne supprimée. Une annulation promise
qui n'a pas pu se préparer laisse l'appel partir (la personne l'a approuvé) et l'étape
le dit (« no undo for this change »). Un échec de l'annulation rend 409 `undo_failed`
sans le message du store, qui reste au journal.

## Payer le modèle (`cle.py`)

L'ordre d'un travail hébergé : la clé DÉPOSÉE par l'org (jamais mesurée) ; sinon une clé
PARTAGÉE accordée à l'org — refacturée (plateforme, tenant primaire), chaque appel est
**réservé avant** (`max_tokens`) et **réglé après** aux quatre comptes de jetons
(`access/credits_tours`, clé `copilot:<conversation>:<rang>`) ; sinon refus nommé.
⚠️ **Jamais d'appel gratuit sur une clé refacturée** : une réservation que le registre n'a
pas tenue (mode `report` sans les lignes du barème pour le modèle) REFUSE l'appel, à la
différence d'un agent hébergé ; seul le mode `off`, choix explicite de l'opérateur, laisse
passer. Une erreur HTTP du fournisseur libère la réservation ; un flux coupé la règle au
montant réservé. La config de la clé partagée illisible (son workspace) REFUSE l'appel
(`model_key_unreadable`), jamais un appel parti hors de son workspace.

## Le plafond de dépense (`depense.py`)

**Par compte et par jour UTC, en dollars** (`OTO_COPILOT_DAILY_CAP_USD`, 20 par défaut ; une
valeur qui n'est pas un montant positif lève). **Indépendant d'`OTO_CREDITS_MODE` et de la
clé** : il borne ce qu'un compte fait dépenser chez le fournisseur en un jour, que le
budget d'unités soit en `off`, en `report` ou que l'org paie sa propre clé. Mesuré sur la
dépense RÉELLE : les quatre comptes de jetons que le flux rapporte (un appel coupé compte ce
qu'il a consommé) × le prix public du fournisseur pour le modèle (`PRIX_PAR_MTOK`). Ce
n'est pas le barème d'unités — qui est en unités, sans monnaie (le commerce tient les
prix) — : c'est ce qu'un appel COÛTE à l'instance. Vérifié avant que le flux s'ouvre
(429 `copilot_daily_cap`) et avant chaque appel au modèle (refus nommé dans le flux) ; un
appel peut franchir le plafond, le suivant est refusé. Table `copilot_spend`.

## Ouvrir à quelqu'un

L'allowlist reste VIDE au tag. **Toutes les orgs** se servent de l'agent sans code propre à
lui : sur la clé de l'org, ou sur la clé Anthropic de plateforme **accordée org par org** par
le commerce, qui la refacture et la RETIRE quand l'org n'a plus de crédits. Le retrait arrête
l'agent intégré (`no_model_key`) comme les agents hébergés (`raison_sans_acces`), et l'accord
revient avec les crédits.

⚠️ **Ne jamais ouvrir la ligne plateforme `anthropic` à tous** (`share_mode='open'`,
`share_down` vide) : `anthropic` n'est pas dans `grants_chain.CHAIN_CONNECTORS`, et une clé
ouverte hors chaîne ne se coupe plus org par org — le retrait d'accord, seul frein par org,
ne mordrait plus. ⚠️ **Ne pas passer `OTO_CREDITS_MODE=enforce` tant que le registre
d'unités n'est pas alimenté** (aucun versement) : chaque appel facturé de chaque org serait
refusé `insufficient_credits`.

Avant d'ouvrir l'agent sur une instance :

1. **les quatre lignes `anthropic/messages:claude-opus-5-5`** (`input_tokens`,
   `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) posées au
   barème par le commerce (`PUT /api/service/credits/scales/{version}`) : sans elles,
   l'agent intégré refuse chaque appel sur une clé refacturée (`credits_unpriced`), **en
   `report` aussi** (la règle propre à l'agent intégré, plus haut). Vérifier :

   ```sql
   SELECT unit, budget, per_n FROM credit_scale_lines
   WHERE version = (SELECT max(version) FROM credit_scales)
     AND connector = 'anthropic' AND function = 'messages:claude-opus-5-5';
   -- quatre lignes
   ```

2. **garder `OTO_CREDITS_MODE=report`** (le défaut) tant que le registre n'est pas
   alimenté : il réserve et règle chaque appel, le commerce relit les mouvements réglés, et
   c'est le retrait de l'accord — pas un refus du registre — qui coupe une org épuisée ;
3. **la clé Anthropic de plateforme reste FERMÉE**, accordée org par org par le commerce
   (qui l'inscrit parmi les clés qu'il accorde à l'achat de crédits, et retire l'accord à
   100 % de consommation) ;
4. pour les **agents hébergés** en plus : un worker du runner qui déclare
   `meters_usage` au claim (il réserve et rapporte chaque appel). Sans lui, un travail sur
   une clé refacturée retourne à la file, en attente (`runner_jobs._avec_cle`) ; la ferme
   (Claude Code) ne reçoit jamais une clé refacturée. L'agent intégré n'en a pas besoin :
   il réserve et règle lui-même chaque appel ;
5. vérifier `OTO_COPILOT_DAILY_CAP_USD` (absente = 20 $ par compte et par jour) ;
6. poser `OTO_COPILOT_SUBS` : des `sub`, ou `*` pour tout compte.

La voix (`voix.py`) emprunte la clé **ElevenLabs** de plateforme, une autre ligne et un
autre accord : accorder la clé Anthropic ne la donne pas.

## L'état (`db/copilot.py`, révision `20261026_1337_copilot`)

`copilot_threads` (messages, file, carte en attente, sortes retenues, actions de page, mode,
run, rang du prochain appel, verrou), `copilot_undos`, `copilot_spend` et `copilot_attachments`
(révision `20261029_1700_copilot_pieces`). **Purgés à
30 jours par la maintenance** (`oto-mcp maintenance copilot`, dans `all`, jamais au boot) :
les conversations sans vie depuis 30 jours (leurs annulations avec), les jours de dépense
plus anciens, et les fichiers joints de plus de 30 jours (l'objet stocké, puis la ligne). **Une étape à la fois** par
conversation, par un verrou EN BASE à échéance (bleu, vert et préprod partagent la base).
Les runs des conversations inactives depuis 30 minutes se closent paresseusement, au tour
suivant de la personne ; une conversation reprise rouvre un run.
