"""Guide & instructions d'ORG (ADR 0009) — domaine migré en capacités.

Miroir d'`groups_guide` au grain org. Une opération co-déclarée une fois, ses
deux faces (MCP + REST) dérivées par les adaptateurs → fin de la duplication
d'autz `_resolve_org_write` (MCP) vs `_active_org_edit` (REST).

Deux paliers, par combinateur d'autz (pas de branche `org_id` à la main) :
- **membre** : scopé à l'**org active** (`org_id` injecté depuis l'état serveur).
  Lecture = `ORG_MEMBER`/`SUB_ONLY` ; écriture = `ORG_ADMIN`. Chemins `/api/me/*`,
  outil console `oto_procedure` (op=get/list/set/delete, ADR 0047 — ex 4 tools par-verbe).
- **admin** : org ciblée par `org_id` (cross-org = platform admin via l'escalade
  `roles`). Lecture = `ORG_MEMBER_OF` ; écriture = `ORG_ADMIN_OF`. Chemins
  `/api/admin/orgs/{id}/*`, outil `oto_admin_guide`.

Les handlers lisent `ctx.org_id` / `ctx.group_id` (injectés par l'autz) →
**partagés** entre les deux paliers. Modèle versionné (slug réservé `claude_md` =
guide de base).

Le palier **équipe** n'est plus seulement lisible : depuis #681, `scope='group'`
écrit et supprime aussi (chef d'équipe), sur le MÊME store et la MÊME forme de
réponse — la clé de scope changeant de nom (`group_id` au lieu d'`org_id`), comme
partout ailleurs dans ce module. La face REST `/api/groups/{id}/instructions*`
(`groups/guide.py`) reste, avec ses propres modèles de sortie.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ... import (access, db, deprecations, group_store, guide_store, org_store,
                procedure_diagram, procedure_digest, procedure_retrait, roles,
                slots as slots_mod, tool_registry)
from .._authz import (ORG_ADMIN, ORG_ADMIN_OF, ORG_ADMIN_OPT, ORG_MEMBER,
                      ORG_MEMBER_OF, SUB_ONLY, capacite_autorise)
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                      RestBinding)
from ..registry import CAPABILITIES

logger = logging.getLogger(__name__)

# ── Les droits ANNONCÉS par le bundle d'org ─────────────────────────────────
#
# Même forme qu'au palier équipe (`groups/guide.py`) : chaque drapeau nomme la
# capacité dont il rend la règle, et c'est cette règle-là qu'on exécute. Ici les deux
# valent `org_admin` aujourd'hui — le palier org n'a pas été redécoupé par #695 — mais
# ils sont servis SÉPARÉMENT quand même : sans ça, « qui peut annoter » redeviendrait
# une propriété du palier, et un écran factorisé sur les deux surfaces devrait deviner
# lequel des deux sens `can_edit` porte selon la page où il est.
_DROITS_SERVIS = {
    "can_write_instructions": "org.instruction.set",
    "can_delete_instructions": "org.instruction.delete",
}

_OID = {"id": "org_id"}
_OID_SLUG = {"id": "org_id", "slug": "slug"}
# Borne du corps d'une PROCÉDURE. Relevée de 64 à 128 Ko le 03/09/2026 (décision
# d'Alexis : « fais une amélioration sur le mode http »), pour débloquer une mission
# dont la procédure était à SEPT octets de l'ancienne — chaque nouvelle leçon y
# chassait une ancienne.
#
# Pourquoi 128 Ko et pas plus : une procédure n'est PAS injectée à chaque session
# (le README l'est, et ce handler le refuse explicitement quelques lignes plus bas),
# elle est chargée À LA DEMANDE dans le contexte d'un agent. Le critère est donc
# « tient-elle dans un contexte utile, avec de quoi travailler autour ». 128 Ko de
# français ≈ 34 000 jetons (ratio mesuré le 03/09 : 3,78 octets par jeton) — environ
# un sixième d'une fenêtre de 200 k, un quart d'une fenêtre de 128 k. Une procédure au
# plafond reste chargeable sans manger la place du travail. Doubler plutôt que
# décupler est délibéré : la borne doit rester un signal qu'il est temps de découper.
#
# ⚠️ Cette garde est COMMUNE aux deux faces — `org.instruction.set` (REST),
# `.create` et `.admin_set` passent tous par `_set_instruction`, que `oto_procedure`
# atteint aussi via l'adaptateur MCP. La relever la relève donc PARTOUT, pas
# seulement sur la route. Constaté avant de la toucher, pas après.
#
# ⚠️ Et ce n'est PAS la jumelle de `guides._MAX_BODY_BYTES`, qui garde 64 Ko : ce
# corps-là est injecté dans CHAQUE session, sa borne protège un budget réel. Même
# valeur hier, deux raisons différentes — c'est la raison qui décide, pas la symétrie.
_MAX_BODY_BYTES = 128 * 1024

_BASE = org_store.BASE_SLUG
# Outil MCP qui charge le guide (donc loggé dans `tool_calls`) → c'est lui que
# l'usage compte. UNE source pour le nom : sert de `mcp=` de la capacité de lecture
# ET de filtre dans `_instruction_usage` → plus de chaîne magique à dériver (le bug
# d'origine : un filtre sur un nom d'outil mort renvoyait toujours 0).
_GUIDE_GET_TOOL = "oto_procedure"
# Fait d'OUVERTURE d'un déroulé. Même journal que ci-dessus, autre verbe :
# `_runs_from_journal` reconstruit les runs depuis ces mêmes lignes, ce qui est
# précisément pourquoi l'usage peut les compter sans nouvelle table ni nouveau
# chemin d'écriture. La CLÉ d'`args` qui y nomme la procédure vit chez le lecteur
# du journal (`db.usage._ARG_PROCEDURE`) et n'est pas recopiée ici — une clé servie
# ne se renomme pas d'un côté sans l'autre.
_RUN_START_TOOL = "run_start"


# ── Sorties ─────────────────────────────────────────────────────────────────
# Vocabulaire, parce qu'il piège : une **procédure** (skill nommée, versionnée) est
# un objet de CE module ; le **readme d'org** est un GUIDE `delivery='init'` (ADR
# 0042) qui vit ailleurs. Le slug réservé `claude_md` désigne le second — d'où une
# asymétrie visible plus bas : la liste l'annonce, `get` ne le sert pas.

class ReferencedTool(BaseModel):
    """Un `<tool:slug>` du corps, résolu **à la lecture** contre le registre vivant
    (ADR 0014). `status='missing'` = la référence ne désigne plus rien (outil renommé
    ou non monté) : le corps n'a pas changé, sa résolution si. Une entrée `missing`
    ne porte que `name` + `status` ; une entrée `ok` porte la fiche de l'outil."""
    name: str
    status: str


class GuideView(BaseModel):
    """⚠️ **Trois formes derrière une capacité**, choisies par l'ENTRÉE :

    1. `guide_id` fourni → une procédure par son id STABLE (le seul chemin qui
       traverse les orgs : l'accès passe par le seam ownership, donc une procédure
       PARTAGÉE à toi par une autre org est lisible ici). **C'est la seule forme que
       la face REST peut produire** — `guide_id` y est un segment de chemin.
    2. `slug` omis (MCP) → le *bundle de session* : readme d'org + readme d'équipe +
       index des procédures.
    3. `slug` fourni (MCP) → une procédure nommée.

    ⚠️ La clé de scope CHANGE de nom selon la forme : `org_id` en scope org,
    `group_id` en scope équipe — pas un champ nul, un champ absent.

    ⚠️ Sans org active, la forme 2 répond **200 avec un bundle vide** (`org_id: null`,
    `guide: ""`, `guides: []`), pas une erreur : `guide: ""` confond « pas d'org » et
    « org sans readme ».

    ⚠️ **Chaque clé est servie DEUX FOIS** le temps du préavis (#519) : sous son nom
    d'aujourd'hui (`guide_id`, `guide`, `group_guide`, `guides`) et sous celui
    d'hier, qui s'en va le 29/10/2026 — cf. `docs/alias-deprecies.md`. Écris le
    nouveau ; l'ancien est là pour ne casser personne, pas pour être choisi."""
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    guide_id: Optional[int] = None
    doctrine_id: Optional[int] = None   # ALIAS déprécié (retrait 29/10/2026)
    scope: Optional[str] = None
    slug: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    version: Optional[int] = None
    body_md: Optional[str] = None
    # Entités requises déclarées (ADR 0035), citées `<slot:name>` dans la prose.
    #
    # ⚠️ Ce que ce champ dit et ne dit PAS (#658) : il porte ce qu'il FAUT brancher,
    # jamais ce qui EST branché. Un slot n'a pas d'état de branchement dans l'absolu —
    # il en a un PAR PROJET, et le binding nom→instance vit sur le projet
    # (`project_links.slot`, écrit par `oto_project op=link`, lu par `oto_project
    # op=get`). Une même procédure branchée dans deux projets a donc deux réponses,
    # et cette fiche-ci n'en connaît aucune.
    slots: Optional[list[slots_mod.SlotDecl]] = None
    referenced_tools: Optional[list[ReferencedTool]] = None
    # Forme 2 seulement : le readme d'org (prose plate), son org, son équipe active.
    org: Optional[str] = None
    guide: Optional[str] = None
    doctrine: Optional[str] = None        # ALIAS déprécié (retrait 29/10/2026)
    group: Optional[str] = None
    group_guide: Optional[str] = None
    group_doctrine: Optional[str] = None  # ALIAS déprécié (retrait 29/10/2026)
    # Index (slug/title/description/scope) — SANS les corps.
    guides: Optional[list[dict]] = None
    doctrines: Optional[list[dict]] = None  # ALIAS déprécié (retrait 29/10/2026)
    # Présent seulement s'il y a un projet actif : les entités du projet contre
    # lesquelles résoudre les `<slot:>`. Dérivé best-effort — son ABSENCE peut donc
    # aussi vouloir dire « la dérivation a échoué », pas seulement « hors projet ».
    project_instance: Optional[dict] = None
    # Forme 3 avec `with_history=true` (org comme équipe depuis #681).
    versions: Optional[list[dict]] = None


