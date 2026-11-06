---
title: Email — envoi per-org, par connecteur
type: reference
---

# Email — envoi per-org, par connecteur

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Envoi d'email modélisé **par connecteur** (la config/gestion email s'exprime comme
celle d'un connecteur, pas une page à part). **Deux connecteurs** (déclarés dans `providers/scaleway.py` et
`providers/resend.py` ; le routage expéditeur→transport reste
`providers.EMAIL_CONNECTOR_TRANSPORT`) :
`scaleway` (**BYO-org depuis le 2026-07-01** : `auth_modes={byo_org}`,
`secret_kind="fields"` — `secret_key`+`project_id`+`region` du compte Scaleway TEM
de L'ORG ; transport = API TEM en direct `email.send_via_scaleway_tem`, sans service
mailer ni clé plateforme. Le mode par défaut — emails de la plateforme (invitations,
digests, alertes) et envoi sous l'adresse de l'instance par `email_send` :
`email.send_composed_email` → `email._send` — part sur le **transport de l'instance**
(voir « Transport des mails de plateforme » ci-dessous) ;
master ON **sûr** car la propriété du domaine
est garantie PAR Scaleway — l'API refuse un `from` dont le domaine n'est pas vérifié
dans le compte de l'org, ce qui rend #64 caduque) + `resend` (BYOK,
`auth_modes={byo_org}`). **Le transport DÉRIVE du connecteur** :
`providers.EMAIL_CONNECTOR_TRANSPORT={scaleway:scaleway, resend:resend}` (pas de
champ transport sur l'expéditeur).

- **Le DESSIN suit la marque du destinataire** (`email_brand.py`, 2026-09-02). Le
  texte des gabarits écrivait le nom du produit du destinataire depuis 7d10a798,
  mais la couleur restait celle d'oto pour tout le monde : le client d'un partenaire
  lisait le nom de SON produit en brun otomata, puis cliquait vers une application
  blanc-et-ardoise.
  `email_brand.marque(<slug>)` (le MÊME slug que le texte : `orgs.front_brand` /
  `config.front_for`) porte nom, site et palette ; `page()` rend le document complet
  et `bouton()` le CTA. Un slug **inconnu** prend un gabarit neutre **portant son
  nom** — jamais un repli sur oto, qui serait le faux qu'on répare.
  ⚠️ **La palette d'un tenant se DÉCLARE en base** (`tenants.brand`, 03/09/2026),
  Celle d'un partenaire écrite dans notre code obligeait à nous redéployer pour
  accueillir le suivant — même défaut que son `dashboard_url`, même remède, même
  table. Le tenant **primaire** a sa marque à part (`email_brand.marque_instance`) :
  ses teintes sont dans le code, son **nom et son site se déclarent**
  (`OTO_BRAND_NAME`, `OTO_BRAND_SITE`, #968) — le site était écrit en dur, donc toute
  autre instance signait ses emails de notre adresse.
  ⚠️ **Sans marque, on passe `None`, jamais `"oto"`** : `None` (une org sans
  `front_brand`) résout vers la marque de l'instance. Le littéral `"oto"` n'est le
  primaire que sur UNE instance ; ailleurs c'est un slug tiers, donc le gabarit neutre
  « oto » — toutes les invitations d'une autre instance sont parties ainsi.
  ⚠️ Le primaire ne lit pas `tenants.brand` : son expéditeur est `OTO_MAIL_FROM`
  (forme `Nom <adresse>` acceptée), pas `expediteur` ; sa langue pour un destinataire
  sans préférence (un invité sans compte) est `OTO_BRAND_LANGUE`, pas `langue`.
  ⚠️ Une palette **incomplète est refusée EN ENTIER**, jamais complétée par la nôtre :
  sept teintes venues de deux chartes donnent un dessin que personne n'a dessiné, et
  qui ne se voit qu'à l'arrivée, chez le destinataire. Les teintes sont validées à la
  LECTURE (`#rgb`/`#rrggbb`) — le registre transporte ce qui est déclaré, il ne juge
  pas des couleurs ; et cette validation est aussi ce qui empêche une valeur de base
  d'écrire autre chose qu'une couleur dans un attribut `style`.
  ⚠️ **Le tenant primaire ne se surcharge pas** : notre charte n'est pas une
  configuration, et une ligne en base ne repeint pas oto (`tests/test_marque_par_tenant.py`). Contraintes de
  client mail portées par le gabarit et gardées par `tests/test_email_charte.py` :
  tables imbriquées (Outlook ne met en page que ça), styles en LIGNE uniquement
  (pas de `<style>`, pas de variable CSS, pas de flex/grid), `color-scheme: light`
  déclaré (sinon Outlook mobile repeint le fond et laisse le texte), preheader caché
  (sinon la boîte affiche « ou collez ce lien : https://… » en ligne d'aperçu).
  Répartition : `email.py` = transport, `email_templates.py` = texte + locale,
  `email_brand.py` = dessin. Les trois modules s'importent **par module**, jamais par
  nom à plat — le cycle est mutuel et seul l'import de module le rend inoffensif.
  ⚠️ **L'EXPÉDITEUR, lui, reste `Oto <oto@otomata.tech>`** pour toutes les marques :
  l'allowlist `MAILER_FROM_DOMAINS` vit dans `otomata-tech/otomata-auth-mailer`, pas
  ici, et un domaine hors allowlist rend un 403 que le best-effort avale en silence.
  ⚠️⚠️ **NE PAS y poser le domaine d'un partenaire — l'inverse a été fait le
  03/09/2026** : ce domaine a été RETIRÉ de l'allowlist ce jour-là (rapporté par la
  session infra, non vérifié depuis ce dépôt — l'allowlist vit ailleurs). Cette note
  disait « le poser là-bas est le seul geste qui manque » : la suivre armerait un
  envoi qui échouerait **sans rien dire**, puisque le refus est avalé. Aucun envoi
  produit ne part sous ce domaine aujourd'hui, donc rien n'est cassé — mais l'écrire
  serait le casser en silence. L'expéditeur reste le nôtre ; **ce qui suit la marque
  d'un partenaire côté produit, c'est le DESSIN, pas le transporteur.**
- `email_send` (`tools/email.py`) = **spine** (pas un connecteur) : route
  `sender→connecteur→transport` ; autz dynamique (membre d'org pour une adresse
  déclarée ; super_admin pour le repli marque `oto@otomata.tech`). `email.py` =
  `send_composed_email` (transport de l'instance : relais `OTO_MAILER_URL` ou API TEM `OTO_TEM_*`) +
  `send_via_resend` (httpx direct, clé org). `scaleway` = provider
  credential/config-only (`tools/scaleway.py` = `register()` no-op) ; `resend`
  sert EN PLUS ses propres outils — la liste de contacts des Broadcasts
  (`resend_contact`, `resend_segment`, `resend_topic`, `tools/resend*.py`), avec
  la MÊME clé d'org, qui doit alors être en accès complet (une clé « envoi
  seul » continue d'envoyer, et ces outils la refusent en le disant). L'envoi
  n'en dépend pas : `email_send` garde son chemin. Le catalogue de namespaces
  ne masque donc que `providers.EMAIL_ONLY_CONNECTORS` (`scaleway`), pas tout
  `EMAIL_CONNECTOR_TRANSPORT`.
- **Config = `orgs.email_settings` JSONB keyé PAR CONNECTEUR** :
  `{<connector>:{senders:[{email,name?,reply_to?}], quiet_hours?}}` (calqué sur
  `field_filters`). `org_store.get/set_org_email_settings(org, connector)`,
  `resolve_sender(org, from)→(sender, connector)`, `org_email_quiet_hours`. Capacité
  `orgs_email_settings` : GET bundle + `PUT /api/orgs/{id}/email-settings/{connector}`.
- **Envoi différé** : params `send_at`/`force_now` + garde-fou **quiet hours par
  connecteur** (défaut Europe/Paris 20h–8h). `scheduler.py` : `compute_scheduled_at`
  (pure, testée) + boucle asyncio démarrée via le lifespan, **en production seulement**
  (`boucles_de_fond.py` : la préprod partage la file et n'y touche pas), batch isolé
  en `asyncio.to_thread` (ne bloque pas l'event loop) ; table `scheduled_emails`
  (claim `FOR UPDATE SKIP LOCKED`, retry ×3 — ⚠️ le claim incrémente `attempts` sans changer
  le statut : deux schedulers concurrents peuvent envoyer deux fois le même message, d'où un
  seul environnement sur la file). Gestion : `oto_list/cancel_scheduled_emails`.
- **Vérif de domaine d'envoi = déléguée au provider** (les deux connecteurs sont
  BYO) : Scaleway TEM comme Resend refusent un `from` hors domaine vérifié dans le
  compte de l'org → pas de vérif côté oto (#64 sans objet depuis le passage BYO).
  Otomata (l'org maison) envoie avec sa clé TEM dédiée (app IAM `oto-email-scaleway`,
  vault `SCW_TEM_*`).

## Transport des mails de plateforme (relais mailer ou API TEM de l'instance)

Tout mail de plateforme passe par `email._send`, qui part sur le transport que
l'instance **déclare** (`email.transport_plateforme`, relu à chaque envoi et rejoué au
démarrage par `identite_instance.verifier`) — l'un ou l'autre, jamais les deux :

| transport | variables | quand |
|---|---|---|
| relais mailer | `OTO_MAILER_URL` (+ bearer `OTO_MAILER_SEND_BEARER`, secret) | notre production |
| API Scaleway TEM en direct | `OTO_TEM_SECRET_KEY` (secret), `OTO_TEM_PROJECT_ID`, `OTO_TEM_REGION` — les trois ensemble | une instance sans mailer |

- **Aucun, les deux, ou TEM à moitié ⟹ `RuntimeError`**, au démarrage comme à l'envoi —
  jamais de repli de l'un sur l'autre (même régime que `require_env`, #968). Une
  instance TEM déclare `OTO_MAILER_URL` **vide** (la variable reste de la classe
  identité, la déclaration de déploiement en exige la clé).
- **Le transport TEM réutilise `send_via_scaleway_tem`**, celui d'une org qui apporte sa
  clé, avec les identifiants de l'instance. L'expéditeur reste `OTO_MAIL_FROM`
  (`Nom <adresse>` décomposé en nom + adresse pour l'API) : son domaine doit être
  **vérifié dans le projet TEM de l'instance**, c'est Scaleway qui refuse sinon — aucune
  logique de domaine côté oto. Un expéditeur sans adresse lisible lève.
- **Mêmes garanties** sur les deux transports : CR/LF neutralisés sur chaque en-tête
  (to, sujet, from, reply-to, cc), `cc` et `reply_to` portés (TEM : `Reply-To` en
  en-tête additionnel). Le refus d'expéditeur signalé en erreur (`_relais_refuse`) est
  propre au relais (son allowlist `MAILER_FROM_DOMAINS`) ; un refus TEM se journalise
  comme celui d'une org (`send_via_scaleway_tem`), best-effort.
- Un relais déclaré **par l'appelant** (`mailer_url` + `bearer`, l'activation d'un
  tenant) passe toujours par ce relais : le transport de l'instance n'intervient pas.
- **Une partie `text/plain` part avec chaque mail de plateforme** (06/11/2026) :
  `email.version_texte` la tire du HTML (liens gardés en clair, preheader caché et
  caractères invisibles retirés), `_send` la passe aux deux transports (`text` au relais,
  `text` à l'API TEM). Un email en HTML seul est un signal anti-spam classique : des
  invitations arrivaient en spam alors que SPF, DKIM et DMARC passaient. Le relais doit
  **transmettre** `text` à son SMTP — un relais qui l'ignore envoie en HTML seul, comme
  avant, sans erreur. Banc : `tests/test_email_version_texte.py`.
- **L'invitation se présente** (06/11/2026) : l'hôte se nomme (`users.name` + son
  adresse, `inviter_name`), une phrase dit ce qu'est le produit, une autre quoi faire
  d'une invitation inattendue ; l'aperçu nomme l'hôte. `email_brand.page` ne met plus de
  bourrage de largeur nulle après le preheader (120 caractères cachés = signal
  anti-spam) : chaque preheader est une phrase qui se lit seule.
- Banc : `tests/test_email_transport_instance.py`.

> **Invariant connecteurs (corrigé 2026-06-24)** : `_org_list` (vue ORG
> `/org/connectors`) ne liste QUE les connecteurs **activés par la plateforme**
> (master ON, ou forcé par l'override d'org), comme la surface USER
> (`_visible_catalog`). Master-OFF non accordé → invisible (fin du levier inerte
> « coupé par la plateforme »). Filtre sur le **cap master**, pas sur `effective`
> (un override OFF d'org doit rester réactivable).

## Front qui héberge l'org (invitations, 07/08)

> **Front qui héberge l'org (invitations, 07/08).** oto-backend sert plusieurs produits
> depuis une instance (oto, un tenant tiers) : deux colonnes `orgs.front_base_url` / `front_brand`
> (NULL = oto) portent le front d'une org, lues par `emit_invitation` — base du lien
> `/invitation/<token>`, marque du texte du mail, **et pas de magic-link** dès qu'un front
> tiers est posé (l'OTT est minté sur NOTRE Logto : il serait inerte sur l'émetteur dédié
> du tiers, soit un échec de connexion silencieux). **Dérivé de l'org CIBLE, jamais déclaré
> par l'appelant** — sinon c'est un champ d'API publique (REST + surface MCP) qu'il faudra
> retirer à l'arrivée de l'étage tenant (ADR 0052, où ces colonnes remontent d'un cran), et
> une invitation pourrait prétendre venir d'un front auquel l'org n'appartient pas. Les 3
> niveaux de la cascade en héritent sans rien porter. La marque s'arrête au TEXTE :
> l'expéditeur reste `_MAIL_FROM`, un domaine d'envoi tiers supposerait sa vérification TEM.
> ⚠️ Aucune surface n'édite ces colonnes (UPDATE à la main) : une nouvelle org sous front
> tiers naît donc sous marque oto tant que personne ne la renseigne.

## Une image en tête d'un `email_send` (2026-08-29)

**Par où une image arrive-t-elle à une URL publique stable ? Par `oto_upload_url(target="image")`.**
Avant ce lot, aucun chemin MCP n'y menait : `target='project_file'` dépose un blob
**privé** durable (l'agent n'en reçoit qu'une `download_url` signée qui expire) ; la
bascule publique d'un fichier de projet (`POST /api/me/projects/{p}/files/{f}/public`)
est **REST-only**, sans face MCP, et concerne un « Autre document » ; et
`media_store.upload_image` (public-read, clé par hash de contenu, 2 Mo, type par magic
bytes) n'était branchée que sur l'avatar et le logo d'org, en multipart REST. La cible
`image` est la **plus petite exposition** de cette fonction, choisie contre une entrée
`image={kind: drive|url}` sur `email_send` : celle-ci aurait ré-uploadé le visuel **à
chaque envoi** — or le même visuel ressert (trois mails d'onboarding, des annonces).
**Un upload, une URL, réutilisée d'envoi en envoi.**

Ce que la cible `image` garantit (`upload_tokens.py`, `media_store.upload_image`) :
- **porteur authentifié** : le sub est scellé dans le jeton signé (même régime que
  l'avatar) ; aucune ressource cible, donc aucune autre autz à réappliquer ;
- **2 Mo max** (`OTO_MCP_S3_MAX_IMAGE_BYTES`), et c'est cette borne que le mint annonce
  dans `max_bytes` — pas le plafond générique de 25 Mo ;
- **png / jpeg / gif / webp seulement, reconnus aux octets** : le `Content-Type` déclaré
  (curl, formulaire) n'est jamais cru ;
- **clé non devinable** : `images/<sub>/<sha256[:32]>.<ext>` — 128 bits qu'on ne retrouve
  qu'en possédant l'image ; ré-uploader le même fichier rend la même URL (idempotent) ;
- **l'accusé rend `url`** (publique, permanente, `Cache-Control: immutable`) — et la page
  d'upload humaine (claude.ai sans shell) l'affiche aussi, sinon le dépôt serait un
  succès dont personne ne peut rien faire.

Ce que le gabarit impose (`email.render_composed_email`, `_image_html`) :
- **une seule image, avant le corps** — pas de galerie, pas d'image par section, pas de
  pièce jointe ;
- **`image_alt` REQUIS** avec `image_url`, et refusé sans elle : beaucoup de clients
  bloquent les images, le mail doit garder son sens ; aucun texte par défaut (il ne
  dirait rien) ;
- **`https://` seul** (un `http://` est bloqué ou marqué « non sécurisé », un `data:`
  n'est pas une URL publique) ;
- **largeur utile 480 px** : `width="480"` (lu par les clients qui ignorent le CSS) +
  `max-width:100%; height:auto; display:block` (affichage réduit) ;
- **URL et alt échappés en attribut, guillemets compris** (`html.escape(quote=True)` —
  `_esc` ne traite pas `"`, et un `"` dans l'alt refermerait l'attribut) ; le `href`
  du bouton (`cta_url`, fourni par l'agent lui aussi) est échappé de la même façon
  depuis ce lot — c'était le même trou ;
- **sans image, le rendu est celui d'avant à l'octet** (golden dans
  `tests/test_email_image.py`).

L'appel complet, tel que le couvrent `tests/test_upload_image_public.py` et
`tests/test_email_image.py` :

```
# 1. publier le visuel UNE fois (agent avec shell)
oto_upload_url(target="image")
  → {url: "https://mcp.oto.cx/api/upload/<jeton>", method: "PUT", max_bytes: 2097152, …}
curl -X PUT --data-binary @hero.png 'https://mcp.oto.cx/api/upload/<jeton>'
  → {"ok": true, "kind": "image", "url": "https://<bucket>/images/<sub>/<hash>.png", "bytes": 48213}
#    (sans shell : transmettre l'URL d'upload à l'humain, la page affiche l'URL publique)

# 2. relire, puis envoyer — la même `url` sert à chaque mail
email_send(to="…", subject="bienvenue sur oto", body="…",
           image_url="https://<bucket>/images/<sub>/<hash>.png",
           image_alt="l'écran d'accueil d'oto : vos connecteurs, prêts à l'emploi",
           cta_text="ouvrir oto", cta_url="https://manage.oto.cx", dry_run=True)
email_send(…, dry_run=False)          # ou send_at=… : la file porte le HTML avec l'image
```

Refus explicites, jamais de repli : `image_url` sans `image_alt`, `image_alt` sans
`image_url`, une URL qui ne commence pas par `https://`, un fichier de plus de 2 Mo, un
fichier qui n'est pas une image. Laissé de côté, volontairement : plusieurs images,
l'image par section, la pièce jointe, et une entrée `image={kind: drive|url}` résolue
côté serveur par `file_source` (un upload par envoi — la mauvaise forme pour un visuel
qui ressert).

## Locale des 6 gabarits transactionnels (oto-backend#700, 2026-09-01)

`users.locale` existait déjà (dashboard : capacité `me.locale.set`, `GET /api/me`,
`lib/i18n.ts` côté front) mais n'était consultée par AUCUN envoi transactionnel :
`email.py` servait tout en français, en dur. Un compte qui avait explicitement mis
son dashboard en anglais recevait quand même ses invitations et notifications en
français — trouvé en préparant une campagne d'onboarding (14 mails FR à des adresses
en partie non francophones).

- **Les 4 gabarits** (`send_invite_email`, `send_resource_shared_email`,
  `send_resource_transferred_email`, `send_signal_digest_email` — les deux e-mails de
  proposition sont retirés depuis le 14/09/2026, oto#191) prennent
  `locale: str | None = None`. `'en'` sert la version anglaise ; toute autre valeur
  (dont `None`, `users.locale IS NULL`) sert le FR **à l'octet près** — comportement
  inchangé pour un compte sans préférence. Comparaison directe (`locale == "en"`),
  sans normalisation de casse : l'énum `'en'|'fr'` est déjà validée en amont par
  `me.locale.set`.
- **Extraites dans `oto_mcp/email_templates.py`** : `email.py` frôlait déjà 500
  lignes, et une deuxième langue par gabarit l'aurait fait déborder. Le TRANSPORT
  (`_send`, l'anti-injection d'en-tête, les envois BYO) reste dans `email.py` ; le
  TEXTE des 4 gabarits vit dans `email_templates.py`, qui y accède par
  `import email as _email` (jamais `from .email import _send` — un import de nom
  capturerait une copie figée qu'un `monkeypatch.setattr(email, "_send", ...)` ne
  toucherait pas). `email.py` réexpose les gabarits pour que
  `email.send_invite_email` etc. restent des attributs valides du module `email`, ce
  que les tests monkeypatchent — par import de MODULE lui aussi (`from . import
  email_templates`), le nom lu À L'APPEL par son `__getattr__`. ⚠️ Jamais
  `from .email_templates import <nom>` : entrer dans le trio par `email_templates`
  (ce que fait la maintenance `alertes-credential`) trouvait ce module à moitié
  chargé, et l'import levait.
- **Les appelants lisent `users.locale` du DESTINATAIRE, pas de l'émetteur** :
  - `orgs/invites.py::emit_invitation` — `db.get_user_by_email(email_addr)`. Une
    adresse jamais vue (pas encore de compte) n'a pas de ligne `users` ⟹
    `locale=None` ⟹ FR. La détection de langue pour un contact jamais loggé reste
    **hors scope** (pas de signal exploitable aujourd'hui — capture à
    l'inscription ? `Accept-Language` ? — question ouverte, pas ce lot).
    ⚠️ **Question MESURÉE le 2026-09-02, et la réponse est « rien »** :
    `users.locale` est posée sur 11 des 64 comptes de notre tenant et sur **2 des
    40** d'une audience de relance, `billing_identities.country_code` est à **zéro
    ligne**, et le TLD ne tranche pas. Il n'y a donc pas d'algorithme à améliorer —
    il faut **demander** la langue à l'inscription. Détail et chiffres :
    `relance-comptes.md`.
  - `resources.py::_notify_grant` — même `db.get_user_by_email`, déjà appelé pour
    dériver `dest_sub`/le front. `type_label` (« projet »/« project ») est choisi
    ICI, à la source (`_TYPE_LABELS` / `_TYPE_LABELS_EN`) : le gabarit ne traduit
    JAMAIS un mot qu'on lui donne.
  - `usage.py::_notify_reporters` — jointe UNE fois dans
    `db.pending_signal_notices()` (`LEFT JOIN users`, colonne `u.locale` à côté de
    `u.email`/`u.name`) : pas d'aller-retour supplémentaire, une propriété du
    destinataire portée par la même ligne pendant tout le regroupement.
- **Ce qui n'est PAS traduit** : le contenu écrit par un humain ou un agent (noms de
  projet, titres de doc, corps libre d'un signal `usage_signals.body`) — ce ne sont
  pas des mots du gabarit, les traduire changerait ce que quelqu'un a écrit.

## Le pied de page d'un envoi avec la clé de l'org (décision du 12/09/2026)

**Le constat.** Apporter sa clé (Resend, Scaleway TEM) changeait le TRANSPORT, pas le
GABARIT : un `email_send` de prospection à froid partait avec notre pied — « vous
recevez ce message car vous avez un compte oto », signature `oto · oto.cx` — et
l'invitation à se désabonner renvoyait vers nous, sur des prospects qui ne nous
connaissent pas. Rapporté par deux retours d'un même espace le 27/08, arbitré le
12/09 comme le seul point de conformité de la pile (oto-backend#443).

**La décision d'Alexis, verbatim** : « Autoriser, désabonnement exigé — l'org peut
retirer notre pied de page à condition de fournir le sien ; sinon le nôtre reste. »

**Le contrat livré :**
- l'org déclare, **par connecteur**, `footer: {unsubscribe_url?, unsubscribe_email?}`
  (`oto_org_settings domain=email op=set` / `PUT /api/orgs/{id}/email-settings/{connector}`,
  org_admin). Stocké dans `orgs.email_settings.<connector>.footer` — JSONB existant,
  **aucun changement de schéma** ; `org_store.set_org_email_footer` / `org_email_footer`
  (fonctions à part : la signature de `set_org_email_settings` est figée) ;
- **déclaré ⟹ son pied remplace le nôtre** sur les envois de ce connecteur : ni
  signature de marque ni mention de compte, seulement le moyen de se désabonner
  (`email._pied_de_l_org`, `email_brand.page(signature=False)`) ;
- **non déclaré ⟹ rien ne change** ; un `footer` sans lien ni adresse est refusé
  (`unsubscribe_required`) par un message qui nomme le geste ; un lien non `https://`
  ou une adresse sans `@` sont refusés à la déclaration (le gabarit les refuserait à
  l'envoi). `clear_footer=true` rend notre pied ;
- **clé commune ⟹ notre pied, toujours** : la condition vit dans `email_send`
  (`_cle_de_l_org`, dérivée du registre : connecteur sans palier `platform`), et
  `send_composed_email` (mailer, clé commune) n'accepte pas de pied d'org ;
- la réponse d'`email_send` dit lequel part : `footer: org | platform`.

⚠️ **Le pied est figé au rendu, pas à l'envoi** : un mail programmé garde le pied en
vigueur quand il a été composé, même si l'org retire son désabonnement entre-temps
(même régime que l'image de tête : la file porte le HTML).

⚠️ **Ce que la décision ne couvre pas, et qui reste à notre marque** : l'en-tête de la
carte (`m.nom`, le nom du produit de l'expéditeur) ; la langue du gabarit (FR par
défaut, `email_send` ne passe aucune locale) ; et aucune liste de suppression n'est
tenue côté org — le désabonnement déclaré est honoré par l'org, pas par nous.

Bancs : `tests/test_email_pied_org.py` (les trois cas, sur PostgreSQL réel pour la
déclaration et la route, éprouvés en chute).
