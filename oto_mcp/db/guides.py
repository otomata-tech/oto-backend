"""Couches de contexte & how-to : la façade `guides`, servie par la table `nodes`.

Historiquement la table `guides` (ADR 0042). Depuis le **lot M1** du chantier
modèle de contenu (blueprint ADR 0054/0063), les lignes vivent dans **`nodes`** —
et le mot « guide » ne désigne plus qu'une **surface** (`oto_guide`,
`/api/me/guides/*`), plus un objet du modèle :

- **une couche de contexte EST une page** (0055-D4) — d'où `kind='page'` ici, et
  pas un hypothétique `kind='guide'` ;
- la **livraison** (`init` injecté au handshake / `on-demand` chargé par
  `oto_guide`) était la NATURE d'une ligne de `guides` ; ce n'est plus qu'une
  **propriété** du nœud, `props->>'delivery'`. Les deux familles de fonctions
  ci-dessous ne survivent que parce que les surfaces, elles, ne changent pas :
  elles ne se distinguent plus que par la valeur d'une clé JSON.

**Contrat inchangé** : mêmes signatures, mêmes clés de retour (`scope`,
`owner_id`, `slug`, `title`, `description`, `body_md`, `delivery`, `updated_at`)
que du temps de la table `guides` — `guide_store` et tout ce qui est au-dessus
n'a pas bougé d'une ligne, et les tests qui bouchonnent ces fonctions non plus.
Le `scope` de la surface est l'`owner_type` du nœud (même vocabulaire :
platform | org | group | user), le `owner_id` reste le même texte.

⚠️ **La table `guides` a été retirée du code le 23/09/2026** (otomata-tech/oto#239) :
son DDL, ses `ALTER` de démarrage, ses index de recherche et la recopie
`guides` → `nodes` jouée à chaque boot. Elle n'avait plus d'écrivain applicatif
depuis le lot M1, mais le démarrage continuait d'y écrire, et un `CREATE TABLE IF
NOT EXISTS` l'aurait fait renaître après un `DROP`. Le `DROP` lui-même est un geste
d'exploitation, pas un geste de démarrage (docs/live-migrations.md).

L'identifiant public (0059-D3) d'une couche de contexte est **dérivé de sa clé
naturelle** `(scope, owner, slug)` — cf. `_PID`. Distinct des PROCÉDURES
(`org_instructions`, slots/versioning), qui restent une table à part jusqu'à leur
propre lot — mais avec qui elles partagent l'espace des slugs d'une portée : la garde
de cette frontière est ici (`SlugDeLAutreFamille`, oto#100). Ré-exporté par
`db/__init__`.

**Les versions sont gardées** (03/11/2026) : chaque remplacement (`set_guide_db`,
`patch_guide_db`, `set_init_guide_db`) et chaque suppression (`delete_guide_db`) garde
l'état PRÉCÉDENT dans `guide_revisions`, dans la même transaction, sous le même verrou
(`_garder_si_remplace` → `db/guide_revisions.py`, qui porte aussi la rétention). Le
semis et son geste de maintenance (`seed_guide_db`, `aligner_guide_db`) n'en gardent
pas : ils posent le contenu d'un fichier du dépôt, versionné par git.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from . import guide_revisions
from ._conn import _connect

# Le `kind` d'une couche de contexte. Une page, comme les autres (0055-D4) : ce
# qui la distingue d'une page de projet est une PROPRIÉTÉ (`delivery`), pas sa
# nature — c'est tout le propos du lot M1.
_KIND = "page"


# L'identifiant public (0059-D3) d'une couche de contexte, DÉRIVÉ de sa clé naturelle
# `(scope, owner, slug)`. Les casts `::text` sont nécessaires : sans eux, `||` sur des
# paramètres de type inconnu laisse PostgreSQL sans résolution d'opérateur.
#
# **Pourquoi dérivé, alors que 0059-D3 veut un opaque tiré au sort** : l'unicité de
# `public_id` porte exactement l'invariant que la table `guides` portait en
# `UNIQUE (scope, owner_id, slug)` — une couche par (scope, propriétaire, slug) — et
# `ON CONFLICT (public_id)` l'arbitre sans index supplémentaire. La dérivation exige
# que la clé naturelle soit IMMUABLE : elle l'est ici, la surface guide n'a pas de
# renommage (on écrit et on supprime par slug). Un nœud NATIF (pages, tableaux,
# lignes — lots M2+) se renomme, lui : son identifiant est tiré au sort, jamais
# dérivé. Ne pas généraliser ceci.
_PID = ("'nod_' || substr(md5('ctx:' || %s::text || ':' || %s::text "
        "|| ':' || %s::text), 1, 24)")

# Projection nœud → forme historique d'une ligne `guides`. `scope` EST l'owner_type
# (même vocabulaire), les champs de prose vivent dans `props`. COALESCE parce que
# `guides` les portait NOT NULL DEFAULT '' : une clé absente ne doit pas rendre None.
#
# `seed_sha256` est ici depuis le 23/09/2026 (oto#236) : tous les appelants
# reprojettent déjà ce dict en clés explicites (`guide_store`, les routes REST,
# `scripts/aligner_guides_plateforme.py`) — l'ajouter ne fuite nulle part, et son
# absence faisait lire `None` au script de maintenance : chaque guide plateforme
# réaligné se voyait donc rapporté « empreinte ABSENTE » à la relecture qui suit
# `--aligner`, alors qu'elle vient d'être posée — un opérateur croit l'alignement
# raté et le rejoue sans fin.
_COLS = ("id, owner_type AS scope, owner_id, props->>'slug' AS slug, "
         "COALESCE(props->>'title', '') AS title, "
         "COALESCE(props->>'description', '') AS description, "
         "COALESCE(props->>'body_md', '') AS body_md, "
         "props->>'delivery' AS delivery, props->>'seed_sha256' AS seed_sha256, "
         "created_at, updated_at")

# --- Un slug, une famille, par portée (otomata-tech/oto#100) ------------------
#
# Les guides à charger (ici) et les PROCÉDURES (`org_instructions`) partagent le même
# espace de slugs, dans deux stockages sans lien : sous une même portée
# `(owner_type, owner_id)`, `oto_guide` lit l'un, `oto_procedure` et le runner lisent
# l'autre. Deux objets sous un slug, c'est deux consignes dont une seule est tenue à
# jour — mesuré en production, un agent a exécuté la copie périmée sans que rien ne le
# dise. D'où la règle : une CRÉATION (ou un renommage) qui ferait coexister les deux
# familles sous un slug, dans la même portée, est refusée ; la mise à jour d'un objet
# existant ne l'est pas (les doublons déjà en base se reconnaissent avant de se
# refuser, cf. l'issue).
#
# La garde vit ICI, à la couche basse, pour les deux familles : le store des
# procédures importe `db`, jamais l'inverse. Elle se prend sous LE MÊME verrou
# advisory que l'écriture d'une procédure (`oi:<palier>:<owner>:<slug>`) : sans lui,
# une création de guide et une création de procédure simultanées passeraient chacune
# leur sonde avant l'écriture de l'autre.

class SlugDeLAutreFamille(Exception):
    """Le slug visé porte déjà un objet de l'AUTRE famille dans la même portée.

    `existant` décrit ce qui occupe la place, de quoi le NOMMER dans le refus servi :
    `{"famille": "procedure", "scope", "slug", "guide_id", "version", "archived"}` ou
    `{"famille": "guide", "scope", "slug", "public_id", "title"}`."""

    def __init__(self, existant: dict):
        self.existant = existant
        s, sc = existant["slug"], existant["scope"]
        if existant["famille"] == "procedure":
            super().__init__(
                f"`{s}` (scope {sc}) porte déjà une PROCÉDURE (#{existant['guide_id']}, "
                f"v{existant['version']}" + (", archivée" if existant["archived"] else "")
                + ") : un guide du même slug dans la même portée coexisterait avec elle, "
                "et l'agent lirait l'une ou l'autre selon l'outil — refusé, rien n'a été "
                f"écrit. Pour changer cette consigne : `oto_procedure(op='set', "
                f"slug='{s}', scope='{sc}')`. Pour un guide distinct : un autre slug.")
        else:
            super().__init__(
                f"`{s}` (scope {sc}) porte déjà un GUIDE à charger (`{existant['public_id']}`"
                + (f", « {existant['title']} »" if existant.get("title") else "")
                + ") : une procédure du même slug dans la même portée coexisterait avec "
                "lui, et l'agent lirait l'un ou l'autre selon l'outil — refusé, rien n'a "
                f"été écrit. Pour changer ce texte : `oto_guide(op='write', slug='{s}', "
                f"scope='{sc}')`. Pour en faire une procédure : supprime d'abord le guide "
                f"(`oto_guide(op='delete', slug='{s}', scope='{sc}')`), ou prends un "
                "autre slug.")


def verrouiller_le_slug(conn, owner_type: str, owner_id: str, slug: str) -> None:
    """LE verrou d'un slug dans une portée, commun aux deux familles — celui que
    l'écriture d'une procédure prend depuis toujours. Transactionnel : tenu jusqu'au
    commit de `conn`."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"oi:{owner_type}:{owner_id}:{slug}",))


