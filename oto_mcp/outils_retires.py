"""Outils MCP RETIRÉS — le nom ne répond plus : il REFUSE en nommant le geste qui aboutit.

Un retrait n'est pas un renommage (`deprecations.TOOLS`) : il n'y a pas de nom
d'aujourd'hui vers lequel traduire l'appel, donc rien à servir « à côté ». Ce n'est pas
non plus un nom simplement inconnu : `error_taxonomy._surviving_siblings` DÉRIVE la
fratrie d'un nom disparu, ce qui suffit quand ses verbes ont été rangés sous un voisin
(`gmail_search` → `gmail_message`, aujourd'hui `gmail`). Pour un retrait, la fratrie serait les ~60 outils
`oto_*` — une liste qui n'apprend rien — alors que le geste qui aboutit est connu et
précis. Il s'écrit donc, une fois, ICI, et trois surfaces le lisent :

- `error_taxonomy.classify` — un `tools/call` direct sur le nom (fastmcp : « Unknown
  tool ») ;
- `tools/meta.oto_call` — AVANT le refus méta/spine : un nom `oto_*` y recevait « appelle-le
  directement », qui renvoie au chemin précédent. Une boucle ;
- `tools/meta.oto_tool_schema` — ce que l'agent lit avant d'appeler.

⚠️ **Le refus doit être AUTOSUFFISANT.** Il est lu par des agents qu'une procédure ou une
page d'org — contenu client, qu'on ne corrige pas — envoie encore vers ce nom : aucun
renvoi à un guide, aucune hypothèse sur ce que l'agent a lu avant.

Un retrait peut aussi REMPLACER un nom sans le servir à côté (`RENOMMAGES`, depuis le 17/10/2026) :
quand l'ancien nom est lui-même la faille, il n'y a pas d'alias à tenir pendant un préavis —
il refuse tout de suite, en nommant le nouvel appel.

Un connecteur qui passe au LECTEUR GÉNÉRIQUE de sa description (`tools/decrits.py`, règle de
convergence, 11/11/2026 pour DataForSEO) change aussi de noms ce jour-là : ses outils écrits à
la main deviennent `<ns>` / `<ns>_act`, dont l'`op` est la fonction décrite
(`RENOMMAGES_DECRITS`). Même refus nommé, sans le récit de sécurité des lots lire/agir, et
la migration des listes enregistrées lit cette table-là.

Module PUR (aucun import `oto_mcp`) : la taxonomie d'erreurs doit rester importable seule.
"""
from __future__ import annotations

import datetime
from typing import NamedTuple, Optional


class Retrait(NamedTuple):
    #: Ce que l'agent lit : ce qui a disparu, pourquoi rien n'est perdu, et le geste.
    message: str
    #: Le `hint` de l'enveloppe d'erreur (`data.oto.hint`) : le geste seul.
    hint: str


#: `oto_kb` — retiré le 10/09/2026 (décision d'Alexis) : la « base de connaissance » n'existe
#: plus comme concept, il n'y a que projet, doc et procédure. La DONNÉE ne bouge pas : c'était
#: déjà un projet ordinaire. La route REST `POST /api/me/kb` reste, le tableau de bord la
#: consomme (cf. `capabilities/kb.py`).
KB = Retrait(
    message=(
        "`oto_kb` is retired: there is no org \"knowledge base\" any more — oto has "
        "projects, docs and procedures, nothing else. What was called the knowledge base "
        "is an ordinary PROJECT of the org; nothing was moved or deleted, its pages are all "
        "still there. Reach them like any project's pages: find the project with "
        "`oto_project op=list` (seeded as \"Knowledge base\", or \"Base de connaissance\" in "
        "older orgs, and possibly renamed since), then read or write its pages with "
        "`oto_doc` and that `project_id` (`op=search`, `op=get`, `op=create`, "
        "`op=update`). Durable knowledge you capture goes, as a doc, into the project it "
        "belongs to. If a procedure or a page told you to call `oto_kb`, it predates this "
        "retirement: take the path above instead — this name only returns this refusal, "
        "called directly or through `oto_call`."),
    hint=("find the project with oto_project op=list, then read or write its pages with "
          "oto_doc and its project_id"),
)

