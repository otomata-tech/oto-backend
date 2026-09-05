"""Les PROCÉDURES (table `org_instructions`) : guide versionné, possédé par un SCOPE.

Le modèle unifié servi par `oto_procedure` : lecture/écriture/recherche d'une
procédure, son historique de versions, et sa vie de **ressource possédée**
(ADR 0030 : id surrogate, copie et déplacement de propriétaire).

⚠️ **Un seul jeu de fonctions, keyé sur `(owner_type, owner_id)`** — la forme que
la table porte déjà (unicité vivante `(owner_type, owner_id, slug)`, sur la table
ET sur ses révisions). Jusqu'au 31/08/2026 il en existait DEUX : celui-ci filtrait
en dur `owner_type='org'`, `group_store` filtrait en dur `owner_type='group'`, et
les deux avaient déjà divergé (l'équipe écrivait `slots='[]'` en dur, ne servait
pas les slots et ignorait l'archivage). Un palier de plus par la même méthode
aurait fait un TROISIÈME jeu — cf. oto-backend#681.

`owner_type` accepté : `org` et `group` (les deux ont une org PARENTE, donc
`org_id` — dénormalisé, FK, NOT NULL — reste toujours renseigné). Le palier
personnel (`user`) est la phase 2 de #681 : il exige de relâcher cette colonne, ce
que ce module refuse explicitement plutôt que d'écrire une ligne bancale.

⚠️ À ne pas confondre avec `oto_mcp/instructions.py`, qui RÉSOUT les instructions
à l'appel ; ici c'est le store.

**Ce module sert le plan CONTENU** : la procédure qu'on lit, écrit, versionne et
archive, à la clé `(owner_type, owner_id, slug)`. La procédure comme **ressource
possédée** — identité par `id` surrogate, copie, déplacement, inventaires de
gouvernance (ADR 0030) — vit chez `instruction_ownership.py`, séparée le
01/09/2026 parce que ce fichier butait sur le plafond de 500 lignes. Les deux
plans étaient déjà distincts en DROITS (`can_access` vs `can_govern`) ; ils le
sont maintenant en fichiers.

Feuille du package : n'importe aucun de ses frères — ni `group_store`, qui dépend
de lui (l'org parente d'une équipe se lit en SQL direct sur `org_groups`, même
parti pris que l'invariant org↔groupe dans `members.py`).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..db import _connect


# --- instructions : guide de base + skills versionnés ----------------------
#
# Modèle unifié servi par oto_procedure(op='get') / oto_*_instruction(s). Le slug réservé
# BASE_SLUG ("claude_md") = le guide de base (servi d'office) ; les autres =
# des skills chargés à la demande. En clair (prose, hors coffre), lu à l'appel
# (pas de cache). Écriture = incrément de version + snapshot d'historique.

BASE_SLUG = "claude_md"
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")

# Les paliers de propriété qu'une procédure connaît. `user` est ouvert depuis le
# 04/09/2026 (ADR 0068, phase 2 de #681) : la colonne `org_id` est devenue nullable
# aux deux tables, une procédure personnelle y porte NULL — le fait, plutôt qu'une
# org qui ne la possède pas et dont la suppression l'emporterait.
OWNER_TYPES: tuple[str, ...] = ("org", "group", "user")

_OWNER_WHERE = "owner_type = %s AND owner_id = %s"


class InstructionExists(Exception):
    """Le slug visé porte DÉJÀ une procédure : une CRÉATION ne l'écrase pas (#662).
    L'écriture est un upsert depuis toujours (la version monte, l'état antérieur part
    en révision) — mais un client qui CRÉE, avec un slug fabriqué chez lui pour un
    agent neuf, n'attend pas de remplacer la procédure d'org qui portait ce nom. Il
    l'apprenait en relisant. D'où ce refus nommé. `archived` : slug pris par une
    procédure ARCHIVÉE, donc absente des listings — sans la nuance le refus semblerait
    porter sur rien, et écrire par-dessus ne désarchiverait pas la ligne (la
    « création » naîtrait invisible)."""

    def __init__(self, slug: str, version: int, archived: bool):
        self.slug, self.version, self.archived = slug, version, archived
        super().__init__(f"slug `{slug}` déjà pris (v{version})")


class InstructionVersionConflict(Exception):
    """Écriture optimiste refusée : la procédure a changé depuis la lecture du client.
    `current_version` vaut `None` quand elle n'existe pas (ou plus) : annoncer une
    version attendue, c'est affirmer avoir lu quelque chose, et l'absence dément cette
    lecture autant qu'un numéro différent. Même parti pris qu'ADR 0044 pour les
    instances de connecteur et qu'`expected_rev` côté pages (`db.DocConflict`) : le
    second écrivain relit et rejoue, il n'écrase pas."""

    def __init__(self, current_version: Optional[int]):
        self.current_version = current_version
        super().__init__("procédure modifiée depuis la lecture")


