"""Le corps d'un nœud, parsé en BLOCS — lot M2 du modèle de contenu (#287).

0054-D2 / 0063-D2 : le corps d'un nœud est une **composition de blocs stockés**, pas
un markdown qu'on reparserait à chaque lecture. On y gagne l'adressage natif (un
bloc a un identifiant, donc une prose peut le désigner), l'édition chirurgicale
(remplacer un paragraphe sans réécrire la page) et le verrouillage fin.

**Ce qui ne devient PAS des blocs** : les révisions. Un instantané sérialisé doit
rester atomique et lisible tel quel — le reconstituer par assemblage serait le
rendre dépendant de l'état courant des blocs. Le courant en table pour l'adressage,
l'historique en document pour l'intégrité (0063-D2).

## L'invariant du parse, et pourquoi il vaut mieux qu'une grammaire

**Chaque bloc porte sa SOURCE EXACTE (`props->>'md'`), et la concaténation des blocs
d'un nœud rend le corps au caractère près.** Ce n'est pas une commodité : c'est ce
qui rend le parse *vérifiable*. Un découpage qui prétend comprendre le markdown
finit toujours par en perdre un bout (une fin de ligne, un espace significatif dans
un bloc de code, une liste indentée) — et cette perte n'est visible qu'au moment où
quelqu'un relit sa page et n'y reconnaît plus ce qu'il avait écrit. Ici la propriété
se teste en une ligne : `render_blocks(parse_blocks(md)) == md`, sur n'importe quel
corpus.

Corollaire assumé : **une seule source de vérité par bloc**. Le contenu d'un bloc de
code n'est pas stocké une deuxième fois « en structuré » à côté de sa source — deux
copies d'une même donnée finissent par diverger. `code_of()` le dérive à la demande.

## Le grain : paragraphes, titres, et le code isolé

- une **clôture de code** (``` / ~~~) est un bloc `code` à elle seule (0054-D2 :
  « code, isolé du texte ») ; son info-string devient `props->>'lang'` ;
- le reste se coupe aux **lignes vides** et **autour des titres**, ce qui reproduit
  l'outline du document — le grain qu'attend n'importe quel éditeur de blocs.
- l'**inline nu** (lien, emphase, mention sans attribut) reste du markup DANS le bloc
  texte (0054-D2, tranché le 05/08) : couper un paragraphe en trois parce qu'il
  contient un lien serait une régression de lecture. Les blocs `image` et `référence`
  naîtront des surfaces d'édition, pas d'une conversion de markdown — un markdown
  brut ne porte pas les attributs qui les définissent.

## ⚠️ Aujourd'hui ces blocs sont une PROJECTION

Le corps courant reste `props->>'body_md'` (et, côté legacy, `docs.body_md`). Le
parse est désormais maintenu **dans la transaction d'écriture** des pages et guides,
donc une lecture qui suit une écriture voit des blocs à jour sans attendre personne.
Le rejeu au boot reste en place et rattrape l'ancien stock — marqueur
`props->>'blocks_md5_v2'`, donc **no-op** quand rien ne bouge ; il ne sortira du
démarrage que le jour où les transitions versionnées seront livrées (oto-backend#891).
Aucune surface n'édite aujourd'hui les blocs seuls. Le jour où les blocs
deviennent la source de vérité, c'est l'ÉCRITURE qui les posera et ce module se
réduira au parseur.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from typing import Iterable, Optional

from ._conn import _connect

logger = logging.getLogger(__name__)

TEXT = "text"
CODE = "code"

# Ouverture/fermeture de clôture de code : jusqu'à 3 espaces d'indentation, au moins
# trois backticks ou tildes. L'info-string (le `python` de ```python) n'existe qu'à
# l'ouverture — une ligne de fermeture n'en porte jamais.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(\S[^\n]*)?$")
# Titre ATX. Sert à COUPER, pas à typer : un titre reste du texte (0054-D2 ne
# connaît que texte/code/image/référence — un genre « titre » de plus serait un
# concept de plus, ce que tout le chantier cherche à éviter).
_HEADING = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")


def parse_blocks(md: str) -> list[dict]:
    """Découpe un corps markdown en blocs `{type, md, lang?}`.

    Invariant : `"".join(b["md"] for b in parse_blocks(x)) == x`, toujours.
    """
    if not md:
        return []
    lines = md.splitlines(keepends=True)
    blocks: list[dict] = []
    buf: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = _FENCE.match(lines[i].rstrip("\r\n"))
        if not m:
            buf.append(lines[i])
            i += 1
            continue
        _flush_text(buf, blocks)
        fence, info = m.group(1), (m.group(2) or "").strip()
        chunk, i = [lines[i]], i + 1
        while i < n:
            chunk.append(lines[i])
            close = _FENCE.match(lines[i].rstrip("\r\n"))
            i += 1
            # Ferme sur le MÊME caractère et au moins aussi longue, sans info-string
            # (CommonMark). Une clôture non fermée court jusqu'à la fin du document —
            # c'est aussi ce que fait un rendu markdown.
            if close and close.group(1)[0] == fence[0] \
                    and len(close.group(1)) >= len(fence) and not close.group(2):
                break
        block = {"type": CODE, "md": "".join(chunk)}
        if info:
            block["lang"] = info.split()[0]
        blocks.append(block)
    _flush_text(buf, blocks)
    return blocks


# Marqueurs de puce, ordonnée ou non. `_PUCE` capture le texte de l'item — c'est ce
# que le front attend dans `items[]`, et il ne peut pas le dériver lui-même sans
# reparser du markdown, ce qu'il refuse de faire (et il a raison : ce serait une
# seconde implémentation du parse, qui divergerait de celle-ci).
_PUCE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+(.*)$")
_PUCE_ORDONNEE = re.compile(r"^ {0,3}\d{1,9}[.)][ \t]+")
# Ce qui trahit une structure markdown qu'on ne classe PAS : tableau, citation.
_AUTRE_STRUCTURE = re.compile(r"^ {0,3}(?:\||>)")


def _role_de(md: str) -> tuple[str | None, list[str] | None]:
    """Le RÔLE DE PRÉSENTATION d'un bloc texte, et ses puces s'il en a.

    ⚠️ **Un rôle, jamais un `type`.** 0054-D2 : le `type` dit le SUPPORT (texte, code,
    image, référence) ; la présentation descend en PROPRIÉTÉ. C'est la règle qu'on a
    proposée au front le 16/08 et qu'il a déjà appliquée de son côté — en faire une
    valeur de `type` rouvrirait le second axe qu'on lui a demandé d'éviter, et
    entrerait en collision le jour où `image` et `référence` arrivent.

    Trois rôles seulement, et **rien quand on ne sait pas** : un tableau ou une citation
    ne reçoit aucun rôle plutôt qu'un `paragraph` qui mentirait. L'absence dit « on ne
    classe pas », et le front rend la source comme il l'entend — son `type` est une
    chaîne libre et son objet est ouvert, précisément pour ce cas.

    **`ordered` n'est pas STOCKÉ ici : il se dérive à la lecture** (`ordered_of`, comme
    `code_of`), donc sans rotation de marqueur. Jusqu'au 29/08/2026 ce paragraphe
    refusait de le servir : chez le front, « ordered » désignait UN PAS d'une suite
    numérotée (N blocs rendus dans un même `<ol>`), là où notre parse garde toute la
    liste dans UN bloc — deux notions sous un même nom. Le front tiers l'a depuis
    demandé sur le bloc `role: list` lui-même (« `md` dit 1. 2. 3., `items[]` perd
    l'ordre »), c'est-à-dire avec NOTRE sens : la liste de ce bloc est numérotée. Le
    faux ami est levé par celui qui l'avait posé.
    """
    lignes = [l for l in md.splitlines() if l.strip()]
    if not lignes:
        return None, None
    if _HEADING.match(lignes[0]):
        return "heading", None
    puces = [_PUCE.match(l) for l in lignes]
    if all(puces):
        return "list", [m.group(1).strip() for m in puces if m]
    if any(_AUTRE_STRUCTURE.match(l) for l in lignes):
        return None, None          # tableau, citation : on ne prétend pas classer
    if any(p for p in puces):
        return None, None          # prose ET puces mêlées : idem
    return "paragraph", None


def ordered_of(md: Optional[str]) -> bool:
    """La liste de CE bloc est-elle NUMÉROTÉE ? Dérivé de la source à la lecture,
    comme `code_of` — jamais stocké, donc aucune rotation de marqueur, et les blocs
    déjà projetés le servent dès le déploiement.

    Décidé par le PREMIER item (CommonMark : un changement de marqueur ouvre une autre
    liste ; ici on ne coupe pas, on qualifie). N'a de sens que sur un bloc `role: list`
    — sur autre chose, rend False sans prétendre classer."""
    for ligne in (md or "").splitlines():
        if ligne.strip():
            return bool(_PUCE_ORDONNEE.match(ligne))
    return False


def _flush_text(buf: list[str], blocks: list[dict]) -> None:
    """Vide le tampon de texte en blocs, coupés aux lignes vides et aux titres.

    Les lignes vides restent COLLÉES au bloc qu'elles suivent : le séparateur
    appartient au bloc du dessus. C'est ce qui évite des blocs de blanc, tout en
    gardant la concaténation exacte."""
    if not buf:
        return
    cur: list[str] = []
    pending = False           # une coupure est due avant le prochain contenu

    def close() -> None:
        if not cur:
            return
        piece = "".join(cur)
        if not piece.strip() and blocks:
            # Du blanc seul (typiquement juste après une clôture de code) : il
            # prolonge le bloc précédent plutôt que d'en former un vide.
            blocks[-1]["md"] += piece
        else:
            bloc = {"type": TEXT, "md": piece}
            role, items = _role_de(piece)
            if role:
                bloc["role"] = role
            if items is not None:
                bloc["items"] = items
            blocks.append(bloc)
        cur.clear()

    for line in buf:
        if not line.strip():
            cur.append(line)
            pending = True
            continue
        if pending:
            close()
            pending = False
        if _HEADING.match(line.rstrip("\r\n")):
            close()               # le titre ouvre son propre bloc…
            cur.append(line)
            pending = True        # …et le referme aussitôt
            continue
        cur.append(line)
    close()
    buf.clear()


def render_blocks(blocks: Iterable[dict]) -> str:
    """Le corps, reconstitué depuis ses blocs. Exactement l'original."""
    return "".join(b["md"] for b in blocks)


def code_of(block: dict) -> Optional[str]:
    """Le contenu d'un bloc de code, DÉRIVÉ de sa source (jamais stocké à part :
    deux copies d'une même donnée finissent par diverger). None si ce n'en est pas."""
    if block.get("type") != CODE:
        return None
    body = block["md"].splitlines(keepends=True)[1:]     # sans la clôture ouvrante
    # Le séparateur qui suit une clôture lui a été COLLÉ (cf. `_flush_text`) : on
    # remonte au-delà de ce blanc avant de chercher la fermeture, sinon un bloc suivi
    # d'une ligne vide se lit comme une clôture non fermée et rend ses backticks.
    end = len(body)
    while end and not body[end - 1].strip():
        end -= 1
    if end and _FENCE.match(body[end - 1].rstrip("\r\n")):
        return "".join(body[:end - 1])
    return "".join(body)                                  # clôture jamais fermée


# --- Backfill au boot ---------------------------------------------------------

# Le marqueur d'idempotence. `md5` et pas sha : c'est l'empreinte que PostgreSQL
# calcule nativement (`md5(text)`), donc le filtre SQL ci-dessous peut se comparer
# sans que Python n'ait à relire un seul corps quand rien n'a bougé — le boot
# nominal coûte UNE requête qui ne rend rien.
#
# ⚠️ **Le nom du marqueur PORTE LA VERSION DU PARSE, et c'est le mécanisme de
# migration.** Le marqueur est l'empreinte du CORPS : tant que le corps ne bouge pas,
# rien ne se re-projette — ce qui est le but, sauf quand c'est le PARSE qui change.
# Le 21/08, l'étiquetage (rôle de présentation + puces) a changé ce que le parse
# PRODUIT sans toucher aux corps : sans ce renommage, aucun nœud existant n'aurait
# jamais reçu son rôle, et la surface aurait servi deux populations de blocs — les
# anciens muets, les neufs étiquetés — sans que rien ne le signale.
#
# Bumper le suffixe = une re-projection de tous les nœuds, une fois, en lots, au boot.
# Elle est **gratuite pour les références externes** : la clé de rapprochement est la
# SOURCE SEULE, donc chaque bloc retrouve son identifiant. C'est précisément ce qui
# rend ce geste anodin aujourd'hui, et ce qui l'aurait rendu coûteux avant #362.
_MARKER = "blocks_md5_v2"
# L'ancien nom, retiré des props à la re-projection : un marqueur qui ne veut plus rien
# dire est de la carte qui ment — le prochain lecteur croirait qu'il pilote quelque chose.
_MARKER_PERIME = "blocks_md5"

_SELECT_STALE = (
    "SELECT id, public_id, COALESCE(props->>'body_md', '') AS body "
    "FROM nodes WHERE props ? 'body_md' "
    f"AND (props->>'{_MARKER}') IS DISTINCT FROM md5(COALESCE(props->>'body_md', '')) "
    "AND NOT (id = ANY(%s)) ORDER BY id LIMIT %s"
)

_INSERT_BLOCK = (
    "INSERT INTO blocks (public_id, node_id, position, type, props) "
    "VALUES (%s, %s, %s, %s, %s::jsonb) "
    "ON CONFLICT ON CONSTRAINT blocks_public_id_key DO UPDATE SET "
    "  position = EXCLUDED.position, type = EXCLUDED.type, "
    "  props = EXCLUDED.props, updated_at = NOW()"
)


def _new_block_id() -> str:
    """L'identifiant d'un bloc est un TIRAGE — jamais dérivé du rang ni du contenu
    (#362, qui RETOURNE l'ancien schéma md5(nœud:rang) : l'identité positionnelle
    faisait qu'un paragraphe inséré en tête ré-identifiait TOUS les blocs en
    dessous — toute référence externe cassait au premier réordonnancement, la
    classe de coût qu'on paie sur docs(id) en ce moment même. Trivial à changer
    tant que rien ne lit les blocs, très cher après). Même schéma de tirage
    qu'un nœud natif (0059-D3) ; l'identité posée est ensuite CONSERVÉE par le
    rapprochement de `write_node_blocks`."""
    return "blk_" + secrets.token_hex(12)


def write_node_blocks(conn, node_id: int, body: str) -> int:
    """(Ré)écrit les blocs d'UN nœud depuis son corps, et pose le marqueur.

    **L'identité survit à la re-projection** (#362) : les blocs existants du nœud
    sont lus d'abord, et chaque bloc parsé RÉCUPÈRE l'identifiant d'un existant
    reconnaissable — **même SOURCE exacte** — apparié dans l'ordre des positions,
    chaque existant consommé au plus une fois. Insérer ou déplacer ne ré-identifie
    donc jamais les blocs intacts ; un bloc ÉDITÉ prend une identité neuve, et c'est
    assumé — mieux vaut une adresse neuve qu'une adresse qui pointe un texte qui
    n'est plus celui qu'on visait. Les cas ambigus (deux blocs de même source)
    s'apparient dans l'ordre : stable au no-op.

    ⚠️ **La clé de rapprochement est la SOURCE SEULE, plus `(type, source)`** — changé
    le 21/08, et ce n'est pas un détail d'implémentation. Le lot qui vient étiquette les
    blocs (`text` → titre / paragraphe / liste) : avec le type dans la clé, ré-étiqueter
    un corps **inchangé au caractère près** aurait fait tourner toutes les identités,
    donc cassé toute référence externe — pour un texte que personne n'avait touché. Une
    rotation gratuite, et la seconde en deux mois après celle de #362.

    C'est sûr, et vérifié dans les deux sens plutôt que supposé :
    - **structurellement**, un bloc `code` porte ses clôtures DANS sa source (c'est une
      clôture qui l'ouvre) et un bloc `text` ne peut pas en contenir (une clôture est
      précisément ce qui le termine) ⟹ les deux familles ont des sources DISJOINTES ;
    - **empiriquement**, zéro collision sur les 140 blocs des corps réels du dépôt ;
    - **après l'étiquetage**, les types seront DÉRIVÉS de la source (un titre commence
      par `#`) ⟹ même source, même type : la disjonction tient encore.

    Le seul cas résiduel — deux blocs strictement identiques dans un même nœud — est
    celui que l'appariement par ordre départage déjà.

    ⚠️ Le marqueur est l'empreinte du corps QU'ON VIENT DE PARSER, pas une relecture
    SQL de `props->>'body_md'` : si le corps a changé entre la lecture et l'écriture,
    l'empreinte ne correspondra pas au nouveau corps et le boot suivant re-parsera.
    Relire en SQL stamperait au contraire le nouveau corps avec les blocs de
    l'ancien — un décalage définitif, et silencieux."""
    parsed = parse_blocks(body)
    dispo: dict[str, list] = {}
    for r in conn.execute(
            "SELECT public_id, props->>'md' AS md FROM blocks "
            "WHERE node_id = %s ORDER BY position", (node_id,)).fetchall():
        dispo.setdefault(r["md"] or "", []).append(r["public_id"])
    conn.execute("DELETE FROM blocks WHERE node_id = %s", (node_id,))
    if parsed:
        params = []
        for idx, b in enumerate(parsed):
            reconnus = dispo.get(b["md"])
            pid = reconnus.pop(0) if reconnus else _new_block_id()
            props = {k: v for k, v in b.items() if k != "type"}
            params.append((pid, node_id,
                           (idx + 1) * 16, b["type"], json.dumps(props)))
        with conn.cursor() as cur:
            cur.executemany(_INSERT_BLOCK, params)
    conn.execute(
        f"UPDATE nodes SET props = (props - '{_MARKER_PERIME}') "
        f"|| jsonb_build_object('{_MARKER}', %s::text) "
        "WHERE id = %s",
        (hashlib.md5(body.encode("utf-8")).hexdigest(), node_id))
    return len(parsed)


def count_stale_nodes() -> int:
    """Combien de nœuds `backfill_node_blocks` aurait à re-parser — le « à blanc » de
    la commande, et la sonde qui dit si une rotation de marqueur vient d'avoir lieu."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM nodes WHERE props ? 'body_md' "
            f"AND (props->>'{_MARKER}') IS DISTINCT FROM md5(COALESCE(props->>'body_md', ''))"
        ).fetchone()
        return int(row["n"]) if row else 0


def backfill_node_blocks(*, batch: int = 200) -> int:
    """Parse le corps des nœuds qui n'ont pas (ou plus) leurs blocs. Rejouable.

    Jouée par `oto-mcp maintenance blocks` (timer), **plus au boot** (ADR 0065, lot 0) :
    en régime stable elle ne coûtait qu'une sonde, mais une ROTATION DE MARQUEUR la
    fait re-parser tout le corpus — celle de `blocks_md5` vers `blocks_md5_v2` a
    re-parsé 1 526 nœuds, soit ~19 s dans la fenêtre du healthcheck, ajoutées par un
    lot qui ne savait pas les ajouter. **Fail-open, par nœud** : ces blocs ne sont lus
    que par la vue d'un nœud (`db/node_view.py`), qui tolère d'être en retard d'un tir
    de timer — faire tomber une passe de maintenance pour un markdown biscornu serait
    hors de proportion. L'échec est loggué et le nœud ÉCARTÉ de cette passe ; sans
    cet écart, il serait resélectionné indéfiniment (son marqueur n'ayant pas été
    posé) et bloquerait tous les suivants. Il est retenté à la passe d'après.

    ⚠️ Le marqueur vit dans `props`, que la conversion RÉÉCRIT en newer-wins quand la
    source change : un corps édité perd donc son marqueur et se re-parse à la passe
    suivante. C'est voulu — les blocs sont une projection tant que l'écriture n'est
    pas basculée."""
    done: int = 0
    skipped: list[int] = []
    while True:
        with _connect() as conn:
            rows = conn.execute(_SELECT_STALE, (skipped, batch)).fetchall()
            if not rows:
                return done
            for r in rows:
                try:
                    with conn.transaction():
                        write_node_blocks(conn, int(r["id"]), r["body"] or "")
                    done += 1
                except Exception:
                    logger.warning("blocs : nœud %s non parsé (fail-open)",
                                   r["public_id"], exc_info=True)
                    skipped.append(int(r["id"]))
