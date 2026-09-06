"""Le MODÈLE d'un connecteur — la forme, jamais le contenu.

`CredentialField`, `Connector` et la factory `_c` vivent ici ; les ~90 entrées
qui les instancient vivent chacune dans `providers/<nom>.py`, et
`providers/__init__.py` les AGRÈGE. Séparer les deux évite le cycle d'import
qu'aurait un modèle défini dans l'agrégateur et importé par les déclarations.

Module PUR : aucun import `oto_mcp` au niveau module (les propriétés qui ont
besoin d'une donnée curée — `doc_sections`, `category`, `description`,
`publisher_name`, `logo_url_for` — l'importent PARESSEUSEMENT depuis
l'agrégateur, jamais à l'import).

Chaque connecteur porte les 3 axes du modèle plateforme :
- **A. Disponibilité** : `availability` (self_serve | platform_granted). platform_granted
  = grant-only (la plateforme accorde explicitement, ex. un connecteur réservé à un client).
- **B. Visibilité** : `default_active` (SOCLE curé, ADR 0050 — installé d'office
  dans la sélection d'un nouveau (sub, org) ; le reste du catalogue = library
  installable). Policy, tunable.
- **C. Credential** : `auth_modes` ⊆ {byo_user, byo_org, platform} ; `keyed` (résolu via
  `resolve_api_key` avec une clé api) ; `secret_kind` ; `personal_session` (session
  physiologiquement per-user : linkedin/google/slack/whatsapp/crunchbase, jamais org).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialField:
    """Un champ de saisie d'un credential (modèle générique multi-champs, ADR 0011).

    SOURCE UNIQUE du formulaire de saisie (dashboard), de l'endpoint REST et du
    packing au coffre.

    `secret` porte à lui seul la règle de sortie, et il n'y a **plus d'exception** :
    `secret=False` (une URL de base, une région, un email) est rendu tel quel en
    lecture ; `secret=True` n'est **JAMAIS** rendu — ni en clair, ni tronqué, ni vidé :
    sa clé est absente du corps, et une empreinte non inversible le nomme à côté.

    ⚠️ **Un cran `reveal` a existé jusqu'au 2026-08-31** (oto-backend#671) et il était
    le DÉFAUT : tout connecteur `secret_kind="api_key"` sans `credential_fields`
    explicites héritait d'un `key` révélable, soit 55 connecteurs dont 49 ne l'avaient
    jamais décidé. Il est **retiré**, pas neutralisé : le passer à un `CredentialField`
    lève désormais un `TypeError` à l'import, plutôt que d'ouvrir en silence une sortie
    de secret. Une déclaration qui aurait besoin de le rendre ne l'a pas — c'est ce que
    la décision a tranché."""
    name: str
    label: str
    secret: bool = True
    help: str = ""
    # False = champ facultatif (connecteur « ET/OU » type slack : au moins un
    # champ non vide exigé à la pose, mais aucun champ individuellement requis).
    required: bool = True
    # False (défaut) = les whitespace n'ont aucun sens dans la valeur (clés, tokens,
    # ids) → nettoyés à la pose (parasites d'un copier-coller). True = l'espace est
    # significatif (mot de passe) → strip des bords seul. Cf.
    # credentials_store.clean_field_value.
    whitespace_significant: bool = False
    # Valeurs PERTINENTES du champ discriminant du connecteur (`field_discriminator`)
    # pour ce champ-ci. Vide (défaut) = le champ vaut quel que soit le discriminant —
    # donc RIEN NE CHANGE pour les ~90 connecteurs qui n'en déclarent pas.
    # Renseigné, il dit deux choses d'un coup : le champ ne s'AFFICHE que pour ces
    # valeurs, et son `required` ne s'APPLIQUE que là. Ex. `http` : `header_name` est
    # requis, mais seulement en `auth_mode=header`. Cf. `Connector.fields_for`.
    when: tuple[str, ...] = ()
    # Jeu FERMÉ de valeurs acceptées (un `select`, pas un champ libre). Vide = libre.
    # À réserver aux champs qui discriminent ou dont une faute de frappe casse
    # silencieusement : une valeur hors liste est refusée à l'ÉCRITURE, avec le jeu
    # attendu dans le message.
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Connector:
    name: str                          # identité = clé de credential
    namespaces: tuple[str, ...]        # préfixes de tools possédés
    availability: str                  # "self_serve" | "platform_granted"
    auth_modes: frozenset              # ⊆ {"byo_user","byo_org","platform"}
    keyed: bool                        # résolu via resolve_api_key (→ KEY_PROVIDERS)
    personal_session: bool             # catégorie « session navigateur » (Live View
                                       # Browserbase) côté UI — ORTHOGONAL au partage :
                                       # le niveau (user/équipe/org) suit `auth_modes`
                                       # (`byo_org` ⇒ session partageable, ex. pennylaneged)
    secret_kind: str                   # api_key|refresh_token|oauth|cookie|none
    default_quota: int                 # 0 = illimité
    default_active: bool               # axe B : socle curé (ADR 0050) — installé
                                       # d'office au seed d'un nouveau (sub, org) ;
                                       # le reste reste installable depuis la library
    platform_key_open: bool = False    # free-tier : clé plateforme utilisable SANS grant
                                       # (quota gratuit = default_quota par user/jour, ADR 0031)
    label: str = ""
    help: str = ""
    href: str | None = None
    # Éditeur du connecteur (affiché au catalogue). Vide → dérivé de la constante
    # `PUBLISHER` du module de déclaration (cf. `publisher_name`). Vide des DEUX
    # côtés ⟹ la fiche n'affiche AUCUN éditeur : il n'y a plus de défaut (2026-09-02).
    # Le champ sert quand l'éditeur est une propriété PARTAGÉE par plusieurs entrées
    # construites par une même factory (les six canaux hébergés, cf. `unipile.channel`) ;
    # sinon, le domicile normal est la constante du module.
    publisher: str = ""
    # URL publique du logo de l'éditeur. None → dérivée du CDN logo.dev à partir
    # du domaine de marque curé `LOGO_DOMAIN` du module de déclaration
    # (cf. `logo_url_for`).
    # Le champ explicite reste un override (logo custom hébergé ailleurs).
    logo_url: str | None = None
    # "tools" = module in-process (tools/<name>.py) ; "remote" = bridge distant
    # (ADR 0003) servi par le module générique tools/remote.py — le credential
    # d'org est alors {secret=token M2M, meta.base_url=endpoint du bridge} ;
    # "mount" = MCP distant fédéré (otomata#16) monté via FastMCP proxy par le
    # module générique tools/mount.py — credential per-user (token OAuth) injecté
    # par requête, endpoint = `mount_url`.
    # "credential" = ne fournit AUCUN outil : l'objet ne sert qu'à porter une clé
    # que la plateforme utilise pour le compte de l'org (ex. la clé de modèle
    # qu'un agent programmé consomme). Tranché par Alexis le 03/09 : un type
    # DISTINCT, pas un connecteur à namespaces vides.
    #
    # ⚠️ La raison n'est pas technique — un connecteur sans namespace se construit
    # très bien. C'est qu'il **se présenterait comme un connecteur sans en avoir
    # les effets** : il apparaîtrait dans la liste, se « connecterait », et son
    # activation n'apporterait aucune capacité. Un objet qui a l'air de faire
    # quelque chose et ne fait rien est la famille de défauts qu'on documente
    # depuis le 03/09 — un champ lu sans producteur, un champ produit que rien ne
    # lit, une alerte qui ne se déclenche jamais. Ici c'est la version que
    # l'UTILISATEUR voit.
    #
    # Un type distinct laisse l'écran le présenter pour ce qu'il est — « clé de
    # fournisseur », pas « connecteur » — au lieu de mentir par omission.
    kind: str = "tools"
    # Endpoint MCP du serveur distant à monter (kind="mount" uniquement).
    mount_url: str | None = None
    # Préfixe à retirer du NOM des tools distants avant le préfixe de namespace
    # (kind="mount"). Évite la redondance quand le MCP distant préfixe déjà ses
    # tools d'un mot proche du namespace oto — ex. folkmcp : distant `folk_*`
    # monté `folkmcp_*` (strip="folk_") au lieu de `folkmcp_folk_*`. Le forward
    # vers le distant garde le nom d'origine (ProxyTool). None = pas de strip.
    mount_strip_prefix: str | None = None
    # Schéma de saisie EXPLICITE du credential (modèle générique multi-champs).
    # Vide → dérivé du secret_kind (cf. `secret_fields`). Renseigné pour les
    # credentials à >1 champ qui ne sont ni api_key ni basic_auth (ex. Silae :
    # client_id + client_secret + subscription_key).
    credential_fields: tuple[CredentialField, ...] = ()
    # Modules `tools/<m>.py` à importer pour ce connecteur (kind="tools" seulement).
    # Vide ⇒ `(name,)`. Renseigné quand le module ≠ nom du provider (sirene→fr) ou
    # qu'un provider porte plusieurs modules (google→gmail/datastore/tasks).
    # `register_all` DÉRIVE le chargement de ce champ (fin de la liste hardcodée, #24).
    modules: tuple[str, ...] = ()
    # Auth « hébergée » (ADR 0024) : le credential est une clé (resolve_api_key,
    # cascade inchangée), MAIS la connexion user-facing passe par un flux hébergé
    # tiers (ex. unipile : l'org pose l'abonnement, chaque membre lie son compte
    # LinkedIn/WhatsApp par hosted-auth) — pas un formulaire de clé. Posé ici, le
    # descripteur `auth.method` vaut "hosted" → la carte rend le widget dédié sans
    # cas par nom côté front.
    hosted_auth: bool = False
    # Instance PERSONNELLE cross-org (issue #172, ADR 0033 amendé) : le credential
    # est intrinsèquement PAR-PERSONNE — un compte de messagerie hébergé (unipile :
    # le login LinkedIn/WhatsApp EST l'humain, pas l'appartenance). Sa clé membre
    # posée dans UNE org suit alors le `sub` dans TOUTES ses orgs (résolution de
    # proximité, pas seulement pin `_instance=`) : « même email = instance dispo dans
    # chaque org ». Sans ce flag, un credential membre reste strictement `(sub, org)`
    # (ADR 0033) — la valeur par défaut ne change rien pour les ~autres connecteurs.
    personal_cross_org: bool = False
    # LA CARDINALITÉ D'AUTH, déclarée — `"mono"` | `"multi"` | `""` (dérivée du
    # descripteur d'auth, le cas normal). Tranché par Alexis le 2026-08-27 : « ce qui
    # gêne, c'est la liste ELLE-MÊME » — la cardinalité se dérive par défaut et se
    # déclare **par connecteur, dans son entrée**, jamais dans une liste transverse
    # indexée par nom (c'est ce qui a emporté `MULTI_ACCOUNT_PROVIDERS`).
    #
    # À ne poser que pour une raison de FOURNISSEUR, motivée à côté de la déclaration
    # — jamais pour la forme du credential : le nombre de champs ne dit rien de la
    # cardinalité (un token Slack est émis par installation, deux champs ou pas), et
    # c'était précisément le trou de règle d'oto-backend#409.
    #
    # ⚠️ **Le défaut du CODE, pas le dernier mot** : une ligne de `connector_settings`
    # peut le surcharger par org (le patron du bloc d'instructions — constante =
    # défaut, ligne DB = surcharge), pour qu'un élargissement ne demande pas un
    # déploiement. Le seam qui tranche les deux est `connectors.cardinality`, pas
    # cette propriété.
    cardinality: str = ""
    # Le connecteur ANNONCE-T-IL l'axe `_account=` dans le schéma de ses tools, sans
    # attendre que l'appelant détienne deux comptes ? Rien à voir avec la cardinalité,
    # et c'est pourquoi ça n'est plus le même champ : chaque propriété d'axe est
    # recopiée dans le schéma de CHAQUE tool à chaque handshake (test_call_axes_budget),
    # donc l'annonce statique est CURÉE — elle est réservée aux connecteurs dont
    # l'appelant a, en pratique, toujours plusieurs comptes. Ailleurs l'axe reste
    # annoncé DYNAMIQUEMENT (dès 2 comptes détenus) et ACCEPTÉ partout où il a un sens.
    account_axis_static: bool = False
    # Mot métier d'un compte chez CE fournisseur, quand « compte » sonne faux : un
    # compte Slack du coffre est un workspace, un compte Zoho une organisation.
    # Vide = « compte ». Publié dans le descripteur `auth` et affiché tel quel.
    account_noun: str = ""
    # Nom du champ dont la VALEUR sélectionne les autres (`auth_mode` chez `http`).
    # Vide (défaut) = credential à schéma plat : tous les champs valent toujours.
    # Renseigné, il active `fields_for` : un formulaire qui ne montre que ce qui
    # sert, et une validation à l'écriture qui refuse un mode incohérent au lieu de
    # l'accepter et d'échouer au premier appel réel (oto-backend#449).
    field_discriminator: str = ""
    # DÉLÉGATION DE CREDENTIAL (split unipile, oto-backend — 2026-08-28). Ce
    # connecteur n'a PAS de credential propre : sa clé, son quota, sa clé plateforme
    # et son option (couche 3) vivent sous le connecteur NOMMÉ ici. Le cas d'usage
    # est un fournisseur dont UNE clé ouvre N *connexions* qu'on veut gouverner
    # séparément — unipile : une clé d'abonnement, six canaux, chacun sa carte, son
    # activation, son ACL et sa sélection.
    #
    # ⚠️ Deux questions cohabitent alors, et CHAQUE site doit choisir laquelle il pose :
    #   · « quel connecteur est APPELÉ ? »  → les gates : activation, ACL
    #     (`require_connector_access`), sélection, visibilité de session, pin
    #     `_instance=`. C'est le nom NU.
    #   · « quel connecteur PORTE la clé ? » → le coffre, la cascade, le quota, la
    #     clé plateforme, l'option. C'est `providers.credential_provider(nom)`.
    # Confondre les deux redonne exactement la divergence de 2026-07-07 (carte « clé
    # d'org » verte + « Bloqué » rouge) : le statut et la résolution répondaient à
    # deux questions différentes en croyant répondre à la même.
    #
    # Un connecteur délégant ne DÉTIENT jamais de credential : il est hors
    # `CREDENTIAL_PROVIDERS`, donc la pose d'une clé sous son nom est refusée et
    # renvoie sur le porteur (sinon deux clés diraient le contraire l'une de l'autre).
    credential_of: str | None = None
    # Canal Unipile connecté par ce connecteur (`LINKEDIN`/`WHATSAPP`/…) quand la
    # carte représente UNE connexion hébergée. None = pas un connecteur de canal.
    # Rend le canal DÉRIVABLE du connecteur : le flux hébergé se déclare alors sans
    # paramètre (`connectors/flow.py`), et le front n'a plus de sélecteur de canal à
    # rendre — la carte EST le canal.
    hosted_channel: str | None = None

    @property
    def org_shareable(self) -> bool:
        """Un secret de ce connecteur peut-il être posé au niveau ORG (ou équipe) ?

        Faux pour un connecteur qui DÉLÈGUE son credential, même s'il hérite
        `byo_org` de son porteur : ce qui se partage, c'est la clé du COMPTE, sur la
        carte du compte. Sans ce faux, les lecteurs qui interrogent le nom NU —
        `org_secret_meta`, les hints « une équipe a la clé » (`access/rbac.py`), le
        levier de partage de la carte — proposeraient de poser ou d'atteindre un
        secret d'org sous `whatsapp`, que la cascade (normalisée vers `unipile`)
        n'irait jamais lire."""
        return "byo_org" in self.auth_modes and not self.credential_of

    @property
    def family(self) -> str:
        """Nature de l'intégration (axe *builder*, ADR 0011) — DÉRIVÉE du credential
        + runtime : open-data | api | browser | google | federated | bridge."""
        if self.kind == "remote":
            return "bridge"
        if self.kind == "mount":
            return "federated"
        if self.name in BROWSER_PROVIDERS:
            return "browser"
        if self.name == "google":
            return "google"
        if self.secret_kind == "none":
            return "open-data"
        return "api"

    @property
    def category(self) -> str:
        """Domaine d'usage (axe *utilisateur*, ADR 0011) — CURÉ (constante
        `CATEGORY` du module de déclaration), pour grouper l'UI."""
        from . import _CATEGORY_BY_CONNECTOR
        return _CATEGORY_BY_CONNECTOR.get(self.name, "Autres")

    @property
    def doc_sections(self) -> tuple:
        """Sections de doc « how-to » (CURÉ) — un markdown par connecteur,
        `connectors/docs/<nom>.md`, joint par NOM. `connectors/docs_reader.py` n'en porte
        que le parseur et la résolution des marqueurs, plus aucune prose. Lazy
        import : garde ce module pur au niveau module.

        ⚠️ Cette ligne a dit « contenu dans `connector_docs.py` » (le module de l'époque) jusqu'au
        27/08/2026 : vrai à l'écriture, faux depuis la migration du 02/08 (la
        prose est passée du dict Python aux markdown), et recopiée telle quelle
        au découpage du registre du 27/08. Un audit du 27/08 en a conclu qu'il
        restait 153 lignes de prose curée à ventiler dans `providers/<nom>.py` —
        le déplacement était fait depuis trois semaines. Une carte périmée coûte
        plus qu'une carte absente : elle est lue avec confiance."""
        from ..connectors.docs_reader import DOC_SECTIONS
        return DOC_SECTIONS.get(self.name, ())

    @property
    def description(self) -> str:
        """Description user-facing 2-3 phrases (CURÉE, constante `DESCRIPTION`
        du module de déclaration).
        Vide si non rédigée — le front retombe alors sur `help`."""
        from . import _DESCRIPTION_BY_CONNECTOR
        return _DESCRIPTION_BY_CONNECTOR.get(self.name, "")

    @property
    def publisher_name(self) -> str:
        """Éditeur affiché au catalogue — override champ si renseigné, sinon la
        constante curée `PUBLISHER` du module de déclaration. **Sinon RIEN.**

        ⚠️ Ce repli valait « Otomata » jusqu'au 2026-09-02 : une omission de
        déclaration était alors indiscernable du choix « connecteur maison », et le
        défaut nous attribuait le produit d'un tiers — le MCP *officiel* de Folk a
        été servi sous notre nom pendant un mois, et six canaux de messagerie
        envoyaient des messages via une passerelle tierce sous notre nom.

        **Une absence se voit et se corrige ; une attribution fausse se croit.**

        Le vide servi est la chaîne vide, **pas `None`**. Mesuré le 2026-09-02 en
        rendant les composants du dashboard : les cinq surfaces qui affichent
        l'éditeur traitent `""` et `null` à l'IDENTIQUE — elles masquent
        (`v-if`, `filter(Boolean)`) ou coercent (`(c.publisher || "")`,
        `[…].join(" ")`), aucune ne rend « undefined » ni ne casse. Le départage
        est donc le CONTRAT, pas le rendu : oto-dashboard déclare
        `publisher: string` (`types/api.ts`) et la capacité `MyConnectors` sert la
        clé sur toutes ses lignes verbeuses ; `None` ferait mentir le premier sans
        rien gagner sur le second.

        Le ratchet `tests/test_connector_publisher.py` fait que ce vide ne DOIT
        jamais être atteint depuis le registre : il exige une déclaration par
        connecteur, tout le registre, sans filtre de famille. Le vide reste le
        comportement d'une entrée construite AILLEURS — c'est là qu'il protège."""
        if self.publisher:
            return self.publisher
        from . import _PUBLISHER_BY_CONNECTOR
        return _PUBLISHER_BY_CONNECTOR.get(self.name, "")

    def logo_url_for(self) -> str | None:
        """URL publique du logo de l'éditeur. Override `logo_url` si présent,
        sinon dérivée du CDN **logo.dev** : domaine de marque curé
        (`LOGO_DOMAIN` du module de déclaration) + token publishable
        `LOGODEV_TOKEN` (env).
        None si pas de domaine connu (open-data/maison → monogramme côté UI) ou
        token absent. Le token est *publishable* (conçu pour vivre dans l'URL)."""
        if self.logo_url:
            return self.logo_url
        from . import _LOGO_DOMAIN_BY_CONNECTOR
        domain = _LOGO_DOMAIN_BY_CONNECTOR.get(self.name)
        token = os.environ.get("LOGODEV_TOKEN")
        if not domain or not token:
            return None
        return (f"https://img.logo.dev/{domain}"
                f"?token={token}&size=256&format=png&retina=true")

    @property
    def auth_method(self) -> str:
        """Mécanisme d'obtention du credential (ADR 0024) — DÉRIVÉ. Pilote le
        widget rendu par la `ConnectorCard` (un flux, une carte). Priorité :
        `hosted` (flux hébergé tiers, ex. unipile) > `remote` (bridge ADR 0003,
        posé par grant d'org) > `oauth`/`cookie`/`none` (flux dédiés / pas de
        credential) > `secret` (champ(s) à coller : api_key, basic_auth, fields).
        ⚠️ Ce jeu de valeurs est FERMÉ *et consommé par un `switch` dans un AUTRE
        repo* (oto-dashboard, `ConnectorConnectionPanel.connKind`) : y ajouter une
        valeur est une rupture de contrat cross-repo qui échoue en SILENCE (branche
        `default` → panneau de connexion vide). Vécu avec `secret_then_oauth`,
        retiré le 29/07 : « il reste une étape » se dit par `status_hints`
        (pending_action), pas par une nouvelle méthode d'auth. NB : un MCP fédéré
        (kind=mount) hérite de son `secret_kind`
        (planity=basic_auth→secret, atlassian=oauth→oauth)."""
        if self.hosted_auth:
            return "hosted"
        if self.kind == "remote" and not self.credential_fields:
            # Bridge legacy (ADR 0003) : credential posé par grant d'org, pas de
            # formulaire. Un bridge NOUVEAU modèle (ADR 0034) déclare ses
            # credential_fields → formulaire self-serve standard (method=secret).
            return "remote"
        if self.secret_kind in ("oauth", "cookie", "none"):
            return self.secret_kind
        return "secret"

    @property
    def auth_multi_account(self) -> bool:
        """Le credential est-il multi-compte — N grants pour une même entité
        (ADR 0024) ?

        ⚠️ **C'est le défaut du CODE, pas la réponse servie.** Une ligne de
        `connector_settings` peut le surcharger par org : l'appelant qui doit TRANCHER
        passe par `connectors.cardinality.is_multi_account(connector, org)`, jamais par
        cette propriété. Elle reste ici parce que le registre est PUR (aucun import
        `oto_mcp`, aucune base) — et c'est ce qui la rend lisible d'un test.

        Par DÉFAUT pour tout connecteur dont le credential se POSE (`method=secret`
        — clé simple `api_key`/`basic_auth` **ou** multi-champs `fields`) : le coffre
        est déjà segmenté par `account` sur chaque ligne, la résolution membre
        (access/resolve.py `_member_fetch`) traite un compte unique — la ligne legacy
        `account=''` comprise — exactement comme avant. Une clé posée hier reste
        donc la clé d'aujourd'hui ; ce qui change est qu'on peut en poser une
        deuxième, nommée.

        **Une déclaration `cardinality` PRIME sur la dérivation** — les deux seuls
        porteurs sont ceux dont le descripteur d'auth dit faux : `google` (OAuth, donc
        dérivé mono, mais N consentements = N comptes) et `browser` (session cookie,
        donc dérivé mono, mais un compte = un SITE). `zoho` et `folk`, longtemps dans
        la liste transverse, n'ont rien à déclarer : la dérivation les rend multi toute
        seule. C'est la mesure qui l'a montré, pas une intuition.

        ⚠️ **Le nombre de champs du credential ne dit RIEN de la cardinalité.**
        Jusqu'au 2026-08-27, `fields` était hors règle : Slack (`bot_token` +
        `user_token`) en tombait mono-compte alors qu'un token Slack est émis par
        INSTALLATION dans un workspace (N installations = N tokens indépendants) —
        un trou de règle, pas une raison de fournisseur. Poser un 2ᵉ compte y
        écrivait une ligne que la résolution n'allait jamais lire (oto-backend#409).

        Hors périmètre, volontairement : OAuth/cookie/none (N comptes = N
        consentements, un autre problème), hosted/remote (pas de clé à poser), et
        les connecteurs `personal_cross_org` (unipile), dont le barreau
        cross-org de la cascade est mono-compte par construction. Un cas qui
        n'entre dans aucune de ces familles se déclare `cardinality="mono"`, DANS
        son entrée de registre et avec son motif — jamais par une liste transverse."""
        if self.cardinality:
            return self.cardinality == "multi"
        return (self.auth_method == "secret"
                and self.secret_kind in ("api_key", "basic_auth", "fields")
                and not self.personal_cross_org)

    @property
    def auth(self) -> dict:
        """Descripteur d'auth unifié (ADR 0024) — source unique du rendu de la
        face credential, quel que soit le mécanisme. `fields` = schéma de saisie
        (vide hors `method=secret`, où les flux sont dédiés)."""
        return {
            "method": self.auth_method,
            "cardinality": "multi_account" if self.auth_multi_account else "single",
            # Le MOT que l'utilisateur emploie pour un compte de ce connecteur, quand
            # « compte » est faux chez lui : un compte Slack du coffre EST un workspace.
            # Le front l'affiche tel quel (oto-dashboard#121) — c'est le registre qui
            # connaît le vocabulaire du fournisseur, pas l'écran.
            "account_noun": self.account_noun or "compte",
            # Le champ dont la valeur sélectionne les autres, quand il y en a un
            # (`auth_mode` chez `http`) : le front n'a pas à connaître le connecteur
            # pour n'afficher que les champs qui servent — il lit `when` (#449).
            "field_discriminator": self.field_discriminator,
            # Le CANAL hébergé de ce connecteur, quand il en porte un (split unipile
            # du 2026-08-28). Sans lui, un écran qui rend la face « connecter un
            # compte » ne peut pas savoir LEQUEL des six canaux sa carte représente —
            # et la seule issue est une liste des six recopiée dans le front, qui
            # s'affiche identique sur les six cartes. C'est exactement la panne
            # constatée : la carte WhatsApp proposait de connecter LinkedIn,
            # Telegram, Instagram… et affichait leur état de connexion comme le sien.
            # Minuscule, comme le paramètre du flux — le coffre, lui, majuscule.
            "hosted_channel": (self.hosted_channel or "").lower() or None,
            # Le connecteur qui DÉTIENT le credential, quand ce n'est pas celui-ci
            # (`credential_of`). Un connecteur qui délègue n'a AUCUN champ à saisir
            # (`secret_fields` est vide par construction, cf. plus bas) : un écran qui
            # lui propose quand même « pose ta propre clé » propose un geste sans
            # effet, la clé qu'il poserait n'étant lue par personne. Le nom du porteur
            # plutôt qu'un booléen : c'est LUI que l'écran doit nommer pour dire où le
            # geste se fait vraiment.
            "credential_of": self.credential_of,
            "fields": [
                {"name": f.name, "label": f.label, "secret": f.secret,
                 "required": f.required, "help": f.help,
                 "when": list(f.when), "choices": list(f.choices)}
                for f in self.secret_fields
            ],
        }

    def fields_for(self, values: dict) -> tuple[CredentialField, ...]:
        """Les champs PERTINENTS d'une saisie, le discriminant une fois connu.

        Sans `field_discriminator`, c'est `secret_fields` — le cas des ~90 autres
        connecteurs. Avec, on retire les champs qu'aucune valeur du discriminant ne
        rend utiles : `header_name` n'a rien à faire dans un formulaire `bearer`,
        et son `required` n'a rien à y refuser.

        ⚠️ Discriminant ABSENT ou vide = tout est pertinent. C'est volontaire : à ce
        stade la saisie n'a pas encore choisi, et masquer serait deviner. Le refus
        d'une valeur hors `choices` est le travail de l'écriture, pas d'ici."""
        if not self.field_discriminator:
            return self.secret_fields
        picked = str(values.get(self.field_discriminator) or "").strip().lower()
        if not picked:
            return self.secret_fields
        return tuple(f for f in self.secret_fields if not f.when or picked in f.when)

    @property
    def secret_fields(self) -> tuple[CredentialField, ...]:
        """Schéma de saisie du credential — SOURCE UNIQUE pour l'UI, l'endpoint REST,
        `status_for` et le packing. Déclaré explicitement (`credential_fields`),
        sinon dérivé des formes simples. Vide = pas de saisie générique : `cookie`
        (linkedin/crunchbase), `oauth` (google/atlassian) et `none` (open-data) ont
        des flux dédiés, pas un formulaire de champs.

        Vide aussi pour un connecteur qui DÉLÈGUE son credential (`credential_of`) :
        il n'a pas de champ à lui. Rendre le formulaire de son porteur sur sa carte
        inviterait à poser une deuxième clé — que le coffre refuse (hors
        `CREDENTIAL_PROVIDERS`) et que la cascade n'irait jamais lire. La carte du
        porteur est le seul endroit où la clé se pose."""
        if self.credential_of:
            return ()
        if self.credential_fields:
            return self.credential_fields
        if self.secret_kind == "api_key":
            return (CredentialField("key", "API key", secret=True),)
        if self.secret_kind == "basic_auth":
            return (CredentialField("email", "Email", secret=False),
                    CredentialField("password", "Mot de passe", secret=True,
                                    whitespace_significant=True))
        return ()

    @property
    def config_fields(self) -> tuple[CredentialField, ...]:
        """Champs NON-secrets du credential (endpoint/host/region : `base_url`
        n8n/make, `data_center` zoho, `org_id` zohodesk…). Dérivés de `secret_fields`
        (flag `secret=False`) — la config voyage avec la clé via `resolve_credential`
        (le `meta` non-secret, ex. `dsn` unipile, s'y ajoute à la résolution)."""
        return tuple(f for f in self.secret_fields if not f.secret)