class GuideMeta(BaseModel):
    """État du readme d'org. ⚠️ **`version` est un faux compteur** : il vaut 1 s'il
    existe un readme, 0 sinon, et n'atteint JAMAIS 2 — le readme est de la prose plate
    sans historique (ADR 0042). L'afficher comme un numéro de révision promet un
    versionnage qui n'existe pas."""
    exists: bool
    version: int
    updated_at: Optional[str] = None


class InstructionIndexEntry(BaseModel):
    """Métadonnées d'une procédure — **sans le corps** (`body_md` s'obtient par
    `GET /api/me/instructions/{slug}`)."""
    id: int
    slug: str
    title: Optional[str] = None
    description: Optional[str] = None
    version: int
    updated_at: Optional[str] = None


class InstructionsBundle(BaseModel):
    """Readme + index des procédures de l'ORG ACTIVE.

    ⚠️ **Sans org active, c'est un 200 avec tout à vide** (`org_id: null`,
    `can_edit: false`, `guide.exists: false`, `instructions: []`) — pas un 400.
    Indiscernable, à la lecture, d'une org réelle qui n'aurait rien écrit.

    ⚠️ **`instructions` exclut le readme** (slug réservé `claude_md`), qui n'est décrit
    que par `guide` (servi aussi sous son nom d'hier, `doctrine`, jusqu'au
    29/10/2026). Et l'asymétrie va plus loin : `guide.exists: true` annonce
    un readme que `GET /api/me/instructions/claude_md` **ne sait pas servir** (404) —
    le readme se lit sur la surface guide, pas ici.

    ⚠️ **`can_edit` est le droit d'administrer l'org** — et c'est LUI qui gouverne
    l'écriture du readme sur la surface guide (`PUT …/guides/{scope}/{slug}`, garde
    org_admin), pas celui d'écrire une procédure : ce sont `can_write_instructions` et `can_delete_instructions` qui
    répondent à cette question-là, ici comme au palier équipe. À l'org les deux valent
    la même chose aujourd'hui (org_admin des deux côtés) ; à l'équipe, non — un membre
    écrit, seul le chef supprime. **Un écran qui sert les deux paliers doit lire les
    mêmes deux champs**, sinon le droit d'annoter redevient une propriété de la page."""
    org_id: Optional[int] = None
    org_name: Optional[str] = None
    can_edit: bool
    # Cf. `_DROITS_SERVIS` : rendus par les règles d'autz déclarées, pas recopiés.
    can_write_instructions: bool = Field(description=(
        "Peut créer, modifier et restaurer une procédure de cette org : org_admin. "
        "⚠️ Au palier ÉQUIPE le même champ est vrai pour tout membre — c'est la "
        "garde qui diffère, pas le champ."))
    can_delete_instructions: bool = Field(description=(
        "Peut supprimer une procédure de cette org ET tout son historique "
        "(irréversible) : org_admin."))
    guide: GuideMeta
    doctrine: GuideMeta   # ALIAS déprécié, retrait le 29/10/2026 (#519)
    instructions: list[InstructionIndexEntry]


class InstructionView(BaseModel):
    """Une procédure, corps compris. `slug` est le slug NORMALISÉ (l'entrée est
    tolérante).

    ⚠️ **`updated_at: null` ne veut pas dire « jamais modifiée »** : c'est le signe
    qu'on lit une VERSION ARCHIVÉE (`?version=N`), servie depuis la table des
    révisions, qui ne porte pas cette colonne. Sur la version courante, elle est
    toujours renseignée."""
    slug: str
    title: Optional[str] = None
    description: Optional[str] = None
    version: int
    body_md: str
    slots: list[slots_mod.SlotDecl]
    set_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InstructionVersion(BaseModel):
    version: int
    title: Optional[str] = None
    set_by: Optional[str] = None
    created_at: Optional[str] = None


class InstructionVersions(BaseModel):
    """Historique d'une procédure, plus récente d'abord.

    ⚠️ **Une liste vide recouvre trois situations distinctes** et rend 200 dans les
    trois : le slug n'existe pas (aucun 404 n'est levé ici), c'est le readme (qui n'a
    par nature pas d'historique), ou la procédure n'a encore aucune révision archivée.
    Il faut `GET /api/me/instructions/{slug}` pour trancher."""
    slug: str
    versions: list[InstructionVersion]


class InstructionUsage(BaseModel):
    """Usage d'une procédure, dérivé du journal d'appels (ADR 0014).

    ⚠️ **`count` et `series` ne mesurent pas la même fenêtre** : `series` couvre les 30
    derniers jours, `count` et `callers` n'ont **aucun filtre de date** — ils comptent
    tout ce qui reste en base. `count` ≠ `sum(series)`, et l'écart n'est pas un bug.

    ⚠️ **`callers` peut être plus court que ce que `count` totalise** : les appelants
    sans compte `users` connu sont exclus de la liste mais comptés dans le total.

    ⚠️ **Sur le slug du readme (`claude_md`), le filtre par procédure disparaît** : le
    compte devient celui de TOUS les chargements de procédure de l'org, quelle qu'elle
    soit. Ce n'est pas l'usage d'un document, c'est le volume d'une surface.

    Autres bornes : seuls les appels RÉUSSIS comptent, et le périmètre est celui des
    membres ACTUELS de l'org — le départ d'un membre efface rétroactivement ses
    chargements."""
    slug: str
    count: int
    # Emails des appelants, du plus actif au moins actif.
    callers: list[str]
    # Exactement 30 entiers, du plus ancien au plus récent (jour UTC). Les jours sans
    # appel valent 0 — ici, contrairement au monitoring, la série est densifiée.
    series: list[int]
    # ── Déroulés (`run_start`), à ne PAS confondre avec les chargements ci-dessus ──
    # Un CHARGEMENT est « l'agent a ouvert la procédure » ; un DÉROULÉ est « l'agent
    # a déclaré l'exécuter ». Les deux vivent dans `tool_calls`, sous deux verbes, et
    # ils ne se déduisent pas l'un de l'autre : on peut lire sans dérouler, et un run
    # peut couvrir plusieurs lectures. Deux séries distinctes, jamais additionnées ni
    # tracées ensemble — les confondre est l'erreur que le front a déjà commise une
    # fois (compteur de chargements lu comme un compteur de runs).
    runs_count: int = 0
    # Même fenêtre et même densification que `series` : 30 jours, zéros compris.
    runs_series: list[int] = []


class InstructionWritten(BaseModel):
    """Écriture d'une procédure. Chaque écriture **incrémente la version** et archive
    un instantané ; il n'y a pas de mise à jour en place.

    Les checks croisés sont **non bloquants par conception** (ADR 0014/0035) : ils
    signalent le drift, ils ne refusent pas l'écriture. Donc `ok: true` avec des
    `unresolved_tools` ou des `unresolved_slots` non vides = **la procédure est
    enregistrée ET cassée**. C'est le seul endroit où ça se voit. `diagram_warning`
    (front tiers, issue #108) est du même régime : la procédure est enregistrée, mais sa
    page se rendra en état vide faute de schéma.

    `slots` renvoyé est l'état EFFECTIF après écriture (envoyer `slots: null` conserve
    l'existant, donc l'écho peut différer de ce qui a été posté)."""
    ok: bool
    # ⚠️ La clé de scope CHANGE de nom selon le palier écrit : `org_id` en scope org,
    # `group_id` en scope équipe (#681) — pas un champ nul, un champ absent. Même
    # convention que `GuideView` en lecture. `scope` dit lequel, sans avoir à deviner.
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    scope: Optional[str] = None
    slug: str
    # Le NOUVEAU numéro de version (jamais celui qu'on a envoyé).
    version: int
    # Constante d'écho : vaut toujours `true` quand la réponse existe.
    set: bool
    # Présent seulement si l'écriture était une restauration (`from_version`).
    reverted_from: Optional[int] = None
    referenced_tools: Optional[list[ReferencedTool]] = None
    # Refs `<tool:>` qui ne désignent plus rien — l'écriture a quand même eu lieu.
    unresolved_tools: Optional[list[str]] = None
    slots: Optional[list[slots_mod.SlotDecl]] = None
    # `<slot:name>` cité dans la prose sans déclaration correspondante.
    unresolved_slots: Optional[list[str]] = None
    # Déclaré mais jamais cité — l'inverse, tout aussi silencieux.
    unreferenced_slots: Optional[list[str]] = None
    slot_warnings: Optional[list[str]] = None
    # Même forme qu'un slot, PLUS le motif — d'où un modèle à part : typer ce champ
    # `list[SlotDecl]` tairait `reason`, qui est tout ce que la suggestion apporte.
    suggested_slots: Optional[list[slots_mod.SuggestedSlot]] = None
    # Le SCHÉMA de la procédure (front tiers, issue #108) : le front en fait la vue par
    # défaut de la page du process, donc une procédure sans dessin s'y affiche vide.
    # `None` = le check a tourné et n'a rien à dire ; la clé est toujours présente,
    # pour qu'un client sache distinguer « rien à signaler » d'un serveur trop vieux.
    diagram_warning: Optional[str] = None
    # Le DIGEST d'ouverture (`procedure_digest`) : ce que le dernier déroulé a appris.
    # Même régime — la procédure est enregistrée, il lui manque son bloc d'ouverture.
    digest_warning: Optional[str] = None
    # Ce que cette version RETIRE (`procedure_retrait`, oto#61). Le digest raconte ce
    # qu'on ajoute ; rien ne disait ce qu'on enlève, et une réécriture « resserrée »
    # enlève par construction. `None` = aucune SECTION entière n'a disparu — ce qui ne
    # veut pas dire que rien n'a été retiré.
    retrait_warning: Optional[str] = None


