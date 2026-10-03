---
title: Emails d'activation d'un tenant (2026-10-03)
type: reference
---

# Emails d'activation d'un tenant (2026-10-03)

Le code : `activation.py` (réglage, contenu, passage, essai), requête
`db/activation.py`, travail `oto-mcp maintenance activation`. Journal et refus :
ceux de la relance (`outreach_sends`, `outreach_optouts`) — **aucune table neuve**.

## Ce que c'est

Un tenant qui sert la plateforme sous sa marque peut déclarer qu'il écrit à ses comptes
pour les faire avancer, une étape à la fois. Trois emails, en anglais, chacun UNE fois,
quand le journal dit que la personne est à l'étape qu'il fait franchir :

| étape | quand | ce que dit l'email |
|---|---|---|
| `connect` | jamais branchée | les étapes exactes pour ajouter le connecteur à l'agent, le lien du connecteur, la page d'accueil pour les autres agents |
| `first-process` | branchée depuis 48 h, aucun processus terminé, silencieuse depuis 48 h | la phrase à dire à l'agent pour qu'il propose et lance un premier processus |
| `recurring` | un processus terminé, rien de régulier | la phrase pour le programmer |

C'est l'inverse de la relance (`relance-comptes.md`) : la relance est écrite **par
nous** à **nos** comptes, et écarte ceux des tenants tiers. L'activation est écrite
**par le tenant** à **ses** comptes, dans sa marque et depuis son adresse, parce qu'il
l'a déclaré. La requête ne lit donc que les comptes du tenant configuré ; celle de la
relance reste intacte, garde-fou partenaire compris.

## Qui le reçoit

Une **personne** (une boîte mail, comme la relance), dont au moins un compte est
qualifié sous le tenant (`<slug>:…`). L'étape se lit sur TOUS ses comptes :

- **`connect`** : aucune ligne `tool_calls` de kind `mcp` **ni `protocol`**. Une ligne
  `protocol` est un `initialize` : la personne a ajouté le connecteur sans rien demander
  encore, le message serait faux pour elle. Une ligne `rest` (le tableau de bord) ne
  compte pas : ouvrir l'application n'est pas brancher son agent. Inscrite entre
  `delay_hours` (24 h) et `window_days` (30 j) — la fenêtre couvre du même geste les
  nouveaux et ceux qui attendent déjà.
- **`first-process`** : branchée depuis 48 h, aucun déroulé de procédure clos `done`
  (même définition que la case « First process run » de l'écran d'accueil), **et aucun
  appel d'agent depuis 48 h** — beaucoup travaillent sans ouvrir de run, et leur écrire
  « lancez un premier processus » serait du bruit.
- **`recurring`** : au moins un déroulé `done`, aucun agent programmé actif à son nom,
  et pas deux jours distincts de déroulés terminés — une tâche programmée côté agent ne
  se voit pas d'ici, deux jours distincts sont la trace qu'elle laisse.
- Les deux dernières étapes ne valent que pendant `sequence_days` (14 j) après
  l'inscription. Les étapes sont disjointes : une personne n'en reçoit jamais deux dans
  le même passage.

Pour toutes : **48 h au moins entre deux emails d'activation** (lu dans la requête,
toutes étapes confondues) ; une fois par étape (campagne `activation-<étape>:<slug>`) ;
ni suspendue, ni désinscrite ;
- ni opérateur de plateforme, ni adresse `+suffixe` d'un opérateur (nos comptes
  d'essai), ni sur un domaine que le tenant déclare comme le sien (`exclude_domains`).

## Le réglage

`OTO_ACTIVATION`, un objet JSON par slug de tenant :