def procedure_du_slug(owner_type: str, owner_id: str, slug: str,
                      conn=None) -> Optional[dict]:
    """La procédure (archivée comprise) qui porte `slug` dans la portée, ou None —
    sous la forme `existant` de `SlugDeLAutreFamille`."""
    if conn is None:
        with _connect() as c:
            return procedure_du_slug(owner_type, owner_id, slug, c)
    row = conn.execute(
        "SELECT id, version, archived_at FROM org_instructions "
        "WHERE owner_type = %s AND owner_id = %s AND slug = %s",
        (owner_type, str(owner_id), slug),
    ).fetchone()
    if row is None:
        return None
    return {"famille": "procedure", "scope": owner_type, "slug": slug,
            "guide_id": row["id"], "version": row["version"],
            "archived": row["archived_at"] is not None}


def guide_du_slug(owner_type: str, owner_id: str, slug: str,
                  conn=None) -> Optional[dict]:
    """Le guide À CHARGER qui porte `slug` dans la portée, ou None — sous la forme
    `existant` de `SlugDeLAutreFamille`. Un readme `init` n'en est pas un : son slug
    est canonique (`readme`) et aucune lecture de procédure ne le confond."""
    if conn is None:
        with _connect() as c:
            return guide_du_slug(owner_type, owner_id, slug, c)
    row = conn.execute(
        f"SELECT public_id, COALESCE(props->>'title', '') AS title FROM nodes "
        f"WHERE public_id = {_PID} AND props->>'delivery' = 'on-demand'",
        (owner_type, str(owner_id), slug),
    ).fetchone()
    if row is None:
        return None
    return {"famille": "guide", "scope": owner_type, "slug": slug,
            "public_id": row["public_id"], "title": row["title"]}


