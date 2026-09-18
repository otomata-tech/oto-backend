"""Forcer une colonne verrouillée — **sur l'appel**, jamais par un état (#658).

Une colonne `readonly: true` protège la valeur remise par le client contre son
écrasement par un agent (#606). Elle était fermée à TOUT LE MONDE : celui à qui la
donnée appartient ne pouvait plus la corriger, et la seule sortie nommée était le
schéma — lever le cran, écrire, le remettre.

⚠️ **Cette manœuvre-là est le défaut qu'on ferme, pas la sortie qu'on offre.** Mesurée
sur l'autre verrou de la plateforme, celui qui a la forme d'un ÉTAT (`key_required`,
#668) : le 01/09/2026 un agent refusé la retrouve seul et la rejoue deux fois sur deux
tableaux — il a bien refermé ; le lendemain un autre passage ne la retrouve pas et
s'arrête. *Il suffit qu'une exécution s'interrompe entre « lever » et « remettre » pour
que le verrou reste ouvert sans que personne le sache* — et une colonne déverrouillée
ne produit aucun signal. Le forçage vaut donc pour CET APPEL et rien d'autre : rien à
refermer, rien à oublier.

Trois choses le tiennent, et il en faut trois :

- **le palier** (`core.DatastorePg._forcage_readonly`) — le PROPRIÉTAIRE du tableau
  (owner-match : lui, son org, son équipe) OU celui qui le GOUVERNE
  (`ownership.can_govern` : gérant, admin d'org, admin plateforme). L'un des deux
  suffit. Un accès en écriture PARTAGÉ (`data_share`) ne suffit PAS : un verrou que
  quiconque peut écrire peut lever ne protège de personne ;
- **le refus qui nomme le geste** (`arbitrer`) — qui peut forcer, et comment. Un refus
  qui dit seulement « colonne verrouillée » renvoie l'appelant chercher une manœuvre :
  c'est exactement ce qui a produit le contournement ci-dessus ;
- **la trace** — chaque substitution est relevée (ligne, colonne, valeur remplacée) et
  versée aux arguments du journal des appels (`server._TRACED_ARGS`), à côté du `sub`
  que le journal stampe déjà. ⚠️ Tranché le 02/09/2026 **en connaissance de cause** :
  le journal garde **90 jours** en ligne — une seule politique (#426) — puis ARCHIVE en
  froid sur l'Object Storage avant d'effacer. La trace quitte donc la table que le
  produit sait interroger, alors que la valeur forcée reste. Pas de colonne de plus sur
  la ligne — la question a été posée et fermée.
  ⚠️ Ce passage annonçait « ~35 jours » jusqu'au 10/09/2026, et oto#138 a calculé sur lui
  une échéance fausse (« les traces expirent entre le 12 et le 15 octobre » au lieu de
  début décembre). La décision, elle, ne bouge pas : c'est son urgence qui était fausse.

Module PUR (aucun I/O, aucun import du paquet) : `schema.py` l'importe en tête, le
store lui remet un `Forcage` déjà tranché.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Le nom du paramètre, dans les DEUX faces (MCP `data_write`, REST `?…=true`). Cité par
# chaque refus, et par la description servie : une capacité qu'aucun texte n'annonce
# n'existe pas pour un agent — il retombe sur la manœuvre qu'on cherche à supprimer.
PARAMETRE = "readonly_override"

# Ce que le journal garde d'un forçage, borné. Un lot force autant de lignes qu'il en
# porte ; la ligne de journal, elle, doit rester lisible et insérable.
MAX_RELEVE = 25
MAX_VALEUR = 120

# La phrase du palier, citée par les deux REFUS.
#
# ⚠️ **Ce commentaire disait « une seule fois : […] la description servie aussi ». La
# seconde moitié était fausse** (mesuré le 07/09/2026) : `PALIER` n'a AUCUNE référence
# hors de ce fichier, et les deux descriptions servies re-rédigent le palier chacune de
# son côté — l'une en anglais dans `tools/datastore.py` (« open to the OWNER of the
# table (you, your org or your team) or to whoever GOVERNS it »), l'autre en français
# dans `capabilities/datastore/rows.py` (« Réservé au propriétaire du tableau ou à qui
# le gouverne »). Trois formulations, exactement le risque que la phrase annonçait
# éviter — et une promesse creuse est pire qu'un doublon assumé, parce que celui qui
# la lit croit n'avoir qu'un endroit à changer.
#
# ⚠️ **Donc : le jour où le palier bouge, TROIS endroits changent** — ici, la docstring
# anglaise de `data_write` (face MCP), et `_FORCAGE` dans `rows.py` (face REST). Elles
# ne se fusionnent pas telles quelles : deux sont dans des langues différentes, et les
# descriptions d'outils MCP sont des docstrings littérales, non interpolées. Les unifier
# demande de construire ces descriptions, ce qui est un lot, pas une retouche.
PALIER = ("le PROPRIÉTAIRE du tableau (toi, ton org ou ton équipe) ou celui qui le "
          "GOUVERNE")


def _borne(valeur: Any) -> Any:
    """La valeur telle que le journal la gardera : les scalaires JSON tels quels, tout
    le reste stringifié et coupé. Le journal doit dire CE QUI a été remplacé, pas
    reporter une fiche entière dans une colonne d'audit."""
    if valeur is None or isinstance(valeur, (bool, int, float)):
        return valeur
    texte = valeur if isinstance(valeur, str) else str(valeur)
    return texte[:MAX_VALEUR]


