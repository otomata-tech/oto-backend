---
title: Email d'activation d'un tenant (2026-10-03)
type: reference
---

# Email d'activation d'un tenant (2026-10-03)

Le code : `activation.py` (réglage, contenu, passage, essai), requête
`db/activation.py`, travail `oto-mcp maintenance activation-connect`. Journal et refus :
ceux de la relance (`outreach_sends`, `outreach_optouts`) — **aucune table neuve**.

## Ce que c'est

Un tenant qui sert la plateforme sous sa marque peut déclarer qu'il écrit à ses comptes
qui ne se sont **jamais branchés** : un email, une fois, en anglais, qui donne les
étapes exactes pour ajouter le connecteur à l'agent (Claude), le lien du connecteur, et
la page d'accueil du tenant pour les autres agents.

C'est l'inverse de la relance (`relance-comptes.md`) : la relance est écrite **par
nous** à **nos** comptes, et écarte ceux des tenants tiers. L'activation est écrite
**par le tenant** à **ses** comptes, dans sa marque et depuis son adresse, parce qu'il
l'a déclaré. La requête ne lit donc que les comptes du tenant configuré ; celle de la
relance reste intacte, garde-fou partenaire compris.

## Qui le reçoit

Une **personne** (une boîte mail, comme la relance) :

- dont au moins un compte est qualifié sous le tenant (`<slug>:…`) ;
- inscrite entre `delay_hours` (24 h) et `window_days` (30 j) — la fenêtre couvre du
  même geste les nouveaux et ceux qui attendent déjà ;
- **jamais branchée** : aucune ligne `tool_calls` de kind `mcp` **ni `protocol`** sur
  aucun de ses comptes. Une ligne `protocol` est un `initialize` : la personne a ajouté
  le connecteur sans rien demander encore, le message serait faux pour elle. Une ligne
  `rest` (le tableau de bord) ne compte pas : ouvrir l'application n'est pas brancher
  son agent ;
- ni suspendue, ni désinscrite, ni déjà servie sur cette campagne
  (`activation-connect:<slug>`) ;
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
| `delay_hours`, `window_days`, `max_per_run` | non | 24, 30, 50 |
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
   python -m oto_mcp.activation essai --tenant <slug> --operateur <sub>
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
