"""Les refus du datastore — un vocabulaire d'erreurs, sans dépendance.

Extrait du store (#325), déplacement pur. Ce module ne connaît rien : c'est ce qui lui
permet d'être importé de partout, y compris par les modules que le store COMPOSE. Sans
lui, chacun d'eux devait remonter vers le store pour lever une erreur — donc un cycle,
donc un import local par fonction.

Le point commun de toutes ces classes : **un refus porte de quoi agir**. Les champs
fautifs, le titulaire d'un bail et sa date d'expiration, la façon de le libérer. Un
refus qui dit seulement « non » fait deviner, et on l'a payé en production : le refus
d'écriture sur une ligne réservée ressortait en « erreur interne », alors que dire qui
la tenait était l'objet même du mécanisme.
"""
from __future__ import annotations

from typing import Any, Optional


class RowValidationError(ValueError):
    """Écriture refusée par le schéma strict / le cycle de vie (ADR 0046 B/C).
    Le message liste les champs fautifs — actionnable, jamais un refus muet.

    `row` = la DÉSIGNATION de la ligne fautive, quand le geste en visait plusieurs
    (#412). Un lot de 200 lignes qui refuse en nommant le champ et la valeur, mais
    pas la ligne, fait chercher à la main dans un fichier client de 8 910 lignes :
    le coût n'est pas les lignes non écrites, c'est le temps de trouver la fautive.
    Le store la connaît — il valide ligne par ligne — l'information existait et ne
    sortait pas.

    `details` = ce que le refus a de STRUCTURÉ, quand une phrase ne suffit pas au
    client pour agir (#545) : aujourd'hui `expected_column`, la colonne où la valeur
    aurait dû atterrir. La face REST le rend dans son enveloppe d'erreur (via
    `AuthzDenied.details`) — un front peut alors POINTER le bon champ au lieu de
    reparser une phrase française, ce qui serait un contrat déguisé. La face MCP,
    elle, n'a pas d'enveloppe structurée : le message doit rester suffisant seul."""

    def __init__(self, errors: list[str], *, row: Optional[str] = None,
                 details: Optional[dict] = None):
        self.errors = errors
        self.row = row
        self.details = details or None
        tete = "écriture refusée par le schéma"
        if row:
            tete += f" · {row}"
        super().__init__(tete + " : " + " ; ".join(errors))


class BusinessKeyRequired(ValueError):
    """Écriture refusée sur un tableau qui n'accepte que des écritures VISANT une
    ligne existante (`schema.key_required`, #516).

    Le cran est OPT-IN, posé par le propriétaire du tableau. Ce qu'il ferme : une
    écriture qui ne désigne aucune ligne — ni par son identifiant, ni par une valeur
    de clé métier que le tableau porte — CRÉAIT une ligne, et le seul signal était un
    `notices` dans la réponse. Deux incidents datés : une 8 911ᵉ ligne sans `siren`
    (28/08), puis deux entreprises FICTIVES nées d'un SIREN inconnu au registre après
    qu'un identifiant inventé eut été refusé (29/08). Une clé n'empêche rien tant
    qu'elle peut être inconnue.

    ⚠️ Dérive de `ValueError` : la face MCP traduit toute `ValueError` d'écriture en
    INVALID_PARAMS actionnable. Sans cet héritage, le refus ressortirait en « Erreur
    interne du serveur » — le défaut déjà payé sur `RowLocked`.

    Le refus porte de quoi AGIR : la clé, la valeur refusée quand il y en a une, et
    le geste (viser la ligne par son identifiant). `row` = la désignation de la ligne
    fautive quand le geste en visait plusieurs, comme `RowValidationError` (#412)."""

    def __init__(self, message: str, *, key: str, namespace: Optional[str] = None,
                 value: Any = None, row: Optional[str] = None):
        # Le motif NU est conservé : le batch reconstruit le même refus en lui
        # ajoutant sa désignation de ligne, sans reformuler le message.
        self.motif = message
        self.key = key
        self.namespace = namespace
        self.value = value
        self.row = row
        super().__init__(f"{row} : {message}" if row else message)


class ColumnAbsent(ValueError):
    """Purge de colonne qui n'a touché AUCUNE ligne (#680) — nommée, pas seulement
    dite.

    Le message reste celui de `_rien_purge` : il distingue déjà la faute de frappe de
    l'annotation (`site_web.comment`), et il est suffisant seul pour la face MCP, qui
    n'a pas d'enveloppe structurée. Ce qui manquait est pour la face REST, donc pour
    le cockpit : sa suppression de colonne est en DEUX temps (retirer le champ du
    schéma, puis purger les données), et le cas normal d'une colonne fraîchement
    ajoutée puis retirée est précisément « aucune ligne ne la portait ». Sans code
    distinct, le front devait soit crier au rouge sur un geste parfaitement abouti,
    soit reconnaître une phrase française — un contrat déguisé, exactement ce que
    `RowValidationError.details` existe pour éviter.

    Reste une `ValueError` : c'est ce qui garde le refus actionnable côté MCP plutôt
    que de le laisser ressortir en « erreur interne » (même raison que
    `BusinessKeyRequired`)."""


class InvalidCursor(ValueError):
    """Curseur de pagination illisible (mal formé / tronqué)."""


class NamespaceNotFound(Exception):
    """Le nom ne désigne aucun tableau VISIBLE dans l'org de l'appel.

    `indice` (#631) : ce que le refus a de plus à dire quand le tableau existe bel et
    bien — dans une autre org du même appelant — et que c'est l'org de l'appel qui ne
    convient pas. Calculé par `hors_org.indice_autre_org` au moment du refus, rendu tel
    quel par la face MCP ; None quand il n'y a rien d'utile (le nom n'existe nulle
    part) — une piste vide vaut mieux qu'une phrase qui meuble."""

    def __init__(self, namespace: Optional[str] = None, *, indice: Optional[str] = None):
        self.namespace = namespace
        self.indice = indice
        super().__init__(*([namespace] if namespace is not None else []))