# --- 17/10/2026 : lire ou simuler (`ns`), agir (`ns_act`) --------------------------------
# Décision d'Alexis, pour la sécurité : un client MCP autorise des OUTILS, pas des
# arguments. `gmail_compose(mode="send")` envoyait sous l'autorisation du brouillon,
# `gmail_message(op="trash")` vidait une boîte sous celle de la lecture. Chaque connecteur
# sert désormais DEUX outils : `<ns>` lit ou simule (un brouillon est une simulation),
# `<ns>_act` engage ou détruit (envoi, corbeille, archivage, déplacement) — c'est lui que
# le client fait confirmer. Coupure IMMÉDIATE, sans transition (exception à l'Art 8.2,
# `docs/alias-deprecies.md`).
#
# 21/10/2026, même décision pour TOUTES les messageries : un agent a envoyé un WhatsApp
# sans validation parce que `whatsapp_chat` (le lecteur, en « toujours autoriser » dans
# un projet) servait encore son ancienne `op="send"`. Les lecteurs de messagerie et leurs
# outils d'envoi deviennent des paires — `whatsapp`/`whatsapp_act`, `telegram`/
# `telegram_act`, `instagram`/`instagram_act`, `linkedin_unipile`/`linkedin_unipile_act`
# (la messagerie LinkedIn), `teams`/`teams_act`, `chat`/`chat_act` (Google Chat) — et
# leurs anciennes ops d'envoi en transition (oto-backend#1189)
# disparaissent avec les anciens noms.
#
# **Une table de correspondance par lot**, et `RENOMMAGES` les réunit : le refus nommé de
# chaque ancien nom et l'annonce servie aux agents (`annonce_act`) dérivent de
# `RENOMMAGES` ; la migration des listes d'outils enregistrées lit la table de SON lot —
# `RENOMMAGES_ACT` pour la révision 0062 (déjà jouée : cette table est FIGÉE),
# `RENOMMAGES_MESSAGERIES` pour `20261021_1219_outils_act_msg`. Une ligne ajoutée est
# annoncée, refusée et migrée ; rien ne se recopie ailleurs.
#
# **Le sens d'une liste réécrite** (décision du 21/10/2026, à partir du lot des
# messageries) : une liste qui AUTORISE ne reçoit, pour un ancien outil MIXTE (qui lisait
# et, en transition, envoyait encore), que le nouveau lecteur — jamais `<ns>_act` ; seul
# un ancien outil d'envoi DÉDIÉ donne `<ns>_act`, et seulement si `<ns>_act` ne fait rien
# de plus que lui (`remplacants_autorises`) : `whatsapp_send` → `whatsapp_act` (send, et
# rien d'autre) ; `linkedin_unipile_send`, `_publish` et `_invite` ne donnent RIEN, car
# `linkedin_unipile_act` publie, invite et envoie — le droit se perd, le refus de l'ancien
# nom dit d'ajouter `linkedin_unipile_act` explicitement. Une liste qui
# MASQUE masque tout ce que l'ancien outil faisait (`remplacants`) : masquer reste large,
# autoriser reste étroit. Le lot Gmail/Outlook (0062), joué avant cette règle, garde la
# sienne : ses deux sens prenaient tous les remplaçants.
#
# 21/10/2026, même jour, les LECTEURS QUI ENVOYAIENT À UN TIERS hors messageries (lot
# « envois ») : LinkedIn (publication, invitations, actes sur un membre), Apollo,
# Lightfield, Luma, Pennylane, SignWell, Productlane, Ahrefs, lemlist. Deux formes :
# - l'outil d'envoi DÉDIÉ disparaît dans `<ns>_act` (`linkedin_unipile_publish`,
#   `linkedin_unipile_invite`, `lemlist_inbox_send`) : un RENOMMAGE comme les autres
#   (`RENOMMAGES_ENVOIS`) ;
# - le lecteur MIXTE reste, sous son nom, et PERD ses ops d'envoi au profit de
#   `<ns>_act` (`apollo_email(op="send")` → `apollo_act(op="send_email")`…) : une OP
#   DÉPLACÉE (`OPS_DEPLACEES`). L'outil restant servi, il ne refuse pas : c'est l'op qui
#   refuse, en nommant son nouvel appel (`op_deplacee`, lu par
#   `error_taxonomy.classify`). Une liste qui l'autorise n'a rien à réécrire — elle
#   n'autorise plus que ce qu'il fait encore, la lecture ; une liste qui le masque
#   masque aussi `<ns>_act` (`remplacants`).
# Les ops de `<ns>_act` portent leur objet quand le connecteur agit sur plusieurs
# (`react_message` n'est pas `react_post`, `send_email` chez Apollo).
#
# 23/10/2026, les PERSONAS lemlist : `lemlist_database` (la base partagée) sauvait et
# supprimait aussi des personas, donc aucune recette ne pouvait l'appeler. Il ne fait plus
# que lire ; ses deux ops d'écriture passent à `lemlist_act`, l'acteur unique du
# connecteur (`OPS_DEPLACEES_PERSONAS`, que la révision `20261023_0713_lemlist_personas`
# lit). Choix assumé : autoriser `lemlist_inbox_send` donne toujours `lemlist_act`, qui
# porte désormais aussi les personas — aucun agent vivant ne perd l'envoi
# (`ops_en_plus` ne compte pas les ops de ce lot). La révision du lot des envois lit
# `OPS_DEPLACEES_ENVOIS`, pas ce lot : elle garde le sens avec lequel elle a été jouée.