@dataclass
class Forcage:
    """Le forçage demandé sur CET appel, et s'il est tenu.

    `autorise` est tranché par le store AVANT toute transaction : le palier coûte une
    lecture d'ownership, et l'évaluer dans le `_apply` du verrou de ligne ouvrirait une
    seconde connexion pendant qu'on tient un `FOR UPDATE` — la forme exacte du gel de
    prod du 02/09/2026. Ici, c'est déjà un booléen.

    Un SEUL objet par appel : un lot force plusieurs lignes sous le même geste, et le
    relevé qui part au journal est celui de l'appel entier."""

    demande: bool = False
    autorise: bool = False
    forcees: list = field(default_factory=list)
    #: Les chemins nommés par `force: [...]` (oto#140). `None` = le geste vaut pour
    #: TOUT l'appel — la forme historique du booléen.
    #:
    #: ⚠️ **Nommer par champ améliore la PORTÉE, pas la retenue, et le contrat le dit
    #: lui-même** : mesuré sur 57 appels portant le forçage, 56 étaient à « oui ». Tout
    #: paramètre offert au modèle sera réglé par lui. Ce qui TIENT un forçage, c'est
    #: son palier — ce que nommer les cibles change, c'est qu'un lot de cinq cents
    #: lignes cesse de forcer tout ce qu'il porte.
    chemins: Optional[frozenset] = None

    @property
    def actif(self) -> bool:
        """Le geste est-il tenu ? — sans regarder la cible. Utile pour savoir qu'un
        forçage est en cours ; **ne décide d'aucune écriture** : c'est `actif_sur` qui
        tranche, parce qu'un forçage peut être tenu et ne pas viser cette colonne."""
        return self.demande and self.autorise

    def actif_sur(self, colonne: str) -> bool:
        """Ce forçage vise-t-il CETTE colonne ?

        Sans `force`, il vise tout (compatibilité du booléen). Avec, il ne vise que ce
        qui est nommé — la colonne elle-même (`raison_sociale`) ou l'une de ses couches
        (`raison_sociale.origine`), parce que forcer une valeur et forcer sa provenance
        ne sont pas le même geste et ne devraient pas se demander ensemble.
        """
        if not self.actif:
            return False
        if self.chemins is None:
            return True
        return colonne in self.chemins or any(
            c.split(".", 1)[0] == colonne for c in self.chemins)

    def relever(self, colonne: str, avant: Any, apres: Any) -> None:
        """Note une substitution. La LIGNE est agrafée après coup (`rattacher`) : la
        décision se prend sur le payload et la ligne en place, sans connaître son
        identifiant.

        Une entrée encore SANS ligne et de même colonne est remplacée, pas ajoutée :
        `datastore_merge_row_locked` documente que son `_apply` peut être rejoué, et
        deux entrées pour un seul remplacement se liraient comme deux forçages."""
        if any(self._reprendre(e, colonne, avant, apres) for e in self.forcees):
            return
        if len(self.forcees) >= MAX_RELEVE:
            return
        self.forcees.append({"row": None, "col": colonne,
                             "was": _borne(avant), "now": _borne(apres)})

    @staticmethod
    def _reprendre(entree: dict, colonne: str, avant: Any, apres: Any) -> bool:
        if entree.get("row") is not None or entree.get("col") != colonne:
            return False
        entree["was"], entree["now"] = _borne(avant), _borne(apres)
        return True

    def rattacher(self, row_id: Optional[str]) -> None:
        """Agrafe la ligne aux substitutions relevées depuis la précédente. Appelée par
        le store dès qu'une écriture aboutit — donc jamais sur un geste refusé."""
        for entree in self.forcees:
            if entree.get("row") is None:
                entree["row"] = None if row_id is None else str(row_id)

    def releve(self) -> list:
        """Ce qui part au journal — les seules entrées rattachées à une ligne écrite."""
        return [e for e in self.forcees if e.get("row") is not None]