def exiger_aucun_guide(conn, owner_type: str, owner_id: str, slug: str) -> None:
    """Refuse (`SlugDeLAutreFamille`) une procédure NEUVE sur un slug qu'un guide porte.
    À appeler sous `verrouiller_le_slug`, dans la transaction qui écrit."""
    existant = guide_du_slug(owner_type, owner_id, slug, conn)
    if existant is not None:
        raise SlugDeLAutreFamille(existant)


def _exiger_aucune_procedure(conn, owner_type: str, owner_id: str, slug: str) -> None:
    existant = procedure_du_slug(owner_type, owner_id, slug, conn)
    if existant is not None:
        raise SlugDeLAutreFamille(existant)


# --- On-demand (catalogue `oto_guide`) : delivery='on-demand' UNIQUEMENT ------

def _body_before_write(conn, scope: str, owner_id: str, slug: str) -> Optional[str]:
    row = conn.execute(
        f"SELECT COALESCE(props->>'body_md', '') AS body FROM nodes WHERE public_id = {_PID} FOR UPDATE",
        (scope, str(owner_id), slug),
    ).fetchone()
    return row["body"] if row else None


def _maintain_projections(conn, row: dict, body_md: Optional[str]) -> None:
    # Corps explicite modifié seulement : une édition de titre ne réécrit aucun
    # bloc. Même transaction que le contenu, aucune réparation nocturne requise.
    from .blocks import write_node_blocks
    from .search import stamp_rank_vector
    if body_md is not None:
        write_node_blocks(conn, row["id"], body_md)
    stamp_rank_vector(conn, "nodes", "id = %s", (row["id"],))

def list_guides_db(scope: str, owner_id: str) -> list[dict]:
    """Guides ON-DEMAND d'un (scope, owner), triés par slug — métadonnées + corps.
    Exclut les readmes init (delivery='init')."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM nodes "
            f"WHERE kind = '{_KIND}' AND owner_type = %s AND owner_id = %s "
            "AND props->>'delivery' = 'on-demand' ORDER BY props->>'slug'",
            (scope, str(owner_id)),
        ).fetchall()
        return [dict(r) for r in rows]


def _get_one(scope: str, owner_id: str, slug: str, delivery: str) -> Optional[dict]:
    """La couche de contexte `(scope, owner, slug)` SI elle a cette livraison.
    Lookup par identifiant public dérivé = un accès à l'index d'identité."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = %s",
            (scope, str(owner_id), slug, delivery),
        ).fetchone()
        return dict(row) if row else None