#: Les jours des lots : Gmail et Outlook, puis les messageries et les envois.
LOT_MAILS = datetime.date(2026, 10, 17)
LOT_MESSAGERIES = datetime.date(2026, 10, 21)
LOT_ENVOIS = datetime.date(2026, 10, 21)
LOT_PERSONAS = datetime.date(2026, 10, 23)


class Renommage(NamedTuple):
    ancien: str                 # l'outil retiré (dans `OPS_DEPLACEES` : l'outil qui reste)
    selecteur: Optional[str]    # ce qui choisissait le geste : "op=…", "mode=…", None = tout l'outil
    outil: str                  # le nouvel outil
    op: str                     # son op
    depuis: datetime.date       # le jour du retrait


def _ops(ancien: str, cible: str, ops: tuple, depuis: datetime.date) -> tuple:
    return tuple(Renommage(ancien, f"op={o}", cible, o, depuis) for o in ops)


def _vers(ancien: str, cible: str, gestes: dict, depuis: datetime.date) -> tuple:
    """`gestes` = {ancienne op (ou sélecteur complet) : op de `cible`}."""
    return tuple(Renommage(ancien, a if "=" in a else f"op={a}", cible, o, depuis)
                 for a, o in gestes.items())


#: Le lot du 17/10/2026, que la révision 0062 lit. FIGÉ : 0062 est jouée.
RENOMMAGES_ACT: tuple[Renommage, ...] = (
    Renommage("gmail_list_accounts", None, "gmail", "accounts", LOT_MAILS),
    *_ops("gmail_message", "gmail", ("search", "get", "attachment", "drafts"), LOT_MAILS),
    *_ops("gmail_message", "gmail_act", ("archive", "trash"), LOT_MAILS),
    Renommage("gmail_compose", "mode=draft", "gmail", "draft", LOT_MAILS),
    Renommage("gmail_compose", "mode=send", "gmail_act", "send", LOT_MAILS),
    *_ops("outlook_message", "outlook", ("search", "get", "attachment", "drafts", "folders"),
          LOT_MAILS),
    *_ops("outlook_message", "outlook_act", ("archive", "trash", "move"), LOT_MAILS),
    Renommage("outlook_compose", "mode=draft", "outlook", "draft", LOT_MAILS),
    Renommage("outlook_compose", "mode=send", "outlook_act", "send", LOT_MAILS),
)