class InstructionDescribed(BaseModel):
    """Correction de la VITRINE d'une procédure (titre / description), corps intact.

    ⚠️ `version` est un numéro NEUF : corriger une description est une écriture, elle
    monte la version et archive un instantané comme les autres. C'est ce qui la rend
    réversible par `from_version` — et ce qui fait que la version lue avant la
    correction ne vaut plus pour un `expected_version` ultérieur.

    Pas de check croisé dans la réponse (`unresolved_slots`, `diagram_warning`,
    `digest_warning`) : ils portent tous sur le CORPS, que ce geste ne touche pas.
    Les rendre ici les ferait passer pour un verdict sur ce qui vient d'être écrit.

    `title`/`description` sont l'état APRÈS correction — celui qui n'était pas fourni
    est reconduit, donc l'écho dit la vitrine entière, pas seulement le champ changé.

    ⚠️ Comme à l'écriture, la clé de scope change de nom : `org_id` en scope org,
    `group_id` en scope équipe (#681)."""
    ok: bool
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    scope: Optional[str] = None
    slug: str
    version: int
    title: str
    description: str


class InstructionDeleted(BaseModel):
    """Suppression d'une procédure **et de tout son historique** — irréversible, aucune
    corbeille. `deleted` ne vaut jamais `false` (un slug absent lève un 404) : c'est
    une constante d'écho. `slug` est le slug normalisé.

    ⚠️ Comme à l'écriture, la clé de scope change de nom : `org_id` en scope org,
    `group_id` en scope équipe (#681)."""
    ok: bool
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    scope: Optional[str] = None
    slug: str
    deleted: bool


class InstructionArchived(BaseModel):
    """Archivage d'une procédure — l'alternative NON destructive à la suppression.
    La ligne et TOUT son historique de révisions restent en base ; ce qui change,
    c'est qu'elle disparaît de tous les listings, y compris ceux que l'IA lit
    (l'index de skills derrière `oto_procedure`, `op=list`, l'index de guides),
    donc l'agent cesse de la proposer et de la suivre. `archived` ne vaut jamais
    `false` (un slug absent lève un 404) : c'est une constante d'écho.

    Pas de désarchivage sur cette surface, même choix que pour les projets : la
    procédure est récupérable en base, pas d'un clic dans l'app.

    ⚠️ Comme à l'écriture, la clé de scope change de nom (#681) — ici toujours
    `org_id` : l'archivage n'est pas servi au palier équipe."""
    ok: bool
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    scope: Optional[str] = None
    slug: str
    archived: bool


class InstructionReverted(BaseModel):
    """Restauration d'une version passée. ⚠️ **`version` est un numéro NEUF, pas celui
    qu'on restaure** : revenir à la v2 d'une procédure en v6 produit une v7 dont le
    contenu est celui de la v2. L'historique n'est jamais rembobiné — `reverted_from`
    est la seule trace de l'intention."""
    ok: bool
    slug: str
    version: int
    reverted_from: int
    # Un retour en arrière peut ramener un corps d'avant le schéma OU le digest requis.
    diagram_warning: Optional[str] = None
    digest_warning: Optional[str] = None


def _inconnu(message: str) -> AuthzDenied:
    """« Guide inconnu » — code d'aujourd'hui, code d'hier dans `details.legacy_code`.

    Un code d'erreur ne se DOUBLE pas : il n'y a qu'un champ `error`. Le nouveau
    prend donc la place, et l'ancien est conservé à côté — un client qui teste
    `error == "unknown_doctrine"` a jusqu'au 29/10/2026 pour lire `legacy_code`, ou
    mieux, le nouveau code (#519, retrait #526).
    """
    return AuthzDenied(404, "unknown_guide", message,
                       deprecations.details_avec_code_dhier("unknown_guide"))


# ── Inputs — palier membre (org active, pas d'org_id) ───────────────────────
class EmptyInput(BaseModel):
    pass


class GuideGetInput(BaseModel):
    slug: Optional[str] = None
    guide_id: Optional[int] = None      # lecture par ID STABLE (ADR 0032) — y compris un guide PARTAGÉ à ton org (grant read, livraison #52)
    doctrine_id: Optional[int] = None   # ALIAS déprécié du précédent (retrait 29/10/2026, #519)
    # `None` = CASCADE de lecture (chez soi, puis l'org) — cf. `_get_guide`. Ce
    # champ valait `"org"` en dur : depuis que l'écriture sans scope va chez SOI
    # (ADR 0068), ce défaut faisait relire ailleurs qu'on venait d'écrire.
    scope: Optional[str] = None
    version: Optional[int] = None
    with_history: bool = False


class GuideListInput(BaseModel):
    query: Optional[str] = None
    scope: Optional[str] = None


class InstrGetInput(BaseModel):
    slug: str
    version: Optional[int] = None


class SlugInput(BaseModel):
    slug: str


class InstrSetInput(BaseModel):
    slug: Optional[str] = None
    body_md: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _MAX_BODY_BYTES},
        description=(f"Corps markdown, au plus {_MAX_BODY_BYTES} OCTETS UTF-8 — "
                     "au-delà : 400 `body_too_large`, qui donne le poids atteint et "
                     "la borne. `maxLength` compte des CARACTÈRES : nécessaire, pas "
                     "suffisant — un accent pèse deux octets, un trait de schéma "
                     "(`─│┌┘`) trois."))
    title: Optional[str] = None
    description: Optional[str] = None
    from_version: Optional[int] = None
    # ADR 0035 : entités requises déclarées [{name, type: tableau|connecteur|doc,
    # description?, connector?, schema?}] — référencées <slot:name> dans la prose.
    # `schema` (slots tableau, ADR 0046) = schéma CIBLE du tableau attendu (fields/
    # strict/lifecycle/key) : au binding du slot dans un projet, un namespace vierge
    # est PROVISIONNÉ avec, un schéma différent lève un warning. None = conserver.
    #
    # ⚠️ `Optional[list]` NU, et c'est délibéré (#658) : la SORTIE est typée
    # `list[slots.SlotDecl]`, pas l'entrée. `slots.validate_slots` NORMALISE (minuscules,
    # description tronquée, `connector` déduit du nom) là où un modèle refuserait, et
    # rend un message actionnable par index de champ. Poser `SlotDecl` ici changerait ce
    # que le serveur ACCEPTE sur une route déjà consommée — hors sujet d'un lot qui ne
    # corrige que ce qu'il DIT. Le jour où on resserrera, ce sera une décision datée.
    slots: Optional[list] = None
    # #69 : épingle l'écriture à une org EXPLICITE (robuste au reset de session).
    # None = org active (self-service). Gardé org_admin sur l'org nommée.
    org: Optional[int] = None
    # #662 : verrou OPTIMISTE — la version que le client a lue. Différente de la
    # version courante (ou procédure absente) ⟹ 409 `version_conflict`, l'écriture
    # n'a pas lieu. Omis = l'upsert historique, qui écrase le travail d'un autre
    # éditeur sans le dire. Miroir d'`expected_rev` sur les pages (`capabilities/docs/`).
    expected_version: Optional[int] = None


