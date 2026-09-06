---
title: "La forme d'une procédure — digest d'ouverture, tableau, schéma"
description: toute procédure s'ouvre sur son digest d'auto-amélioration et embarque un schéma, qui est la vue par défaut de sa page ; le dessin est parsé, donc sa grammaire est un contrat — à lire AVANT d'écrire ou de réécrire une procédure
---

# Le schéma d'une procédure

**Toute procédure embarque un schéma.** Pas en option, pas « quand c'est un beau
process » : le front rend ce dessin comme la **vue par défaut** de la page du process.
Une procédure sans schéma s'y affiche en état vide — le lecteur voit qu'il manque
quelque chose avant de lire une ligne de prose.

Le dessin n'est pas typographié tel quel : il est **reparsé en graphe** puis redessiné
en cartes (logos des connecteurs, sorties, voies de sortie). D'où la seule chose à
retenir avant de dessiner :

> **La grammaire ci-dessous est un contrat, pas un guide de style.** Le parseur refuse
> tout ce qu'elle ne couvre pas — et il *préfère* refuser : un schéma faussement
> confiant est pire qu'un dessin fidèle. Un dessin refusé retombe sur ses caractères
> bruts. Il n'y a pas de demi-rendu.

## L'ouverture : le digest d'auto-amélioration

**Toute procédure s'ouvre sur son digest.** C'est le premier bloc, avant la prose, avant
les sections, avant tout :

```
> **Self-improvement digest** — <ce que le dernier déroulé a appris et ce qui a été
> corrigé, daté>.
```

Ce qu'il dit : **ce que le DERNIER RUN a appris, et ce qui a été corrigé en conséquence,
daté**. C'est un relevé, donc la date est obligatoire — c'est le seul bloc d'une
procédure où un fait daté est à sa place (partout ailleurs, cf. §Deux règles, un fait
daté dérive).

Une procédure qui n'a jamais tourné **le dit, en une phrase** :

```
> **Self-improvement digest** — Never run end to end; nothing to report until it is.
```

⚠️ **Ne fabrique jamais un déroulé.** Ce bloc ne vaut que tant qu'il est vrai : un
digest décoratif est pire que pas de digest, parce qu'il se lit comme une preuve. Les
sources admissibles, dans cet ordre : le journal des runs
(`oto_org_monitoring(op="runs", org_id=…)`), le relevé daté que le corps porte déjà
(un « Corrected on <date> by a rehearsal run that found thirteen defects », un exemple
travaillé avec de vraies valeurs — c'est une preuve qu'un run a eu lieu), puis
l'historique des versions (`oto_procedure(op="get", …, with_history=true)`), qui dit
QUAND ça a été corrigé pendant que le corps dit POURQUOI. Une retouche de la page n'est
pas un déroulé du process.

⚠️ **Où exactement**, et ça vient du RENDU, pas du goût : la page d'un process **retire
un titre de tête qui répète le nom de la procédure** et affiche le sien à la place. Donc
si le corps ouvre sur `# <le titre>`, le digest se pose **juste en dessous** (au-dessus,
il laisserait ce titre orphelin au milieu de la page) ; si le corps n'a pas ce H1 — il
ouvre sur `## Goal` ou sur un paragraphe — le digest est **littéralement le premier
bloc**. Dans les deux cas, c'est la première chose que le lecteur voit.

À l'écriture, la réponse porte `digest_warning` quand le bloc manque ou n'est pas en
tête. Warning, pas refus.

## Où il va

Le schéma est une **section requise** de la procédure, à un emplacement fixe :

- juste **après le tableau « At a glance »** (le tableau de synthèse en tête de corps) ;
- **après le paragraphe d'intro** s'il n'y a pas de tableau ;
- toujours **avant le premier titre de phase**.