def _messagerie_unipile(canal: str) -> tuple:
    """Un canal de messagerie Unipile (`register_messaging_tools`) : `<canal>_chat`
    (op=list/read, et l'ancienne op=send) et `<canal>_send` → `<canal>` / `<canal>_act`."""
    return (*_ops(f"{canal}_chat", canal, ("list", "read"), LOT_MESSAGERIES),
            Renommage(f"{canal}_chat", "op=send", f"{canal}_act", "send", LOT_MESSAGERIES),
            Renommage(f"{canal}_send", None, f"{canal}_act", "send", LOT_MESSAGERIES))


#: Le lot des messageries (21/10/2026), que la révision `20261021_1219_outils_act_msg` lit.
RENOMMAGES_MESSAGERIES: tuple[Renommage, ...] = (
    *_messagerie_unipile("whatsapp"),
    *_messagerie_unipile("telegram"),
    *_messagerie_unipile("instagram"),
    *_ops("linkedin_unipile_chat", "linkedin_unipile", ("list", "read", "attendees",
                                                        "contacts"), LOT_MESSAGERIES),
    *_vers("linkedin_unipile_chat", "linkedin_unipile_act",
           {"send": "send", "update": "update", "react": "react_message"}, LOT_MESSAGERIES),
    *_vers("linkedin_unipile_send", "linkedin_unipile_act",
           {"send": "send", "update": "update", "react": "react_message"}, LOT_MESSAGERIES),
    *_ops("teams_spaces", "teams", ("teams", "channels", "chats"), LOT_MESSAGERIES),
    *_ops("teams_message", "teams", ("list", "replies"), LOT_MESSAGERIES),
    *_ops("teams_message", "teams_act", ("post", "reply"), LOT_MESSAGERIES),
    *_ops("teams_send", "teams_act", ("post", "reply"), LOT_MESSAGERIES),
    Renommage("chat_spaces", None, "chat", "spaces", LOT_MESSAGERIES),
    Renommage("chat_message", "op=list", "chat", "list", LOT_MESSAGERIES),
    Renommage("chat_message", "op=send", "chat_act", "send", LOT_MESSAGERIES),
    Renommage("chat_send", None, "chat_act", "send", LOT_MESSAGERIES),
)

#: Les envois LinkedIn de la publication et du réseau : une seule table pour l'outil
#: dédié et pour le lecteur qui les servait encore en transition.
_PUBLICATION = {"create": "publish_post", "comment": "comment_post", "react": "react_post"}
_INVITATIONS = {"invite": "invite", "op=handle, action=accept": "accept_invitation",
                "op=handle, action=decline": "decline_invitation",
                "cancel": "cancel_invitation"}

#: Le lot des envois (21/10/2026), outils d'envoi DÉDIÉS — que la révision
#: `20261021_1352_outils_act_envois` lit, avec `OPS_DEPLACEES`.
RENOMMAGES_ENVOIS: tuple[Renommage, ...] = (
    *_vers("linkedin_unipile_publish", "linkedin_unipile_act", _PUBLICATION, LOT_ENVOIS),
    *_vers("linkedin_unipile_invite", "linkedin_unipile_act", _INVITATIONS, LOT_ENVOIS),
    *_vers("lemlist_inbox_send", "lemlist_act",
           {"email": "send_email", "linkedin": "send_linkedin",
            "whatsapp": "send_whatsapp"}, LOT_ENVOIS),
)

#: Tous les lots de RETRAITS : ce que le refus et l'annonce lisent.
RENOMMAGES: tuple[Renommage, ...] = (RENOMMAGES_ACT + RENOMMAGES_MESSAGERIES
                                     + RENOMMAGES_ENVOIS)

#: Le lot des personas lemlist (23/10/2026), que la révision
#: `20261023_0713_lemlist_personas` lit : le lecteur de la base partagée reste servi.
OPS_DEPLACEES_PERSONAS: tuple[Renommage, ...] = (
    *_vers("lemlist_database", "lemlist_act",
           {"persona_create": "create_persona", "persona_delete": "delete_persona"},
           LOT_PERSONAS),
)