class InstrCreateInput(BaseModel):
    """CRÉER une procédure — le geste qui refuse d'écraser (#662).

    Séparé de `InstrSetInput` pour une raison de fond : `PUT …/instructions/{slug}`
    est le chemin de l'ÉDITION, et y refuser un slug pris casserait toute écriture
    sur une procédure existante. C'est donc un VERBE de plus, pas une garde de plus
    sur celui-là — un client qui crée le dit, et un slug déjà pris lui vaut un 409
    `slug_taken` au lieu d'un remplacement muet.

    `slug` reste FOURNI par le client, comme aujourd'hui : le dériver du titre est
    une question ouverte de l'issue (le slug est une référence lisible, citée dans la
    prose des guides et dans les descriptions d'outils), et la trancher au détour
    d'un correctif de perte de données serait la trancher sans la poser.

    Pas de `from_version` : restaurer une version suppose une procédure existante,
    donc l'inverse exact d'une création."""
    slug: str
    body_md: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _MAX_BODY_BYTES},
        description=(f"Corps markdown, au plus {_MAX_BODY_BYTES} OCTETS UTF-8 — "
                     "au-delà : 400 `body_too_large`, qui donne le poids atteint et "
                     "la borne. `maxLength` compte des CARACTÈRES : nécessaire, pas "
                     "suffisant — un accent pèse deux octets, un trait de schéma "
                     "(`─│┌┘`) trois."))
    title: Optional[str] = None
    description: Optional[str] = None
    slots: Optional[list] = None
    org: Optional[int] = None


class InstrDescribeInput(BaseModel):
    """CORRIGER la vitrine d'une procédure — titre et/ou description, corps INTACT.

    Le geste que le domaine n'avait pas (issue `oto`#27). `title` et `description`
    sont la ligne que l'agent lit dans le catalogue pour CHOISIR sa procédure ; les
    corriger passait par `PUT`, qui exige `body_md`, donc par une retranscription de
    plusieurs milliers de caractères qu'on ne voulait pas toucher. Deux procédures
    dont le corps était juste et la description périmée sont restées en l'état parce
    que le geste de correction était plus risqué que le défaut.

    Verbe à part et non `body_md` rendu facultatif sur `set` — même raison que la
    CRÉATION (cf. `InstrCreateInput`) : `set` est l'écriture DU CORPS, et y accepter
    un corps vide échangerait un refus bruyant contre un glissement muet.

    Pas de `slots` ni de `from_version` : les slots sont une propriété du CORPS (ils
    en indexent la prose `<slot:name>`), et restaurer une version est un geste de
    corps. Ce verbe ne fait qu'une chose."""
    slug: str
    title: Optional[str] = None
    description: Optional[str] = None
    # #69 : idem set — org explicite optionnelle (None = org active).
    org: Optional[int] = None
    # #662 : même verrou optimiste que `set`. La version LUE par le client ; différente
    # de la courante ⟹ 409 `version_conflict`, la correction n'a pas lieu.
    expected_version: Optional[int] = None


class GuideDeleteInput(BaseModel):
    slug: str
    # #69 : idem set — org explicite optionnelle (None = org active).
    org: Optional[int] = None


# … et les porter sur les entrées de la CONSOLE seule (#681). Les faces REST
# `/api/me/instructions/*` gardent leur palier org : le palier équipe y a déjà ses
# propres routes (`/api/groups/{id}/instructions/*`, `groups/guide.py`). Publier
# `scope`/`group` dans le corps d'une route qui les refuserait décrirait une porte
# qui n'existe pas.
class ConsoleInstrSetInput(InstrSetInput):
    """`InstrSetInput` + l'axe de PALIER de la console `oto_procedure`.

    `scope` : `org` (défaut) | `group`. `group` : l'équipe visée quand
    `scope='group'` — None = l'équipe ACTIVE, miroir exact d'`org` au palier
    au-dessus. La garde de l'ÉCRITURE au palier équipe est « membre de l'équipe »
    (`GROUP_MEMBER_OPT`) : celui qui déroule la procédure est celui qui l'améliore, et
    le geste se défait par `from_version`. La SUPPRESSION, elle, reste au chef
    (`ConsoleGuideDeleteInput`)."""
    scope: Optional[str] = None
    group: Optional[int] = None


class ConsoleInstrCreateInput(InstrCreateInput):
    """`InstrCreateInput` + le même axe de palier que `ConsoleInstrSetInput`.

    Même garde que `set` (`_ECRIRE` côté console) : créer n'est pas plus dangereux
    qu'écrire — c'est le geste qui l'était trop peu, faute de pouvoir refuser."""
    scope: Optional[str] = None
    group: Optional[int] = None


class ConsoleInstrDescribeInput(InstrDescribeInput):
    """`InstrDescribeInput` + le même axe de palier que `ConsoleInstrSetInput`.

    Même garde que `set` (`_ECRIRE` côté console) : corriger la vitrine est une
    écriture, ni plus ni moins — au palier équipe, un MEMBRE la corrige, pour la même
    raison qu'il corrige le corps (celui qui déroule la procédure est celui qui
    l'améliore) et avec la même réversibilité (la version monte, `from_version`
    défait)."""
    scope: Optional[str] = None
    group: Optional[int] = None


class ConsoleGuideDeleteInput(GuideDeleteInput):
    """`GuideDeleteInput` + le même axe de palier (cf. `ConsoleInstrSetInput`).

    Même axe, **autre garde** : `GROUP_ADMIN_OPT` (« chef de l'équipe »). Supprimer
    emporte la procédure et tout son historique, sans corbeille — c'est le seul geste du
    domaine que rien ne défait, et le partage `set`/`delete` tient à ça."""
    scope: Optional[str] = None
    group: Optional[int] = None


class RevertInput(BaseModel):
    slug: str
    version: int


# ── Inputs — palier admin (org ciblée par org_id) ───────────────────────────
class AdminGuideGetInput(BaseModel):
    org_id: int
    slug: Optional[str] = None
    scope: str = "org"
    version: Optional[int] = None
    with_history: bool = False


class AdminGuideListInput(BaseModel):
    org_id: int
    query: Optional[str] = None
    scope: Optional[str] = None


class AdminInstrSetInput(BaseModel):
    org_id: int
    slug: Optional[str] = None
    body_md: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _MAX_BODY_BYTES},
        description=(f"Corps markdown, au plus {_MAX_BODY_BYTES} OCTETS UTF-8 — "
                     "au-delà : 400 `body_too_large`, qui donne le poids atteint et "
                     "la borne. `maxLength` compte des CARACTÈRES : nécessaire, pas "
                     "suffisant — un accent pèse deux octets, un trait de schéma "
                     "(`─│┌┘`) trois."))
    title: Optional[str] = None
    description: Optional[str] = None
    from_version: Optional[int] = None
    slots: Optional[list] = None        # nu comme `InstrSetInput.slots` — même raison


class AdminSlugInput(BaseModel):
    org_id: int
    slug: str


def _project_instance(member_mode: bool) -> Optional[dict]:
    """Bloc « instance » du projet actif (ADR 0032 §5, B3) : les entités du projet
    (tableaux + connecteurs surchargés) contre lesquelles l'agent résout les
    placeholders de la procédure — « la procédure partage à 100 % les ressources du
    projet, pas de ressources propres ». None hors projet, ou en mode admin cross-org
    (le bracelet est une notion de session membre). Best-effort."""
    if not member_mode:
        return None
    pid = access.current_project()
    if pid is None:
        return None
    try:
        p = db.get_project_by_id(pid)
        if p is None:
            return None
        return {
            "project_id": pid,
            "name": p.get("name"),
            "entities": [
                {"target_type": l["target_type"], "target_ref": l["target_ref"],
                 "label": l.get("label"), "role": l.get("role"), "config": l.get("config") or {}}
                for l in db.list_project_links(pid)
            ],
        }
    # noqa: SILENT — instance de projet non résolue ⇒ pas d'épinglage, pas d'erreur
    except Exception:
        return None


def _active_group(ctx: ResolvedCtx) -> Optional[int]:
    """L'équipe visée : celle que l'autz a résolue (épingle `group=`), sinon l'active."""
    return ctx.group_id if ctx.group_id is not None else access.current_group(ctx.sub)


def _owner_of(ctx: ResolvedCtx, inp) -> tuple[str, str]:
    """Le propriétaire visé par un geste sur une procédure — `(owner_type, owner_id)`,
    la clé unique du store (#681).

    Le palier vient de `inp.scope` ; l'IDENTITÉ vient toujours du `ResolvedCtx`, donc
    d'une cible que la règle d'autz a déjà **vérifiée et injectée**. Un `scope='group'`
    arrivé sur une surface dont la règle est org (les faces admin, les routes REST
    `/api/me/instructions/*`) trouve `ctx.group_id` vide et se fait refuser : le palier
    équipe n'est atteignable que par une règle qui a vérifié l'équipe. C'est le
    verrou — relire l'équipe ici (via l'équipe ACTIVE, par exemple) rendrait le champ
    client suffisant pour écrire chez elle.

    ⚠️ **Le palier PERSONNEL (`scope='user'`) est ouvert depuis le 04/09/2026** (ADR
    0068), mais il n'est PAS le défaut ici — et c'est délibéré. Les quatre appelants de
    cette fonction sont des capacités gardées par `ORG_ADMIN_OPT` : leur objet EST
    d'écrire la procédure de l'org. Y mettre un défaut personnel transformerait une
    surface d'administration en espace privé, ce que personne n'a demandé. Le défaut
    personnel vit sur la surface AGENT (`oto_procedure`), là où l'appelant est un
    modèle qui écrit ce qu'on lui a dicté sans rien demander de partagé.

    ⚠️ Ce verrou ne dit RIEN du palier de droits : le rôle exigé dépend du VERBE et se
    déclare à la capacité (`set` = membre de l'équipe, `delete` = chef — #681). Deux
    handlers passent ici, ils ne demandent pas la même chose."""
    if getattr(inp, "scope", None) == "group":
        if ctx.group_id is None:
            raise AuthzDenied(403, "forbidden",
                              "Le palier équipe s'écrit par `oto_procedure` "
                              "(`scope='group'`, `group` pour viser une équipe "
                              "précise) : y écrire demande d'être membre de l'équipe, "
                              "y supprimer d'en être le chef. Cette surface-ci écrit "
                              "l'org.")
        return ("group", str(ctx.group_id))
    if getattr(inp, "scope", None) == "user":
        # Palier PERSONNEL : l'identité vient du contexte, jamais d'un champ client —
        # même verrou que les deux autres paliers. Un `owner_id` accepté ici
        # permettrait d'écrire la procédure de quelqu'un d'autre.
        if ctx.sub is None:
            raise AuthzDenied(401, "no_identity", "Identité requise pour écrire ici.")
        return ("user", str(ctx.sub))
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — vois `oto_use_org`, ou passe `org` "
                          "explicitement.")
    return ("org", str(ctx.org_id))