| clé | requis | rôle |
|---|---|---|
| `sender` | oui | « Nom <adresse> » — domaine vérifié chez le relais utilisé |
| `reply_to` | oui | la boîte qui reçoit les réponses |
| `cc` | non | copies VISIBLES de chaque envoi |
| `app_url` | oui | la page d'accueil du tenant (bouton, autres agents) |
| `mcp_url` | oui | le lien du connecteur, tel que l'écran d'accueil le montre |
| `help_url` | non | un article d'aide, cité s'il est donné |
| `exclude_domains` | non | les domaines de l'équipe du tenant |
| `delay_hours`, `window_days`, `sequence_days` | non | 24, 30, 14 |
| `max_per_run` | non | 50 — pour le passage entier, toutes étapes |
| `mailer_url` | non | le relais du tenant ; son jeton dans `OTO_ACTIVATION_MAILER_BEARER` |
| `link_base` | non | l'hôte du lien de refus (celui du tenant) ; sinon `OTO_MCP_PUBLIC_URL` |
| `steps`, `agent_label` | non | les étapes et le nom de l'agent, si le tenant en sert d'autres |

⚠️ **Le relais.** `from` doit être sur un domaine que le relais a vérifié chez son
fournisseur (TEM). Sans `mailer_url`, c'est le relais de l'instance (`OTO_MAILER_URL`)
et son allowlist `MAILER_FROM_DOMAINS` ; ajouter un domaine à cette liste sans l'avoir
vérifié chez le fournisseur échange un refus franc contre un envoi en indésirables. Un
relais déclaré sans jeton n'envoie rien : le jeton de l'instance ne part jamais vers un
autre relais.

⚠️ **La marque.** Le nom du produit et le dessin viennent de `tenants.brand`. Le
travail tourne hors du serveur : il pose lui-même le registre des tenants, et sans
marque déclarée complète il n'envoie rien (le gabarit neutre signerait du slug).

## Les trois verrous

1. **Fermé par défaut** (`OTO_ACTIVATION_ENVOI`) : sans le drapeau, le passage dit
   combien de personnes sont dues et n'écrit rien.
2. **Pas d'envoi sans essai reçu** : comme la relance, un envoi réel exige une ligne
   `kind='test'` portant l'empreinte du contenu servi — texte, expéditeur, réponse et
   copies compris. L'essai :

   ```
   python -m oto_mcp.activation essai --tenant <slug> --operateur <sub> [--etape <étape>]
   ```

   envoie le message à l'opérateur (avec les copies) et enregistre l'essai.
   `python -m oto_mcp.activation apercu` montre le contenu et l'audience sans rien
   envoyer.
3. **Une fois par personne** : la trace est écrite avant l'envoi, retirée si le relais
   refuse — la personne redevient due au passage suivant.

## Ce qui reste partagé avec la relance

⚠️ Le **refus est commun** : se désinscrire de l'activation désinscrit des relances, et
réciproquement — c'est la même table. Le digest de signaux garde la sienne (oto#150).
Si l'activation doit devenir un canal distinct, c'est une table à ajouter. Le lien de
refus porte `?lang=en` : la page de confirmation parle la langue du mail.

⚠️ **Les copies portent le lien de refus du destinataire.** Un clic sur
« unsubscribe » dans une copie désinscrit la personne, pas celui qui lit la copie :
c'est un levier pour l'équipe du tenant, et un risque si un analyseur de liens suit
les URL de ces boîtes (le lien est un GET qui écrit, cf. `outreach_unsubscribe`).

## Ce qui n'est pas encore là

Les textes sont FIXES, un par étape — d'où l'essai par empreinte. L'étape suivante est
un message écrit pour chaque personne par un agent, à partir d'un instantané de son
avancement et du contexte de son entreprise. Elle demande trois choses que ce lot
ne pose pas : une identité de service pour un travail qui n'appartient à aucune org
cliente ; une table des décisions et de leurs suites (donc une migration) ; et, à la
place de l'essai par empreinte (impossible quand chaque texte est unique), une
validation automatique de chaque message et un chemin de relecture humaine quand le
validateur doute.