def arbitrer(forcage: Optional[Forcage], colonne: str,
             avant: Any, apres: Any, *, colonne_formule: bool = False) -> Optional[str]:
    """Cette écriture sur colonne verrouillée passe-t-elle ? — `None` = elle passe (et
    elle est relevée), sinon le refus, qui NOMME le geste.

    Trois sorties, et les trois disent où va la chose ET qui peut forcer. Un refus
    exact mais sans issue fait deviner exactement comme un refus muet (#668).

    `colonne_formule=True` (oto-backend#1008) court-circuite les trois sorties
    ci-dessous : une colonne CALCULÉE n'est pas « du fichier source » (le forçage
    n'y a jamais eu de sens — même écrite de force, elle serait écrasée au
    prochain recalcul, à la prochaine écriture d'une colonne d'entrée)."""
    if colonne_formule:
        return (
            f"`{colonne}` est une colonne CALCULÉE (formule) — jamais écrite "
            f"directement, elle se recalcule automatiquement quand les colonnes "
            f"dont elle dépend changent. `{PARAMETRE}` n'y change rien : forcer "
            f"une valeur ici n'a pas de sens, elle serait remplacée au prochain "
            f"recalcul. Écris plutôt les colonnes d'ENTRÉE de la formule.")
    ou_va = (f"Ce que dit une autre source va dans `{colonne}.comment` "
             f"({{\"{colonne}\": {{\"comment\": …}}}})")
    if forcage is not None and forcage.actif_sur(colonne):
        forcage.relever(colonne, avant, apres)
        return None
    if forcage is not None and forcage.actif:
        # Le geste est TENU mais ne vise pas cette colonne : le dire, plutôt que de
        # rendre le refus général. Sans cette branche, l'appelant relit « forcer est
        # réservé à … » alors qu'il A le palier — et il chercherait un droit qu'il
        # possède déjà, au lieu de corriger sa liste.
        return (
            f"`{colonne}` est verrouillée (`readonly`) et ta liste `force` ne la "
            f"nomme pas — rien n'a été écrit sur elle. Ajoute-la (`force=[…, "
            f"\"{colonne}\"]`) si tu voulais la remplacer ; {ou_va} sinon. "
            f"Ce que tu as nommé a été forcé normalement.")
    if forcage is not None and forcage.demande:
        # Le palier n'est pas tenu. Ne pas répéter « passe le paramètre » : il est
        # passé, et le redire enverrait chercher une manœuvre pour l'obtenir.
        return (
            f"`{colonne}` est une colonne du fichier source, non modifiable "
            f"(`readonly`) — rien n'a été écrit, et `{PARAMETRE}` n'y change rien "
            f"ici : forcer est réservé à {PALIER}, jamais à un simple accès en "
            f"écriture partagé — sinon le verrou ne protégerait de personne. "
            f"{ou_va}, ou demande la correction à qui possède le tableau.")
    return (
        f"`{colonne}` est une colonne du fichier source, non modifiable "
        f"(`readonly`) — rien n'a été écrit. {ou_va} ; la valeur reste celle du "
        f"fichier. Pour la REMPLACER malgré le verrou : `{PARAMETRE}=true` sur CET "
        f"appel, ouvert à {PALIER}. Il ne vaut que pour cet appel — il n'y a rien à "
        f"rouvrir dans le schéma, donc rien à refermer.")