def normalize_slug(slug: str) -> str:
    """Slug canonique : minuscules, [a-z0-9_-], séparateurs compactés. '' si vide."""
    return _SLUG_RE.sub("-", (slug or "").strip().lower()).strip("-_")


def _owner(owner_type: str, owner_id: int | str) -> tuple[str, str]:
    """Valide la paire propriétaire et la rend sous la forme EXACTE des colonnes
    (`owner_id` est du TEXTE). Lève `ValueError` sur un palier inconnu — pas de repli
    silencieux vers l'org, qui écrirait la procédure chez quelqu'un d'autre."""
    otype = (owner_type or "").strip()
    if otype not in OWNER_TYPES:
        raise ValueError(
            f"owner_type `{owner_type}` inconnu — attendu : {' | '.join(OWNER_TYPES)}")
    oid = str(owner_id).strip()
    if not oid:
        raise ValueError("owner_id requis")
    return otype, oid


def _parent_org_id(conn, owner_type: str, owner_id: str) -> Optional[int]:
    """L'org PARENTE du propriétaire = la valeur d'`org_instructions.org_id`, colonne
    dénormalisée (FK vers `orgs`, porteuse de la cascade de suppression).

    Une org EST son org ; une équipe tient la sienne dans `org_groups` (`org_id` NOT
    NULL). Lu en SQL direct : `org_store` n'importe jamais `group_store` (cycle).

    ⚠️ **Une PERSONNE n'a pas d'org parente, et rend donc None** (ADR 0068). Y mettre
    son org de CONTEXTE serait plus commode et faux : la cascade de cette colonne
    ferait disparaître une procédure personnelle le jour où l'org est supprimée, alors
    qu'elle ne lui a jamais appartenu. Le NULL dit ce qui est."""
    if owner_type == "user":
        return None
    if owner_type == "org":
        return int(owner_id)
    row = conn.execute(
        "SELECT org_id FROM org_groups WHERE id = %s", (int(owner_id),)).fetchone()
    if row is None:
        raise ValueError(f"équipe #{owner_id} inconnue")
    return int(row["org_id"])


