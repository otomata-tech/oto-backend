"""Le receveur des téléphones révélés par Apollo — oto héberge l'URL, plus l'agent.

Apollo ne rend jamais un mobile dans sa réponse : il vérifie les numéros de son côté et
les POSTe quelques minutes plus tard à une `webhook_url`, qu'il EXIGE. Cette URL était
fournie par l'appelant — une adresse hors de l'environnement de l'utilisateur, passée
en argument d'outil, et un client MCP qui filtre les appels sortants la refuse. Elle est
désormais GÉNÉRÉE ici, côté serveur, à chaque reveal :

    <OTO_MCP_PUBLIC_URL>/api/receivers/apollo/phones/<jeton>

Le jeton est aléatoire (256 bits), propre à UNE commande, et n'est stocké qu'en
empreinte SHA-256. Il désigne la ligne de `apollo_phone_reveals` ouverte avant l'appel
(`db/apollo_reveals.py`) : l'org et le compte qui ont commandé, la portée de la clé
Apollo qui a payé, puis le `request_id` qu'Apollo a rendu. `apollo_reveal_phone_result`
lit d'abord ce qu'Apollo nous a livré, et ne sonde Apollo (`webhook_result/{id}`) que si
rien n'est encore arrivé.

**Qui lit un reveal reçu : qui a accès au connecteur**, par la même clé. Le lecteur
résout sa clé Apollo AVANT toute lecture (comme pour commander), et ne voit que les
commandes payées par cette clé — sa `portee`. Un compte sans accès au connecteur ne
lit rien ; deux comptes sans org, chacun sur sa clé, ne se lisent pas.

Ce module DÉCIDE (jeton, URL, réception, lecture) ; la route HTTP (`api/receveurs.py`)
ne fait qu'adapter, hors boucle.

⚠️ **La forme du corps POSTé n'a pas été relevée en vrai depuis cet environnement.**
Apollo documente que le sondage rend « le même contenu que le POST » ; le code servi
lisait jusqu'ici ce sondage sous `result.webhook_result.people[]`. Le corps est donc
stocké tel quel et servi tel quel, et une seule forme de repli est reconnue et
NOMMÉE (`enveloppe`) : un `people[]` au premier niveau, rangé sous `webhook_result`
pour que les procédures qui lisent ce chemin continuent de le trouver.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Any, Optional

from . import access, config
from .access import ResolvedCredential, heritage
from .db import apollo_reveals as db_apollo

logger = logging.getLogger(__name__)

#: Le chemin monté par `api/receveurs.py` — écrit une fois, lu par les deux côtés.
CHEMIN = "/api/receivers/apollo/phones/{token}"
#: Plafond du corps accepté. Un lot de dix fiches complètes pèse quelques centaines de
#: kilo-octets ; au-delà, le POST est refusé (413) et le sondage d'Apollo reste le
#: chemin de repli — rien n'est perdu, seulement rapatrié autrement.
CORPS_MAX = 1024 * 1024

RECU, DOUBLON, INCONNU = db_apollo.RECU, db_apollo.DOUBLON, db_apollo.INCONNU


#: La forme d'un jeton émis : `secrets.token_urlsafe(32)`, 43 caractères base64url.
_FORME_JETON = re.compile(r"[A-Za-z0-9_-]{43}")


def forme_valide(jeton: str) -> bool:
    """Le jeton a-t-il la forme de ceux qu'on émet ? Jugé AVANT de lire le corps :
    qui arrose la route avec n'importe quoi ne fait pas lire un mégaoctet."""
    return bool(_FORME_JETON.fullmatch(jeton))


def empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode("utf-8")).hexdigest()


def url_de_reception(jeton: str) -> str:
    """L'adresse qu'Apollo POSTera. `public_base_url` lève sans `OTO_MCP_PUBLIC_URL` :
    une adresse de réception devinée enverrait les numéros ailleurs."""
    return config.public_base_url() + CHEMIN.format(token=jeton)


def portee(cle: ResolvedCredential, sub: Optional[str] = None) -> str:
    """La portée d'une clé Apollo résolue : `niveau:entité:compte`. Deux appels qui
    résolvent la même clé (même niveau gagnant, même entité, même compte) ont la même
    portée — c'est la clé de lecture d'un reveal reçu.

    ⚠️ **La clé PLATEFORME est mutualisée** : toutes les orgs la résolvent à
    l'identique (`platform:::`). Sa portée seule ferait lire à une org les numéros
    qu'une AUTRE a commandés, au seul `request_id`. Sur cette clé, la portée porte
    donc l'org de l'appelant (son compte, sans org) : chacun ne lit que ses
    commandes, comme sur une clé propre."""
    base = f"{cle.mode}:{cle.entity_type or ''}:{cle.entity_id or ''}:{cle.account or ''}"
    if not cle.is_platform:
        return base
    sub = sub or access.current_user_sub_or_raise()
    # L'org dont l'appel CONSOMME la clé commune — la même que compte
    # `platform_rate` (`heritage.org_des_cles`) : un bénéficiaire hors de l'org d'un
    # projet partagé, à qui rien n'est prêté, compte (et lit) sous son compte, pas
    # sous l'org du projet.
    org = heritage.org_des_cles(sub, access.current_org(sub))
    return f"{base}:org:{org}" if org is not None else f"{base}:user:{sub}"


