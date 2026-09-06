# Relance des comptes jamais actifs (2026-09-02)

`oto_admin_outreach` (MCP) + `POST /api/admin/outreach` (REST). Le code : capacité
`capabilities/outreach.py`, requête `db/outreach.py`, DDL `db/schema/outreach.py`,
jeton et page de désinscription `outreach_optout.py` + route publique `/o/u/{token}`.

La plateforme savait **compter** ses comptes inactifs (`oto_admin_monitoring op=funnel`
→ `never_active`, `db.usage.activation_funnel`) sans jamais pouvoir les **nommer** ni
leur écrire. Ce lot ferme l'écart. L'envoi réutilise le chemin existant
(`email.send_composed_email` → `mailer.oto.zone`, Scaleway TEM) : **il n'y a pas de
second chemin d'envoi**.

## Le piège de comptage, mesuré avant d'écrire une ligne

Sur 78 organisations directes vivantes, **64 sont des espaces personnels créés d'office
à l'inscription** — pas des espaces que quelqu'un a voulus. Compter l'inactivité **par
organisation** revient donc à écrire à quelqu'un au sujet d'un espace qu'il n'a jamais
demandé, et le message n'a alors aucun sens pour lui.

⟹ **rien ne se compte par org.**

## Le second piège : un humain s'inscrit deux fois (2026-09-04)

Le grain n'est pas non plus `users.sub`. L'audience du 2026-09-04 a affiché **deux fois
la même personne** — deux inscriptions avec la même adresse, deux `sub`, deux lignes,
donc deux mails dans une seule boîte.

Recensé le 2026-09-04 : **91 comptes, 77 adresses distinctes, 10 adresses portées par
2 comptes** (aucune au-delà de 2). Deux motifs, qui ne se traitent pas pareil :

| motif | combien | ce que ça donne dans l'audience |
|---|---|---|
| un sub qualifié `<tenant>:` **et** un sub nu, même humain | **9** | le filtre partenaire écarte déjà la moitié qualifiée : pas de doublon |
| **deux subs nus** — une vraie double inscription chez nous | **1** | deux lignes, deux mails |

⚠️ **L'index unique `(campagne, sub)` ne pouvait rien y voir** : les deux comptes sont
distincts, la contrainte n'était pas violée une seule fois. Un garde-fou d'écriture ne
protège que le grain qu'il connaît, et son grain était le compte.

⟹ **tout se compte par BOÎTE MAIL** (`lower(btrim(email))`). `_AUDIENCE_SQL` regroupe et
sert **une ligne par boîte** :

- le compte servi (`sub`) est **le plus récent** de la boîte — celui par lequel la
  personne est entrée en dernier ; c'est lui qui porte la trace d'envoi et le lien de
  refus. La réponse porte `accounts` (combien de comptes ont fusionné) : sans ce
  compteur, une audience qui rétrécit se lit comme un filtre qui a trop mordu ;
- l'activité s'agrège — `calls` = la somme, `last_seen_at` = le maximum. Sans ça,
  quelqu'un d'actif sur son second compte serait dit « jamais actif » ;
- un nom ou une langue **déclarés sur l'un des comptes** valent pour la personne : le
  second compte, souvent vide, n'efface pas ce que le premier savait d'elle ;
- ⚠️ **les trois soustractions se lisent sur TOUS les comptes de la boîte** (refus,
  déjà-relancé, appartenance). C'est le point qui compte : **se désinscrire une fois
  doit suffire**, même à qui s'est inscrit deux fois — sinon le lien de désinscription
  mentirait, la personne aurait refusé et le mail suivant serait quand même parti.

⚠️ **Le regroupement est EN AVAL du filtre partenaire**, délibérément : `notres` a déjà
écarté les comptes d'un tenant tiers, donc un compte de partenaire ne peut ni entrer
dans une boîte, ni en faire sortir un des nôtres. Regrouper d'abord rouvrirait la porte
que tout ce fichier ferme.