class RowNotFound(Exception):
    pass


class NamespaceExists(Exception):
    pass


class NamespaceReadOnly(Exception):
    """Écriture tentée sur un namespace partagé en lecture seule."""
    pass


class NamespaceForbidden(Exception):
    """Action de gouvernance (supprimer/transférer) tentée sans droit de gouvernance."""
    pass


class RowLocked(ValueError):
    """Écriture refusée sur une ligne sous bail ACTIF d'un autre (#317).

    ⚠️ **Dérive de `ValueError` depuis le 05/09/2026, et c'est un correctif.** Elle
    héritait d'`Exception` : aucune face REST ne l'attrapait, donc ce refus JUSTE
    sortait en « 500, corps vide » — l'appelant ne savait ni qu'il s'agissait d'un
    bail, ni qui le tenait, ni quoi faire. La face MCP, elle, traduisait correctement
    depuis toujours. Une session a cru à une perte de données à cause de ça, et il a
    fallu une épreuve complète pour établir qu'il ne s'était rien passé.

    ⚠️ **Le code le SAVAIT** : la docstring de `BusinessKeyRequired`, écrite avant,
    nomme ce cas — « sans cet héritage, le refus ressortirait en Erreur interne du
    serveur — *le défaut déjà payé sur RowLocked* ». Un défaut connu, nommé à côté, et
    laissé en place. Le savoir n'a jamais refusé personne.

    Le bail protégeait l'ATTRIBUTION, pas la donnée : deux agents ne prenaient pas la
    même ligne, mais rien n'empêchait le second d'écrire dessus. « Verrou natif » veut
    dire que la ligne réservée est aussi protégée en écriture.

    ⚠️ Porte de quoi SORTIR, pas seulement de quoi comprendre : qui tient, jusqu'à
    quand, et le geste — libérer explicitement, puis écrire. Sans la sortie, on
    remplace un silence par un mur."""

    def __init__(self, row_id: str, claimed_by: Any = None, claimed_until: Any = None,
                 claimed_run: Any = None, row: Optional[str] = None):
        self.row_id = row_id
        self.claimed_by = claimed_by
        self.claimed_until = claimed_until
        # La désignation de la ligne dans un LOT (#412), comme `BusinessKeyRequired` :
        # le batch reconstruit le même refus en lui ajoutant OÙ il s'est arrêté et
        # combien de lignes étaient déjà écrites. ⚠️ Sans ça, le lot le ré-emballait
        # en `ValueError` nue et le refus PERDAIT sa classe — donc son code 409 et son
        # message de bail, pour redevenir un « entrée invalide » qui n'apprend rien.
        self.row = row
        # Le RUN qui tient le bail (#547). Porté sur l'exception — jamais dans le
        # message : le publier ferait du verrou une étiquette, puisqu'un `_run_id=`
        # n'autorise rien, il NOMME. La surface s'en sert pour un seul test, qui
        # n'apprend rien à un tiers : « ce run est-il le tien ? » (cf.
        # `tools/datastore._omitted_run_hint`).
        self.claimed_run = claimed_run
        motif = (
            f"ligne « {row_id} » réservée par « {claimed_by} » jusqu'à "
            f"{claimed_until} — écriture refusée. Si le travail est terminé ou "
            f"l'agent abandonné, libère la ligne (data_release), puis écris.")
        self.motif = motif
        super().__init__(f"{row} : {motif}" if row else motif)


class ClaimedRefUnresolved(ValueError):
    """`@claimed` n'a pas pu désigner une ligne — et le refus dit laquelle manque (#517).

    L'alias existe parce que recopier trente-deux caractères aléatoires est une tâche
    à laquelle un agent échoue : mesuré sur trois passages d'une campagne, il altère
    l'identifiant ou en fabrique un dans une convention étrangère. Ce qui suit le refus
    coûte plus cher que le refus — l'agent réessaie sans identifiant, et une écriture
    sans identifiant CRÉE au lieu de corriger.

    D'où la règle de ces messages : **jamais « non » tout court**. Ne rien tenir se dit
    en nommant le geste qui réserve ; tenir ailleurs se dit en nommant le tableau qui
    tient — c'est le cas qui a écrit des fiches d'essai dans le fichier d'une cliente,
    et l'agent avait l'information sans la voir. Tenir plusieurs lignes se dit en les
    nommant plutôt qu'en en choisissant une : écrire sur la mauvaise ligne d'un fichier
    client ne se voit sur aucun écran.

    ⚠️ Ce n'est pas une propriété par identifiant (#546, refusée) : sans jeton de run,
    l'alias REFUSE. Il ne consolide rien, il rend lisible ce que le serveur sait déjà."""


class RowClaimed(Exception):
    """Row nommée déjà sous bail ACTIF d'un autre worker (ADR 0046 D).

    Le conflit qu'il faut rendre visible : deux personnes qui prennent la même
    ligne à la même seconde, l'une des deux doit l'apprendre. Porte le bail en
    place pour que la surface dise QUI la tient et jusqu'à QUAND."""

    def __init__(self, row_id: str, claimed_by: Any = None, claimed_until: Any = None):
        self.row_id = row_id
        self.claimed_by = claimed_by
        self.claimed_until = claimed_until
        super().__init__(f"row {row_id} sous bail de {claimed_by!r} jusqu'à {claimed_until!r}")