def get_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    return _get_one(scope, owner_id, slug, "on-demand")


def set_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                 title: str = "", description: str = "", *,
                 expected_rev: Optional[str] = None, edited_by: Optional[str] = None,
                 reason: str = "write") -> Optional[dict]:
    """Crée ou met à jour (upsert par `(scope, owner_id, slug)`) un guide ON-DEMAND.

    La mise à jour ne touche QUE la prose — `delivery` n'est posé qu'à l'insertion,
    exactement comme la table `guides` ne le mettait pas à jour. `embed_dirty` suit
    la prose (#282) : écrire une couche la remet dans l'outbox sémantique, comme
    `guides` le faisait par sa colonne.

    **Renvoie `None` quand la clé est déjà occupée par une couche `init`** — et rien
    n'a alors été écrit. C'est le trou du 09/09/2026 : `public_id` dérive de
    `(scope, owner, slug)` et IGNORE `delivery`, alors que toutes les lectures
    filtrent dessus. Un `PUT …/guides/org/readme` on-demand tombait donc sur la MÊME
    ligne que le readme injecté de l'org, en remplaçait le corps sans changer son
    `delivery`, et rendait 200 : la prose reçue par toutes les sessions de l'org était
    détruite, et le guide écrit restait introuvable à la lecture (qui, elle, exige
    `delivery='on-demand'`). Le refus est porté par le `WHERE` de l'`ON CONFLICT` —
    donc par l'instruction elle-même, pas par un pré-contrôle qu'une écriture
    concurrente traverserait.

    **Lève `SlugDeLAutreFamille` quand la CRÉATION tomberait sur le slug d'une
    procédure** de la même portée (oto#100) — rien n'est alors écrit. Mettre à jour un
    guide qui existe déjà reste permis, doublon ou non.

    **Lève `GuideConflict` si `expected_rev` n'est pas le rev en place** (signal
    #1203, `REV_ABSENT` = « il n'y a rien ») — rien n'est alors écrit. La ligne rendue
    porte `previous_rev`, le rev de ce que l'écriture a remplacé.

    Ce qu'elle remplace est gardé comme version (`guide_revisions`, raison `reason` :
    `write`, ou `restore` pour une restauration), au nom de `edited_by`."""
    with _connect() as conn:
        # Une CRÉATION ne fait pas coexister un guide et une procédure (oto#100) —
        # sous le verrou commun aux deux familles (pris par `_exiger_la_version`),
        # avant la sonde et l'écriture.
        previous_rev, avant = _exiger_la_version(conn, scope, owner_id, slug,
                                                 "on-demand", expected_rev)
        previous_body = _body_before_write(conn, scope, owner_id, slug)
        if previous_body is None:
            _exiger_aucune_procedure(conn, scope, str(owner_id), slug)
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'on-demand', "
            "                           'title', %s::text, 'description', %s::text, "
            "                           'body_md', %s::text, 'embed_dirty', TRUE)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET "
            "  props = nodes.props || jsonb_build_object("
            "      'title', %s::text, 'description', %s::text, 'body_md', %s::text, "
            "      'embed_dirty', TRUE), "
            "  updated_at = NOW() "
            "  WHERE nodes.props->>'delivery' = 'on-demand' "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug,                       # public_id dérivé
             scope, str(owner_id),                             # owner_type, owner_id
             slug, title, description, body_md,                # props à l'insertion
             title, description, body_md),                     # prose à la mise à jour
        ).fetchone()
        if row is None:                       # une couche `init` occupe la clé
            return None
        _garder_si_remplace(conn, avant, dict(row), "on-demand", reason, edited_by)
        _maintain_projections(conn, row, body_md if body_md != previous_body else None)
        return {**dict(row), "previous_rev": previous_rev}


def empreinte_de_couche(title: str, description: str, body_md: str) -> str:
    """L'empreinte d'une couche de contexte — ce qui permet de dire si la BASE a
    bougé depuis le dernier semis, et si le FICHIER a bougé depuis celui-ci.

    Sur les TROIS champs que le semis pose, pas seulement le corps : un titre ou une
    description corrigés dans le dépôt doivent atteindre la base comme un corps
    réécrit — c'est ce que l'index d'`oto_guide` sert à l'agent avant qu'il ne charge
    quoi que ce soit. Le séparateur `\0` n'apparaît dans aucun des trois : sans lui,
    déplacer une fin de titre au début de la description rendrait la même empreinte.
    """
    brut = "\0".join([title or "", description or "", body_md or ""])
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def guide_rev(title: Optional[str], description: Optional[str],
              body_md: Optional[str]) -> str:
    """Jeton de version d'un guide, dérivé de son CONTENU — l'ETag que `doc_rev` rend
    pour une page, sur les trois champs qu'une écriture de guide pose. Ni colonne de
    version ni migration ; deux écritures au même contenu = même rev."""
    return empreinte_de_couche(title or "", description or "", body_md or "")[:16]


