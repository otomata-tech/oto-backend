"""Les procédures vues du plan GOUVERNANCE (ADR 0030) : identité, circulation, inventaires.

`instructions.py` sert l'autre plan, celui du CONTENU : une procédure qu'on lit, écrit,
versionne et archive à la clé `(owner_type, owner_id, slug)`. Ici c'est la procédure
comme **ressource possédée** — désignée par son `id` surrogate (ADR 0032 « stop using
slug »), qui CHANGE de propriétaire, et qui s'ÉNUMÈRE pour être gouvernée. Ces fonctions
alimentent le kind procédure d'`ownership.py` (`TYPE_RESSOURCE_PROCEDURE`), la cascade de livraison d'un projet
(`oto_resource`) et la vue opérateur plateforme.

Les deux plans ne se confondent pas, et c'est tout l'intérêt de la séparation : lire le
CORPS d'une procédure et pouvoir la DÉPLACER sont deux droits distincts (ADR 0030,
`can_access` vs `can_govern`). Un fichier par plan rend la frontière visible au lieu de
la laisser à un commentaire de section.

Découpé le 01/09/2026 (issue `oto`#27) : `instructions.py` était à 499 lignes pour un
plafond de 500, et le lot d'avant avait raboté sa prose pour y tenir — un module qu'on
ne peut plus commenter est un module qu'on ne peut plus corriger. La couture était déjà
écrite dans le fichier, en bannière de section ; elle est devenue un fichier.
**Déplacement pur** : aucun corps de fonction n'a changé, la surface `org_store.<fn>`
est identique, aucun appelant n'a bougé.

Le RENOMMAGE vit ici aussi (issue `oto`#261) : changer le slug sans toucher à l'`id`
est un geste d'identité, frère du déplacement. Il réécrit en SQL direct ce qui, dans le
runner, désigne une procédure par son slug (`runner_triggers`, `runner_fleets`, et la
charge des travaux en attente `runner_jobs.payload`) pour le faire SUIVRE
le nouveau nom dans la même transaction : un agent planifié ne cherche jamais un nom
disparu.

Étage 1 du DAG du package, comme `library` : dépend d'`instructions`, et de lui seul.
Les références croisées passent par `instructions.<nom>` et jamais par un import à plat —
c'est ce qui fait qu'un `monkeypatch.setattr(org_store, …)` les atteint (cf. `_Facade`).
"""
from __future__ import annotations

from typing import Optional

from . import instructions
from ..db import _connect
from ..db.guides import exiger_aucun_guide, guide_du_slug, verrouiller_le_slug


_COLONNES_PAR_ID = ("id, org_id, owner_type, owner_id, slug, title, description, body_md, "
                    "slots, version, set_by, created_at, updated_at, archived_at")