def _scope_ref(owner: tuple[str, str]) -> dict:
    """La clé de scope d'une réponse — `org_id`, `group_id` ou `user_id`, jamais deux,
    jamais une clé nulle (convention de tout ce module, cf. `GuideView`).

    ⚠️ `int()` sur l'identifiant vaut pour une org et une équipe (clés primaires), pas
    pour une PERSONNE : un `sub` est du texte. La conversion levait un `ValueError` que
    l'adaptateur REST ne rattrape pas — donc un 500 sur une procédure personnelle, au
    retour d'une écriture qui avait RÉUSSI (ADR 0068)."""
    otype, oid = owner[0], owner[1]
    return {f"{otype}_id": (oid if otype == "user" else int(oid)), "scope": otype}


# ── Handlers (core ; owner depuis ctx → partagés membre/admin) ──────────────
def _read_guide(ctx: ResolvedCtx, inp) -> tuple[dict, tuple[str, ...] | None]:
    """Bundle session-start (slug omis) OU un guide nommé. En mode membre
    (`inp.org_id` absent) complète avec le guide du département actif."""
    org_id = ctx.org_id
    member_mode = getattr(inp, "org_id", None) is None
    slug = inp.slug
    scope = inp.scope
    version = inp.version

    # Lecture par ID STABLE (ADR 0032 « stop using slug ») — le chemin des liens de
    # projet ET des guides PARTAGÉS cross-org (grant read via oto_resource, #52) :
    # l'accès passe par le seam ownership (membre de l'org propriétaire ∪ grants),
    # pas par l'org active.
    # ⚠️ Le kind d'ownership `doctrine` ci-dessous est une VALEUR EN BASE
    # (`resource_grants.resource_type`) : elle ne se double pas, elle se migre — lot B4.
    # Les deux noms du paramètre sont acceptés (#519, l'ancien part le 29/10/2026).
    guide_id = getattr(inp, "guide_id", None)
    if guide_id is None:
        guide_id = getattr(inp, "doctrine_id", None)
    if guide_id is not None:
        from ... import ownership   # import paresseux (miroir _authz, zéro cycle au boot)
        instr = org_store.get_instruction_by_id(int(guide_id))
        if not instr:
            raise _inconnu(f"Aucun guide #{guide_id}.")
        if not ownership.can_access(ctx.sub, "doctrine", str(guide_id), "read"):
            raise AuthzDenied(403, "forbidden", "Accès refusé à ce guide.")
        # Le scope se lit sur les colonnes qui le portent, pas sur `org_id` (l'org
        # PARENTE, identique pour une procédure d'équipe et pour celles de son org).
        # `scope` était écrit « org » en dur : vrai tant qu'aucune procédure d'équipe
        # n'existait, faux le jour où l'une est partagée par id (#681).
        owner_type, owner_id = str(instr["owner_type"]), str(instr["owner_id"])
        parent_org = instr["org_id"]
        if version is not None:
            versioned = org_store.get_instruction(owner_type, owner_id, instr["slug"],
                                                  version)
            if not versioned:
                raise _inconnu(f"Guide #{guide_id} : pas de version {version}.")
            instr = {**versioned, "id": instr["id"]}
        return deprecations.avec_les_deux_noms({
            "org_id": parent_org, "guide_id": int(guide_id),
            "scope": owner_type, "slug": instr["slug"], "title": instr["title"],
            "description": instr["description"], "version": instr["version"],
            "body_md": instr["body_md"], "slots": instr.get("slots") or []}), (instr["body_md"],)

    if slug is None:
        # Début de session : guide de base + index (vide gracieux si pas d'org).
        if org_id is None:
            return deprecations.avec_les_deux_noms({
                "org_id": None, "org": None, "guide": "", "group_id": None,
                "group": None, "group_guide": "", "guides": [], "referenced_tools": []}), None
        o = org_store.get_org(org_id)
        # Le readme d'org/équipe est un GUIDE `delivery='init'` (ADR 0042), plus une
        # instruction déguisée : on le lit sur sa surface, pas via le store de procédures.
        base_body = guide_store.init_guide_body("org", org_id) or ""
        index = [{"slug": i["slug"], "title": i["title"],
                  "description": i["description"], "scope": "org"}
                 for i in org_store.list_instructions("org", org_id)]
        group_id = _active_group(ctx) if member_mode else None
        group_name, group_guide = None, ""
        if group_id is not None:
            g = group_store.get_group(group_id)
            group_name = g["name"] if g else None
            group_guide = guide_store.init_guide_body("group", group_id) or ""
            index += [{"slug": i["slug"], "title": i["title"],
                       "description": i["description"], "scope": "group"}
                      for i in org_store.list_instructions("group", group_id)]
        guide_body = base_body
        pi = _project_instance(member_mode)
        return deprecations.avec_les_deux_noms({
            "org_id": org_id, "org": o["name"] if o else None, "guide": guide_body,
            "group_id": group_id, "group": group_name, "group_guide": group_guide,
            "guides": index,
            **({"project_instance": pi} if pi else {}),
        }), (guide_body, group_guide)

    # Un guide nommé précis.
    if scope is None and member_mode:
        # ⚠️ CASCADE de lecture (04/09/2026) — l'écriture sans `scope` va chez SOI
        # depuis l'ADR 0068 ; sans elle, relire sans `scope` chercherait dans l'org et
        # rendrait « introuvable » la procédure qu'on vient d'écrire.
        # L'ordre est celui de la propriété : la mienne d'abord, celle de l'org
        # ensuite. Une procédure perso qui porte le même slug qu'une procédure d'org
        # gagne — c'est la plus proche de qui demande, et il l'a écrite exprès.
        # L'ÉQUIPE reste sur `scope='group'` explicite : elle n'était pas dans ce
        # chemin avant, et l'y ajouter changerait ce que lisent les appels existants.
        # ⚠️ On ne RÉSOUT ici que le palier — le rendu reste celui du chemin commun
        # ci-dessous. Recopier la réponse ferait diverger les deux formes au premier
        # champ ajouté (l'historique y a manqué le temps d'un essai).
        scope = ("user" if (ctx.sub is not None and org_store.get_instruction(
            "user", str(ctx.sub), slug, version)) else "org")
    if scope == "user" and member_mode:
        # Palier PERSONNEL (ADR 0068) — l'identité vient du contexte d'autz, jamais
        # d'un champ client : le palier personnel de QUELQU'UN D'AUTRE n'est pas
        # atteignable, même en le nommant. Même verrou que l'org et l'équipe.
        # ⚠️ Sans cette branche, `scope='user'` tombait dans le `else` et cherchait au
        # palier ORG : la procédure écrite à soi devenait « introuvable » à la
        # relecture, sur un message qui nommait pourtant le bon scope.
        if ctx.sub is None:
            raise AuthzDenied(401, "no_identity", "Identité requise.")
        owner: tuple[str, str] = ("user", str(ctx.sub))
        scope_ref: dict = {"user_id": str(ctx.sub)}
    elif scope == "group" and member_mode:
        group_id = _active_group(ctx)
        if group_id is None:
            raise AuthzDenied(400, "no_active_group", "Pas de département actif — vois `oto_use_group`.")
        owner = ("group", str(group_id))
        scope_ref = {"group_id": group_id}
    else:
        if org_id is None:
            raise AuthzDenied(400, "no_active_org", "Pas d'org active — vois `oto_use_org`.")
        owner = ("org", str(org_id))
        scope_ref = {"org_id": org_id}
    instr = org_store.get_instruction(*owner, slug, version)
    if not instr:
        raise _inconnu(f"Aucun guide `{org_store.normalize_slug(slug)}` (scope {scope})"
                       + (f" en version {version}" if version is not None else "")
                       + ". Vois `oto_procedure(op='list')`.")
    out = {**scope_ref, "scope": scope, "slug": instr["slug"], "title": instr["title"],
           "description": instr["description"], "version": instr["version"],
           "body_md": instr["body_md"], "slots": instr.get("slots") or []}
    pi = _project_instance(member_mode)
    if pi:
        out["project_instance"] = pi
    # L'historique suit le palier lu. Il était réservé au scope org, alors que le
    # modèle versionné est le MÊME aux deux étages depuis la fusion des stores (#681) :
    # une procédure d'équipe avait des versions que cette réponse taisait.
    if inp.with_history:
        out["versions"] = org_store.list_instruction_versions(*owner, slug)
    return out, (instr["body_md"],)