# Connecteurs passant par un browser IN-PROCESS (o-browser local) — non dérivable
# du seul secret_kind. Vide depuis la migration de crunchbase sur le substrat
# HÉBERGÉ Browserbase (ADR 0026) : crunchbase appelle désormais l'API privée
# `/v4/data` via une session navigateur distante (family dérivée → "api", comme
# brevo). LinkedIn était déjà parti vers Unipile. Mécanisme conservé pour un
# éventuel futur connecteur browser local.
BROWSER_PROVIDERS = frozenset()

# ⚠️ **`MULTI_ACCOUNT_PROVIDERS` A ÉTÉ RETIRÉE le 2026-08-29** (lot L6 pièce 2 c),
# et sa disparition EST le lot. C'était la dernière liste transverse indexée par nom,
# et Alexis l'a tranché le 27/08 : « ce qui gêne, c'est la LISTE elle-même ». Elle
# portait deux choses sans rapport, qui vivent désormais chacune dans l'entrée du
# connecteur concerné, à côté de la déclaration qu'elle qualifie :
#
# · la CARDINALITÉ → `cardinality="multi"` — et seulement là où la dérivation dit
#   faux : `google` (OAuth ⟹ dérivé mono, mais N consentements = N comptes) et
#   `browser` (cookie ⟹ dérivé mono, mais un compte = un SITE). `zoho` et `folk` n'ont
#   RIEN à déclarer : depuis que la règle couvre les credentials multi-champs, la
#   dérivation les rend multi toute seule — ils n'étaient plus dans la liste que par
#   habitude, et c'est la mesure qui l'a montré ;
# · l'annonce STATIQUE de l'axe `_account=` → `account_axis_static=True`, sur les
#   quatre. Ce rôle-là n'a jamais été la cardinalité (il parle du SCHÉMA des tools, pas
#   du coffre) — les confondre dans une seule liste est ce qui la rendait indéboulonnable.