class PlafondDuJour(Exception):
    """La portée a déjà commandé `used` reveals aujourd'hui (UTC), son plafond est
    `limit` : rien n'est écrit, rien n'est commandé chez Apollo."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"{used}/{limit}")
        self.used, self.limit = used, limit


def commander(cle: ResolvedCredential,
              plafond: Optional[int] = None) -> tuple[str, str]:
    """Ouvre une commande AVANT l'appel à Apollo, payée par `cle` (la clé Apollo que
    l'appelant vient de résoudre). Rend `(jeton, url)`.

    `plafond` : le nombre de commandes du jour (UTC) que la portée de `cle` ne dépasse
    pas — compté et écrit d'un seul tenant (`db_apollo.ouvrir`), donc tenu même sous
    des commandes parallèles. Atteint : lève `PlafondDuJour`."""
    sub = access.current_user_sub_or_raise()
    jeton = secrets.token_urlsafe(32)
    url = url_de_reception(jeton)
    refus = db_apollo.ouvrir(empreinte(jeton), access.current_org(sub), sub,
                             portee(cle, sub), plafond=plafond)
    if refus is not None:
        raise PlafondDuJour(refus, plafond)  # type: ignore[arg-type]
    return jeton, url


def lier(jeton: str, request_id: Any) -> None:
    """Rattache le `request_id` d'Apollo à la commande.

    ⚠️ Ne lève JAMAIS : à ce point le reveal est commandé et PAYÉ, et l'agent doit
    recevoir son `request_id` quoi qu'il arrive ici. Si la liaison échoue, la
    commande reste lisible par le repli — le sondage d'Apollo — et le POST d'Apollo
    la complète s'il porte l'identifiant."""
    try:
        db_apollo.lier_request_id(empreinte(jeton), str(request_id))
    except Exception:  # noqa: BLE001 — journalisé ; le repli Apollo couvre la lecture
        logger.warning("receveur apollo : request_id non rattaché à sa commande",
                       exc_info=True)


def abandonner(jeton: str) -> None:
    """Apollo n'a rien accepté : l'URL de réception cesse d'exister.

    Ne lève pas non plus — appelée sur le chemin d'une AUTRE erreur, qu'elle ne doit
    pas masquer ; une commande non retirée expire d'elle-même en trente jours."""
    try:
        db_apollo.abandonner(empreinte(jeton))
    except Exception:  # noqa: BLE001 — journalisé ; l'échéance la retire de toute façon
        logger.warning("receveur apollo : commande abandonnée non retirée",
                       exc_info=True)


def recevoir(jeton: str, corps: Any) -> str:
    """Le POST d'Apollo : `RECU`, `DOUBLON` (retentative, rien d'écrasé) ou `INCONNU`."""
    rid = corps.get("request_id") if isinstance(corps, dict) else None
    return db_apollo.recevoir(empreinte(jeton), corps,
                              str(rid) if rid is not None else None)


def enveloppe(corps: Any) -> dict:
    """Le corps reçu, à la forme que sert le sondage d'Apollo (`result`).

    Tel quel s'il porte `webhook_result` ; sinon un `people[]` de premier niveau est
    rangé sous `webhook_result`, le reste des clés gardé à côté."""
    if not isinstance(corps, dict):
        return {"webhook_result": {"people": []}}
    if "webhook_result" in corps or not isinstance(corps.get("people"), list):
        return corps
    reste = {k: v for k, v in corps.items() if k != "people"}
    return {**reste, "webhook_result": {"people": corps["people"]}}


def resultat_recu(request_id: str,
                  cle: ResolvedCredential) -> Optional[dict]:
    """L'enveloppe livrée par Apollo pour cette commande payée par `cle` (la clé
    Apollo que le lecteur a résolue), ou None si rien n'est arrivé (commande en
    attente, inconnue sur cette clé, ou expirée)."""
    ligne = db_apollo.lire(portee(cle), str(request_id))
    if not ligne or ligne.get("payload") is None:
        return None
    return enveloppe(ligne["payload"])


def commande_connue(request_id: str, cle: ResolvedCredential) -> bool:
    """Une commande de ce `request_id` existe-t-elle dans la portée de `cle`, livrée
    ou pas encore ? Sur la clé plateforme, c'est la condition du repli : sonder
    Apollo (`webhook_result/{id}`) avec la clé commune rendrait le reveal de
    N'IMPORTE quelle org à qui devine son identifiant."""
    return db_apollo.lire(portee(cle), str(request_id)) is not None


def personnes(result: dict) -> list[dict]:
    wr = result.get("webhook_result") if isinstance(result, dict) else None
    people = wr.get("people") if isinstance(wr, dict) else None
    return [p for p in people if isinstance(p, dict)] if isinstance(people, list) else []