⚠️ **RIEN entre le tableau et le dessin.** Pas un paragraphe, pas une note, pas un
titre. Là encore c'est le rendu qui commande : quand le corps dessine, la page **retire
le titre « At a glance » et son tableau** — les deux disent la même chose et le dessin
le dit mieux (le tableau reste dans le corps, parce que l'agent qui EXÉCUTE le run le
lit ; c'est l'affichage seul qui le laisse tomber). Ce qui traînait dans l'intervalle se
retrouve donc collé au-dessus du dessin, sans plus rien à quoi se rattacher. Une note
qui explique le TABLEAU passe **au-dessus** de lui ; tout ce qui explique le DESSIN —
ce qui tourne en parallèle, ce qu'une phase exige — va **directement en dessous**.

Un **seul** bloc fencé **non tagué** (` ``` ` sans langage) dans tout le corps : c'est
celui-là que le front cherche. Un second dessin, ou un dessin dans un bloc tagué
(` ```text `), ne sera pas rendu.

## The grammar

*(Verbatim — le parseur est `src/lib/ascii-diagram.ts` côté front ; chaque
règle ci-dessous est appliquée.)*

- Monospace, spaces only (a tab anywhere rejects the drawing). One main spine column; every `▼`, `├`, `┬` on the spine sits at the same column.
- Entry: one or two bare text lines above the first box. Line 1 is the trigger's title ("Natural language input in Claude"). Line 2, if present, is the example request in straight double quotes: `"Wholesale distributors in the East Bay, 200 to 2,000 employees. Source 40 accounts."` That quoted line is what the UI shows as the example prompt.
- A step is a box `┌───┐ │ … │ └──┬──┘` (corners ┌ ┐ └ ┘, sides │, one `┬` on the bottom edge where the flow leaves). Line 1 inside is the title, optionally numbered `0 · Find companies on Apollo`; the following lines are the detail sentence. Text to the right of the box's closing `│` is a margin note (tool names in it become logos).
- A check / gate is the same box drawn with double rules `╔═╗ ║ ║ ╚═╤═╝`.
- A human step is an ordinary box whose title or margin note contains the word "human".
- Flow: `▼` under the stem, optionally followed by a condition: `▼  tier A or B only`.
- An exit (a row that stops here): `├───────────────▶  ▪ excluded          on the hard exclusion list` on the stem between two boxes: name after `▪`, then 2+ spaces, then the reason.
- A side output (a system the step writes to): on the box's right edge, `│──▶  Apollo sequence  matched on vertical, sub-vertical and lane`.
- Parallel steps: a fan-out bar `┌───────┴─────────┐` with the `┴` at the spine, a row with one `▼` per column, the boxes side by side, then a fan-in bar `└───────┬─────────┘` with the `┬` back at the spine. No labels on the fan-out arrow row; the condition goes on the `▼` above the bar. Exits inside a column hang off that column's own stem, above the fan-in bar (never off the merged stem below it).
- Nothing else: no text between two `▼` on one row, no loose prose inside the drawing, no second drawing after a blank line (a legend line after a blank line is fine and is ignored).

## Worked example

Ce dessin-là se rend aujourd'hui sur la page d'un process réel, à un chemin de la forme
`/org/<id>/processes/acme-outbound-engine`.
**Recopie son alignement**, puis écris tes propres étapes.

```
              Natural language input in Claude
              "Wholesale distributors in the East Bay, 200 to 2,000 employees. Source 40 accounts."
                         │
                         ▼
╔═════════════════════════════════════════════════╗
║  Check the tools before spending                ║   Mailbox revoked?  CRM key present?
║  Three free calls confirm the sending mailbox,  ║   Read the sequence register live.
║  the email sequences and the CRM key.           ║
╚════════════════════════┬════════════════════════╝
                         ▼
┌─────────────────────────────────────────────────┐
│  0 · Find companies on Apollo                   │   apollo_search_organizations
│  Search for Bay Area companies of 201 to 2000   │   one credit per page of 100
│  staff, then filter by industry code.           │
└────────────────────────┬────────────────────────┘
                         ├───────────────▶  ▪ excluded          on the hard exclusion list
                         ▼  tier A or B only
┌─────────────────────────────────────────────────┐
│  2 · Choose who to contact                      │   one domain per call
│  Take up to two Verification people and one     │
│  Margin person at each company.                 │
└────────────────────────┬────────────────────────┘
                         ▼  a contact exists for this company
                 ┌───────┴─────────────────────────────┐
                 ▼                                     ▼
┌──────────────────────────────────┐  ┌──────────────────────────────────┐
│  3 · Buy signals for each company│  │  4 · Find details for each person│
│  Read the software stack and the │  │  Look up an email and a phone    │
│  open jobs with TheirStack.      │  │  number with Apollo.             │
└────────────────┬─────────────────┘  └────────────────┬─────────────────┘
                 │                                     ├──────────▶   ▪ unreachable   no email, no phone, no LinkedIn
                 └───────┬─────────────────────────────┘
                         ▼  company scored and person reachable
┌─────────────────────────────────────────────────┐
│  6 · Send the email and the invite              │──▶  Apollo sequence  matched on vertical and lane
│  Check the guards, enrol the person in the      │──▶  LinkedIn invite  Unipile sender pool, paced per identity
│  matching sequence and send a LinkedIn invite.  │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  Calling                                        │   the one human step in the machine
│  A person works the list by phone and records   │
│  the outcome and their own notes.               │
└─────────────────────────────────────────────────┘

▪ terminal — the row stops there and nothing further is spent on it
```