def _snippet(body: str, query: str, width: int = 200) -> str:
    """Extrait de `body` autour de la 1ʳᵉ occurrence de `query` (pour la recherche)."""
    i = body.lower().find(query.lower())
    if i < 0:
        return body[:width].strip()
    start = max(0, i - width // 3)
    end = min(len(body), i + len(query) + (2 * width) // 3)
    return ("…" if start else "") + body[start:end].strip() + ("…" if end < len(body) else "")


def get_instruction(owner_type: str, owner_id: int | str, slug: str,
                    version: Optional[int] = None) -> Optional[dict]:
    """Une PROCÉDURE (courante, ou une `version` archivée précise). None si absente.

    ⚠️ Ne sert plus le readme : `claude_md` était intercepté ici et servi depuis `guides`
    sous la FORME d'une instruction (compat de migration 0042). Le readme n'est pas une
    procédure — il se lit sur la surface guide (`guide_store.get_init_guide(scope, id)`,
    capacité `me.guides.*`). Un appel avec ce slug renvoie donc None.

    ⚠️ Une version archivée vient de la table des RÉVISIONS, qui ne porte ni `id` ni
    `updated_at` : la forme rendue est plus petite."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT id, org_id, owner_type, owner_id, slug, title, description, "
                "body_md, slots, version, set_by, created_at, updated_at "
                f"FROM org_instructions WHERE {_OWNER_WHERE} AND slug = %s",
                (otype, oid, slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT org_id, owner_type, owner_id, slug, title, description, "
                "body_md, slots, version, set_by, created_at "
                f"FROM org_instruction_revisions WHERE {_OWNER_WHERE} "
                "AND slug = %s AND version = %s",
                (otype, oid, slug, version),
            ).fetchone()
        return dict(row) if row else None


def list_instructions(owner_type: str, owner_id: int | str,
                      include_base: bool = False) -> list[dict]:
    """Métadonnées des instructions (SANS body) = l'index des skills. Exclut la
    guide de base sauf `include_base` (surface admin), et TOUJOURS les
    procédures archivées.

    Toujours, faute d'appelant qui veuille le contraire : le jour où une surface
    admin voudra les voir, elle ajoutera son paramètre avec son besoin sous les
    yeux. C'est le point de l'archivage : cette fonction alimente aussi bien
    l'index que l'IA lit (`instructions.skills_index_md`, qui enrichit la
    description d'`oto_procedure` au tools/list) que `oto_procedure op=list`.
    Une procédure retirée du service doit cesser d'être proposée à l'agent — un
    archivage qui la laisserait dans cet index ne serait qu'un habillage."""
    otype, oid = _owner(owner_type, owner_id)
    where = _OWNER_WHERE if include_base else _OWNER_WHERE + " AND slug <> %s"
    where += " AND archived_at IS NULL"
    params: tuple = (otype, oid) if include_base else (otype, oid, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, slug, title, description, version, updated_at "
            f"FROM org_instructions WHERE {where} ORDER BY slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_instruction_bodies(owner_type: str, owner_id: int | str) -> list[dict]:
    """Slug + body_md des instructions d'un propriétaire (hors guide de base) — pour
    dériver les références d'outils `<tool:slug>` (compteur « guide-only », ADR 0024)."""
    otype, oid = _owner(owner_type, owner_id)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT slug, body_md FROM org_instructions WHERE {_OWNER_WHERE} "
            "AND slug <> %s",
            (otype, oid, BASE_SLUG),
        ).fetchall()
        return [dict(r) for r in rows]


def search_instructions(owner_type: str, owner_id: int | str, query: str,
                        include_base: bool = False) -> list[dict]:
    """Recherche substring (title/description/body) dans les instructions du scope.
    Renvoie les métadonnées + un `snippet` ; le body complet passe par get_instruction."""
    otype, oid = _owner(owner_type, owner_id)
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    # Archivées exclues sans option d'inclusion : chercher, c'est chercher ce qui
    # est en service (même raison que `list_instructions`).
    base_filter = "AND archived_at IS NULL " + ("" if include_base else "AND slug <> %s ")
    head: tuple = (otype, oid) if include_base else (otype, oid, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, slug, title, description, body_md, version, updated_at "
            f"FROM org_instructions WHERE {_OWNER_WHERE} " + base_filter +
            "AND (title ILIKE %s OR description ILIKE %s OR body_md ILIKE %s) "
            "ORDER BY (title ILIKE %s) DESC, (description ILIKE %s) DESC, updated_at DESC",
            head + (like, like, like, like, like),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = _snippet(d.pop("body_md", "") or "", q)
        out.append(d)
    return out


def set_instruction(owner_type: str, owner_id: int | str, slug: str, body_md: str,
                    title: Optional[str] = None, description: Optional[str] = None,
                    set_by: Optional[str] = None, slots: Optional[list] = None,
                    must_create: bool = False,
                    expected_version: Optional[int] = None) -> int:
    """Crée/met à jour une instruction ; renvoie la NOUVELLE version et archive un
    snapshot. `title`/`description`/`slots` None = conserver l'existant ('' / [] à
    la création). `slots` = entités requises déclarées (ADR 0035, validées en amont
    par `slots.validate_slots`). Sérialisé par (owner, slug) via verrou advisory.

    Deux gardes anti-écrasement (#662), opt-in, vérifiées SOUS le verrou — qui
    sérialise deux écritures simultanées sans empêcher la seconde d'écraser :
    `must_create` veut le slug LIBRE (sinon `InstructionExists`, geste de création),
    `expected_version` la version que le client a lue (sinon
    `InstructionVersionConflict`, édition concurrente). Aucune par défaut : l'écriture
    nue reste l'upsert que la console MCP et le dashboard exercent depuis toujours. Le
    défaut corrigé est l'absence de tout moyen de NE PAS écraser, pas l'upsert."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    # Le readme vit dans `guides` (ADR 0042) et s'écrit sur la surface guide : cette
    # API-ci est celle des PROCÉDURES (slots, versions). Plus de redirection silencieuse.
    if slug == BASE_SLUG:
        raise ValueError(
            f"`{BASE_SLUG}` est le readme, pas une procédure — écris-le via la "
            f"surface guide (scope='{otype}', delivery='init').")
    with _connect() as conn:
        with conn.transaction():
            org_id = _parent_org_id(conn, otype, oid)
            # Verrou + arbitre sur la clé OWNER : l'unicité vivante est
            # (owner_type, owner_id, slug) — la PK legacy (org_id, slug) est tombée.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         (f"oi:{otype}:{oid}:{slug}",))
            cur = conn.execute(
                "SELECT version, title, description, slots, archived_at "
                f"FROM org_instructions WHERE {_OWNER_WHERE} AND slug = %s",
                (otype, oid, slug),
            ).fetchone()
            # Gardes anti-écrasement DANS la transaction verrouillée : entre un
            # pré-check hors verrou et l'INSERT, une écriture concurrente se glisse.
            if must_create and cur is not None:
                raise InstructionExists(slug, cur["version"],
                                        cur["archived_at"] is not None)
            if expected_version is not None and (
                    cur is None or cur["version"] != expected_version):
                raise InstructionVersionConflict(cur["version"] if cur else None)
            new_version = (cur["version"] + 1) if cur else 1
            new_title = title if title is not None else (cur["title"] if cur else "")
            new_desc = description if description is not None else (cur["description"] if cur else "")
            new_slots = json.dumps(slots if slots is not None
                                   else ((cur["slots"] if cur else None) or []))
            _write_version(conn, org_id, otype, oid, slug, version=new_version,
                           title=new_title, description=new_desc, body_md=body_md,
                           slots_json=new_slots, set_by=set_by)
            return new_version


def _write_version(conn, org_id: int, otype: str, oid: str, slug: str, *, version: int,
                   title: str, description: str, body_md: str, slots_json: str,
                   set_by: Optional[str]) -> None:
    """Pose UNE version : la ligne vivante + son snapshot d'historique, dans la
    transaction (verrouillée) de l'appelant.

    Les deux écritures ne se séparent pas — une ligne vivante sans sa révision, c'est
    une version qu'aucun `from_version` ne restaurera. Elles vivent donc ici, en un
    seul endroit, plutôt que recopiées par chaque geste d'écriture : `set_instruction`
    (le corps) et `set_instruction_meta` (la vitrine) écrivent la MÊME chose, à ceci
    près que le second reconduit le corps qu'il a lu."""
    conn.execute(
        """
        INSERT INTO org_instructions
            (org_id, owner_type, owner_id, slug, title, description, body_md, slots,
             version, set_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (owner_type, owner_id, slug) DO UPDATE SET
            title = EXCLUDED.title, description = EXCLUDED.description,
            body_md = EXCLUDED.body_md, slots = EXCLUDED.slots,
            version = EXCLUDED.version,
            set_by = EXCLUDED.set_by, updated_at = NOW()
        """,
        (org_id, otype, oid, slug, title, description, body_md, slots_json,
         version, set_by),
    )
    # Le rang se maintient DANS la transaction qui écrit le corps : le rattrapage
    # de fond ne repasse que sur les vecteurs absents, jamais sur un vecteur
    # présent mais périmé — un cache laissé là serait faux indéfiniment.
    from ..db.search import stamp_rank_vector
    stamp_rank_vector(conn, "org_instructions", _OWNER_WHERE + " AND slug = %s", (otype, oid, slug))
    conn.execute(
        """
        INSERT INTO org_instruction_revisions
            (org_id, owner_type, owner_id, slug, version, title, description,
             body_md, slots, set_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (org_id, otype, oid, slug, version, title, description, body_md,
         slots_json, set_by),
    )


def set_instruction_meta(owner_type: str, owner_id: int | str, slug: str, *,
                         title: Optional[str] = None,
                         description: Optional[str] = None,
                         set_by: Optional[str] = None,
                         expected_version: Optional[int] = None) -> Optional[int]:
    """Corrige le TITRE et/ou la DESCRIPTION d'une procédure **sans toucher au corps**.
    Renvoie la nouvelle version, ou `None` si la procédure n'existe pas (issue `oto`#27).

    Le geste qui manquait. `set_instruction` exige `body_md` : corriger la ligne que
    l'agent lit dans le catalogue pour CHOISIR sa procédure obligeait donc à repasser
    plusieurs milliers de caractères de prose qu'on ne voulait pas toucher — et une
    retranscription peut dégrader ce qu'elle recopie. Le coût s'est payé en procédures
    laissées avec une description périmée parce que le geste de correction était plus
    risqué que le défaut. Une carte périmée est pire que pas de carte : elle est lue
    avec confiance.

    ⚠️ **Verbe à part, et non `body_md` rendu facultatif sur `set_instruction`** —
    même parti pris que la CRÉATION (#662, cf. `InstrCreateInput`). `set` est
    l'écriture DU CORPS : y accepter un corps vide échangerait un refus bruyant
    (`body_md requis`) contre un glissement muet, où l'appelant qui voulait réécrire
    le corps et n'a rien produit repartirait avec une simple retouche de vitrine, et
    croirait avoir écrit.

    Le corps courant est RECONDUIT tel quel dans la nouvelle version, ainsi que les
    slots : chaque version reste un instantané complet, donc `from_version` restaure
    toujours un état cohérent. La version monte comme pour toute autre écriture — une
    correction de vitrine est une écriture, elle se voit dans l'historique et se
    défait pareil.

    `expected_version` : même verrou optimiste que `set_instruction`, vérifié SOUS le
    verrou advisory (`InstructionVersionConflict`). `None` des deux champs à la fois
    est refusé : une écriture qui ne change rien mais consomme une version est un
    défaut, pas un no-op."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if title is None and description is None:
        raise ValueError("title et/ou description requis")
    # Même frontière que `set_instruction` : le readme n'est pas une procédure.
    if slug == BASE_SLUG:
        raise ValueError(
            f"`{BASE_SLUG}` est le readme, pas une procédure — écris-le via la "
            f"surface guide (scope='{otype}', delivery='init').")
    with _connect() as conn:
        with conn.transaction():
            org_id = _parent_org_id(conn, otype, oid)
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         (f"oi:{otype}:{oid}:{slug}",))
            # Le corps et les slots sont LUS sous le verrou pour être reconduits :
            # les relire dehors les exposerait à une écriture concurrente glissée
            # entre la lecture et l'INSERT, qui ressusciterait un corps périmé.
            cur = conn.execute(
                "SELECT version, title, description, body_md, slots "
                f"FROM org_instructions WHERE {_OWNER_WHERE} AND slug = %s",
                (otype, oid, slug),
            ).fetchone()
            if cur is None:
                # Rien à décrire : il n'y a pas de création déguisée par ce chemin —
                # créer, c'est fournir un corps.
                return None
            if expected_version is not None and cur["version"] != expected_version:
                raise InstructionVersionConflict(cur["version"])
            new_version = cur["version"] + 1
            _write_version(
                conn, org_id, otype, oid, slug, version=new_version,
                title=title if title is not None else cur["title"],
                description=description if description is not None else cur["description"],
                body_md=cur["body_md"], slots_json=json.dumps(cur["slots"] or []),
                set_by=set_by)
            return new_version


def list_instruction_versions(owner_type: str, owner_id: int | str, slug: str) -> list[dict]:
    """Historique d'une procédure (métadonnées par version, plus récent d'abord).
    Le readme n'est pas une procédure et n'a pas d'historique (ADR 0042) → []."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    if slug == BASE_SLUG:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, title, set_by, created_at FROM org_instruction_revisions "
            f"WHERE {_OWNER_WHERE} AND slug = %s ORDER BY version DESC",
            (otype, oid, slug),
        ).fetchall()
        return [dict(r) for r in rows]


def archive_instruction(owner_type: str, owner_id: int | str, slug: str) -> bool:
    """Archive une procédure (soft-delete) : elle sort de tous les listings, la
    ligne et ses révisions restent. False si elle n'existait pas.

    Idempotent en pratique — ré-archiver rafraîchit l'horodatage plutôt que
    d'échouer, le résultat visé (« elle n'est plus en service ») étant déjà
    atteint. Pas de désarchivage sur cette surface : même choix que
    `db/projects.archive_project`, dont l'inverse n'existe pas non plus côté
    app. Ce qu'archiver garantit ici, c'est que RIEN n'est détruit — contrairement
    à `delete_instruction` juste en dessous, qui emporte l'historique."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE org_instructions SET archived_at = NOW(), updated_at = NOW() "
            f"WHERE {_OWNER_WHERE} AND slug = %s", (otype, oid, slug)
        )
        return (cur.rowcount or 0) > 0


def delete_instruction(owner_type: str, owner_id: int | str, slug: str) -> bool:
    """Supprime une instruction ET son historique. False si elle n'existait pas."""
    otype, oid = _owner(owner_type, owner_id)
    slug = normalize_slug(slug)
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                f"DELETE FROM org_instructions WHERE {_OWNER_WHERE} AND slug = %s",
                (otype, oid, slug),
            )
            removed = (cur.rowcount or 0) > 0
            conn.execute(
                f"DELETE FROM org_instruction_revisions WHERE {_OWNER_WHERE} AND slug = %s",
                (otype, oid, slug),
            )
    return removed