#: Le lot des envois, lecteurs MIXTES : l'outil reste servi, ces ops passent à
#: `<ns>_act`. `ancien` = l'outil qui reste. La révision `20261021_1352_outils_act_envois`
#: lit cette table-ci.
OPS_DEPLACEES_ENVOIS: tuple[Renommage, ...] = (
    *_vers("linkedin_unipile_post", "linkedin_unipile_act", _PUBLICATION, LOT_ENVOIS),
    *_vers("linkedin_unipile_network", "linkedin_unipile_act", _INVITATIONS, LOT_ENVOIS),
    *_vers("linkedin_unipile_profile", "linkedin_unipile_act",
           {"endorse": "endorse", "action": "update_member"}, LOT_ENVOIS),
    *_vers("apollo_email", "apollo_act", {"send": "send_email"}, LOT_ENVOIS),
    *_vers("lightfield_emails", "lightfield_act", {"send": "send_email"}, LOT_ENVOIS),
    *_vers("luma_blasts", "luma_act", {"send": "send_blast"}, LOT_ENVOIS),
    *_vers("pennylane_invoice", "pennylane_act", {"send": "send_invoice"}, LOT_ENVOIS),
    *_vers("signwell_document", "signwell_act",
           {"send": "send_document", "remind": "remind_document"}, LOT_ENVOIS),
    *_vers("productlane_threads", "productlane_act", {"send": "send_message"},
           LOT_ENVOIS),
    *_vers("productlane_changelogs", "productlane_act",
           {"broadcast": "broadcast_changelog"}, LOT_ENVOIS),
    *_vers("ahrefs_social", "ahrefs_act", {"publish": "publish_post"}, LOT_ENVOIS),
)

#: Tous les lecteurs MIXTES (envois, puis personas) : ce que le refus et l'annonce lisent.
OPS_DEPLACEES: tuple[Renommage, ...] = OPS_DEPLACEES_ENVOIS + OPS_DEPLACEES_PERSONAS

#: L'annonce est servie aux agents (`instructions`, section « Ton contexte oto » : au
#: handshake et dans `oto_context`) jusqu'à ce jour inclus — deux mois après le dernier
#: lot, la durée du préavis qu'elle remplace.
ANNONCE_ACT_JUSQU_AU = datetime.date(2026, 12, 21)


def remplacants(ancien: str) -> tuple[str, ...]:
    """Les outils qui reprennent TOUT ce que faisait `ancien` — le sens d'une liste qui
    MASQUE (et des deux sens au lot 0062) : ses remplaçants s'il est retiré, lui-même et
    l'acteur de ses ops déplacées s'il reste servi. `()` s'il n'a pas changé."""
    renomme = tuple(dict.fromkeys(r.outil for r in RENOMMAGES if r.ancien == ancien))
    if renomme:
        return renomme
    deplace = tuple(dict.fromkeys(r.outil for r in OPS_DEPLACEES if r.ancien == ancien))
    return (ancien, *deplace) if deplace else ()


def ops_de_l_acteur(acteur: str) -> frozenset[str]:
    """Les ops que sert `acteur`, telles que les tables les lui donnent — la garde
    `test_convention_act` vérifie qu'elles sont TOUTES celles de sa signature."""
    return frozenset(r.op for r in RENOMMAGES + OPS_DEPLACEES if r.outil == acteur)


def ops_en_plus(ancien: str, acteur: str) -> tuple[str, ...]:
    """Ce que `acteur` fait de plus que l'ancien outil retiré `ancien`. Les ops du lot
    des personas n'y comptent pas : `lemlist_inbox_send` autorisé garde `lemlist_act`
    (choix du 23/10/2026)."""
    donnees = {r.op for r in RENOMMAGES if r.ancien == ancien and r.outil == acteur}
    hors = {r.op for r in OPS_DEPLACEES_PERSONAS if r.outil == acteur}
    return tuple(sorted(ops_de_l_acteur(acteur) - donnees - hors))