## Combien un carton peut porter

Le front **reflow** le texte : ces bornes-là ne sont pas de la syntaxe, elles règlent la
DENSITÉ de la carte rendue. Les dépasser ne casse rien — ça donne un carton qu'on ne lit
plus, et une page de process qui se lit moins bien que la prose qu'elle résume.

| Élément | Borne |
|---|---|
| Titre d'une étape | ~40 caractères |
| Phrase de détail | **UNE** phrase, ~80 caractères |
| Phrase de détail, pour deux étapes CÔTE À CÔTE | ~60 caractères |
| Raison d'une sortie (après `▪ nom` + 2 espaces) | ~35 caractères — « on the hard exclusion list » |
| Note d'une sortie latérale (`──▶ nom  note`) | ~50 caractères |

Et une règle de PLACEMENT, pas de longueur : **les noms d'outils vont dans la note de
marge**, à droite de la boîte — jamais dans la phrase de détail. C'est à droite qu'ils
deviennent des logos ; dans le détail, ils ne sont que du texte plus long.

⚠️ Ces bornes sont plus serrées que ce que la largeur du dessin autorise : une boîte de
49 colonnes tient trois lignes de détail, soit ~140 caractères, sans qu'aucun parseur ne
proteste. La boîte n'est pas la borne — **la carte rendue l'est**. Écris le détail
d'abord, coupe-le à une phrase, et seulement ensuite dessine la boîte autour.

⚠️ **Le vrai danger d'une coupe, c'est le qualificatif.** Couper enlève des mots ;
certains mots portent la vérité de la phrase. « la recherche a **probablement** déjà
tourné » devenu « la recherche a déjà tourné », « grep **contre les noms propres de la
source** » devenu « grep les noms propres » : les deux sont plus courts, les deux se
rendent, les deux sont faux ou inutilisables — et aucun outil ne le voit. Les deux ont
été écrits ici, et rattrapés à la relecture. Coupe des mots, jamais un *probablement*,
un *seulement*, un *sauf* ni le complément qui dit CONTRE QUOI on vérifie.

⚠️ **L'exemple ci-dessus dépasse de quelques caractères sur trois détails** (81, 82 et 87
pour ~80). Il est ANTÉRIEUR à ces bornes et on le garde tel quel : sa valeur est d'être,
au caractère près, le dessin qui se rend en production. Copie sa géométrie, pas sa
densité — et ne le « corrige » pas ici sans corriger la procédure elle-même, sinon la
seule chose qui le rend vérifiable disparaît.

## Ce que la grammaire ne sait PAS dire

Relevé en dessinant treize procédures réelles. Aucun de ces points n'est un bug : le
parseur refuse ce qu'il ne peut pas lire sans risque. Ce qu'il faut, c'est savoir quoi
faire à la place — et le DIRE, plutôt que tordre le dessin pour faire semblant.