#: Le nom servi du forçage par CIBLES (oto#140). Il coexiste avec le booléen pendant
#: le préavis : nommer ses cibles est un geste plus précis, pas un droit différent.
PARAMETRE_CIBLES = "force"


def chemins_forces(value: Any) -> Optional[frozenset]:
    """`force=[...]` validé, ou un refus qui NOMME le paramètre et sa forme.

    Rend `None` quand rien n'est demandé — le forçage garde alors sa portée d'appel
    entier, la forme historique du booléen.

    ⚠️ Une liste VIDE est refusée, pas lue comme « rien ». `force=[]` est un geste
    délibéré qui ne veut rien dire : le traiter comme une absence ferait passer une
    écriture sur colonne verrouillée pour un refus ordinaire, et l'appelant chercherait
    un droit qu'il vient de demander.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(
            f"`{PARAMETRE_CIBLES}` attend une liste non vide de chemins — reçu "
            f"{value!r}. Un chemin est une colonne (`raison_sociale`) ou l'une de ses "
            f"couches (`raison_sociale.origine`). Nommer ce qu'on force évite qu'un "
            f"lot de cinq cents lignes force tout ce qu'il porte.")
    mauvais = [c for c in value if not isinstance(c, str) or not c.strip()]
    if mauvais:
        raise ValueError(
            f"`{PARAMETRE_CIBLES}` : {mauvais!r} n'est pas un chemin. Attendu des "
            f"chaînes — `\"raison_sociale\"` ou `\"raison_sociale.origine\"`.")
    return frozenset(c.strip() for c in value)


def description_parametre_cibles() -> str:
    """Ce que les DEUX faces disent du paramètre. Une seule source, comme le reste.

    ⚠️ Le texte dit ce que ça change ET ce que ça ne change PAS. Le contrat le note
    lui-même : nommer par champ améliore la PORTÉE, pas la retenue — 56 des 57 appels
    portant le forçage étaient à « oui ». Laisser croire qu'on a resserré un droit
    ferait relâcher l'attention sur le seul mécanisme qui le tient vraiment, le palier.
    """
    return (f"`{PARAMETRE_CIBLES}=[\"colonne\", \"colonne.origine\"]` force les "
            f"colonnes NOMMÉES sur cet appel, au lieu de tout ce qu'il porte. Le "
            f"nommer suffit : pas besoin de `{PARAMETRE}` en plus. ⚠️ Ça change la "
            f"PORTÉE, pas le droit — forcer reste réservé à {PALIER}, et un lot qui "
            f"nomme ses cibles cesse seulement de forcer les cinq cents lignes qu'il "
            f"transporte. Une colonne verrouillée absente de la liste est refusée "
            f"normalement, et le refus le dit.")