`tests/test_outreach_audience_db.py` porte quatre boîtes-témoins à deux comptes (une par
situation) et **la mutation qui dégrade le regroupement au grain du compte** : sans elle,
les tests de fusion pourraient être verts faute de doublon dans le jeu de données.

## Deux populations, à ne pas confondre

| `status` | définition | mesuré le 2026-09-02 |
|---|---|---|
| `never_active` | aucune ligne `tool_calls` de `kind='mcp'` | **40** comptes |
| `dormant` | a appelé, puis plus rien depuis `dormant_days` | 10 à 7 j, 6 à 14 j, **0** à 30 j |

Le message n'est pas le même, donc les deux ne se mélangent pas dans une audience.

## ⚠️ « Jamais actif » cache DEUX intentions différentes — à lire avant de rédiger

Une trace `kind='rest'` ou `'protocol'` (dashboard ouvert, handshake MCP) **ne compte
pas** comme un usage — c'est juste. Mais elle n'est pas rien non plus, et sur l'audience
exacte du 2026-09-02 (39 comptes) elle sépare deux populations qu'un même texte ne peut
pas servir :

| | combien | ce qu'ils ont fait | ce que le texte doit faire |
|---|---|---|---|
| **venus puis repartis** | **16** | ouvert le tableau de bord, ou branché leur client MCP — puis plus rien | ils ont eu une INTENTION et se sont arrêtés quelque part. Le texte doit lever un obstacle (« voilà la première chose à faire »), pas présenter le produit |
| **aucune trace du tout** | **23** | inscrits, jamais revenus | ils n'ont jamais vu le produit. Le texte doit donner une raison de venir |

Écrire un seul message pour les deux, c'est expliquer à quelqu'un qui a déjà ouvert
l'outil ce qu'est l'outil, et donner à quelqu'un qui n'y est jamais entré des
instructions pour un écran qu'il n'a pas vu.

⚠️ **La distinction est dans les données, PAS dans le sélecteur.** `status` ne connaît
que `never_active` et `dormant` ; il n'y a aujourd'hui aucune valeur pour « venu puis
reparti ». Deux façons de faire en attendant : passer `only` avec la liste des subs
concernés (deux campagnes distinctes, donc deux `campaign` — ce qui est de toute façon
la bonne granularité pour deux textes différents), ou ajouter un troisième `status`.
Le partage se relit par :

```sql
-- sur l'audience servie : qui a laissé une trace non-outil, qui n'a rien laissé
CASE WHEN EXISTS (SELECT 1 FROM tool_calls c WHERE c.sub = n.sub)
     THEN 'venu puis reparti' ELSE 'aucune trace du tout' END
```

⚠️ Et **ne pas recopier les 16/23 d'un relevé fait sur une autre population** : le même
partage valait 17/23 avant le filtre « adresse email connue ». Un chiffre juste sur
40 comptes est faux sur 39.

## L'exclusion du tenant partenaire est dans la REQUÊTE

Les comptes hébergés chez un tenant tiers sont **les clients de ce tenant**. Leur
écrire, c'est parler par-dessus lui, dans son produit. L'exclusion ne peut donc pas
être une consigne : elle vit dans `_AUDIENCE_SQL`, **en amont de tout critère
d'activité**, et `tests/test_outreach_audience_db.py` la mute pour prouver qu'elle mord.