def meilleur_numero(personne: dict) -> Optional[dict]:
    """Le numéro à écrire pour une personne : le premier mobile, sinon le premier
    numéro. None si Apollo n'en a livré aucun."""
    numeros = [n for n in (personne.get("phone_numbers") or [])
               if isinstance(n, dict) and n.get("sanitized_number")]
    if not numeros:
        return None
    mobiles = [n for n in numeros if n.get("type_cd") == "mobile"]
    return (mobiles or numeros)[0]


def ecrire_numeros(result: dict, *, datastore: Any, row_id: Optional[str],
                   match_column: Optional[str], phone_column: str) -> dict:
    """Écrit les numéros livrés dans le tableau de l'appelant ; rend des COMPTES.

    Aucun numéro ne sort de cette fonction : c'est tout son objet. `row_id` désigne
    LA ligne d'un reveal d'une personne ; `match_column` rapproche chaque personne
    d'un lot de la ou des lignes dont cette colonne porte son id Apollo. Le numéro
    écrit est le premier mobile, sinon le premier numéro ; son type et son statut
    d'opposition au démarchage (`dnc_status_cd`) vont dans la couche `comment` de la
    case — à lire avant d'appeler."""
    from .datastore import par_reference as pr

    if (row_id is None) == (match_column is None):
        raise pr.refus("apollo_phone_target",
                       "avec `datastore`, passe exactement un de `row_id` (la ligne de "
                       "cette personne) ou `match_column` (la colonne qui porte l'id "
                       "Apollo de chaque personne). Rien n'a été écrit.")
    gens = personnes(result)
    if row_id is not None and len(gens) > 1:
        raise pr.refus("apollo_phone_target",
                       f"ce reveal a livré {len(gens)} personnes : une seule ligne ne "
                       "peut pas les recevoir — passe `match_column`. Rien n'a été écrit.")
    ids = [str(p.get("id")) for p in gens if p.get("id")]
    if row_id is not None:
        lot = pr.ouvrir(datastore, row_ids=[row_id], filter=None, colonne_etat=None,
                        limite=1)

        def lignes_de(_personne: dict) -> list[dict]:
            return lot.lignes
    else:
        # Sans aucun id Apollo livré, aucune ligne ne peut être rapprochée : on
        # n'ouvre pas le tableau pour rien (un filtre `in` vide est d'ailleurs refusé).
        lot = pr.ouvrir(datastore, row_ids=None, filter={match_column: {"in": ids}},
                        colonne_etat=None, limite=pr.MAX_LIGNES) if ids else None
        cibles: dict[str, list[dict]] = {}
        for ligne in (lot.lignes if lot else []):
            cibles.setdefault(str(pr.valeur(ligne, match_column)), []).append(ligne)

        def lignes_de(personne: dict) -> list[dict]:
            return cibles.get(str(personne.get("id")), []) if personne.get("id") else []

    comptes = {"people": len(gens), "with_numbers": 0, "rows_written": 0,
               "people_without_rows": 0}
    erreurs: list[dict] = []
    for personne in gens:
        numero = meilleur_numero(personne)
        if numero is None:
            continue
        comptes["with_numbers"] += 1
        lignes = lignes_de(personne)
        if not lignes:
            comptes["people_without_rows"] += 1
            continue
        commentaire = (f"apollo_reveal_phone · {numero.get('type_cd') or '?'} · "
                       f"dnc: {numero.get('dnc_status_cd') or '?'}")
        for ligne in lignes:
            rid = str(ligne.get("_id"))
            if pr.tenue_ailleurs(ligne):
                erreurs.append({"row_id": rid, "code": "row_locked"})
                continue
            code = pr.ecrire(lot, rid, {phone_column: numero["sanitized_number"],
                                        f"{phone_column}.comment": commentaire})
            if code:
                erreurs.append({"row_id": rid, "code": code})
            else:
                comptes["rows_written"] += 1
    out = {**(lot.identite() if lot else {}), **comptes, "phone_column": phone_column,
           "errors": erreurs}
    if lot is not None:
        out["errors"] += [{"row_id": i, "code": "row_not_found"} for i in lot.absentes]
    return out


def marquer_releve(request_id: str, cle: ResolvedCredential) -> None:
    """La relève a tracé le coût de cette commande (clé plateforme) : le relevé serveur
    (`apollo_releve.py`) ne la facturera pas. Ne lève pas : la relève est servie, et le
    facturant ne garde qu'une ligne par commande si le relevé la retrace."""
    try:
        db_apollo.marquer_releve(portee(cle), str(request_id))
    except Exception:  # noqa: BLE001 — journalisé ; le facturant dédoublonne
        logger.warning("receveur apollo : relève non marquée", exc_info=True)


def commandes_du_jour(cle: ResolvedCredential) -> int:
    """Les commandes passées aujourd'hui (UTC) dans la portée de `cle` — sur la clé
    plateforme, celles de l'org dont l'appel consomme la clé. Une lecture : le plafond
    de `apollo_reveal_phone`, lui, se compte dans `commander`, sous verrou."""
    return db_apollo.commandes_du_jour(portee(cle))