# Le `rev` de ce qui n'a PAS de contenu à cette clé : aucune ligne, ou un readme vidé
# (signal #1203). C'est ce que rend la lecture d'un readme vide, et ce qu'un écrivain
# repasse pour dire « j'écris là où il n'y a rien » — une création concurrente le fait
# alors échouer au lieu d'être écrasée.
REV_ABSENT = "absent"


def rev_de_couche(row: Optional[dict]) -> str:
    """Le `rev` d'une couche lue (`_COLS`), ou `REV_ABSENT` sans contenu."""
    if not row or not (row.get("body_md") or "").strip():
        return REV_ABSENT
    return guide_rev(row.get("title"), row.get("description"), row.get("body_md"))


def _garder_si_remplace(conn, avant: Optional[dict], apres: Optional[dict],
                        delivery: str, reason: str, edited_by: Optional[str]) -> None:
    """Garde `avant` comme version de sa couche si l'écriture le REMPLACE vraiment :
    rien quand il n'y avait rien (première écriture, readme vide), rien quand `apres`
    porte le même contenu. `apres=None` = la couche est supprimée. Dans `conn`, donc
    dans la transaction et sous le verrou de l'écriture."""
    rev = rev_de_couche(avant)
    if rev == REV_ABSENT or (apres is not None and rev_de_couche(apres) == rev):
        return
    guide_revisions.garder(conn, avant, delivery=delivery, rev=rev, reason=reason,
                           edited_by=edited_by)


def _exiger_la_version(conn, scope: str, owner_id: str, slug: str, delivery: str,
                       expected_rev: Optional[str]) -> tuple[str, Optional[dict]]:
    """Le `rev` en place à cette clé, pour cette livraison, et la ligne elle-même
    (`None` sans ligne) — sous le verrou du slug et de la ligne, donc sans fenêtre
    entre le contrôle et l'écriture qui suit dans `conn`. Lève `GuideConflict` si
    `expected_rev` est donné et diffère (signal #1203 : un readme d'org posé par un
    tiers entre la lecture et l'écriture d'un agent était remplacé sans que personne
    ne le voie). Rend l'état d'AVANT l'écriture — celui qu'elle garde en version."""
    verrouiller_le_slug(conn, scope, str(owner_id), slug)
    courant = conn.execute(
        f"SELECT {_COLS} FROM nodes WHERE public_id = {_PID} "
        "AND props->>'delivery' = %s FOR UPDATE",
        (scope, str(owner_id), slug, delivery)).fetchone()
    avant = dict(courant) if courant else None
    rev = rev_de_couche(avant)
    if expected_rev is not None and expected_rev != rev:
        raise GuideConflict(rev)
    return rev, avant


class SemisConflitDeLivraison(ValueError):
    """Le slug d'un guide plateforme est occupé par une couche `init` (readme injecté).

    Écrire là remplacerait le readme d'accueil par un how-to sans que sa livraison
    change : la prose servie à toutes les sessions serait détruite, et le guide semé
    resterait introuvable à la lecture (qui exige `delivery='on-demand'`). Même trou
    que celui fermé le 09/09/2026 dans `set_guide_db`, pris du côté du semis."""