async def _get_guide(ctx: ResolvedCtx, inp) -> dict:
    out, bodies = await run_in_threadpool(_read_guide, ctx, inp)
    if bodies is not None:
        out["referenced_tools"] = await tool_registry.manifest_for(*bodies)
    return out


def _list_guides(ctx: ResolvedCtx, inp) -> dict:
    """Catalogue des guides nommés (slug/title/description/version, sans corps)."""
    org_id = ctx.org_id
    member_mode = getattr(inp, "org_id", None) is None
    query = inp.query
    scope = inp.scope
    if org_id is None:
        return deprecations.avec_les_deux_noms({"org_id": None, "guides": []})
    out: list = []
    if member_mode and scope in (None, "user") and ctx.sub is not None:
        # ⚠️ Le palier PERSONNEL entre dans le cumul (04/09/2026). Sans lui, on écrit
        # chez soi par défaut et on ne se voit PAS dans la liste : la procédure existe,
        # `op=list` ne la montre pas, et on la croit perdue. Elle vient EN TÊTE — c'est
        # la sienne, et c'est le premier endroit où l'on cherche ce qu'on a écrit.
        rows = (org_store.search_instructions("user", str(ctx.sub), query) if query
                else org_store.list_instructions("user", str(ctx.sub)))
        out += [{**r, "scope": "user"} for r in rows]
    if scope in (None, "org"):
        include_base = not member_mode  # la surface admin inclut le guide de base
        rows = (org_store.search_instructions("org", org_id, query, include_base=include_base)
                if query
                else org_store.list_instructions("org", org_id, include_base=include_base))
        out += [{**r, "scope": "org"} for r in rows]
    group_id = _active_group(ctx) if (member_mode and scope in (None, "group")) else None
    if group_id is not None:
        rows = (org_store.search_instructions("group", group_id, query) if query
                else org_store.list_instructions("group", group_id))
        out += [{**r, "scope": "group"} for r in rows]
    return deprecations.avec_les_deux_noms(
        {"org_id": org_id, "group_id": group_id, "guides": out})


def _write_instruction(ctx: ResolvedCtx, inp, must_create: bool = False) -> tuple[dict, str]:
    """Crée/met à jour une instruction (incrémente la version + archive un snapshot).
    `from_version` = restaure une version passée comme nouvelle (revert MCP) — corps,
    métadonnées ET slots. `slots` (ADR 0035) : None = conserver l'existant.

    Le PALIER écrit vient de `inp.scope` (#681) : org par défaut, équipe si demandé —
    même corps de handler, même store, seule la clé de propriété change.

    `must_create` (posé par `_create_instruction`, jamais par le client) et
    `expected_version` (lu sur l'entrée) sont les deux refus anti-écrasement #662."""
    owner = _owner_of(ctx, inp)
    norm = org_store.normalize_slug(inp.slug) if inp.slug else _BASE
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug invalide (attendu [a-z0-9_-]).")
    body_md, title, description = inp.body_md, inp.title, inp.description
    slots_in = getattr(inp, "slots", None)
    # `from_version` (restauration) n'existe pas sur l'entrée de CRÉATION : restaurer
    # suppose une procédure existante — cf. `InstrCreateInput`.
    from_version = getattr(inp, "from_version", None)
    if from_version is not None:
        old = org_store.get_instruction(*owner, norm, from_version)
        if not old:
            raise AuthzDenied(404, "unknown_version", f"Pas de version {from_version} pour `{norm}`.")
        body_md, title, description = old["body_md"], old["title"], old["description"]
        slots_in = old.get("slots") or []
    if slots_in is not None:
        try:
            slots_in = slots_mod.validate_slots(slots_in)
        except ValueError as e:
            raise AuthzDenied(400, "invalid_slots", str(e))
    body_md = (body_md or "").strip()
    if not body_md:
        raise AuthzDenied(400, "body_md_required", "body_md vide (ou fournis `from_version`).")
    # Injecté dans le guide de base servi à chaque session → caper la taille.
    # ⚠️ La borne n'est PAS publiée dans le schéma servi de `InstrSetInput` /
    # `AdminInstrSetInput` / la création — contrairement au `body_md` des guides, qui
    # porte son `maxLength` depuis le 29/08. Un front tiers ne peut donc pas dériver sa
    # garde de saisie ici : ligne du lot 3 d'oto#42, pas traitée dans ce commit.
    poids = len(body_md.encode())
    if poids > _MAX_BODY_BYTES:
        raise AuthzDenied(
            400, "body_too_large",
            f"body_md pèse {poids} octets UTF-8 pour {_MAX_BODY_BYTES} au plus "
            f"({poids - _MAX_BODY_BYTES} de trop). La borne est en OCTETS : un caractère "
            "accentué en pèse deux, donc compter les caractères la sous-estime.")
    if norm == _BASE:
        raise AuthzDenied(400, "reserved_slug",
                          f"`{_BASE}` est le readme (prose injectée), pas une "
                          "procédure — édite-le sur la surface guide "
                          f"(scope='{owner[0]}', delivery='init').")
    # Le corps AVANT écriture, pour dire ce que cette version RETIRE (oto#61).
    # ⚠️ Best-effort, et lu APRÈS toutes les validations : un contrôle de forme ne
    # change ni l'ordre des refus ni leur nature. Une création n'a rien à comparer.
    ancien_md = ""
    if not must_create:
        try:
            precedent = org_store.get_instruction(*owner, norm)
            ancien_md = (precedent or {}).get("body_md") or ""
        except Exception as e:  # noqa: BLE001 — cf. les autres checks de forme
            # noqa: SILENT — l'avertissement de retrait est optionnel et ne doit jamais
            # empêcher une écriture légitime ; journalisé pour qu'un silence durable se
            # voie.
            logger.warning("retrait: corps précédent illisible pour %s : %s", norm, e)
    # Idem : l'entrée de CRÉATION n'a pas de verrou optimiste (rien à verrouiller).
    expected_version = getattr(inp, "expected_version", None)
    try:
        version = org_store.set_instruction(
            *owner, norm, body_md, title=title, description=description,
            set_by=ctx.sub, slots=slots_in, must_create=must_create,
            expected_version=expected_version)
    except org_store.InstructionExists as e:
        # Le cœur de #662 : ce qui était un remplacement muet devient un refus qui
        # NOMME le slug, sa version et son état — de quoi choisir un autre slug ou
        # assumer l'édition (`PUT`), sans avoir à relire pour découvrir le dégât.
        raise AuthzDenied(
            409, "slug_taken",
            f"`{norm}` porte déjà une procédure (v{e.version}"
            + (", archivée" if e.archived else "") + ") dans ce scope. Crée-la sous "
            "un autre slug, ou édite l'existante (`PUT /api/me/instructions/"
            f"{norm}`) — la création, elle, n'écrase pas.",
            {"slug": norm, "version": e.version, "archived": e.archived})
    except org_store.InstructionVersionConflict as e:
        raise AuthzDenied(
            409, "version_conflict",
            (f"`{norm}` est en v{e.current_version}, tu as lu v{expected_version}"
             if e.current_version is not None
             else f"`{norm}` n'existe pas (ou plus) dans ce scope")
            + " : relis-la (`op=get`) et rejoue ton édition sur la version à jour.",
            {"slug": norm, "current_version": e.current_version})
    # Slots EFFECTIFS après écriture (None = conservés → relire la row) pour le
    # check croisé <slot:name> ↔ déclaration (ADR 0035, non bloquant comme 0014).
    effective_slots = slots_in
    if effective_slots is None:
        cur = org_store.get_instruction(*owner, norm)
        effective_slots = (cur or {}).get("slots") or []
    return {"ok": True, **_scope_ref(owner), "slug": norm, "version": version, "set": True,
            **({"reverted_from": from_version} if from_version is not None else {}),
            **slots_mod.slots_check(body_md, effective_slots),
            **procedure_diagram.diagram_check(body_md),
            **procedure_digest.digest_check(body_md),
            **procedure_retrait.retrait_check(ancien_md, body_md)}, body_md


async def _set_instruction(ctx: ResolvedCtx, inp, must_create: bool = False) -> dict:
    out, body_md = await run_in_threadpool(_write_instruction, ctx, inp, must_create)
    return {**await tool_registry.write_check(body_md), **out}


async def _create_instruction(ctx: ResolvedCtx, inp) -> dict:
    """CRÉE une procédure — même corps que l'écriture, mais le slug doit être LIBRE.

    Le geste que le domaine n'avait pas (#662) : jusqu'ici toute création passait par
    l'upsert de `_set_instruction`, et un slug déjà pris y remplaçait la procédure en
    place sans un mot. Le refus est levé DANS la transaction verrouillée du store, pas
    par un pré-check ici : deux créations simultanées sur le même slug s'y glisseraient."""
    return await _set_instruction(ctx, inp, must_create=True)