def remplacants_autorises(ancien: str) -> tuple[str, ...]:
    """Ce qu'une liste qui AUTORISE `ancien` autorise désormais — jamais plus que ce
    qu'elle autorisait. Un ancien outil MIXTE (qui avait un lecteur parmi ses
    remplaçants) ne donne que la lecture, jamais `<ns>_act` ; un ancien outil d'envoi
    dédié donne `<ns>_act` seulement si `<ns>_act` ne fait rien de plus que lui, sinon
    RIEN (`linkedin_unipile_publish` : `linkedin_unipile_act` envoie aussi des messages).
    Un lecteur qui reste servi (`OPS_DEPLACEES`) se donne lui-même, et lui seul."""
    tous = remplacants(ancien)
    lecteurs = tuple(o for o in tous if not o.endswith("_act"))
    if lecteurs:
        return lecteurs
    return tuple(a for a in tous if not ops_en_plus(ancien, a))


def _retrait_act(ancien: str) -> Retrait:
    lignes = [r for r in RENOMMAGES if r.ancien == ancien]
    gestes = "; ".join(
        (f"{r.selecteur} → " if r.selecteur else "")
        + f'`{r.outil}(op="{r.op}", …)`' for r in lignes)
    acteur = next((r.outil for r in lignes if r.outil.endswith("_act")),
                  f"{lignes[0].outil}_act")
    lecteur = acteur.removesuffix("_act")
    # Le lecteur de la PAIRE n'est nommé que s'il lit ce que l'ancien outil lisait : un
    # outil d'envoi du lot des envois (`lemlist_inbox_send`, `linkedin_unipile_invite`)
    # n'a pas de lecteur `<ns>` — lister les invitations reste `linkedin_unipile_network`.
    paire = (any(r.outil == lecteur for r in lignes)
             or (lignes[0] not in RENOMMAGES_ENVOIS
                 and any(r.outil == lecteur for r in RENOMMAGES)))
    # Un outil d'envoi dédié dont l'acteur fait PLUS que lui : l'autoriser n'autorise pas
    # l'acteur (`remplacants_autorises`) — le refus le dit, pour qu'on l'ajoute exprès.
    en_plus = () if remplacants_autorises(ancien) else ops_en_plus(ancien, acteur)
    perdu = (f" Allowing `{ancien}` does NOT allow `{acteur}`, which does more "
             f"({', '.join(f'op={o}' for o in en_plus)}): to use it, add `{acteur}` "
             "explicitly to the allowed tools." if en_plus else "")
    qui = (f"`{lecteur}` reads and simulates (a draft is a simulation); `{acteur}` acts — "
           "it sends, deletes or changes" if paire else
           f"What reaches a third party lives in its own tool: `{acteur}` acts — it sends, "
           "publishes or changes")
    return Retrait(
        message=(
            f"`{ancien}` is retired ({lignes[0].depuis:%d/%m/%Y}, security): an MCP client "
            "authorizes "
            f"TOOLS, not arguments, so a tool that reads must not also send or delete. "
            f"{qui}, and your client asks you to confirm it. Call "
            f"instead, with the same "
            f"other arguments: {gestes}. If a procedure or a page told you to call "
            f"`{ancien}`, it predates this change: take the path above — this name only "
            f"returns this refusal, called directly or through `oto_call`.{perdu}"),
        hint=gestes,
    )


def _selectionne(r: Renommage, op: str) -> bool:
    return r.selecteur == f"op={op}" or (r.selecteur or "").startswith(f"op={op},")


def op_deplacee(outil: str, op: str) -> Optional[Retrait]:
    """Le refus d'une op d'envoi qu'un outil encore servi a perdue (`OPS_DEPLACEES`), ou
    None. Lu par `error_taxonomy.classify` quand la signature refuse cette `op`."""
    lignes = [r for r in OPS_DEPLACEES if r.ancien == outil and _selectionne(r, op)]
    if not lignes:
        return None
    acteur = lignes[0].outil
    gestes = " or ".join(
        (f"{r.selecteur.split(', ', 1)[1]} → " if ", " in (r.selecteur or "") else "")
        + f'`{acteur}(op="{r.op}", …)`' for r in lignes)
    return Retrait(
        message=(
            f'`{outil}(op="{op}")` moved ({lignes[0].depuis:%d/%m/%Y}, security): an MCP '
            "client authorizes TOOLS, not arguments, so a tool that reads must not also "
            f"reach a third party. `{outil}` keeps everything else; `{acteur}` acts — "
            "your client asks you to confirm it. Call instead, with the same other "
            f"arguments: {gestes}. If a procedure or a page told you to call "
            f'`{outil}(op="{op}")`, it predates this change: take the path above.'),
        hint=gestes,
    )