def seed_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                  title: str = "", description: str = "", *,
                  seed_sha256: str) -> str:
    """Sème un guide ON-DEMAND depuis son FICHIER, et le met à jour si le fichier a
    changé sans que la base ait été éditée. Renvoie le verdict de ce guide :

    - `seme` — la ligne n'existait pas, elle est posée ;
    - `mis_a_jour` — le fichier a changé, la base était encore au dernier semis ;
    - `inchange` — même empreinte des deux côtés, aucune écriture ;
    - `diverge` — la base a été éditée depuis le dernier semis : **on conserve**
      l'édition, l'appelant la signale ;
    - `sans_empreinte` — ligne posée avant que le semis n'empreinte : on ne devine
      pas laquelle des deux fait foi, c'est le geste de maintenance
      (`scripts/aligner_guides_plateforme.py`) qui tranche, jamais le démarrage.

    ⚠️ La base reste la source ÉDITABLE (ADR 0042) : ce n'est pas le fichier qui
    gagne, c'est le fichier qui atteint enfin une base que personne n'a touchée.
    L'empreinte posée dans `props->>'seed_sha256'` est ce qui distingue les deux —
    sans elle, le semis en insertion seule laissait une mise à jour du dépôt sans
    jamais atteindre un environnement existant (otomata-tech/oto#236).

    Décision et écriture dans la MÊME transaction, ligne verrouillée (`FOR UPDATE`) :
    une édition admin concurrente ne peut pas se glisser entre le constat et l'écrasement.
    """
    with _connect() as conn:
        courant = conn.execute(
            f"SELECT id, props FROM nodes WHERE public_id = {_PID} FOR UPDATE",
            (scope, str(owner_id), slug),
        ).fetchone()
        if courant is None:
            row = conn.execute(
                f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
                f"VALUES ({_PID}, '{_KIND}', %s, %s, "
                "        jsonb_build_object('slug', %s::text, 'delivery', 'on-demand', "
                "                           'title', %s::text, 'description', %s::text, "
                "                           'body_md', %s::text, 'embed_dirty', TRUE, "
                "                           'seed_sha256', %s::text)) "
                "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO NOTHING RETURNING id",
                (scope, str(owner_id), slug, scope, str(owner_id),
                 slug, title, description, body_md, seed_sha256),
            ).fetchone()
            if row is None:                   # course perdue : quelqu'un vient de poser
                return "inchange"
            _maintain_projections(conn, row, body_md)
            return "seme"

        props = courant["props"] or {}
        if props.get("delivery") != "on-demand":
            raise SemisConflitDeLivraison(
                f"`{slug}` (scope {scope}) porte une couche `{props.get('delivery')}` : "
                "le semis n'écrit pas par-dessus un readme injecté.")
        posee = (props.get("seed_sha256") or "").strip()
        if not posee:
            return "sans_empreinte"
        if posee != empreinte_de_couche(props.get("title") or "",
                                        props.get("description") or "",
                                        props.get("body_md") or ""):
            return "diverge"
        if posee == seed_sha256:
            return "inchange"
        _ecrire_le_semis(conn, courant["id"], title, description, body_md, seed_sha256)
        return "mis_a_jour"


def _ecrire_le_semis(conn, node_id: int, title: str, description: str,
                     body_md: str, seed_sha256: str) -> None:
    """Écrit la prose du fichier ET son empreinte sur un nœud existant, projections
    comprises. Une seule écriture pour les deux appelants (le semis de démarrage et
    le geste de maintenance) : une empreinte posée sans la prose qu'elle décrit
    ferait croire au démarrage suivant que la base est à jour."""
    row = conn.execute(
        "UPDATE nodes SET props = props || jsonb_build_object("
        "      'title', %s::text, 'description', %s::text, 'body_md', %s::text, "
        "      'seed_sha256', %s::text, 'embed_dirty', TRUE), "
        "  updated_at = NOW() "
        f"WHERE id = %s RETURNING {_COLS}",
        (title, description, body_md, seed_sha256, node_id),
    ).fetchone()
    _maintain_projections(conn, row, body_md)


def aligner_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                     title: str = "", description: str = "", *,
                     seed_sha256: str) -> bool:
    """Aligne INCONDITIONNELLEMENT une couche sur le fichier, empreinte comprise.

    ⚠️ Écrase ce que la base porte — c'est le geste de maintenance
    (`scripts/aligner_guides_plateforme.py`), joué à la main, après avoir MONTRÉ
    l'écart. Le démarrage, lui, ne fait jamais ça : il conserve et signale.
    `False` = aucune ligne à ce slug."""
    with _connect() as conn:
        courant = conn.execute(
            f"SELECT id FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = 'on-demand' FOR UPDATE",
            (scope, str(owner_id), slug),
        ).fetchone()
        if courant is None:
            return False
        _ecrire_le_semis(conn, courant["id"], title, description, body_md, seed_sha256)
        return True


class GuideConflict(Exception):
    """L'édition optimiste d'un guide a échoué : il a changé depuis la lecture de
    l'appelant (rev attendue ≠ rev courante) — le pendant de `DocConflict`."""
    def __init__(self, current_rev: str):
        self.current_rev = current_rev
        super().__init__("guide modifié depuis la lecture")