def get_instruction_by_id(instruction_id: int) -> Optional[dict]:
    """Une instruction par son id surrogate (identité publique). None si absente.
    Porte `archived_at` comme la lecture par slug (#857) : cette lecture ne filtre pas
    l'archivage non plus, donc elle doit pouvoir dire l'état."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLONNES_PAR_ID} FROM org_instructions WHERE id = %s",
            (instruction_id,),
        ).fetchone()
        return dict(row) if row else None


def get_instructions_by_ids(instruction_ids: list[int]) -> dict[int, dict]:
    """`get_instruction_by_id` pour plusieurs ids, en une requête : `{id: instruction}`,
    les absentes n'ont pas d'entrée."""
    if not instruction_ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLONNES_PAR_ID} FROM org_instructions WHERE id = ANY(%s)",
            (sorted({int(i) for i in instruction_ids}),),
        ).fetchall()
    return {int(r["id"]): dict(r) for r in rows}


def _free_instruction_slug(conn, owner_type: str, owner_id: int | str, slug: str) -> str:
    """Slug libre chez `(owner_type, owner_id)` : le slug tel quel, sinon suffixé
    (-2, -3…). On ne remplace JAMAIS une procédure existante de la cible.

    ⚠️ La sonde porte sur la clé d'unicité RÉELLE `(owner_type, owner_id, slug)`.
    Jusqu'au 31/08/2026 elle sondait `owner_type='org' AND org_id=%s` : tant que
    `owner_id = org_id::text` les deux coïncident, mais une seule ligne d'un autre
    palier (ou d'une autre org parente) suffisait à faire répondre « libre » — et
    l'`ON CONFLICT DO UPDATE` qui suit ÉCRASAIT la procédure en place, sans un mot.

    Les RÉVISIONS sont sondées aussi : elles portent la même clé (+ version), donc un
    slug libre côté table vivante mais pris côté historique ferait échouer l'insertion
    du snapshot — et un déplacement ne peut pas emmener son historique sur une
    collision.

    Un slug porté par un GUIDE à charger de la cible n'est pas libre non plus (oto#100)."""
    otype, oid = instructions._owner(owner_type, owner_id)
    candidate = slug
    for i in range(2, 100):
        taken = conn.execute(
            f"SELECT 1 FROM org_instructions WHERE {instructions._OWNER_WHERE} AND slug = %s "
            "UNION ALL "
            f"SELECT 1 FROM org_instruction_revisions WHERE {instructions._OWNER_WHERE} "
            "AND slug = %s "
            "LIMIT 1",
            (otype, oid, candidate) * 2,
        ).fetchone()
        # Libre = libre dans les DEUX familles (oto#100) : un guide à charger du même
        # slug ferait refuser la copie, ou coexister le déplacement avec lui.
        if taken is None and guide_du_slug(otype, oid, candidate, conn) is None:
            return candidate
        candidate = f"{slug}-{i}"
    raise ValueError(f"aucun slug libre dérivé de `{slug}` chez {owner_type} {owner_id}")


def copy_instruction_to_owner(instruction_id: int, owner_type: str, owner_id: int | str,
                              set_by: Optional[str] = None) -> dict:
    """Copie une procédure chez un AUTRE propriétaire (livraison par transfert de
    projet, #52) : nouvelle procédure v1 chez la cible (slug suffixé si pris — jamais
    d'écrasement), l'originale reste intacte chez la source. Renvoie
    {id, slug, owner_type, owner_id, org_id} de la copie."""
    otype, oid = instructions._owner(owner_type, owner_id)
    src = get_instruction_by_id(instruction_id)
    if src is None:
        raise ValueError(f"procédure #{instruction_id} introuvable")
    with _connect() as conn:
        dest_slug = _free_instruction_slug(conn, otype, oid, src["slug"])
    instructions.set_instruction(otype, oid, dest_slug, src["body_md"],
                                 title=src.get("title"),
                                 description=src.get("description"),
                                 set_by=set_by, slots=src.get("slots") or [])
    created = instructions.get_instruction(otype, oid, dest_slug)
    return {"id": created["id"], "slug": dest_slug, "owner_type": otype,
            "owner_id": oid, "org_id": created["org_id"]}


def instruction_slug_libre(owner_type: str, owner_id: int | str, slug: str) -> bool:
    """Le slug est-il libre TEL QUEL chez `(owner_type, owner_id)` — table vivante,
    historique et guides compris ? La sonde de `move_instruction`, lue AVANT de
    déplacer : un déplacement qui suffixerait le slug changerait ce que ses agents lisent."""
    with _connect() as conn:
        return _free_instruction_slug(conn, owner_type, owner_id, slug) == slug


def move_instruction(instruction_id: int, new_owner_type: str,
                     new_owner_id: int | str) -> str:
    """DÉPLACE une procédure d'un palier à l'autre — org ↔ équipe (#681).

    L'`id` surrogate NE BOUGE PAS : c'est lui que `project_links.target_ref` et
    `resource_grants.resource_id` désignent, donc c'est lui qui fait survivre le lien
    de projet et les partages au déplacement. Les RÉVISIONS suivent dans la MÊME
    transaction : une procédure et son historique ne se séparent pas (le chemin
    précédent les déplaçait dans une seconde connexion et, sur collision, laissait
    l'historique chez la source en n'écrivant qu'un warning — 26 versions perdues de
    vue pour un slug déjà pris). Slug suffixé si pris chez la cible (sur la table
    vivante ET l'historique). Renvoie le slug final."""
    otype, oid = instructions._owner(new_owner_type, new_owner_id)
    src = get_instruction_by_id(instruction_id)
    if src is None:
        raise ValueError(f"procédure #{instruction_id} introuvable")
    prev = (str(src["owner_type"]), str(src["owner_id"]))
    if prev == (otype, oid):
        return src["slug"]
    with _connect() as conn:
        with conn.transaction():
            org_id = instructions._parent_org_id(conn, otype, oid)
            dest_slug = _free_instruction_slug(conn, otype, oid, src["slug"])
            conn.execute(
                "UPDATE org_instruction_revisions SET owner_type = %s, owner_id = %s, "
                f"org_id = %s, slug = %s WHERE {instructions._OWNER_WHERE} AND slug = %s",
                (otype, oid, org_id, dest_slug, prev[0], prev[1], src["slug"]),
            )
            cur = conn.execute(
                "UPDATE org_instructions SET owner_type = %s, owner_id = %s, org_id = %s, "
                "slug = %s, updated_at = NOW() WHERE id = %s",
                (otype, oid, org_id, dest_slug, instruction_id),
            )
            if (cur.rowcount or 0) != 1:
                raise ValueError(f"procédure #{instruction_id} introuvable")
    return dest_slug


def rename_instruction(owner_type: str, owner_id: int | str, slug: str,
                       new_slug: str) -> Optional[dict]:
    """RENOMME une procédure sans toucher à son identité (issue `oto`#261).

    L'`id` surrogate ne bouge pas — c'est lui que `project_links.target_ref`,
    `resource_grants.resource_id` et le nœud dérivé (`db/nodes`, lot ⑧) désignent —,
    la version non plus : changer le nom n'est pas écrire le contenu. Les RÉVISIONS
    suivent dans la MÊME transaction, comme au déplacement (`move_instruction`) : une
    procédure et son historique ne se séparent pas. L'entrée de bibliothèque publiée
    depuis elle garde sa trace (`guide_library.source_slug`), et le RUNNER suit le
    nouveau nom dans la même transaction (`_repointer_le_runner`) : ses déclencheurs,
    campagnes et travaux en attente la désignent par SLUG, pas par id.

    Pas d'alias : l'ancien slug ne résout plus, et redevient libre. Rend
    `{id, slug, previous_slug, runner}`, ou `None` si la procédure n'existe pas.
    Lève `instructions.InstructionExists` sur un slug pris (rien n'est écrasé),
    `db.SlugDeLAutreFamille` sur un slug qu'un guide à charger porte (oto#100),
    `ValueError` sur un slug vide, identique ou réservé. Une procédure ARCHIVÉE se renomme aussi : c'est un geste
    de gouvernance, pas une écriture sur une consigne retirée."""
    otype, oid = instructions._owner(owner_type, owner_id)
    old, new = instructions.normalize_slug(slug), instructions.normalize_slug(new_slug)
    if not old or not new:
        raise ValueError("slug et nouveau slug requis")
    if old == new:
        raise ValueError(f"`{new}` est déjà le slug de cette procédure")
    if instructions.BASE_SLUG in (old, new):
        raise ValueError(f"`{instructions.BASE_SLUG}` est le readme, pas une procédure")
    with _connect() as conn:
        with conn.transaction():
            # Les DEUX clés sous verrou, dans un ordre fixe : l'ancienne contre une
            # écriture concurrente, la nouvelle contre une création concurrente.
            for s in sorted((old, new)):
                verrouiller_le_slug(conn, otype, oid, s)
            cur = conn.execute(
                "SELECT id, org_id FROM org_instructions "
                f"WHERE {instructions._OWNER_WHERE} AND slug = %s", (otype, oid, old),
            ).fetchone()
            if cur is None:
                return None
            pris = conn.execute(
                "SELECT version, archived_at FROM org_instructions "
                f"WHERE {instructions._OWNER_WHERE} AND slug = %s", (otype, oid, new),
            ).fetchone()
            if pris is not None:
                raise instructions.InstructionExists(new, pris["version"],
                                                     pris["archived_at"] is not None)
            # Ni un guide à charger du même slug dans la même portée (oto#100).
            exiger_aucun_guide(conn, otype, oid, new)
            runner = _repointer_le_runner(conn, otype, oid, cur["org_id"], old, new)
            conn.execute(
                "UPDATE org_instruction_revisions SET slug = %s "
                f"WHERE {instructions._OWNER_WHERE} AND slug = %s", (new, otype, oid, old))
            conn.execute(
                "UPDATE org_instructions SET slug = %s, updated_at = NOW() WHERE id = %s",
                (new, cur["id"]))
            if otype == "org":
                conn.execute(
                    "UPDATE guide_library SET source_slug = %s "
                    "WHERE source_org_id = %s AND source_slug = %s", (new, int(oid), old))
    return {"id": cur["id"], "slug": new, "previous_slug": old, "runner": runner}


def _portee_runner(otype: str, oid: str, org_id: Optional[int],
                   slug: str) -> tuple[str, tuple]:
    """Les lignes du runner (alias `r` : `org_id`, `sub`) pour lesquelles `slug`
    RÉSOUT vers cette procédure-ci — la cascade de lecture de l'agent qu'elles
    lancent : sa procédure personnelle d'abord, celle de l'org ensuite, l'équipe en
    dernier. Une ligne dont le slug résout vers une AUTRE procédure ne suit pas."""
    perso = ("NOT EXISTS (SELECT 1 FROM org_instructions p WHERE p.owner_type = 'user' "
             "AND p.owner_id = r.sub AND p.slug = %s)")
    if otype == "user":
        return "r.sub = %s", (oid,)
    if otype == "org":
        return f"r.org_id = %s AND {perso}", (int(oid), slug)
    return (f"r.org_id = %s AND {perso} AND NOT EXISTS (SELECT 1 FROM org_instructions "
            "p WHERE p.owner_type = 'org' AND p.owner_id = %s AND p.slug = %s)",
            (org_id, slug, str(org_id), slug))


def _repointer_le_runner(conn, otype: str, oid: str, org_id: Optional[int],
                         old: str, new: str) -> dict:
    """Le runner SUIT le renommage, dans la transaction de l'appelant (#261).

    Trois porteurs désignent une procédure par son slug : `runner_triggers.procedure`,
    `runner_fleets.procedure` et la charge des travaux (`runner_jobs.payload`). Leur
    instruction de départ (`input`) le cite aussi — dérivée par `_instruction`, sous
    la forme `` `slug` ``, que l'on réécrit. Un travail DÉJÀ PRIS (`claimed`) tourne
    avec ce qu'il a lu : on ne le touche pas, on le NOMME (`in_flight_jobs`). Une
    instruction qui cite encore l'ancien slug hors de cette forme est nommée aussi
    (`inputs_to_review`) plutôt que devinée.

    La procédure d'une campagne est figée à sa déclaration (`CHAMPS_MODIFIABLES`), pour
    que l'attribution des lignes écrites reste vraie : ce gel porte sur l'OBJET, que
    le renommage ne change pas — même id, même contenu, même version."""
    portee, args = _portee_runner(otype, oid, org_id, old)
    motif = f"(^|[^a-z0-9_-]){old}([^a-z0-9_-]|$)"
    ancien, neuf = f"`{old}`", f"`{new}`"
    out: dict = {"inputs_to_review": []}
    for table, cle in (("runner_triggers", "triggers"), ("runner_fleets", "fleets")):
        rows = conn.execute(
            f"UPDATE {table} r SET procedure = %s, input = replace(r.input, %s, %s) "
            f"WHERE r.procedure = %s AND {portee} "
            "RETURNING r.id, COALESCE(r.input ~ %s, FALSE) AS a_reprendre",
            (new, ancien, neuf, old, *args, motif)).fetchall()
        out[cle] = sorted(r["id"] for r in rows)
        out["inputs_to_review"] += [f"{cle}:{r['id']}" for r in rows if r["a_reprendre"]]
    rows = conn.execute(
        "UPDATE runner_jobs r SET payload = jsonb_set(r.payload, '{procedure}', "
        "to_jsonb(%s::text)) || CASE WHEN jsonb_typeof(r.payload->'input') = 'string' "
        "THEN jsonb_build_object('input', replace(r.payload->>'input', %s, %s)) "
        "ELSE '{}'::jsonb END "
        f"WHERE r.status IN ('pending', 'held') AND r.payload->>'procedure' = %s "
        f"AND {portee} "
        "RETURNING r.id, COALESCE((r.payload->>'input') ~ %s, FALSE) AS a_reprendre",
        (new, ancien, neuf, old, *args, motif)).fetchall()
    out["jobs"] = sorted(r["id"] for r in rows)
    out["inputs_to_review"] += [f"jobs:{r['id']}" for r in rows if r["a_reprendre"]]
    out["in_flight_jobs"] = [r["id"] for r in conn.execute(
        "SELECT r.id FROM runner_jobs r WHERE r.status = 'claimed' "
        f"AND r.payload->>'procedure' = %s AND {portee} ORDER BY r.id",
        (old, *args)).fetchall()]
    return out


def list_instructions_for_owners(owners: list[tuple[str, str]]) -> list[dict]:
    """Procédures (hors base) des propriétaires donnés — plan GOUVERNANCE
    (métadonnées + propriétaire, sans body). Alimente
    `oto_resource(op=list, resource_type='procedure')`."""
    if not owners:
        return []
    clause = " OR ".join([f"({instructions._OWNER_WHERE})"] * len(owners))
    params: tuple = tuple(str(x) for pair in owners for x in pair) + (instructions.BASE_SLUG,)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, owner_type, owner_id, slug, title, description, "
            f"version, updated_at FROM org_instructions WHERE ({clause}) AND slug <> %s "
            # Archivée = hors service : elle ne se propose plus comme ressource
            # à lier à un projet.
            "AND archived_at IS NULL ORDER BY owner_type, owner_id, slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_instructions() -> list[dict]:
    """Toutes les procédures nommées, TOUS paliers (vue opérateur plateforme —
    gouvernance). Le filtre `owner_type='org'` d'avant #681 cachait à l'opérateur
    exactement les lignes qu'il est là pour voir."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, owner_type, owner_id, slug, title, description, "
            "version, updated_at FROM org_instructions WHERE slug <> %s "
            "ORDER BY org_id, owner_type, owner_id, slug",
            (instructions.BASE_SLUG,),
        ).fetchall()
        return [dict(r) for r in rows]