def annonce_act_du_jour(aujourd_hui: Optional[datetime.date] = None) -> Optional[str]:
    """`annonce_act()` tant qu'elle est servie, None ensuite."""
    jour = aujourd_hui or datetime.date.today()
    return annonce_act() if jour <= ANNONCE_ACT_JUSQU_AU else None


def annonce_act() -> str:
    """La ligne servie aux agents : « ces outils ont changé », ancien → nouveau, lot par
    lot."""
    lots: dict[datetime.date, dict[str, str]] = {}
    for r in RENOMMAGES:
        lots.setdefault(r.depuis, {})[r.ancien] = " + ".join(
            f"`{o}`" for o in remplacants(r.ancien))
    deplacees: dict[tuple, dict[str, None]] = {}
    for r in OPS_DEPLACEES:
        deplacees.setdefault((r.depuis, r.ancien, r.outil), {})[
            r.selecteur.split(", ")[0].removeprefix("op=")] = None
    for (jour, outil, acteur), ops in deplacees.items():
        lots.setdefault(jour, {})[f"{outil}(op={'/'.join(ops)})"] = f"`{acteur}`"
    return ("Tools changed (security: reading and acting are two tools) — "
            + " ; ".join(
                f"on {jour.isoformat()}: "
                + " · ".join(f"`{a}` → {vers}" for a, vers in anciens.items())
                for jour, anciens in lots.items())
            + ". The old names and ops refuse, naming the call to make instead.")


# --- 11/11/2026 : les connecteurs servis par leur description ---------------------------
# Décision d'Alexis (règle de convergence, connectors `docs/conception/fabrique.md`) : un
# connecteur décrit dans la lib est servi par le lecteur générique, et ses noms d'outils
# d'oto 1 changent le jour où il bascule, avec une note de migration. L'`op` est le nom de
# la fonction décrite, les arguments sont les siens : un ancien appel ne se traduit pas
# argument pour argument, le refus nomme l'appel et renvoie au schéma de l'op.

#: Le jour de la bascule de DataForSEO.
LOT_DATAFORSEO = datetime.date(2026, 11, 11)

