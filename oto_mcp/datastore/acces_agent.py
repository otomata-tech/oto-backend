"""À QUI une colonne est servie — le quatrième cran de la famille (oto#83).

Le schéma savait dire qu'une colonne est verrouillée. Il ne savait pas dire **pour
qui**. Entre « le client la modifie depuis son écran » et « personne ne la modifie »
(`readonly`), il n'y avait rien — et c'est dans ce trou qu'un incident est tombé.

**Le fait qui fonde ce module.** Un tableau client porte une colonne de suivi
commercial, déclarée modifiable, dont la description dit : « Où en est VOTRE démarche
auprès de cette entreprise. À vous de le renseigner. » Cette phrase est écrite pour le
client. Elle est LUE par un agent, à qui la plateforme sert le schéma entier : elle
s'adresse donc à lui. Un agent a posé un statut de clôture sur un prospect avant tout
contact, sans aucune source. Le tableau ne mentait pas — il n'avait aucun moyen de dire
« pas toi ».

## La forme : un attribut, trois valeurs

`agent_access` sur une colonne, au premier niveau :

- absent ou `"write"` — le défaut d'aujourd'hui, rien ne change ;
- `"read"` — l'agent VOIT la colonne (schéma et lignes) mais son écriture de la
  VALEUR est refusée. Les couches `comment`/`link` restent ouvertes : c'est là que va
  ce qu'il a constaté ;
- `"none"` — l'agent ne voit rien : ni dans le schéma servi, ni dans la ligne qu'il
  lit ou réserve. Toute mention de la colonne dans une écriture est refusée.

Trois valeurs plutôt qu'un booléen parce que le diagnostic porte sur deux crans
distincts — « pas éditable par l'agent » et « pas même montrée ». Un booléen
`hidden_from_agent` les aurait soudés pour toujours : on n'aurait jamais pu servir en
lecture une colonne qu'un agent doit CONSULTER pour décider sans avoir le droit de la
réécrire. La valeur inconnue est REFUSÉE à la pose (`_validate_reserved_def`) : sur un
cran de garde, une faute de frappe silencieuse désarme la garde, et c'est exactement la
plaie que `read_only`/`readonly` a déjà coûtée.

## Ce qu'est « un agent », et pourquoi ce n'est pas le run

La seule chose que le serveur sache de l'appelant sans la tenir de lui : **la porte
empruntée**. Les tools MCP sont écrits POUR un modèle — leurs schémas, leurs
descriptions, et le registre du datastore le dit déjà mot pour mot (`mcp=None,  # …
tient déjà la face agent`). Les routes REST `/api/*` servent le dashboard, les fronts
tiers et les scripts du client.

`_run_id` était le candidat naturel. Il ne tient pas :

- il est **déclaré par celui qu'on juge**. Le runner le pose en `setdefault`, donc un
  modèle qui envoie le sien gagne ; il n'est posé que si l'outil déclare la propriété ;
  et sur le chemin des Conversations (boucle d'outils hébergée chez le fournisseur), le
  worker ne voit jamais passer les appels — il ne peut rien injecter du tout ;
- **rien ne le pose sur la face REST** (`session_org.set_call_run` n'est appelé que
  depuis l'axe `_run_id=` et les tools `run_*`, le run de session — `set_session_run`,
  #830 — que depuis le middleware MCP). Un `or run` dans le
  prédicat serait donc une branche inerte : verte, jamais empruntée.

Un agent SANS run ouvert est donc reconnu — c'est le cas que le run ratait, et c'est le
plus dangereux, puisque c'est celui de l'agent qui n'a pas suivi la consigne.

⚠️ **Le trou qui reste, nommé.** Un agent qui atteint la face REST avec la clé de son
propriétaire (le connecteur `api` pointé sur notre propre API, un script du client
piloté par un modèle) n'est pas distingué de l'écran : sur REST, la clé du client et
l'écran du client sont le même porteur. Le fermer demande une identité d'agent, pas une
heuristique — la brique existe à moitié (`token_kind ∈ {user, delegation}`, frappé à la
réservation d'un job runner), mais elle est jetée à la vérification du jeton MCP
(`server.py`, `AccessToken` reconstruit sans `token_kind`) et aucun appel REST ne la
porte aujourd'hui. Câbler ça est un lot d'authentification, pas de datastore.

## Ce que le masquage n'est pas

Une colonne masquée **n'est pas supprimée** : sa valeur reste en base, l'écran la lit et
l'écrit, les exports et les agrégats du propriétaire la voient. Le masquage porte sur ce
qui est SERVI. Il ne rend pas la colonne inaccessible comme ORACLE : un agent qui
connaîtrait son nom peut encore filtrer, trier ou agréger dessus, et la recherche plein
texte balaie toutes les valeurs. Fermer cet axe est un lot à part — le point où le
filtre de l'APPELANT se distingue du périmètre déclaré par le PROPRIÉTAIRE n'est pas
unique aujourd'hui (`claim_next` compose les deux avant de descendre en SQL), et une
garde posée au mauvais endroit couperait la file de travail.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .. import session_org

#: L'attribut de colonne. Déclaré dans `schema_keys.CLES` — c'est cette déclaration qui
#: fait dénoncer une faute de frappe sur le NOM ; le refus de valeur ci-dessous ferme
#: l'autre moitié (une faute de frappe sur la VALEUR).
CLE = "agent_access"

ECRITURE = "write"
LECTURE = "read"
AUCUN = "none"

#: Les valeurs permises, dans l'ordre où elles s'ouvrent. Servie dans le refus de pose :
#: qui se trompe lit ce qui existe, pas « valeur invalide ».
VALEURS: tuple[str, ...] = (ECRITURE, LECTURE, AUCUN)

#: Combien de destinations le refus nomme au plus. Un refus qui déroule quarante noms
#: n'est plus une destination, c'est un schéma.
_DESTINATIONS_MAX = 6


def acces_of(f: Any) -> str:
    """L'accès agent DÉCLARÉ d'une colonne. Absent ⇒ `"write"` (le défaut d'avant).

    Une valeur non reconnue rend `"write"` : elle a été refusée à la pose, donc elle ne
    peut venir que d'un schéma antérieur au cran — et retomber sur le défaut d'avant est
    le seul choix qui ne change rien pour lui."""
    if not isinstance(f, dict):
        return ECRITURE
    v = f.get(CLE)
    return v if v in VALEURS else ECRITURE


def _champs(schema: Optional[dict]) -> list:
    if not isinstance(schema, dict):
        return []
    fields = schema.get("fields")
    return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []


def _cles_par_acces(schema: Optional[dict], acces: tuple[str, ...]) -> set:
    """Les colonnes dont l'accès agent tombe dans `acces` — **jamais la clé métier**.

    `_validate_reserved_def` la refuse déjà à la pose ; l'écarter ICI aussi couvre les
    schémas ANTÉRIEURS au cran, où `agent_access` n'était qu'une clé transportée que
    rien ne lisait. Sans cette ligne, un tableau qui la portait par hasard sur sa clé
    deviendrait, au déploiement, illisible et inécrivable pour tous ses agents d'un
    coup — un effet massif, simultané et de cause vieille de plusieurs semaines, très
    exactement le mode de panne que `enforced_keys` existe pour rendre visible."""
    cle_metier = schema.get("key") if isinstance(schema, dict) else None
    return {f["key"] for f in _champs(schema)
            if isinstance(f.get("key"), str) and f["key"] and f["key"] != cle_metier
            and acces_of(f) in acces}


def masquees(schema: Optional[dict]) -> set:
    """Les colonnes qu'un agent ne voit pas du tout (`agent_access: "none"`)."""
    return _cles_par_acces(schema, (AUCUN,))


def fermees(schema: Optional[dict]) -> set:
    """Les colonnes dont un agent n'écrit pas la VALEUR (`"read"` et `"none"`)."""
    return _cles_par_acces(schema, (LECTURE, AUCUN))


def sans_effet(schema: Optional[dict]) -> list[tuple[str, str]]:
    """`(colonne, raison)` pour chaque accès agent DÉCLARÉ que le cran n'applique pas
    (oto#94) — lu par la lecture du schéma tel qu'il est stocké.

    Dérivé de `masquees`/`fermees`, les deux prédicats que la sortie et le refus
    consultent, jamais d'une copie de leur règle : une colonne déclarée `"none"` qui
    n'est pas dans `masquees` n'est pas masquée, quelle qu'en soit la raison. La
    raison, elle, se nomme : une valeur que `acces_of` ne reconnaît pas, ou la clé
    métier, que `_cles_par_acces` écarte."""
    masq, ferm = masquees(schema), fermees(schema)
    cle_metier = schema.get("key") if isinstance(schema, dict) else None
    out = []
    for cle, v in _acces_declares(schema).items():
        if v is None or v == ECRITURE:
            continue
        if v not in VALEURS:
            out.append((cle, f"value {v!r} not recognized: read as \"{ECRITURE}\""))
        elif cle not in (masq if v == AUCUN else ferm):
            out.append((cle, "business key: never hidden from or closed to an agent"
                        if cle == cle_metier else "not applied"))
    return out


def appel_d_agent() -> bool:
    """Cet appel est-il piloté par un modèle ?

    Vrai si, et seulement si, il est entré par un tool MCP. Le défaut est FAUX — hors
    d'un appel d'outil (REST, boot, tâche de fond, test), rien n'est masqué et rien
    n'est refusé : un masquage qui s'applique par accident retirerait à un propriétaire
    la colonne de son propre écran."""
    return session_org.current_call_face() == session_org.FACE_MCP


# ── Ce qui est SERVI ─────────────────────────────────────────────────────────


def schema_servi(schema: Optional[dict]) -> Optional[dict]:
    """Le schéma tel qu'il part à l'appelant — amputé des colonnes masquées si c'est
    un agent, rendu tel quel sinon.

    ⚠️ Ne JAMAIS employer pour valider : le validateur a besoin du schéma ENTIER, sinon
    la colonne masquée devient une colonne inconnue et l'écriture de l'agent y passerait
    comme un champ hors schéma — le contraire exact du but. Le masquage est une opération
    de SORTIE, jamais d'entrée.

    Une copie superficielle suffit : on remplace `fields` (et `claimable`) par des
    listes/dicts neufs, on ne touche à aucun champ en place."""
    if not appel_d_agent():
        return schema
    cachees = masquees(schema)
    if not cachees or not isinstance(schema, dict):
        return schema
    out = dict(schema)
    out["fields"] = [f for f in schema.get("fields") or []
                     if not (isinstance(f, dict) and f.get("key") in cachees)]
    _rincer_le_perimetre(out, cachees)
    return out


def _rincer_le_perimetre(servi: dict, cachees: set) -> None:
    """Retire du périmètre de réservation les colonnes masquées à l'agent.

    Le périmètre NOMME des colonnes et porte une de leurs VALEURS. Le laisser intact
    rendrait par la porte de derrière ce que la porte d'entrée vient de retirer — le
    nom d'une colonne que le propriétaire a déclarée invisible aux agents, et la
    valeur sur laquelle elle est filtrée.

    ⚠️ **Ce rinçage existait depuis l'origine et cherchait à la MAUVAISE ADRESSE.**
    Il lisait `schema["claimable"]`, au premier niveau, où rien ne vit : l'adresse
    réelle est `fields[role="status"].lifecycle.claimable`, celle que
    `cycle_de_vie.claimable_of` interroge et que la pose valide. Mesuré le 07/09/2026
    sur un schéma que `validate_schema_def` accepte sans une erreur — la colonne
    disparaissait bien des `fields`, et ressortait entière dans le périmètre.

    Et le banc qui gardait ce rinçage ne pouvait pas le voir : sa fixture posait
    `claimable` au premier niveau, là où le moteur ne lit rien, et nommait une colonne
    NON masquée. Son assertion était donc satisfaite par la copie du schéma — branche
    retirée, trente-deux verts sur trente-deux. C'est le pire cas d'un banc : vert en
    n'exerçant rien.

    ⚠️ **L'adresse est DÉRIVÉE de `status_field`**, jamais réécrite ici : deux endroits
    qui ne s'accordent pas sur l'endroit où vit le périmètre, c'est exactement le
    défaut qu'on répare. Le rinçage du premier niveau est conservé — il ne coûte rien,
    et un schéma qui porterait la clé là ne doit pas passer entre les mailles.
    """
    from .declaration import status_field

    haut = servi.get("claimable")
    if isinstance(haut, dict):
        servi["claimable"] = {k: v for k, v in haut.items() if k not in cachees}

    ancre = status_field(servi)
    if not isinstance(ancre, dict):
        return
    cycle = ancre.get("lifecycle")
    if not isinstance(cycle, dict):
        return
    perimetre = cycle.get("claimable")
    if not isinstance(perimetre, dict):
        return
    propre = {k: v for k, v in perimetre.items() if k not in cachees}
    if propre == perimetre:
        return
    # Copies neuves jusqu'à la clé touchée : le schéma d'entrée n'appartient pas à
    # cette fonction, et le muter contaminerait le validateur, qui lit l'ENTIER.
    servi["fields"] = [
        {**f, "lifecycle": {**cycle, "claimable": propre}}
        if f is ancre else f
        for f in servi.get("fields") or []
    ]


# ── Ce qui est REFUSÉ, et où porter l'intention ──────────────────────────────


def _destinations(schema: Optional[dict], sauf: str) -> list:
    """Les colonnes qu'un agent peut réellement écrire sur ce tableau.

    Le refus les NOMME. Un refus qui dit seulement « interdit » fait rejouer le même
    appel — mesuré dans la nuit du 05 au 06/09/2026 sur une autre garde du datastore :
    huit refus sur dix-huit rejouaient le geste refusé, la reprise la plus rapide à neuf
    secondes."""
    from . import schema as dsv2
    ro = dsv2.readonly_fields(schema)
    hors = fermees(schema) | ro | {sauf}
    noms = [f["key"] for f in _champs(schema)
            if isinstance(f.get("key"), str) and f["key"] and f["key"] not in hors]
    return noms[:_DESTINATIONS_MAX]


def _ou_porter(schema: Optional[dict], cle: str, *, couche_ouverte: bool) -> str:
    """La phrase de destination, construite depuis le schéma RÉEL du tableau."""
    morceaux = []
    if couche_ouverte:
        morceaux.append(f"as a comment on the column (`{cle}.comment`), which stays "
                        f"open for writing")
    noms = _destinations(schema, cle)
    if noms:
        morceaux.append("on the columns served to you for writing: "
                        + ", ".join(f"`{n}`" for n in noms))
    if not morceaux:
        return ("No column of this table is open to you for writing: what you "
                "observed goes in your end-of-work report, not here.")
    return "What you observed goes " + ", or ".join(morceaux) + "."


def refus(schema: Optional[dict], cle: str, acces: str) -> str:
    """Le texte servi quand un agent écrit une colonne qui ne lui appartient pas.

    Il dit les trois mêmes choses que ses deux sœurs de la famille (`readonly`,
    `origine`) : le champ, la raison, où va la chose — plus une quatrième que
    l'incident a rendue nécessaire : **réessayer ne changera rien**. Le refus ne nomme
    ni le propriétaire ni le réglage à modifier : la sortie n'est pas d'ouvrir la
    colonne, elle est de porter l'intention ailleurs."""
    if acces == AUCUN:
        return (
            f"`{cle}` is not served to you: it is a column the owner of this "
            f"table maintains themselves, and it appears neither in the schema nor in the "
            f"rows you read or claim. Nothing was written, and replaying this call "
            f"will return the same refusal. "
            + _ou_porter(schema, cle, couche_ouverte=False))
    return (
        f"`{cle}` is served to you for READING, not for writing: the owner of "
        f"this table keeps it up to date, and a value set from elsewhere would state what "
        f"you assume where they know. Nothing was written, and replaying this call will return the "
        f"same refusal. "
        + _ou_porter(schema, cle, couche_ouverte=True))


# ── Le SCHÉMA lui-même ne s'ouvre pas depuis la face agent ───────────────────


def acces_declare(schema: Optional[dict], cle: str) -> Optional[str]:
    """La valeur BRUTE de l'attribut sur cette colonne, sans aucune règle appliquée.

    Distincte de `acces_of` (qui normalise) et de `fermees` (qui écarte la clé métier) :
    c'est la seule lecture qui voie ce que l'auteur a ÉCRIT, et c'est ce dont a besoin
    le refus de pose — une garde qui se relit à travers sa propre exception ne refuse
    jamais rien."""
    return _acces_declares(schema).get(cle)


def _acces_declares(schema: Optional[dict]) -> dict:
    """`{colonne: valeur DÉCLARÉE de l'attribut}` — `None` quand la colonne ne le porte
    pas. Sert à comparer un schéma AVANT à un schéma APRÈS, ce qui est la seule façon
    de distinguer « je pose ce réglage » de « je transporte celui qui était là »."""
    return {f["key"]: f.get(CLE) for f in _champs(schema)
            if isinstance(f.get("key"), str) and f["key"]}


def refus_de_schema(ancien: Optional[dict], nouveau: Optional[dict],
                    *, geste: str, a_titre: Callable[[], bool]) -> Optional[str]:
    """Le refus d'un geste de SCHÉMA sur l'accès agent, ou `None` s'il passe.

    Sans ce cran, la capacité serait décorative : il suffirait à un agent de poser
    `agent_access: "write"` (ou de reposer le schéma sans la colonne) pour rouvrir ce
    qu'on vient de fermer, puis d'écrire. C'est la manœuvre « lever, écrire, refermer »
    que le dépôt a déjà payée sur `readonly` (#658) et sur `key_required` (#668) — à
    ceci près qu'ici elle n'aurait même pas besoin d'être refermée.

    Deux gestes distincts, deux refus, dans cet ordre :

    - **reposer un schéma ENTIER** (`geste="schema"`) sur un tableau qui porte un accès
      agent déclaré. `set_schema` REMPLACE, il ne fusionne pas, et l'agent ne voit pas
      les colonnes masquées : le schéma qu'il relit puis repose les efface, réglage
      compris. C'est le piège du `_id` relu puis réécrit, à l'échelle du format. La
      destination est nommée : `data_patch_schema`, qui fusionne par clé et ne peut pas
      détruire ce qu'il ne nomme pas ;
    - **déclarer, changer ou retirer `agent_access`** — réservé au TITRE, pas à la
      face (oto#93) : `a_titre()` dit si l'appelant possède le tableau ou le gouverne,
      le palier du forçage de `readonly` (`_peut_forcer`). Le jugement portait sur la
      porte d'entrée : le propriétaire qui passait par un outil était traité en
      exécutant, alors que le forçage voisin lui était ouvert. Désormais une session
      qui possède ou gouverne pose le réglage par l'outil comme par l'écran ; qui n'a
      reçu qu'un accès en écriture partagé ne le pose ni par l'un ni par l'autre.
      Jugé sur le DELTA `ancien → nouveau`, jamais sur la présence de l'attribut : un
      patch légitime réémet les champs tels qu'il les a lus, réglage compris, et le
      refuser arrêterait tout patch sur un tableau réglé. Le poser sur une colonne
      NEUVE compte aussi : sinon un exécutant s'en déclare un en `"write"` et le cran
      ne mord jamais. `a_titre` n'est appelé que si le réglage bouge : le palier coûte
      une lecture d'ownership, le chemin nominal n'en paie aucune.

    Le premier refus reste une affaire de FACE, et c'est voulu : il tient à ce que
    l'appelant VOIT, et la face agent masque les colonnes `none` à tout le monde,
    propriétaire compris.
    """
    avant, apres = _acces_declares(ancien), _acces_declares(nouveau)
    if appel_d_agent() and geste == "schema" \
            and any(v is not None for v in avant.values()):
        return (
            "ce tableau porte des colonnes dont l'accès agent est déclaré, et tu ne "
            "les vois pas toutes : poser un schéma le REMPLACE, celui-ci les "
            "effacerait avec leur réglage. Passe par `data_patch_schema`, qui fusionne "
            "par clé et ne peut pas détruire ce qu'il ne nomme pas. Rien n'a été "
            "écrit, et rejouer cet appel rendra le même refus.")
    bouge = sorted({k for k, v in apres.items() if v != avant.get(k)}
                   | {k for k, v in avant.items() if v is not None and k not in apres})
    if bouge and not a_titre():
        noms = ", ".join(f"`{k}`" for k in bouge)
        return (
            f"`{CLE}` dit à qui une colonne est servie — seul qui POSSÈDE ce tableau "
            f"(lui, son org, son équipe) ou le GOUVERNE le décide, par l'outil comme "
            f"par l'écran ; un accès en écriture partagé ne suffit pas. Ton geste le "
            f"pose, le change ou le retire sur {noms} : laisse cet attribut tel que le "
            f"schéma le porte et le reste de ta déclaration passera. Rien n'a été "
            f"écrit, et rejouer cet appel rendra le même refus — si une colonne te "
            f"manque pour travailler, dis-le dans ton compte rendu ou au propriétaire "
            f"du tableau.")
    return None