def patch_guide_db(scope: str, owner_id: str, slug: str, appliquer, *,
                   expected_rev: Optional[str] = None,
                   edited_by: Optional[str] = None) -> Optional[dict]:
    """Réécrit le CORPS d'un guide ON-DEMAND existant à partir de son corps actuel :
    `appliquer(corps actuel) -> nouveau corps`. Titre et description ne bougent pas.

    Lecture et écriture dans la MÊME transaction, ligne verrouillée (`FOR UPDATE`) :
    l'édition partielle d'un guide est un lire-modifier-écrire, et deux ajouts
    simultanés au même journal ne doivent pas se perdre l'un l'autre — le second
    attend le premier et s'applique à ce qu'il a écrit. `expected_rev` (le `rev` lu
    par l'appelant) ajoute le contrôle optimiste : `GuideConflict` si le guide a
    changé depuis, sous le même verrou, donc sans fenêtre entre contrôle et écriture.

    `appliquer` peut lever (cible introuvable, corps trop lourd…) : rien n'est alors
    écrit. Renvoie la ligne écrite, ou **`None` s'il n'y a pas de guide on-demand à
    cette clé** — jamais de création ici, et une couche `init` n'est pas touchée.

    Le corps remplacé est gardé comme version (`guide_revisions`, raison `write`)."""
    with _connect() as conn:
        courant = conn.execute(
            f"SELECT {_COLS} FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = 'on-demand' FOR UPDATE",
            (scope, str(owner_id), slug),
        ).fetchone()
        if courant is None:
            return None
        rev = guide_rev(courant["title"], courant["description"], courant["body_md"])
        if expected_rev is not None and expected_rev != rev:
            raise GuideConflict(rev)
        body_md = appliquer(courant["body_md"])
        # La prose seule, comme `set_guide_db` à la mise à jour : `embed_dirty` suit
        # le corps, les projections sont refaites dans la transaction.
        row = conn.execute(
            "UPDATE nodes SET props = props || jsonb_build_object("
            "      'body_md', %s::text, 'embed_dirty', TRUE), "
            "  updated_at = NOW() "
            f"WHERE id = %s RETURNING {_COLS}",
            (body_md, courant["id"]),
        ).fetchone()
        _garder_si_remplace(conn, dict(courant), dict(row), "on-demand", "write",
                            edited_by)
        _maintain_projections(conn, row,
                              body_md if body_md != courant["body_md"] else None)
        return dict(row)


def delete_guide_db(scope: str, owner_id: str, slug: str, *,
                    expected_rev: Optional[str] = None,
                    edited_by: Optional[str] = None) -> bool:
    """Supprime un guide ON-DEMAND. `expected_rev` (signal #1203) : `GuideConflict` si
    le guide a changé depuis la lecture de l'appelant — on ne supprime pas ce qu'on
    n'a pas lu. Rien n'est alors supprimé.

    Le guide supprimé est gardé comme version (`guide_revisions`, raison `delete`) :
    il se relit et se restaure par sa clé. D'où le verrou pris à CHAQUE suppression,
    garde ou pas — l'état gardé est celui que la suppression emporte."""
    with _connect() as conn:
        _, avant = _exiger_la_version(conn, scope, owner_id, slug, "on-demand",
                                      expected_rev)
        cur = conn.execute(
            f"DELETE FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = 'on-demand'",
            (scope, str(owner_id), slug),
        )
        supprime = (cur.rowcount or 0) > 0
        if supprime:
            _garder_si_remplace(conn, avant, None, "on-demand", "delete", edited_by)
        return supprime


# --- Init (readme injecté au handshake) : delivery='init' UNIQUEMENT ----------

def get_init_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    """Le readme INIT d'un (scope, owner, slug), ou None. `{body_md, updated_at, …}`."""
    return _get_one(scope, owner_id, slug, "init")