def _c(name, namespaces, *, availability="self_serve", auth_modes=(), keyed=False,
       personal_session=False, secret_kind="none",
       default_quota=0, default_active=False,
       platform_key_open=False, label="", help="", href=None,
       publisher="", logo_url=None, kind="tools", mount_url=None,
       mount_strip_prefix=None,
       credential_fields=(), modules=(), hosted_auth=False,
       personal_cross_org=False, cardinality="", account_axis_static=False,
       account_noun="",
       field_discriminator="", credential_of=None, hosted_channel=None) -> Connector:
    """Factory d'une entrée de registre — appelée par `providers/<nom>.py`.

    Ajouter un CHAMP par connecteur = l'ajouter à `Connector` ET ici, puis le
    renseigner dans le module du connecteur concerné, à côté de la déclaration
    qu'il qualifie (jamais dans une liste transverse indexée par nom)."""
    return Connector(
        name=name, namespaces=tuple(namespaces), availability=availability,
        auth_modes=frozenset(auth_modes), keyed=keyed, personal_session=personal_session,
        secret_kind=secret_kind, default_quota=default_quota,
        default_active=default_active, platform_key_open=platform_key_open,
        label=label or name.capitalize(), help=help, href=href,
        publisher=publisher, logo_url=logo_url, kind=kind,
        mount_url=mount_url, mount_strip_prefix=mount_strip_prefix,
        credential_fields=tuple(credential_fields),
        modules=tuple(modules), hosted_auth=hosted_auth,
        personal_cross_org=personal_cross_org, cardinality=cardinality,
        account_axis_static=account_axis_static,
        account_noun=account_noun, field_discriminator=field_discriminator,
        credential_of=credential_of, hosted_channel=hosted_channel,
    )