| Ce que le process fait | Ce que la grammaire a | Ce qu'on fait |
|---|---|---|
| Une **boucle** / un **retry** (« rejoue jusqu'à ce que ça passe ») | rien : pas d'arête retour | l'énoncer dans la phrase de détail de la boîte ou en note de marge |
| Une **branche qui saute une étape puis rejoint** (« seulement si… ») | rien : un `▼` conditionnel reste sur le chemin de TOUT LE MONDE | replier la condition dans la boîte concernée, jamais une boîte que « certains runs sautent » |
| Un **repli** (B échoue → C) | seulement le fan-out, qui affirme que les deux colonnes tournent toujours | soit deux colonnes en l'assumant, soit une seule boîte qui porte le repli en détail |
| Un **déclencheur planifié** (cron) | une ligne de titre + une ligne d'exemple entre guillemets | dire la planification dans le TITRE du déclencheur, garder la ligne citée pour la phrase qu'un humain taperait |
| Une **sortie qui repart ailleurs** (pas un arrêt) | `▪` veut dire « ça s'arrête ici » | ne pas en faire un `▪` ; s'il le faut, corriger la ligne de légende pour ne pas mentir |

Et cinq pièges de géométrie, chacun payé au moins une fois :

- **Une sortie ne tient que sur la colonne de DROITE d'une paire parallèle.** Son
  `├───▶` part vers la droite et traverserait le `│` de l'autre colonne ; et une ligne
  de branche ne porte qu'UN `├`. Si une seule des deux colonnes a une sortie, mets-la à
  droite — l'exemple de référence fait exactement ça.
- **Le texte à droite de la DERNIÈRE boîte appartient à cette boîte** ; le texte dans
  l'écart entre deux boîtes côte à côte appartient à celle de GAUCHE — et cet écart ne
  fait que deux caractères. En pratique : dans une paire parallèle, seule la boîte de
  droite peut porter une note de marge. Ce qu'aurait dit la gauche descend dans sa
  phrase de détail.
- **Une sortie latérale sépare son nom de sa description par 2 espaces AU MOINS.** Avec
  un seul, tout devient le nom.
- **Une étape humaine se reconnaît au mot « human »** dans le titre ou la note de
  marge — et seulement sur une boîte ORDINAIRE : une boîte à double filet reste un
  contrôle, même si c'est une personne qui l'exécute.
- **Deux boîtes parallèles numérotées pareil s'affichent pareil.** Le parseur l'accepte,
  le lecteur voit deux fois le même numéro.

Enfin le **budget** : 4 à 9 boîtes. Au-delà, on GROUPE des phases (et on le dit dans la
prose), on ne tronque jamais en silence. Un process de quinze étapes se dessine en neuf
boîtes honnêtes ; il ne se dessine pas en neuf premières étapes.

## Deux règles de rédaction qui coûtent cher

**La deuxième ligne du déclencheur est un EXEMPLE, entre guillemets droits.** Écris la
phrase que l'opérateur de CE client taperait vraiment — pas une consigne (« give one
short example »), pas une description de l'entrée. Le front l'extrait comme *prompt
d'exemple* et l'affiche comme telle : une consigne à cet endroit s'affiche au lecteur
comme si c'était ce qu'il doit taper.

**La prose sous le dessin n'énonce aucun fait qui dérive.** Pas de nombre de runs, pas
de taux de réponse, pas de dates, pas de volumes. Le dessin survit à ces chiffres ; une
légende d'une ligne (`▪ terminal — …`) après une ligne vide est acceptée et ignorée par
le parseur, c'est le bon endroit pour dire ce que veut dire un marqueur, jamais pour
dire combien.

## Se relire

Les mots du dessin sont les mots du corps : chaque boîte, chaque sortie, chaque
condition doit être quelque chose que la procédure dit vraiment. On ne dessine pas un
process qu'on aimerait avoir.

À l'écriture (`oto_procedure op=set`), la réponse porte `diagram_warning` quand le corps
n'embarque aucun dessin. C'est un **warning, pas un refus** — la procédure est
enregistrée, et sa page se rendra vide. Le check du serveur est volontairement grossier
(assez de caractères de tracé, sur assez de lignes) : il attrape l'absence, il ne
garantit pas la grammaire. Seul le rendu de la page tranche — relis la page du process
après avoir écrit.