⚠️ **Le discriminant n'est PAS `orgs.tenant_id`** — mesuré INERTE : les 160 orgs
portent le tenant primaire, partenaires compris (le provisioning ne l'écrit pas). Deux
axes portent, en UNION :

1. la **qualification du sub** (`tenancy.qualify`, préfixe `<slug>:`) — 22 comptes ;
2. l'appartenance à au moins une org dont le tenant EFFECTIF est le nôtre
   (`tenants._ORG_TENANT_EXPR`, **source unique** partagée avec `org_tenant_slug`).

Le (2) couvre l'angle mort du (1) : un compte inscrit chez nous, invité uniquement dans
des orgs de partenaire. **Mesuré à 0 aujourd'hui**, ce qui ne dit rien de demain.

⚠️ **(1) est aujourd'hui REDONDANT avec (2)** — un sub qualifié est toujours membre
d'une org que sa seule présence fait lire comme celle du partenaire. Gardé en
profondeur. **Corollaire à connaître : un seul membre qualifié suffit à sortir TOUTE
une org de l'audience**, ses membres à nous compris. Sur-exclusion assumée (rater une
relance ne coûte rien, écrire aux clients d'un tiers coûte le partenariat), mais elle
peut vider une audience sans rien dire — si le compte servi paraît trop petit, c'est là
qu'il faut regarder.

## La langue : ce qui existe vraiment, et ce qui n'existe pas

**Réponse à la question laissée ouverte par `email.md` §Locale** (« détection de langue
pour un contact jamais loggé — question ouverte »). Relevé sur les 64 comptes de notre
tenant, et sur les 40 de l'audience :

| signal | couverture (64 comptes) | sur l'audience (40) | verdict |
|---|---|---|---|
| `users.locale` (préférence d'UI du dashboard) | 11 (9 fr, 2 en) | **2** | le seul déclaré, quasi vide |
| `billing_identities.country_code` | **0 ligne en base** | 0 | inexistant |
| TLD de l'adresse | `.fr` sur 7 | 3 sur 39 | non concluant |
| domaine grand public (gmail, outlook…) | 15 | — | ne dit rien de la langue |

**Il n'existe aucun signal fiable de langue.** `users.locale` est une préférence
d'INTERFACE, pas une nationalité, et elle est posée sur 5 % de l'audience. Le TLD ne
tranche rien (un `.com` peut être français, un `.fr` une filiale) et **n'entre dans
aucune décision** — il est servi comme `email_domain`, pour l'œil de l'opérateur.

Conséquence assumée dans le contrat servi : la capacité rend `locale` (déclarée, souvent
`null`), `served_locale` et `locale_source` (`declared` | `default`), et l'opérateur
CHOISIT `default_locale` pour tous ceux qui n'ont pas déclaré. Les compteurs
`with_declared_locale` / `with_default_locale` disent combien tombent de chaque côté.

**Ce qui reste à faire pour mieux savoir** est un autre lot : demander la langue à
l'inscription (ou capter `Accept-Language` au premier login) et l'écrire dans
`users.locale`. Sans ça, aucune amélioration de l'algorithme ne changera quoi que ce soit.

### Le partage EXACT de l'audience servie (prod, 2026-09-02)

| `users.locale` | comptes | dont venus puis repartis | dont aucune trace |
|---|---|---|---|
| aucune | **37** | 14 | 23 |
| `fr` | **2** | 2 | 0 |
| `en` | **0** | — | — |
| total | **39** | 16 | 23 |

⚠️ **Conséquence qu'on ne voit pas en lisant `default_locale`** : les deux seules
préférences déclarées valent `fr`, et la préférence DÉCLARÉE prime toujours (`_langue`).
Servir l'anglais par défaut ne rend donc pas la campagne monolingue — les langues servies
sont **{en, fr}**, pour 37 et 2 personnes. Il faut donc :

- écrire les DEUX versions (`subject_fr`/`body_fr` sont exigés par `_contenu`, sinon
  `content_required`, pour exactement 2 destinataires) ;
- `op=test` envoie alors **deux** mails à l'opérateur, un par langue ;
- `op=send` exige un essai valide **dans chacune des deux**.

Il n'existe aucun moyen de forcer l'anglais à ces deux comptes : la préférence déclarée
gagne, par construction, et c'est le comportement voulu.

## Les garde-fous, et pourquoi chacun est mécanique

| garde-fou | mécanisme | ce qu'il empêche |
|---|---|---|
| une seule relance par personne | audience regroupée par **boîte mail** (lecture) + index unique partiel `(campaign, sub) WHERE kind='send'`, **écrit AVANT l'envoi** | le doublon dans une boîte mail |
| rien ne part sans essai reçu | `op=send` exige un `op=test` portant la MÊME empreinte de contenu, **pour chaque langue servie** | envoyer un message qu'on n'a pas vu arriver chez soi |
| le nombre est annoncé | `op=send` sans `confirm` refuse en disant N ; `confirm` faux refuse | découvrir N après coup |
| plafond dur | `MAX_ENVOI = 200`, jugé sur `taille_audience()` — l'audience ENTIÈRE | l'envoi de masse non relu |
| le refus se respecte | lien signé `/o/u/<jeton>` → `outreach_optouts`, lu par l'audience | relancer qui s'est désinscrit |

⚠️ **Le plafond se juge sur le total, pas sur la liste servie** : la lecture tronque
déjà à `MAX_ENVOI`, donc un plafond comparé à la page serait vert pour toujours. D'où
`taille_audience()`, et les champs `total` / `selected` / `truncated`.

⚠️ **Toute retouche du texte invalide l'essai** (l'empreinte est un sha256 du sujet + du
corps + du CTA, toutes langues). C'est voulu : « je l'ai vu arriver » ne vaut que pour
le message qu'on a vu.

⚠️ **La trace précède l'envoi, et se retire si rien n'est parti** (`annule_envoi`) :
sans ce retrait, un hoquet du mailer sortirait la personne de toute audience future
alors qu'elle n'a jamais rien reçu.

## Le pied de page marketing a changé

`email.render_composed_email` accepte `locale` et `unsubscribe_url` (tous deux
additifs — sans eux, le rendu est **inchangé à l'octet près**). Avec un lien, la phrase
cesse de proposer « répondez pour ne plus en recevoir » : offrir deux chemins dont un
seul laisse une trace ferait croire à un refus enregistré qui ne l'est pas.

Le pied **transactionnel** (`email_brand.mention_transactionnelle`) continue de ne
proposer aucun désabonnement, délibérément : on ne se désabonne pas d'une invitation.

## Le lien de désinscription

Jeton HMAC-SHA256 sur le seul `sub`, **sans expiration** — le mail relu six mois plus
tard est précisément celui dont on ne veut plus, et « ce lien a expiré » transforme un
refus en corvée. Le risque est borné par ce qu'il autorise : cesser de recevoir nos
relances. Le contrôle de `typ` empêche qu'un jeton d'upload (même secret d'instance)
vaille désinscription.

La route `/o/u/{token}` est **anonyme et server-rendered**, sur le BACKEND
(`OTO_MCP_PUBLIC_URL`) : exiger une session la demanderait à celui-là même qui ne veut
plus rien avoir à faire avec nous, et un front indisponible ne doit pas bloquer un
refus. C'est un **GET qui écrit**, assumé : les clients mail ne postent pas, l'écriture
est idempotente et strictement soustractive.

Sans `OTO_MCP_OAUTH_STATE_SECRET`, `lien()` **lève** — plutôt qu'un lien mort dans le
pied de page de dizaines de mails.

## ⚠️ REST SEULE — ce qu'on a perdu en retirant le verbe conversationnel (2026-09-02)

`oto_admin_outreach` **n'existe plus côté MCP** (`mcp=None`). Motif, mesuré : le verbe
pesait **3 138 caractères** dans la surface servie à CHAQUE compte plateforme (18 de
nom + 1 616 de description + 1 504 de schéma), soit **14,2 % du poids cumulé des 17
outils `oto_admin_*`**. Piloter une campagne n'est pas une raison assez forte : cette
surface se paie à chaque handshake, par tout le monde, et elle n'a pas de bouton
« replier ». Après retrait : 16 outils, 18 916 caractères.

**Le coût, en clair, pour qu'il ne se découvre pas le jour où il fait mal :**

> **Il n'y a plus aucun diagnostic depuis une conversation.** Ni lire l'audience, ni
> lancer l'essai, ni voir pourquoi un envoi est refusé. **Tout passe par l'écran
> d'administration — y compris le jour où l'écran ne marche pas.**

C'est un vrai renoncement, et il est asymétrique : l'écran sert le cas nominal, la
conversation servait le cas dégradé. On a gardé le premier.

### Quand l'écran ne répond pas

Le repli est un appel REST à la main, pas une session d'agent. Un jeton d'API suffit
(`allow_api_token` est vrai sur ce binding) :

```bash
curl -sS -X POST https://mcp.oto.ninja/api/admin/outreach \
  -H "Authorization: Bearer $OTO_TOKEN" -H "Content-Type: application/json" \
  -d '{"op":"audience","campaign":"onboarding-2026-09"}'
```

Toutes les `op` passent par ce chemin, refus compris — c'est la même capacité, la même
autorisation, le même handler. `op=preview` rend le HTML des deux langues sans rien
envoyer ; `op=test` écrit à l'appelant. **Aucune n'exige l'écran.**

### Le rétablir

Une ligne (`mcp="oto_admin_outreach"` dans `capabilities/outreach.py`). ⚠️ Ne pas le
faire sans **remesurer le poids** : c'est le chiffre qui a tranché, pas le principe, et
la surface aura bougé. `tests/test_outreach_rest_face.py` fige les deux moitiés — le
verbe absent, et la route qui aboutit quand même en 200.

## Autorisation

`oto_admin_outreach`, plancher `operator`. Lectures (`audience`, `preview`, `journal`,
`optouts`) = `PLATFORM_ADMIN` ; tout ce qui fait PARTIR un mail sous notre marque
(`test`, `send`) ou lève le refus de quelqu'un (`optout_clear`) = `SUPER_ADMIN`.
`tests/test_outreach_guards.py` **exerce** la règle pour chaque valeur de l'énuméré
`op` — une op ajoutée sans gate y arrive toute seule.

## Empreinte sur le registre servi — mesurée, pas déduite (2026-09-02)

La question posée au lot : **est-ce qu'il pollue la toolbox de tout le monde ?** Réponse
mesurée en exécutant le filtre de visibilité (`session_visibility.compute_hidden_tools`)
sur la liste RÉELLE des outils montés, pour les trois rôles plateforme :

| | ce que le lot ajoute |
|---|---|
| verbes MCP | **1** — `oto_admin_outreach` |
| routes REST | 1 — `POST /api/admin/outreach` |
| poids servi du verbe | description 1 616 c. + schéma d'entrée 1 432 c. |
| texte injecté au handshake | **+0 caractère** (le catalogue du bloc A ne liste que des namespaces de CONNECTEURS ; `oto_admin_*` n'y figure pas) |

| rôle | `oto_admin_outreach` | comptes concernés en prod |
|---|---|---|
| `member` | **masqué** | 82 |
| `admin` (opérateur plateforme) | visible | 1 |
| `super_admin` | visible | 4 |

⟹ **82 des 87 comptes ne le voient pas.** Le verbe ne coûte rien à la surface d'un
utilisateur ordinaire.

⚠️ **Pourquoi il ne tombe PAS dans le piège du namespace transverse.** `oto_admin_outreach`
est dans le namespace `oto`, dont `connector_for_namespace` rend `None` : aucun bloc de
gating par connecteur (activation, RBAC, sélection) ne le touche. Ce qui le masque, c'est
`_hors_de_portee_plateforme`, qui dérive le plancher de l'autz **DÉCLARÉE** de la capacité
— ici `operator`, le plus bas des branches d'`ADMIN_BY_OP`.

C'est exactement là que se joue la différence avec les verbes qui ont fuité : **31
capacités du namespace `oto` ont un plancher `None`** (donc sont servies à tout le monde),
dont `oto_salesforce_connect` et `oto_zoho_connect`. Une capacité neuve du namespace `oto`
n'est pas gatée « par défaut » : elle l'est **si et seulement si son autz déclare un
plancher plateforme**. Le cliquet qui le rappelle est la table `PLANCHERS` de
`tests/test_admin_tool_visibility_by_authz.py`, et ce test **exécute** désormais le filtre
sur `oto_admin_outreach` (il ne se contente pas de comparer la déclaration).

## Décisions d'exploitation de la première campagne (2026-09-02)

Prises par Alexis, consignées ici parce qu'elles ne vivent nulle part dans le code : une
campagne n'est pas un objet, c'est un jeu de paramètres passés à l'appel.

1. **`default_locale='en'`** — les 37 comptes sans préférence déclarée reçoivent l'anglais ;
   les 2 qui ont déclaré `fr` gardent le français (cf. le partage exact ci-dessus). Le
   défaut du champ reste `fr` côté schéma : le rendre `en` changerait la langue de toute
   campagne future sans que personne ne le redise. **La décision se porte à l'appel.**
2. **Un seul message pour les 39** — pas de campagne séparée pour les « venus puis
   repartis ». La mesure 16/23 reste écrite plus haut parce qu'elle est vraie et qu'elle
   doit guider la RÉDACTION, mais elle n'est pas un axe de ciblage : ni troisième `status`,
   ni double campagne.
3. **L'usage passe par un écran d'administration** (front), pas par la conversation. Voir
   « Ce que ce lot ne fait pas ».

## Ce que les garde-fous ne garantissent PAS

Le tableau ci-dessus dit ce que chaque mécanisme empêche. Ce qu'il n'empêche pas, mesuré
sur banc le 2026-09-02, pour que personne ne le découvre au mauvais moment :

- **L'essai prouve qu'un mail a été REMIS AU MAILER** pour l'adresse du compte appelant —
  pas qu'un humain l'a ouvert. Rien n'attend d'accusé de lecture : `op=test` puis `op=send`
  s'enchaînent en deux appels.
- **`confirm` annonce le nombre DE CET APPEL, et `limit` choisit ce nombre.** Une boucle
  `limit=1, confirm=1` atteint toute une audience homogène en n'annonçant jamais plus de 1
  (mesuré : 40 comptes, 40 envois). Le plafond, lui, tient — il se juge sur l'audience
  entière, que ni `limit`, ni `only`, ni un slug neuf ne réduisent.
- ⚠️ **Au-dessus de `MAX_ENVOI`, il n'existe aucun découpage** : le refus
  `audience_too_large` conseille `only` ou « découpe en plusieurs campagnes », or ni l'un
  ni l'autre ne change le nombre sur lequel le plafond se prononce. Le conseil est faux —
  à corriger avant qu'une audience dépasse 200.
- **« Une seule relance par personne » vaut PAR CAMPAGNE** : un slug neuf réécrit aux mêmes
  gens. C'est le dessin voulu ; le compteur servi `previous_outreach` est ce qui permet de
  le voir.
- ⚠️ **L'index unique ne garde que le COMPTE.** Ce qui empêche aujourd'hui deux mails
  dans une même boîte, c'est le regroupement à la LECTURE — pas une contrainte de base.
  Un futur chemin d'envoi qui n'emprunterait pas `_AUDIENCE_SQL` ne serait donc gardé
  par rien. Un index unique `(campaign, lower(to_email)) WHERE kind='send'` fermerait ça
  mécaniquement ; il n'a pas été posé, faute d'avoir été demandé.
- **Le refus (`outreach_optouts`) n'est lu que par CETTE audience.** `email_send` — même
  mailer, même marque, `super_admin` pour le repli marque — écrit à n'importe quelle
  adresse sans essai, sans plafond et sans lien de désinscription.

## Ce que ce lot ne fait pas

- **Pas d'écran de dashboard** : la route REST existe, la vue est du ressort d'`oto-front`.
- **Pas d'en-tête `List-Unsubscribe`** : le service d'envoi (`mailer.oto.zone`) n'accepte
  que `from`/`to`/`subject`/`html`/`reply_to`. Le lien dans le pied est le seul canal.
- **Pas de cadencement** : l'envoi est synchrone, borné par `MAX_ENVOI`. Il ne passe ni
  par `scheduled_emails` ni par les quiet hours (qui sont propres à `email_send` d'une
  org, cf. `email.md`).
- **Aucun envoi réel n'a été effectué** en construisant ce lot.