def _describe_instruction(ctx: ResolvedCtx, inp) -> dict:
    """CORRIGE le titre / la description d'une procédure — le corps n'est pas touché.

    Le geste manquant de l'issue `oto`#27 : la seule façon de changer une ligne de
    vitrine était de repasser tout le corps par `_set_instruction`, et une
    retranscription peut dégrader ce qu'elle recopie.

    Le corps est reconduit DANS la transaction verrouillée du store, jamais relu ici
    pour être renvoyé : entre une lecture faite dehors et l'écriture, une édition
    concurrente se glisse — et on réécrirait alors par-dessus elle le corps d'avant,
    ce que ce geste est précisément censé ne pas faire."""
    owner = _owner_of(ctx, inp)
    norm = org_store.normalize_slug(inp.slug)
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug invalide (attendu [a-z0-9_-]).")
    if inp.title is None and inp.description is None:
        raise AuthzDenied(
            400, "nothing_to_describe",
            "Fournis `title` et/ou `description`. Une correction qui ne change rien "
            "consommerait quand même une version — refusé plutôt que subi. Pour "
            "changer le CORPS, c'est `op=set` (qui exige `body_md`).")
    if norm == _BASE:
        raise AuthzDenied(400, "reserved_slug",
                          f"`{_BASE}` est le readme (prose injectée), pas une "
                          "procédure — édite-le sur la surface guide "
                          f"(scope='{owner[0]}', delivery='init').")
    try:
        version = org_store.set_instruction_meta(
            *owner, norm, title=inp.title, description=inp.description,
            set_by=ctx.sub, expected_version=inp.expected_version)
    except org_store.InstructionVersionConflict as e:
        raise AuthzDenied(
            409, "version_conflict",
            f"`{norm}` est en v{e.current_version}, tu as lu v{inp.expected_version}"
            " : relis-la (`op=get`) et rejoue ta correction sur la version à jour.",
            {"slug": norm, "current_version": e.current_version})
    if version is None:
        raise AuthzDenied(404, "not_found", f"Instruction `{norm}` absente.")
    # L'écho porte la vitrine ENTIÈRE, pas seulement le champ corrigé : l'appelant qui
    # n'a envoyé qu'une description voit le titre reconduit, donc ce que le catalogue
    # affiche désormais — sans avoir à relire.
    cur = org_store.get_instruction(*owner, norm) or {}
    return {"ok": True, **_scope_ref(owner), "slug": norm, "version": version,
            "title": cur.get("title") or "", "description": cur.get("description") or ""}


def _delete_instruction(ctx: ResolvedCtx, inp) -> dict:
    owner = _owner_of(ctx, inp)
    norm = org_store.normalize_slug(inp.slug)
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug requis.")
    deleted = org_store.delete_instruction(*owner, norm)
    if not deleted:
        raise AuthzDenied(404, "not_found", f"Instruction `{norm}` absente.")
    return {"ok": True, **_scope_ref(owner), "slug": norm, "deleted": True}


def _archive_instruction(ctx: ResolvedCtx, inp) -> dict:
    owner = _owner_of(ctx, inp)
    norm = org_store.normalize_slug(inp.slug)
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug requis.")
    archived = org_store.archive_instruction(*owner, norm)
    if not archived:
        raise AuthzDenied(404, "not_found", f"Instruction `{norm}` absente.")
    return {"ok": True, **_scope_ref(owner), "slug": norm, "archived": True}


# ── Handlers REST-only (org active) ─────────────────────────────────────────
def _instructions_list(ctx: ResolvedCtx, inp: EmptyInput) -> dict:
    """Guide de base (meta) + index des instructions nommées de l'org active.
    Bundle vide en 200 si pas d'org active (consommé par l'overview)."""
    org_id = ctx.org_id
    if org_id is None:
        # Sans org active, aucun geste n'aboutit : tous les droits à faux — et TOUS
        # ceux que le modèle déclare, pas seulement `can_edit` (un drapeau absent d'une
        # branche se lit `undefined`, ce qu'un front prend pour « pas le droit » sur
        # l'une et pour « peut-être » sur l'autre).
        return deprecations.avec_les_deux_noms({
            "org_id": None, "org_name": None, "can_edit": False,
            **{nom: False for nom in _DROITS_SERVIS},
            "guide": {"exists": False, "version": 0, "updated_at": None},
            "instructions": []})
    o = org_store.get_org(org_id)
    base = guide_store.get_init_guide("org", org_id)      # readme = guide init (ADR 0042)
    has_readme = bool((base["body_md"] or "").strip())
    return deprecations.avec_les_deux_noms({
        "org_id": org_id,
        "org_name": o["name"] if o else None,
        # Droit d'ADMINISTRER l'org — inchangé.
        "can_edit": roles.is_org_admin(ctx.sub, org_id),
        # Droits sur les PROCÉDURES, rendus par les règles d'autz déclarées.
        # `org=` épingle CETTE org : la règle a une branche self-service qui relirait
        # l'org active, et le bundle doit parler de l'org qu'il affiche.
        **{nom: capacite_autorise(cle, ctx.sub, org=org_id)
           for nom, cle in _DROITS_SERVIS.items()},
        "guide": {
            "exists": has_readme,
            "version": 1 if has_readme else 0,        # prose plate : pas d'historique
            "updated_at": base["updated_at"] if has_readme else None,
        },
        "instructions": org_store.list_instructions("org", org_id),
    })


def _instruction_get(ctx: ResolvedCtx, inp: InstrGetInput) -> dict:
    instr = org_store.get_instruction("org", ctx.org_id, inp.slug, version=inp.version)
    if not instr:
        raise AuthzDenied(404, "not_found", f"Instruction `{org_store.normalize_slug(inp.slug)}` absente.")
    return {
        "slug": instr["slug"], "title": instr["title"], "description": instr["description"],
        "version": instr["version"], "body_md": instr["body_md"],
        "slots": instr.get("slots") or [], "set_by": instr.get("set_by"),
        "created_at": instr.get("created_at"), "updated_at": instr.get("updated_at"),
    }


def _instruction_versions(ctx: ResolvedCtx, inp: SlugInput) -> dict:
    slug = org_store.normalize_slug(inp.slug)
    return {"slug": slug,
            "versions": org_store.list_instruction_versions("org", ctx.org_id, slug)}


def _instruction_revert(ctx: ResolvedCtx, inp: RevertInput) -> dict:
    slug = org_store.normalize_slug(inp.slug)
    old = org_store.get_instruction("org", ctx.org_id, slug, version=inp.version)
    if not old:
        raise AuthzDenied(404, "not_found", f"Pas de version {inp.version} pour `{slug}`.")
    version = org_store.set_instruction("org", ctx.org_id, slug, old["body_md"],
                                        title=old["title"],
                                        description=old["description"], set_by=ctx.sub,
                                        slots=old.get("slots") or [])
    # Revenir en arrière peut RAMENER une procédure d'avant le schéma requis : le signal
    # part ici aussi (la face MCP passe par `_set_instruction`, qui l'a déjà).
    return {"ok": True, "slug": slug, "version": version, "reverted_from": inp.version,
            **procedure_diagram.diagram_check(old["body_md"]),
            **procedure_digest.digest_check(old["body_md"])}


def _instruction_usage(ctx: ResolvedCtx, inp: SlugInput) -> dict:
    """Usage d'un guide (ADR 0014) : chargements par l'agent (nb, appelants, série 30j)
    ET déroulés (`run_start`, nb + série 30j) — deux verbes du MÊME journal
    `tool_calls`, scopés aux membres de l'org. Voir `InstructionUsage` sur pourquoi
    les deux séries ne se confondent ni ne s'additionnent."""
    slug = org_store.normalize_slug(inp.slug)
    subs = [m["sub"] for m in org_store.list_org_members(ctx.org_id)]
    slug_filter = None if slug == _BASE else slug
    u = db.instruction_usage(subs, _GUIDE_GET_TOOL, slug_filter, days=30)
    # Les DÉROULÉS, du même journal, sous la clé que le lecteur de runs y lit.
    #
    # C'est le seul chemin par lequel un MEMBRE voit les runs de sa procédure. Les
    # surfaces existantes passent par `/api/orgs/{id}/monitoring/*`, qui est
    # ORG_ADMIN_OF de bout en bout et sans filtre par procédure (le front en récupère
    # 100 max et trie côté client) : un membre n'y a droit à rien. Cette capacité-ci
    # est ORG_MEMBER et déjà scopée aux subs de l'org — la série arrive donc sans
    # plafond, sans tri client, et pour tout le monde.
    r = db.instruction_usage(subs, _RUN_START_TOOL, slug_filter, days=30,
                             slug_key=db.usage._ARG_PROCEDURE)
    today = date.today()

    def _dense(daily: dict) -> list[int]:
        return [daily.get(str(today - timedelta(days=29 - i)), 0) for i in range(30)]

    return {"slug": slug, "count": u["count"], "callers": u["callers"],
            "series": _dense(u["daily"]),
            "runs_count": r["count"], "runs_series": _dense(r["daily"])}