#: DataForSEO : huit outils écrits à la main → `dataforseo`, une op par fonction décrite.
RENOMMAGES_DECRITS: tuple[Renommage, ...] = (
    *_vers("dataforseo_keywords", "dataforseo", {
        "overview": "keyword_overview", "volume": "google_ads_search_volume",
        "difficulty": "bulk_keyword_difficulty", "intent": "search_intent",
        "trends": "google_trends_explore", "ai_volume": "ai_keyword_search_volume",
        "ideas": "keyword_ideas", "suggestions": "keyword_suggestions",
        "related": "related_keywords"}, LOT_DATAFORSEO),
    *_vers("dataforseo_domain", "dataforseo", {
        "overview": "domain_rank_overview", "history": "historical_rank_overview",
        "keywords": "ranked_keywords", "pages": "relevant_pages", "subdomains": "subdomains",
        "competitors": "competitors_domain", "relevant_keywords": "keywords_for_site",
        "gap": "domain_intersection", "serp_competitors": "serp_competitors",
        "traffic": "bulk_traffic_estimation"}, LOT_DATAFORSEO),
    *_vers("dataforseo_backlinks", "dataforseo", {
        "summary": "backlinks_summary", "list": "backlinks_list",
        "referring_domains": "referring_domains", "anchors": "anchors",
        "top_pages": "backlinks_domain_pages", "competitors": "backlinks_competitors",
        "gap": "backlinks_intersection", "ranks": "bulk_ranks",
        "history": "backlinks_history", "new_lost": "backlinks_new_lost"}, LOT_DATAFORSEO),
    *_vers("dataforseo_account", "dataforseo", {
        "balance": "user_data", "locations": "labs_locations_and_languages"}, LOT_DATAFORSEO),
    *_vers("dataforseo_ai_visibility", "dataforseo", {
        "metrics": "llm_mentions_metrics", "mentions": "llm_mentions_search",
        "top_domains": "llm_mentions_top_domains", "top_pages": "llm_mentions_top_pages",
        "history": "llm_mentions_history", "ask": "llm_response", "models": "llm_models",
        "ai_mode": "google_ai_mode"}, LOT_DATAFORSEO),
    *_vers("dataforseo_web_mentions", "dataforseo", {
        "summary": "content_summary", "search": "content_search"}, LOT_DATAFORSEO),
    *_vers("dataforseo_site_audit", "dataforseo", {
        "page": "onpage_instant", "content": "onpage_content", "lighthouse": "lighthouse",
        "crawl_start": "onpage_crawl_start", "crawl_status": "onpage_summary",
        "op=crawl_results, report=pages": "onpage_pages",
        "op=crawl_results, report=non_indexable": "onpage_non_indexable",
        "op=crawl_results, report=duplicate_titles": "onpage_duplicate_tags",
        "op=crawl_results, report=duplicate_descriptions": "onpage_duplicate_tags",
        "op=crawl_results, report=broken_links": "onpage_links",
        "op=crawl_results, report=links": "onpage_links",
        "op=crawl_results, report=redirect_chains": "onpage_redirect_chains"},
        LOT_DATAFORSEO),
    *_vers("dataforseo_technologies", "dataforseo", {
        "domain": "domain_technologies", "domains_using": "domains_by_technology",
        "summary": "technologies_summary"}, LOT_DATAFORSEO),
)


def remplacants_decrits(ancien: str) -> tuple[str, ...]:
    """Les outils servis par la description qui reprennent `ancien` (`RENOMMAGES_DECRITS`) :
    ce qu'une liste enregistrée reçoit à sa place, qu'elle autorise ou masque — le
    connecteur et son geste sont les mêmes, seul le nom change. `()` s'il n'y figure pas."""
    return tuple(dict.fromkeys(r.outil for r in RENOMMAGES_DECRITS if r.ancien == ancien))


def _retrait_decrit(ancien: str) -> Retrait:
    lignes = [r for r in RENOMMAGES_DECRITS if r.ancien == ancien]
    gestes = "; ".join(f'{r.selecteur} → `{r.outil}(op="{r.op}", …)`' for r in lignes)
    outils = " and ".join(f"`{o}`" for o in remplacants_decrits(ancien))
    return Retrait(
        message=(
            f"`{ancien}` is retired ({lignes[0].depuis:%d/%m/%Y}): the connector is now served "
            f"from its description, by {outils}, whose `op` is the vendor function and whose "
            "arguments are that function's own — not the old ones: read the op's schema "
            "(`oto_tool_schema`), or call it, a call that does not match answers the op's "
            f"description, schema and an example. Call instead: {gestes}. If a procedure or a "
            f"page told you to call `{ancien}`, it predates this change: take the path above — "
            "this name only returns this refusal, called directly or through `oto_call`."),
        hint=gestes,
    )


RETIRES: dict[str, Retrait] = {
    "oto_kb": KB,
    **{a: _retrait_act(a) for a in dict.fromkeys(r.ancien for r in RENOMMAGES)},
    **{a: _retrait_decrit(a) for a in dict.fromkeys(r.ancien for r in RENOMMAGES_DECRITS)},
}


def retrait(nom: str) -> Optional[Retrait]:
    """Le refus servi pour un nom CANONIQUE retiré, ou None. L'appelant canonicalise
    d'abord (préfixe de tenant) : `acme_kb` d'un tenant arrive ici en `oto_kb`."""
    return RETIRES.get(nom)