def set_init_guide_db(scope: str, owner_id: str, slug: str,
                      body_md: str, *, expected_rev: Optional[str] = None,
                      edited_by: Optional[str] = None,
                      reason: str = "init") -> Optional[dict]:
    """Upsert d'un readme INIT (édition admin/org/user). Corps vide = readme effacé,
    la ligne reste (comme les ex-tables).

    **Renvoie `None` quand la clé est déjà occupée par une couche `on-demand`** — rien
    n'a alors été écrit. Symétrique de `set_guide_db` : la même clé dérivée sert les
    deux livraisons, donc le sens qui n'a pas été signalé le 09/09 est tout aussi
    ouvert. Il est plus étroit (les slugs d'init sont canoniques : `readme` pour
    org/group/user, la clé du bloc pour la plateforme), mais un guide on-demand nommé
    `readme` rendrait sinon le readme injecté de ce périmètre silencieusement
    remplaçable par son écriture — et réciproquement.

    **Lève `GuideConflict` si `expected_rev` n'est pas le rev en place** (signal #1203 :
    le readme d'une org posé par un tiers pendant un onboarding, remplacé sans que
    personne ne le voie) — rien n'est alors écrit. `REV_ABSENT` = « il n'y a rien »,
    ce que rend la lecture d'un readme vide. La ligne rendue porte `previous_rev`.

    Le readme remplacé est gardé comme version (`guide_revisions`, raison `reason` :
    `init`, `delete` quand on le vide, `restore`), au nom de `edited_by`."""
    with _connect() as conn:
        # Le verrou du slug est pris à CHAQUE écriture, garde ou pas : sans lui, une
        # écriture non gardée se glisserait entre le contrôle d'une écriture gardée
        # et son upsert, sur une clé encore vide (aucune ligne à verrouiller).
        previous_rev, avant = _exiger_la_version(conn, scope, owner_id, slug, "init",
                                                 expected_rev)
        previous_body = _body_before_write(conn, scope, owner_id, slug)
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'init', "
            "                           'body_md', %s::text)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET "
            "  props = nodes.props || jsonb_build_object('body_md', %s::text), "
            "  updated_at = NOW() "
            "  WHERE nodes.props->>'delivery' = 'init' "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug, scope, str(owner_id),
             slug, body_md or "", body_md or ""),
        ).fetchone()
        if row is None:                       # une couche `on-demand` occupe la clé
            return None
        _garder_si_remplace(conn, avant, dict(row), "init", reason, edited_by)
        _maintain_projections(conn, row, (body_md or "") if body_md != previous_body else None)
        return {**dict(row), "previous_rev": previous_rev}


def contexte_non_lu(sub: str, org_id: int,
                    couches: list[tuple[str, str, str]]) -> Optional[str]:
    """Le NOM de l'org quand son contexte est à rappeler à `sub`, sinon `None`
    (oto-backend#1041) — UNE requête.

    À rappeler = l'org porte un readme d'init NON VIDE, et `sub` n'a pas lu
    `oto_context` sous cette org (journal `tool_calls`, appel réussi) depuis la
    dernière modification d'une des `couches` cumulées `[(scope, owner_id, slug)]`
    (org, équipe active, note de l'utilisateur) qui porte un corps. Une org sans
    readme ne déclenche rien, quelles que soient les autres couches.

    Tout vient de la base — aucun état de session : le verdict vaut sur tous les
    clients, et suit un readme réécrit en cours de session. Coût : les couches par
    l'index d'identité (`public_id`), le journal par `idx_tool_call_log_tool` (les
    lignes `oto_context` sont une fraction du journal), borné par la date de référence."""
    if not couches:
        return None
    pids = ", ".join([_PID] * len(couches))
    params: list = [x for c in couches for x in (c[0], str(c[1]), c[2])]
    with _connect() as conn:
        row = conn.execute(
            f"""
            WITH ref AS (
                SELECT max(updated_at) AS at,
                       bool_or(owner_type = 'org') AS org_a_un_readme
                FROM nodes
                WHERE public_id IN ({pids}) AND props->>'delivery' = 'init'
                  AND COALESCE(props->>'body_md', '') ~ '\\S'
            )
            SELECT o.name
            FROM ref JOIN orgs o ON o.id = %s
            WHERE ref.org_a_un_readme
              AND NOT EXISTS (
                  SELECT 1 FROM tool_calls l
                  WHERE l.tool = 'oto_context' AND l.kind = %s AND l.ok
                    AND l.sub = %s AND l.org_id = %s AND l.created_at >= ref.at)
            """,
            # `kind` du JOURNAL (face d'appel), passé en paramètre : cette façade ne
            # nomme qu'un seul genre de NŒUD, `{_KIND}` (test_nodes_m1_migration).
            (*params, int(org_id), "mcp", sub, int(org_id)),
        ).fetchone()
    if not row:
        return None
    return row["name"] or f"#{org_id}"


def seed_init_guide_db(scope: str, owner_id: str, slug: str, body_md: str) -> None:
    """Pose le défaut d'un readme INIT s'il n'existe pas (boot, idempotent). Ne touche
    JAMAIS une ligne déjà éditée."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'init', "
            "                           'body_md', %s::text)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO NOTHING RETURNING id",
            (scope, str(owner_id), slug, scope, str(owner_id), slug, body_md or ""),
        ).fetchone()
        if row is not None:
            _maintain_projections(conn, row, body_md or "")