CAPABILITIES += [
    # ── Lectures membre (org active) ────────────────────────────────────────
    Capability(
        key="org.guide.get", handler=_get_guide, Input=GuideGetInput,
        authz=SUB_ONLY, Output=GuideView,
        description=("Operational guide of your active org. The base guide is now "
                     "INJECTED into your session instructions at connect — call this with "
                     "`slug` to load ONE named skill's full markdown (list skills with "
                     "oto_procedure op=list). No-arg returns base + index, e.g. to refresh "
                     "after switching org with oto_use_org. `scope=group` targets your "
                     "active department. `guide_id` loads a guide by its STABLE id "
                     "(project procedure links) — including one SHARED to you/your org "
                     "by another org (delivered project)."),
        # Face REST par ID stable : résolution des liens `procedure` d'un projet côté
        # dashboard — y compris un projet LIVRÉ (guide d'une autre org, grant read).
        rest=RestBinding("GET", "/api/me/guides/{guide_id}"),
    ),
    Capability(
        key="org.instruction.list", handler=_instructions_list, Input=EmptyInput,
        authz=SUB_ONLY, Output=InstructionsBundle,
        rest=RestBinding("GET", "/api/me/instructions"),
    ),
    Capability(
        key="org.instruction.get", handler=_instruction_get, Input=InstrGetInput,
        authz=ORG_MEMBER, Output=InstructionView,
        rest=RestBinding("GET", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.versions", handler=_instruction_versions, Input=SlugInput,
        authz=ORG_MEMBER, Output=InstructionVersions,
        rest=RestBinding("GET", "/api/me/instructions/{slug}/versions"),
    ),
    Capability(
        key="org.instruction.usage", handler=_instruction_usage, Input=SlugInput,
        authz=ORG_MEMBER, Output=InstructionUsage,
        rest=RestBinding("GET", "/api/me/instructions/{slug}/usage"),
    ),
    # ── Écritures membre (org active, org_admin) ────────────────────────────
    Capability(
        key="org.instruction.set", handler=_set_instruction, Input=InstrSetInput,
        authz=ORG_ADMIN_OPT("org"), Output=InstructionWritten,
        description=("Write your org's guide (org_admin). Each write bumps the version "
                     "and archives a snapshot. slug omitted = base guide; given = a named "
                     "skill. `from_version` restores a past version as a new one (revert). "
                     "`slots` = the procedure's REQUIRED ENTITIES [{name, type: tableau|"
                     "connecteur|base, description?, connector?}] — reference them BY NAME "
                     "in the prose as <slot:name> (never a hardcoded instance: the project "
                     "binds name→instance). EVERY procedure OPENS with "
                     "`> **Self-improvement digest** — …` (what the last run taught and "
                     "what was fixed, dated) and must carry a FLOWCHART (one "
                     "untagged fenced block drawn in box characters, right after the « At a "
                     "glance » table and before the first phase heading) — it is the DEFAULT "
                     "view of the process page; read the `procedure-flowchart` guide first. "
                     "Response returns cross-check warnings "
                     "(unresolved/unreferenced slots, suggestions, `digest_warning`, "
                     "`diagram_warning`). "
                     "`org` pins the write to "
                     "an EXPLICIT org id (default = your active org) — pass it to stay robust "
                     "if a reconnect dropped your session org; you must be org_admin of it. "
                     "⚠️ This is an UPSERT: a slug that already exists is EDITED (new "
                     "version, prior one snapshotted), never rejected. To CREATE without "
                     "risking someone else's procedure, use POST /api/me/instructions, "
                     "which refuses a taken slug. Pass `expected_version` (the version you "
                     "read) to turn a concurrent edit into a 409 instead of an overwrite."),
        errors=(DeclaredError(409, "version_conflict",
                              "`expected_version` fourni et ≠ version courante (ou "
                              "procédure absente) — l'écriture n'a pas eu lieu"),),
        rest=RestBinding("PUT", "/api/me/instructions/{slug}"),
    ),
    # La CRÉATION, seul geste du domaine qui refuse un slug pris (#662). Verbe à part
    # et non garde de plus sur `set` : `PUT …/{slug}` est AUSSI le chemin de l'édition,
    # et y refuser l'existant casserait toute écriture sur une procédure en place.
    Capability(
        key="org.instruction.create", handler=_create_instruction, Input=InstrCreateInput,
        authz=ORG_ADMIN_OPT("org"), Output=InstructionWritten,
        description=("CREATE a named procedure (org_admin) — refuses a slug that is "
                     "already taken (409 `slug_taken`) instead of overwriting it. `slug` "
                     "is REQUIRED and yours to choose (it is the readable reference cited "
                     "in prose and tool descriptions); it is normalized to [a-z0-9_-]. Use "
                     "PUT /api/me/instructions/{slug} to EDIT an existing one. Same body "
                     "as the write otherwise (body_md, title, description, slots), and the "
                     "same cross-check warnings in the response."),
        errors=(DeclaredError(409, "slug_taken",
                              "le slug porte déjà une procédure dans ce scope (y compris "
                              "archivée) — rien n'a été écrit"),),
        rest=RestBinding("POST", "/api/me/instructions"),
    ),
    # La CORRECTION DE VITRINE (issue `oto`#27). Troisième verbe d'écriture, pour la
    # même raison que le deuxième : `PUT …/{slug}` exige `body_md`, donc corriger une
    # description imposait de repasser tout le corps — un geste plus risqué que le
    # défaut qu'il répare, et qu'on renonçait à faire.
    Capability(
        key="org.instruction.describe", handler=_describe_instruction,
        Input=InstrDescribeInput,
        authz=ORG_ADMIN_OPT("org"), Output=InstructionDescribed,
        description=("Fix a procedure's TITLE and/or DESCRIPTION without resending its "
                     "body (org_admin). These two fields are the line an agent reads in "
                     "the catalog to CHOOSE a procedure, so they go stale fastest — and "
                     "until now correcting them meant re-sending the whole `body_md` "
                     "through PUT, which risks degrading prose you did not mean to "
                     "touch. The body and slots are carried over UNCHANGED. This still "
                     "bumps the version and snapshots the previous one, so a bad edit is "
                     "undone with `from_version` like any other write. Pass at least one "
                     "of `title` / `description`; `expected_version` (the version you "
                     "read) turns a concurrent edit into a 409 instead of an overwrite. "
                     "`org` pins to an explicit org id (default = active org). To change "
                     "the BODY, use PUT /api/me/instructions/{slug}."),
        errors=(DeclaredError(400, "nothing_to_describe",
                              "ni `title` ni `description` fourni — rien n'a été écrit "
                              "(une correction vide consommerait une version)"),
                DeclaredError(409, "version_conflict",
                              "`expected_version` fourni et ≠ version courante — la "
                              "correction n'a pas eu lieu")),
        rest=RestBinding("PATCH", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.delete", handler=_delete_instruction, Input=GuideDeleteInput,
        authz=ORG_ADMIN_OPT("org"), Output=InstructionDeleted,
        description=("Delete a guide and its history (org_admin). Pass the EXACT slug. "
                     "`org` pins to an explicit org id (default = active org; must be "
                     "org_admin of it)."),
        rest=RestBinding("DELETE", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.archive", handler=_archive_instruction, Input=GuideDeleteInput,
        authz=ORG_ADMIN_OPT("org"), Output=InstructionArchived,
        description=("Retire a guide WITHOUT destroying it (org_admin) — prefer this "
                     "to `delete` whenever the point is 'stop using it', not 'erase it'. "
                     "The procedure and its whole version history stay in place; it simply "
                     "leaves every listing, including the skills index you read, so it "
                     "stops being offered and followed. Pass the EXACT slug. `org` pins to "
                     "an explicit org id (default = active org; must be org_admin of it)."),
        rest=RestBinding("POST", "/api/me/instructions/{slug}/archive"),
    ),
    Capability(
        key="org.instruction.revert", handler=_instruction_revert, Input=RevertInput,
        authz=ORG_ADMIN, Output=InstructionReverted,
        rest=RestBinding("POST", "/api/me/instructions/{slug}/revert"),
    ),
    # ── Palier admin (org ciblée par org_id ; cross-org = platform admin) ────
    Capability(
        key="org.guide.admin_get", handler=_get_guide, Input=AdminGuideGetInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="[ADMIN] Read another org's guide by id (base+index, or one skill).",
        rest=RestBinding("GET", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
    Capability(
        key="org.guide.admin_list", handler=_list_guides, Input=AdminGuideListInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="[ADMIN] List another org's named guides by id (incl. base guide).",
        rest=RestBinding("GET", "/api/admin/orgs/{id}/instructions", _OID),
    ),
    Capability(
        key="org.instruction.admin_set", handler=_set_instruction, Input=AdminInstrSetInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[ADMIN] Write another org's guide by id (cross-org = platform admin).",
        rest=RestBinding("PUT", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
    Capability(
        key="org.instruction.admin_delete", handler=_delete_instruction, Input=AdminSlugInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[ADMIN] Delete another org's guide by id and its history.",
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
]
